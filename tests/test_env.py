import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bedahreview import env


class DotenvTests(unittest.TestCase):
    def load(self, text, existing=None):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".env").write_text(text, encoding="utf-8")
            with mock.patch.object(env, "ROOT", Path(d)), mock.patch.dict(os.environ, existing or {}, clear=True):
                env.load_dotenv()
                return dict(os.environ)

    def test_reads_plain_quoted_and_commented_lines(self):
        got = self.load("# note\nA=1\nB=\"two words\"\nC='3'\n\nnot a setting\n")
        self.assertEqual((got["A"], got["B"], got["C"]), ("1", "two words", "3"))

    def test_trailing_comment_is_not_part_of_the_value(self):
        got = self.load("APIFY_MAX_COST_USD=0.05   # about 10 reviews\nTOKEN=abc#def\nQ=\"keep # this\"\n")
        self.assertEqual(got["APIFY_MAX_COST_USD"], "0.05")
        self.assertEqual(got["TOKEN"], "abc#def")  # no space before #, so it belongs to the value
        self.assertEqual(got["Q"], "keep # this")

    def test_real_environment_wins_over_the_file(self):
        self.assertEqual(self.load("A=file\n", {"A": "shell"})["A"], "shell")

    def test_missing_file_is_fine_and_has_api_key(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(env, "ROOT", Path(d)), mock.patch.dict(os.environ, {}, clear=True):
            env.load_dotenv()
            self.assertFalse(env.has_api_key())
            os.environ["GEMINI_API_KEY"] = "x"
            self.assertTrue(env.has_api_key())
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=True):
            self.assertFalse(env.has_api_key())  # an empty key from .env.example does not count


if __name__ == "__main__":
    unittest.main()
