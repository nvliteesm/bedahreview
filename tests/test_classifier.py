import copy
import json
import re
import unittest
from types import SimpleNamespace

from bedahreview import classifier
from bedahreview.classifier import ClassificationError, classify, validate_results


def _example():
    blocks = re.findall(r"```\n(.*?)\n```", classifier.PROMPT_PATH.read_text(encoding="utf-8"), re.S)
    return json.loads(blocks[0]), json.loads(blocks[1])


class StubClient:
    """Mimics genai.Client: queued replies in order, recording the calls."""

    def __init__(self, *replies, finish_reason="STOP", no_candidates=False):
        self.replies, self.calls = list(replies), []
        self.finish_reason, self.no_candidates = finish_reason, no_candidates
        self.models = self

    def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": copy.deepcopy(contents), "config": config})
        text = self.replies.pop(0)
        if self.no_candidates:
            return SimpleNamespace(candidates=[], prompt_feedback="blocked")
        part = SimpleNamespace(text=text)
        cand = SimpleNamespace(content=SimpleNamespace(parts=[part]), finish_reason=SimpleNamespace(name=self.finish_reason))
        return SimpleNamespace(candidates=[cand], prompt_feedback=None)


def _sent_text(call, index=0):
    return call["contents"][index].parts[0].text


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.reviews, self.good = _example()

    def test_prompt_example_passes_validation(self):
        self.assertEqual(validate_results(self.reviews, self.good), [])

    def test_product_title_is_sent_only_when_known(self):
        client = StubClient(json.dumps(self.good), json.dumps(self.good))
        classify(self.reviews, client=client)
        classify(self.reviews, client=client, product="Kaos Boxy Retro")
        self.assertFalse(_sent_text(client.calls[0]).startswith("Product listing title"))
        self.assertTrue(_sent_text(client.calls[1]).startswith("Product listing title: Kaos Boxy Retro\nReviews:\n["))

    def test_valid_reply_returned_in_order(self):
        client = StubClient(json.dumps(self.good))
        out = classify(self.reviews, client=client, model="m")
        self.assertEqual([r["id"] for r in out], ["33", "34", "31", "99"])
        config = client.calls[0]["config"]
        self.assertEqual(config.system_instruction, classifier.build_system_prompt())
        self.assertIn("Colour vs photo", config.system_instruction)  # theme enum reaches the model
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertEqual(json.loads(_sent_text(client.calls[0])), self.reviews)

    def test_markdown_fences_are_tolerated(self):
        client = StubClient("```json\n" + json.dumps(self.good) + "\n```")
        self.assertEqual(len(classify(self.reviews, client=client)), 4)

    def test_retry_fixes_a_bad_quote(self):
        bad = copy.deepcopy(self.good)
        bad[0]["proof_quote"] = "paraphrased, not in the text"
        client = StubClient(json.dumps(bad), json.dumps(self.good))
        self.assertEqual(classify(self.reviews, client=client)[0]["proof_quote"], self.good[0]["proof_quote"])
        self.assertEqual(len(client.calls), 2)
        retry = client.calls[1]["contents"]
        self.assertEqual([c.role for c in retry], ["user", "model", "user"])
        self.assertIn("proof_quote", _sent_text(client.calls[1], -1))

    def test_gives_up_after_one_retry(self):
        client = StubClient("not json", "still not json")
        with self.assertRaises(ClassificationError):
            classify(self.reviews, client=client)
        self.assertEqual(len(client.calls), 2)

    def test_fight_without_fight_object_is_flagged(self):
        bad = copy.deepcopy(self.good)
        bad[1]["fight"] = None
        self.assertTrue(any("fight" in p for p in validate_results(self.reviews, bad)))

    def test_wrong_order_and_count_are_flagged(self):
        self.assertTrue(validate_results(self.reviews, self.good[:3]))
        self.assertTrue(any("order" in p for p in validate_results(self.reviews, self.good[::-1])))

    def test_fake_flag_must_mean_fight_with_report_text(self):
        bad = copy.deepcopy(self.good)
        bad[2]["likely_fake"] = {"flag": True, "signals": ["translated-sounding"]}  # keep + fake
        self.assertTrue(any("must be fight" in p for p in validate_results(self.reviews, bad)))
        bad[2].update(bucket="fight", fight=dict(self.good[1]["fight"], report_text=None))
        self.assertTrue(any("report_text" in p for p in validate_results(self.reviews, bad)))
        bad[2]["fight"]["report_text"] = "Ulasan ini tampaknya tidak asli."
        self.assertEqual([p for p in validate_results(self.reviews, bad) if "review 31" in p], [])

    def test_check_first_only_for_courier_or_unclear(self):
        bad = copy.deepcopy(self.good)
        bad[0]["check_first"] = True  # cause is listing
        self.assertTrue(any("check_first" in p for p in validate_results(self.reviews, bad)))
        bad[0]["cause"] = "unclear"
        self.assertEqual([p for p in validate_results(self.reviews, bad) if "check_first" in p], [])

    def test_fix_hint_rules(self):
        check = copy.deepcopy(self.good)
        check[1]["bucket"] = "check"  # a set-aside card: reply kit + optional own-side hint
        check[1]["fix_suggestion_id"] = "Cek kembali deskripsi dan stok produk."
        self.assertEqual(validate_results(self.reviews, check), [])
        fight_with_hint = copy.deepcopy(self.good)
        fight_with_hint[1]["fix_suggestion_id"] = "Cek kembali deskripsi."
        self.assertTrue(any("must be null" in p for p in validate_results(self.reviews, fight_with_hint)))
        keep_with_hint = copy.deepcopy(self.good)
        keep_with_hint[2]["fix_suggestion_id"] = "Ganti foto."
        self.assertTrue(any("must be null" in p for p in validate_results(self.reviews, keep_with_hint)))
        fix_without_hint = copy.deepcopy(self.good)
        fix_without_hint[0]["fix_suggestion_id"] = None
        self.assertTrue(any("must be set" in p for p in validate_results(self.reviews, fix_without_hint)))

    def test_check_needs_the_reply_kit(self):
        bad = copy.deepcopy(self.good)
        bad[1].update(bucket="check", fight=None)
        self.assertTrue(any("reply kit" in p for p in validate_results(self.reviews, bad)))
        bad = copy.deepcopy(self.good)
        bad[2]["bucket"] = "check"  # keep -> check but no reply kit
        self.assertTrue(any("reply kit" in p for p in validate_results(self.reviews, bad)))

    def test_bad_enum_is_flagged(self):
        bad = copy.deepcopy(self.good)
        bad[2]["bucket"] = "praise"
        self.assertTrue(validate_results(self.reviews, bad))

    def test_truncated_reply_raises(self):
        with self.assertRaises(ClassificationError):
            classify(self.reviews, client=StubClient("[", finish_reason="MAX_TOKENS"))

    def test_blocked_or_empty_reply_raises(self):
        with self.assertRaises(ClassificationError):
            classify(self.reviews, client=StubClient("x", no_candidates=True))
        with self.assertRaises(ClassificationError):
            classify(self.reviews, client=StubClient("x", finish_reason="SAFETY"))

    def test_batches_split_by_batch_size(self):
        many = [dict(self.reviews[2], id=str(i)) for i in range(classifier.BATCH_SIZE + 1)]
        replies = []
        for chunk in (many[: classifier.BATCH_SIZE], many[classifier.BATCH_SIZE :]):
            replies.append(json.dumps([dict(self.good[2], id=r["id"]) for r in chunk]))
        client = StubClient(*replies)
        self.assertEqual(len(classify(many, client=client)), len(many))
        self.assertEqual(len(client.calls), 2)

    def test_transient_error_is_retried(self):
        class Overloaded(Exception):
            code = 503

        class Flaky(StubClient):
            def generate_content(self, model, contents, config):
                if len(self.calls) == 0:
                    self.calls.append("failed")
                    raise Overloaded("high demand")
                return super().generate_content(model, contents, config)

        client = Flaky(json.dumps(self.good))
        original = classifier.RETRY_DELAYS_S
        classifier.RETRY_DELAYS_S = (0, 0)
        try:
            self.assertEqual(len(classify(self.reviews, client=client)), 4)
        finally:
            classifier.RETRY_DELAYS_S = original

    def test_dropped_connections_are_retried(self):
        import httpx

        for error in (ConnectionResetError("Connection reset by peer"), httpx.ReadError("dropped"), httpx.RemoteProtocolError("closed")):
            class Drops(StubClient):
                def generate_content(self, model, contents, config, _error=error):
                    if not self.calls:
                        self.calls.append("failed")
                        raise _error
                    return super().generate_content(model, contents, config)

            client = Drops(json.dumps(self.good))
            original = classifier.RETRY_DELAYS_S
            classifier.RETRY_DELAYS_S = (0, 0)
            try:
                self.assertEqual(len(classify(self.reviews, client=client)), 4, repr(error))
            finally:
                classifier.RETRY_DELAYS_S = original

    def test_non_transient_error_is_not_retried(self):
        class BadKey(Exception):
            code = 403

        class Broken(StubClient):
            def generate_content(self, model, contents, config):
                self.calls.append("x")
                raise BadKey("denied")

        client = Broken()
        with self.assertRaises(BadKey):
            classify(self.reviews, client=client)
        self.assertEqual(len(client.calls), 1)

    def test_hung_request_hits_the_deadline(self):
        import time as _time

        class Hung(StubClient):
            def generate_content(self, model, contents, config):
                self.calls.append("x")
                _time.sleep(3)

        original = (classifier.REQUEST_DEADLINE_S, classifier.RETRY_DELAYS_S)
        classifier.REQUEST_DEADLINE_S, classifier.RETRY_DELAYS_S = 0.2, (0,)
        try:
            client = Hung()
            with self.assertRaises(TimeoutError):
                classify(self.reviews, client=client)
            self.assertEqual(len(client.calls), 2)  # first attempt + one retry
        finally:
            classifier.REQUEST_DEADLINE_S, classifier.RETRY_DELAYS_S = original

    def test_duplicate_ids_rejected_and_empty_input(self):
        with self.assertRaises(ValueError):
            classify([self.reviews[0], self.reviews[0]], client=StubClient())
        self.assertEqual(classify([], client=StubClient()), [])


if __name__ == "__main__":
    unittest.main()
