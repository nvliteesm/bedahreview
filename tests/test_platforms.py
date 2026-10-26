import http.client
import json
import tempfile
import threading
import unittest
import urllib.error
from email.message import Message
from pathlib import Path

from bedahreview.fetchers import FetchError, LinkError
from bedahreview.web import make_server
from bedahreview.fetchers.platforms import LAZADA_ACTOR, TIKTOK_ACTOR, TOKOPEDIA_ACTOR, LinkRouter, identify_link, resolve_short_link

TOKO = "https://www.tokopedia.com/nayla-hijab/hijab-segiempat-paris-f4b54"
LAZADA = "https://www.lazada.co.id/products/pdp-i5558538744-s13072104368.html"
TIKTOK_ID = "1736089506777433672"
TIKTOK = "https://shop-id.tokopedia.com/view/product/" + TIKTOK_ID
SECRET_NAME = "Siti Rahma"


def _no_resolve(url):
    raise AssertionError("must not follow a link here")


class FakeActors:
    """Stands in for the Apify client: one canned list of rows per actor, and a record of every call."""

    def __init__(self, rows, status="SUCCEEDED"):
        self.rows, self.status, self.calls = rows, status, []

    def actor(self, actor_id):
        outer = self

        class Actor:
            def call(self, run_input, max_total_charge_usd, timeout_secs, wait_secs):
                outer.calls.append((actor_id, run_input, max_total_charge_usd))
                return {"status": outer.status, "defaultDatasetId": str(len(outer.calls) - 1)}

        return Actor()

    def dataset(self, dataset_id):
        rows = self.rows(self.calls[int(dataset_id)][1]) if callable(self.rows) else self.rows

        class Dataset:
            def iterate_items(self):
                return iter(rows)

        return Dataset()


TOKO_ROWS = [
    {"rating": 1, "review_text": "wrna ny kurang sesuai", "variant_name": "Grey", "review_time_iso": "2024-11-04T12:49:39.000Z", "seller_reply_text": "maaf kak",
     "images_count": 2, "product_name": "Hijab Segiempat", "buyer_name": SECRET_NAME, "buyer_id": "9911", "buyer_profile_url": "https://x/9911"},
    {"rating": 1, "review_text": "", "variant_name": "Putih", "review_time_iso": "2024-12-27T22:40:55.000Z"},
    {"rating": 5, "review_text": "bagus", "variant_name": "Hitam", "review_time_iso": "2024-12-28T00:00:00.000Z"},
]
LAZADA_ROWS = [
    {"review": {"rating": 2, "content": "ukuran kecil,padahal xxl", "sku_info": "Tampilan Warna:Abu-abu, Ukuran:Int:XXL", "review_time": "09 Okt 2023", "image_count": 0},
     "product": {"title": "Singlet Cowok"}, "seller_reply": None, "buyer": {"buyer_name": SECRET_NAME}},
    {"record_type": "summary", "rating_summary": {"average": 4}},
    {"review": {"rating": 1, "content": "robek", "sku_info": "Warna:Biru", "review_time": "02 Feb 2024"}, "seller_reply": {"content": "terima kasih"}},
]
TIKTOK_ROWS = [
    {"review_rating": 4, "review_text": "ada bercak hitam", "sku_specification": "AiryFit - Putih , M", "review_time": "1788266820318",
     "product_name": "MyBasic Tee", "reviewer_name": SECRET_NAME, "reviewer_id": "9911"},
]


def router(rows, resolver=_no_resolve, status="SUCCEEDED"):
    client = FakeActors(rows, status)
    return LinkRouter("token", 0.05, Path(tempfile.mkdtemp()), client=client, resolver=resolver), client


class IdentifyTests(unittest.TestCase):
    def test_each_platform_is_recognised_from_the_link_alone(self):
        self.assertEqual(identify_link(TOKO + "?aff_unique_id=&channel=others", _no_resolve), ("tokopedia", TOKO))
        self.assertEqual(identify_link(LAZADA + "?pvid=abc&search=jfy&spm=x", _no_resolve), ("lazada", LAZADA))
        self.assertEqual(identify_link(TIKTOK, _no_resolve), ("tiktokshop", TIKTOK))
        self.assertEqual(identify_link("https://www.tiktok.com/shop/pdp/some-name/" + TIKTOK_ID + "?x=1", _no_resolve), ("tiktokshop", TIKTOK))

    def test_a_share_message_with_words_around_the_link_works(self):
        self.assertEqual(identify_link("Lihat produk ini! %s?pvid=1 Mantap." % LAZADA, _no_resolve)[0], "lazada")

    def test_short_links_are_resolved_then_identified(self):
        seen = []

        def resolver(url):
            seen.append(url)
            return TIKTOK + "?_svg=1"

        self.assertEqual(identify_link("https://vt.tokopedia.com/t/ZS9yYewLpEeYF-vNQLy/", resolver), ("tiktokshop", TIKTOK))
        self.assertEqual(seen, ["https://vt.tokopedia.com/t/ZS9yYewLpEeYF-vNQLy/"])

    def test_shopee_gets_a_clear_message(self):
        with self.assertRaises(LinkError) as ctx:
            identify_link("https://shopee.co.id/Kaos-i.450430857.27207810374", _no_resolve)
        self.assertIn("Paste the reviews", str(ctx.exception))

    def test_everything_else_is_refused(self):
        for bad in ("", "halo", "javascript:alert(1)", "http://www.tokopedia.com/a/b", "https://www.tokopedia.com.evil.com/a/b",
                    "https://user@www.tokopedia.com/a/b", "https://www.tokopedia.com:8443/a/b", "https://www.tokopedia.com/a",
                    "https://www.lazada.co.id/shop/x", "https://www.tiktok.com/@someone/video/1", "https://example.com/p", "https://shop-id.tokopedia.com/other"):
            with self.assertRaises(LinkError, msg=bad):
                identify_link(bad, _no_resolve)


