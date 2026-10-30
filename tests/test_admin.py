"""The owner's dashboard (/admin): the numbers, who may see them, the failure and limit events, and the page in headless Chrome. Fake Supabase, no network."""
import contextlib
import http.client
import io
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bedahreview import web
from bedahreview.admin_stats import CACHE_SECS, PAGE_ROWS, AdminStats, StatsError, parse_ts
from bedahreview.auth import SupabaseAuth
from bedahreview.event_store import EventSink
from bedahreview.events import EVENTS, SERVER_EVENTS, clean_batch, clean_event, server_event

try:
    from test_auth import URL, USER, FakeSupabase, new_key, token
    from test_browser import CHROME, _read_probe
except ImportError:  # run as tests.<module>
    from tests.test_auth import URL, USER, FakeSupabase, new_key, token
    from tests.test_browser import CHROME, _read_probe

SECRET = "sb_secret_TESTKEY0123456789"
ADMIN = USER  # the token helper signs for USER, so the owner is USER here
A = "aaaaaaaa-1111-4222-8333-444455556666"
B = "bbbbbbbb-1111-4222-8333-444455556666"
C = "cccccccc-1111-4222-8333-444455556666"
NOW = datetime(2026, 10, 30, 5, 0, tzinfo=timezone.utc).timestamp()  # 12:00 in Jakarta


def ev(ts, user, name, **props):
    return {"ts": ts, "user_id": user, "event": name, "props": props}


USERS = [
    {"id": ADMIN, "email": "owner@example.com", "created_at": "2026-09-01T00:00:00Z", "app_metadata": {"provider": "google"}},
    {"id": A, "email": "ani@example.com", "created_at": "2026-10-20T02:00:00.123456Z", "last_sign_in_at": "2026-10-28T02:00:00Z", "app_metadata": {"provider": "google"}},
    {"id": B, "email": "budi@example.com", "created_at": "2026-09-01T00:00:00Z", "app_metadata": {"provider": "email"}},
    {"id": C, "email": "citra@example.com", "created_at": "2026-10-29T03:00:00Z", "app_metadata": {"provider": "google"}},
]
EVENT_ROWS = [
    ev("2026-09-15T03:00:00+00:00", B, "opened"),  # before the 30 days: not counted
    ev("2026-10-20T03:00:00.5+00:00", A, "opened"),
    ev("2026-10-20T03:01:00+00:00", A, "analysed", reviews=10, source="paste", lang="id"),
    ev("2026-10-20T03:02:00+00:00", A, "tab_viewed", tab="fix"),
    ev("2026-10-20T03:03:00+00:00", A, "draft_copied", kind="chat"),
    ev("2026-10-24T18:30:00+00:00", B, "opened"),  # 01:30 on the 25th in Jakarta
    ev("2026-10-25T02:00:00+00:00", B, "analysed", reviews=5, source="paste", lang="id"),
    ev("2026-10-25T02:05:00+00:00", B, "fix_marked"),
    ev("2026-10-28T04:00:00+00:00", A, "opened"),
    ev("2026-10-28T04:01:00+00:00", A, "analysed", reviews=20, source="link", lang="en"),
    ev("2026-10-28T04:02:00+00:00", A, "feedback", answer="yes", comment="mantap"),
    ev("2026-10-29T04:00:00+00:00", A, "analysis_failed", step="analyse", code="model_quota"),
    ev("2026-10-29T04:01:00+00:00", A, "limit_hit", step="analyse", limit="daily"),
    ev("2026-10-30T01:00:00+00:00", ADMIN, "opened"),  # the owner testing: left out
    ev("2026-10-30T01:01:00+00:00", ADMIN, "analysed", reviews=3, source="paste", lang="id"),
]
PROFILES = [{"user_id": A, "platform": "tokopedia", "category": "Fashion", "lang": "id"}, {"user_id": B, "platform": None, "category": None, "lang": "id"},
            {"user_id": ADMIN, "platform": "lazada", "category": "beauty", "lang": "en"}]
