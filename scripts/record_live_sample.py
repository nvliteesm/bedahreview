"""Record one real Gemini reply to a small batch of INVENTED reviews, for samples/live_gemini_sample.json.

    PYTHONPATH=src python scripts/record_live_sample.py

It calls the live API once (a few US cents) with the key from .env. The reviews below are made up for this file: never put
reviews from the private sheet or from a real shop here, because the output is committed.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bedahreview.classifier import classify, resolve_model  # noqa: E402
from bedahreview.env import has_api_key, load_dotenv  # noqa: E402

PRODUCT = "Kaos Boxy Retro"
REVIEWS = [
    {"id": "1", "stars": 3, "variant": "Putih, M", "date": "2026-10-02", "text": "di foto warna tulisannya ijo, pas dateng malah hitam"},
    {"id": "2", "stars": 1, "variant": "Hitam, L", "date": "2026-10-03", "text": "kardusnya penyok parah dan kaosnya kotor kena oli, kecewa"},
    {"id": "3", "stars": 5, "variant": "Putih, L", "date": "2026-10-04", "text": "bahannya tebal dan adem, suka banget"},
    {"id": "4", "stars": 2, "variant": "Hitam, L", "date": "2026-10-05", "text": "size L rasa M, sempit di bahu, tolong cek size chart"},
    {"id": "5", "stars": 5, "variant": "", "date": "2026-10-06", "text": ""},
    {"id": "6", "stars": 1, "variant": "Putih, S", "date": "2026-10-07", "text": "kecewa, barangnya tidak sesuai deskripsi"},
    {"id": "7", "stars": 5, "variant": "Hitam, M", "date": "2026-10-08", "text": "pengiriman cepat dan adminnya ramah"},
]


def main() -> int:
    load_dotenv()
    if not has_api_key():
        print("GEMINI_API_KEY is not set. Add it to .env first.", file=sys.stderr)
        return 1
    out = classify(REVIEWS, product=PRODUCT)
    sample = {
        "note": "A real model reply to invented reviews, recorded once. See scripts/record_live_sample.py. No private reviews.",
        "model": resolve_model(),
        "recorded": time.strftime("%Y-%m-%d"),
        "product": PRODUCT,
        "input": REVIEWS,
        "output": out,
    }
    path = ROOT / "samples" / "live_gemini_sample.json"
    path.write_text(json.dumps(sample, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Recorded %d results from %s to %s" % (len(out), sample["model"], path.relative_to(ROOT)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
