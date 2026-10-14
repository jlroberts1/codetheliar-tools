# codetheliar-tools

Public repo. Never commit keys, `credentials.json`, or anything from the private `x-personal-brand` repo (List ID, accounts, rollups). X keys only ever come from env vars or GitHub secrets.

## Git gating (overrides Agentic SDLC invariant 4)
The only git action gated is a push that deploys. Everything else is pre-authorized: add, commit, amend unpushed commits, branches, local tags, and pushes to non-deploy branches.

**This repo deploys nothing**, so pushes to `main` don't need approval (exception granted by the owner, 2026-10-14). The radar workflow on GitHub Actions also commits to `main` hourly; run `git pull --rebase` before pushing.

Still ask first:
- force-push, or rewriting history that's already on GitHub
- `gh release create`, or making this repo private
