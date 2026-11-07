# BedahReview

**Live demo:** https://bedahreview-156186000276.asia-southeast2.run.app

A phone-first web app (installable PWA) for Indonesian online sellers. The seller pastes a **Tokopedia or TikTok Shop product link**, and BedahReview reads the complaints and sorts every review into what to do about it:

| Group | Meaning |
|---|---|
| **Perbaiki** (Fix) | A real problem the seller can fix |
| **Balas** (Respond) | Unfair or unclear review: a Bahasa reply draft (chat and public reply), no promise of removal. Reviews the AI is unsure about go here too |
| **Pertahankan** (Keep) | What buyers praise |
| **Abaikan** (Ignore) | Star-only, likely-fake or off-topic reviews |

Every card shows **"Kata pembeli"** (the exact buyer quote as proof) and **"Yang perlu kamu lakukan"** (what to do next). The result starts with a short **Ringkasan**. **Analisa Kompetitor** finds similar products, reads their complaints and shows where buyers prefer the competitor and where the seller is ahead. Finished fixes become a checklist in **Tugas**, and past analyses are kept in **Riwayat**.

Built as a product-management portfolio piece for Shopee and TikTok. Bahasa Indonesia first, English available (ID / EN switch in the header).

## What it does

- **One input:** "Tempel Link Produk" (Tokopedia and TikTok Shop). Shopee links are refused for now (Shopee blocks review access); Lazada is switched off (`BEDAHREVIEW_LAZADA=1` turns it back on). "Belum punya? Coba dengan contoh" shows a recorded example, with a recorded competitor comparison, without sign-in or any model call.
- **Reviews per link:** 10 by default, 20 at most.
- **Triage by Gemini** (`gemini-3.5-flash-lite` by default, `BEDAHREVIEW_MODEL` to change): batches of up to 20 reviews, schema-validated output, exact-quote proof, a fake-review check across reviews that share a template, and a courier-versus-product split. Star-only reviews are answered in code and never sent to the model.
- **Competitor analysis:** finds similar products (Tokopedia or TikTok Shop search), the seller picks 1 or 2, then gets a comparison with a chart. Costs one of the day's analyses.
- **Accounts:** Google or email 6-digit code through Supabase. A visitor gets one free analysis (counts and Ringkasan; quotes and drafts wait behind sign-in). Daily limit per account: `BEDAHREVIEW_USER_DAILY` (default 10).
- **Tugas / Selesai:** finishing an analysis makes a checklist box (30 days, synced to the account, task sentences only), with a calendar reminder file.
- **Privacy:** review text and buyer names are never logged or stored. Only a short fixed list of anonymous usage events is kept. `/privacy` and `/terms` are public (Bahasa and English), and "Hapus akun dan data saya" deletes the account and its rows.
- **Owner dashboard** at `/admin` (accounts, funnel, failures, feedback) for listed admin ids only.

## Stack

Standard-library Python server (`src/bedahreview/web.py`), one plain-JavaScript page (`static/index.html`), no build step and no Node. Gemini for the model, Supabase for accounts and small tables, Apify actors to read product links, Google Cloud Run to host (continuous deploy from `main`).

## Layout

| Path | What |
|---|---|
| `prompts/classifier.md`, `prompts/output_schema.json` | The classifier system prompt and the JSON Schema of one triaged review |
| `src/bedahreview/classifier.py` | `classify()`: prompt + Gemini + schema validation, batching |
| `src/bedahreview/competitors.py` | Competitor search, pick and comparison |
| `src/bedahreview/fetchers/` | Product-link readers (Apify actors), platform router, competitor search, 24-hour fetch cache |
| `src/bedahreview/web.py`, `static/index.html` | The server and the single-page phone-first app |
| `src/bedahreview/auth.py`, `account.py` | Supabase token check and account deletion |
| `src/bedahreview/events.py`, `event_store.py`, `admin_stats.py` | Anonymous usage events and the `/admin` numbers |
| `src/bedahreview/data/example_result.json` | The recorded example result |
| `static/manifest.webmanifest`, `static/sw.js`, `static/icons/` | Installable app shell (icons from `scripts/make_icons.py`); the service worker never caches `/api/` |
| `db/schema.sql`, `db/queries.sql` | Supabase tables, policies and questions to ask them |
| `tests/` | Offline tests (stub model, fake Supabase, headless Chrome for the page) |
| `docs/` | Eval results and the analytics event list (`docs/analytics.md`) |
| `eval/` | Eval data and results (gitignored, real review data stays local) |
| `CLAUDE.md` | Detailed product rules and decisions |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -e .
cp .env.example .env   # then add your keys; never commit .env
```

Run the tests (no API key needed; the page tests need Google Chrome):

```bash
python -W ignore -m unittest discover -s tests
```

Try the classifier on the demo reviews:

```bash
python -m bedahreview samples/demo_reviews.json
```

Real-model check: `tests/test_live_gemini.py` holds the schema and the page to a real Gemini reply recorded once from invented reviews (`samples/live_gemini_sample.json`, always checked offline). Its live half calls the API (a few US cents) and runs only when `GEMINI_API_KEY` is in the environment:

```bash
set -a; source .env; set +a; python -m unittest discover -s tests -p "test_live_gemini.py"
PYTHONPATH=src python scripts/record_live_sample.py   # record a fresh sample, for example after a prompt change
```

## Run the app

```bash
source .venv/bin/activate
python -m bedahreview.web
```

Open **http://localhost:8000** (not 127.0.0.1: Google sends people back to the address listed in Supabase). Ctrl+C stops it. A Python or `.env` change needs a restart; a change to the page file only needs a refresh. Without `SUPABASE_URL` the app runs open locally, with no sign-in.

Environment (names only; set them in `.env`, never in code or git):

| Name | For |
|---|---|
| `GEMINI_API_KEY`, `BEDAHREVIEW_MODEL` | The model |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` | Sign-in (the browser talks to Supabase directly) |
| `SUPABASE_SERVICE_KEY` | Server only: events, account deletion, `/admin` |
| `FETCHER=apify`, `APIFY_TOKEN` | Reading product links (paid) |
| `APIFY_MAX_COST_USD`, `APIFY_DAILY_BUDGET_USD` | Spending caps per run and per day |
| `BEDAHREVIEW_USER_DAILY`, `BEDAHREVIEW_FREE_DAILY`, `BEDAHREVIEW_PAID_USER_IDS` | Daily limits |
| `BEDAHREVIEW_ADMIN_USER_IDS` | Who may open `/admin` |
| `BEDAHREVIEW_CONTACT_EMAIL` | Contact address on the privacy page (empty = none) |
| `BEDAHREVIEW_ACCESS_CODE`, `BEDAHREVIEW_ACCESS_CODES` | Access codes at `/code` for interviewers or testers |

