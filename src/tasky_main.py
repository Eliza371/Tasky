#!/usr/bin/env python3
"""Tasky — Telegram bot that monitors and instantly notifies subscribers of
quick earning opportunities (crypto quests/bounties/airdrops, hackathons,
freelance/dev bounties).

Setup:
  1. pip install python-telegram-bot requests beautifulsoup4
  2. Put your BotFather token in the TASKY_TOKEN env var, or paste it into
     TOKEN below.
  3. python -c "from src.db import init; init()"
  4. python src/tasky_main.py

Requires python-telegram-bot v20+ (async API).
"""

import logging
import os

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# Load environment variables from a local .env file if present. This keeps the
# bot token out of source files. python-dotenv is optional — if it isn't
# installed we simply fall back to real environment variables.
# .env.local overrides .env (for local development secrets).
try:
    from dotenv import load_dotenv

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(project_root, ".env"))
    load_dotenv(os.path.join(project_root, ".env.local"), override=True)
except ImportError:
    pass

# Support running both as `python src/tasky_main.py` and `python -m src.tasky_main`.
try:
    from . import db
    from .scraper_full import scrape_all
except ImportError:  # run directly as a script
    import db
    from scraper_full import scrape_all

# --- Configuration ----------------------------------------------------------
# Prefer the environment variable; fall back to the literal below.
TOKEN = os.environ.get("TASKY_TOKEN", "PASTE_YOUR_BOTFATHER_TOKEN_HERE")

# How often to scrape sources, in seconds.
POLL_INTERVAL = int(os.environ.get("TASKY_POLL_INTERVAL", "300"))

# Chat id of the admin who may mint codes and grant/revoke access. 0 = unset,
# which disables all admin commands. Set TASKY_ADMIN_ID to your own chat id
# (the gate message tells any user their chat id).
ADMIN_ID = int(os.environ.get("TASKY_ADMIN_ID", "0") or "0")

# Max codes a single /gencode call may mint, to avoid accidental floods.
MAX_GENCODE = 20

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
# httpx logs every Telegram API request at INFO, and the request URL embeds the
# bot token (…/bot<TOKEN>/getUpdates). Quiet it to WARNING so the token never
# lands in the logs (Railway, files, stdout).
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("tasky")

# Shown to any chat without access that hits a gated command. `{chat_id}` is
# filled with the requester's own id; a Copy button is attached so they can
# copy the number with one tap and send it to the admin.
GATE_MESSAGE = (
    "🔒 Access required\n"
    "This bot is invite-only.\n\n"
    "If you have a code: /redeem YOUR_CODE\n"
    "If not: ask the admin to send you one. Your chat id is {chat_id}"
)

# Human-readable labels for each category (must match db.CATEGORIES keys).
CATEGORY_LABELS = {
    "crypto": "🪙 Crypto (quests/airdrops)",
    "hackathon": "💻 Hackathons",
    "bounty": "🎯 Bounties",
    "freelance": "💼 Freelance/Tasks",
    "creator": "🎨 Creator (UGC/content)",
    "internship": "🎓 Internships",
}


# --- Formatting -------------------------------------------------------------
def format_item(title, url, source, deadline=None):
    line = f"🔔 *{_escape(title)}*\n_{_escape(source)}_"
    if deadline:
        line += f"\n⏳ Deadline: {_escape(str(deadline))}"
    return f"{line}\n{url}"


def _escape(text):
    # Minimal Markdown escaping for user-facing text.
    for ch in ("_", "*", "[", "]", "`"):
        text = text.replace(ch, "\\" + ch)
    return text


# Telegram rejects messages longer than 4096 chars. Keep a margin.
_MAX_MSG = 3900


def _pack_blocks(blocks, sep="\n\n", limit=_MAX_MSG):
    """Pack text blocks into as few messages as possible, each under `limit`
    characters. A single block longer than `limit` is emitted on its own
    (Telegram will still reject it, but that's an unrealistic single task)."""
    messages = []
    current = ""
    for block in blocks:
        if not current:
            current = block
        elif len(current) + len(sep) + len(block) <= limit:
            current += sep + block
        else:
            messages.append(current)
            current = block
    if current:
        messages.append(current)
    return messages


