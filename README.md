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
| `src/bedahreview/web.py`, `static/index.html` | The paste-in web app (step 4) |
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

## Product-link box (demo only)

Off by default. Paste is the main way to bring in reviews. To try fetching from a Shopee link:

1. Create an Apify account and copy your API token (Apify Console > Settings > API & Integrations).
2. In `.env` set `FETCHER=apify` and `APIFY_TOKEN=...` (never paste the token into code, chat or git).
3. Restart the app. The terminal says "Product-link box (demo): ON" and a dashed "Demo only" box appears.

It uses the Apify actor `zen-studio/shopee-product-reviews-scraper`, which costs roughly $0.004 per review plus
$0.008 per run. `APIFY_MAX_COST_USD` (default 0.05, about 10 reviews) is a hard cap on each run, so asking for
20 reviews at the default cap returns about 10. Results are saved in `fetch_cache/` so the same link is not
paid for twice. Only `shopee.co.id` product links are accepted. Shopee's terms for this have not been checked,
the service often returns nothing, and the app then tells the seller to paste instead. Reviewer names are
never kept. Tokopedia is not supported.

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
