# BedahReview

A seller pastes Shopee/Tokopedia review text and gets **Fix it or fight it** triage:
each review is labelled `fix`, `fight`, `check` (unsure: the seller looks into it), `ignore` or `keep` (praise), with a proof quote,
a Bahasa fix suggestion, Bahasa reply drafts for fight items, a likely-fake flag and a
courier-vs-product split.

v1 scope: paste-text input only. No scraping, no logins.

## Layout

| Path | What |
|---|---|
| `prompts/classifier.md` | The classifier system prompt (the core of v1) |
| `prompts/output_schema.json` | JSON Schema for one triaged review |
| `src/bedahreview/` | `classify()` (prompt + Gemini API + schema validation) and a CLI |
| `samples/` | Demo reviews for trying the classifier |
| `src/bedahreview/evaluate.py` | Eval: scores the classifier against the labelled Review log |
| `tests/` | Offline tests (stub client, no API key) |
| `eval/data/` | Your Review log CSV export (gitignored; real review data stays local) |
| `src/bedahreview/web.py`, `static/index.html` | The phone-first web app: three tabs, ID/EN, Share-menu pre-fill |
| `static/manifest.webmanifest`, `static/sw.js`, `static/icons/` | Installable app shell (PWA); icons come from `scripts/make_icons.py` |
| `src/bedahreview/parse.py` | Turns pasted text into reviews (stars, variant, text) |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -e .
cp .env.example .env   # then add your GEMINI_API_KEY
```

Try it on the 5 demo reviews, and run the tests (no API key needed):

```bash
python -m bedahreview samples/demo_reviews.json
python -m unittest discover -s tests
```

Set `BEDAHREVIEW_MODEL` to use a different Gemini model (default `gemini-3.5-flash-lite`).

The API key lives only in `.env` (gitignored).

## Run the app

```bash
source .venv/bin/activate
python -m bedahreview.web
```

Open http://127.0.0.1:8000, paste reviews, click **Bedah ulasan**. Press Ctrl+C in the terminal to stop it.
It only listens on your own computer, so your API key never leaves it. Up to 50 reviews per paste.
Paste one review per line (or separate multi-line reviews with a blank line). Stars and variant are
optional: `★★★☆☆  Putih, M   warna tulisannya ijo`.

## Install on a phone (PWA)

A phone needs https to install it, so the local app only installs on the same computer (Chrome's install icon).
Hosting is not chosen yet. Android Chrome can then add a Share-menu entry; iPhone cannot (as far as we know) and uses
paste only (Safari > Share > Add to Home Screen to install). The Share route is untested with Shopee and Tokopedia.

## Put it online (Google Cloud Run)

The app switches to public mode when `PORT` is set (Cloud Run does this). It then refuses to start without `BEDAHREVIEW_ACCESS_CODE`, shows a sign-in page, and caps analyses per visitor per hour (`BEDAHREVIEW_MAX_PER_HOUR`, default 30). The sign-in uses a shared code, so give it only to people you trust. Keys go in Secret Manager, never in the image or git. The link box stays off unless you also set `FETCHER=apify` and `APIFY_TOKEN` there, and that spends money.

Not tested here (no Docker or gcloud on this Mac): the image build and the deploy itself. Steps, run by the owner:

1. Create a Google Cloud project, link a billing account, and set a budget alert (it only warns; it does not stop spending).
2. Install the gcloud CLI, then `gcloud auth login` and `gcloud config set project YOUR_PROJECT_ID`.
3. Turn on the services: `gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com`
4. Store the secrets (`printf` keeps the key out of your shell history file only if your shell is set that way; paste carefully):
   `printf '%s' 'YOUR_GEMINI_KEY' | gcloud secrets create gemini-api-key --data-file=-`
   `printf '%s' 'A_LONG_ACCESS_CODE' | gcloud secrets create bedahreview-access-code --data-file=-`
5. Let Cloud Run read them: grant the role `roles/secretmanager.secretAccessor` on both secrets to the service account Cloud Run uses (the default is `PROJECT_NUMBER-compute@developer.gserviceaccount.com`).
6. Deploy from the repo folder (pick the region closest to your users; `asia-southeast2` is Jakarta, check that it is offered to you):
   `gcloud run deploy bedahreview --source . --region asia-southeast2 --allow-unauthenticated --max-instances 1 --set-secrets GEMINI_API_KEY=gemini-api-key:latest,BEDAHREVIEW_ACCESS_CODE=bedahreview-access-code:latest`
   `--allow-unauthenticated` is needed because the app does its own sign-in. `--max-instances 1` keeps the cost cap and the in-memory rate limits meaningful.
7. Open the URL it prints, sign in, and install it on your phone.

The rate limits live in memory, so they reset when the instance restarts. That is fine for a demo with one instance.

## Product-link box (demo only)

Off by default. Paste is the main way to bring in reviews. The box takes one link and picks the platform from it,
so the seller never chooses. To switch it on:

1. Create an Apify account and copy your API token (Apify Console > Settings > API & Integrations).
2. In `.env` set `FETCHER=apify` and `APIFY_TOKEN=...` (never paste the token into code, chat or git).
3. Restart the app. The terminal says "Product-link box (demo): ON" and a dashed "Demo only" box appears.

| Link | Actor (paid, third-party) | Tested 2026-10-26 (5 reviews) |
|---|---|---|
| Tokopedia product page | `shahidirfan/Tokopedia-Reviews-Scraper` | worked, 12.5 s |
| Lazada Indonesia product page | `zen-studio/lazada-reviews-scraper` | worked, 8.9 s |
| TikTok Shop (a `vt.tokopedia.com` share link, `shop-id.tokopedia.com`, or `tiktok.com/shop` link) | `web_wanderer/tiktok-reviews-scraper` | worked, 13.9 s |
| Shopee Indonesia | none | blocked: the actor reports `reviews_blocked`. A Shopee link gets a message and costs nothing |

Short links from the Share button are followed through plain redirects, only between known shop hosts. Each run has a
hard spending cap (`APIFY_MAX_COST_USD`, default 0.05). Results are saved in `fetch_cache/` per platform so the same
link is not paid for twice. Reviewer names are never kept. These are small third-party scrapers: any of them can
break or be blocked without warning, the platforms' terms for this have not been checked, and the app then tells
the seller to paste instead. The older Shopee fetcher (`apify_shopee.py`) is kept in the repo but is not used by
the router.

**Complaints first** (default) fetches 1-3 star reviews with comments, worst first. **Latest reviews** fetches the
newest ones. A few spare reviews are requested because some have no text.

## Run the eval

1. In the Google Sheet, open the **Review log** tab and choose File > Download > Comma-separated values.
2. Save it as `eval/data/review_log.csv` (this folder is gitignored).
3. Run:

```bash
python -m bedahreview.evaluate eval/data/review_log.csv --limit 10   # cheap smoke test
python -m bedahreview.evaluate eval/data/review_log.csv              # all reviews
```

It classifies one product at a time (so fake-review signals work within a product) and prints
bucket accuracy, per-bucket precision/recall, a confusion matrix, likely-fake and
courier "check first" scores, and the reviews where the model and the sheet disagree.
Full results are saved to `eval/results/` (gitignored).

The sheet has Yes/Partly/No "fixable" flags rather than fix/fight buckets, so `gold_bucket()`
in `evaluate.py` derives them: likely-fake or courier "Maybe" -> fight; `Praise:` theme -> keep;
fixable Yes/Partly -> fix; fixable No -> fight. The sheet's labels are drafts, so the score is
agreement with the sheet, not ground truth.

## Steps

1. Skeleton + classifier prompt (done)
2. `classify()` function: prompt + Gemini API, validated against the schema (done; not yet run against the live API; Gemini is the main provider, Groq/Qwen are a possible backup)
3. Eval script scoring against the labelled Review log (done; needs your CSV export to run)
4. Minimal paste-in web UI (done)
