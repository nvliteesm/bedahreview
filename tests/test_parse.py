import unittest

from bedahreview.parse import parse_reviews


class ParseTests(unittest.TestCase):
    def test_demo_format_stars_variant_text(self):
        out = parse_reviews("★★★☆☆  Putih, M   di tampilan sebelom beli, warna tulisannya ijo")
        self.assertEqual(out[0]["stars"], 3)
        self.assertEqual(out[0]["variant"], "Putih, M")
        self.assertEqual(out[0]["text"], "di tampilan sebelom beli, warna tulisannya ijo")

    def test_one_review_per_line_when_no_blank_lines(self):
        out = parse_reviews("★★★★★ bagus\n★☆☆☆☆ jelek\nbiasa saja")
        self.assertEqual([r["id"] for r in out], ["1", "2", "3"])
        self.assertEqual([r["stars"] for r in out], [5, 1, None])
        self.assertEqual(out[2]["text"], "biasa saja")

    def test_blank_lines_separate_multiline_reviews(self):
        out = parse_reviews("4★ bagus\nbahan tebal\n\n2★ koyak\nsaat sampai")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["text"], "bagus\nbahan tebal")
        self.assertEqual(out[1]["stars"], 2)

    def test_numeric_star_forms(self):
        out = parse_reviews("4/5 mantap\nbintang 2: rusak\n3* biasa")
        self.assertEqual([r["stars"] for r in out], [4, 2, 3])
        self.assertEqual(out[1]["text"], "rusak")

    def test_leading_number_is_not_stars(self):
        out = parse_reviews("3 hari langsung rusak")
        self.assertIsNone(out[0]["stars"])
        self.assertEqual(out[0]["text"], "3 hari langsung rusak")

    def test_double_space_in_text_is_not_a_variant_without_comma(self):
        out = parse_reviews("5★  bagus  banget pakenya")
        self.assertIsNone(out[0]["variant"])
        self.assertEqual(out[0]["text"], "bagus  banget pakenya")

    def test_pipe_variant(self):
        out = parse_reviews("5★ Hitam | bagus banget")
        self.assertEqual(out[0]["variant"], "Hitam")
        self.assertEqual(out[0]["text"], "bagus banget")

    def test_stars_only_review_has_empty_text(self):
        out = parse_reviews("★★☆☆☆")
        self.assertEqual((out[0]["stars"], out[0]["text"]), (2, ""))

    def test_empty_and_whitespace(self):
        self.assertEqual(parse_reviews(""), [])
        self.assertEqual(parse_reviews("  \n\n \r\n"), [])

    def test_windows_newlines(self):
        self.assertEqual(len(parse_reviews("5★ a\r\n5★ b")), 2)


if __name__ == "__main__":
    unittest.main()
