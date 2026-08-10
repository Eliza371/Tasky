# Tasky

A Telegram bot that monitors and instantly notifies subscribers of quick
earning opportunities: crypto quests, bounties, airdrops, learn-and-earn,
hackathons, and developer bounties.

**Active sources:** Reddit (r/cryptocurrency, r/web3) and Devpost hackathons,
both via their public JSON feeds. New sources plug into `SCRAPERS` in
`src/scraper_full.py` without touching the rest of the code.

## Install

```
pip install "python-telegram-bot[job-queue]" requests beautifulsoup4 python-dotenv
```

`sqlite3` is part of the Python standard library — do **not** try to pip-install
it. The `[job-queue]` extra is required for the background poll loop.

## Configure the token

Copy the example env file and fill in your BotFather token:

```
# PowerShell
Copy-Item .env.example .env
notepad .env
```

`.env` is git-ignored, so your token never gets committed. `tasky_main.py`
loads it automatically on startup.

Alternatively, set it as a session environment variable:

```
# PowerShell
$env:TASKY_TOKEN = "123456:ABC-your-token"
```

Never paste the token into a source file or the README — those are easy to
leak. If a token is ever exposed, revoke it via @BotFather (`/revoke`).

## Initialize the database

```
python -c "from src.db import init; init()"
```

This creates `tasky.db` in the project root (`tasks`, `subscribers`, `access`,
and `invite_codes` tables).

## Run

```
python src/tasky_main.py
```

The bot scrapes every `TASKY_POLL_INTERVAL` seconds (default 300) and pushes new
opportunities to every subscriber.

## Bot commands

| Command | Action |
| --- | --- |
| `/start` | Intro and help (shows your chat id) |
| `/id` | Show your chat id |
| `/redeem CODE` | Unlock the bot with an access code |
| `/subscribe` | Choose categories via buttons: 🪙 Crypto, 💻 Hackathons, 🎯 Bounties, 🐛 Bug Bounties, 💼 Freelance/Tasks, 🎨 Creator, 🎓 Internships |
| `/mysubs` | Show your current categories |
| `/unsubscribe` | Stop all notifications |
| `/latest` | Show the 10 most recent finds |
| `/available` | Show all available tasks for your subscribed categories, grouped by category |

Each user picks their own categories and only receives matching opportunities.

## Invite-only access

Tasky is gated. New chats can run `/start` and `/redeem`, but everything else
(`/subscribe`, `/mysubs`, `/unsubscribe`, `/latest`, and notifications) requires
access. A chat without access that hits a gated command gets:

```
🔒 Access required
This bot is invite-only.

If you have a code: /redeem YOUR_CODE
If not: ask the admin to send you one. Your chat id is 7825996569
(tap it to copy)
```

The chat id is shown as a tap-to-copy code span in Telegram, so users can copy
it with a single tap instead of selecting the digits by hand.

Access is granted two ways: a user redeems a single-use code, or the admin
grants a chat id directly.

### How a new user finds their chat id

Telegram doesn't expose chat ids in its UI, so the bot tells the user directly.
The first message anyone sends a bot is `/start`, and that reply now includes
their chat id. They can also send `/id` at any time, or just trip the 🔒 gate by
running any locked command — all three show the same number as a tap-to-copy
code span. They copy that id to the admin to request access.

### Admin setup

Set `TASKY_ADMIN_ID` to your own Telegram chat id (the gate message above shows
any user their id). Only that chat can run the admin commands:

| Command | Action |
| --- | --- |
| `/gencode [n]` | Mint `n` single-use codes (default 1, max 20) |
| `/codes` | List codes that haven't been redeemed yet |
| `/grant <chat_id>` | Grant access directly, no code needed |
| `/revoke <chat_id>` | Revoke access and stop that chat's feed |

Typical flow: a user messages you their chat id → you `/gencode` and forward the
code → they `/redeem CODE` → `/subscribe`. Or skip the code entirely with
`/grant <their_chat_id>`.

The admin commands appear in Telegram's `/` autocomplete menu only for your chat
id; everyone else sees just the public commands. The bot registers both menus
automatically on startup — no BotFather configuration needed.

