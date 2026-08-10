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


# Registry of active scrapers. Add new callables here to extend coverage
# without touching scrape_all().
#
# Source health (as of last review):
#   devpost   — reliable public JSON API, hackathons w/ deadlines
#   superteam — reliable public JSON API, crypto bounties w/ deadlines
#   remotive  — reliable public JSON API, remote freelance/contract gigs
#   pasiflora — reliable public JSON API, paid AI-training expert jobs (+creator)
#   themuse   — reliable public JSON API, internships (native Internship filter)
#   reddit    — RSS; rate-limited (429) from datacenter/cloud IPs, best-effort
SCRAPERS = (
    ("reddit", scrape_reddit),
    ("devpost", scrape_devpost),
    ("remotive", scrape_remotive),
    ("superteam", scrape_superteam),
    ("pasiflora", scrape_pasiflora),
    ("themuse", scrape_themuse),
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
