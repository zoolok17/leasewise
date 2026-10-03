"""Card 7c-2, fix round 2 (the last for steps 1 and 2).

1. The queue accepts only known window words (the central validator shares ONE list with the reader).
2. A malformed evidence value is refused with the queue's fixed sentence, never a raw error.
(Item 3, the cycle summary, is in test_card7c2_summary.py.)
Synthetic data only.
"""

import os
import tempfile
import unittest

from quota_balancer.reporter import mapper, parkreader
from quota_balancer.reporter.state import State, StateError

from tests.test_card7c2_state import NOW, body

FIXED = "a limit event body is not valid"
PRIVATE = "synthetic_private_prose"                # lowercase, short: the old pattern accepted it


class _Queue(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH") or None
        if base:
            os.makedirs(base, exist_ok=True)
        self._dir = tempfile.TemporaryDirectory(dir=base, prefix="quota-7c2fix2-")
        self.addCleanup(self._dir.cleanup)
        self.state = State(os.path.join(self._dir.name, "r.db"))
        self.addCleanup(self.state.close)

    def refused(self, doc):
        with self.assertRaises(StateError) as caught:
            self.state.add_limit_event("synthetic-account", doc, NOW)
        self.assertEqual(str(caught.exception), FIXED)
        self.assertEqual(self.state.one("SELECT COUNT(*) FROM limit_event")[0], 0)


class WindowWords(_Queue):

    def test_the_two_producer_words_are_accepted_one_or_both(self):
        for windows in (["five_hour"], ["seven_day"], ["five_hour", "seven_day"], []):
            with self.subTest(windows=windows):
                self.assertTrue(mapper.limit_body_text(body(exhausted_windows=windows)))

    def test_a_short_lowercase_private_phrase_is_refused_by_the_body_builder(self):
        for windows in ([PRIVATE], ["five_hour", PRIVATE], ["Five_Hour"], ["five_hour "], ["weekly"], [""],
                        ["five_hour", "five_hour", "seven_day", "seven_day", "five_hour"]):
            with self.subTest(windows=windows):
                with self.assertRaises(ValueError):
                    mapper.limit_body_text(body(exhausted_windows=windows))

    def test_and_by_the_queue(self):
        self.refused(body(exhausted_windows=[PRIVATE]))
        self.refused(body(exhausted_windows=["five_hour", PRIVATE]))

    def test_entries_must_be_text_before_they_are_compared(self):
        for entry in ([], {}, None, 5, True, b"five_hour"):
            with self.subTest(entry=entry):
                self.refused(body(exhausted_windows=[entry]))

    def test_the_reader_and_the_queue_share_one_list(self):
        self.assertIs(parkreader.WINDOWS, mapper.LIMIT_WINDOWS)
        self.assertEqual(set(mapper.LIMIT_WINDOWS), {"five_hour", "seven_day"})
        for window in parkreader.WINDOWS:                         # whatever the reader lets through, the queue takes
            doc = mapper.limit_body(mapper.limit_key("desktop", "claude-main", window, 1_788_948_000, 1),
                                    "11111111-1111-4111-8111-111111111111", 1_788_900_000_000, 1_788_948_000, window)
            self.assertTrue(mapper.limit_body_text(doc))


class EvidenceType(_Queue):

    def test_a_list_an_object_or_another_type_is_the_fixed_refusal_not_a_raw_error(self):
        for bad in ([], {}, [1], {"a": 1}, None, 5, True, b"claude_exhausted", ("claude_exhausted",)):
            with self.subTest(bad=bad):
                self.refused(body(evidence_kind=bad))

    def test_a_known_word_is_accepted_and_an_unknown_one_refused(self):
        self.assertTrue(self.state.add_limit_event("synthetic-account", body(), NOW))
        self.refused_after_one(body(evidence_kind="synthetic_private_prose"))

    def refused_after_one(self, doc):
        with self.assertRaises(StateError):
            self.state.add_limit_event("synthetic-account", doc, NOW)
        self.assertEqual(self.state.one("SELECT COUNT(*) FROM limit_event")[0], 1)

    def test_the_body_builder_raises_a_value_error_for_an_unhashable_kind(self):
        for bad in ([], {}):
            with self.assertRaises(ValueError):
                mapper.limit_body_text(body(evidence_kind=bad))


if __name__ == "__main__":
    unittest.main()
