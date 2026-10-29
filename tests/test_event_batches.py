"""Usage events sent in batches and saved to Supabase from a background thread. Fake Supabase, no network."""
import contextlib
import http.client
import io
import json
import sys
import threading
import time
import unittest
import urllib.error

from bedahreview import web
from bedahreview.auth import SupabaseAuth
from bedahreview.event_store import MAX_PENDING, EventSink
from bedahreview.events import MAX_BATCH, clean_batch, db_row

try:
    from test_auth import URL, FakeSupabase, new_key, token
except ImportError:  # run as tests.<module>
    from tests.test_auth import URL, FakeSupabase, new_key, token

DEVICE = "3fa9c2d41b7e8a06c5d1e2f3"
SECRET = "sb_secret_TESTKEY0123456789"


class CleanBatchTests(unittest.TestCase):
    def test_a_good_batch_is_read_event_by_event(self):
        device, events = clean_batch({"device": DEVICE, "events": [{"event": "opened"}, {"event": "tab_viewed", "props": {"tab": "fix"}}]})
        self.assertEqual((device, [e["event"] for e in events]), (DEVICE, ["opened", "tab_viewed"]))

    def test_one_bad_event_refuses_the_whole_batch(self):
        good = {"event": "opened"}
        bad = {
            "unknown event": {"event": "hacked"}, "unknown field": {"event": "opened", "props": {"email": "a@b.c"}}, "wrong type": {"event": "analysed", "props": {"reviews": "ten", "source": "paste", "lang": "id"}},
            "extra key": {"event": "opened", "user_id": "someone-else"}, "not an object": "opened", "no event name": {},
        }
        for why, item in bad.items():
            with self.assertRaises(ValueError, msg=why):
                clean_batch({"device": DEVICE, "events": [good, item]})

    def test_the_batch_itself_is_checked(self):
        for why, body in {"empty": {"device": DEVICE, "events": []}, "too many": {"device": DEVICE, "events": [{"event": "opened"}] * (MAX_BATCH + 1)}, "no device": {"events": [{"event": "opened"}]},
                          "bad device": {"device": "x", "events": [{"event": "opened"}]}, "events not a list": {"device": DEVICE, "events": {"event": "opened"}}, "extra key": {"device": DEVICE, "events": [{"event": "opened"}], "x": 1},
                          "not an object": [{"event": "opened"}]}.items():
            with self.assertRaises(ValueError, msg=why):
                clean_batch(body)

    def test_a_database_row_has_the_account_the_event_and_its_fixed_fields_only(self):
        _, events = clean_batch({"device": DEVICE, "events": [{"event": "analysed", "props": {"reviews": 7, "source": "paste", "lang": "id"}}]})
        self.assertEqual(db_row("user-1", events[0]), {"user_id": "user-1", "event": "analysed", "props": {"reviews": 7, "source": "paste", "lang": "id"}})
        self.assertNotIn(DEVICE, json.dumps(db_row("user-1", events[0])))  # the device id never reaches the table


class SinkTests(unittest.TestCase):
    def capture_stderr(self):
        buf = io.StringIO()
        old, sys.stderr = sys.stderr, buf
        self.addCleanup(setattr, sys, "stderr", old)
        return buf

    def test_rows_are_saved_in_the_background_and_batched(self):
        posted = []
        sink = EventSink(URL, SECRET, post=lambda rows: posted.append(list(rows)))
        for i in range(5):
            sink.submit([{"user_id": "u", "event": "opened", "props": {}, "n": i}])
        sink.wait_idle()
        self.assertEqual(sum(len(p) for p in posted), 5)  # all arrived, in however many posts

    def test_supabase_failing_drops_quietly_with_one_warning_until_it_works_again(self):
        err = self.capture_stderr()
        outcomes = [urllib.error.HTTPError(URL, 401, "no", None, None), TimeoutError("slow"), None, OSError("down")]
        posted = []

        def post(rows):
            outcome = outcomes.pop(0)
            if outcome:
                raise outcome
            posted.append(rows)
        sink = EventSink(URL, SECRET, post=post)
        for _ in range(4):
            sink.submit([{"user_id": "u", "event": "opened", "props": {}}])
            sink.wait_idle()  # one at a time, so each is its own attempt
        warnings = [l for l in err.getvalue().splitlines() if l.startswith("events not saved")]
        self.assertEqual(warnings, ["events not saved to Supabase: HTTP 401", "events not saved to Supabase: OSError"])  # one per streak: 401 and timeout share one, then a success, then a new failure
        self.assertEqual(len(posted), 1)
        self.assertNotIn(SECRET, err.getvalue())

    def test_a_full_queue_drops_rows_instead_of_blocking(self):
        err = self.capture_stderr()
        gate = threading.Event()
        sink = EventSink(URL, SECRET, post=lambda rows: gate.wait(5))
        results = [sink.submit([{"user_id": "u", "event": "opened", "props": {}}]) for _ in range(MAX_PENDING + 20)]
        gate.set()
        self.assertIn(False, results)
        self.assertEqual(err.getvalue().count("queue full"), 1)
        sink.wait_idle()


