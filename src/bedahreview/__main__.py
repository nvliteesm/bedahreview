"""Try the classifier: python -m bedahreview samples/demo_reviews.json"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .classifier import ROOT, classify


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m bedahreview <reviews.json>", file=sys.stderr)
        return 2
    _load_dotenv()
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        print("GEMINI_API_KEY is not set. Add it to .env (see .env.example).", file=sys.stderr)
        return 1
    reviews = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(json.dumps(classify(reviews), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