FIXES = [{"user_id": B, "fixed_on": "2026-10-25", "after_on": "2026-10-28"}, {"user_id": ADMIN, "fixed_on": "2026-10-30", "after_on": None}]


class FakeRest:
    """Stands in for Supabase's admin and table endpoints. Pages the rows like Supabase does and records each address asked for."""

    def __init__(self, users=USERS, events=EVENT_ROWS, profiles=PROFILES, fixes=FIXES):
        self.tables = {"events": events, "profiles": profiles, "fixes": fixes}
        self.users, self.calls, self.down = users, [], None

    def __call__(self, url, headers):
        assert url.startswith(URL)
        assert headers["apikey"] == SECRET
        self.calls.append(url.replace(URL, ""))
        if self.down:
            raise self.down
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        if parts.path == "/auth/v1/admin/users":
            page, per = int(query["page"][0]), int(query["per_page"][0])
            return {"users": self.users[(page - 1) * per: page * per], "aud": "authenticated"}
        table = parts.path.rsplit("/", 1)[-1]
        limit, offset = int(query["limit"][0]), int(query["offset"][0])
        return self.tables[table][offset:offset + limit]


def stats(fake=None, clock=lambda: NOW):
    return AdminStats(URL, SECRET, exclude=frozenset({ADMIN}), get=fake or FakeRest(), clock=clock)


class NumbersTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRest()
        self.s = stats(self.fake).summary(30)

    def test_accounts_and_activity(self):
        s = self.s
        self.assertEqual((s["from"], s["to"], s["days"]), ("2026-10-01", "2026-10-30", 30))
        self.assertEqual(s["accounts"], {"total": 3, "new": 2, "by_method": {"google": 2, "email": 1}, "ever_analysed_in_window": 2})
        self.assertEqual(s["active"], {"window": 2, "week": 2, "today": 0, "returned": 1, "analysed_on_two_days": 1})  # a failure is not a visit, and the owner is left out
        self.assertEqual(s["excluded"], 1)

    def test_days_use_jakarta_time(self):
        daily = {d["day"]: d for d in self.s["daily"]}
        self.assertEqual(len(daily), 30)
        self.assertEqual(daily["2026-10-25"]["people"], 1)  # 18:30 UTC on the 24th is the 25th in Jakarta
        self.assertEqual(daily["2026-10-24"]["people"], 0)
        self.assertEqual((daily["2026-10-29"]["signups"], daily["2026-10-29"]["failed"]), (1, 1))
        self.assertEqual(daily["2026-10-30"]["analyses"], 0)

    def test_analyses_funnel_features_and_health(self):
        s = self.s
        self.assertEqual(s["analyses"], {"total": 3, "by_source": {"paste": 2, "link": 1}, "by_lang": {"id": 2, "en": 1}, "avg_reviews": 11.7, "per_person": 1.5})
        self.assertEqual([f["people"] for f in s["funnel"]], [2, 2, 1, 1, 1])
        self.assertEqual(s["features"], {"tabs": {"fix": 1}, "drafts": {"chat": 1}, "fix_marked": 1, "rivals_started": 0})
        self.assertEqual(s["health"], {"failed": 1, "failed_by_code": {"model_quota": 1}, "failed_by_step": {"analyse": 1}, "limits": 1, "limits_by_kind": {"daily": 1}, "people_hit_limit": 1})
        self.assertEqual(s["fixes"], {"total": 1, "accounts": 1, "rechecked": 1})
        self.assertEqual(s["feedback"], {"answers": {"yes": 1}, "comments": [{"day": "2026-10-28", "answer": "yes", "comment": "mantap"}]})

    def test_profiles_and_people(self):
        s = self.s
        self.assertEqual(s["profiles"]["intro_done"], 1)
        self.assertEqual(s["profiles"]["platform"], {"tokopedia": 1, "not given": 1})
        self.assertEqual(s["profiles"]["category"], {"fashion": 1, "not given": 1})
        self.assertEqual([(p["id"], p["days"], p["analyses"], p["failed"], p["platform"]) for p in s["people"]], [("aaaaaaaa", 2, 2, 1, "tokopedia"), ("bbbbbbbb", 1, 1, 0, None)])
        self.assertEqual((s["people"][0]["joined"], s["people"][0]["first"], s["people"][0]["last"]), ("2026-10-20", "2026-10-20", "2026-10-28"))

    def test_no_email_full_id_or_owner_data_ever_comes_out(self):
        out = json.dumps(self.s)
        for secret in ("@example.com", A, B, C, ADMIN, ADMIN[:8], "lazada", "beauty", SECRET):
            self.assertNotIn(secret, out)

    def test_only_harmless_columns_are_asked_for(self):
        asked = " ".join(self.fake.calls)
        self.assertIn("/rest/v1/profiles?select=user_id,platform,category,lang&", asked)  # not the shop link
        self.assertIn("/rest/v1/fixes?select=user_id,fixed_on,after_on&", asked)  # not the product or the note
        self.assertIn("/rest/v1/events?select=ts,user_id,event,props&ts=gte.2026-09-30T17%3A00%3A00Z", asked)  # midnight on the 1st in Jakarta
        for column in ("shop_url", "product", "note", "email"):
            self.assertNotIn(column, asked)

    def test_a_period_outside_the_choices_falls_back_to_30_days(self):
        self.assertEqual(stats().summary(7)["from"], "2026-10-24")
        self.assertEqual(stats().summary(365)["days"], 30)


