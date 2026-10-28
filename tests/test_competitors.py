import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from bedahreview import competitors
from bedahreview.competitors import (
    compare_competitors, comparison_rows, fetch_sample, insights, pick_comparable, plain_query, plan_search, suggest_competitors, tally,
)
from bedahreview.fetchers import FetchError, Review
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


def item(theme, bucket="fix", fake=False):
    return {"theme": theme, "bucket": bucket, "fake": fake}


def review(star, text, product=None):
    return Review(star=star, text=text, variant=None, date="", photo_count=0, replied=False, product=product)


def result(rid, theme, bucket, quote, fake=False):
    return {"id": rid, "theme": theme, "bucket": bucket, "proof_quote": quote, "likely_fake": {"flag": fake, "signals": []}}


class TallyTests(unittest.TestCase):
    def test_fakes_no_text_and_other_are_left_out_of_the_counts(self):
        out = tally([item("Sizing & fit"), item("Sizing & fit", "fight"), item("Sizing & fit", fake=True), item("No text", "ignore"),
                     item("Other", "ignore"), item("Praise: taste", "keep"), item("Praise: taste", "keep", fake=True)])
        self.assertEqual(out["complaints"], {"Sizing & fit": 2})
        self.assertEqual(out["praise"], {"Praise: taste": 1})
        self.assertEqual((out["reviews"], out["credible"], out["fake"]), (7, 5, 2))


class FetchSampleTests(unittest.TestCase):
    def test_a_mix_of_worst_and_newest_without_repeats(self):
        calls = []

        def fetch(url, limit, mode):
            calls.append((limit, mode))
            return [review(1, "rusak"), review(1, "rusak")] if mode == "complaints" else [review(5, "bagus"), review(1, "rusak")]

        mixed = fetch_sample(fetch, "u")
        self.assertEqual(sorted(calls), [(3, "latest"), (4, "complaints")])
        self.assertEqual(sorted((r.star, r.text) for r in mixed), [(1, "rusak"), (5, "bagus")])

    def test_one_half_failing_still_gives_a_sample_but_both_failing_raises(self):
        def only_latest(url, limit, mode):
            if mode == "complaints":
                raise FetchError("No low-rated reviews")
            return [review(5, "bagus")]

        self.assertEqual(len(fetch_sample(only_latest, "u")), 1)

        def nothing(url, limit, mode):
            raise FetchError("took too long")

        with self.assertRaises(FetchError):
            fetch_sample(nothing, "u")


    def test_when_both_halves_fail_the_latest_error_is_the_one_reported(self):
        def fetch(url, limit, mode):
            raise FetchError("No low-rated reviews" if mode == "complaints" else "The fetch service failed (ApifyApiError).")

        with self.assertRaises(FetchError) as ctx:
            fetch_sample(fetch, "u")
        self.assertIn("ApifyApiError", str(ctx.exception))


class StartRetryTests(unittest.TestCase):
    class ApifyApiError(Exception):  # the real class is looked up by name, so the fake only needs the same name
        pass

    def client(self, failures, error):
        calls = []

        class Client:
            def actor(self, actor_id):
                class Actor:
                    def call(self, **kwargs):
                        calls.append(1)
                        if len(calls) <= failures:
                            raise error("rejected")
                        return {"status": "SUCCEEDED", "defaultDatasetId": "d"}
                return Actor()

            def dataset(self, dataset_id):
                class Dataset:
                    def iterate_items(self):
                        return iter([{"a": 1}])
                return Dataset()

        return Client(), calls

    def go(self, failures, error):
        from bedahreview.fetchers.platforms import _Actors

        client, calls = self.client(failures, error)
        with mock.patch("bedahreview.fetchers.platforms.START_RETRY_DELAY_S", 0):
            try:
                return _Actors(client, 0.05).run("a", {}), len(calls)
            except FetchError as exc:
                return exc, len(calls)

    def test_a_start_the_api_rejects_is_tried_once_more(self):
        rows, calls = self.go(1, self.ApifyApiError)
        self.assertEqual((rows, calls), ([{"a": 1}], 2))

    def test_two_rejections_give_up_with_a_plain_error(self):
        out, calls = self.go(2, self.ApifyApiError)
        self.assertIsInstance(out, FetchError)
        self.assertEqual(calls, 2)

    def test_anything_else_is_never_retried_so_a_run_is_not_paid_for_twice(self):
        out, calls = self.go(1, ConnectionError)
        self.assertIsInstance(out, FetchError)
        self.assertEqual(calls, 1)


