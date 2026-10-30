import copy
import json
import threading
import time
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
        self.all_reviews, self.all_good = _example()
        # The model only sees reviews with text; the star-only example (id 99) is answered in code.
        self.reviews, self.good = self.all_reviews[:3], self.all_good[:3]

    def test_prompt_example_passes_validation(self):
        self.assertEqual(validate_results(self.all_reviews, self.all_good), [])

    def test_check_first_is_kept_only_for_courier_or_unclear_fights_that_are_not_fake(self):
        def result(**kw):
            base = {"bucket": "fight", "cause": "courier", "check_first": True, "likely_fake": {"flag": False, "signals": []}}
            base.update(kw)
            return base

        self.assertTrue(classifier.tidy_check_first(result())["check_first"])  # transit damage: the flag stays
        self.assertTrue(classifier.tidy_check_first(result(cause="unclear"))["check_first"])
        for why, odd in {"a suspected fake": result(cause="unclear", likely_fake={"flag": True, "signals": ["template"]}), "the check bucket": result(bucket="check", cause="unclear"),
                         "a fix": result(bucket="fix", cause="listing"), "a buyer-caused fight": result(cause="buyer"), "praise": result(bucket="keep", cause="none")}.items():
            self.assertFalse(classifier.tidy_check_first(odd)["check_first"], why)
        untouched = result(check_first=False, cause="unclear")
        self.assertIs(classifier.tidy_check_first(untouched), untouched)

    def test_a_fake_filed_under_unclear_does_not_reach_the_seller_as_a_courier_question(self):
        bad = copy.deepcopy(self.good)
        bad[0].update(bucket="fight", cause="unclear", check_first=True, likely_fake={"flag": True, "signals": ["template"]},
                      fix_suggestion_id=None, fight=dict(copy.deepcopy(self.good[1]["fight"]), report_text="Mohon tinjau ulasan ini, isinya tampak seragam."))
        out = classify(self.reviews, client=StubClient(json.dumps(bad)))
        self.assertEqual([r["check_first"] for r in out], [False, True, False])  # only the real transit complaint (id 34) keeps it

    def test_fight_kit_needs_the_explanation_and_steps_in_both_languages(self):
        kit = self.good[1]["fight"]
        self.assertEqual(len(kit["steps"]), len(kit["steps_id"]))  # the prompt's own example shows the model the same steps in both
        for missing in ("why_id", "steps_id", "why", "steps"):
            broken = copy.deepcopy(self.good)
            del broken[1]["fight"][missing]
            self.assertTrue(validate_results(self.reviews, broken), missing)

    def test_product_title_is_sent_only_when_known(self):
        client = StubClient(json.dumps(self.good), json.dumps(self.good))
        classify(self.reviews, client=client)
        classify(self.reviews, client=client, product="Kaos Boxy Retro")
        self.assertFalse(_sent_text(client.calls[0]).startswith("Product listing title"))
        self.assertTrue(_sent_text(client.calls[1]).startswith("Product listing title: Kaos Boxy Retro\nReviews:\n["))

    def test_valid_reply_returned_in_order(self):
        client = StubClient(json.dumps(self.good))
        out = classify(self.reviews, client=client, model="m")
        self.assertEqual([r["id"] for r in out], ["33", "34", "31"])
        config = client.calls[0]["config"]
        self.assertEqual(config.system_instruction, classifier.build_system_prompt())
        self.assertIn("Colour vs photo", config.system_instruction)  # theme enum reaches the model
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertEqual(json.loads(_sent_text(client.calls[0])), self.reviews)

    def test_markdown_fences_are_tolerated(self):
        client = StubClient("```json\n" + json.dumps(self.good) + "\n```")
        self.assertEqual(len(classify(self.reviews, client=client)), 3)

    def test_retry_fixes_a_bad_quote(self):
        bad = copy.deepcopy(self.good)
        bad[0]["proof_quote"] = "paraphrased, not in the text"
        client = StubClient(json.dumps(bad), json.dumps(self.good))
        self.assertEqual(classify(self.reviews, client=client)[0]["proof_quote"], self.good[0]["proof_quote"])
        self.assertEqual(len(client.calls), 2)
        retry = client.calls[1]["contents"]
        self.assertEqual([c.role for c in retry], ["user", "model", "user"])
        self.assertIn("proof_quote", _sent_text(client.calls[1], -1))

    def test_star_only_reviews_skip_the_model(self):
        client = StubClient(json.dumps(self.good))
        out = classify(self.all_reviews, client=client)
        self.assertEqual([r["id"] for r in out], ["33", "34", "31", "99"])
        self.assertEqual(out[3], self.all_good[3])  # the code-built result equals the prompt's Ignore example
        self.assertEqual([r["id"] for r in json.loads(_sent_text(client.calls[0]))], ["33", "34", "31"])
        # only star-only reviews: no model call at all
        quiet = StubClient()
        out = classify([{"id": "1", "stars": 1, "variant": "Putih, L", "date": "", "text": "  "}], client=quiet)
        self.assertEqual((quiet.calls, out[0]["bucket"], out[0]["theme"], out[0]["variant"]), ([], "ignore", "No text", "Putih, L"))
        self.assertEqual(validate_results([{"id": "1", "text": "  "}], out), [])

    def test_unverifiable_quote_is_dropped_after_the_retry(self):
        bad = copy.deepcopy(self.good)
        bad[1]["proof_quote"] = "paraphrased, not in the text"
        client = StubClient(json.dumps(bad), json.dumps(bad))
        out = classify(self.reviews, client=client)
        self.assertEqual([r["id"] for r in out], ["33", "31"])  # review 34 is left out, not shown with an invented quote
        self.assertEqual(len(client.calls), 2)

    def test_other_problems_still_fail_the_batch(self):
        bad = copy.deepcopy(self.good)
        bad[1]["proof_quote"] = "paraphrased, not in the text"
        bad[0]["bucket"] = "nonsense"
        with self.assertRaises(ClassificationError):
            classify(self.reviews, client=StubClient(json.dumps(bad), json.dumps(bad)))

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
        self.assertTrue(validate_results(self.reviews, self.good[:2]))
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

    def _by_batch_client(self, many, hold=None):
        """Answers each call with results for exactly the ids it was sent, whatever order the calls arrive in."""
        good, state = self.good[2], {"now": 0, "peak": 0}
        lock = threading.Lock()

        class ByBatch(StubClient):
            def generate_content(inner, model, contents, config):
                with lock:
                    state["now"] += 1
                    state["peak"] = max(state["peak"], state["now"])
                if hold:
                    time.sleep(hold)
                text = _sent_text({"contents": contents})
                ids = [r["id"] for r in many if '"id": "%s"' % r["id"] in text or "'id': '%s'" % r["id"] in text or r["id"] in text.split()]
                with lock:
                    state["now"] -= 1
                inner.replies = [json.dumps([dict(good, id=i) for i in ids])]
                return StubClient.generate_content(inner, model, contents, config)
        return ByBatch(), state

    def test_batches_split_by_batch_size(self):
        many = [dict(self.reviews[2], id="r%d" % i) for i in range(classifier.BATCH_SIZE + 1)]
        client, _ = self._by_batch_client(many)
        out = classify(many, client=client)
        self.assertEqual([r["id"] for r in out], [r["id"] for r in many])  # input order survives parallel batches
        self.assertEqual(len(client.calls), 2)

    def test_a_planted_group_is_not_cut_in_two_by_the_batch_boundary(self):
        size = classifier.BATCH_SIZE
        template = "Pengiriman cepat sekali, produk kolagen ini bikin kulit sehat sekali, sangat direkomendasikan %s"
        planted = [{"id": "p%d" % i, "stars": 5, "variant": None, "date": "", "text": template % ("untuk semua")} for i in range(6)]
        others = ["barang bagus bahan tebal warna %d sesuai foto" % i for i in range(size - 3)] + ["kemasan penyok pas sampai", "ukuran kekecilan"]
        reviews = [dict(self.reviews[2], id="o%d" % i, text=t) for i, t in enumerate(others)]
        reviews[size - 5 : size - 5] = planted[:3]  # the group straddles the first batch boundary in plain input order
        reviews += planted[3:]
        batches = classifier.make_batches(reviews)
        homes = {i for i, batch in enumerate(batches) for r in batch if r["id"].startswith("p")}
        self.assertEqual(len(homes), 1)  # all six planted reviews were sent to the model together
        self.assertEqual(sorted(r["id"] for b in batches for r in b), sorted(r["id"] for r in reviews))  # nobody lost or repeated
        self.assertTrue(all(len(b) <= size for b in batches))

    def test_a_group_bigger_than_a_batch_is_split_and_nothing_is_lost(self):
        size = classifier.BATCH_SIZE
        many = [dict(self.reviews[2], id="s%d" % i, text="paket sampai bagus sekali terima kasih") for i in range(size * 2 + 3)]
        batches = classifier.make_batches(many)
        self.assertEqual(sorted(len(b) for b in batches), [3, size, size])
        self.assertEqual(sorted(r["id"] for b in batches for r in b), sorted(r["id"] for r in many))

    def test_reviews_that_differ_are_not_grouped_and_fill_whole_batches(self):
        size = classifier.BATCH_SIZE
        distinct = [dict(self.reviews[2], id="d%d" % i, text="ulasan nomor %d tentang topik %s" % (i, chr(97 + i) * 5)) for i in range(size + 1)]
        batches = classifier.make_batches(distinct)
        self.assertEqual([len(b) for b in batches], [size, 1])  # no wasted calls

    def test_results_keep_input_order_even_when_grouping_moves_reviews_between_batches(self):
        size = classifier.BATCH_SIZE
        good = self.good[2]
        same = "bahannya bagus luamayan tebel paket sampai bagus sekali terima kasih"
        many = [dict(self.reviews[2], id="r%d" % i, text=(same if i % 2 else "bahannya bagus luamayan tebel ulasan unik %d %s" % (i, "x" * i))) for i in range(size + 5)]

        class InSentOrder(StubClient):
            def generate_content(inner, model, contents, config):
                text = _sent_text({"contents": contents})
                sent = json.loads(text[text.index("["):])
                inner.replies = [json.dumps([dict(good, id=r["id"]) for r in sent])]  # answers in the order this batch was sent
                return StubClient.generate_content(inner, model, contents, config)

        out = classify(many, client=InSentOrder())
        self.assertEqual([r["id"] for r in out], [r["id"] for r in many])

    def test_parallel_batches_are_capped(self):
        many = [dict(self.reviews[2], id="r%d" % i) for i in range(classifier.BATCH_SIZE * 5)]
        client, state = self._by_batch_client(many, hold=0.05)
        self.assertEqual(len(classify(many, client=client)), len(many))
        self.assertLessEqual(state["peak"], classifier.MAX_PARALLEL)
        self.assertGreater(state["peak"], 1)  # they really do overlap

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
            self.assertEqual(len(classify(self.reviews, client=client)), 3)
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
                self.assertEqual(len(classify(self.reviews, client=client)), 3, repr(error))
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