class ReadingTests(unittest.TestCase):
    def test_long_lists_are_read_page_by_page_and_a_cut_is_reported(self):
        many = [ev("2026-10-29T04:00:00+00:00", A, "opened") for _ in range(PAGE_ROWS * 2 + 5)]
        users = [dict(USERS[1], id="%08d-1111-4222-8333-444455556666" % i) for i in range(PAGE_ROWS + 3)]
        fake = FakeRest(users=users, events=many)
        s = stats(fake).summary(30)
        self.assertEqual(s["accounts"]["total"], PAGE_ROWS + 3)
        self.assertEqual(sum(1 for c in fake.calls if c.startswith("/rest/v1/events")), 3)
        self.assertFalse(s["cut"])
        with unittest.mock.patch("bedahreview.admin_stats.MAX_EVENT_ROWS", PAGE_ROWS):
            self.assertTrue(stats(FakeRest(events=many)).summary(30)["cut"])

    def test_results_are_kept_for_a_minute(self):
        fake, now = FakeRest(), [NOW]
        s = stats(fake, clock=lambda: now[0])
        s.summary(30)
        asked = len(fake.calls)
        s.summary(30)
        self.assertEqual(len(fake.calls), asked)
        now[0] += CACHE_SECS + 1
        s.summary(30)
        self.assertEqual(len(fake.calls), asked * 2)

    def test_supabase_trouble_is_a_stats_error_that_names_no_key(self):
        for trouble in (OSError("down"), urllib.error.HTTPError(URL, 503, "x", {}, None), ValueError("bad json")):
            fake = FakeRest()
            fake.down = trouble
            with self.assertRaises(StatsError) as caught:
                stats(fake).summary(30)
            self.assertNotIn(SECRET, str(caught.exception))
        fake = FakeRest()
        fake.tables["events"] = {"message": "not a list"}
        with self.assertRaises(StatsError):
            stats(fake).summary(30)

    def test_supabase_times_are_read_in_every_shape_it_sends(self):
        for text in ("2026-10-27T09:15:02Z", "2026-10-27T09:15:02.12345+00:00", "2026-10-27 09:15:02.1+00", "2026-10-27T16:15:02+0700"):
            self.assertEqual(parse_ts(text).astimezone(timezone.utc).strftime("%H:%M:%S"), "09:15:02", text)
        for bad in ("", None, "yesterday", 12):
            self.assertIsNone(parse_ts(bad))