class RunSlotTests(unittest.TestCase):
    def test_no_more_than_the_allowed_runs_are_in_flight_at_once(self):
        import time
        from concurrent.futures import ThreadPoolExecutor
        from bedahreview.fetchers.platforms import MAX_CONCURRENT_RUNS, _Actors

        state = {"now": 0, "peak": 0}
        lock = threading.Lock()

        class Client:
            def actor(self, actor_id):
                class Actor:
                    def call(self, **kwargs):
                        with lock:
                            state["now"] += 1
                            state["peak"] = max(state["peak"], state["now"])
                        time.sleep(0.05)
                        with lock:
                            state["now"] -= 1
                        return {"status": "SUCCEEDED", "defaultDatasetId": "d"}
                return Actor()

            def dataset(self, dataset_id):
                class Dataset:
                    def iterate_items(self):
                        return iter([])
                return Dataset()

        actors = _Actors(Client(), 0.05)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: actors.run("a", {}), range(8)))
        self.assertLessEqual(state["peak"], MAX_CONCURRENT_RUNS)
        self.assertGreater(state["peak"], 1)


class InsightTests(unittest.TestCase):
    def test_rows_and_plain_statements_come_from_the_counts_only(self):
        you = {"complaints": {"Sizing & fit": 4, "Colour vs photo": 1}, "praise": {"Praise: taste": 0}}
        a = {"title": "Toko A", "complaints": {"Sizing & fit": 1, "Fabric & material quality": 3}, "praise": {"Praise: taste": 3}}
        b = {"title": "Toko B", "complaints": {"Fabric & material quality": 2}, "praise": {}}
        rows = comparison_rows(you, [a, b])
        by_theme = {r["theme"]: r for r in rows}
        self.assertEqual(by_theme["Sizing & fit"]["you"], 4)
        self.assertEqual(by_theme["Sizing & fit"]["others"], [1, 0])
        found = {(i["kind"], i["theme"]): i for i in insights(rows, [a, b])}
        self.assertEqual(found[("worse", "Sizing & fit")]["best"], 0)
        self.assertEqual(found[("opportunity", "Fabric & material quality")]["who"], "Toko A")
        self.assertEqual(found[("praise_gap", "Praise: taste")]["them"], 3)
        self.assertNotIn(("worse", "Colour vs photo"), found)  # a gap of one is not worth a statement


