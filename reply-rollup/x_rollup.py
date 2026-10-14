#!/usr/bin/env python3
"""Pull recent posts from the accounts on an X List and rank them as reply candidates.

Run `--sync-list` after changing the List; it caches member handles in
accounts.json. Each run then searches those accounts for original posts,
capped at max_posts_per_run (X bills per post returned).

Reads only. Never posts, likes, or follows. Output is a JSON file that the
scheduled Claude routine turns into a rollup with reply angles.

Auth: OAuth 1.0a user context, so it can read a private List. Keys come from
env vars X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET, or from
~/.config/x-rollup/credentials.json (`--setup` writes it, chmod 600). Set the X
app to Read only.

Private data (config.json, accounts.json, state.json, rollups/) lives in
--data-dir, so the script can sit in a public repo and the data in a private one.
"""
import argparse
import base64
import getpass
import hashlib
import hmac
import json
import math
import secrets
import sys
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
API = "https://api.x.com/2"
CREDS_FILE = Path.home() / ".config" / "x-rollup" / "credentials.json"
CRED_KEYS = ("api_key", "api_secret", "access_token", "access_token_secret")
QUERY_SUFFIX = " -is:retweet -is:reply"
MAX_QUERY_LEN = 512


def setup_credentials():
    """Prompt for the four keys without echoing them, and save them chmod 600."""
    print("Paste each value from the app's Keys and tokens page. Input is hidden.")
    creds = {k: getpass.getpass(f"{k}: ").strip() for k in CRED_KEYS}
    if not all(creds.values()):
        sys.exit("A value was empty. Nothing saved.")
    CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CREDS_FILE.touch(mode=0o600)
    CREDS_FILE.chmod(0o600)
    CREDS_FILE.write_text(json.dumps(creds))
    print(f"Saved to {CREDS_FILE}")


def load_credentials():
    env = {k: os.environ.get(f"X_{k.upper()}", "") for k in CRED_KEYS}
    if all(env.values()):
        return env
    if not CREDS_FILE.exists():
        sys.exit("No credentials. Set X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN and "
                 f"X_ACCESS_TOKEN_SECRET, or run: python3 {Path(__file__).name} --setup")
    creds = json.loads(CREDS_FILE.read_text())
    missing = [k for k in CRED_KEYS if not creds.get(k)]
    if missing:
        sys.exit(f"{CREDS_FILE} is missing {', '.join(missing)}. Run --setup again.")
    return creds


def _enc(s):
    return urllib.parse.quote(str(s), safe="~")


def oauth1_header(method, url, params, creds, nonce=None, timestamp=None):
    """Build an OAuth 1.0a HMAC-SHA1 Authorization header (RFC 5849)."""
    oauth = {
        "oauth_consumer_key": creds["api_key"],
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(timestamp or int(time.time())),
        "oauth_token": creds["access_token"],
        "oauth_version": "1.0",
    }
    pairs = sorted((_enc(k), _enc(v)) for k, v in {**params, **oauth}.items())
    param_str = "&".join(f"{k}={v}" for k, v in pairs)
    base = "&".join([method.upper(), _enc(url), _enc(param_str)])
    key = f"{_enc(creds['api_secret'])}&{_enc(creds['access_token_secret'])}"
    sig = hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()
    oauth["oauth_signature"] = base64.b64encode(sig).decode()
    return "OAuth " + ", ".join(f'{_enc(k)}="{_enc(v)}"' for k, v in sorted(oauth.items()))


def api_get(path, params, creds):
    base_url = f"{API}{path}"
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="~")
    auth = oauth1_header("GET", base_url, params, creds)
    req = urllib.request.Request(f"{base_url}?{query}", headers={"Authorization": auth})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        sys.exit(f"X API error {e.code}: {e.read().decode(errors='replace')[:500]}")


def sync_accounts(creds, list_id, accounts_file):
    """Cache the List's member handles so each run can search them directly."""
    params = {"max_results": "100", "user.fields": "username"}
    handles = []
    while True:
        body = api_get(f"/lists/{list_id}/members", params, creds)
        handles += [u["username"] for u in body.get("data", [])]
        next_token = body.get("meta", {}).get("next_token")
        if not next_token:
            break
        params["pagination_token"] = next_token
    accounts_file.write_text(json.dumps(sorted(handles, key=str.lower), indent=2) + "\n")
    print(f"Saved {len(handles)} accounts to {accounts_file}")


def build_queries(handles):
    """Pack from: clauses into as few queries as fit X's query length limit."""
    queries, chunk = [], []
    for h in handles:
        candidate = chunk + [f"from:{h}"]
        if chunk and len(f"({' OR '.join(candidate)}){QUERY_SUFFIX}") > MAX_QUERY_LEN:
            queries.append(f"({' OR '.join(chunk)}){QUERY_SUFFIX}")
            candidate = [f"from:{h}"]
        chunk = candidate
    if chunk:
        queries.append(f"({' OR '.join(chunk)}){QUERY_SUFFIX}")
    return queries


def split_cap(cap, n):
    """Spread the per-run post cap across n queries (X allows 10-100 per request)."""
    base, extra = divmod(cap, n)
    return [max(10, min(100, base + (1 if i < extra else 0))) for i in range(n)]


