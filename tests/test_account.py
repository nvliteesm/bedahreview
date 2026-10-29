"""Deleting an account ("Hapus akun dan data saya"), the privacy page, and what a deleted account can no longer do. Fake Supabase, no network."""
import contextlib
import http.client
import io
import json
import re
import threading
import time
import unittest
from pathlib import Path

from bedahreview import web
from bedahreview.account import TABLES, AccountAdmin, AccountError
from bedahreview.auth import SupabaseAuth
from bedahreview.event_store import EventSink

try:
    from test_auth import URL, USER, FakeSupabase, new_key, token
except ImportError:  # run as tests.<module>
    from tests.test_auth import URL, USER, FakeSupabase, new_key, token

ROOT = Path(__file__).resolve().parents[1]
SECRET = "sb_secret_TESTKEY0123456789"
OTHER = "9c9c9c9c-aaaa-4bbb-8ccc-dddd00001111"


class Recorder:
    """Stands in for Supabase's web side: records each call and answers with a status you choose."""

    def __init__(self):
        self.calls, self.status_for = [], {}

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url.replace(URL, ""), dict(headers), body))
        for needle, status in self.status_for.items():
            if needle in url:
                if isinstance(status, Exception):
                    raise status
                return status
        return 204

    def paths(self):
        return [(m, p) for m, p, _, _ in self.calls]


class AccountAdminTests(unittest.TestCase):
    def test_the_rows_go_first_then_the_account(self):
        rec = Recorder()
        AccountAdmin(URL, SECRET, request=rec).delete_account(USER)
        self.assertEqual(rec.paths(), [("DELETE", "/rest/v1/%s?user_id=eq.%s" % (t, USER)) for t in TABLES] + [("DELETE", "/auth/v1/admin/users/" + USER)])
        self.assertEqual(json.loads(rec.calls[-1][3]), {"should_soft_delete": False})  # really deleted, not just switched off

    def test_every_table_in_the_schema_that_holds_an_account_is_on_the_delete_list(self):
        sql = (ROOT / "db" / "schema.sql").read_text()
        with_owner = {m.group(1) for m in re.finditer(r"create table if not exists public\.(\w+) \((.*?)\n\);", sql, re.S) if "user_id" in m.group(2)}
        self.assertEqual(set(TABLES), with_owner)  # add a table with a user_id and this fails until the list knows about it

    def test_every_table_with_an_account_also_cascades_when_the_account_goes(self):
        sql = (ROOT / "db" / "schema.sql").read_text()
        for table, body in re.findall(r"create table if not exists public\.(\w+) \((.*?)\n\);", sql, re.S):
            if "user_id" in body:
                self.assertRegex(body, r"user_id\s+uuid[^\n]*references auth\.users \(id\) on delete cascade", table)

    def test_a_failure_in_the_rows_stops_everything_else(self):
        rec = Recorder()
        rec.status_for["/rest/v1/fixes"] = 500
        with self.assertRaises(AccountError) as ctx:
            AccountAdmin(URL, SECRET, request=rec).delete_account(USER)
        self.assertEqual(str(ctx.exception), "HTTP 500")
        self.assertFalse([p for _, p in rec.paths() if "/auth/v1/admin" in p])  # the account itself is untouched, so a retry starts clean

    def test_a_failure_deleting_the_account_is_reported_and_a_missing_account_is_fine(self):
        rec = Recorder()
        rec.status_for["/auth/v1/admin"] = 500
        with self.assertRaises(AccountError):
            AccountAdmin(URL, SECRET, request=rec).delete_account(USER)
        rec = Recorder()
        rec.status_for["/auth/v1/admin"] = 404  # already gone: the job is done
        AccountAdmin(URL, SECRET, request=rec).delete_account(USER)

    def test_an_unreachable_supabase_is_an_error_not_a_crash(self):
        rec = Recorder()
        rec.status_for["/rest/v1/events"] = TimeoutError("slow")
        with self.assertRaises(AccountError) as ctx:
            AccountAdmin(URL, SECRET, request=rec).delete_account(USER)
        self.assertEqual(str(ctx.exception), "TimeoutError")

    def test_only_a_plain_uuid_can_ever_reach_a_web_address(self):
        rec = Recorder()
        for bad in ("", "../../auth/v1/admin/users", USER + "?x=1", USER.upper(), "user-1111-2222", None, 5, USER + "\n"):
            with self.assertRaises(AccountError):
                AccountAdmin(URL, SECRET, request=rec).delete_account(bad)
        self.assertEqual(rec.calls, [])

    def test_the_key_goes_where_supabase_expects_it_and_nowhere_else(self):
        rec = Recorder()
        AccountAdmin(URL, SECRET, request=rec).delete_account(USER)
        for _, _, headers, _ in rec.calls:
            self.assertEqual(headers["apikey"], SECRET)
            self.assertNotIn("Authorization", headers)  # the newer secret key is not a token
        rec = Recorder()
        AccountAdmin(URL, "eyJ" + "a" * 30, request=rec).delete_account(USER)
        self.assertEqual(rec.calls[0][2]["Authorization"], "Bearer " + "eyJ" + "a" * 30)  # an older service_role key is


