# Full scraper module for Tasky
# Active sources: Reddit (r/cryptocurrency, r/web3), Devpost hackathons.
#
# Each scraper returns a list of dicts:
#   {"title", "url", "source", "type", "currency"}
# scrape_all() aggregates them with per-source error isolation so one
# failing source never takes down the run.

import re

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


def scrape_remoteok(limit=20):
    """Scrape RemoteOK for remote gigs/jobs.

    The public API returns a JSON array; first element is metadata, rest are
    listings. Filter by keywords to surface bounty/freelance-like roles.
    """
    results = []
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    url = "https://remoteok.com/api"
    resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    # First item is metadata; actual listings start at index 1.
    listings = data[1 : limit + 1] if len(data) > 1 else []
    for job in listings:
        title = (job.get("position") or "").strip()
        link = job.get("url") or ""
        tags = " ".join(job.get("tags") or []).lower()
        # RemoteOK is very broad; filter by keywords to focus on relevant roles.
        if not title or not link or not _matches(title + " " + tags):
            continue
        results.append(
            {
                "title": title,
                "url": f"https://remoteok.com{link}" if link.startswith("/") else link,
                "source": "remoteok",
                "type": "freelance",
                "currency": "USD",
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


# Registry of active scrapers. Add new callables here to extend coverage
# (e.g. Immunefi) without touching scrape_all().
SCRAPERS = (
    ("reddit", scrape_reddit),
    ("devpost", scrape_devpost),
    ("remoteok", scrape_remoteok),
    ("superteam", scrape_superteam),
)


def scrape_all():
    results = []
    for name, fn in SCRAPERS:
        try:
            found = fn()
            results.extend(found)
            print(f"[scraper] {name}: {len(found)} items")
        except Exception as e:  # never let one source kill the run
            print(f"[scraper] {name} failed: {e}")
    return results


if __name__ == "__main__":
    for item in scrape_all():
        print(f"- ({item['source']}) {item['title']}\n  {item['url']}")
