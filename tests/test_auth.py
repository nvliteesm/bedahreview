"""Supabase sign-in tokens, checked on our server with a fake key set. No network, no real Supabase."""
import http.client
import json
import re
import threading
import time
import unittest
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from bedahreview import web
from bedahreview.auth import KEYS_TTL_SECS, REFETCH_COOLDOWN_SECS, SupabaseAuth
from bedahreview.events import EVENTS, SERVER_EVENTS

ROOT = Path(__file__).resolve().parents[1]
URL = "https://abcdwxyz.supabase.co"
USER = "5b1f2c3a-1111-4222-8333-444455556666"


def new_key(kid):
    key = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid=kid, alg="ES256", use="sig")
    return key, jwk


def token(key, kid="k1", **overrides):
    now = int(time.time())
    claims = {"sub": USER, "aud": "authenticated", "iss": URL + "/auth/v1", "iat": now, "exp": now + 3600, "email": "someone@example.com"}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="ES256", headers={"kid": kid})


class FakeSupabase:
    """Stands in for the key endpoint: serves whatever key set it holds, counts the calls, and can go down."""

    def __init__(self, *jwks):
        self.keys, self.calls, self.down = list(jwks), 0, False

    def __call__(self, url):
        self.calls += 1
        assert url == URL + "/auth/v1/.well-known/jwks.json"
        if self.down:
            raise OSError("supabase is down")
        return {"keys": list(self.keys)}


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.key, jwk = new_key("k1")
        self.fake = FakeSupabase(jwk)
        self.now = [1000.0]
        self.auth = SupabaseAuth(URL, fetch=self.fake, clock=lambda: self.now[0])

    def test_a_genuine_token_gives_the_user_id_and_nothing_else(self):
        self.assertEqual(self.auth.verify(token(self.key)), USER)

    def test_bad_tokens_are_refused(self):
        other, _ = new_key("k1")
        cases = {
            "expired": token(self.key, exp=int(time.time()) - 60),
            "wrong audience": token(self.key, aud="anon"),
            "wrong issuer": token(self.key, iss="https://evil.supabase.co/auth/v1"),
            "no expiry": token(self.key, exp=None),
            "no subject": token(self.key, sub=None),
            "signed by someone else": token(other),
            "unknown key id": token(self.key, kid="nope"),
            "shared-secret token": jwt.encode({"sub": USER, "aud": "authenticated", "iss": URL + "/auth/v1", "exp": int(time.time()) + 60}, "x" * 40, algorithm="HS256", headers={"kid": "k1"}),
            "alg none": jwt.encode({"sub": USER, "aud": "authenticated", "iss": URL + "/auth/v1", "exp": int(time.time()) + 60}, None, algorithm="none", headers={"kid": "k1"}),
            "junk": "not.a.token", "empty": "", "huge": "a" * 5000,
        }
        for why, bad in cases.items():
            self.assertIsNone(self.auth.verify(bad), why)
        self.assertIsNone(self.auth.verify(None))

    def test_keys_are_fetched_once_not_per_request(self):
        for _ in range(20):
            self.assertEqual(self.auth.verify(token(self.key)), USER)
        self.assertEqual(self.fake.calls, 1)  # so Supabase's per-address limit is never in play
        self.now[0] += KEYS_TTL_SECS + 1
        self.auth.verify(token(self.key))
        self.assertEqual(self.fake.calls, 2)

    def test_signed_in_people_keep_working_when_supabase_goes_down(self):
        self.assertEqual(self.auth.verify(token(self.key)), USER)
        self.fake.down = True
        self.now[0] += KEYS_TTL_SECS + 1  # the keys are due for a refresh, and the refresh fails
        self.assertEqual(self.auth.verify(token(self.key)), USER)  # the keys we already hold are still used

    def test_a_server_that_never_reached_supabase_refuses_quietly(self):
        self.fake.down = True
        self.assertIsNone(self.auth.verify(token(self.key)))

    def test_a_rotated_key_is_picked_up_but_not_more_than_once_a_minute(self):
        key2, jwk2 = new_key("k2")
        self.auth.verify(token(self.key))
        self.fake.keys = [jwk2]
        self.now[0] += 5
        self.assertIsNone(self.auth.verify(token(key2, kid="k2")))  # too soon after the last fetch: no new call
        calls = self.fake.calls
        self.now[0] += REFETCH_COOLDOWN_SECS
        self.assertEqual(self.auth.verify(token(key2, kid="k2")), USER)
        self.assertEqual(self.fake.calls, calls + 1)

    def test_the_email_in_a_token_is_never_returned(self):
        self.assertNotIn("example.com", str(self.auth.verify(token(self.key))))