class RevokeTests(unittest.TestCase):
    def test_a_deleted_accounts_token_stops_working_but_others_do_not(self):
        key, jwk = new_key("k1")
        now = [1000.0]
        auth = SupabaseAuth(URL, fetch=FakeSupabase(jwk), clock=lambda: now[0])
        mine, theirs = token(key), token(key, sub=OTHER)
        self.assertEqual(auth.verify(mine), USER)
        auth.revoke(USER)
        self.assertIsNone(auth.verify(mine))
        self.assertEqual(auth.verify(theirs), OTHER)

    def test_the_block_outlasts_a_tokens_life_then_is_forgotten(self):
        key, jwk = new_key("k1")
        now = [1000.0]
        auth = SupabaseAuth(URL, fetch=FakeSupabase(jwk), clock=lambda: now[0])
        auth.revoke(USER)
        now[0] += 3500
        self.assertIsNone(auth.verify(token(key, exp=int(1000 + 4000))))  # a token that still has life in it is still refused
        now[0] += 4000
        auth.revoke(OTHER)  # the next revoke tidies the old entry away
        self.assertNotIn(USER, auth._revoked)


class SinkForgetsTests(unittest.TestCase):
    def test_rows_of_a_deleted_account_are_never_sent(self):
        posted, gate = [], threading.Event()
        sink = EventSink(URL, SECRET, post=lambda rows: (gate.wait(5), posted.extend(rows)))
        sink.submit([{"user_id": "first", "event": "opened", "props": {}}])  # keeps the writer busy
        sink.submit([{"user_id": USER, "event": "opened", "props": {}}, {"user_id": OTHER, "event": "opened", "props": {}}])
        sink.forget(USER)
        sink.submit([{"user_id": USER, "event": "tab_viewed", "props": {"tab": "fix"}}])  # arrives after: also dropped
        gate.set()
        sink.wait_idle()
        self.assertEqual(sorted(r["user_id"] for r in posted), sorted(["first", OTHER]))


