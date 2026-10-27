import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from bedahreview import competitors
from bedahreview.competitors import pick_comparable, plain_query, plan_search, suggest_competitors
from bedahreview.fetchers import FetchError
from bedahreview.fetchers.platforms import LinkRouter
from bedahreview.fetchers.search import (
    LAZADA_SEARCH_ACTOR, TOKOPEDIA_SEARCH_ACTOR, Candidate, lazada_candidate, own_key, search_products, shortlist, tokopedia_candidate,
)
from bedahreview.web import make_server

from test_platforms import TOKO, FakeActors, _no_resolve

OWN_TOKO = "https://www.tokopedia.com/nayla-hijab/hijab-segiempat-paris-f4b54"
OWN_LAZADA = "https://www.lazada.co.id/products/pdp-i8819714593.html"


def toko_row(shop, name="Kaos Oversize Pria", price=60000):
    return {"productName": name, "productUrl": "https://www.tokopedia.com/%s/%s-1" % (shop, name.lower().replace(" ", "-")),
            "price": price, "rating": 4.7, "soldCount": 250, "shopName": shop.title()}


def cand(shop, title="Kaos", platform="tokopedia", url=None):
    return Candidate(platform, title, url or "https://www.tokopedia.com/%s/x-1" % shop, 50000, 4.8, 100, shop)


class CandidateTests(unittest.TestCase):
    def test_tokopedia_row_becomes_a_listing_with_no_buyer_data(self):
        c = tokopedia_candidate(dict(toko_row("ty-baju-kaos"), buyer_name="Siti"))
        self.assertEqual((c.platform, c.price, c.rating, c.sold, c.shop), ("tokopedia", 60000, 4.7, 250, "Ty-Baju-Kaos"))
        self.assertNotIn("Siti", repr(c))

    def test_rows_without_a_link_or_title_are_dropped(self):
        self.assertIsNone(tokopedia_candidate({"productName": "x"}))
        self.assertIsNone(lazada_candidate({"type": "notice", "notice": "Run summary"}))

    def test_lazada_product_row(self):
        c = lazada_candidate({"type": "product", "title": "Kaos", "url": OWN_LAZADA, "price": 57999, "rating": 4.35, "soldQuantity": 351, "sellerName": "SilkSphere67"})
        self.assertEqual((c.platform, c.sold, c.shop), ("lazada", 351, "SilkSphere67"))

    def test_a_zero_or_odd_number_is_unknown_not_zero(self):
        c = tokopedia_candidate(dict(toko_row("a"), price=0, rating=None, soldCount=True))
        self.assertEqual((c.price, c.rating, c.sold), (None, None, None))


class ShortlistTests(unittest.TestCase):
    def test_own_key(self):
        self.assertEqual(own_key("tokopedia", OWN_TOKO), "nayla-hijab")
        self.assertEqual(own_key("lazada", OWN_LAZADA), "8819714593")
        self.assertEqual(own_key("lazada", "https://www.lazada.co.id/products/pdp-i5558538744-s13072104368.html"), "5558538744")

    def test_own_shop_and_repeats_from_one_shop_are_dropped(self):
        found = [cand("nayla-hijab"), cand("ty-baju-kaos", "a"), cand("ty-baju-kaos", "b"), cand("kaibo", "c")]
        kept = shortlist(found, "tokopedia", OWN_TOKO)
        self.assertEqual([c.title for c in kept], ["a", "c"])

    def test_own_lazada_listing_is_dropped_by_item_number(self):
        own = cand("x", platform="lazada", url=OWN_LAZADA)
        other = cand("y", platform="lazada", url="https://www.lazada.co.id/products/pdp-i1.html")
        self.assertEqual(shortlist([own, other], "lazada", OWN_LAZADA), [other])


class SearchProductsTests(unittest.TestCase):
    class Actors:
        def __init__(self, rows):
            self.rows, self.calls = rows, []

        def run(self, actor_id, run_input):
            self.calls.append((actor_id, run_input))
            return self.rows

    def test_tokopedia_search_asks_for_the_best_reviewed(self):
        actors = self.Actors([toko_row("a"), {"productName": "no link"}])
        out = search_products(actors, "tokopedia", "  kaos   oversize ")
        self.assertEqual(len(out), 1)
        actor, run_input = actors.calls[0]
        self.assertEqual(actor, TOKOPEDIA_SEARCH_ACTOR)
        self.assertEqual((run_input["keywords"], run_input["sortBy"]), (["kaos oversize"], "reviews"))

    def test_lazada_search_skips_the_free_notice_row(self):
        actors = self.Actors([{"type": "product", "title": "Kaos", "url": OWN_LAZADA}, {"type": "notice", "notice": "x"}])
        self.assertEqual(len(search_products(actors, "lazada", "kaos")), 1)
        self.assertEqual(actors.calls[0][0], LAZADA_SEARCH_ACTOR)

    def test_tiktok_shop_and_empty_queries_are_refused_before_any_paid_run(self):
        actors = self.Actors([])
        for platform, query in (("tiktokshop", "kaos"), ("tokopedia", "   ")):
            with self.assertRaises(FetchError):
                search_products(actors, platform, query)
        self.assertEqual(actors.calls, [])


