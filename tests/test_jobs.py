"""Background analysis: the page gets an id at once and asks for the result, so a dropped connection loses nothing. No network, no model."""
import json
import threading
import time
import unittest

from bedahreview import web
from bedahreview.classifier import ClassificationError

try:
    from test_robustness import Base, result
except ImportError:  # run as tests.<module>
    from tests.test_robustness import Base, result


def job_body(text="bagus"):
    return json.dumps({"text": text, "async": True}).encode()


JSON = {"Content-Type": "application/json"}


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.jobs = web.Jobs(ttl=600, limit=3, clock=lambda: self.now[0])

    def test_a_job_runs_then_finishes_and_keeps_its_reply(self):
        key, job = self.jobs.create("reading")
        job.progress(1, 4)
        self.assertEqual(self.jobs.get(key), {"state": "running", "step": "reading", "done": 1, "total": 4})
        self.jobs.finish(job, 200, {"items": []})
        self.assertEqual(self.jobs.get(key), {"state": "done", "status": 200, "payload": {"items": []}})
        self.assertEqual(self.jobs.get(key)["state"], "done")  # asking again (a lost reply) gives the same answer

    def test_a_failed_job_keeps_the_status_and_body_of_the_plain_reply(self):
        key, job = self.jobs.create("reading")
        self.jobs.finish(job, 502, {"error": "x", "code": "model_down"})
        self.assertEqual(self.jobs.get(key), {"state": "failed", "status": 502, "payload": {"error": "x", "code": "model_down"}})

    def test_a_job_is_forgotten_after_ten_minutes_and_unknown_ids_are_none(self):
        key, job = self.jobs.create("reading")
        self.jobs.finish(job, 200, {})
        self.now[0] += 601
        self.assertIsNone(self.jobs.get(key))
        self.assertIsNone(self.jobs.get("0" * 32))

    def test_too_many_held_jobs_are_refused_until_old_ones_expire(self):
        for _ in range(3):
            self.assertIsNotNone(self.jobs.create("reading"))
        self.assertIsNone(self.jobs.create("reading"))
        self.now[0] += 601
        self.assertIsNotNone(self.jobs.create("reading"))
        self.assertEqual(self.jobs.size(), 1)


class JobRouteTests(Base):
    def start(self, server, text="bagus"):
        resp, body = self.request(server, "POST", "/api/classify", job_body(text), JSON)
        return resp, json.loads(body)

    def wait(self, server, key, seconds=10):
        end = time.time() + seconds
        while time.time() < end:
            resp, body = self.request(server, "GET", "/api/job/" + key)
            data = json.loads(body)
            if resp.status != 200 or data["state"] != "running":
                return resp, data
            time.sleep(0.02)
        self.fail("the job did not finish")

    def test_the_reply_is_an_id_at_once_and_the_result_follows(self):
        server = self.serve()
        resp, data = self.start(server)
        self.assertEqual(resp.status, 202)
        self.assertRegex(data["job"], r"^[0-9a-f]{32}$")
        resp, done = self.wait(server, data["job"])
        self.assertEqual((resp.status, done["state"], done["status"]), (200, "done", 200))
        self.assertEqual(done["payload"]["counts"]["keep"], 1)
        self.assertEqual(self.wait(server, data["job"])[1]["state"], "done")  # fetching it again works: the phone may have missed the first answer

    def test_the_plain_request_still_works_without_the_async_flag(self):
        server = self.serve()
        resp, body = self.analyse(server)
        self.assertEqual((resp.status, json.loads(body)["counts"]["keep"]), (200, 1))

    def test_progress_is_reported_while_the_model_works(self):
        gate, started = threading.Event(), threading.Event()

        def classify(reviews, product=None, progress=None):
            progress(0, 4)
            progress(2, 4)
            started.set()
            gate.wait(5)
            return [result(r["id"]) for r in reviews]

        server = self.serve(classify_fn=classify)
        _, data = self.start(server)
        self.assertTrue(started.wait(5))
        _, body = self.request(server, "GET", "/api/job/" + data["job"])
        self.assertEqual(json.loads(body), {"state": "running", "step": "reading", "done": 2, "total": 4})
        gate.set()
        self.assertEqual(self.wait(server, data["job"])[1]["state"], "done")

    def test_a_model_failure_arrives_with_the_same_status_and_code_as_a_plain_request(self):
        def broken(reviews, product=None):
            raise ClassificationError("invalid reply after retry")

        server = self.serve(classify_fn=broken)
        _, data = self.start(server)
        _, done = self.wait(server, data["job"])
        self.assertEqual((done["state"], done["status"], done["payload"]["code"]), ("failed", 502, "model_bad"))

    def test_an_unexpected_error_from_the_model_call_is_reported_and_frees_the_slot(self):
        def bug(reviews, product=None):
            raise KeyError("boom")

        server = self.serve(classify_fn=bug, max_concurrent=1)
        _, data = self.start(server)
        _, done = self.wait(server, data["job"])
        self.assertEqual((done["state"], done["status"], done["payload"]["code"]), ("failed", 502, "model_down"))
        for _ in range(50):  # the single slot is free again, so a second analysis starts instead of getting "busy"
            resp, _ = self.start(server)
            if resp.status == 202:
                break
            time.sleep(0.02)
        self.assertEqual(resp.status, 202)

    def test_a_job_holds_its_slot_until_it_ends_and_the_next_one_is_told_busy(self):
        gate = threading.Event()

        def slow(reviews, product=None):
            gate.wait(5)
            return [result(r["id"]) for r in reviews]

        server = self.serve(classify_fn=slow, max_concurrent=1)
        resp, first = self.start(server)
        self.assertEqual(resp.status, 202)
        resp, data = self.start(server)
        self.assertEqual((resp.status, data["limit"]), (503, "busy"))
        gate.set()
        self.assertEqual(self.wait(server, first["job"])[1]["state"], "done")

    def test_unknown_or_malformed_ids_are_not_found(self):
        server = self.serve()
        for path in ("/api/job/" + "a" * 32, "/api/job/short", "/api/job/../../etc/passwd"):
            resp, body = self.request(server, "GET", path)
            self.assertEqual((resp.status, json.loads(body)["code"]), (404, "job_gone"), path)

    def test_a_signed_out_visitor_cannot_start_a_job_in_access_code_mode(self):
        server = self.serve(access_codes={"ani": "open-sesame-1"}, public=True)
        resp, _ = self.request(server, "POST", "/api/classify", job_body(), JSON)
        self.assertEqual(resp.status, 401)
        self.assertEqual(self.request(server, "GET", "/api/job/" + "a" * 32)[0].status, 401)  # nor ask about one

    def test_a_paste_with_no_reviews_fails_in_the_job_with_the_usual_code(self):
        server = self.serve()
        _, data = self.start(server, text="   ")
        _, done = self.wait(server, data["job"])
        self.assertEqual((done["state"], done["status"], done["payload"]["code"]), ("failed", 400, "no_parse"))


if __name__ == "__main__":
    unittest.main()
