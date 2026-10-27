import contextlib
import http.client
import io
import json
import threading
import unittest

from bedahreview.events import EVENTS, MAX_COMMENT_CHARS, clean_event, log_line
from bedahreview.events_report import format_report, parse_events, summarise
from bedahreview.web import make_server

DEVICE = "3fa9c2d41b7e8a06c5d1e2f3"
REVIEW = "Size L kayak M, sempit di bahu dan jahitannya lepas"


def event(name, **props):
    return {"event": name, "device": DEVICE, "props": props}


class CleanEventTests(unittest.TestCase):
    def test_every_allowed_event_is_accepted_with_its_fields(self):
        good = [event("opened"), event("analysed", reviews=10, source="paste", lang="id"), event("tab_viewed", tab="rivals"), event("draft_copied", kind="chat"),
                event("fix_marked"), event("rivals_started"), event("feedback", answer="partly"), event("feedback", answer="no", comment="  agak  lama ")]
        self.assertEqual({e["event"] for e in good}, set(EVENTS))
        for body in good:
            self.assertEqual(clean_event(body)["event"], body["event"])
        self.assertEqual(clean_event(good[-1])["comment"], "agak lama")

    def test_unknown_events_fields_and_types_are_refused(self):
        bad = [event("clicked"), event("opened", product="Kaos"), event("analysed", reviews=10, source="paste", lang="id", text=REVIEW),
               event("analysed", reviews="10", source="paste", lang="id"), event("analysed", reviews=True, source="paste", lang="id"), event("analysed", reviews=51, source="paste", lang="id"),
               event("analysed", reviews=10, source="https://tokopedia.com/x", lang="id"), event("tab_viewed", tab=REVIEW), event("draft_copied", kind="Halo Kak"),
               event("feedback", answer="maybe"), event("feedback"), event("feedback", answer="yes", comment="x" * (MAX_COMMENT_CHARS + 1)), event("feedback", answer="yes", comment=5),
               {"event": "opened", "device": DEVICE, "props": {}, "extra": 1}, {"event": "opened", "device": "x", "props": {}}, {"event": "opened", "device": DEVICE.upper(), "props": {}},
               {"event": "opened", "props": {}}, {"event": "opened", "device": DEVICE, "props": []}, ["opened"], "opened"]
        for body in bad:
            with self.assertRaises(ValueError, msg=body):
                clean_event(body)

    def test_no_event_can_carry_review_text_except_the_typed_comment(self):
        free_text_fields = [(name, field) for name, fields in EVENTS.items() for field, rule in fields.items() if rule[0] == "text"]
        self.assertEqual(free_text_fields, [("feedback", "comment")])  # every other field is a number or one of a fixed list of words
        for name, fields in EVENTS.items():
            for field, rule in fields.items():
                if rule[0] == "enum":
                    self.assertTrue(all(len(c) <= 7 for c in rule[1]), (name, field))  # short fixed words: a review cannot fit
        for name, fields in EVENTS.items():
            props = {}
            for field, rule in fields.items():
                props[field] = REVIEW if rule[0] != "int" else 5
                if rule[0] == "enum":
                    with self.assertRaises(ValueError):
                        clean_event(event(name, **props))
                if rule[0] == "int":
                    props[field] = 5

    def test_log_line_is_one_json_line_without_extras(self):
        line = log_line(clean_event(event("analysed", reviews=3, source="link", lang="en")))
        self.assertNotIn("\n", line)
        self.assertEqual(set(json.loads(line)), {"severity", "message", "ts", "event", "device", "reviews", "source", "lang"})


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server(0, classify_fn=lambda reviews, product=None: [])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, body, content_type="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        conn.request("POST", "/api/event", raw, {"Content-Type": content_type})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def test_an_allowed_event_is_written_as_one_json_line_on_stdout(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.post(event("tab_viewed", tab="fix")), 200)
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        logged = json.loads(lines[0])
        self.assertEqual((logged["event"], logged["tab"], logged["device"]), ("tab_viewed", "fix", DEVICE))

    def test_refused_events_write_nothing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            codes = [self.post(event("analysed", reviews=3, source="paste", lang="id", text=REVIEW)), self.post(event("nope")), self.post(b"{not json"), self.post(b"x" * 5000),
                     self.post(event("opened"), content_type="text/plain")]
        self.assertEqual(codes, [400, 400, 400, 400, 400])
        self.assertEqual(out.getvalue(), "")

    def test_it_needs_the_access_code_when_one_is_set(self):
        server = make_server(0, access_code="12345678")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("POST", "/api/event", json.dumps(event("opened")).encode(), {"Content-Type": "application/json"})
            status = conn.getresponse().status
            conn.close()
        self.assertEqual(status, 401)
        self.assertEqual(out.getvalue(), "")


class ReportTests(unittest.TestCase):
    def lines(self):
        def line(name, device=DEVICE, ts="2026-10-27T09:00:00Z", **props):
            logged = json.loads(log_line(clean_event({"event": name, "device": device, "props": props})))
            logged["ts"] = ts
            return json.dumps(logged)

        other = "aaaaaaaabbbbbbbbcccccccc"
        return "\n".join([line("opened"), line("analysed", reviews=10, source="paste", lang="id"), line("tab_viewed", tab="fight"), line("draft_copied", kind="chat"),
                          line("feedback", answer="yes", comment="cepat sekali"), line("opened", device=other), line("analysed", device=other, reviews=20, source="link", lang="en"),
                          line("feedback", device=other, answer="no"), "not json", '{"message":"something else"}'])

    def test_summary_counts_devices_and_the_feedback_split(self):
        s = summarise(parse_events(self.lines()))
        self.assertEqual((s["total"], s["devices"], s["devices_that_analysed"], s["devices_that_copied"]), (8, 2, 2, 1))
        self.assertEqual(s["analysed_by_source"], {"paste": 1, "link": 1})
        self.assertEqual(s["avg_reviews"], 15.0)
        self.assertEqual(s["feedback"], {"yes": 1, "partly": 0, "no": 1})
        self.assertEqual(s["comments"], [("yes", "cepat sekali")])
        text = format_report(s)
        self.assertIn("Was this useful? (2 answers)", text)
        self.assertIn("cepat sekali", text)

    def test_cloud_logging_json_lists_are_understood(self):
        entry = json.loads(log_line(clean_event(event("opened"))))
        wrapped = json.dumps([{"jsonPayload": entry, "severity": "INFO"}, {"textPayload": "GET /"}])
        self.assertEqual(len(parse_events(wrapped)), 1)

    def test_an_empty_log_says_so(self):
        self.assertIn("No usage events", format_report(summarise(parse_events(""))))


if __name__ == "__main__":
    unittest.main()
