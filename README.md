# codetheliar tools

Small tools I use to run [@codetheliar](https://x.com/codetheliar), built in public. Plain Python 3.9+, no dependencies.

## reply-rollup
Twice a day it finds the posts most worth replying to among ~60 accounts on a private X List, and ranks them. A Claude routine then turns the top picks into reply *angles*. It never writes finished replies and never posts.

- **Cheap on X's pay-per-use API:** it searches `from:` the List's accounts with `-is:retweet -is:reply`, so X filters before you're billed, and it caps each run (50 posts by default, about $0.25 max).
- **Ranking:** engagement per minute × freshness × how crowded the replies are × author reach.
- **Private Lists need user auth:** an app-only Bearer token can't read them, so requests are signed with OAuth 1.0a (stdlib only). Set your X app to **Read** only.
- **Private data stays out of this repo:** config, cached accounts and history live in `--data-dir`.

```
python3 reply-rollup/x_rollup.py --data-dir ~/my-data --sync-list   # cache the List's accounts
python3 reply-rollup/x_rollup.py --data-dir ~/my-data               # one run -> rollups/<time>.json
python3 reply-rollup/x_rollup.py --data-dir reply-rollup/fixtures \
  --fixture reply-rollup/fixtures/sample_list_tweets.json           # try it without the API
```
`config.json` in the data dir: `{"list_id": "...", "lookback_hours": 6, "top": 25, "max_posts_per_run": 50, "timezone": "America/Chicago"}`

Keys: `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET` env vars, or `--setup` to save them locally (chmod 600).

## radar
Hourly check for new AI releases, so I can test them while they're fresh: lab blogs, GitHub releases (new minor versions only), the Hacker News front page and a few X release accounts. New items land in [`radar/radar.md`](radar/radar.md). It runs on GitHub Actions ([workflow](.github/workflows/radar.yml)) and commits only when something new shows up. Details in [radar/README.md](radar/README.md).

## License
MIT
