import contextlib
import http.client
import io
import json
import re
import threading
import unittest

from bedahreview.classifier import ClassificationError
from bedahreview.fetchers import FetchError
from bedahreview.fetchers.base import Review
from bedahreview.web import MAX_REVIEWS, build_response, fix_groups, make_server


def _result(rid, bucket="fix", theme="Colour vs photo", fake=False):
    return {
        "id": rid, "bucket": bucket, "theme": theme, "variant": None, "cause": "listing", "check_first": False,
        "proof_quote": "warna ijo", "likely_fake": {"flag": fake, "signals": []}, "confidence": "high",
        "fix_suggestion_id": "Ganti foto." if bucket == "fix" else None,
        "fight": None, "stars": None, "text": None,
    }


class WebTests(unittest.TestCase):
    def setUp(self):
        self.seen, self.products = [], []

        def fake_classify(reviews, product=None):
            self.seen.append(reviews)
            self.products.append(product)
            return [_result(r["id"], "fix" if i % 2 == 0 else "keep", fake=False) for i, r in enumerate(reviews)]

        self.fake_classify = fake_classify
        self._start(fake_classify)

    def _start(self, fn):
        if getattr(self, "server", None):
            self.server.shutdown(); self.server.server_close()
        self.server = make_server(0, classify_fn=fn)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def post(self, text, **kw):
        body = json.dumps({"text": text}).encode()
        headers = {"Content-Type": "application/json"}
        headers.update(kw.get("headers", {}))
        return self.request("POST", "/api/classify", body, headers)

    def test_serves_the_page(self):
        resp, data = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"BedahReview", data)
        self.assertIn(b"manifest.webmanifest", data)
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")

    def test_share_url_serves_the_page_and_the_log_has_no_review_text(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            resp, data = self.request("GET", "/?title=Kaos&text=Pas%20sampe%20koyak%20bajunya")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"BedahReview", data)
        self.assertIn("GET /", err.getvalue())
        self.assertNotIn("koyak", err.getvalue())
        self.assertNotIn("Kaos", err.getvalue())

    def test_pwa_files_are_served_with_the_right_types(self):
        resp, data = self.request("GET", "/manifest.webmanifest")
        self.assertEqual((resp.status, resp.getheader("Content-Type").split(";")[0]), (200, "application/manifest+json"))
        manifest = json.loads(data)
        self.assertEqual((manifest["display"], manifest["theme_color"]), ("standalone", "#0f6b5a"))
        self.assertEqual(manifest["share_target"], {"action": "./", "method": "GET", "params": {"title": "title", "text": "text", "url": "url"}})
        sizes = {(i["sizes"], i.get("purpose")) for i in manifest["icons"]}
        self.assertEqual(sizes, {("192x192", None), ("512x512", None), ("512x512", "maskable")})
        for icon in manifest["icons"]:
            resp, png = self.request("GET", "/" + icon["src"])
            self.assertEqual((resp.status, resp.getheader("Content-Type"), png[:4]), (200, "image/png", b"\x89PNG"), icon["src"])
        resp, js = self.request("GET", "/sw.js")
        self.assertEqual((resp.status, resp.getheader("Content-Type").split(";")[0]), (200, "text/javascript"))

    def test_service_worker_never_touches_the_api(self):
        js = self.request("GET", "/sw.js")[1].decode()
        self.assertIn("/api/", js)  # explicitly skipped
        self.assertIn("startsWith('/api/')", js)
        self.assertNotIn("POST", js.replace("method !== 'GET'", ""))  # only GET requests are ever handled

    def test_static_paths_are_an_allowlist(self):
        for bad in ("/static/index.html", "/icons/../web.py", "/icons/other.png", "/../README.md", "/sw.js/../../web.py"):
            self.assertEqual(self.request("GET", bad)[0].status, 404, bad)

    def test_classifies_pasted_text_and_joins_results(self):
        resp, data = self.post("★★★☆☆  Putih, M   warna ijo\nPas sampe koyak")
        self.assertEqual(resp.status, 200)
        out = json.loads(data)
        self.assertEqual(out["counts"], {"fix": 1, "fight": 0, "check": 0, "ignore": 0, "keep": 1})
        first = out["items"][0]
        self.assertEqual((first["stars"], first["variant"], first["text"]), (3, "Putih, M", "warna ijo"))
        self.assertEqual(self.seen[0][0]["text"], "warna ijo")

    def test_empty_paste_is_rejected_without_calling_the_model(self):
        resp, data = self.post("   \n  ")
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.seen, [])

    def test_too_many_reviews(self):
        resp, data = self.post("\n".join("review %d" % i for i in range(MAX_REVIEWS + 1)))
        self.assertEqual(resp.status, 400)
        self.assertIn("up to %d" % MAX_REVIEWS, json.loads(data)["error"])

    def test_foreign_host_header_is_refused(self):
        resp, _ = self.request("GET", "/", headers={"Host": "evil.example.com"})
        self.assertEqual(resp.status, 403)
        resp, _ = self.post("bagus", headers={"Host": "evil.example.com:8000"})
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.seen, [])

    def test_requires_json_content_type(self):
        resp, _ = self.request("POST", "/api/classify", b'{"text":"bagus"}', {"Content-Type": "text/plain"})
        self.assertEqual(resp.status, 415)
        self.assertEqual(self.seen, [])

    def test_bad_bodies(self):
        h = {"Content-Type": "application/json"}
        self.assertEqual(self.request("POST", "/api/classify", b"not json", h)[0].status, 400)
        self.assertEqual(self.request("POST", "/api/classify", b'{"text": 5}', h)[0].status, 400)
        self.assertEqual(self.request("POST", "/api/classify", b"x" * 300_000, h)[0].status, 413)

    def test_model_failures_become_clean_errors(self):
        def broken(reviews):
            raise ClassificationError("invalid reply after retry:\n- details")

        self._start(broken)
        resp, data = self.post("bagus")
        self.assertEqual(resp.status, 502)
        self.assertIn("could not be used", json.loads(data)["error"])

        def down(reviews):
            raise TimeoutError("no response")

        self._start(down)
        resp, data = self.post("bagus")
        self.assertEqual(resp.status, 502)
        self.assertIn("TimeoutError", json.loads(data)["error"])
        self.assertNotIn("Traceback", data.decode())

    def test_link_box_is_off_by_default(self):
        resp, data = self.request("GET", "/api/config")
        self.assertEqual(json.loads(data)["url_box"], False)
        body = json.dumps({"url": "https://shopee.co.id/p-i.1.2"}).encode()
        resp, data = self.request("POST", "/api/classify", body, {"Content-Type": "application/json"})
        self.assertEqual(resp.status, 400)
        self.assertIn("switched off", json.loads(data)["error"])
        self.assertEqual(self.seen, [])

    def _with_fetcher(self, fetcher):
        self.server.shutdown(); self.server.server_close()
        self.server = make_server(0, classify_fn=self.fake_classify, fetcher=fetcher)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def post_json(self, payload):
        return self.request("POST", "/api/classify", json.dumps(payload).encode(), {"Content-Type": "application/json"})

    def test_link_flow_fetches_then_classifies(self):
        class Fake:
            last_from_cache = True
            calls = []

            def fetch_reviews(self, url, limit, mode="latest"):
                Fake.calls.append((url, limit, mode))
                return [Review(2, "Pas sampe koyak", "Hitam, L", "2026-10-07", 0, None), Review(5, "bagus", None, "", 1, "terima kasih")]

        self._with_fetcher(Fake())
        cfg = json.loads(self.request("GET", "/api/config")[1])
        self.assertEqual((cfg["url_box"], cfg["max_limit"], cfg["default_limit"]), (True, 20, 10))
        resp, data = self.post_json({"url": "https://shopee.co.id/p-i.1.2?x=1", "limit": 99})
        out = json.loads(data)
        self.assertEqual(resp.status, 200)
        self.assertEqual(Fake.calls, [("https://shopee.co.id/p-i.1.2", 20, "complaints")])  # tracking stripped, limit clamped
        self.assertEqual(out["source"], {"kind": "url", "url": "https://shopee.co.id/p-i.1.2", "title": "p", "mode": "complaints", "count": 2, "cached": True})
        self.assertEqual(self.products, ["p"])  # the listing title from the link reaches the classifier
        self.assertEqual([i["text"] for i in out["items"]], ["Pas sampe koyak", "bagus"])
        self.assertEqual(self.seen[0][0], {"id": "1", "stars": 2, "variant": "Hitam, L", "date": "2026-10-07", "text": "Pas sampe koyak"})

    def test_failed_fetch_points_to_paste_and_skips_the_model(self):
        class Down:
            def fetch_reviews(self, url, limit, mode="latest"):
                raise FetchError("No reviews came back.")

        self._with_fetcher(Down())
        resp, data = self.post_json({"url": "https://shopee.co.id/p-i.1.2"})
        out = json.loads(data)
        self.assertEqual((resp.status, out["fallback"]), (502, "paste"))
        self.assertIn("paste the reviews instead", out["error"])
        self.assertEqual(self.seen, [])

        class Crash:
            def fetch_reviews(self, url, limit, mode="latest"):
                raise RuntimeError("boom with details")

        self._with_fetcher(Crash())
        resp, data = self.post_json({"url": "https://shopee.co.id/p-i.1.2"})
        self.assertEqual(resp.status, 502)
        self.assertNotIn("boom", data.decode())

    def test_bad_links_are_rejected_before_any_fetch(self):
        class Never:
            def fetch_reviews(self, url, limit, mode="latest"):
                raise AssertionError("must not be called")

        self._with_fetcher(Never())
        for bad in ("https://tokopedia.com/a/b-i.1.2", "https://shopee.co.id.evil.com/p-i.1.2", "javascript:alert(1)"):
            self.assertEqual(self.post_json({"url": bad})[0].status, 400, bad)
        self.assertEqual(self.post_json({"url": "https://shopee.co.id/p-i.1.2", "limit": "lots"})[0].status, 400)

    def test_odd_limits_and_modes_are_clean_400s(self):
        class Never:
            def fetch_reviews(self, url, limit, mode="latest"):
                raise AssertionError("must not be called")

        self._with_fetcher(Never())
        link = "https://shopee.co.id/p-i.1.2"
        for body in ({"limit": float("inf")}, {"limit": float("nan")}, {"limit": None}, {"limit": [1]}, {"mode": ["latest"]}, {"mode": "all"}, {"mode": 5}):
            resp, data = self.post_json(dict(body, url=link))
            self.assertEqual(resp.status, 400, body)
            self.assertIn("error", json.loads(data))

    def test_unknown_paths(self):
        self.assertEqual(self.request("GET", "/nope")[0].status, 404)
        self.assertEqual(self.request("POST", "/nope", b"{}", {"Content-Type": "application/json"})[0].status, 404)

    def test_product_name_reaches_the_model_and_the_response(self):
        resp, data = self.post_json({"text": "Pas sampe koyak", "product": "  Kaos   Boxy Retro "})
        out = json.loads(data)
        self.assertEqual(self.products, ["Kaos Boxy Retro"])
        self.assertEqual(out["source"], {"kind": "paste", "title": "Kaos Boxy Retro"})
        self.assertEqual(self.post_json({"text": "x", "product": 5})[0].status, 400)

    def test_build_response_counts_fakes(self):
        reviews = [{"id": "1", "stars": 5, "variant": "A, B", "text": "x"}, {"id": "2", "stars": None, "variant": None, "text": "y"}]
        out = build_response(reviews, [_result("1", "fight", fake=True), _result("2", "check")], "m")
        self.assertEqual(out["likely_fake"], 1)
        self.assertEqual(out["counts"]["check"], 1)
        self.assertEqual(out["items"][0]["variant"], "A, B")


