import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from bedahreview import __main__ as cli


class CliTests(unittest.TestCase):
    def run_cli(self, argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["bedahreview"] + argv), mock.patch.object(cli, "load_dotenv"), \
                mock.patch.dict(os.environ, env or {}, clear=True), redirect_stdout(out), redirect_stderr(err):
            code = cli.main()
        return code, out.getvalue(), err.getvalue()

    def test_usage_when_no_file_given(self):
        self.assertEqual(self.run_cli([])[0], 2)

    def test_missing_key_is_explained_and_never_calls_the_model(self):
        with mock.patch.object(cli, "classify", side_effect=AssertionError("no call")):
            code, _, err = self.run_cli(["x.json"])
        self.assertEqual(code, 1)
        self.assertIn("GEMINI_API_KEY", err)

    def test_prints_results_as_json_and_reports_failures_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "r.json"
            path.write_text(json.dumps([{"id": "1", "stars": 1, "variant": None, "date": "", "text": "rusak"}]))
            with mock.patch.object(cli, "classify", return_value=[{"id": "1"}]):
                code, out, _ = self.run_cli([str(path)], {"GEMINI_API_KEY": "k"})
            self.assertEqual((code, json.loads(out)), (0, [{"id": "1"}]))
            with mock.patch.object(cli, "classify", side_effect=TimeoutError("no response")):
                code, _, err = self.run_cli([str(path)], {"GEMINI_API_KEY": "k"})
            self.assertEqual(code, 1)
            self.assertIn("TimeoutError", err)
            self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
