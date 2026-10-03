"""Card L6a-2, step 1: the wire format of `reporter admit` (neutral spec 4.2 and 4.3): strict JSON on both sides, the
bounds of every field, and the 512-byte caps, each at the cap and one past it. Synthetic data only.
"""

import json
import unittest

from quota_balancer.reporter import admit_wire as wire

from tests.l6a2_env import request_bytes

A, M, P, R = "a" * 64, "m" * 32, "p" * 32, "r" * 64


def load(text):
    return text if isinstance(text, bytes) else text.encode("ascii")


class Requests(unittest.TestCase):

    def refuses(self, raw, word):
        with self.assertRaises(wire.WireFailure) as caught:
            wire.parse_request(load(raw))
        self.assertEqual(caught.exception.word, word)

    def test_the_largest_legal_request_is_301_bytes_and_is_accepted(self):
        raw = wire.encode_request(A, M, M, P, "resume", 999_999_999_999)
        self.assertEqual(len(raw), 301)
        parsed = wire.parse_request(raw)
        self.assertEqual((parsed.agent, parsed.mode, parsed.dispatch_bytes), (A, "resume", 999_999_999_999))

    def test_a_request_with_no_parent_carries_the_empty_string_and_is_accepted(self):
        parsed = wire.parse_request(request_bytes(parent=""))
        self.assertEqual(parsed.parent_request_id, "")

    def test_each_bound_is_checked_at_the_limit_and_one_past_it(self):
        ok = [("agent", "a" * 64), ("agent", "0"), ("agent", "a.b_c-d"), ("message_id", "x" * 32),
              ("message_id", "a:b._-9"), ("profile", "p" * 32), ("profile", "ovh-qwen"), ("dispatch", 0),
              ("dispatch", 10 ** 12 - 1)]
        for field, value in ok:
            with self.subTest(field=field, value=value):
                wire.parse_request(request_bytes(**{field if field != "dispatch" else "dispatch": value}))
        bad = [("agent", "a" * 65), ("agent", ""), ("agent", ".a"), ("agent", "-a"), ("agent", "a b"),
               ("agent", "a/b"), ("agent", "a\n"), ("agent", "operator"), ("agent", "Operator"),
               ("message_id", "m" * 33), ("message_id", ""), ("message_id", "a b"), ("profile", "p" * 33),
               ("profile", ""), ("profile", "Ovh"), ("profile", "a_b"), ("dispatch", 10 ** 12), ("dispatch", -1)]
        for field, value in bad:
            with self.subTest(field=field, value=value):
                self.refuses(request_bytes(**{field: value}), wire.OUT_OF_BOUNDS)

    def test_a_parent_id_is_empty_or_within_the_same_bounds_and_never_invented(self):
        self.refuses(request_bytes(parent="m" * 33), wire.OUT_OF_BOUNDS)
        self.refuses(request_bytes(parent="a b"), wire.OUT_OF_BOUNDS)

    def test_the_context_mode_is_fresh_or_resume_in_lower_case_only(self):
        for mode in ("FRESH", "Resume", "summary", "unknown", ""):
            with self.subTest(mode=mode):
                self.refuses(request_bytes(mode=mode), wire.OUT_OF_BOUNDS)

    def test_a_boolean_or_a_float_is_never_a_whole_number(self):
        base = json.loads(request_bytes())
        for path, value in (("v", True), ("v", 1.0), ("dispatch_bytes", True), ("dispatch_bytes", 5.0),
                            ("dispatch_bytes", "5")):
            with self.subTest(path=path, value=value):
                doc = json.loads(json.dumps(base))
                (doc["context"] if path == "dispatch_bytes" else doc)[path] = value
                self.refuses(json.dumps(doc), wire.BAD_SHAPE)
        self.refuses(request_bytes().replace(b'"dispatch_bytes":500', b'"dispatch_bytes":5e2'), wire.BAD_SHAPE)
        self.refuses(request_bytes().replace(b'"dispatch_bytes":500', b'"dispatch_bytes":500.0'), wire.BAD_SHAPE)
        self.refuses(request_bytes().replace(b'"v":1', b'"v":2'), wire.OUT_OF_BOUNDS)

    def test_a_repeated_key_nan_or_trailing_text_is_refused(self):
        good = request_bytes()
        self.refuses(good.replace(b'"v":1', b'"v":1,"v":1'), wire.BAD_SHAPE)
        self.refuses(good.replace(b'"dispatch_bytes":500', b'"dispatch_bytes":NaN'), wire.BAD_SHAPE)
        self.refuses(good.replace(b'"dispatch_bytes":500', b'"dispatch_bytes":Infinity'), wire.BAD_SHAPE)
        self.refuses(good + b"{}", wire.NOT_JSON)
        self.refuses(good + b"x", wire.NOT_JSON)
        self.refuses(b"", wire.NOT_JSON)
        self.refuses(b"[]", wire.BAD_SHAPE)
        self.refuses(b'"admit"', wire.BAD_SHAPE)

    def test_a_missing_or_extra_key_is_refused_at_both_levels(self):
        doc = json.loads(request_bytes())
        for key in doc:
            with self.subTest(missing=key):
                less = dict(doc)
                del less[key]
                self.refuses(json.dumps(less), wire.BAD_SHAPE)
        for key in doc["context"]:
            less = json.loads(request_bytes())
            del less["context"][key]
            self.refuses(json.dumps(less), wire.BAD_SHAPE)
        more = json.loads(request_bytes())
        more["extra"] = 1
        self.refuses(json.dumps(more), wire.BAD_SHAPE)
        more = json.loads(request_bytes())
        more["context"]["extra"] = 1
        self.refuses(json.dumps(more), wire.BAD_SHAPE)

    def test_another_operations_shape_is_refused(self):
        for doc in ({"v": 1, "op": "recall", "agent": "alpha", "message_id": "m1"},
                    {"v": 1, "op": "pending", "agent": "alpha", "limit": 5},
                    {"v": 1, "op": "closed", "agent": "alpha", "message_id": "m1"},
                    dict(json.loads(request_bytes()), op="recall")):
            with self.subTest(op=doc["op"]):
                self.refuses(json.dumps(doc), wire.BAD_SHAPE)

    def test_a_value_of_the_wrong_type_is_refused(self):
        for key in ("agent", "message_id", "parent_request_id", "profile"):
            for value in (5, None, ["a"], True):
                doc = json.loads(request_bytes())
                doc[key] = value
                with self.subTest(key=key, value=value):
                    self.refuses(json.dumps(doc), wire.BAD_SHAPE)
        doc = json.loads(request_bytes())
        doc["context"] = "fresh"
        self.refuses(json.dumps(doc), wire.BAD_SHAPE)

    def test_non_ascii_text_is_refused(self):
        self.refuses(request_bytes().replace(b"alpha", "ä".encode("utf-8")), wire.NOT_JSON)

    def test_the_request_cap_is_512_bytes_and_one_more_is_too_large(self):
        good = request_bytes()
        padded = good + b" " * (512 - len(good))
        self.assertEqual(len(padded), 512)
        wire.parse_request(padded)
        self.refuses(padded + b" ", wire.TOO_LARGE)

    def test_a_failure_carries_a_closed_word_and_no_text_of_the_input(self):
        with self.assertRaises(wire.WireFailure) as caught:
            wire.parse_request(b'{"loud-private-text": 1}')
        self.assertIn(caught.exception.word, {wire.NOT_JSON, wire.BAD_SHAPE, wire.OUT_OF_BOUNDS, wire.TOO_LARGE})
        self.assertNotIn("loud", str(caught.exception))
        self.assertNotIn("loud", repr(wire.parse_request(request_bytes())))


