# BedahReview: notes for Claude Code

URL box is demo-only and uses a swappable fetcher in `src/bedahreview/fetchers/`. Paste/upload is the default. Platform terms are unverified: never bypass blocks or logins, never store reviewer names, keep the token in .env. The link box routes Tokopedia, Lazada and TikTok Shop links to paid Apify actors (`fetchers/platforms.py`); Shopee Indonesia is blocked, so its links are refused with a message.

## Working rules
- Never put API keys or tokens in code, chat or git. `.env`, `eval/data/`, `eval/results/` and `fetch_cache/` stay untracked.
- Do not run a paid fetch (Apify) without asking the owner first; the per-run cost cap is `APIFY_MAX_COST_USD`.
- Run the app from the venv: `source .venv/bin/activate`. Tests: `python -m unittest discover -s tests`.
- After each build step, stage the files and draft a commit message; the owner commits and pushes.

## Product direction
Phone-first PWA. Triage = Fix / Fight / Ignore with exact-quote proof and Bahasa drafts. Fight it never promises review removal. Model name and keys in .env. No reviewer names stored. Fix tracking comes after the triage works.
- The model still returns five buckets (fix, fight, check, ignore, keep); the page shows three tabs: check goes under Tanggapi, keep is the "Pertahankan" strip, star-only reviews are answered in code (Ignore) and never sent to the model. Likely-fake reviews are left out of every count.
- The model env var stays `BEDAHREVIEW_MODEL`. Never log review text: the Share menu puts it in the URL query, so the request log prints the path only.
- The service worker caches the app shell only, never `/api/`.
