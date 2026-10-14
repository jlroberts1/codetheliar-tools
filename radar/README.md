# Release radar

Checks every hour for new AI releases and appends them to [`radar.md`](radar.md), newest first.

## Sources (`sources.json`)
- **Lab blogs (free):** OpenAI, Google DeepMind, Hugging Face, and Simon Willison's `llm-release` tag. Anthropic has no RSS feed, so X covers it.
- **GitHub releases (free):** Claude Code, Codex, Gemini CLI, the MCP spec, Ollama. Only new minor/major versions are logged; daily patch releases are skipped.
- **Hacker News (free):** front-page stories with 150+ points whose titles match AI keywords.
- **X release accounts (paid, pay-per-use):** only posts that read like announcements. `since_id` means each post is fetched once, roughly $3–5/month. Set `"enabled": false` to skip X.

## How it runs
[`.github/workflows/radar.yml`](../.github/workflows/radar.yml) runs it at :17 past each hour. GitHub's scheduled runs can start a few minutes late. X keys come from repo secrets (`X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`); without them, X is skipped and the free sources still run. `state.json` remembers what's been seen. A commit happens only when something changed.

Run it locally:
```
python3 radar/radar.py --dry-run
```