def fetch_search_posts(creds, handles, cutoff, cap):
    """Original posts only (no reposts or replies), most relevant first, capped per run.

    X bills per post returned, so filtering and capping server-side is what keeps
    each run cheap.
    """
    queries = build_queries(handles)
    posts, users = [], {}
    for query, limit in zip(queries, split_cap(cap, len(queries))):
        params = {
            "query": query,
            "start_time": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "max_results": str(limit),
            "sort_order": "relevancy",
            "tweet.fields": "created_at,public_metrics,conversation_id,referenced_tweets,lang",
            "expansions": "author_id",
            "user.fields": "username,name,public_metrics",
        }
        body = api_get("/tweets/search/recent", params, creds)
        for u in body.get("includes", {}).get("users", []):
            users[u["id"]] = u
        posts.extend(body.get("data", []))
    return posts, users, len(queries)


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def is_original(post):
    """Keep standalone posts and quotes; skip reposts and replies to others."""
    for ref in post.get("referenced_tweets", []) or []:
        if ref["type"] in ("retweeted", "replied_to"):
            return False
    return True


def score(post, author, now):
    m = post.get("public_metrics", {})
    age_min = max((now - parse_time(post["created_at"])).total_seconds() / 60, 5)
    engagement = (
        m.get("like_count", 0)
        + 3 * m.get("retweet_count", 0)
        + 3 * m.get("quote_count", 0)
        + 2 * m.get("reply_count", 0)
        + 2 * m.get("bookmark_count", 0)
    )
    velocity = engagement / age_min
    followers = author.get("public_metrics", {}).get("followers_count", 0)
    # Fresh posts with momentum but not yet buried in replies are the best targets.
    freshness = math.exp(-age_min / 240)
    crowding = 1 / (1 + m.get("reply_count", 0) / 150)
    reach = math.log10(followers + 10)
    return round(velocity * freshness * crowding * reach * 100, 2), round(age_min)


def build_candidates(posts, users, seen, now, cutoff, top):
    out = []
    for p in posts:
        if p["id"] in seen or not is_original(p):
            continue
        if parse_time(p["created_at"]) < cutoff:
            continue
        author = users.get(p["author_id"], {})
        s, age = score(p, author, now)
        out.append({
            "id": p["id"],
            "url": f"https://x.com/{author.get('username', 'i')}/status/{p['id']}",
            "author": author.get("username"),
            "author_followers": author.get("public_metrics", {}).get("followers_count"),
            "age_minutes": age,
            "metrics": p.get("public_metrics", {}),
            "text": p["text"],
            "score": s,
        })
    out.sort(key=lambda c: c["score"], reverse=True)
    return out[:top]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=HERE, type=Path,
                    help="folder with config.json, accounts.json, state.json and rollups/")
    ap.add_argument("--config", type=Path, help="default: <data-dir>/config.json")
    ap.add_argument("--fixture", type=Path, help="use a saved API response instead of calling X")
    ap.add_argument("--setup", action="store_true", help="save your X API keys locally")
    ap.add_argument("--lookback-hours", type=float, help="override lookback_hours in config.json")
    ap.add_argument("--sync-list", action="store_true", help="refresh accounts.json from the X List")
    args = ap.parse_args()
    if args.setup:
        return setup_credentials()

    data = args.data_dir.resolve()
    cfg = json.loads((args.config or data / "config.json").read_text())
    accounts_file = data / "accounts.json"
    if args.sync_list:
        if not cfg.get("list_id"):
            sys.exit("config.json has no list_id yet. Create the X List and add its ID.")
        return sync_accounts(load_credentials(), cfg["list_id"], accounts_file)
    if args.lookback_hours:
        cfg["lookback_hours"] = args.lookback_hours
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=cfg.get("lookback_hours", 6))
    state_path = data / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"seen": {}}
    # Forget seen IDs after 3 days so state stays small.
    keep_after = (now - timedelta(days=3)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v > keep_after}

    if args.fixture:
        body = json.loads(args.fixture.read_text())
        posts = body.get("data", [])
        users = {u["id"]: u for u in body.get("includes", {}).get("users", [])}
        pages = 0
        if body.get("_fixture_now"):
            now = parse_time(body["_fixture_now"])
            cutoff = now - timedelta(hours=cfg.get("lookback_hours", 6))
    else:
        if not accounts_file.exists():
            sys.exit(f"No {accounts_file} yet. Run: python3 {Path(__file__).name} --sync-list")
        posts, users, pages = fetch_search_posts(
            load_credentials(),
            json.loads(accounts_file.read_text()),
            cutoff,
            cfg.get("max_posts_per_run", 50),
        )

    candidates = build_candidates(posts, users, state["seen"], now, cutoff, cfg.get("top", 25))
    for c in candidates:
        state["seen"][c["id"]] = now.isoformat()

    run = {
        "generated_at": now.isoformat(),
        "lookback_hours": cfg.get("lookback_hours", 6),
        "posts_fetched": len(posts),
        "requests": pages,
        "est_cost_usd": round(len(posts) * 0.005, 3),
        "candidates": candidates,
    }
    local = now.astimezone(ZoneInfo(cfg.get("timezone", "America/Chicago")))
    out_path = data / "rollups" / f"{local.strftime('%Y-%m-%d-%H%M')}.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(run, indent=2))
    if not args.fixture:
        state_path.write_text(json.dumps(state, indent=2))
    print(out_path)


if __name__ == "__main__":
    main()
