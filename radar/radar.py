#!/usr/bin/env python3
"""Release radar: spot new AI releases fast so you can test and post within hours.

Checks free sources first (lab blogs, GitHub releases, Hacker News), plus a
few X release accounts via the API (each post is fetched once, using since_id).
New items go to radar.md, newest first.

Runs hourly on GitHub Actions (see README.md), or anywhere with Python 3.9+.
The first run only records what's already out, so old items don't flood the log.
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "reply-rollup"))
import x_rollup  # noqa: E402  (OAuth signing + credentials, shared with the rollup)

SOURCES = HERE / "sources.json"
STATE = HERE / "state.json"
LOG = HERE / "radar.md"
LOG_HEADER = "# Release radar\n\nNewest first. Written by radar.py every hour.\n\n"
LOG_MAX_LINES = 400
MAX_AGE = timedelta(hours=48)
ATOM = "{http://www.w3.org/2005/Atom}"
UA = {"User-Agent": "release-radar/1.0 (personal script)"}


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            return parsedate_to_datetime(s)
        except (TypeError, ValueError):
            return None


def parse_feed(xml_bytes):
    """Yield (id, title, url, published) from RSS or Atom."""
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        yield (item.findtext("guid") or link, (item.findtext("title") or "").strip(), link,
               parse_date(item.findtext("pubDate")))
    for entry in root.iter(f"{ATOM}entry"):
        link = next((l.get("href") for l in entry.findall(f"{ATOM}link")
                     if l.get("rel", "alternate") == "alternate"), "")
        yield (entry.findtext(f"{ATOM}id") or link, (entry.findtext(f"{ATOM}title") or "").strip(),
               link, parse_date(entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated")))


def minor_version(tag):
    """'rust-v0.46.2' -> (0, 46). None for pre-releases or tags without a version."""
    if re.search(r"alpha|beta|rc|preview|nightly|dev", tag, re.I):
        return None
    m = re.search(r"(\d+)\.(\d+)", tag)
    return (int(m.group(1)), int(m.group(2))) if m else None


def check_feeds(cfg, now, errors):
    items = []
    for f in cfg.get("feeds", []):
        try:
            for id_, title, url, published in parse_feed(fetch(f["url"])):
                if published and now - published > MAX_AGE:
                    continue
                items.append({"id": f"feed:{id_}", "source": f["name"], "title": title,
                              "url": url})
        except Exception as e:  # one broken feed shouldn't stop the rest
            errors.append(f"{f['name']}: {e}")
    return items


def check_github(cfg, state, now, errors):
    """Only new minor/major versions; daily patch releases are noise."""
    items, last = [], state.setdefault("github_minor", {})
    for repo in cfg.get("github_releases", []):
        try:
            entries = list(parse_feed(fetch(f"https://github.com/{repo}/releases.atom")))
        except Exception as e:
            errors.append(f"{repo}: {e}")
            continue
        for id_, title, url, published in reversed(entries):  # oldest first
            ver = minor_version(url.rsplit("/", 1)[-1]) or minor_version(title)
            if not ver or (published and now - published > MAX_AGE):
                continue
            if repo in last and tuple(last[repo]) >= ver:
                continue
            last[repo] = list(ver)
            items.append({"id": f"gh:{id_}", "source": f"GitHub {repo}",
                          "title": f"{repo.split('/')[-1]} {title}", "url": url})
    return items


def check_hn(cfg, errors):
    hn = cfg.get("hacker_news")
    if not hn:
        return []
    try:
        body = json.loads(fetch("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=50"))
    except Exception as e:
        errors.append(f"Hacker News: {e}")
        return []
    kw = re.compile(rf"\b({hn['keywords']})", re.I)
    return [{"id": f"hn:{h['objectID']}", "source": f"HN {h.get('points', 0)} pts",
             "title": h.get("title", ""),
             "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
             }
            for h in body.get("hits", [])
            if h.get("points", 0) >= hn["min_points"] and kw.search(h.get("title", ""))]


def check_x(cfg, state, now, errors):
    """Release-style posts from a few X accounts. Billed per post; since_id avoids refetching."""
    xc = cfg.get("x_accounts", {})
    if not xc.get("enabled") or not xc.get("handles"):
        return [], 0
    query = "(" + " OR ".join(f"from:{h}" for h in xc["handles"]) + ")" + x_rollup.QUERY_SUFFIX
    params = {
        "query": query,
        "max_results": str(max(10, min(100, xc.get("max_posts_per_run", 10)))),
        "tweet.fields": "created_at",
        "expansions": "author_id",
        "user.fields": "username",
    }
    if state.get("x_since_id"):
        params["since_id"] = state["x_since_id"]
    else:
        params["start_time"] = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        body = x_rollup.api_get("/tweets/search/recent", params, x_rollup.load_credentials())
    except SystemExit as e:  # api_get exits on HTTP errors; keep the free sources going
        errors.append(f"X: {e}")
        return [], 0
    if body.get("meta", {}).get("newest_id"):
        state["x_since_id"] = body["meta"]["newest_id"]
    users = {u["id"]: u["username"] for u in body.get("includes", {}).get("users", [])}
    kw = re.compile(xc.get("notify_keywords", "."), re.I)
    posts = body.get("data", [])
    items = []
    for p in posts:
        if not kw.search(p["text"]):
            continue
        user = users.get(p["author_id"], "i")
        items.append({"id": f"x:{p['id']}", "source": f"X @{user}",
                      "title": " ".join(p["text"].split())[:140],
                      "url": f"https://x.com/{user}/status/{p['id']}"})
    return items, len(posts)


def write_log(items, now, tz):
    existing = LOG.read_text()[len(LOG_HEADER):] if LOG.exists() else ""
    stamp = now.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    lines = [f"- {stamp} · **{it['source']}** · [{it['title'] or 'untitled'}]({it['url']})\n"
             for it in items]
    body = "".join(lines) + existing
    LOG.write_text(LOG_HEADER + "".join(body.splitlines(keepends=True)[:LOG_MAX_LINES]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print new items; don't save")
    args = ap.parse_args()

    cfg = json.loads(SOURCES.read_text())
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    now = datetime.now(timezone.utc)
    seen = state.setdefault("seen", {})
    first_run = not state.get("seeded")
    errors = []

    items = check_feeds(cfg, now, errors) + check_github(cfg, state, now, errors) + check_hn(cfg, errors)
    x_items, x_fetched = check_x(cfg, state, now, errors)
    items += x_items

    new = []
    for it in items:
        if it["id"] not in seen:
            seen[it["id"]] = now.isoformat()
            new.append(it)

    month = now.strftime("%Y-%m")
    cost = state.setdefault("x_cost_usd", {})
    cost[month] = round(cost.get(month, 0) + x_fetched * 0.005, 3)

    if args.dry_run:
        for it in new:
            print(f"{it['source']}: {it['title']} {it['url']}")
        print(f"{len(new)} new, X posts fetched: {x_fetched}, errors: {errors}")
        return

    keep_after = (now - timedelta(days=14)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v > keep_after}
    state["seeded"] = True
    state["last_errors"] = errors
    if not first_run and new:
        write_log(new, now, ZoneInfo(cfg.get("timezone", "America/Chicago")))
    STATE.write_text(json.dumps(state, indent=2))
    print(f"{'seeded' if first_run else 'ok'}: {len(new)} new, X posts {x_fetched}, errors {len(errors)}")


if __name__ == "__main__":
    main()
