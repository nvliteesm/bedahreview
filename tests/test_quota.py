"""The per-account daily limit, and the rule that normal accounts never use the paid link box. Fake Supabase keys, no network."""
import http.client
import json
import threading
import time
import unittest
from datetime import datetime, timezone

from bedahreview import web
from bedahreview.auth import SupabaseAuth
from bedahreview.fetchers.base import Fetcher

try:
    from test_auth import URL, USER, FakeSupabase, new_key, token
except ImportError:  # run as tests.<module>
    from tests.test_auth import URL, USER, FakeSupabase, new_key, token

JAKARTA_MIDNIGHT = datetime(2026, 10, 29, 17, 0, tzinfo=timezone.utc).timestamp()  # 00:00 on 30 Oct in Jakarta (UTC+7)


class DailyQuotaTests(unittest.TestCase):
    def setUp(self):
        self.now = [JAKARTA_MIDNIGHT - 3600]  # 23:00 Jakarta, the evening of 29 Oct
        self.quota = web.DailyQuota(3, clock=lambda: self.now[0])

    def test_each_account_gets_its_own_turns_and_is_counted_up_to_the_limit(self):
        self.assertEqual([self.quota.take("ani") for _ in range(4)], [(True, 1), (True, 2), (True, 3), (False, 3)])
        self.assertEqual(self.quota.take("budi"), (True, 1))  # another account is not affected

    def test_the_day_ends_at_midnight_in_jakarta_not_utc(self):
        for _ in range(3):
            self.quota.take("ani")
        self.now[0] = JAKARTA_MIDNIGHT - 1
        self.assertFalse(self.quota.take("ani")[0])  # one second before midnight in Jakarta: still the same day
        self.now[0] = JAKARTA_MIDNIGHT
        self.assertEqual(self.quota.take("ani"), (True, 1))  # a new day: counts start again

    def test_a_refund_gives_the_turn_back_but_never_goes_below_zero(self):
        self.quota.take("ani")
        self.quota.refund("ani")
        self.quota.refund("ani")
        self.assertEqual(self.quota.take("ani"), (True, 1))

    def test_old_accounts_are_forgotten_on_a_new_day(self):
        for i in range(50):
            self.quota.take("user-%d" % i)
        self.now[0] = JAKARTA_MIDNIGHT + 10
        self.quota.take("newcomer")
        self.assertEqual(list(self.quota._counts), ["newcomer"])


class NoPaidFetch(Fetcher):
    """A link fetcher that must never be asked to fetch: it records the attempt so a test can see."""

    last_platform = "tokopedia"

    def __init__(self):
        self.fetched = []

    def check_link(self, url):
        return url

    def fetch_reviews(self, url, limit, mode="latest"):
        self.fetched.append(url)
        raise AssertionError("a paid fetch was attempted")

    fetch_fresh = fetch_reviews

    def search(self, *args, **kwargs):
        self.fetched.append("search")
        raise AssertionError("a paid search was attempted")


