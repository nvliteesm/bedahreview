import dataclasses
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from bedahreview.fetchers import CachedFetcher, FetcherConfigError, FetchError, get_fetcher, to_review_dicts
from bedahreview.fetchers.apify_shopee import ACTOR_ID, ApifyShopeeFetcher, review_from_item
from bedahreview.fetchers.base import MAX_LIMIT, Review, check_shopee_product_url, clamp_limit

URL = "https://shopee.co.id/Kaos-Boxy-Retro-i.42255190.2257643102"
SECRET_NAME = "Budi Santoso"

ITEM = {
    "ratingStar": 2,
    "comment": "  Pas sampe koyak bajunya  ",
    "variations": [{"name": "Warna", "options": ["Hitam", "L"]}],
    "createdAt": "2026-10-07T08:30:00Z",
    "images": ["a.jpg", "b.jpg"],
    "shopReply": {"comment": "Mohon maaf Kak"},
    # fields the app must never keep:
    "author": SECRET_NAME, "authorId": 9911, "authorPortrait": "avatar.png", "isAnonymous": False,
}


class FakeApifyClient:
    """Stands in for apify_client.ApifyClient and records what the fetcher sends."""

    def __init__(self, items=None, run=None, error=None):
        self.items = [ITEM] if items is None else items
        self.run = {"defaultDatasetId": "ds1", "status": "SUCCEEDED"} if run is None else run
        self.error, self.calls, self.actor_ids = error, [], []

    def actor(self, actor_id):
        self.actor_ids.append(actor_id)
        return self

    def call(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.run or None

    def dataset(self, _id):
        return mock.Mock(iterate_items=lambda: iter(self.items))


class StarFakeClient(FakeApifyClient):
    """Answers each run with reviews of the star the fetcher asked for, so a complaints run can be checked."""

    def __init__(self, per_star, failing=()):
        super().__init__()
        self.per_star, self.failing, self._last = per_star, failing, None

    def call(self, **kwargs):
        self.calls.append(kwargs)
        self._last = kwargs["run_input"].get("starFilter")
        if self._last in self.failing:
            raise RuntimeError("boom")
        return self.run

    def dataset(self, _id):
        star = self._last
        items = [dict(ITEM, ratingStar=int(star), comment="review %s-%d" % (star, i)) for i in range(self.per_star.get(star, 0))]
        return mock.Mock(iterate_items=lambda: iter(items))


class CountingInner:
    def __init__(self, reviews):
        self.reviews, self.calls = reviews, []

    def fetch_reviews(self, url, limit, mode="latest"):
        self.calls.append((url, limit, mode))
        return self.reviews[:limit]


def _reviews(n):
    return [Review(star=3, text="r%d" % i, variant=None, date="2026-10-01", photo_count=0, seller_reply=None) for i in range(n)]


class UrlCheckTests(unittest.TestCase):
    def test_accepts_product_links_and_strips_tracking(self):
        self.assertEqual(check_shopee_product_url(URL + "?sp_atk=abc&xptdk=1#x"), URL)
        self.assertEqual(check_shopee_product_url("https://www.shopee.co.id/p-i.1.2/"), "https://www.shopee.co.id/p-i.1.2/")

    def test_rejects_everything_else(self):
        for bad in [
            "https://tokopedia.com/shop/product-i.1.2",
            "https://shopee.com.my/p-i.1.2",
            "https://shopee.co.id.evil.com/p-i.1.2",
            "https://shopee.co.id@evil.com/p-i.1.2",
            "http://shopee.co.id/p-i.1.2",
            "https://shopee.co.id:8080/p-i.1.2",
            "https://shopee.co.id/shop-page",
            "https://shopee.co.id/product/123/456",
            "https://id.shp.ee/abc",
            "not a link", "", None,
        ]:
            with self.assertRaises(FetchError, msg=repr(bad)):
                check_shopee_product_url(bad)

    def test_limit_is_clamped(self):
        self.assertEqual((clamp_limit(0), clamp_limit(5), clamp_limit(999), clamp_limit("7")), (1, 5, MAX_LIMIT, 7))
        with self.assertRaises(FetchError):
            clamp_limit("many")


class MappingTests(unittest.TestCase):
    def test_fields_are_mapped(self):
        r = review_from_item(ITEM)
        self.assertEqual((r.star, r.text, r.variant, r.date, r.photo_count, r.seller_reply),
                         (2, "Pas sampe koyak bajunya", "Hitam, L", "2026-10-07", 2, "Mohon maaf Kak"))

    def test_reviewer_identity_never_reaches_review(self):
        r = review_from_item(ITEM)
        self.assertEqual({f.name for f in dataclasses.fields(Review)},
                         {"star", "text", "variant", "date", "photo_count", "seller_reply"})
        blob = json.dumps(dataclasses.asdict(r)) + repr(r)
        for leaked in (SECRET_NAME, "9911", "avatar.png"):
            self.assertNotIn(leaked, blob)

    def test_odd_items_do_not_crash(self):
        r = review_from_item({"ratingStar": 9, "comment": None, "variations": "x", "createdAt": 1760000000, "images": None, "shopReply": 5})
        self.assertEqual((r.star, r.text, r.variant, r.photo_count, r.seller_reply), (None, "", None, 0, None))
        self.assertEqual(r.date, "2025-10-09")  # epoch seconds
        self.assertEqual(review_from_item({"createdAt": 1760000000000}).date, "2025-10-09")  # milliseconds

    def test_to_review_dicts_matches_what_the_classifier_expects(self):
        d = to_review_dicts([review_from_item(ITEM)])[0]
        self.assertEqual(d, {"id": "1", "stars": 2, "variant": "Hitam, L", "date": "2026-10-07", "text": "Pas sampe koyak bajunya"})


class ApifyFetcherTests(unittest.TestCase):
    def test_sends_input_and_a_spending_cap(self):
        client = FakeApifyClient()
        reviews = ApifyShopeeFetcher("tok", client=client).fetch_reviews(URL + "?utm=1", 10)
        call = client.calls[0]
        self.assertEqual(client.actor_ids, [ACTOR_ID])
        self.assertEqual(call["run_input"], {"startUrls": [{"url": URL}], "maxReviewsPerProduct": 10, "contentFilter": "with comments"})
        self.assertEqual(call["max_total_charge_usd"], Decimal("0.05"))
        self.assertEqual(len(reviews), 1)

    def test_complaints_mode_runs_one_filtered_run_per_low_star_worst_first(self):
        client = StarFakeClient({"1": 4, "2": 4, "3": 4})
        reviews = ApifyShopeeFetcher("tok", client=client).fetch_reviews(URL, 10, "complaints")
        self.assertEqual(sorted(c["run_input"]["starFilter"] for c in client.calls), ["1", "2", "3"])
        self.assertTrue(all(c["max_total_charge_usd"] == Decimal("0.05") for c in client.calls))
        self.assertEqual([r.star for r in reviews], [1, 1, 1, 1, 2, 2, 2, 2, 3, 3])

    def test_complaints_mode_survives_one_failed_or_empty_star(self):
        client = StarFakeClient({"1": 3, "3": 2}, failing=("2",))
        reviews = ApifyShopeeFetcher("tok", client=client).fetch_reviews(URL, 10, "complaints")
        self.assertEqual(sorted({r.star for r in reviews}), [1, 3])

    def test_complaints_mode_fails_cleanly_when_every_star_fails_or_is_empty(self):
        with self.assertRaises(FetchError):
            ApifyShopeeFetcher("tok", client=StarFakeClient({}, failing=("1", "2", "3"))).fetch_reviews(URL, 5, "complaints")
        with self.assertRaises(FetchError):
            ApifyShopeeFetcher("tok", client=StarFakeClient({})).fetch_reviews(URL, 5, "complaints")

    def test_unknown_mode_never_reaches_the_paid_service(self):
        client = FakeApifyClient()
        with self.assertRaises(FetchError):
            ApifyShopeeFetcher("tok", client=client).fetch_reviews(URL, 5, "everything")
        self.assertEqual(client.calls, [])

    def test_cap_is_configurable_and_limit_clamped(self):
        client = FakeApifyClient()
        ApifyShopeeFetcher("tok", max_cost_usd=0.1, client=client).fetch_reviews(URL, 500)
        self.assertEqual(client.calls[0]["max_total_charge_usd"], Decimal("0.1"))
        self.assertEqual(client.calls[0]["run_input"]["maxReviewsPerProduct"], MAX_LIMIT)

    def test_bad_url_never_reaches_the_paid_service(self):
        client = FakeApifyClient()
        with self.assertRaises(FetchError):
            ApifyShopeeFetcher("tok", client=client).fetch_reviews("https://tokopedia.com/x", 5)
        self.assertEqual(client.calls, [])

    def test_empty_result_is_a_fetch_error(self):
        for client in (FakeApifyClient(items=[]), FakeApifyClient(items=[{"ratingStar": 5, "comment": ""}])):
            with self.assertRaises(FetchError):
                ApifyShopeeFetcher("tok", client=client).fetch_reviews(URL, 5)

    def test_timeout_and_service_errors_are_clean_and_do_not_leak_the_token(self):
        with self.assertRaises(FetchError):
            ApifyShopeeFetcher("tok", client=FakeApifyClient(run={})).fetch_reviews(URL, 5)
        client = FakeApifyClient(error=RuntimeError("401 bad token SECRET-TOKEN-123"))
        with self.assertRaises(FetchError) as ctx:
            ApifyShopeeFetcher("SECRET-TOKEN-123", client=client).fetch_reviews(URL, 5)
        self.assertNotIn("SECRET-TOKEN-123", str(ctx.exception))
        self.assertIn("RuntimeError", str(ctx.exception))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_call_is_served_from_cache(self):
        inner = CountingInner(_reviews(10))
        f = CachedFetcher(inner, self.dir)
        first = f.fetch_reviews(URL + "?a=1", 10)
        self.assertFalse(f.last_from_cache)
        second = f.fetch_reviews(URL + "?b=2", 10)  # same product, different tracking
        self.assertTrue(f.last_from_cache)
        self.assertEqual((first, len(inner.calls)), (second, 1))

    def test_smaller_limit_reuses_cache_but_larger_refetches(self):
        inner = CountingInner(_reviews(20))
        f = CachedFetcher(inner, self.dir)
        f.fetch_reviews(URL, 10)
        self.assertEqual(len(f.fetch_reviews(URL, 5)), 5)
        self.assertEqual(len(inner.calls), 1)
        f.fetch_reviews(URL, 20)
        self.assertEqual(len(inner.calls), 2)

    def test_modes_are_cached_separately(self):
        inner = CountingInner(_reviews(20))
        f = CachedFetcher(inner, self.dir)
        f.fetch_reviews(URL, 10, "latest")
        f.fetch_reviews(URL, 10, "complaints")
        self.assertEqual(len(inner.calls), 2)
        f.fetch_reviews(URL, 10, "complaints")
        self.assertEqual((len(inner.calls), f.last_from_cache), (2, True))

    def test_failures_are_not_cached_and_cache_holds_no_reviewer_data(self):
        class Boom(CountingInner):
            def fetch_reviews(self, url, limit, mode="latest"):
                raise FetchError("nope")

        with self.assertRaises(FetchError):
            CachedFetcher(Boom([]), self.dir).fetch_reviews(URL, 5)
        self.assertEqual(list(self.dir.glob("*.json")), [])
        real = ApifyShopeeFetcher("t", client=FakeApifyClient())
        CachedFetcher(real, self.dir).fetch_reviews(URL, 5)
        saved = next(self.dir.glob("*.json")).read_text(encoding="utf-8")
        self.assertNotIn(SECRET_NAME, saved)
        self.assertIn("Pas sampe koyak bajunya", saved)

    def test_corrupt_cache_file_is_ignored(self):
        inner = CountingInner(_reviews(3))
        f = CachedFetcher(inner, self.dir)
        f.fetch_reviews(URL, 3)
        next(self.dir.glob("*.json")).write_text("{not json", encoding="utf-8")
        self.assertEqual(len(f.fetch_reviews(URL, 3)), 3)
        self.assertEqual(len(inner.calls), 2)


class GetFetcherTests(unittest.TestCase):
    def env(self, **values):
        base = {k: v for k, v in os.environ.items() if k not in ("FETCHER", "APIFY_TOKEN", "APIFY_MAX_COST_USD")}
        base.update(values)
        return mock.patch.dict(os.environ, base, clear=True)

    def test_paste_is_the_default(self):
        with self.env():
            self.assertIsNone(get_fetcher())
        with self.env(FETCHER="paste", APIFY_TOKEN="x"):
            self.assertIsNone(get_fetcher())

    def test_apify_needs_a_token_and_a_sane_cap(self):
        with self.env(FETCHER="apify"):
            with self.assertRaises(FetcherConfigError):
                get_fetcher()
        for cap in ("abc", "0", "-1", "50"):
            with self.env(FETCHER="apify", APIFY_TOKEN="x", APIFY_MAX_COST_USD=cap):
                with self.assertRaises(FetcherConfigError, msg=cap):
                    get_fetcher()
        with self.env(FETCHER="nonsense"):
            with self.assertRaises(FetcherConfigError):
                get_fetcher()

    def test_apify_builds_a_cached_fetcher_with_the_default_cap(self):
        with self.env(FETCHER="apify", APIFY_TOKEN="x"):
            f = get_fetcher()
        self.assertIsInstance(f, CachedFetcher)
        self.assertEqual(f.inner.max_cost_usd, 0.05)


if __name__ == "__main__":
    unittest.main()
