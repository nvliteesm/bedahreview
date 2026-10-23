# BedahReview: notes for Claude Code

URL box is demo-only and uses a swappable fetcher in `src/bedahreview/fetchers/`. Paste/upload is the default. Platform terms are unverified: never bypass blocks or logins, never store reviewer names, keep the token in .env. Tokopedia fetcher not built.

## Working rules
- Never put API keys or tokens in code, chat or git. `.env`, `eval/data/`, `eval/results/` and `fetch_cache/` stay untracked.
- Do not run a paid fetch (Apify) without asking the owner first; the per-run cost cap is `APIFY_MAX_COST_USD`.
- Run the app from the venv: `source .venv/bin/activate`. Tests: `python -m unittest discover -s tests`.
- After each build step, stage the files and draft a commit message; the owner commits and pushes.