class ServerEventTests(unittest.TestCase):
    def test_the_page_can_never_send_the_server_events(self):
        for name in SERVER_EVENTS:
            with self.assertRaises(ValueError):
                clean_event({"event": name, "device": "3fa9c2d41b7e8a06c5d1e2f3", "props": {"step": "analyse", "code": "server"}})
            with self.assertRaises(ValueError):
                clean_batch({"device": "3fa9c2d41b7e8a06c5d1e2f3", "events": [{"event": name, "props": {}}]})
        self.assertFalse(set(SERVER_EVENTS) & set(EVENTS))

    def test_server_events_hold_only_fixed_choices_or_a_short_code(self):
        self.assertEqual(server_event("analysis_failed", "analyse", "model_quota"), {"event": "analysis_failed", "device": "server", "step": "analyse", "code": "model_quota"})
        self.assertEqual(server_event("analysis_failed", "x", "Gemini said: bagus banget")["code"], "other")  # never a message
        self.assertEqual(server_event("limit_hit", "compare", "weekly")["limit"], "hour")
        with self.assertRaises(ValueError):
            server_event("opened", "analyse", "x")


class ServerTests(unittest.TestCase):
    CODES = {"ani": "ani-secret-1"}

    def setUp(self):
        self.key, self.jwk = new_key("k1")
        self.fake = FakeRest()
        self.saved = []
        self.sink = EventSink(URL, SECRET, post=lambda rows: self.saved.extend(rows))
        self.fail_with = None

        def classify(reviews, product=None):
            if self.fail_with:
                raise self.fail_with
            return [{"id": r["id"], "bucket": "keep", "theme": "Praise: quality / material", "variant": None, "cause": "none", "check_first": False, "proof_quote": r["text"],
                     "likely_fake": {"flag": False, "signals": []}, "fix_suggestion_id": None, "fight": None, "confidence": "high"} for r in reviews]
        self.server = self.start(classify_fn=classify, admin_stats=stats(self.fake), admin_ids=frozenset({ADMIN}))
        self.port = self.server.server_address[1]

    def start(self, **kwargs):
        args = dict(access_codes=dict(self.CODES), public=True, event_sink=self.sink, supabase_auth=SupabaseAuth(URL, fetch=FakeSupabase(self.jwk)),
                    supabase_anon_key="sb_publishable_x", user_daily=1, max_per_hour=100)
        args.update(kwargs)
        server = web.make_server(0, **args)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def call(self, method, path, payload=None, headers=None, port=None):
        conn = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=10)
        body = json.dumps(payload).encode() if payload is not None else None
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            conn.request(method, path, body=body, headers=dict({"Content-Type": "application/json"} if body else {}, **(headers or {})))
            resp = conn.getresponse()
            data = resp.read()
        conn.close()
        return resp.status, data, out.getvalue(), err.getvalue()

    def bearer(self, sub=ADMIN):
        return {"Authorization": "Bearer " + token(self.key, sub=sub)}

    def cookie(self):
        return {"Cookie": web.COOKIE + "=" + web.session_token("ani", self.CODES["ani"], int(time.time()) + 100)}

    def test_the_owner_gets_the_numbers(self):
        status, body, _, _ = self.call("GET", "/api/admin/stats?days=7", headers=self.bearer())
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual((data["days"], data["accounts"]["total"]), (7, 3))
        self.assertNotIn(SECRET.encode(), body)
        self.assertNotIn(b"@example.com", body)
        self.assertEqual(json.loads(self.call("GET", "/api/admin/stats?days=x", headers=self.bearer())[1])["days"], 30)

    def test_everyone_else_gets_the_same_not_found_and_supabase_is_never_asked(self):
        refused = [self.call("GET", "/api/admin/stats", headers=h) for h in ({}, self.bearer(A), self.cookie(), {"Authorization": "Bearer junk"})]
        self.assertEqual({(r[0], r[1]) for r in refused}, {(404, b'{"error": "Not found"}')})
        self.assertEqual(self.fake.calls, [])

    def test_the_page_holds_no_numbers_and_is_only_there_when_the_dashboard_is_on(self):
        status, body, _, _ = self.call("GET", "/admin")
        self.assertEqual(status, 200)
        self.assertIn(b"/api/admin/stats", body)
        self.assertIn(b'name="robots" content="noindex', body)
        self.assertEqual(self.fake.calls, [])
        off = self.start(classify_fn=lambda reviews, product=None: [])  # no admin ids: no dashboard at all
        for path in ("/admin", "/api/admin/stats"):
            self.assertEqual(self.call("GET", path, headers=self.bearer(), port=off.server_address[1])[0], 404, path)
        no_accounts = web.make_server(0, admin_stats=stats(), admin_ids=frozenset({ADMIN}))  # local run without Supabase: off too
        self.assertIsNone(no_accounts.RequestHandlerClass.admin_stats)
        no_accounts.server_close()

    def test_when_supabase_cannot_be_read_the_owner_is_told(self):
        self.fake.down = OSError("paused")
        status, body, _, err = self.call("GET", "/api/admin/stats", headers=self.bearer())
        self.assertEqual((status, json.loads(body)["code"]), (502, "stats_failed"))
        self.assertIn("admin stats failed: OSError", err)
        self.assertNotIn(SECRET, err)

    def test_a_failed_analysis_is_saved_as_an_event_with_its_code_only(self):
        class Quota(Exception):
            code = 402
        self.fail_with = Quota("Gemini: prepayment credits are depleted for project 12345")
        status, body, printed, _ = self.call("POST", "/api/classify", {"text": "Bahannya tipis banget"}, self.bearer(A))
        self.assertEqual((status, json.loads(body)["code"]), (502, "model_quota"))
        self.sink.wait_idle()
        self.assertEqual(self.saved, [{"user_id": A, "event": "analysis_failed", "props": {"step": "analyse", "code": "model_quota"}}])
        line = json.loads(printed.strip().splitlines()[-1])
        self.assertEqual((line["event"], line["device"], line["code"]), ("analysis_failed", "server", "model_quota"))
        self.assertNotIn("tipis", printed)
        self.assertNotIn("depleted", printed)

    def test_a_daily_limit_hit_is_saved_and_a_working_analysis_is_not_an_event(self):
        self.assertEqual(self.call("POST", "/api/classify", {"text": "bagus"}, self.bearer(A))[0], 200)
        status, body, _, _ = self.call("POST", "/api/classify", {"text": "bagus"}, self.bearer(A))
        self.assertEqual((status, json.loads(body)["code"]), (429, "daily_limit"))
        self.sink.wait_idle()
        self.assertEqual(self.saved, [{"user_id": A, "event": "limit_hit", "props": {"step": "analyse", "limit": "daily"}}])

    def test_do_not_track_means_no_failure_event_either(self):
        self.fail_with = RuntimeError("down")
        for header in ({"DNT": "1"}, {"Sec-GPC": "1"}):
            status, _, printed, _ = self.call("POST", "/api/classify", {"text": "bagus"}, dict(self.bearer(A), **header))
            self.assertEqual(status, 502)
            self.assertNotIn("bedahreview_event", printed)
        self.sink.wait_idle()
        self.assertEqual(self.saved, [])

    def test_an_access_code_tester_failure_is_a_log_line_only(self):
        self.fail_with = RuntimeError("down")
        status, _, printed, _ = self.call("POST", "/api/classify", {"text": "bagus"}, self.cookie())
        self.assertEqual(status, 502)
        self.assertIn('"event":"analysis_failed"', printed)
        self.sink.wait_idle()
        self.assertEqual(self.saved, [])

    def test_refusals_that_are_not_failures_are_not_events(self):
        status, _, printed, _ = self.call("POST", "/api/classify", {"text": ""}, self.bearer(A))  # nothing pasted: the person's slip, not the app breaking
        self.assertEqual(status, 400)
        self.assertNotIn("bedahreview_event", printed)