class Answers(unittest.TestCase):

    def test_the_largest_admitted_answer_is_174_bytes(self):
        raw = wire.admitted(R, 100_000, 999_999_999_999, 86_400)
        self.assertEqual(len(raw), 174)
        self.assertEqual(raw, json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")).encode())
        self.assertEqual(wire.parse_answer(raw)["ttl_seconds"], 86_400)

    def test_null_caps_are_written_as_null(self):
        raw = wire.admitted("0123456789abcdef", None, None, 1800)
        self.assertEqual(json.loads(raw), {"max_calls": None, "max_micro_eur": None, "reference": "0123456789abcdef",
                                           "status": "admitted", "ttl_seconds": 1800, "v": 1})

    def test_an_answer_outside_its_bounds_is_never_written(self):
        for args in ((R + "r", None, None, 10), ("", None, None, 10), ("a b", None, None, 10), (R, 0, None, 10),
                     (R, 100_001, None, 10), (R, None, 10 ** 12, 10), (R, None, None, 0), (R, None, None, 86_401),
                     (R, True, None, 10), (R, None, None, True), (R, 1.5, None, 10)):
            with self.subTest(args=args):
                with self.assertRaises(wire.WireFailure):
                    wire.admitted(*args)

    def test_every_held_reason_is_one_of_the_six_words_and_the_longest_is_58_bytes(self):
        self.assertEqual(wire.HELD_REASONS, ("coordinator_unreachable", "refused", "parked", "context_unprovable",
                                             "store_busy", "not_configured"))
        sizes = {reason: len(wire.held(reason)) for reason in wire.HELD_REASONS}
        self.assertEqual(max(sizes.values()), 58)
        self.assertEqual(wire.held("refused"), b'{"reason":"refused","status":"held","v":1}')
        with self.assertRaises(wire.WireFailure):
            wire.held("made_up")

    def test_the_reading_side_is_as_strict_as_the_writing_side(self):
        good = wire.admitted("abc", None, None, 5)
        wire.parse_answer(good)
        bad = [good.replace(b'"v":1', b'"v":true'), good.replace(b'"ttl_seconds":5', b'"ttl_seconds":5.0'),
               good.replace(b'"ttl_seconds":5', b'"ttl_seconds":0'), good + b"{}",
               good.replace(b'"status":"admitted"', b'"status":"unknown"'),
               good.replace(b"}", b',"extra":1}'), good.replace(b'"max_calls":null,', b""),
               wire.held("refused").replace(b"refused", b"nonsense"),
               b'{"messages":[],"status":"ok","v":1}',                      # another operation's answer
               b'{"status":"held","reason":"refused","v":1,"v":1}', b"x" * 513]
        for raw in bad:
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(wire.WireFailure):
                    wire.parse_answer(raw)

    def test_only_admit_has_a_held_answer_and_an_unknown_reason_is_a_failure(self):
        with self.assertRaises(wire.WireFailure) as caught:
            wire.parse_answer(b'{"reason":"made_up","status":"held","v":1}')
        self.assertEqual(caught.exception.word, wire.OUT_OF_BOUNDS)


if __name__ == "__main__":
    unittest.main()