def _copy_id_keyboard(chat_id):
    """An inline keyboard with a single button that copies the chat id to the
    user's clipboard on tap (Telegram's native copy_text button)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"📋 Copy chat id ({chat_id})",
            copy_text=CopyTextButton(text=str(chat_id)),
        )
    ]])


# --- Access gate ------------------------------------------------------------
async def _require_access(update: Update) -> bool:
    """Return True if the chat may proceed; otherwise reply with the gate
    message (showing the requester's own chat id) and return False."""
    chat_id = update.effective_chat.id
    if db.has_access(chat_id):
        return True
    await update.effective_message.reply_text(
        GATE_MESSAGE.format(chat_id=chat_id),
        reply_markup=_copy_id_keyboard(chat_id),
    )
    return False


def _is_admin(update: Update) -> bool:
    return ADMIN_ID != 0 and update.effective_chat.id == ADMIN_ID


# --- Category selection keyboard --------------------------------------------
def build_keyboard(selected):
    """Inline keyboard: a toggle button per category (✅ if selected), plus
    quick Select-all / Clear-all shortcuts and Done."""
    selected = set(selected)
    rows = []
    for cat in db.CATEGORIES:
        mark = "✅ " if cat in selected else "☐ "
        rows.append(
            [InlineKeyboardButton(mark + CATEGORY_LABELS[cat], callback_data=f"toggle:{cat}")]
        )
    all_selected = selected.issuperset(db.CATEGORIES)
    rows.append(
        [
            InlineKeyboardButton(
                "✅ Everything" if not all_selected else "☑️ All selected",
                callback_data="all",
            ),
            InlineKeyboardButton("🧹 Clear", callback_data="clear"),
        ]
    )
    rows.append([InlineKeyboardButton("💾 Done", callback_data="done")])
    return InlineKeyboardMarkup(rows)


# --- Command handlers -------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "👋 Welcome to Tasky!\n\n"
        "I monitor crypto quests, airdrops, bounties, hackathons and "
        "freelance tasks, and notify you the moment new ones appear.\n\n"
        f"🔒 This bot is invite-only. Your chat id is {chat_id} — tap the button "
        "below to copy it and send it to the admin to request access, or use "
        "/redeem YOUR_CODE if you have one.\n\n"
        "/redeem — unlock the bot with an access code\n"
        "/id — show your chat id again\n"
        "/subscribe — choose which categories to follow\n"
        "/mysubs — show your current categories\n"
        "/unsubscribe — stop all notifications\n"
        "/latest — show the 10 most recent finds",
        reply_markup=_copy_id_keyboard(chat_id),
    )


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    chat_id = update.effective_chat.id
    current = db.get_categories(chat_id)  # [] if not yet subscribed
    await update.message.reply_text(
        "Pick the categories you want. Tap to toggle, then press *Done*.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_keyboard(current),
    )


