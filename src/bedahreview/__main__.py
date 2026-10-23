"""Try the classifier: python -m bedahreview samples/demo_reviews.json"""
from __future__ import annotations

import warnings

# Hide the old-Python / LibreSSL notices from Google's libraries (same as `python -W ignore`).
warnings.filterwarnings("ignore")

import json
import sys
from pathlib import Path

from .classifier import classify
from .env import has_api_key, load_dotenv


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m bedahreview <reviews.json>", file=sys.stderr)
        return 2
    load_dotenv()
    if not has_api_key():
        print("GEMINI_API_KEY is not set. Add it to .env (see .env.example).", file=sys.stderr)
        return 1
    reviews = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    try:
        results = classify(reviews)
    except Exception as exc:
        print("error: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
