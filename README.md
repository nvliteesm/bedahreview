# BedahReview

A seller pastes Shopee/Tokopedia review text and gets **Fix it or fight it** triage:
each review is labelled `fix`, `fight`, `ignore` or `keep` (praise), with a proof quote,
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
| `app/` | Minimal web UI (step 4) |

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

Set `BEDAHREVIEW_MODEL` to use a different Gemini model (default `gemini-3.8-flash`).

The API key lives only in `.env` (gitignored).

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
4. Minimal paste-in web UI