class ServerTests(unittest.TestCase):
    CODES = {"ani": "ani-secret-1"}

    def setUp(self):
        self.key, jwk = new_key("k1")
        self.model_calls, self.fail_next = [], [False]

        def classify(reviews, product=None):
            self.model_calls.append(len(reviews))
            if self.fail_next[0]:
                raise TimeoutError("model down")
            return [{"id": r["id"], "bucket": "keep", "theme": "Praise: quality / material", "variant": None, "cause": "none", "check_first": False,
                     "proof_quote": r["text"], "likely_fake": {"flag": False, "signals": []}, "fix_suggestion_id": None, "fight": None, "confidence": "high"} for r in reviews]

        self.fetcher = NoPaidFetch()
        self.server = web.make_server(0, classify_fn=classify, fetcher=self.fetcher, access_codes=dict(self.CODES), public=True,
                                      supabase_auth=SupabaseAuth(URL, fetch=FakeSupabase(jwk)), supabase_anon_key="sb_publishable_x", user_daily=2, max_per_hour=100)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, payload=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps(payload).encode() if payload is not None else None
        conn.request(method, path, body=body, headers=dict({"Content-Type": "application/json"} if body else {}, **(headers or {})))
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    def bearer(self, sub="5b1f2c3a-1111-4222-8333-444455556666"):
        return {"Authorization": "Bearer " + token(self.key, sub=sub)}

    def analyse(self, headers, text="bagus banget bahannya"):
        return self.call("POST", "/api/classify", {"text": text}, headers)

    def cookie(self):
        return {"Cookie": web.COOKIE + "=" + web.session_token("ani", self.CODES["ani"], int(time.time()) + 100)}

    def test_an_account_gets_its_daily_turns_then_a_clear_refusal(self):
        me = self.bearer()
        first, second = self.analyse(me), self.analyse(me)
        self.assertEqual((first[0], first[1]["quota"]), (200, {"used": 1, "max": 2}))
        self.assertEqual((second[0], second[1]["quota"]), (200, {"used": 2, "max": 2}))
        status, data = self.analyse(me)
        self.assertEqual(status, 429)
        self.assertEqual((data["code"], data["limit"], data["max"]), ("daily_limit", "daily", 2))
        self.assertEqual(len(self.model_calls), 2)  # the refused one never reached the model

    def test_another_account_is_not_affected(self):
        for _ in range(3):
            self.analyse(self.bearer())
        self.assertEqual(self.analyse(self.bearer(sub="9c9c9c9c-aaaa-4bbb-8ccc-dddd00001111"))[0], 200)

    def test_a_failed_analysis_does_not_use_up_a_turn(self):
        me = self.bearer()
        self.fail_next[0] = True
        self.assertEqual(self.analyse(me)[0], 502)
        self.fail_next[0] = False
        self.assertEqual(self.analyse(me)[1]["quota"]["used"], 1)  # the failed one was given back
        self.assertEqual(self.analyse(me)[0], 200)

    def test_a_paste_that_is_refused_before_the_model_is_not_counted(self):
        me = self.bearer()
        self.assertEqual(self.analyse(me, text="   ")[0], 400)
        self.assertEqual(self.analyse(me)[1]["quota"]["used"], 1)

    def test_access_code_testers_are_not_counted_by_the_account_limit(self):
        for _ in range(4):
            status, data = self.analyse(self.cookie())
            self.assertEqual(status, 200)
            self.assertNotIn("quota", data)

    def test_a_normal_account_can_never_start_a_paid_fetch(self):
        status, data = self.call("POST", "/api/classify", {"url": "https://www.tokopedia.com/toko/produk"}, self.bearer())
        self.assertEqual((status, data["code"]), (403, "paid_off"))
        self.assertEqual(self.call("POST", "/api/competitors/suggest", {"url": "https://www.tokopedia.com/toko/produk", "title": "Kaos"}, self.bearer())[0], 403)
        self.assertEqual(self.call("POST", "/api/competitors/compare", {"url": "", "title": "", "own": [], "competitors": [{"url": "https://www.tokopedia.com/a/b"}]}, self.bearer())[0], 403)
        self.assertEqual(self.fetcher.fetched, [])

    def test_the_link_box_is_only_offered_to_access_code_testers(self):
        self.assertFalse(self.call("GET", "/api/config")[1]["url_box"])  # signed out
        self.assertFalse(self.call("GET", "/api/config", headers=self.bearer())[1]["url_box"])  # a normal account
        self.assertTrue(self.call("GET", "/api/config", headers=self.cookie())[1]["url_box"])  # an access-code tester