## Supabase

Run `db/schema.sql` in the Supabase SQL editor (safe to repeat; run it again after updates). It creates `profiles`, `events`, `fixes`, `history` and `task_boxes` with row level security. Never store review text, quotes, drafts, reviewer names or IP addresses. The email code needs the "Confirm signup" and "Magic Link" email templates to contain `{{ .Token }}`; Supabase's own sender allows 2 emails an hour. `static/vendor/supabase.js` is a pinned copy of `@supabase/supabase-js` 2.117.3, checked by a test.

## Deploy (Google Cloud Run)

A push to GitHub `main` builds the `Dockerfile` and deploys the service `bedahreview` (region `asia-southeast2`, min and max instances 1 because limits and background jobs live in memory, request timeout 300). Only pushed commits go live. Keys come from Secret Manager, never from the image or git. `PORT` being set (Cloud Run does this) turns on public mode, which needs Supabase sign-in or an access code and refuses to start with neither. `requirements.txt` and the `Dockerfile` use exact versions so a deploy repeats what was tested. To check a live deploy use `/api/config` (200); `/healthz` is caught by Google's front end.

An analysis runs as a background job: the page gets a job id at once and polls, so a dropped phone connection loses nothing (results stay in server memory for 10 minutes only).

## Product-link readers (demo only)

Paid third-party Apify actors, swappable in `src/bedahreview/fetchers/`. Platform terms are unverified: the app never bypasses blocks or logins, and tells the seller when a platform cannot be read.

| Platform | Reviews | Competitor search |
|---|---|---|
| Tokopedia | `shahidirfan/Tokopedia-Reviews-Scraper`, about $0.014 for 10 | `memo23/tokopedia-scraper`, about $0.02 |
| TikTok Shop | `web_wanderer/tiktok-reviews-scraper` | `cunning_soil/tiktok-shop-product-search-api`, $0.10 per search page |
| Lazada | off by default (`lergassy/lazada-scraper`, no product name or seller replies) | off |
| Shopee | none: blocked, links are refused and cost nothing | none |

Each run has a hard spending cap, and the daily Apify budget caps the day. Results are cached in `fetch_cache/` for 24 hours so the same link is not paid for twice. Reviewer names and seller replies are never kept.

## Eval

The classifier was scored against labels drafted by Claude: about 86-88% bucket accuracy (optimistic; a fresh hand-labelled set is still needed). Results: `docs/eval-results*.md`. The eval has not been re-run since batching changed to 20 reviews grouped by template.

```bash
python -m bedahreview.evaluate eval/data/review_log.csv --limit 10   # cheap smoke test
python -m bedahreview.evaluate eval/data/review_log.csv              # all reviews
```

It needs your Review log CSV in `eval/data/` (gitignored) and spends Gemini credit.

## Status and known gaps

Live on Cloud Run. Not yet verified: a full phone smoke test on the live site (Google sign-in unlock, email codes, iPhone install), real cost per analysis, the Bahasa drafts with a native speaker, Gemini's data terms and the platforms' terms. Next: a CI test run before deploy, a custom email sender, a backup model, a Shopee route and a fresh hand-labelled eval set.

## Copyright

Copyright © 2026 BedahReview. All rights reserved. See [LICENSE](LICENSE). Contact: [bedahreview@gmail.com](mailto:bedahreview@gmail.com)
