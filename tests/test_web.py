import http.client
import json
import threading
import unittest

from bedahreview.classifier import ClassificationError
from bedahreview.web import MAX_REVIEWS, build_response, make_server


def _result(rid, bucket="fix", theme="Colour vs photo", fake=False):
    return {
        "id": rid, "bucket": bucket, "theme": theme, "variant": None, "cause": "listing", "check_first": False,
        "proof_quote": "warna ijo", "likely_fake": {"flag": fake, "signals": []}, "confidence": "high",
        "fix_suggestion_id": "Ganti foto." if bucket == "fix" else None,
        "fight": None, "stars": None, "text": None,
    }


class WebTests(unittest.TestCase):
    def setUp(self):
        self.seen = []

        def fake_classify(reviews):
            self.seen.append(reviews)
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
        self.assertIn(b"Fix it or fight it", data)
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")

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

    def test_unknown_paths(self):
        self.assertEqual(self.request("GET", "/nope")[0].status, 404)
        self.assertEqual(self.request("POST", "/nope", b"{}", {"Content-Type": "application/json"})[0].status, 404)

    def test_build_response_counts_fakes(self):
        reviews = [{"id": "1", "stars": 5, "variant": "A, B", "text": "x"}, {"id": "2", "stars": None, "variant": None, "text": "y"}]
        out = build_response(reviews, [_result("1", "fight", fake=True), _result("2", "check")], "m")
        self.assertEqual(out["likely_fake"], 1)
        self.assertEqual(out["counts"]["check"], 1)
        self.assertEqual(out["items"][0]["variant"], "A, B")


if __name__ == "__main__":
    unittest.main()