ADMIN_FAKE = """
<script>
window.__session = location.hash === "#out" ? null : { access_token: location.hash === "#other" ? "__OTHER__" : "__TOKEN__" };
window.supabase = { createClient: () => ({ auth: { getSession: async () => ({ data: { session: window.__session } }) } }) };
</script>
"""

ADMIN_PROBE = """
<script>
window.__probe = (async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const until = async (fn, n = 80) => { for (let i = 0; i < n; i++) { if (fn()) return true; await wait(100); } return false; };
  const q = (id) => document.getElementById(id);
  await until(() => !q("state").hidden || !q("board").hidden);
  const out = { board: !q("board").hidden, state: q("state").hidden ? "" : q("state").textContent };
  if (out.board) {
    out.tiles = ["t-accounts", "t-active", "t-returned", "t-analyses"].map((id) => q(id).textContent);
    out.bars = document.querySelectorAll("#c-people .bar").length;
    out.failed = q("failed").textContent; out.limits = q("limits").textContent;
    out.people = [...document.querySelectorAll("#people tr")].slice(1).map((tr) => tr.cells[0].textContent);
    out.comment = q("comments").textContent; out.note = q("note").textContent;
    // headless Chrome will not make a window narrower than 500px, so the page is held to a phone's 360px to look for sideways scrolling
    document.documentElement.style.width = "360px"; window.dispatchEvent(new Event("resize")); await wait(400);
    out.overflow = [...document.querySelectorAll("main *")].filter((n) => !n.closest(".scroll") && n.getBoundingClientRect().right > 361).map((n) => n.tagName + "." + n.className).slice(0, 5);
    document.querySelector('.seg button[data-days="7"]').click();
    await until(() => q("note").textContent.startsWith("24 Oct"));
    out.week = q("note").textContent.slice(0, 16);
  }
  return out;
})();
</script>
"""


