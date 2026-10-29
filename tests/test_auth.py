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
from bedahreview.events import EVENTS

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

    def setUp(self):
        self.key, jwk = new_key("k1")
        self.server = web.make_server(0, classify_fn=lambda reviews, product=None: [], access_codes=dict(self.CODES), public=True, supabase_auth=SupabaseAuth(URL, fetch=FakeSupabase(jwk)))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def test_the_api_accepts_a_genuine_token_and_refuses_the_rest(self):
        self.assertEqual(self.get("/api/config", {"Authorization": "Bearer " + token(self.key)}), 200)
        self.assertEqual(self.get("/api/config", {"Authorization": "bearer " + token(self.key)}), 200)
        for bad in ("Bearer " + token(self.key, exp=int(time.time()) - 60), "Bearer junk", "Bearer ", token(self.key), "Basic abc"):
            self.assertEqual(self.get("/api/config", {"Authorization": bad}), 401, bad)
        self.assertEqual(self.get("/api/config"), 401)

    def test_a_token_does_not_replace_the_cookie_for_the_page_yet(self):
        self.assertEqual(self.get("/", {"Authorization": "Bearer " + token(self.key)}), 200)  # the browser never sends it on a page load; here it is accepted like any API call
        self.assertEqual(self.get("/"), 401)  # no sign-in: the access-code page, as before

    def test_the_access_code_cookie_still_works(self):
        cookie = web.COOKIE + "=" + web.session_token("ani", self.CODES["ani"], int(time.time()) + 100)
        self.assertEqual(self.get("/api/config", {"Cookie": cookie}), 200)


class LocalRunTests(unittest.TestCase):
    def test_a_local_run_without_codes_stays_open_even_with_supabase_set(self):
        key, jwk = new_key("k1")
        server = web.make_server(0, classify_fn=lambda reviews, product=None: [], supabase_auth=SupabaseAuth(URL, fetch=FakeSupabase(jwk)))
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.request("GET", "/api/config")
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()


class SchemaTests(unittest.TestCase):
    SQL = (ROOT / "db" / "schema.sql").read_text()

    def test_the_event_names_match_events_py(self):
        listed = re.search(r"event\s+text not null check \(event in \(([^)]*)\)", self.SQL).group(1)
        self.assertEqual(set(re.findall(r"'([a-z_]+)'", listed)), set(EVENTS))

    def test_every_table_has_row_level_security_and_no_anon_access(self):
        tables = re.findall(r"create table if not exists public\.(\w+)", self.SQL)
        self.assertEqual(set(tables), {"profiles", "events"})
        for table in tables:
            self.assertIn("alter table public.%s" % table, self.SQL.replace("  ", " "))
            self.assertRegex(self.SQL, r"revoke all on public\.%s\s+from anon" % table)
        self.assertNotRegex(self.SQL, r"to\s+anon")
        self.assertNotRegex(self.SQL, r"to\s+public\b")

    def test_nothing_that_identifies_a_review_or_a_person_has_a_column(self):
        columns = re.findall(r"^\s+(\w+)\s+(?:uuid|text|timestamptz|bigint|jsonb|boolean)\b", self.SQL, re.M)
        for forbidden in ("email", "name", "review", "text", "product", "ip", "ip_address", "user_agent", "phone", "ok_to_contact"):
            self.assertNotIn(forbidden, columns)

    def test_the_secret_key_is_only_ever_named_in_server_files(self):
        for path in (ROOT / "src" / "bedahreview" / "static").rglob("*"):
            if path.is_file() and path.suffix in (".html", ".js", ".webmanifest"):
                self.assertNotIn("SERVICE_KEY", path.read_text(), path.name)
                self.assertNotIn("sb_secret_", path.read_text(), path.name)


if __name__ == "__main__":
    unittest.main()
