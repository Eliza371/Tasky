import os
import secrets
import sqlite3
import time

# Store the DB alongside this project (src/../tasky.db), regardless of CWD.
# NOTE: use a raw path or forward slashes so backslash escapes (\t, \U, ...)
# are never interpreted by Python.
# TASKY_DB overrides the location (e.g. a mounted persistent volume in cloud
# hosting like Railway, where the container filesystem is otherwise ephemeral).
_DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tasky.db")
DB = os.environ.get("TASKY_DB", _DEFAULT_DB)


def _connect():
    return sqlite3.connect(DB)


def init():
    conn = _connect()
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            url TEXT UNIQUE,
            source TEXT,
            type TEXT,
            currency TEXT,
            posted INTEGER,
            notified INTEGER DEFAULT 0,
            deadline TEXT
        )"""
    )
    # Migration: add deadline column to pre-existing task tables. Kept at the
    # end of the row so freshly-created and migrated tables share one layout
    # (SQLite ALTER always appends), i.e. SELECT * index 8 is always deadline.
    tcols = [r[1] for r in c.execute("PRAGMA table_info(tasks)").fetchall()]
    if "deadline" not in tcols:
        c.execute("ALTER TABLE tasks ADD COLUMN deadline TEXT")
    # Migration: Immunefi listings were originally stored as type='bounty' but
    # now belong to the dedicated 'bug_bounty' category. Retype existing rows so
    # they surface under the new category (new inserts already use bug_bounty,
    # and the URL is unchanged so INSERT OR IGNORE would never update them).
    c.execute(
        "UPDATE tasks SET type='bug_bounty' WHERE source='immunefi' AND type='bounty'"
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS subscribers (
            chat_id INTEGER PRIMARY KEY,
            added INTEGER,
            categories TEXT DEFAULT 'crypto,hackathon,bounty,bug_bounty,freelance,creator,internship'
        )"""
    )
    # Migration: add categories column to pre-existing subscriber tables.
    cols = [r[1] for r in c.execute("PRAGMA table_info(subscribers)").fetchall()]
    if "categories" not in cols:
        c.execute(
            "ALTER TABLE subscribers ADD COLUMN categories TEXT "
            "DEFAULT 'crypto,hackathon,bounty,freelance'"
        )
    # Invite-only gating. `access` records which chats may use the bot;
    # `invite_codes` holds single-use codes (used_by is NULL until redeemed).
    c.execute(
        """CREATE TABLE IF NOT EXISTS access (
            chat_id INTEGER PRIMARY KEY,
            granted INTEGER,
            via TEXT
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS invite_codes (
            code TEXT PRIMARY KEY,
            created INTEGER,
            used_by INTEGER,
            used_at INTEGER
        )"""
    )
    conn.commit()
    conn.close()


# All opportunity categories the bot understands. A task's `type` field must be
# one of these; a subscriber's `categories` is a subset.
CATEGORIES = ("crypto", "hackathon", "bounty", "bug_bounty", "freelance", "creator", "internship")


def insert(title, url, source, type_, currency="USD/Crypto", deadline=None):
    """Insert an opportunity. Returns True if it was new, False if a duplicate.

    `deadline` is an optional human-readable due date/time (e.g. "2026-09-01"
    or "Sep 1, 2026"); None for sources that don't expose one.
    """
    conn = _connect()
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO tasks (title, url, source, type, currency, posted, notified, deadline) "
        "VALUES (?,?,?,?,?,?,0,?)",
        (title, url, source, type_, currency, int(time.time()), deadline),
    )
    conn.commit()
    inserted = c.rowcount > 0
    conn.close()
    return inserted