class WebTests(unittest.TestCase):
    CODES = {"ani": "ani-secret-1"}
    ANON = "sb_publishable_test_key_for_tests"

    def setUp(self):
        self.key, jwk = new_key("k1")
        self.server = web.make_server(0, classify_fn=lambda reviews, product=None: [], access_codes=dict(self.CODES), public=True,
                                      supabase_auth=SupabaseAuth(URL, fetch=FakeSupabase(jwk)), supabase_anon_key=self.ANON)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, path, headers=None, method="GET", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def get(self, path, headers=None):
        return self.call(path, headers)[0].status

    def test_a_gated_api_call_accepts_a_genuine_token_and_refuses_the_rest(self):
        # /api/ping does not exist: 404 means the sign-in check passed, 401 means it did not
        self.assertEqual(self.get("/api/ping", {"Authorization": "Bearer " + token(self.key)}), 404)
        self.assertEqual(self.get("/api/ping", {"Authorization": "bearer " + token(self.key)}), 404)
        for bad in ("Bearer " + token(self.key, exp=int(time.time()) - 60), "Bearer junk", "Bearer ", token(self.key), "Basic abc"):
            self.assertEqual(self.get("/api/ping", {"Authorization": bad}), 401, bad)
        self.assertEqual(self.get("/api/ping"), 401)
        self.assertEqual(self.call("/api/event", method="POST", body=b"{}", headers={"Content-Type": "application/json"})[0].status, 401)

    def test_the_access_code_cookie_still_works(self):
        cookie = web.COOKIE + "=" + web.session_token("ani", self.CODES["ani"], int(time.time()) + 100)
        self.assertEqual(self.get("/api/ping", {"Cookie": cookie}), 404)

    def test_the_page_and_its_files_load_without_signing_in(self):
        for path in ("/", "/vendor/supabase.js", "/manifest.webmanifest", "/sw.js"):
            self.assertEqual(self.get(path), 200, path)

    def test_config_is_public_and_says_whether_you_are_signed_in(self):
        resp, data = self.call("/api/config")
        cfg = json.loads(data)
        self.assertEqual((resp.status, cfg["signed_in"]), (200, False))
        self.assertEqual(cfg["supabase"], {"url": URL, "anon_key": self.ANON})
        signed = json.loads(self.call("/api/config", {"Authorization": "Bearer " + token(self.key)})[1])
        self.assertTrue(signed["signed_in"])
        cookie = web.COOKIE + "=" + web.session_token("ani", self.CODES["ani"], int(time.time()) + 100)
        self.assertTrue(json.loads(self.call("/api/config", {"Cookie": cookie})[1])["signed_in"])  # a code user is signed in too

    def test_only_the_publishable_key_ever_leaves_the_server(self):
        for path in ("/", "/api/config", "/api/example"):
            self.assertNotIn(b"sb_secret_", self.call(path)[1])
            self.assertNotIn(b"SERVICE_KEY", self.call(path)[1])

    def test_the_free_example_needs_no_sign_in_and_no_model(self):
        resp, data = self.call("/api/example")
        out = json.loads(data)
        self.assertEqual(resp.status, 200)
        self.assertTrue(out["example"])
        self.assertEqual(out["reviews"], len(out["items"]) + out["dropped"])
        self.assertTrue(out["groups"] and out["counts"]["fight"] >= 1)
        self.assertEqual(out["source"]["title"], "Kaos Boxy Retro")

    def test_the_example_copy_matches_the_recorded_sample(self):
        sample = json.loads((ROOT / "samples" / "live_gemini_sample.json").read_text(encoding="utf-8"))
        shipped = json.loads((web.EXAMPLE_PATH).read_text(encoding="utf-8"))
        self.assertEqual((shipped["input"], shipped["output"]), (sample["input"], sample["output"]))  # the copy inside src/ is what gets deployed

    def test_the_page_may_talk_to_supabase_and_nobody_else(self):
        csp = self.call("/")[0].getheader("Content-Security-Policy")
        self.assertIn("connect-src 'self' " + URL, csp)
        self.assertIn("default-src 'self'", csp)
        self.assertNotIn("cdn", csp)

    def test_the_access_code_page_is_still_reachable_for_interviewers(self):
        resp, page = self.call("/code")
        self.assertEqual(resp.status, 200)
        self.assertIn(b'name="csrf"', page)

    def test_the_pinned_supabase_js_is_the_file_we_reviewed(self):
        import hashlib
        data = (web.STATIC / "vendor" / "supabase.js").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), "d6a5c4414a5d4ce646d9c1de223aa7067d3ff664c15394ffeb7fcffc763354a3")  # @supabase/supabase-js 2.117.3, umd build