class ServerTests(unittest.TestCase):
    CODES = {"ani": "ani-secret-1"}
    USER = "5b1f2c3a-1111-4222-8333-444455556666"

    def setUp(self):
        self.key, jwk = new_key("k1")
        self.saved, self.down = [], [False]

        def post(rows):
            if self.down[0]:
                raise OSError("supabase paused")
            self.saved.extend(rows)
        self.sink = EventSink(URL, SECRET, post=post)
        self.server = web.make_server(0, classify_fn=lambda reviews, product=None: [], access_codes=dict(self.CODES), public=True, event_sink=self.sink,
                                      supabase_auth=SupabaseAuth(URL, fetch=FakeSupabase(jwk)), supabase_anon_key="sb_publishable_x")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, path, payload, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            conn.request("POST", path, body=json.dumps(payload).encode(), headers=dict({"Content-Type": "application/json"}, **(headers or {})))
            resp = conn.getresponse()
            body = resp.read()
        conn.close()
        self.sink.wait_idle()
        return resp.status, body, out.getvalue()

    def bearer(self):
        return {"Authorization": "Bearer " + token(self.key)}

    def cookie(self):
        return {"Cookie": web.COOKIE + "=" + web.session_token("ani", self.CODES["ani"], int(time.time()) + 100)}

    BATCH = {"device": DEVICE, "events": [{"event": "opened"}, {"event": "tab_viewed", "props": {"tab": "fix"}}, {"event": "draft_copied", "props": {"kind": "chat"}}]}

    def test_an_accounts_batch_is_saved_against_the_account_and_logged(self):
        status, body, printed = self.post("/api/events", self.BATCH, self.bearer())
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        self.assertEqual(self.saved, [{"user_id": self.USER, "event": "opened", "props": {}}, {"user_id": self.USER, "event": "tab_viewed", "props": {"tab": "fix"}}, {"user_id": self.USER, "event": "draft_copied", "props": {"kind": "chat"}}])
        lines = [json.loads(l) for l in printed.splitlines()]
        self.assertEqual([l["event"] for l in lines], ["opened", "tab_viewed", "draft_copied"])  # the log line is still printed
        self.assertTrue(all(l["device"] == DEVICE and "user_id" not in l for l in lines))  # the log never carries the account

    def test_an_access_code_tester_only_gets_the_log_line(self):
        status, _, printed = self.post("/api/events", self.BATCH, self.cookie())
        self.assertEqual((status, self.saved), (200, []))
        self.assertEqual(len(printed.splitlines()), 3)

    def test_nobody_signed_in_and_bad_batches_save_nothing(self):
        self.assertEqual(self.post("/api/events", self.BATCH)[0], 401)
        self.assertEqual(self.post("/api/events", {"device": DEVICE, "events": [{"event": "opened"}, {"event": "hacked"}]}, self.bearer())[0], 400)
        self.assertEqual(self.post("/api/events", {"device": DEVICE, "events": [{"event": "opened"}] * (MAX_BATCH + 1)}, self.bearer())[0], 400)
        self.assertEqual(self.saved, [])

    def test_the_old_single_event_endpoint_saves_too(self):
        status, _, _ = self.post("/api/event", {"event": "opened", "device": DEVICE, "props": {}}, self.bearer())
        self.assertEqual((status, [r["event"] for r in self.saved]), (200, ["opened"]))

    def test_supabase_being_down_never_hurts_the_visitor(self):
        self.down[0] = True
        status, body, printed = self.post("/api/events", self.BATCH, self.bearer())
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        self.assertEqual((self.saved, len(printed.splitlines())), ([], 3))  # the counts are still in the log

    def test_the_secret_key_never_appears_in_a_reply(self):
        for path, payload, headers in (("/api/events", self.BATCH, self.bearer()), ("/api/events", {"bad": 1}, self.bearer()), ("/api/events", self.BATCH, {})):
            self.assertNotIn(SECRET.encode(), self.post(path, payload, headers)[1])


if __name__ == "__main__":
    unittest.main()
