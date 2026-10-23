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
| `tests/` | Offline tests (stub client, no API key) |
| `eval/` | Eval script vs. the 100 hand-labelled reviews (step 3) |
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

## Steps

1. Skeleton + classifier prompt (done)
2. `classify()` function: prompt + Gemini API, validated against the schema (done; not yet run against the live API; Gemini is the main provider, Groq/Qwen are a possible backup)
3. Eval script scoring against the labelled Review log
4. Minimal paste-in web UI
