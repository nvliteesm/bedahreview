import contextlib
import http.client
import io
import json
import socket
import threading
import time
import unittest

from bedahreview.web import make_server

CODE = "open-sesame-1"


def result(rid):
    return {"id": rid, "bucket": "keep", "theme": "Praise: quality / material", "variant": None, "cause": "none", "check_first": False, "proof_quote": None,
            "likely_fake": {"flag": False, "signals": []}, "fix_suggestion_id": None, "fight": None, "confidence": "high"}


class Base(unittest.TestCase):
    def serve(self, **kwargs):
        kwargs.setdefault("classify_fn", lambda reviews, product=None: [result(r["id"]) for r in reviews])
        server = make_server(0, **kwargs)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def request(self, server, method, path, body=None, headers=None, timeout=10):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=timeout)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def analyse(self, server, text="bagus", timeout=10):
        return self.request(server, "POST", "/api/classify", json.dumps({"text": text}).encode(), {"Content-Type": "application/json"}, timeout)


class RoutesTests(Base):
    def test_healthz_is_public_and_says_nothing_else(self):
        server = self.serve(access_codes={"ani": CODE}, public=True)
        resp, body = self.request(server, "GET", "/healthz")
        self.assertEqual((resp.status, json.loads(body)), (200, {"ok": True}))  # no sign-in needed, unlike every other page
        self.assertEqual(self.request(server, "GET", "/")[0].status, 401)
        resp, body = self.request(server, "HEAD", "/healthz")
        self.assertEqual((resp.status, body), (200, b""))

    def test_head_matches_get_without_a_body(self):
        server = self.serve()
        get, page = self.request(server, "GET", "/")
        head, empty = self.request(server, "HEAD", "/")
        self.assertEqual((head.status, empty), (200, b""))
        self.assertEqual(head.getheader("Content-Length"), str(len(page)))
        self.assertEqual(head.getheader("Content-Type"), get.getheader("Content-Type"))
        self.assertEqual(self.request(server, "HEAD", "/api/config")[0].status, 200)
        self.assertEqual(self.request(server, "HEAD", "/nope")[0].status, 404)

    def test_head_does_not_skip_the_sign_in(self):
        server = self.serve(access_codes={"ani": CODE}, public=True)
        resp, body = self.request(server, "HEAD", "/")
        self.assertEqual((resp.status, body), (401, b""))

    def test_other_methods_get_a_clean_405(self):
        server = self.serve()
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            resp, body = self.request(server, method, "/api/classify")
            self.assertEqual(resp.status, 405, method)
            self.assertIn("POST", resp.getheader("Allow"))
            self.assertEqual(json.loads(body)["error"], "Method not allowed")


class FailureTests(Base):
    def test_a_bug_becomes_a_plain_500_that_never_echoes_the_request(self):
        server = self.serve(classify_fn=lambda reviews, product=None: [result("not-an-id")])  # an id we never sent: build_response fails
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            resp, body = self.analyse(server, "bagusbanget rahasia")
        self.assertEqual(resp.status, 500)
        self.assertNotIn("rahasia", body.decode())
        self.assertNotIn("rahasia", err.getvalue())  # the log names the error type only
        self.assertIn("KeyError", err.getvalue())
        self.assertEqual(self.analyse(server)[0].status, 500)  # and the server carries on

    def test_an_oversized_upload_always_gets_its_413(self):
        server = self.serve()
        for _ in range(15):  # this used to be answered too early and sometimes lost to a connection reset
            resp, _ = self.request(server, "POST", "/api/classify", b"x" * 1_500_000, {"Content-Type": "application/json"})
            self.assertEqual(resp.status, 413)

    def test_a_wrong_content_type_is_refused_cleanly_even_with_a_body(self):
        server = self.serve()
        resp, _ = self.request(server, "POST", "/api/classify", b"x" * 400_000, {"Content-Type": "text/plain"})
        self.assertEqual(resp.status, 415)


class LimitTests(Base):
    def test_a_silent_visitor_is_dropped(self):
        server = self.serve(read_timeout=0.5)
        with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as sock:
            sock.sendall(b"POST /api/classify HTTP/1.1\r\nHost: localhost\r\nContent-Length: 50\r\n")  # never finishes the headers
            started = time.time()
            self.assertEqual(sock.recv(1024), b"")  # the server closed the connection
            self.assertLess(time.time() - started, 4)

    def test_a_visitor_who_stalls_mid_upload_is_dropped(self):
        server = self.serve(read_timeout=0.5)
        with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as sock:
            sock.sendall(b"POST /api/classify HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 500\r\n\r\n{\"text\": ")
            started = time.time()
            data = b""
            try:
                while True:
                    chunk = sock.recv(1024)
                    if not chunk:
                        break
                    data += chunk
            except ConnectionError:
                pass
            self.assertLess(time.time() - started, 4)
            self.assertNotIn(b" 200 ", data.split(b"\r\n")[0])

    def test_only_so_many_analyses_run_at_once_and_health_checks_still_pass(self):
        entered, release = threading.Event(), threading.Event()

        def slow(reviews, product=None):
            entered.set()
            release.wait(10)
            return [result(r["id"]) for r in reviews]

        server = self.serve(classify_fn=slow, max_concurrent=1)
        first = {}
        worker = threading.Thread(target=lambda: first.update(zip(("resp", "body"), self.analyse(server, timeout=15))))
        worker.start()
        self.assertTrue(entered.wait(5))
        resp, body = self.analyse(server)  # the one slot is taken
        self.assertEqual((resp.status, json.loads(body)["limit"], resp.getheader("Retry-After")), (503, "busy", "5"))
        self.assertEqual(self.request(server, "GET", "/healthz")[0].status, 200)
        self.assertEqual(self.request(server, "GET", "/api/config")[0].status, 200)  # plain pages are not held up
        release.set()
        worker.join(10)
        self.assertEqual(first["resp"].status, 200)
        self.assertEqual(self.analyse(server)[0].status, 200)  # the slot came back

    def test_a_failed_analysis_gives_its_slot_back(self):
        calls = []

        def boom(reviews, product=None):
            calls.append(1)
            raise RuntimeError("model down")

        server = self.serve(classify_fn=boom, max_concurrent=1)
        for _ in range(3):
            self.assertEqual(self.analyse(server)[0].status, 502)
        self.assertEqual(len(calls), 3)

    def test_too_many_open_connections_get_an_immediate_503(self):
        server = self.serve(max_connections=2)
        idle = [socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) for _ in range(2)]
        try:
            time.sleep(0.3)  # let the server pick both up
            resp, body = self.request(server, "GET", "/healthz")
            self.assertEqual((resp.status, resp.getheader("Retry-After")), (503, "5"))
            self.assertEqual(json.loads(body)["error"], "Server is busy.")
        finally:
            for sock in idle:
                sock.close()
        time.sleep(0.5)
        self.assertEqual(self.request(server, "GET", "/healthz")[0].status, 200)  # the slots came back


if __name__ == "__main__":
    unittest.main()
