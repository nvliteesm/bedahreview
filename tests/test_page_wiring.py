"""Checks on the page file that need no browser, for two kinds of bug a click-path audit found on 2026-11-04:
a script that looks up an element which is not in the page (the "Bedah ulasan" tab threw while an analysis ran), and an event the page sends
that the server refuses (opening Tugas made the server drop whole batches of events)."""
import re
import unittest
from pathlib import Path

from bedahreview.events import EVENTS, clean_batch, clean_event

PAGE = (Path(__file__).resolve().parents[1] / "src" / "bedahreview" / "static" / "index.html").read_text(encoding="utf-8")
DEVICE = "abcdef123456"


class PageWiringTests(unittest.TestCase):
    def test_every_element_the_script_looks_up_exists(self):
        in_html = set(re.findall(r'\bid="([^"]+)"', PAGE))
        made_by_script = set(re.findall(r'\bid:\s*"([^"]+)"', PAGE))  # h("input", { id: "rival-name", ... })
        looked_up = set(re.findall(r'\$\("([^"]+)"\)', PAGE)) | set(re.findall(r'getElementById\("([^"]+)"\)', PAGE))
        self.assertEqual(sorted(looked_up - in_html - made_by_script), [])

    def test_every_event_the_page_sends_is_one_the_server_accepts(self):
        names = set(re.findall(r'track\("([a-z_]+)"', PAGE))
        self.assertEqual(sorted(names - set(EVENTS)), [])
        allowed = set(EVENTS["tab_viewed"]["tab"][1])
        literal = set(re.findall(r'track\("tab_viewed", \{ tab: "([a-z]+)" \}', PAGE))
        tabs = set(re.findall(r'"([a-z]+)"', re.search(r"const TABS = \[([^\]]*)\]", PAGE).group(1)))  # the four group tabs, sent as a variable
        self.assertIn("tasks", literal)  # Tugas is one of the screens the page reports
        self.assertEqual(sorted((literal | tabs) - allowed), [])

    def test_opening_tugas_is_accepted_and_does_not_sink_the_batch_it_travels_in(self):
        self.assertEqual(clean_event({"event": "tab_viewed", "device": DEVICE, "props": {"tab": "tasks"}})["tab"], "tasks")
        batch = {"device": DEVICE, "events": [{"event": "opened"}, {"event": "analysed", "props": {"reviews": 10, "source": "link", "lang": "id"}},
                                              {"event": "tab_viewed", "props": {"tab": "tasks"}}]}
        self.assertEqual([e["event"] for e in clean_batch(batch)[1]], ["opened", "analysed", "tab_viewed"])


if __name__ == "__main__":
    unittest.main()