class ServerTests(unittest.TestCase):
    CODES = {"ani": "ani-secret-1"}

    def setUp(self):
        self.key, jwk = new_key("k1")
        self.rec = Recorder()
        self.saved = []
        self.sink = EventSink(URL, SECRET, post=lambda rows: self.saved.extend(rows))
        self.model_calls = []

        def classify(reviews, product=None):
            self.model_calls.append(len(reviews))
            return [{"id": r["id"], "bucket": "keep", "theme": "Praise: quality / material", "variant": None, "cause": "none", "check_first": False, "proof_quote": r["text"],
                     "likely_fake": {"flag": False, "signals": []}, "fix_suggestion_id": None, "fight": None, "confidence": "high"} for r in reviews]
        self.server = web.make_server(0, classify_fn=classify, access_codes=dict(self.CODES), public=True, event_sink=self.sink, account_admin=AccountAdmin(URL, SECRET, request=self.rec),
                                      contact_email="hello@example.com", supabase_auth=SupabaseAuth(URL, fetch=FakeSupabase(jwk)), supabase_anon_key="sb_publishable_x", user_daily=5, max_per_hour=100)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, payload=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps(payload).encode() if payload is not None else None
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            conn.request(method, path, body=body, headers=dict({"Content-Type": "application/json"} if body else {}, **(headers or {})))
            resp = conn.getresponse()
            data = resp.read()
        conn.close()
        return resp.status, data, out.getvalue(), err.getvalue()

    def bearer(self, sub=USER):
        return {"Authorization": "Bearer " + token(self.key, sub=sub)}

    def cookie(self):
        return {"Cookie": web.COOKIE + "=" + web.session_token("ani", self.CODES["ani"], int(time.time()) + 100)}

    def delete(self, headers=None, payload=None):
        return self.call("POST", "/api/account/delete", {"confirm": "delete"} if payload is None else payload, headers)

    def test_deleting_removes_the_rows_then_the_account_and_the_token_dies(self):
        mine = self.bearer()
        self.assertEqual(self.call("POST", "/api/classify", {"text": "bagus"}, mine)[0], 200)  # signed in and working
        status, body, printed, _ = self.delete(mine)
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        self.assertEqual(self.rec.paths()[-1], ("DELETE", "/auth/v1/admin/users/" + USER))
        self.assertEqual(len(self.rec.paths()), len(TABLES) + 1)
        self.assertEqual(self.call("POST", "/api/classify", {"text": "bagus"}, mine)[0], 401)  # the old token is refused at once, not an hour later
        self.assertEqual(self.call("GET", "/api/ping", headers=mine)[0], 401)
        self.assertEqual(self.call("GET", "/api/ping", headers=self.bearer(OTHER))[0], 404)  # somebody else is not affected (404 = signed in, no such page)
        self.assertNotIn(USER, printed)  # the log line is a count, never an id
        self.assertEqual([json.loads(l)["message"] for l in printed.splitlines()], ["bedahreview_account_deleted"])

    def test_the_accounts_counts_and_queued_events_are_forgotten_too(self):
        mine = self.bearer()
        self.call("POST", "/api/classify", {"text": "bagus"}, mine)
        self.assertIn(USER, self.server.RequestHandlerClass.daily_quota._counts)
        self.delete(mine)
        self.assertNotIn(USER, self.server.RequestHandlerClass.daily_quota._counts)
        self.sink.submit([{"user_id": USER, "event": "opened", "props": {}}])
        self.sink.wait_idle()
        self.assertEqual(self.saved, [])

    def test_it_needs_a_signed_in_account_and_an_explicit_confirmation(self):
        self.assertEqual(self.delete()[0], 401)  # nobody signed in
        status, body, _, _ = self.delete(self.cookie())  # an access-code tester has no account
        self.assertEqual((status, json.loads(body)["code"]), (400, "no_account"))
        for payload in ({}, {"confirm": "yes"}, {"confirm": "delete", "x": 1}, [], "delete"):
            self.assertEqual(self.delete(self.bearer(), payload)[0], 400, payload)
        self.assertEqual(self.rec.calls, [])  # none of those touched Supabase

    def test_when_supabase_fails_the_person_is_told_and_nothing_is_revoked(self):
        mine = self.bearer()
        self.rec.status_for["/rest/v1/fixes"] = 500
        status, body, _, err = self.delete(mine)
        self.assertEqual((status, json.loads(body)["code"]), (502, "delete_failed"))
        self.assertNotIn(USER, err)
        self.assertEqual(self.call("GET", "/api/ping", headers=mine)[0], 404)  # still signed in: they can try again
        self.rec.status_for.clear()
        self.assertEqual(self.delete(mine)[0], 200)

    def test_a_server_without_the_secret_key_says_so(self):
        self.server.RequestHandlerClass.account_admin = None
        status, body, _, _ = self.delete(self.bearer())
        self.assertEqual((status, json.loads(body)["code"]), (503, "delete_unavailable"))

    def test_the_secret_key_and_the_account_id_never_appear_in_a_reply(self):
        for headers in (self.bearer(), self.cookie(), {}):
            reply = self.delete(headers)
            self.assertNotIn(SECRET.encode(), reply[1])
            self.assertNotIn(USER.encode(), reply[1])

    def test_config_says_whether_deleting_is_possible_and_gives_the_contact(self):
        cfg = json.loads(self.call("GET", "/api/config")[1])
        self.assertEqual((cfg["can_delete"], cfg["contact"]), (True, "hello@example.com"))
        self.assertNotIn(SECRET, json.dumps(cfg))


class PrivacyPageTests(unittest.TestCase):
    def get(self, server, path):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        conn.close()
        return resp.status, data

    def start(self, **kwargs):
        server = web.make_server(0, classify_fn=lambda reviews, product=None: [], public=True, **kwargs)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_the_page_is_public_with_sign_in_by_accounts_and_by_codes(self):
        key, jwk = new_key("k1")
        accounts = self.start(supabase_auth=SupabaseAuth(URL, fetch=FakeSupabase(jwk)), supabase_anon_key="sb_publishable_x")
        codes = self.start(access_codes={"ani": "ani-secret-1"})
        for server in (accounts, codes):
            status, page = self.get(server, "/privacy")
            self.assertEqual(status, 200)
            self.assertIn("Kebijakan privasi", page)

    def test_it_says_the_things_a_person_needs_to_know_in_both_languages(self):
        _, page = self.get(self.start(access_codes={"ani": "ani-secret-1"}), "/privacy")
        for phrase in ("Hapus akun dan data saya", "nama atau tautan produk", "catatan singkatmu", "Gemini API", "Supabase", "Google Cloud Run", "Apify", "alamat IP", "90 hari", "Do Not Track", "cadangan",
                       "Delete my account and data", "product name or link", "short note", "Google Cloud keeps its own standard request logs", "90 days", "backups"):
            self.assertIn(phrase, page, phrase)

    def test_it_loads_nothing_from_anywhere_else(self):
        _, page = self.get(self.start(access_codes={"ani": "ani-secret-1"}), "/privacy")
        self.assertNotRegex(page, r"<script[^>]+src=")
        self.assertNotRegex(page, r"https?://")
        self.assertNotIn("<link rel=\"stylesheet\"", page)

    def test_the_app_links_to_it_from_the_sign_in_and_the_footer(self):
        html = (web.STATIC / "index.html").read_text(encoding="utf-8")
        self.assertGreaterEqual(html.count('href="/privacy"'), 2)
        self.assertIn("Kami mencatat pemakaian per akun (tanpa isi ulasan) untuk riset produk ini.", html)


if __name__ == "__main__":
    unittest.main()
