# Full scraper module for Tasky. Reliable sources are enabled by default;
# fragile sources are opt-in through TASKY_ENABLE_SCRAPERS.
#
# Each scraper returns a list of dicts:
#   {"title", "url", "source", "type", "currency"}
# scrape_all() aggregates them with per-source error isolation so one
# failing source never takes down the run.

import json
import re
from datetime import datetime, timezone

import requests

# Reddit blocks generic/library user-agents on the public .json endpoints,
# so present a realistic browser UA.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 15

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(value):
    """Turn Devpost's '<span ...>2,000,000 </span>' into clean text."""
    return " ".join(_TAG_RE.sub("", str(value)).split())


def _shorten(text, limit=140):
    """Collapse whitespace and trim to a clean single-line headline.

    Telegram posts can run many paragraphs; we surface only a headline-length
    slice, cut on a word boundary with an ellipsis when it overflows.
    """
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip() + "…"


def _iso_to_date(value):
    """Normalize an ISO-8601 timestamp like '2026-09-01T23:59:59.000Z' to a
    clean 'YYYY-MM-DD' date. Returns None for empty/unparseable values."""
    if not value:
        return None
    text = str(value).strip()
    # Superteam sends e.g. '2026-09-01T23:59:59.000Z' — the date is the first
    # 10 chars. Validate the shape before trusting it.
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return text