def _item(rid, bucket, theme="Colour vs photo", variant="Putih, L", fake=False, stars=3):
    return dict(_result(rid, bucket, theme, fake), variant=variant), {"id": rid, "stars": stars, "variant": variant, "text": "t" + rid if theme != "No text" else ""}


class TriageBoardTests(unittest.TestCase):
    def board(self, specs):
        pairs = [_item(*spec[:1], **spec[1]) for spec in specs]
        return build_response([r for _, r in pairs], [i for i, _ in pairs], "m")

    def test_fix_cards_group_by_theme_and_base_variant_with_counts(self):
        out = self.board([
            ("1", dict(bucket="fix", variant="Putih, L", stars=1)),
            ("2", dict(bucket="fix", variant="Putih, M", stars=3)),
            ("3", dict(bucket="fix", theme="Packaging / damaged on arrival", variant="Putih, L", stars=4)),
            ("4", dict(bucket="fight", variant="Hitam, L", stars=2)),
        ])
        self.assertEqual(out["complaints"], 4)
        first, second = out["groups"]
        self.assertEqual((first["theme"], first["variant"], first["ids"], first["n"], first["of"]), ("Colour vs photo", "Putih", ["1", "2"], 2, 4))
        self.assertEqual((second["theme"], second["ids"], second["n"]), ("Packaging / damaged on arrival", ["3"], 1))
        self.assertEqual(out["star_avg"], 2.5)
        self.assertEqual(out["reviews"], 4)

    def test_likely_fake_reviews_are_left_out_of_counts_and_cards(self):
        out = self.board([
            ("1", dict(bucket="fix")),
            ("2", dict(bucket="fight", fake=True, stars=5)),
            ("3", dict(bucket="keep", fake=True, stars=5)),
        ])
        self.assertEqual(out["counts"], {"fix": 1, "fight": 0, "check": 0, "ignore": 0, "keep": 0})
        self.assertEqual((out["likely_fake"], out["complaints"], len(out["items"])), (2, 1, 3))  # still returned, for the Fight it tab
        self.assertEqual([g["ids"] for g in out["groups"]], [["1"]])

    def test_star_only_reviews_are_counted_under_ignore(self):
        out = self.board([("1", dict(bucket="ignore", theme="No text", variant=None, stars=1)), ("2", dict(bucket="ignore", theme="No text", variant=None, stars=2)), ("3", dict(bucket="fix"))])
        self.assertEqual((out["counts"]["ignore"], out["no_text"], out["complaints"]), (2, 2, 1))

    def test_dropped_reviews_are_reported(self):
        pairs = [_item("1", "fix")[1], _item("2", "fix")[1]]
        out = build_response(pairs, [_item("1", "fix")[0]], "m")
        self.assertEqual((out["dropped"], len(out["items"])), (1, 1))

    def test_fix_groups_ignore_non_fix_reviews(self):
        items = [_item("1", "keep")[0], _item("2", "fix", variant=None)[0]]
        self.assertEqual(fix_groups(items, 1), [{"theme": "Colour vs photo", "variant": None, "ids": ["2"], "n": 1, "of": 1}])


