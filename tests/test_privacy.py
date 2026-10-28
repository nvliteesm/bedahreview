import contextlib
import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from bedahreview.fetchers import CachedFetcher, FetcherConfigError, get_fetcher
from bedahreview.fetchers.base import Review
from bedahreview.fetchers.platforms import LinkRouter
from bedahreview.web import make_server

from test_platforms import TOKO, TOKO_ROWS, FakeActors, _no_resolve

URL = "https://shopee.co.id/Kaos-Boxy-i.1.2"
HOUR = 3600


class Clock:
    def __init__(self, now=1_760_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def reviews(n=3):
    return [Review(star=2, text="r%d" % i, variant=None, date="2026-10-01", photo_count=0, replied=False) for i in range(n)]


class Inner:
    def __init__(self):
        self.calls = 0

    def fetch_reviews(self, url, limit, mode="latest"):
        self.calls += 1
        return reviews(limit)


class CacheExpiryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()

    def fetcher(self, inner, **kw):
        return CachedFetcher(inner, self.dir, check_url=lambda u: u, clock=self.clock, **kw)

    def test_a_copy_is_used_for_a_day_and_then_fetched_again(self):
        inner, f = Inner(), None
        f = self.fetcher(inner)
        f.fetch_reviews(URL, 3)
        self.clock.now += 23 * HOUR
        f.fetch_reviews(URL, 3)
        self.assertEqual((inner.calls, f.last_from_cache), (1, True))
        self.clock.now += 2 * HOUR  # 25 hours after it was saved
        f.fetch_reviews(URL, 3)
        self.assertEqual((inner.calls, f.last_from_cache), (2, False))
        saved = json.loads(next(self.dir.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(saved["saved"], int(self.clock.now))  # the new copy has a new time

    def test_an_expired_copy_is_deleted_when_it_is_met(self):
        inner, f = Inner(), None
        f = self.fetcher(inner)
        f.fetch_reviews(URL, 3)
        path = next(self.dir.glob("*.json"))
        self.clock.now += 30 * HOUR
        self.assertEqual(f._read(URL, 3, "latest"), [])
        self.assertFalse(path.exists())

    def test_start_up_deletes_old_copies_and_copies_that_kept_seller_replies(self):
        def write(name, **extra):
            data = {"url": URL, "mode": "latest", "requested": 1, "reviews": [{"star": 1, "text": "x", "variant": None, "date": "", "photo_count": 0, "replied": False, "product": None}]}
            data.update(extra)
            (self.dir / name).write_text(json.dumps(data), encoding="utf-8")

        now = int(self.clock.now)
        write("fresh.json", saved=now - HOUR)
        write("old.json", saved=now - 40 * HOUR)
        write("nosavetime.json")  # a copy from before copies expired
        write("future.json", saved=now + 5 * HOUR)
        (self.dir / "legacy.json").write_text(json.dumps({"saved": now, "requested": 1, "reviews": [{"star": 1, "text": "x", "seller_reply": "Terima kasih Kak Siti"}]}), encoding="utf-8")
        (self.dir / "broken.json").write_text("{not json", encoding="utf-8")
        self.fetcher(Inner())
        self.assertEqual(sorted(p.name for p in self.dir.glob("*.json")), ["fresh.json"])

    def test_zero_hours_means_nothing_is_kept(self):
        inner = Inner()
        f = self.fetcher(inner, ttl_hours=0)
        f.fetch_reviews(URL, 3)
        f.fetch_reviews(URL, 3)
        self.assertEqual(inner.calls, 2)
        self.assertEqual(list(self.dir.glob("*.json")), [])

    def test_the_setting_is_checked(self):
        def env(value):
            return mock.patch.dict(os.environ, {"FETCHER": "apify", "APIFY_TOKEN": "t", "FETCH_CACHE_HOURS": value}, clear=False)

        for bad in ("abc", "-1", "169", "24000"):
            with env(bad):
                with self.assertRaises(FetcherConfigError, msg=bad):
                    get_fetcher()
        for good in ("0", "1", "24", "168", "2.5"):
            with env(good):
                self.assertIsNotNone(get_fetcher())


class NoReplyTextTests(unittest.TestCase):
    def test_a_sellers_reply_is_looked_at_and_thrown_away(self):
        with tempfile.TemporaryDirectory() as tmp:
            router = LinkRouter("t", 0.05, Path(tmp), client=FakeActors(TOKO_ROWS), resolver=_no_resolve)
            out = router.fetch_reviews(TOKO, 5, "complaints")
            self.assertTrue(out[0].replied)  # the shop did answer this one
            blob = repr(out) + "".join(p.read_text(encoding="utf-8") for p in Path(tmp).glob("*.json"))
            self.assertNotIn("maaf kak", blob)  # but the words of the answer are nowhere: not in memory objects, not on disk
            self.assertIn("wrna ny kurang sesuai", blob)  # the review text is still there for the analysis


class LogTests(unittest.TestCase):
    def test_nothing_a_visitor_sent_reaches_the_logs(self):
        def broken(reviews, product=None):
            raise RuntimeError("could not read: " + reviews[0]["text"])  # an error message that echoes the review

        server = make_server(0, classify_fn=broken)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("POST", "/api/classify", json.dumps({"text": "rahasia pembeli Siti", "product": "Kaos Rahasia"}).encode(), {"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = resp.read().decode()
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("GET", "/?text=rahasia+pembeli+Siti")
            conn.getresponse().read()
            conn.close()
        self.assertEqual(resp.status, 502)
        for place, text in (("response", body), ("stdout", out.getvalue()), ("stderr", err.getvalue())):
            self.assertNotIn("rahasia", text, place)
            self.assertNotIn("Siti", text, place)
        self.assertIn("RuntimeError", err.getvalue())  # the error type is still logged, which is what helps


if __name__ == "__main__":
    unittest.main()
