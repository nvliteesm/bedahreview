import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PinTests(unittest.TestCase):
    def test_every_requirement_is_an_exact_version(self):
        lines = [l.strip() for l in (ROOT / "requirements.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
        self.assertTrue(lines)
        for line in lines:
            self.assertRegex(line, r"^[A-Za-z0-9_.-]+==\d+(\.\d+)+$", line)

    def test_the_docker_image_is_an_exact_tag(self):
        froms = re.findall(r"^FROM\s+(\S+)", (ROOT / "Dockerfile").read_text(), re.M)
        self.assertEqual(len(froms), 1)
        self.assertRegex(froms[0], r"^python:3\.\d+\.\d+-slim(-[a-z]+)?$")

    def test_a_deploy_from_source_never_uploads_private_or_test_files(self):
        ignored = {l.strip() for l in (ROOT / ".gcloudignore").read_text().splitlines()}
        for path in (".env", "eval/", "fetch_cache/", "tests/", "samples/", "scripts/", ".git"):
            self.assertIn(path, ignored, path)


if __name__ == "__main__":
    unittest.main()