@unittest.skipUnless(CHROME, "Chrome is not installed")
class AdminPageBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        static = Path(cls.tmp) / "static"
        shutil.copytree(web.STATIC, static)
        key, jwk = new_key("k1")
        page = static / "admin.html"
        html = page.read_text(encoding="utf-8")
        start = '<script>\n"use strict";'
        assert html.count(start) == 1
        fake = ADMIN_FAKE.replace("__TOKEN__", token(key)).replace("__OTHER__", token(key, sub=A))
        page.write_text(html.replace(start, fake + start).replace("</body>", ADMIN_PROBE + "</body>"), encoding="utf-8")
        cls.real_static, web.STATIC = web.STATIC, static
        cls.server = web.make_server(0, public=True, supabase_auth=SupabaseAuth(URL, fetch=FakeSupabase(jwk)), supabase_anon_key="sb_publishable_x",
                                     admin_stats=stats(), admin_ids=frozenset({ADMIN}))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        web.STATIC = cls.real_static
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def probe(self, hash_, width=390):
        profile = tempfile.mkdtemp(dir=self.tmp)
        url = "http://127.0.0.1:%d/admin%s" % (self.port, hash_)
        chrome = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--remote-debugging-port=0", "--user-data-dir=" + profile,
                                   "--window-size=%d,900" % width, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                return _read_probe(profile, url)
        finally:
            chrome.terminate()
            chrome.wait(timeout=20)

    def test_the_owner_sees_the_board_on_a_phone_without_sideways_scrolling(self):
        out = self.probe("#admin")
        self.assertTrue(out["board"], out)
        self.assertEqual(out["tiles"], ["3", "2", "1", "3"])
        self.assertEqual(out["bars"], 3)  # three days with people in them (the 20th, 25th and 28th)
        self.assertIn("Gemini out of credit or quota", out["failed"])
        self.assertIn("Daily limit per account", out["limits"])
        self.assertEqual(out["people"], ["aaaaaaaa", "bbbbbbbb"])
        self.assertIn("mantap", out["comment"])
        self.assertIn("left out", out["note"])
        self.assertEqual(out["overflow"], [])  # only the wide People table may scroll sideways, inside its own box
        self.assertEqual(out["week"], "24 Oct – 30 Oct ")

    def test_another_account_or_nobody_sees_no_numbers(self):
        other = self.probe("#other")
        self.assertFalse(other["board"])
        self.assertIn("not an owner account", other["state"])
        out = self.probe("#out")
        self.assertFalse(out["board"])
        self.assertIn("Sign in on the app first", out["state"])


if __name__ == "__main__":
    unittest.main()