def _today():
    """Today's date as 'YYYY-MM-DD' in UTC (the host clock's reference).

    Used to drop already-expired listings. Comparing normalized YYYY-MM-DD
    strings works because that format sorts lexicographically == chronologically.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _reward_label(reward, default=None):
    """Turn a reward payload into a short '— 1,000 USDT' prize label.

    Handles the two shapes seen in the wild:
      - plain scalar:  e.g. "$250"
      - WizzHQ's JSON-encoded string:  '{"token":"USDT","prizes":[50,30,...]}'
        or '{"token":"WIZZ","rewardType":"pool","poolAmount":1000000}'
    """
    if reward is None:
        return default
    if isinstance(reward, dict):
        token = reward.get("token") or ""
        prizes = reward.get("prizes")
        if isinstance(prizes, list) and prizes:
            return f" — {prizes[0]} {token}".rstrip()
        pool = reward.get("poolAmount")
        if pool:
            return f" — {pool:,} {token}".rstrip()
        return default
    if isinstance(reward, str):
        text = reward.strip()
        if not text:
            return default
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return _reward_label(parsed, default=default)
        if isinstance(parsed, list) and parsed:
            return _reward_label(parsed[0], default=default)
        return text  # plain scalar like "$250"
    return str(reward)

# Keywords that mark a post as an "earning opportunity". Reddit is noisy, so
# we filter titles; Devpost hackathons are all relevant by definition.
KEYWORDS = (
    "bounty", "bounties", "airdrop", "quest", "learn and earn", "learn-and-earn",
    "grant", "hackathon", "reward", "earn", "giveaway", "testnet", "whitelist",
    "paid", "freelance", "gig", "task",
)


def _matches(title):
    t = title.lower()
    return any(k in t for k in KEYWORDS)


# Keyword groups used to classify a post into a subscription category.
_BOUNTY_WORDS = ("bounty", "bounties", "reward", "grant")
_CRYPTO_WORDS = ("airdrop", "quest", "learn and earn", "learn-and-earn", "testnet",
                 "whitelist", "web3", "token", "crypto", "nft", "defi")


def _classify(title, default="freelance"):
    """Pick a subscription category from the title text."""
    t = title.lower()
    if any(w in t for w in _BOUNTY_WORDS):
        return "bounty"
    if any(w in t for w in _CRYPTO_WORDS):
        return "crypto"
    return default


def scrape_reddit(subreddits=("cryptocurrency", "web3", "forhire", "slavelabour"), limit=25):
    """Scrape new posts via Reddit's RSS feed.

    Reddit blocks the public .json API for datacenter/library clients, but the
    .rss feed is served more permissively. We parse it with the stdlib XML
    parser (Atom format).
    """
    import xml.etree.ElementTree as ET

    results = []
    headers = {"User-Agent": USER_AGENT}
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for sub in subreddits:
        url = f"https://www.reddit.com/r/{sub}/new/.rss?limit={limit}"
        try:
            resp = requests.get(url, headers=headers, timeout=TIMEOUT)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as e:
            # Reddit rate-limits datacenter IPs (429); don't let one
            # subreddit's failure discard results from the others.
            print(f"[scraper] reddit/r/{sub} skipped: {e}")
            continue
        for entry in root.findall("atom:entry", ns):
            title_el = entry.find("atom:title", ns)
            link_el = entry.find("atom:link", ns)
            title = (title_el.text or "").strip() if title_el is not None else ""
            link = link_el.get("href") if link_el is not None else ""
            if not title or not link or not _matches(title):
                continue
            # Classify by content, with subreddit as fallback.
            if sub in ("forhire", "slavelabour"):
                type_ = _classify(title, default="freelance")
                currency = "USD"
            else:
                type_ = _classify(title, default="crypto")
                currency = "Crypto" if type_ == "crypto" else "USD"
            results.append(
                {
                    "title": title,
                    "url": link,
                    "source": f"reddit/r/{sub}",
                    "type": type_,
                    "currency": currency,
                }
            )
    return results


def scrape_devpost(limit=30):
    results = []
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    url = f"https://devpost.com/api/hackathons?challenge_type[]=online&status[]=open&per_page={limit}"
    resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    for h in resp.json().get("hackathons", []):
        title = (h.get("title") or "").strip()
        link = h.get("url") or ""
        if not title or not link:
            continue
        prize = ""
        amount = h.get("prize_amount")
        if amount:
            # prize_amount arrives as HTML like "<span>$</span>10,000"
            prize = _strip_html(amount)
        # Devpost gives a submission window like "Aug 01 - Sep 30, 2026";
        # the end of that window is the effective deadline.
        deadline = _strip_html(h.get("submission_period_dates") or "") or None
        results.append(
            {
                "title": f"{title}{(' — ' + prize) if prize else ''}",
                "url": link,
                "source": "devpost",
                "type": "hackathon",
                "currency": "USD",
                "deadline": deadline,
            }
        )
    return results


def scrape_remotive(limit=100):
    """Scrape Remotive's public API for remote freelance/contract gigs.

    Remotive's feed is mostly full-time roles, which aren't "quick earning
    opportunities", so we keep only `freelance` and `contract` job types. The
    API is a documented public JSON endpoint (no auth), which survives cloud
    IPs far better than RemoteOK/Reddit.
    """
    results = []
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    url = f"https://remotive.com/api/remote-jobs?limit={limit}"
    resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    for job in resp.json().get("jobs", []):
        title = (job.get("title") or "").strip()
        link = job.get("url") or ""
        if not title or not link:
            continue
        # Keep only gig-shaped work; skip full_time/part_time salaried roles.
        if job.get("job_type") not in ("freelance", "contract"):
            continue
        salary = (job.get("salary") or "").strip()
        company = (job.get("company_name") or "").strip()
        label = f"{title}{(' @ ' + company) if company else ''}"
        results.append(
            {
                "title": f"{label}{(' — ' + salary) if salary else ''}",
                "url": link,
                "source": "remotive",
                "type": "freelance",
                "currency": "USD",
                # publication_date is when it was posted, not a due date, so no
                # deadline here — Remotive gigs don't expose one.
                "deadline": None,
            }
        )
    return results


def scrape_pasiflora():
    """Scrape Pasiflora AI's public jobs API for paid AI-training expert work.

    Pasiflora pays credentialed experts to validate/annotate AI training data
    across ~17 domains (Healthcare, Technology, Finance, Creative & Media, ...).
    The `/api/jobs` endpoint is public JSON (no auth) and every listing carries a
    pay range. Individual job pages are login-gated (404 unauthenticated), so we
    link to the public jobs listing instead.

    Category routing: "Creative & Media" jobs go to the `creator` category;
    everything else is a paid expert `freelance` gig.
    """
    results = []
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    url = "https://www.pasifloraai.com/api/jobs"
    resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    for job in resp.json().get("jobs", []):
        if job.get("status") != "active":
            continue
        title = (job.get("title") or "").strip()
        if not title:
            continue
        slug = (job.get("slug") or "").strip()
        category = (job.get("category") or "").strip()
        pay = (job.get("pay_range") or "").strip()
        type_ = "creator" if category == "Creative & Media" else "freelance"
        label = f"{title}{(' (' + category + ')') if category else ''}"
        # Job pages are login-gated, so all links point at the public jobs
        # listing. A per-slug #fragment keeps each row's URL unique (the DB
        # dedups on url) without inventing a page that 404s.
        job_url = "https://www.pasifloraai.com/dashboard/jobs"
        if slug:
            job_url += f"#{slug}"
        results.append(
            {
                "title": f"{label}{(' — ' + pay) if pay else ''}",
                "url": job_url,
                "source": "pasiflora",
                "type": type_,
                "currency": "USD",
                # No due date in the feed; these are ongoing openings.
                "deadline": None,
            }
        )
    return results


def scrape_wizzhq():
    """Scrape WizzHQ for Web3 bounties.

    `wizzhq.xyz/api/bounties` is public JSON (no auth) and returns
    `{"listings": [...]}` — a single page carrying every listing (no per-bounty
    endpoint exists). WizzHQ sits behind Cloudflare, which 403s some clients, so
    the realistic browser User-Agent we already send is load-bearing here.

    `reward` arrives JSON-encoded, e.g. '{"token":"USDT","prizes":[50,30]}' or
    '{"token":"WIZZ","rewardType":"pool","poolAmount":1000000}'; _reward_label
    unpacks it. `end_date` is an ISO timestamp; expired bounties are dropped so
    we don't push already-closed opportunities.

    Category routing: WizzHQ's `content` category maps to `creator`; everything
    else is a `bounty`.
    """
    results = []
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    url = "https://wizzhq.xyz/api/bounties"
    resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    for it in resp.json().get("listings", []):
        title = (it.get("title") or "").strip()
        slug = (it.get("slug") or "").strip()
        if not title or not slug:
            continue
        deadline = _iso_to_date(it.get("end_date"))
        # Drop bounties whose deadline has already passed. Compare on the
        # normalized YYYY-MM-DD string (lexicographic == chronological).
        if deadline and deadline < _today():
            continue
        prize = _reward_label(it.get("reward"), default="") or ""
        category = (it.get("categories") or "").strip().lower()
        type_ = "creator" if category == "content" else "bounty"
        results.append(
            {
                "title": f"{title}{prize}",
                "url": f"https://wizzhq.xyz/bounties/{slug}",
                "source": "wizzhq",
                "type": type_,
                "currency": "Crypto",
                "deadline": deadline,
            }
        )
    return results


def scrape_dework(limit=25):
    """Scrape Dework's public GraphQL API for open, rewarded bounties.

    `api.deworkxyz.com/graphql` is a public Apollo endpoint that runs queries
    without auth (introspection is disabled, but `getTasks` validates and
    returns data). We ask only for open (`TODO`) tasks that carry a reward, with
    a bounded `limit`, because the endpoint sits behind a WAF that intermittently
    stalls on large payloads — keeping the response small keeps us under the
    default TIMEOUT, and scrape_all()'s per-source isolation absorbs the
    occasional timeout without killing the run.

    Fields used: `name` (title), `permalink` (url), `dueDate` (deadline), and
    `rewards[].{amount, token{symbol}}` (prize). Tasks without a permalink are
    skipped.
    """
    results = []
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
        # Dework's WAF is friendlier to requests that look like they came from
        # the app itself.
        "Origin": "https://app.dework.xyz",
        "Referer": "https://app.dework.xyz/",
    }
    query = (
        "query($input: GetTasksInput!){"
        " getTasks(input:$input){"
        " name permalink dueDate"
        " rewards { amount token { symbol } } } }"
    )
    variables = {"input": {"statuses": ["TODO"], "rewardNotNull": True, "limit": limit}}
    resp = requests.post(
        "https://api.deworkxyz.com/graphql",
        json={"query": query, "variables": variables},
        headers=headers,
        # Dework's WAF is slow-but-not-dead: the reward query measured ~11-14s
        # live, so a short timeout would fail every cycle. Give it a wider budget
        # than the module default; per-source isolation still absorbs a genuine
        # hang, it just waits a bit longer before giving up.
        timeout=25,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        # Surface a schema/WAF error as an exception so scrape_all() logs it and
        # moves on, rather than silently returning nothing.
        raise RuntimeError(f"dework graphql: {payload['errors'][:1]}")
    for t in (payload.get("data") or {}).get("getTasks", []) or []:
        name = (t.get("name") or "").strip()
        link = (t.get("permalink") or "").strip()
        if not name or not link:
            continue
        prize = ""
        rewards = t.get("rewards") or []
        if rewards:
            r0 = rewards[0] or {}
            amount = r0.get("amount")
            token = ((r0.get("token") or {}).get("symbol") or "").strip()
            if amount:
                prize = f" — {amount} {token}".rstrip()
        deadline = _iso_to_date(t.get("dueDate"))
        results.append(
            {
                "title": f"{name}{prize}",
                "url": link,
                "source": "dework",
                "type": "bounty",
                "currency": "Crypto",
                "deadline": deadline,
            }
        )
    return results


def scrape_immunefi(limit=60):
    """Scrape Immunefi bug-bounty programs via a community-maintained mirror.

    Immunefi's own site is a Cloudflare-fronted Next.js app with no documented
    public JSON API. A bot-maintained GitHub mirror publishes the full program
    list as raw JSON (public, no auth), auto-updated when programs change:
    `.../Immunefi-Bug-Bounty-Programs-Unofficial/main/projects.json` — a ~7.5MB
    array of ~250 programs. We take the first `limit` after sorting by
    `updatedDate` (most recently changed first) so the freshest programs land
    even though the DB dedups on url.

    Most Immunefi programs are standing (no deadline); audit competitions carry
    an `endDate`, which we surface when present. `maxBounty` is the reward
    ceiling. Links point at the canonical `immunefi.com/bug-bounty/<slug>` page.
    """
    results = []
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    url = (
        "https://raw.githubusercontent.com/infosec-us-team/"
        "Immunefi-Bug-Bounty-Programs-Unofficial/main/projects.json"
    )
    resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    programs = resp.json()
    if not isinstance(programs, list):
        return results
    # Newest-changed first, so a bounded slice still reflects recent activity.
    programs.sort(key=lambda p: str(p.get("updatedDate") or ""), reverse=True)
    for p in programs[:limit]:
        name = (p.get("project") or "").strip()
        slug = (p.get("slug") or "").strip()
        if not name or not slug:
            continue
        max_bounty = p.get("maxBounty")
        token = (p.get("rewardsToken") or "").strip()
        prize = ""
        if max_bounty:
            try:
                prize = f" — up to {int(max_bounty):,} {token}".rstrip()
            except (TypeError, ValueError):
                prize = ""
        deadline = _iso_to_date(p.get("endDate"))
        results.append(
            {
                "title": f"{name}{prize}",
                "url": f"https://immunefi.com/bug-bounty/{slug}/",
                "source": "immunefi",
                "type": "bug_bounty",
                "currency": "Crypto",
                "deadline": deadline,
            }
        )
    return results


def scrape_superteam(limit=25):
    """Scrape Superteam Earn for crypto bounties and grants.

    Public JSON API. The `earn.` host 308-redirects to the root domain, so we
    hit the root directly. Response is UTF-8; decode explicitly to avoid
    mojibake in emoji-laden titles.
    """
    results = []
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    # `bounty` = tasks with a reward; `project` = grants/gigs.
    for listing_type in ("bounty", "project"):
        url = f"https://superteam.fun/api/listings?type={listing_type}&take={limit}"
        try:
            resp = requests.get(url, headers=headers, timeout=TIMEOUT)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            items = resp.json()
        except Exception as e:
            print(f"[scraper] superteam {listing_type} skipped: {e}")
            continue
        if not isinstance(items, list):
            continue
        for it in items:
            title = (it.get("title") or "").strip()
            slug = it.get("slug") or ""
            if not title or not slug:
                continue
            reward = it.get("rewardAmount")
            token = it.get("token") or ""
            prize = f" — {reward:,} {token}".rstrip() if reward else ""
            deadline = _iso_to_date(it.get("deadline"))
            results.append(
                {
                    "title": f"{title}{prize}",
                    "url": f"https://earn.superteam.fun/listing/{slug}/",
                    "source": f"superteam/{listing_type}",
                    "type": "bounty",
                    "currency": "Crypto",
                    "deadline": deadline,
                }
            )
    return results


def scrape_themuse(pages=2):
    """Scrape The Muse's public jobs API for internships.

    The Muse exposes a documented public JSON API with a native
    `level=Internship` filter, so no title-guessing is needed — every result
    is genuinely an internship. ~7,900 internships are available; we take the
    first `pages` (20 per page) newest each cycle. No auth, survives cloud IPs.

    Each job carries a real company name and a `refs.landing_page` URL that is
    unique per posting (good dedup key). No deadline is exposed — these are
    ongoing openings — so `deadline` is None.
    """
    results = []
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    for page in range(1, pages + 1):
        url = f"https://www.themuse.com/api/public/jobs?level=Internship&page={page}"
        try:
            resp = requests.get(url, headers=headers, timeout=TIMEOUT)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            payload = resp.json()
        except Exception as e:
            print(f"[scraper] themuse page {page} skipped: {e}")
            continue
        for job in payload.get("results", []):
            name = (job.get("name") or "").strip()
            landing = ((job.get("refs") or {}).get("landing_page") or "").strip()
            if not name or not landing:
                continue
            company = ((job.get("company") or {}).get("name") or "").strip()
            label = f"{name}{(' @ ' + company) if company else ''}"
            results.append(
                {
                    "title": label,
                    "url": landing,
                    "source": "themuse",
                    "type": "internship",
                    "currency": "USD",
                    "deadline": None,
                }
            )
    return results


def scrape_web3career(limit=100):
    """Scrape web3.career for web3/crypto internships via its official API.

    web3.career offers a documented API that requires a free API token, read
    from the WEB3CAREER_TOKEN env var (never hard-coded — same handling as the
    bot token). If the token is unset, the source is skipped cleanly.

    The API's `tag=internships` filter returns nothing (not a valid tag value),
    so we pull the untagged feed (max 100/call on the free tier) and filter to
    internships client-side using each job's `tags` array and title. Web3
    internships are genuinely rare, so expect only a handful per run.

    Response shape: [metaStr, metaStr, [job, job, ...]].
    """
    import os

    token = os.environ.get("WEB3CAREER_TOKEN")
    if not token:
        print("[scraper] web3career skipped: WEB3CAREER_TOKEN not set")
        return []
    results = []
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    url = f"https://web3.career/api/v1?token={token}&limit={limit}"
    resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    payload = resp.json()
    jobs = []
    if isinstance(payload, list):
        for part in payload:
            if isinstance(part, list):
                jobs = part
                break
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = (job.get("title") or "").strip()
        link = (job.get("apply_url") or job.get("url") or "").strip()
        if not title or not link:
            continue
        # Keep only internships: match the title or an 'intern' tag.
        tags = job.get("tags") or []
        is_intern = bool(_INTERN_RE.search(title)) or any(
            "intern" in str(t).lower() for t in tags
        )
        if not is_intern:
            continue
        company = (job.get("company") or "").strip()
        label = f"{title}{(' @ ' + company) if company else ''}"
        results.append(
            {
                "title": label,
                "url": link,
                "source": "web3career",
                "type": "internship",
                "currency": "Crypto",
                "deadline": None,
            }
        )
    return results


# Default public Telegram channels to watch when TASKY_TG_CHANNELS is unset.
# All verified to expose the public /s/ web preview (no login) at time of
# writing. Curated airdrop/quest feeds, so most posts are genuine opportunities.
_DEFAULT_TG_CHANNELS = (
    "airdrops_io", "airdropinspector", "airdropdetectivee",
    "airdropalertcom", "airdropfind", "airdropsmob",
)


def scrape_telegram(channels=None, per_channel=20):
    """Scrape public Telegram channels via their t.me/s/ web preview.

    Telegram serves a channel's recent posts as static HTML at
    `https://t.me/s/<channel>` — no login, no API token — the same best-effort
    approach we use for Reddit. Channels are read from the `TASKY_TG_CHANNELS`
    env var (comma-separated handles, '@' optional); if unset we fall back to a
    curated list of airdrop/quest feeds.

    A channel with its public preview disabled 302-redirects `/s/<ch>` to
    `/<ch>` (a join page with zero message wrappers); we detect the empty parse
    and skip it cleanly. Each post carries a unique `data-post` id ("chan/123"),
    which yields a stable per-post permalink — an ideal dedup key for the DB.

    Posts are noisy, so titles are keyword-filtered like Reddit and trimmed to a
    headline. `deadline` is None: a post's timestamp is when it was sent, not a
    due date.
    """
    import os
    from bs4 import BeautifulSoup

    if channels is None:
        env = os.environ.get("TASKY_TG_CHANNELS", "")
        channels = [c.strip().lstrip("@") for c in env.split(",") if c.strip()] \
            or list(_DEFAULT_TG_CHANNELS)

    results = []
    headers = {"User-Agent": USER_AGENT}
    for ch in channels:
        url = f"https://t.me/s/{ch}"
        try:
            resp = requests.get(url, headers=headers, timeout=TIMEOUT)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            # One unreachable/slow channel must not sink the others (t.me can be
            # sluggish); per-source isolation in scrape_all() is the outer net.
            print(f"[scraper] telegram/{ch} skipped: {e}")
            continue
        msgs = soup.select(".tgme_widget_message")
        if not msgs:
            # No message wrappers => preview disabled (redirected to join page).
            print(f"[scraper] telegram/{ch}: no public preview, skipped")
            continue
        # Newest posts are last in the DOM; take the most recent `per_channel`.
        for m in msgs[-per_channel:]:
            post = (m.get("data-post") or "").strip()  # e.g. "airdrops_io/1234"
            text_el = m.select_one(".tgme_widget_message_text")
            if not post or text_el is None:
                continue  # media-only post or malformed row
            title = _shorten(text_el.get_text(" ", strip=True))
            if not title or not _matches(title):
                continue
            results.append(
                {
                    "title": title,
                    "url": f"https://t.me/{post}",
                    "source": f"telegram/{ch}",
                    "type": _classify(title, default="crypto"),
                    "currency": "Crypto",
                    "deadline": None,
                }
            )
    return results


def scrape_zealy(communities=None, limit=50):
    """Scrape Zealy community questboards for crypto quests.

    Zealy's API is per-community and, as of the v2 migration, **every** endpoint
    — including the `/public/` ones — requires that community's own `x-api-key`
    (an unauthenticated call returns 401). There is no true no-key feed, so this
    source is opt-in and degrades cleanly:

      - Communities come from `TASKY_ZEALY_COMMUNITIES` (comma-separated
        subdomains). Unset => the source is skipped entirely, like web3career.
      - `ZEALY_API_KEY`, if set, is sent as `x-api-key`. Without it we still
        attempt the public endpoint and skip cleanly on 401/403 — the bot never
        breaks for lack of a key; the source simply stays dormant until one is
        provided.

    Quests map to the 🪙 `crypto` category. Links use the canonical questboard
    URL with the quest id in the path so each row dedups uniquely; no reliable
    public deadline is exposed, so `deadline` stays None unless the quest carries
    one.
    """
    import os

    if communities is None:
        env = os.environ.get("TASKY_ZEALY_COMMUNITIES", "")
        communities = [c.strip().strip("/").lstrip("@") for c in env.split(",") if c.strip()]
    if not communities:
        print("[scraper] zealy skipped: TASKY_ZEALY_COMMUNITIES not set")
        return []

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    api_key = os.environ.get("ZEALY_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key

    results = []
    for sub in communities:
        url = f"https://api-v2.zealy.io/public/communities/{sub}/quests"
        try:
            resp = requests.get(url, headers=headers, timeout=TIMEOUT)
            if resp.status_code in (401, 403):
                # Needs this community's x-api-key. Honor the no-key mode: log
                # once and move on rather than raising.
                print(f"[scraper] zealy/{sub} skipped: needs ZEALY_API_KEY "
                      f"({resp.status_code})")
                continue
            resp.raise_for_status()
            resp.encoding = "utf-8"
            payload = resp.json()
        except Exception as e:
            print(f"[scraper] zealy/{sub} skipped: {e}")
            continue

        # The endpoint may return a flat quest list, a {data|quests: [...]}
        # wrapper, or modules that each nest a `quests` list. Flatten all shapes.
        quests = []
        if isinstance(payload, list):
            for entry in payload:
                if isinstance(entry, dict) and isinstance(entry.get("quests"), list):
                    quests.extend(entry["quests"])  # module -> quests
                elif isinstance(entry, dict):
                    quests.append(entry)             # already a quest
        elif isinstance(payload, dict):
            quests = payload.get("quests") or payload.get("data") or []

        for q in quests[:limit]:
            if not isinstance(q, dict):
                continue
            # Skip drafts/archived when the flag is present; default to visible.
            if q.get("published") is False or q.get("archived") is True:
                continue
            name = (q.get("name") or q.get("title") or "").strip()
            qid = str(q.get("id") or "").strip()
            if not name or not qid:
                continue
            deadline = _iso_to_date(q.get("deadline") or q.get("endDate"))
            results.append(
                {
                    "title": name,
                    "url": f"https://zealy.io/cw/{sub}/questboard/{qid}",
                    "source": f"zealy/{sub}",
                    "type": "crypto",
                    "currency": "Crypto",
                    "deadline": deadline,
                }
            )
    return results


# Registry of active scrapers. Add new callables here to extend coverage
# without touching scrape_all().
#
# Source health (as of last review):
#   devpost   — reliable public JSON API, hackathons w/ deadlines
#   superteam — reliable public JSON API, crypto bounties w/ deadlines
#   remotive  — reliable public JSON API, remote freelance/contract gigs
#   pasiflora — reliable public JSON API, paid AI-training expert jobs (+creator)
#   themuse   — reliable public JSON API, internships (native Internship filter)
#   wizzhq    — public JSON API (Cloudflare; needs browser UA), Web3 bounties w/ deadlines
#   dework    — public GraphQL, open rewarded bounties; WAF can stall (best-effort)
#   immunefi  — public GitHub-raw mirror, bug-bounty programs (mostly no deadline)
#   web3career— official API (needs WEB3CAREER_TOKEN env var), web3 internships
#   telegram  — t.me/s/ public channel preview (HTML); best-effort, skips
#               channels with preview disabled. Channels via TASKY_TG_CHANNELS.
#   zealy     — per-community API; every endpoint needs x-api-key. Opt-in via
#               TASKY_ZEALY_COMMUNITIES (+ ZEALY_API_KEY); skips cleanly w/o key.
#   reddit    — RSS; rate-limited (429) from datacenter/cloud IPs, best-effort
_OPTIONAL = {
    "reddit": scrape_reddit,
    "superteam": scrape_superteam,
    "wizzhq": scrape_wizzhq,
    "dework": scrape_dework,
    "telegram": scrape_telegram,
    "zealy": scrape_zealy,
}
_DEFAULT = (
    ("devpost", scrape_devpost),
)


def _active_scrapers():
    enabled = {x.strip().lower() for x in __import__("os").environ.get("TASKY_ENABLE_SCRAPERS", "").split(",") if x.strip()}
    disabled = {x.strip().lower() for x in __import__("os").environ.get("TASKY_DISABLE_SCRAPERS", "").split(",") if x.strip()}
    return _DEFAULT + tuple((name, fn) for name, fn in _OPTIONAL.items() if name in enabled and name not in disabled)


def scrape_all():
    results = []
    try:
        from . import db
    except ImportError:
        import db
    db.init()
    for name, fn in _active_scrapers():
        try:
            found = fn()
            results.extend(found)
            db.record_source_run(name, len(found))
            print(f"[scraper] {name}: {len(found)} items")
        except Exception as e:  # never let one source kill the run
            db.record_source_run(name, error=str(e))
            print(f"[scraper] {name} failed: {e}")
    return results


if __name__ == "__main__":
    for item in scrape_all():
        print(f"- ({item['source']}) {item['title']}\n  {item['url']}")
