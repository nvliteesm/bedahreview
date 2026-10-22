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
| `src/bedahreview/` | Python package (classifier call comes in step 2) |
| `eval/` | Eval script vs. the 100 hand-labelled reviews (step 3) |
| `app/` | Minimal web UI (step 4) |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then add your ANTHROPIC_API_KEY
```

The API key lives only in `.env` (gitignored).

## Steps

1. Skeleton + classifier prompt (this commit)
2. `classify()` function: prompt + Claude API, validated against the schema
3. Eval script scoring against the labelled Review log
4. Minimal paste-in web UI