class ShortLinkTests(unittest.TestCase):
    class Opener:
        def __init__(self, hops):
            self.hops, self.opened = list(hops), []

        def open(self, request, timeout):
            self.opened.append(request.full_url)
            code, location = self.hops.pop(0)
            headers = Message()
            if location:
                headers["Location"] = location
            raise urllib.error.HTTPError(request.full_url, code, "x", headers, None)

    def test_follows_redirects_by_hand_until_a_shop_page(self):
        opener = self.Opener([(302, "https://vt.tokopedia.com/second"), (301, "https://shop-id.tokopedia.com/view/product/%s?a=1" % TIKTOK_ID)])
        self.assertEqual(resolve_short_link("https://vt.tokopedia.com/t/x/", opener), "https://shop-id.tokopedia.com/view/product/%s?a=1" % TIKTOK_ID)
        self.assertEqual(len(opener.opened), 2)

    def test_never_leaves_the_known_hosts(self):
        for target in ("https://evil.example/x", "http://shop-id.tokopedia.com/view/product/%s" % TIKTOK_ID, "https://127.0.0.1/admin"):
            with self.assertRaises(LinkError, msg=target):
                resolve_short_link("https://vt.tokopedia.com/t/x/", self.Opener([(302, target)]))

    def test_no_redirect_loops_and_no_dead_ends(self):
        with self.assertRaises(LinkError):
            resolve_short_link("https://vt.tokopedia.com/t/x/", self.Opener([(302, "https://vt.tokopedia.com/t/x/")] * 10))
        with self.assertRaises(LinkError):
            resolve_short_link("https://vt.tokopedia.com/t/x/", self.Opener([(404, None)]))


