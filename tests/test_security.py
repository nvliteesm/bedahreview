import http.client
import json
import re
import threading
import time
import unittest

from bedahreview.web import (COOKIE, SESSION_SECS, RateLimiter, check_session, csrf_ok, make_csrf, make_server, parse_access_codes, session_token)

CODES = {"ani": "ani-secret-1", "budi": "budi-secret-2"}


class AccessCodeTests(unittest.TestCase):
    def test_named_codes_are_read(self):
        self.assertEqual(parse_access_codes("ani:ani-secret-1, Budi:budi-secret-2 ,"), CODES)
        self.assertEqual(parse_access_codes(""), {})

    def test_bad_entries_are_refused_without_echoing_a_code(self):
        for bad in ("justacode123", "ani:short", "Bad Name:longenough1", ":longenough1", "a" * 21 + ":longenough1", "ani:longenough1,ani:another-code-2", "ani:longenough1,budi:longenough1"):
            with self.assertRaises(ValueError, msg=bad) as ctx:
                parse_access_codes(bad)
            self.assertNotIn("longenough1", str(ctx.exception))


class SessionTests(unittest.TestCase):
    def test_a_genuine_token_names_the_tester(self):
        now = time.time()
        self.assertEqual(check_session(session_token("ani", CODES["ani"], int(now) + 100), CODES, now), "ani")

    def test_bad_tokens_are_refused(self):
        now = time.time()
        good = session_token("ani", CODES["ani"], int(now) + 100)
        name, exp, sig = good.split(".")
        cases = {
            "expired": session_token("ani", CODES["ani"], int(now) - 1),
            "wrong code": session_token("ani", CODES["budi"], int(now) + 100),
            "tampered name": "budi." + exp + "." + sig,
            "tampered expiry": "%s.%d.%s" % (name, int(exp) + 5, sig),
            "no end date in sight": session_token("ani", CODES["ani"], int(now) + SESSION_SECS * 50),
            "unknown tester": session_token("zed", "whatever-code-9", int(now) + 100),
            "junk": "not a token", "empty": "", "odd characters": "ani.123.éé",
        }
        for why, token in cases.items():
            self.assertIsNone(check_session(token, CODES, now), why)

    def test_removing_a_code_ends_that_testers_sign_in_only(self):
        now = time.time()
        ani, budi = (session_token(n, CODES[n], int(now) + 100) for n in ("ani", "budi"))
        left = {"budi": CODES["budi"]}
        self.assertIsNone(check_session(ani, left, now))
        self.assertEqual(check_session(budi, left, now), "budi")
        self.assertIsNone(check_session(ani, {"ani": "a-brand-new-code-3", "budi": CODES["budi"]}, now))  # changing a code does the same


class CsrfTests(unittest.TestCase):
    def test_signed_double_submit(self):
        token = make_csrf(CODES)
        self.assertTrue(csrf_ok(CODES, token, token))
        self.assertFalse(csrf_ok(CODES, token, make_csrf(CODES)))  # cookie and field must match
        self.assertFalse(csrf_ok(CODES, "abc.def", "abc.def"))  # and must be a token we issued
        self.assertFalse(csrf_ok(CODES, "", ""))
        self.assertFalse(csrf_ok({"x": "other-codes-here"}, token, token))
        self.assertFalse(csrf_ok(CODES, "é.é", "é.é"))


