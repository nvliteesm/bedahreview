import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bedahreview.fetchers.platforms import LinkRouter, TokopediaFetcher, _Actors, tokopedia_review
from bedahreview.fetchers import CachedFetcher, FetcherConfigError, FetchError, get_fetcher, to_review_dicts
from bedahreview.fetchers.base import MAX_LIMIT, Review, check_shopee_product_url, clamp_limit, per_star_limit, product_title_from_url

from test_platforms import TOKO, FakeActors

URL = "https://shopee.co.id/Kaos-Boxy-Retro-i.42255190.2257643102"
SECRET_NAME = "Budi Santoso"

ITEM = {
    "rating": 2,
    "review_text": "  Pas sampe koyak bajunya  ",
    "variant_name": "Hitam,  L",
    "review_time_iso": "2026-10-07T08:30:00Z",
    "images_count": 2,
    "seller_reply_text": "Mohon maaf Kak",
    "product_name": "Kaos Boxy Retro",
    # fields the app must never keep:
    "buyer_name": SECRET_NAME, "buyer_id": "9911", "buyer_profile_url": "https://x/avatar.png",
}


class CountingInner:
    def __init__(self, reviews):
        self.reviews, self.calls = reviews, []

    def fetch_reviews(self, url, limit, mode="latest"):
        self.calls.append((url, limit, mode))
        return self.reviews[:limit]


def _reviews(n):
    return [Review(star=3, text="r%d" % i, variant=None, date="2026-10-01", photo_count=0, replied=False) for i in range(n)]


class UrlCheckTests(unittest.TestCase):
    def test_accepts_product_links_and_strips_tracking(self):
        self.assertEqual(check_shopee_product_url(URL + "?sp_atk=abc&xptdk=1#x"), URL)
        self.assertEqual(check_shopee_product_url("https://www.shopee.co.id/p-i.1.2/"), "https://www.shopee.co.id/p-i.1.2/")

    def test_title_is_read_from_the_link(self):
        self.assertEqual(product_title_from_url(URL), "Kaos Boxy Retro")
        self.assertEqual(product_title_from_url("https://shopee.co.id/Kopi-Susu-%26-Gula-i.1.2"), "Kopi Susu & Gula")
        self.assertEqual(product_title_from_url("https://shopee.co.id/product-i.1.2"), "product")
        self.assertEqual(len(product_title_from_url("https://shopee.co.id/" + "a-" * 400 + "i.1.2")), 150)

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
        for odd in (float("inf"), float("-inf"), float("nan"), None, [], {}):
            with self.assertRaises(FetchError, msg=repr(odd)):
                clamp_limit(odd)
        with self.assertRaises(FetchError):
            clamp_limit("many")


class MappingTests(unittest.TestCase):
    def test_fields_are_mapped(self):
        r = tokopedia_review(ITEM)
        self.assertEqual((r.star, r.text, r.variant, r.date, r.photo_count, r.replied),
                         (2, "Pas sampe koyak bajunya", "Hitam, L", "2026-10-07", 2, True))  # only yes or no: the reply text is not kept

    def test_reviewer_identity_never_reaches_review(self):
        r = tokopedia_review(ITEM)
        self.assertEqual({f.name for f in dataclasses.fields(Review)},
                         {"star", "text", "variant", "date", "photo_count", "replied", "product"})
        blob = json.dumps(dataclasses.asdict(r)) + repr(r)
        for leaked in (SECRET_NAME, "9911", "avatar.png", "Mohon maaf Kak"):
            self.assertNotIn(leaked, blob)

    def test_odd_items_do_not_crash(self):
        r = tokopedia_review({"rating": 9, "review_text": None, "variant_name": 5, "review_time_iso": 1760000000, "images_count": None, "seller_reply_text": 5})
        self.assertEqual((r.star, r.text, r.variant, r.photo_count, r.replied), (None, "", None, 0, False))
        self.assertEqual(r.date, "2025-10-09")  # epoch seconds
        self.assertEqual(tokopedia_review({"review_time_iso": 1760000000000}).date, "2025-10-09")  # milliseconds

    def test_to_review_dicts_matches_what_the_classifier_expects(self):
        d = to_review_dicts([tokopedia_review(ITEM)])[0]
        self.assertEqual(d, {"id": "1", "stars": 2, "variant": "Hitam, L", "date": "2026-10-07", "text": "Pas sampe koyak bajunya"})

    def test_complaint_fetches_ask_each_low_star_for_a_little_over_a_third(self):
        self.assertEqual([per_star_limit(n) for n in (1, 3, 10, 20)], [2, 2, 5, 8])


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
        real = TokopediaFetcher(_Actors(FakeActors([ITEM]), 0.05))
        CachedFetcher(real, self.dir, lambda u: u).fetch_reviews(TOKO, 5)
        saved = next(self.dir.glob("*.json")).read_text(encoding="utf-8")
        self.assertNotIn(SECRET_NAME, saved)
        self.assertNotIn("Mohon maaf Kak", saved)
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

    def test_apify_builds_the_link_router_with_the_default_cap(self):
        with self.env(FETCHER="apify", APIFY_TOKEN="x"):
            f = get_fetcher()
        self.assertIsInstance(f, LinkRouter)
        self.assertEqual(f._caches["lazada"].inner._actors._cap, 0.05)


if __name__ == "__main__":
    unittest.main()