async def _safe_edit_markup(query, keyboard):
    """Update a message's inline keyboard, ignoring Telegram's harmless
    'Message is not modified' error when the keyboard is unchanged."""
    try:
        await query.edit_message_reply_markup(reply_markup=keyboard)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def on_category_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle taps on the category keyboard."""
    query = update.callback_query
    # Guard the keyboard too, so a non-granted chat can't toggle via an old message.
    if not db.has_access(query.message.chat_id):
        await query.answer("🔒 Access required — /redeem your code first.", show_alert=True)
        return
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    # Track the in-progress selection on the message itself via current DB state.
    current = set(db.get_categories(chat_id))

    if data == "done":
        if not current:
            await query.edit_message_text(
                "No categories selected — you won't get notifications. "
                "Send /subscribe to choose some."
            )
            db.remove_subscriber(chat_id)
            return
        labels = ", ".join(CATEGORY_LABELS[c] for c in db.CATEGORIES if c in current)
        await query.edit_message_text(f"✅ Subscribed to:\n{labels}")
        return

    if data == "all":
        current = set(db.CATEGORIES)
        db.add_subscriber(chat_id, list(db.CATEGORIES))
        await _safe_edit_markup(query, build_keyboard(current))
        return

    if data == "clear":
        current = set()
        db.remove_subscriber(chat_id)
        await _safe_edit_markup(query, build_keyboard(current))
        return

    if data.startswith("toggle:"):
        cat = data.split(":", 1)[1]
        if cat not in db.CATEGORIES:
            return
        if cat in current:
            current.discard(cat)
        else:
            current.add(cat)
        # Persist immediately so the toggle survives even without pressing Done.
        ordered = [c for c in db.CATEGORIES if c in current]
        if ordered:
            db.add_subscriber(chat_id, ordered)
        else:
            db.remove_subscriber(chat_id)
        await _safe_edit_markup(query, build_keyboard(current))


async def cmd_mysubs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    chat_id = update.effective_chat.id
    cats = db.get_categories(chat_id)
    if not cats:
        await update.message.reply_text("You're not subscribed. Send /subscribe to choose categories.")
        return
    labels = "\n".join(f"• {CATEGORY_LABELS[c]}" for c in db.CATEGORIES if c in cats)
    await update.message.reply_text(f"You're subscribed to:\n{labels}")


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    chat_id = update.effective_chat.id
    if db.remove_subscriber(chat_id):
        await update.message.reply_text("🔕 Unsubscribed from everything. Send /subscribe to resume.")
    else:
        await update.message.reply_text("You weren't subscribed.")


async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_access(update):
        return
    rows = db.get_new(limit=10)
    if not rows:
        await update.message.reply_text("Nothing yet — check back soon.")
        return
    # get_new returns full rows: id, title, url, source, type, currency, posted, notified, deadline
    lines = [format_item(r[1], r[2], r[3], r[8]) for r in rows]
    await update.message.reply_text(
        "\n\n".join(lines), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
    )


async def cmd_available(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all available tasks for the categories the user is subscribed to,
    grouped by category."""
    if not await _require_access(update):
        return
    chat_id = update.effective_chat.id
    cats = db.get_categories(chat_id)
    if not cats:
        await update.message.reply_text(
            "You haven't picked any categories yet. Send /subscribe to choose some."
        )
        return

    # Fetch a fair slice PER category, not one combined top-N. A single
    # ORDER BY posted DESC LIMIT N lets a bulk source (e.g. Pasiflora inserts
    # ~120 rows in one poll cycle with near-identical `posted`) swamp the list
    # and crowd every other category out of the cut. Querying each category
    # independently guarantees each one shows its own recent items.
    by_cat = {}
    for cat in cats:
        cat_rows = db.get_by_categories([cat], limit=15)
        if cat_rows:
            by_cat[cat] = cat_rows
    if not by_cat:
        picked = ", ".join(CATEGORY_LABELS[c] for c in db.CATEGORIES if c in cats)
        await update.message.reply_text(
            f"No tasks yet for your categories ({picked}). I'll notify you as they land."
        )
        return

    # Build a flat list of message "blocks" (a category header or a task item).
    # We then pack blocks into messages under Telegram's 4096-char limit so a
    # long list is split across several messages instead of failing to send.
    # Row layout: id, title, url, source, type, currency, posted, notified, deadline
    blocks = []
    for cat in db.CATEGORIES:
        items = by_cat.get(cat)
        if not items:
            continue
        blocks.append(f"*{_escape(CATEGORY_LABELS[cat])}* ({len(items)})")
        blocks.extend(format_item(it[1], it[2], it[3], it[8]) for it in items)

    for chunk in _pack_blocks(blocks):
        await update.message.reply_text(
            chunk,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )


# --- Access commands --------------------------------------------------------
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the user their chat id. Open to everyone."""
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"Your chat id is {chat_id}",
        reply_markup=_copy_id_keyboard(chat_id),
    )


async def cmd_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Redeem a single-use invite code. Open to everyone."""
    chat_id = update.effective_chat.id
    if db.has_access(chat_id):
        await update.message.reply_text("✅ You already have access. Send /subscribe to get started.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /redeem YOUR_CODE")
        return
    code = context.args[0]
    result = db.redeem_code(code, chat_id)
    if result == "ok":
        await update.message.reply_text(
            "🎉 Access granted! Send /subscribe to choose the categories you want."
        )
    elif result == "used":
        await update.message.reply_text("That code has already been used. Ask the admin for a fresh one.")
    else:  # invalid
        await update.message.reply_text("That's not a valid code. Double-check it or ask the admin.")


