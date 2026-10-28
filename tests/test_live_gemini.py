"""Checks against real Gemini output.

RecordedSampleTests always run, offline: they hold the schema and the page's data builder to a real model reply that was recorded once
(samples/live_gemini_sample.json, invented reviews only). LiveGeminiTests call the live API, so they run only when GEMINI_API_KEY is in
the environment when the tests start (the .env file is not read for this), and they cost a few US cents:

    set -a; source .env; set +a; python -m unittest tests.test_live_gemini    # from the BedahReview folder, venv on

To record a fresh sample: PYTHONPATH=src python scripts/record_live_sample.py
"""
import json
import os
import unittest
from pathlib import Path

from bedahreview.classifier import classify, validate_results
from bedahreview.evaluate import load_rows
from bedahreview.web import build_response

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = json.loads((ROOT / "samples" / "live_gemini_sample.json").read_text(encoding="utf-8"))
LIVE = bool(os.environ.get("GEMINI_API_KEY"))  # read once at import: a test that loads .env later must not switch this on


def _validate(reviews, results):
    """Star-only reviews are answered in code, never by the model, so only the reviews with text are held to the model's schema."""
    texted = [r for r in reviews if r["text"]]
    return validate_results(texted, [r for r in results if r["id"] in {t["id"] for t in texted}])


def _kits(results):
    return [r["fight"] for r in results if r["bucket"] in ("fight", "check")]


class RecordedSampleTests(unittest.TestCase):
    def test_the_recorded_reply_still_passes_the_schema(self):
        self.assertEqual(_validate(SAMPLE["input"], SAMPLE["output"]), [])

    def test_it_has_what_the_page_needs_in_both_languages(self):
        kits = _kits(SAMPLE["output"])
        self.assertTrue(kits)
        for kit in kits:
            self.assertTrue(kit["why"] and kit["why_id"])
            self.assertTrue(kit["steps"])
            self.assertEqual(len(kit["steps"]), len(kit["steps_id"]))  # the same steps, in the same order
        fix = [r for r in SAMPLE["output"] if r["bucket"] == "fix"]
        self.assertTrue(fix and all(r["fix_suggestion_id"] for r in fix))

    def test_the_page_data_builder_accepts_it(self):
        reviews = [{"id": r["id"], "stars": r["stars"], "variant": r["variant"], "date": r["date"], "text": r["text"]} for r in SAMPLE["input"]]
        data = build_response(reviews, SAMPLE["output"], SAMPLE["model"], {"kind": "paste", "title": SAMPLE["product"]})
        self.assertEqual(data["reviews"], len(SAMPLE["input"]))
        self.assertEqual(sum(data["counts"].values()), len(SAMPLE["output"]))
        self.assertTrue(data["groups"])

    def test_the_sample_holds_no_private_review(self):
        private = ROOT / "eval" / "data" / "review_log.csv"
        if not private.exists():
            self.skipTest("the private review sheet is not on this machine")
        sheet = {row["text"].strip().lower() for row in load_rows(private) if row.get("text")}
        for review in SAMPLE["input"]:
            self.assertNotIn(review["text"].strip().lower(), sheet)


@unittest.skipUnless(LIVE, "set GEMINI_API_KEY in the environment to call the live API (costs a few US cents)")
class LiveGeminiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reviews = SAMPLE["input"]
        cls.out = classify(cls.reviews, product=SAMPLE["product"])
        cls.by_id = {r["id"]: r for r in cls.out}

    def test_every_review_comes_back_in_order_and_validates(self):
        self.assertEqual([r["id"] for r in self.out], [r["id"] for r in self.reviews])
        self.assertEqual(_validate(self.reviews, self.out), [])

    def test_clear_cases_land_where_a_seller_would_expect(self):
        b = {i: r["bucket"] for i, r in self.by_id.items()}
        self.assertEqual(b["5"], "ignore")  # a star with no text never goes to the model
        self.assertEqual((b["3"], b["7"]), ("keep", "keep"))
        self.assertIn(b["1"], ("fix", "check"))  # the colour does not match the photo: the listing
        self.assertIn(b["4"], ("fix", "check"))  # sizing
        self.assertIn(b["2"], ("fight", "check"))  # a dented box and oil on the shirt: transit

    def test_fight_and_check_cards_carry_both_languages(self):
        kits = _kits(self.out)
        self.assertTrue(kits)
        for kit in kits:
            self.assertTrue(kit["why"].strip() and kit["why_id"].strip())
            self.assertEqual(len(kit["steps"]), len(kit["steps_id"]))
            self.assertTrue(kit["chat_to_buyer"])

    def test_the_page_data_builder_accepts_the_live_reply(self):
        data = build_response(self.reviews, self.out, "live", {"kind": "paste"})
        self.assertEqual(sum(data["counts"].values()), len(self.reviews))


if __name__ == "__main__":
    unittest.main()