def get_new(limit=20):
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT * FROM tasks ORDER BY posted DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_by_categories(categories, limit=20):
    """Recent tasks whose `type` is in `categories`, newest first.

    Returns full task rows (id, title, url, source, type, currency, posted,
    notified). Empty `categories` yields no rows.
    """
    cats = [c for c in categories if c]
    if not cats:
        return []
    conn = _connect()
    c = conn.cursor()
    placeholders = ",".join("?" for _ in cats)
    c.execute(
        f"SELECT * FROM tasks WHERE type IN ({placeholders}) "
        "ORDER BY posted DESC, id DESC LIMIT ?",
        (*cats, limit),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def get_unnotified():
    """Rows that have not yet been pushed to subscribers."""
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT id, title, url, source, type, currency, posted, deadline FROM tasks WHERE notified=0 ORDER BY posted ASC")
    rows = c.fetchall()
    conn.close()
    return rows


def mark_notified(task_id):
    conn = _connect()
    c = conn.cursor()
    c.execute("UPDATE tasks SET notified=1 WHERE id=?", (task_id,))
    conn.commit()
    conn.close()


def add_subscriber(chat_id, categories=None):
    """Subscribe a chat. `categories` is a list; defaults to all."""
    if categories is None:
        categories = list(CATEGORIES)
    cats = ",".join(categories)
    conn = _connect()
    c = conn.cursor()
    c.execute(
        "INSERT INTO subscribers (chat_id, added, categories) VALUES (?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET categories=excluded.categories",
        (chat_id, int(time.time()), cats),
    )
    conn.commit()
    conn.close()


def remove_subscriber(chat_id):
    conn = _connect()
    c = conn.cursor()
    c.execute("DELETE FROM subscribers WHERE chat_id=?", (chat_id,))
    conn.commit()
    changed = c.rowcount > 0
    conn.close()
    return changed


def get_categories(chat_id):
    """Return the list of categories a chat is subscribed to, or [] if none."""
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT categories FROM subscribers WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    conn.close()
    if not row or not row[0]:
        return []
    return [x for x in row[0].split(",") if x]


def get_subscribers():
    """Return list of (chat_id, [categories]) for all subscribers."""
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT chat_id, categories FROM subscribers")
    rows = c.fetchall()
    conn.close()
    result = []
    for chat_id, cats in rows:
        cat_list = [x for x in (cats or "").split(",") if x]
        result.append((chat_id, cat_list))
    return result


# --- Invite-only access ------------------------------------------------------
def has_access(chat_id):
    """True if this chat has been granted access (via code or admin)."""
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT 1 FROM access WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    conn.close()
    return row is not None


def grant_access(chat_id, via="admin"):
    """Grant a chat access. `via` records how (e.g. 'admin' or 'code:ABCD')."""
    conn = _connect()
    c = conn.cursor()
    c.execute(
        "INSERT INTO access (chat_id, granted, via) VALUES (?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET granted=excluded.granted, via=excluded.via",
        (chat_id, int(time.time()), via),
    )
    conn.commit()
    conn.close()


def revoke_access(chat_id):
    """Remove a chat's access. Returns True if it had access, else False."""
    conn = _connect()
    c = conn.cursor()
    c.execute("DELETE FROM access WHERE chat_id=?", (chat_id,))
    conn.commit()
    changed = c.rowcount > 0
    conn.close()
    return changed


def gen_code():
    """Return a fresh unguessable invite code like 'A1B2-C3D4'."""
    raw = secrets.token_hex(4).upper()  # 8 hex chars
    return f"{raw[:4]}-{raw[4:]}"


def create_code(code):
    """Store a new single-use invite code."""
    conn = _connect()
    c = conn.cursor()
    c.execute(
        "INSERT INTO invite_codes (code, created, used_by, used_at) VALUES (?,?,NULL,NULL)",
        (code, int(time.time())),
    )
    conn.commit()
    conn.close()


def redeem_code(code, chat_id):
    """Redeem a single-use code for a chat.

    Returns 'ok' on success (and grants access in the same transaction),
    'used' if the code was already redeemed, or 'invalid' if unknown.
    Matching is case-insensitive.
    """
    code = (code or "").strip().upper()
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT used_by FROM invite_codes WHERE code=?", (code,))
    row = c.fetchone()
    if row is None:
        conn.close()
        return "invalid"
    if row[0] is not None:
        conn.close()
        return "used"
    now = int(time.time())
    c.execute(
        "UPDATE invite_codes SET used_by=?, used_at=? WHERE code=?",
        (chat_id, now, code),
    )
    c.execute(
        "INSERT INTO access (chat_id, granted, via) VALUES (?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET granted=excluded.granted, via=excluded.via",
        (chat_id, now, f"code:{code}"),
    )
    conn.commit()
    conn.close()
    return "ok"


def list_unused_codes():
    """Return the list of codes that have not yet been redeemed."""
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT code FROM invite_codes WHERE used_by IS NULL ORDER BY created")
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]