class SmallSampleTests(unittest.TestCase):
    def test_a_small_shop_is_shown_but_never_used_for_a_statement(self):
        you = {"complaints": {"Sizing & fit": 4}, "praise": {}}
        small = {"title": "Tiny", "small": True, "complaints": {}, "praise": {}}
        big = {"title": "Big", "small": False, "complaints": {"Sizing & fit": 3}, "praise": {}}
        rows = comparison_rows(you, [small, big])
        self.assertEqual(rows[0]["others"], [0, 3])
        self.assertEqual(insights(rows, [small, big]), [])  # against Big alone the gap is 1, and Tiny's zero must not count as "best"
        self.assertEqual(insights(rows, [small]), [])

    def test_compare_marks_shops_with_fewer_than_five_credible_reviews(self):
        def fetch(url, limit, mode):
            return [review(1, "ukuran kekecilan %d" % i) for i in range(6 if "big" in url else 1)] if mode == "complaints" else []

        def classify_fn(reviews, product=None):
            return [result(r["id"], "Sizing & fit", "fix", "ukuran kekecilan") for r in reviews]

        out = compare_competitors(fetch, classify_fn, [item("Sizing & fit")], "x", [{"url": "https://big"}, {"url": "https://tiny"}])
        self.assertEqual({c["url"]: c["small"] for c in out["competitors"]}, {"https://big": False, "https://tiny": True})


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.fetched = []

        def fetch(url, limit, mode):
            self.fetched.append((url, limit, mode))
            if "bad" in url:
                raise FetchError("The fetch service took too long.")
            return [review(1, "ukuran kekecilan"), review(5, "bahan adem", product="Kaos Rival")]

        def classify_fn(reviews, product=None):
            return [result(r["id"], "Sizing & fit", "fix", "ukuran kekecilan") if "ukuran" in r["text"] else result(r["id"], "Praise: quality / material", "keep", "bahan adem") for r in reviews]

        self.fetch, self.classify_fn = fetch, classify_fn

    def test_counts_quotes_and_a_failed_rival_are_reported_separately(self):
        own = [item("Sizing & fit"), item("Sizing & fit"), item("Praise: quality / material", "keep")]
        out = compare_competitors(self.fetch, self.classify_fn, own, "Kaos Saya", [{"url": "https://ok", "title": ""}, {"url": "https://bad", "title": "Toko Bad"}])
        self.assertEqual(len(out["competitors"]), 1)
        rival = out["competitors"][0]
        self.assertEqual((rival["title"], rival["reviews"], rival["complaints"]), ("Kaos Rival", 2, {"Sizing & fit": 1}))
        self.assertEqual(rival["quotes"]["Sizing & fit"], {"quote": "ukuran kekecilan", "stars": 1})
        self.assertEqual(out["failed"][0]["title"], "Toko Bad")
        self.assertEqual(out["you"]["complaints"], {"Sizing & fit": 2})
        self.assertNotIn("text", json.dumps(out["competitors"]))  # quotes only, never whole reviews

    def test_without_own_reviews_there_is_no_statement_about_you(self):
        out = compare_competitors(self.fetch, self.classify_fn, [], "", [{"url": "https://ok", "title": ""}])
        self.assertTrue(out["you"]["absent"])
        self.assertEqual(out["insights"], [])
        self.assertEqual(out["competitors"][0]["complaints"], {"Sizing & fit": 1})

    def test_every_rival_failing_is_an_error(self):
        with self.assertRaises(FetchError):
            compare_competitors(self.fetch, self.classify_fn, [item("Sizing & fit")], "x", [{"url": "https://bad", "title": ""}])


class CompareEndpointTests(unittest.TestCase):
    def setUp(self):
        self.seen = []
        self.client = FakeActors([])
        self.router = LinkRouter("t", 0.05, Path(tempfile.mkdtemp()), client=self.client, resolver=_no_resolve)

        def compare(fetch, classify_fn, own, title, competitors):
            self.seen.append((own, title, competitors))
            return {"you": {}, "competitors": [], "failed": [], "rows": [], "insights": []}

        self.server = make_server(0, compare_fn=compare, fetcher=self.router)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, payload):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/competitors/compare", json.dumps(payload).encode(), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def test_links_are_cleaned_and_the_sellers_own_product_is_never_a_rival(self):
        own = [{"theme": "Sizing & fit", "bucket": "fix", "fake": False}]
        rival = "https://www.tokopedia.com/toko-b/kaos-b-1"
        status, _ = self.post({"url": TOKO, "title": "Hijab", "own": own, "competitors": [{"url": TOKO + "?x=1"}, {"url": rival + "?utm=1", "title": "Kaos B"}, {"url": rival}]})
        self.assertEqual(status, 200)
        self.assertEqual(self.seen[0][2], [{"url": rival, "title": "Kaos B"}])

    def test_comparing_without_own_reviews_is_allowed(self):
        rival = "https://www.tokopedia.com/toko-b/kaos-b-1"
        for body in ({"own": [], "competitors": [{"url": rival}]}, {"competitors": [{"url": rival}]}):
            self.assertEqual(self.post(body)[0], 200, body)

    def test_bad_requests_are_clean_400s_before_any_paid_run(self):
        own = [{"theme": "Sizing & fit", "bucket": "fix", "fake": False}]
        for body in ({"own": own, "competitors": []}, {"own": own, "competitors": [{"url": TOKO}] * 4},
                     {"own": own, "competitors": [{"url": "https://shopee.co.id/Kaos-i.450430857.27207810374"}]},
                     {"url": TOKO, "own": own, "competitors": [{"url": TOKO}]}):
            self.assertEqual(self.post(body)[0], 400, body)
        self.assertEqual(self.seen, [])


if __name__ == "__main__":
    unittest.main()