if __name__ == "__main__":
    unittest.main()


class PublicModeTests(unittest.TestCase):
    CODE = "open-sesame-1"

    def setUp(self):
        self.calls = []

        def fake_classify(reviews, product=None):
            self.calls.append(reviews)
            return [_result(r["id"]) for r in reviews]

        self.server = make_server(0, classify_fn=fake_classify, access_code=self.CODE, public=True, max_per_hour=2)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def login(self, code, nxt="/"):
        page, html_ = self.request("GET", "/")  # the sign-in page hands out the anti-forgery token (a cookie and a hidden field)
        token = re.search(rb'name="csrf" value="([^"]+)"', html_).group(1).decode()
        body = ("code=%s&next=%s&csrf=%s" % (code, nxt, token)).encode()
        return self.request("POST", "/login", body, {"Content-Type": "application/x-www-form-urlencoded", "Cookie": page.getheader("Set-Cookie").split(";")[0]})

    def cookie(self):
        resp, _ = self.login(self.CODE)
        return resp.getheader("Set-Cookie").split(";")[0]

    def classify(self, cookie=None):
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        return self.request("POST", "/api/classify", json.dumps({"text": "warna ijo"}).encode(), headers)

    def test_any_host_is_allowed_in_public_mode(self):
        resp, _ = self.request("GET", "/manifest.webmanifest", headers={"Host": "bedahreview-abc.a.run.app"})
        self.assertEqual(resp.status, 200)

    def test_page_and_api_need_the_code(self):
        resp, body = self.request("GET", "/")
        self.assertEqual(resp.status, 401)  # not 200: the service worker must not cache the sign-in page as the app
        self.assertIn(b'name="code"', body)
        self.assertEqual(self.request("GET", "/api/config")[0].status, 401)
        resp, _ = self.classify()
        self.assertEqual(resp.status, 401)
        self.assertEqual(self.calls, [])

    def test_install_files_stay_public(self):
        for path in ("/manifest.webmanifest", "/sw.js", "/icons/icon-192.png"):
            self.assertEqual(self.request("GET", path)[0].status, 200, path)

    def test_wrong_code_is_refused_and_locks_after_five_tries(self):
        for _ in range(5):
            resp, _ = self.login("nope")
            self.assertEqual(resp.status, 401)
            self.assertFalse([h for h in resp.msg.get_all("Set-Cookie") or [] if h.startswith("br_session=")])  # only the anti-forgery cookie, never a sign-in
        resp, _ = self.login(self.CODE)  # even the right code waits out the lock
        self.assertEqual(resp.status, 429)

    def test_right_code_sets_a_secure_cookie_and_opens_the_app(self):
        resp, _ = self.login(self.CODE, "/?text=halo")
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.getheader("Location"), "/?text=halo")
        cookie = resp.getheader("Set-Cookie")
        for flag in ("HttpOnly", "Secure", "SameSite=Lax"):
            self.assertIn(flag, cookie)
        self.assertNotIn(self.CODE, cookie)  # the cookie is a derived token, not the code
        headers = {"Cookie": cookie.split(";")[0]}
        self.assertEqual(self.request("GET", "/", headers=headers)[0].status, 200)
        self.assertEqual(self.classify(cookie.split(";")[0])[0].status, 200)

    def test_login_cannot_redirect_off_site(self):
        for nxt in ("//evil.example", "https://evil.example", "/\\evil"):
            resp, _ = self.login(self.CODE, nxt)
            self.assertEqual(resp.getheader("Location"), "/", nxt)

    def test_analyses_per_hour_are_capped(self):
        cookie = self.cookie()
        self.assertEqual(self.classify(cookie)[0].status, 200)
        self.assertEqual(self.classify(cookie)[0].status, 200)
        self.assertEqual(self.classify(cookie)[0].status, 429)
        self.assertEqual(len(self.calls), 2)  # the third never reached the model

    def test_local_mode_has_no_login(self):
        server = make_server(0, classify_fn=lambda reviews, product=None: [])
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/")
            self.assertEqual(conn.getresponse().status, 200)
            conn.close()
        finally:
            server.shutdown(); server.server_close()
