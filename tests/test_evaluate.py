import csv
import tempfile
import unittest
from pathlib import Path

from bedahreview.classifier import ClassificationError
from bedahreview.evaluate import format_report, gold_bucket, gold_fake, load_rows, predict, score

HEADER = [
    "#", "Product ID", "Stars", "Review date", "Variant bought", "Review text (exact)", "Sentiment",
    "Theme (auto draft)", "Fixable by seller? (auto draft)", "Courier-caused? (auto draft)",
    "Category (auto)", "Verified on Shopee? (you)", "Likely fake? (auto)",
]
# invented rows in the sheet's format
ROWS = [
    ["1", "P1", "5", "1/1/2026", "Putih, L", "bahan tebal bagus", "Positive", "Praise: quality / material", "N/A", "No", "Apparel", "Yes", ""],
    ["2", "P1", "3", "1/1/2026", "Putih, M", "warna tulisan ijo jadi hitam", "Neutral", "Colour vs photo", "Yes", "No", "Apparel", "Yes", ""],
    ["3", "P1", "2", "1/1/2026", "Hitam, L", "Pas sampe koyak", "Negative", "Packaging / damaged on arrival", "Yes", "Maybe", "Apparel", "Yes", ""],
    ["4", "P2", "5", "1/1/2026", "", "kapsul glukosamin ini wajib", "Positive", "Praise: works as described", "N/A", "No", "Supplements", "Yes", "Yes: calls collagen glukosamin"],
    ["5", "P2", "5", "1/1/2026", "", "mantap", "Positive", "Praise: works as described", "N/A", "No", "Supplements", "Yes", "Check: very short, generic"],
    ["6", "P2", "2", "1/1/2026", "", "rasanya aneh", "Negative", "Taste / subjective", "No", "No", "Food", "Yes", ""],
]


def _row(**kw):
    base = {"text": "x", "theme": "Colour vs photo", "fixable": "Yes", "courier": "No", "fake_note": ""}
    base.update(kw)
    return base


def _pred(rid, bucket, theme="Colour vs photo", check_first=False, fake=False, hint=None):
    return {"id": rid, "fix_suggestion_id": hint, "bucket": bucket, "theme": theme, "cause": "none", "check_first": check_first,
            "likely_fake": {"flag": fake, "signals": []}}


class GoldTests(unittest.TestCase):
    def test_bucket_mapping(self):
        self.assertEqual(gold_bucket(_row(theme="Praise: taste", fixable="N/A")), "keep")
        self.assertEqual(gold_bucket(_row(fixable="Partly")), "fix")
        self.assertEqual(gold_bucket(_row(courier="Maybe")), "fight")
        self.assertEqual(gold_bucket(_row(fixable="No", theme="Taste / subjective")), "fight")
        self.assertEqual(gold_bucket(_row(fake_note="Yes: translated", theme="Praise: x", fixable="N/A")), "fight")
        self.assertEqual(gold_bucket(_row(text="")), "ignore")
        self.assertIsNone(gold_bucket(_row(fixable="")))

    def test_fake_note(self):
        self.assertTrue(gold_fake(_row(fake_note="Yes: same template")))
        self.assertIsNone(gold_fake(_row(fake_note="Check: very short")))
        self.assertFalse(gold_fake(_row()))


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "log.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(HEADER)
            w.writerows(ROWS)
        self.rows = load_rows(path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_rows_maps_columns(self):
        self.assertEqual(len(self.rows), 6)
        self.assertEqual(self.rows[1]["text"], "warna tulisan ijo jadi hitam")
        self.assertEqual(self.rows[3]["fake_note"], "Yes: calls collagen glukosamin")
        self.assertEqual(self.rows[0]["variant"], "Putih, L")

    def test_missing_column_raises(self):
        bad = Path(self.tmp.name) / "bad.csv"
        bad.write_text("#,Stars\n1,5\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_rows(bad)

    def test_perfect_and_imperfect_scores(self):
        preds = {
            "1": _pred("1", "keep", "Praise: quality / material"),
            "2": _pred("2", "fix"),
            "3": _pred("3", "fight", "Packaging / damaged on arrival", check_first=True),
            "4": _pred("4", "fight", "Praise: works as described", fake=True),
            "5": _pred("5", "keep", "Praise: works as described"),
            "6": _pred("6", "fix", "Taste / subjective"),  # sheet says fight
        }
        m = score(self.rows, preds, {})
        self.assertEqual(m["scored"], 6)
        self.assertAlmostEqual(m["bucket_accuracy"], 5 / 6)
        self.assertEqual(m["confusion"]["fight"]["fix"], 1)
        self.assertEqual(m["likely_fake"]["tp"], 1)  # row 5 ("Check:") is left out
        self.assertEqual(m["likely_fake"]["fn"], 0)
        self.assertEqual(m["check_first_vs_courier"]["tp"], 1)
        self.assertEqual(m["mismatches"][0]["id"], "6")
        self.assertIn("Bucket accuracy: 83%", format_report(m))

    def test_fix_suggestion_coverage_counts_fight_cards_with_a_hint(self):
        # gold=fix rows are 2 (Colour vs photo) -> one fix card, one fight card that carries a hint
        preds = {r["id"]: _pred(r["id"], "keep") for r in self.rows}
        preds["2"] = _pred("2", "fix", hint="Ganti foto.")
        self.assertEqual(score(self.rows, preds, {})["fix_suggestion_coverage"], 1.0)
        preds["2"] = _pred("2", "fight", hint="Cek deskripsi.")
        self.assertEqual(score(self.rows, preds, {})["fix_suggestion_coverage"], 1.0)
        preds["2"] = _pred("2", "fight")
        self.assertEqual(score(self.rows, preds, {})["fix_suggestion_coverage"], 0.0)

    def test_missed_fake_and_false_courier(self):
        preds = {r["id"]: _pred(r["id"], "fix", check_first=(r["id"] == "1")) for r in self.rows}
        m = score(self.rows, preds, {})
        self.assertEqual(m["likely_fake"]["fn"], 1)
        self.assertEqual(m["check_first_vs_courier"]["fp"], 1)
        self.assertEqual(m["check_first_vs_courier"]["fn"], 1)

    def test_predict_groups_by_product_and_survives_failures(self):
        seen = []

        def fake_classify(reviews):
            seen.append([r["id"] for r in reviews])
            if reviews[0]["id"] == "4":
                raise ClassificationError("invalid reply after retry:\n- bad")
            return [_pred(r["id"], "fix") for r in reviews]

        preds, failures = predict(self.rows, fake_classify)
        self.assertEqual(seen, [["1", "2", "3"], ["4", "5", "6"]])
        self.assertEqual(sorted(preds), ["1", "2", "3"])
        self.assertEqual(sorted(failures), ["4", "5", "6"])
        m = score(self.rows, preds, failures)
        self.assertEqual((m["scored"], m["failed"]), (3, 3))

    def test_non_classification_errors_do_not_abort_the_run(self):
        def flaky(reviews):
            if reviews[0]["id"] == "1":
                raise TimeoutError("deadline exceeded")
            return [_pred(r["id"], "fix") for r in reviews]

        preds, failures = predict(self.rows, flaky)
        self.assertEqual(sorted(preds), ["4", "5", "6"])
        self.assertIn("deadline exceeded", failures["1"])


if __name__ == "__main__":
    unittest.main()