class TrustedAccountTests(unittest.TestCase):
    """BEDAHREVIEW_PAID_USER_IDS: the owner's own account gets the link box and no daily limit; every other account stays as it was."""
    ME, OTHER = USER, "9c9c9c9c-aaaa-4bbb-8ccc-dddd00001111"

    def setUp(self):
        self.key, jwk = new_key("k1")
        self.fetcher, self.fail_with = NoPaidFetch(), [None]

        def classify(reviews, product=None):
            if self.fail_with[0]:
                raise self.fail_with[0]
            return [{"id": r["id"], "bucket": "keep", "theme": "Praise: quality / material", "variant": None, "cause": "none", "check_first": False, "proof_quote": r["text"],
                     "likely_fake": {"flag": False, "signals": []}, "fix_suggestion_id": None, "fight": None, "confidence": "high"} for r in reviews]
        self.server = web.make_server(0, classify_fn=classify, fetcher=self.fetcher, public=True, user_daily=2, max_per_hour=100, paid_users=web.parse_user_ids(self.ME.upper() + " "),
                                      supabase_auth=SupabaseAuth(URL, fetch=FakeSupabase(jwk)), supabase_anon_key="sb_publishable_x")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, payload=None, sub=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Authorization": "Bearer " + token(self.key, sub=sub)} if sub else {}
        conn.request(method, path, body=body, headers=dict({"Content-Type": "application/json"} if body else {}, **headers))
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    def test_the_list_is_read_strictly(self):
        self.assertEqual(web.parse_user_ids(""), frozenset())
        self.assertEqual(web.parse_user_ids(" %s , %s " % (self.ME.upper(), self.OTHER)), frozenset({self.ME, self.OTHER}))
        for bad in ("someone@example.com", "1234", self.ME + "x", "%s;%s" % (self.ME, self.OTHER)):
            with self.assertRaises(ValueError, msg=bad):
                web.parse_user_ids(bad)

    def test_a_trusted_account_sees_the_link_box_and_may_use_it(self):
        self.assertTrue(self.call("GET", "/api/config", sub=self.ME)[1]["url_box"])
        status, data = self.call("POST", "/api/classify", {"url": "https://www.tokopedia.com/toko/produk"}, sub=self.ME)
        self.assertNotEqual(data.get("code"), "paid_off")  # it got as far as the fetcher (which the test makes fail)
        self.assertEqual(self.fetcher.fetched, ["https://www.tokopedia.com/toko/produk"])

    def test_any_other_account_still_cannot(self):
        self.assertFalse(self.call("GET", "/api/config", sub=self.OTHER)[1]["url_box"])
        status, data = self.call("POST", "/api/classify", {"url": "https://www.tokopedia.com/toko/produk"}, sub=self.OTHER)
        self.assertEqual((status, data["code"]), (403, "paid_off"))
        self.assertEqual(self.fetcher.fetched, [])

    def test_a_trusted_account_is_not_counted_by_the_daily_limit(self):
        for _ in range(4):
            status, data = self.call("POST", "/api/classify", {"text": "bagus"}, sub=self.ME)
            self.assertEqual(status, 200)
            self.assertNotIn("quota", data)
        self.assertEqual([self.call("POST", "/api/classify", {"text": "bagus"}, sub=self.OTHER)[0] for _ in range(3)], [200, 200, 429])

    def test_no_credit_or_no_quota_is_named_as_such(self):
        class Depleted(Exception):
            code = 402
        for code in (402, 429):
            Depleted.code = code
            self.fail_with[0] = Depleted("prepayment credits are depleted")
            status, data = self.call("POST", "/api/classify", {"text": "bagus"}, sub=self.OTHER)
            self.assertEqual((status, data["code"]), (502, "model_quota"))
        self.fail_with[0] = TimeoutError("slow")  # anything else is still "try again in a moment"
        self.assertEqual(self.call("POST", "/api/classify", {"text": "bagus"}, sub=self.OTHER)[1]["code"], "model_down")
        self.fail_with[0] = None
        self.assertEqual(self.call("POST", "/api/classify", {"text": "bagus"}, sub=self.OTHER)[1]["quota"]["used"], 1)  # the failed ones were given back


class LimitOffTests(unittest.TestCase):
    def test_zero_turns_the_daily_limit_off(self):
        key, jwk = new_key("k1")
        server = web.make_server(0, classify_fn=lambda reviews, product=None: [], public=True, user_daily=0,
                                 supabase_auth=SupabaseAuth(URL, fetch=FakeSupabase(jwk)), supabase_anon_key="sb_publishable_x")
        self.addCleanup(server.server_close)
        self.assertIsNone(server.RequestHandlerClass.daily_quota)


if __name__ == "__main__":
    unittest.main()