class FetchTests(unittest.TestCase):
    def test_tokopedia_sends_the_right_input_and_keeps_only_low_rated_reviews_with_text(self):
        r, client = router(TOKO_ROWS)
        out = r.fetch_reviews(TOKO + "?utm=1", 5, "complaints")
        actor, run_input, cap = client.calls[0]
        self.assertEqual((actor, run_input["product_url"], run_input["sort_by"], run_input["results_wanted"], str(cap)), (TOKOPEDIA_ACTOR, TOKO, "rating asc", 8, "0.05"))
        self.assertEqual([(x.star, x.text, x.variant, x.date, x.photo_count, x.seller_reply, x.product) for x in out],
                         [(1, "wrna ny kurang sesuai", "Grey", "2024-11-04", 2, "maaf kak", "Hijab Segiempat")])  # blank and 5-star rows dropped
        self.assertEqual(r.last_platform, "tokopedia")

    def test_latest_mode_keeps_every_rating(self):
        r, client = router(TOKO_ROWS)
        out = r.fetch_reviews(TOKO, 5, "latest")
        self.assertEqual([x.star for x in out], [1, 5])
        self.assertEqual(client.calls[0][1]["sort_by"], "create_time desc")

    def test_lazada_asks_for_low_stars_cleans_variants_and_skips_summary_rows(self):
        r, client = router(LAZADA_ROWS)
        out = r.fetch_reviews(LAZADA, 5, "complaints")
        actor, run_input, _ = client.calls[0]
        self.assertEqual((actor, run_input["starRatings"], run_input["startUrls"], run_input["includeRatingSummary"]), (LAZADA_ACTOR, ["1", "2", "3"], [{"url": LAZADA}], False))
        self.assertEqual([(x.star, x.variant, x.seller_reply, x.product) for x in out],
                         [(1, "Biru", "terima kasih", None), (2, "Abu-abu, XXL", None, "Singlet Cowok")])  # worst star first
        self.assertEqual(r.last_platform, "lazada")

    def test_tiktok_complaints_run_once_per_low_star_and_never_ask_for_personal_data(self):
        def rows(run_input):  # the actor filters by star, so only the 2-star run finds something
            return [dict(TIKTOK_ROWS[0], review_rating=2, review_text="ada bercak")] if run_input["reviews_filter"] == "2_star" else []

        r, client = router(rows)
        out = r.fetch_reviews(TIKTOK, 6, "complaints")
        self.assertEqual(sorted(c[1]["reviews_filter"] for c in client.calls), ["1_star", "2_star", "3_star"])
        for actor, run_input, _ in client.calls:
            self.assertEqual((actor, run_input["product_ids"], run_input["region"], run_input["include_personal_information"]), (TIKTOK_ACTOR, [TIKTOK_ID], "ID", False))
        self.assertEqual([(x.star, x.text) for x in out], [(2, "ada bercak")])

    def test_tiktok_latest_is_one_run_with_dates_and_variants(self):
        r, client = router(TIKTOK_ROWS)
        out = r.fetch_reviews(TIKTOK, 5, "latest")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual((out[0].star, out[0].variant, out[0].date, out[0].product), (4, "AiryFit - Putih, M", "2026-09-01", "MyBasic Tee"))

    def test_buyer_identity_never_reaches_the_reviews_or_the_cache(self):
        cache_dir = Path(tempfile.mkdtemp())
        for rows, link in ((TOKO_ROWS, TOKO), (LAZADA_ROWS, LAZADA), (TIKTOK_ROWS, TIKTOK)):
            r = LinkRouter("t", 0.05, cache_dir, client=FakeActors(rows), resolver=_no_resolve)
            out = r.fetch_reviews(link, 5, "latest")
            self.assertNotIn(SECRET_NAME + "9911", json.dumps([vars(x) for x in out]))
            self.assertNotIn(SECRET_NAME, repr(out))
        for f in cache_dir.glob("*.json"):
            self.assertNotIn(SECRET_NAME, f.read_text())
            self.assertNotIn("9911", f.read_text())

    def test_second_fetch_comes_from_the_cache_and_costs_nothing(self):
        r, client = router(TOKO_ROWS)
        r.fetch_reviews(TOKO, 5, "latest")
        r.fetch_reviews(TOKO + "?other=1", 5, "latest")
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(r.last_from_cache)

    def test_shopee_and_bad_links_never_reach_the_paid_service(self):
        r, client = router(TOKO_ROWS)
        for bad in ("https://shopee.co.id/p-i.1.2", "https://example.com/x", "nonsense"):
            with self.assertRaises(LinkError):
                r.fetch_reviews(bad, 5, "latest")
            with self.assertRaises(LinkError):
                r.check_link(bad)
        with self.assertRaises(FetchError):
            r.fetch_reviews(TOKO, 5, "everything")
        self.assertEqual(client.calls, [])

    def test_failures_are_clean(self):
        r, _ = router(TOKO_ROWS, status="FAILED")
        with self.assertRaises(FetchError) as ctx:
            r.fetch_reviews(TOKO, 5, "latest")
        self.assertIn("could not read", str(ctx.exception))
        r, _ = router([])
        with self.assertRaises(FetchError):
            r.fetch_reviews(LAZADA, 5, "latest")
        only_good = [{"rating": 5, "review_text": "bagus", "variant_name": "x", "review_time_iso": ""}]
        r, _ = router(only_good)
        with self.assertRaises(FetchError) as ctx:
            r.fetch_reviews(TOKO, 5, "complaints")
        self.assertIn("Latest reviews", str(ctx.exception))

        class Boom:
            def actor(self, _):
                raise RuntimeError("secret token abc")

        r = LinkRouter("t", 0.05, Path(tempfile.mkdtemp()), client=Boom(), resolver=_no_resolve)
        with self.assertRaises(FetchError) as ctx:
            r.fetch_reviews(TOKO, 5, "latest")
        self.assertNotIn("abc", str(ctx.exception))


class WebFlowTests(unittest.TestCase):
    def setUp(self):
        self.seen = []

        def classify(reviews, product=None):
            self.seen.append((reviews, product))
            return [{"id": r["id"], "bucket": "ignore", "theme": "Other", "variant": None, "cause": "none", "check_first": False, "proof_quote": None,
                     "likely_fake": {"flag": False, "signals": []}, "fix_suggestion_id": None, "fight": None, "confidence": "high"} for r in reviews]

        self.router, self.client = router(TOKO_ROWS)
        self.server = make_server(0, classify_fn=classify, fetcher=self.router)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, payload):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/classify", json.dumps(payload).encode(), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def test_a_tokopedia_link_is_fetched_and_the_listing_title_reaches_the_model(self):
        status, out = self.post({"url": TOKO + "?utm=1", "limit": 5})
        self.assertEqual(status, 200)
        self.assertEqual((out["source"]["url"], out["source"]["title"], out["source"]["count"]), (TOKO, "Hijab Segiempat", 1))
        self.assertEqual(self.seen[0][1], "Hijab Segiempat")
        self.assertNotIn(SECRET_NAME, json.dumps(out))

    def test_shopee_link_is_a_clean_400_that_costs_nothing(self):
        status, out = self.post({"url": "https://shopee.co.id/Kaos-i.450430857.27207810374"})
        self.assertEqual(status, 400)
        self.assertIn("Paste the reviews", out["error"])
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