async def cmd_gencode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin only: mint one or more single-use invite codes."""
    if not _is_admin(update):
        return
    n = 1
    if context.args:
        try:
            n = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Usage: /gencode [count]")
            return
    n = max(1, min(n, MAX_GENCODE))
    codes = []
    for _ in range(n):
        code = db.gen_code()
        db.create_code(code)
        codes.append(code)
    header = "🎟 New invite code:" if n == 1 else f"🎟 {n} new invite codes:"
    await update.message.reply_text(header + "\n" + "\n".join(f"`{c}`" for c in codes),
                                    parse_mode=ParseMode.MARKDOWN)


async def cmd_codes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin only: list unused invite codes."""
    if not _is_admin(update):
        return
    codes = db.list_unused_codes()
    if not codes:
        await update.message.reply_text("No unused codes. Mint some with /gencode.")
        return
    await update.message.reply_text(
        f"Unused codes ({len(codes)}):\n" + "\n".join(f"`{c}`" for c in codes),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin only: grant access directly by chat id."""
    if not _is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /grant <chat_id>")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("chat_id must be a number.")
        return
    db.grant_access(target, "admin")
    await update.message.reply_text(f"✅ Access granted to {target}.")


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin only: revoke access (and stop the feed) by chat id."""
    if not _is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /revoke <chat_id>")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("chat_id must be a number.")
        return
    had = db.revoke_access(target)
    db.remove_subscriber(target)  # also stop the broadcast feed
    if had:
        await update.message.reply_text(f"❌ Access revoked for {target}.")
    else:
        await update.message.reply_text(f"{target} didn't have access.")


# --- Background polling job --------------------------------------------------
async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    """Scrape sources, store new items, broadcast unnotified ones."""
    log.info("Polling sources...")
    items = scrape_all()
    new_count = 0
    for it in items:
        if db.insert(
            it["title"], it["url"], it["source"], it["type"],
            it.get("currency", "USD/Crypto"), it.get("deadline"),
        ):
            new_count += 1
    log.info("Scraped %d items, %d new", len(items), new_count)

    subscribers = db.get_subscribers()  # list of (chat_id, [categories])
    unnotified = db.get_unnotified()
    if not unnotified:
        return

    for row in unnotified:
        # get_unnotified: id, title, url, source, type, currency, posted, deadline
        task_id, title, url, source, type_ = row[0], row[1], row[2], row[3], row[4]
        deadline = row[7]
        text = format_item(title, url, source, deadline)
        for chat_id, cats in subscribers:
            if type_ not in cats:
                continue  # this subscriber didn't opt into this category
            if not db.has_access(chat_id):
                continue  # access was revoked since they subscribed
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                log.warning("Failed to message %s: %s", chat_id, e)
        db.mark_notified(task_id)


# Command menus shown in Telegram's "/" autocomplete. Public commands are
# visible to everyone; admin commands are additionally shown only to ADMIN_ID.
PUBLIC_COMMANDS = [
    BotCommand("start", "Intro and your chat id"),
    BotCommand("id", "Show your chat id"),
    BotCommand("redeem", "Unlock the bot with an access code"),
    BotCommand("subscribe", "Choose which categories to follow"),
    BotCommand("mysubs", "Show your current categories"),
    BotCommand("unsubscribe", "Stop all notifications"),
    BotCommand("latest", "Show the 10 most recent finds"),
    BotCommand("available", "Show all available tasks for your subscriptions"),
]
ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    BotCommand("gencode", "Mint single-use invite codes"),
    BotCommand("codes", "List unused invite codes"),
    BotCommand("grant", "Grant access by chat id"),
    BotCommand("revoke", "Revoke access by chat id"),
]


async def _set_commands(app):
    """post_init hook: register the "/" command menus with Telegram."""
    await app.bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeDefault())
    if ADMIN_ID != 0:
        await app.bot.set_my_commands(
            ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=ADMIN_ID)
        )


def main():
    if TOKEN == "PASTE_YOUR_BOTFATHER_TOKEN_HERE":
        raise SystemExit(
            "No token configured. Set TASKY_TOKEN env var or edit TOKEN in src/tasky_main.py."
        )

    db.init()

    app = Application.builder().token(TOKEN).post_init(_set_commands).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("redeem", cmd_redeem))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("mysubs", cmd_mysubs))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("latest", cmd_latest))
    app.add_handler(CommandHandler("available", cmd_available))
    # Admin commands (no-op for non-admins).
    app.add_handler(CommandHandler("gencode", cmd_gencode))
    app.add_handler(CommandHandler("codes", cmd_codes))
    app.add_handler(CommandHandler("grant", cmd_grant))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CallbackQueryHandler(on_category_button))

    # Schedule the poll loop. first=5 runs one scrape shortly after startup.
    app.job_queue.run_repeating(poll_job, interval=POLL_INTERVAL, first=5)

    if ADMIN_ID == 0:
        log.warning(
            "TASKY_ADMIN_ID is not set — admin commands (/gencode, /grant, "
            "/revoke, /codes) are disabled. Set it to your chat id to enable them."
        )
    log.info("Tasky is running. Poll interval: %ss", POLL_INTERVAL)
    app.run_polling()


if __name__ == "__main__":
    main()