class ModelStepTests(unittest.TestCase):
    def reply(self, text):
        return mock.patch.object(competitors, "_call", return_value=text)

    def test_plan_search_reads_the_models_phrase(self):
        with self.reply('{"product_type": "kaos oversize pria", "query": "kaos oversize cotton combed"}'):
            self.assertEqual(plan_search(None, "m", "KAOS | murah"), ("kaos oversize pria", "kaos oversize cotton combed"))

    def test_plan_search_falls_back_to_the_first_words_of_the_title(self):
        title = "Kaos Oversize Pria Cotton Combed 30s Lengan Pendek Premium | COD"
        with self.reply("sorry"):
            self.assertEqual(plan_search(None, "m", title), ("", "Kaos Oversize Pria Cotton Combed 30s"))
        self.assertEqual(plain_query(title), "Kaos Oversize Pria Cotton Combed 30s")

    def test_picks_are_checked_deduped_and_capped_at_three(self):
        found = [cand(s) for s in "abcdef"]
        with self.reply('{"picks": [4, 4, 9, "x", true, 0, 2, 1]}'):
            self.assertEqual(pick_comparable(None, "m", "own", found), [4, 0, 2])

    def test_an_empty_pick_list_means_nothing_is_comparable(self):
        with self.reply('{"picks": []}'):
            self.assertEqual(pick_comparable(None, "m", "own", [cand("a")]), [])

    def test_an_unreadable_pick_reply_falls_back_to_search_order(self):
        with self.reply("not json"):
            self.assertEqual(pick_comparable(None, "m", "own", [cand(s) for s in "abcd"]), [0, 1, 2])

    def test_no_candidates_costs_no_model_call(self):
        with mock.patch.object(competitors, "_call", side_effect=AssertionError("no call expected")):
            self.assertEqual(pick_comparable(None, "m", "own", []), [])


class SuggestTests(unittest.TestCase):
    def test_picked_cards_come_first_and_the_rest_can_be_swapped_in(self):
        found = [cand("nayla-hijab", "mine"), cand("a", "A"), cand("b", "B"), cand("c", "C")]
        searched = []

        def search(platform, query):
            searched.append((platform, query))
            return found

        replies = iter(['{"product_type": "hijab", "query": "hijab segiempat"}', '{"picks": [1]}'])  # index 1 = "B" once the own shop is dropped
        with mock.patch.object(competitors, "_call", side_effect=lambda *a, **k: next(replies)):
            out = suggest_competitors(search, "tokopedia", OWN_TOKO, "Hijab Segiempat Paris", client=object(), model="m")
        self.assertEqual(searched, [("tokopedia", "hijab segiempat")])
        self.assertEqual([(c["title"], c["picked"]) for c in out["candidates"]], [("B", True), ("A", False), ("C", False)])
        self.assertEqual((out["picked"], out["product_type"], out["platform"]), (1, "hijab", "tokopedia"))


class SuggestEndpointTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.client = FakeActors([])
        self.router = LinkRouter("t", 0.05, Path(tempfile.mkdtemp()), client=self.client, resolver=_no_resolve)

        def suggest(search, platform, url, title):
            self.calls.append((platform, url, title))
            if title == "boom":
                raise FetchError("The fetch service took too long.")
            if title == "crash":
                raise RuntimeError("secret detail")
            return {"platform": platform, "candidates": [], "picked": 0}

        self.server = make_server(0, suggest_fn=suggest, fetcher=self.router)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, payload, port=None):
        conn = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=10)
        conn.request("POST", "/api/competitors/suggest", json.dumps(payload).encode(), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def test_a_tokopedia_link_reaches_the_suggester_with_the_clean_link(self):
        status, out = self.post({"url": TOKO + "?utm=1", "title": "  Hijab   Segiempat "})
        self.assertEqual(status, 200)
        self.assertEqual(self.calls, [("tokopedia", TOKO, "Hijab Segiempat")])
        self.assertEqual(self.client.calls, [])  # the fake suggester did the work: the endpoint itself runs nothing paid

    def test_tiktok_shop_gets_the_paste_fallback_without_a_paid_run(self):
        status, out = self.post({"url": "https://shop-id.tokopedia.com/view/product/1736089506777433672", "title": "Kaos"})
        self.assertEqual((status, out.get("fallback")), (400, "paste"))
        self.assertEqual(self.calls, [])

    def test_bad_links_and_missing_titles_are_clean_400s(self):
        self.assertEqual(self.post({"url": "https://shopee.co.id/Kaos-i.450430857.27207810374", "title": "Kaos"})[0], 400)
        self.assertEqual(self.post({"url": TOKO, "title": "  "})[0], 400)
        self.assertEqual(self.post({"url": 5, "title": "x"})[0], 400)
        self.assertEqual(self.calls, [])

    def test_a_failed_search_offers_pasting_and_leaks_no_detail(self):
        status, out = self.post({"url": TOKO, "title": "boom"})
        self.assertEqual((status, out["fallback"]), (502, "paste"))
        status, out = self.post({"url": TOKO, "title": "crash"})
        self.assertEqual((status, out["fallback"]), (502, "paste"))
        self.assertNotIn("secret", json.dumps(out))

    def test_without_the_link_box_it_says_so(self):
        server = make_server(0, suggest_fn=lambda *a: {})
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            status, out = self.post({"url": TOKO, "title": "Kaos"}, port)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual((status, out["fallback"]), (400, "paste"))


if __name__ == "__main__":
    unittest.main()
