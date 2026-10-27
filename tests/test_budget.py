import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from bedahreview.fetchers import BudgetError, DailyBudget, FetcherConfigError, FetchError, get_fetcher
from bedahreview.fetchers.base import Review
from bedahreview.fetchers.platforms import LinkRouter
from bedahreview.competitors import compare_competitors
from bedahreview.web import RateLimiter, make_server

from test_platforms import TOKO, TOKO_ROWS, FakeActors, _no_resolve

DAY = 24 * 3600


class Clock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class DailyBudgetTests(unittest.TestCase):
    def test_runs_count_at_their_worst_case_until_the_budget_is_gone(self):
        budget = DailyBudget(0.10)
        budget.reserve(0.05)
        budget.reserve(0.05)
        with self.assertRaises(BudgetError):
            budget.reserve(0.05)
        self.assertEqual(float(budget.spent), 0.10)  # a refused run takes nothing

    def test_a_run_that_would_overshoot_is_refused_even_if_something_is_left(self):
        budget = DailyBudget(0.08)
        budget.reserve(0.05)
        with self.assertRaises(BudgetError):
            budget.reserve(0.05)

    def test_it_starts_again_the_next_day(self):
        clock = Clock()
        budget = DailyBudget(0.05, clock)
        budget.reserve(0.05)
        with self.assertRaises(BudgetError):
            budget.reserve(0.05)
        clock.now += DAY
        budget.reserve(0.05)

    def test_threads_cannot_overspend(self):
        budget = DailyBudget(0.50)
        taken = []

        def grab():
            try:
                budget.reserve(0.05)
                taken.append(1)
            except BudgetError:
                pass

        threads = [threading.Thread(target=grab) for _ in range(40)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(taken), 10)

    def test_the_error_is_a_fetch_error_with_a_plain_message(self):
        self.assertTrue(issubclass(BudgetError, FetchError))


class RouterBudgetTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeActors(TOKO_ROWS)
        self.cache = Path(tempfile.mkdtemp())

    def router(self, limit):
        return LinkRouter("t", 0.05, self.cache, client=self.client, resolver=_no_resolve, budget=DailyBudget(limit))

    def test_a_refused_run_never_reaches_the_paid_service(self):
        router = self.router(0.05)
        router.fetch_fresh(TOKO, 5, "latest")
        with self.assertRaises(BudgetError):
            router.fetch_fresh(TOKO, 5, "latest")
        self.assertEqual(len(self.client.calls), 1)

    def test_a_saved_copy_costs_nothing(self):
        router = self.router(0.05)
        router.fetch_reviews(TOKO, 5, "latest")
        router.fetch_reviews(TOKO, 5, "latest")  # served from the disk cache
        self.assertEqual(len(self.client.calls), 1)

    def test_search_counts_too(self):
        router = self.router(0.05)
        router.fetch_fresh(TOKO, 5, "latest")
        with self.assertRaises(BudgetError):
            router.search("tokopedia", "hijab segiempat")
        self.assertEqual(len(self.client.calls), 1)


class ConfigTests(unittest.TestCase):
    def env(self, **extra):
        return mock.patch.dict(os.environ, dict({"FETCHER": "apify", "APIFY_TOKEN": "t"}, **extra), clear=False)

    def test_default_and_valid_values_work(self):
        with self.env():
            os.environ.pop("APIFY_DAILY_BUDGET_USD", None)
            self.assertIsNotNone(get_fetcher())
        for value in ("2.5", "5"):
            with self.env(APIFY_DAILY_BUDGET_USD=value):
                self.assertIsNotNone(get_fetcher())

    def test_typos_and_a_too_high_budget_are_refused(self):
        for bad in ("abc", "0", "-1", "50", "5.01"):
            with self.env(APIFY_DAILY_BUDGET_USD=bad):
                with self.assertRaises(FetcherConfigError):
                    get_fetcher()


class CompareBudgetTests(unittest.TestCase):
    def test_when_every_competitor_hits_the_limit_the_budget_error_comes_through(self):
        def fetch(url, limit, mode):
            raise BudgetError("Demo limit reached today.")

        own = [{"theme": "Sizing & fit", "bucket": "fix", "fake": False}]
        with self.assertRaises(BudgetError):
            compare_competitors(fetch, lambda r, product=None: [], own, "x", [{"url": "https://a", "title": ""}])


class StubFetcher:
    last_platform = "tokopedia"
    last_from_cache = False

    def __init__(self, error=None):
        self.error, self.calls = error, 0

    def check_link(self, url):
        return url

    def fetch_reviews(self, url, limit, mode="latest"):
        self.calls += 1
        if self.error:
            raise self.error
        return [Review(star=1, text="kekecilan", variant=None, date="", photo_count=0, seller_reply=None, product="Kaos")]

    fetch_fresh = fetch_reviews

    def search(self, platform, query):
        return []


class WebLimitTests(unittest.TestCase):
    def serve(self, fetcher, **fns):
        server = make_server(0, classify_fn=lambda reviews, product=None: [], fetcher=fetcher, **fns)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def post(self, server, path, payload):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.request("POST", path, json.dumps(payload).encode(), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    @staticmethod
    def raising(*args, **kwargs):
        raise BudgetError("Demo limit reached today.")

    def test_link_box_says_so_and_offers_paste(self):
        server = self.serve(StubFetcher(BudgetError("Demo limit reached today.")))
        status, data = self.post(server, "/api/classify", {"url": TOKO})
        self.assertEqual((status, data["limit"], data["fallback"]), (429, "budget", "paste"))
        self.assertIn("Demo limit reached today", data["error"])

    def test_finding_and_comparing_competitors_say_so(self):
        server = self.serve(StubFetcher(), suggest_fn=self.raising, compare_fn=self.raising)
        status, data = self.post(server, "/api/competitors/suggest", {"url": TOKO, "title": "Hijab"})
        self.assertEqual((status, data["limit"]), (429, "budget"))
        own = [{"theme": "Sizing & fit", "bucket": "fix", "fake": False}]
        body = {"url": TOKO, "title": "Hijab", "own": own, "competitors": [{"url": "https://www.tokopedia.com/a/b"}]}
        status, data = self.post(server, "/api/competitors/compare", body)
        self.assertEqual((status, data["limit"]), (429, "budget"))

    def test_paid_actions_have_their_own_lower_hourly_limit(self):
        fetcher = StubFetcher()
        server = self.serve(fetcher)
        server.RequestHandlerClass.paid_limiter = RateLimiter(2, 3600)
        results = [self.post(server, "/api/classify", {"url": TOKO}) for _ in range(3)]
        self.assertNotIn(429, [r[0] for r in results[:2]])
        self.assertEqual((results[2][0], results[2][1]["limit"]), (429, "hour"))
        self.assertEqual(fetcher.calls, 2)  # the third never reached the paid service

    def test_pasting_is_not_held_back_by_the_paid_limit(self):
        server = self.serve(StubFetcher())
        server.RequestHandlerClass.paid_limiter = RateLimiter(1, 3600)
        for _ in range(3):
            status, data = self.post(server, "/api/classify", {"text": "bagus"})
            self.assertNotEqual(status, 429, data)


if __name__ == "__main__":
    unittest.main()