class SupabaseOnlyTests(unittest.TestCase):
    """Public mode with Supabase and no access codes: the page is open, the work is not."""

    def test_no_codes_means_no_code_page_and_a_sign_in_is_still_required(self):
        key, jwk = new_key("k1")
        server = web.make_server(0, classify_fn=lambda reviews, product=None: [], public=True, supabase_auth=SupabaseAuth(URL, fetch=FakeSupabase(jwk)), supabase_anon_key="sb_publishable_x")
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)

        def status(path, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("GET", path, headers=headers or {})
            code = conn.getresponse().status
            conn.close()
            return code
        self.assertEqual(status("/"), 200)
        self.assertEqual(status("/api/ping"), 401)
        self.assertEqual(status("/api/ping", {"Authorization": "Bearer " + token(key)}), 404)
        self.assertEqual(status("/code"), 401)  # nothing to type a code into


class SchemaTests(unittest.TestCase):
    SQL = (ROOT / "db" / "schema.sql").read_text()

    def test_the_event_names_match_events_py(self):
        listed = re.search(r"event\s+text not null check \(event in \(([^)]*)\)", self.SQL).group(1)
        self.assertEqual(set(re.findall(r"'([a-z_]+)'", listed)), set(EVENTS) | set(SERVER_EVENTS))
        # a project made with an older list gets the new one too: the constraint is replaced with the same names
        replaced = re.search(r"add constraint events_event_check check \(event in \(([^)]*)\)", self.SQL).group(1)
        self.assertEqual(set(re.findall(r"'([a-z_]+)'", replaced)), set(EVENTS) | set(SERVER_EVENTS))
        self.assertIn("drop constraint if exists events_event_check", self.SQL)

    def test_every_table_has_row_level_security_and_no_anon_access(self):
        tables = re.findall(r"create table if not exists public\.(\w+)", self.SQL)
        self.assertEqual(set(tables), {"profiles", "events", "fixes"})
        for table in tables:
            self.assertIn("alter table public.%s" % table, self.SQL.replace("  ", " "))
            self.assertRegex(self.SQL, r"revoke all on public\.%s\s+from anon" % table)
        self.assertNotRegex(self.SQL, r"to\s+anon")
        self.assertNotRegex(self.SQL, r"to\s+public\b")

    def test_nothing_that_identifies_a_review_or_a_person_has_a_column(self):
        def columns(table):
            body = re.search(r"create table if not exists public\.%s \((.*?)\n\);" % table, self.SQL, re.S).group(1)
            return re.findall(r"^\s+(\w+)\s+(?:uuid|text|timestamptz|bigint|jsonb|boolean|integer|date)\b", body, re.M)
        # no table may hold an email, an address, a reviewer or review text
        for table in ("profiles", "events", "fixes"):
            for forbidden in ("email", "name", "review", "text", "ip", "ip_address", "user_agent", "phone", "ok_to_contact", "reviewer"):
                self.assertNotIn(forbidden, columns(table), table)
        # product names, links and notes are stored in exactly one place, on purpose: the seller's own fixes list
        for table in ("profiles", "events"):
            for column in ("product", "product_label", "note", "shop_name"):
                self.assertNotIn(column, columns(table), table)
        self.assertTrue({"product", "product_label", "note"} <= set(columns("fixes")))

    def test_fixes_are_private_to_their_owner_and_capped(self):
        for action in ("select", "insert", "update", "delete"):
            self.assertRegex(self.SQL, r"create policy fixes_%s_own on public\.fixes for %s to authenticated" % (action, action))
        self.assertIn("< 250", self.SQL)  # a ceiling a little above the page's 200
        self.assertNotRegex(self.SQL, r"fixes[^;]*to\s+anon")

    def test_the_secret_key_is_only_ever_named_in_server_files(self):
        static = ROOT / "src" / "bedahreview" / "static"
        ours = [p for p in static.rglob("*") if p.is_file() and p.suffix in (".html", ".js", ".webmanifest") and "vendor" not in p.parts]  # the pinned library only mentions the prefix, it holds no key
        self.assertTrue(ours)
        for path in ours:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("SERVICE_KEY", text, path.name)
            self.assertNotIn("sb_secret_", text, path.name)
            self.assertNotRegex(text, r"sb_secret_[A-Za-z0-9_-]{10,}", path.name)


if __name__ == "__main__":
    unittest.main()