## Deploy to Railway (24/7 hosting)

Tasky is a long-running poller with no web server, so it runs as a **worker** on
[Railway](https://railway.app). Because Railway's container filesystem is
ephemeral, the SQLite database must live on a **persistent volume** or it resets
on every redeploy.

1. **Push to GitHub.** `.env`, `.env.local`, and `tasky.db` are git-ignored, so
   your token and local data stay out of the repo.
   ```bash
   git init && git add -A && git commit -m "Tasky bot"
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
2. **Create the project.** Railway → New Project → *Deploy from GitHub repo* →
   pick the repo. Railway detects Python, installs `requirements.txt`, and starts
   the `worker` process from the `Procfile`.
3. **Add a volume.** In the service, add a Volume mounted at `/data`. This is the
   persistent disk the database will sit on.
4. **Set variables** (service → Variables) — these replace `.env.local`:
   | Variable | Value |
   | --- | --- |
   | `TASKY_TOKEN` | your BotFather token |
   | `TASKY_ADMIN_ID` | your chat id |
   | `TASKY_DB` | `/data/tasky.db` |
   | `TASKY_POLL_INTERVAL` | `300` (optional) |
5. **Deploy.** Railway redeploys automatically on every `git push`. Watch the
   logs for `Tasky is running. Poll interval: 300s`.

The volume starts empty: the bot creates a fresh database on first boot and
re-scrapes tasks within one poll cycle. Re-grant yourself with `/grant <id>` (or
`/redeem`) and re-run `/subscribe`. To carry your existing data over instead,
copy your local `tasky.db` to `/data/tasky.db` on the volume.

> **Only one instance may poll a token at a time.** Stop any local run before the
> Railway deploy goes live, or Telegram returns `409 Conflict` and the two
> instances fight over updates.

## Sources by category

- **Crypto** — Reddit r/cryptocurrency, r/web3 (airdrops, quests, learn-and-earn)
- **Hackathons** — Devpost (open online hackathons with prizes)
- **Bounties** — Superteam Earn (crypto bounties/grants), Dework (open rewarded
  DAO tasks), WizzHQ (Web3 bounties), plus anything keyword-detected across other
  sources (bounty/reward/grant)
- **Bug Bounties** — Immunefi (security/bug-bounty programs, rewards up to the
  program max), via a community-maintained mirror of the program list
- **Freelance/Tasks** — Remotive (remote freelance/contract gigs), Reddit r/forhire,
  r/slavelabour, Pasiflora AI (paid AI-training expert jobs across ~17 domains)
- **Creator** — Pasiflora AI "Creative & Media" expert jobs (film/video, music,
  game design, graphic design, photography, editorial), WizzHQ `content` bounties
- **Internships** — The Muse (7,900+ internships across companies worldwide,
  via its public `level=Internship` API), web3.career (web3 internships, needs
  `WEB3CAREER_TOKEN`); items from any source whose title names an
  internship/fellowship are also surfaced here

### Source reliability notes

Some Web3 platforms don't offer a clean public feed, so their reliability varies:

- **WizzHQ** — `wizzhq.xyz/api/bounties` is public JSON but sits behind Cloudflare
  (the browser User-Agent is required) and currently returns only *past-deadline*
  listings, which are filtered out; it contributes items once live bounties are
  posted.
- **Dework** — `api.deworkxyz.com/graphql` is public but throttled by a WAF that
  can stall on large queries, so the query is kept small and given a wider
  timeout. Best-effort: a stalled cycle is logged and skipped.
- **Immunefi** — sourced from a community-maintained GitHub-raw mirror of the
  program list (public, no key). Most programs are standing (no deadline).

Many other requested platforms (Whop, Arena, Klout, Hashly, Gitcoin, Layer3,
Scouts, etc.) were evaluated but are **not integrated**: they require login/API
keys, are currently down, or block automated clients. See the git history / the
scraper module header for the per-platform verdict.


## Test the scrapers without Telegram

```
python src/scraper_full.py
```

Prints everything currently found from the active sources.
