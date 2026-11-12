"""
Feed health check — run from GitHub Actions (diagnostics.yml) to see feeds the
way the bot sees them: many sites block datacenter IPs, so a feed that works
from a laptop can return 403 to a GitHub runner.

Usage:
  python check_feeds.py                 # every feed in rss_feeds.json
  python check_feeds.py URL [URL ...]   # also test candidate feeds

Prints one line per feed: verdict, HTTP status, entry count, age of the
newest entry. Verdicts: OK, STALE (newest entry older than STALE_DAYS),
EMPTY (no entries parsed), DEAD (non-200 or request error).
"""

import asyncio
import calendar
import sys
import time

import feedparser
import httpx

from rss_feeds import RSS_FEEDS
from rss_fetcher import USER_AGENT

STALE_DAYS = 60


def _newest_age_days(parsed) -> float | None:
    stamps = []
    for e in parsed.entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        if t:
            stamps.append(calendar.timegm(t))
    if not stamps:
        return None
    return (time.time() - max(stamps)) / 86400


async def check(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    try:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT})
    except Exception as e:
        return "DEAD", f"error {type(e).__name__}: {str(e)[:80]}"
    if resp.status_code != 200:
        return "DEAD", f"HTTP {resp.status_code}"
    parsed = feedparser.parse(resp.content)
    n = len(parsed.entries)
    if n == 0:
        return "EMPTY", "HTTP 200, 0 entries"
    age = _newest_age_days(parsed)
    age_s = "no dates" if age is None else f"newest {age:.1f}d ago"
    redirect = f" -> {resp.url}" if str(resp.url) != url else ""
    verdict = "STALE" if age is not None and age > STALE_DAYS else "OK"
    return verdict, f"HTTP 200, {n} entries, {age_s}{redirect}"


async def main(extra: list[str]) -> None:
    targets = [(f["category"], f["url"]) for f in RSS_FEEDS]
    targets += [("candidate", u) for u in extra]
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        results = await asyncio.gather(*(check(client, u) for _, u in targets))
    rows = sorted(zip(targets, results), key=lambda r: (r[1][0] == "OK", r[0][0]))
    for (cat, url), (verdict, detail) in rows:
        print(f"{verdict:6} {cat[:28]:28} {url}  [{detail}]")
    bad = sum(1 for _, (v, _) in rows if v != "OK")
    print(f"\n{len(rows) - bad}/{len(rows)} OK")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