class RateLimiterTests(unittest.TestCase):
    def test_old_visitors_are_forgotten(self):
        now = [1000.0]
        limiter = RateLimiter(3, 60, clock=lambda: now[0])
        for i in range(200):
            limiter.take("visitor-%d" % i)
        self.assertEqual(limiter.size(), 200)
        now[0] += 61
        limiter.take("newcomer")
        self.assertEqual(limiter.size(), 1)  # the 200 old keys are gone, only the newcomer is kept

    def test_limits_still_work_after_a_prune(self):
        now = [0.0]
        limiter = RateLimiter(2, 60, clock=lambda: now[0])
        self.assertEqual([limiter.take("a") for _ in range(3)], [True, True, False])
        now[0] += 61
        self.assertTrue(limiter.take("a"))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server(0, classify_fn=lambda reviews, product=None: [], access_codes=dict(CODES), public=True)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def form(self):
        resp, page = self.request("GET", "/")
        token = re.search(rb'name="csrf" value="([^"]+)"', page).group(1).decode()
        return resp.getheader("Set-Cookie").split(";")[0], token

    def login(self, code, csrf="ok", cookie_override=None):
        cookie, token = self.form()
        token = token if csrf == "ok" else csrf
        body = ("code=%s&next=/&csrf=%s" % (code, token)).encode()
        return self.request("POST", "/login", body, {"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie_override if cookie_override is not None else cookie})

    def session(self, code):
        resp, _ = self.login(code)
        self.assertEqual(resp.status, 303)
        return [h for h in resp.msg.get_all("Set-Cookie") if h.startswith(COOKIE + "=")][0].split(";")[0]

    def test_login_needs_the_anti_forgery_token(self):
        self.assertEqual(self.login(CODES["ani"], csrf="forged")[0].status, 400)
        self.assertEqual(self.login(CODES["ani"], cookie_override="")[0].status, 400)  # no cookie, no sign-in
        self.assertEqual(self.login(CODES["ani"], cookie_override="br_csrf=abc.def")[0].status, 400)
        resp, _ = self.login(CODES["ani"])
        self.assertEqual(resp.status, 303)

    def test_each_tester_has_their_own_code_and_a_cookie_that_ends(self):
        for name, code in CODES.items():
            cookie = self.session(code)
            self.assertEqual(self.request("GET", "/api/config", headers={"Cookie": cookie})[0].status, 200)
            self.assertTrue(cookie.split("=", 1)[1].startswith(name + "."))
        resp, _ = self.login(CODES["ani"])
        header = [h for h in resp.msg.get_all("Set-Cookie") if h.startswith(COOKIE + "=")][0]
        self.assertIn("Max-Age=%d" % SESSION_SECS, header)  # two weeks, not for ever
        self.assertTrue(SESSION_SECS <= 14 * 24 * 3600)

    def test_cutting_off_one_tester_leaves_the_other(self):
        ani, budi = self.session(CODES["ani"]), self.session(CODES["budi"])
        del self.server.RequestHandlerClass.access_codes["ani"]  # the owner removes ani's entry
        self.assertEqual(self.request("GET", "/api/config", headers={"Cookie": ani})[0].status, 401)
        self.assertEqual(self.request("GET", "/api/config", headers={"Cookie": budi})[0].status, 200)
        self.assertEqual(self.login(CODES["ani"])[0].status, 401)  # and the old code no longer signs anyone in

    def test_a_cookie_with_an_old_end_date_is_refused(self):
        cookie = COOKIE + "=" + session_token("ani", CODES["ani"], int(time.time()) - 5)
        self.assertEqual(self.request("GET", "/api/config", headers={"Cookie": cookie})[0].status, 401)

    def test_sign_out_clears_the_cookie_and_cannot_be_triggered_by_a_plain_form(self):
        cookie = self.session(CODES["ani"])
        resp, _ = self.request("POST", "/logout", b"x=1", {"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie})
        self.assertEqual(resp.status, 415)
        resp, body = self.request("POST", "/logout", b"{}", {"Content-Type": "application/json", "Cookie": cookie})
        self.assertEqual(resp.status, 200)
        cleared = resp.getheader("Set-Cookie")
        self.assertIn(COOKIE + "=;", cleared)
        self.assertIn("Max-Age=0", cleared)

    def test_config_says_whether_a_code_protects_the_page(self):
        cookie = self.session(CODES["budi"])
        self.assertTrue(json.loads(self.request("GET", "/api/config", headers={"Cookie": cookie})[1])["access"])
        local = make_server(0)
        threading.Thread(target=local.serve_forever, daemon=True).start()
        self.addCleanup(local.server_close)
        self.addCleanup(local.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", local.server_address[1], timeout=10)
        conn.request("GET", "/api/config")
        self.assertFalse(json.loads(conn.getresponse().read())["access"])
        conn.close()


if __name__ == "__main__":
    unittest.main()
