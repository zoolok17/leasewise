"""Card L6a-1 (revised), step 1: the receipt source adapter (`quota_balancer/reporter/receipts.py`).

The adapter is the ONE place that knows how gateway receipts reach us, so everything it refuses is refused here, and a
refused page is refused WHOLE: nothing is stored from it. The unit is the versioned page of DESIGN-6A 5.4: its
version, its closed keys, the ledger generation (even on an empty page), the asked-for number, the receipts with
nullable token counts and an ISO close time, and numbers that rise by exactly one. A source that cannot be read is
"read again later", never a settle. The command source is tried with fake scripts only. Synthetic data only.
"""

import os
import tempfile
import unittest

from quota_balancer.reporter import receipts

from tests.l6a1_env import (GENERATION, GENERATION_B, encode, fixture_source, lease, ledger, page,
                            receipt, write_json, write_raw)

LOUD = "loud-secret-l6a1-adapter"


def parse(doc, after=0, limit=receipts.MAX_ROWS):
    """The page of `doc`, or the closed (status, reason) the adapter refuses it with."""
    try:
        return receipts.parse_page(encode(doc), after, limit)
    except receipts._Refused as exc:
        return (receipts.REFUSED, exc.args[0])
    except receipts._Later as exc:
        return (receipts.READ_LATER, exc.args[0])


class Pages(unittest.TestCase):

    def refused(self, doc, reason, after=0):
        self.assertEqual(parse(doc, after), (receipts.REFUSED, reason), reason)

    def test_a_good_page_comes_back_with_every_field_exactly_as_received(self):
        one, two = lease(), lease()
        got = parse(page([receipt(one, 1), receipt(two, 2, outcome="failed", calls=0, input_tokens=0,
                                                   output_tokens=0, actual_micro_eur=0)]))
        self.assertEqual((got.generation, got.after_seq, got.next_seq, got.has_more), (GENERATION, 0, 2, False))
        self.assertEqual([r.seq for r in got.receipts], [1, 2])
        first = got.receipts[0]
        self.assertEqual((first.outcome, first.calls, first.input_tokens, first.output_tokens,
                          first.actual_micro_eur, first.closed_at),
                         ("completed", 2, 1200, 300, 450, "2026-10-03T10:00:00.000000Z"))
        self.assertEqual(set(first.__slots__), {"seq", "ref_sha256", "outcome", "calls", "input_tokens",
                                                "output_tokens", "actual_micro_eur", "closed_at"})
        self.assertNotIn("1200", repr(first) + str(got))

    def test_an_empty_page_still_names_its_generation(self):
        got = parse(page([], after=7, generation=GENERATION_B), after=7)
        self.assertEqual((got.generation, got.after_seq, got.next_seq, got.receipts), (GENERATION_B, 7, 7, ()))
        self.refused(page([], after=7, generation=None), "bad_envelope")
        self.refused({k: v for k, v in page([]).items() if k != "generation"}, "bad_envelope")

    def test_unknown_token_counts_stay_unknown(self):
        got = parse(page([receipt(lease(), 1, input_tokens=None, output_tokens=None),
                          receipt(lease(), 2, input_tokens=5, output_tokens=None),
                          receipt(lease(), 3, input_tokens=None, output_tokens=7)]))
        self.assertEqual([(r.input_tokens, r.output_tokens) for r in got.receipts],
                         [(None, None), (5, None), (None, 7)])

    def test_the_version_is_one(self):
        for bad in (0, 2, "1", 1.0, True, None, [1]):
            self.refused(page([], envelope_version=bad), "bad_version")

    def test_the_envelope_keys_are_closed(self):
        good = page([receipt(lease(), 1)])
        self.refused(dict(good, extra=LOUD), "bad_envelope")
        self.refused({k: v for k, v in good.items() if k != "has_more"}, "bad_envelope")

    def test_the_page_must_be_an_object(self):
        for doc in ([], [page([])], "x", 3, None, True):
            self.refused(doc, "not_an_object")

    def test_the_envelope_values_have_exact_types(self):
        for key, bad in (("generation", "A" * 32), ("generation", "a" * 31), ("generation", "a" * 33),
                         ("generation", "a" * 32 + "\n"), ("generation", 5), ("after_seq", True),
                         ("after_seq", 1.0), ("after_seq", -1), ("next_seq", "1"), ("has_more", 1),
                         ("has_more", "true"), ("receipts", {}), ("receipts", None)):
            self.refused(page([], **{key: bad}), "bad_envelope" if key != "after_seq" else "bad_envelope")

    def test_the_asked_for_number_must_come_back(self):
        self.refused(page([receipt(lease(), 6)], after=5), "wrong_after", after=0)
        self.refused(page([], after=0), "wrong_after", after=4)
        self.assertEqual(parse(page([receipt(lease(), 6)], after=5), after=5).next_seq, 6)

    def test_the_numbers_rise_by_exactly_one_from_the_asked_for_number(self):
        a, b, c = lease(), lease(), lease()
        self.refused(page([receipt(a, 2)], after=0), "bad_order")                            # a gap at the start
        self.refused(page([receipt(a, 1), receipt(b, 3)]), "bad_order")                      # a gap in the middle
        self.refused(page([receipt(a, 1), receipt(b, 1)], next_seq=1), "bad_order")          # a repeat
        self.refused(page([receipt(a, 2), receipt(b, 1)]), "bad_order")                      # backwards
        self.refused(page([receipt(a, 4)], after=5, next_seq=4), "bad_order", after=5)       # at or before the cursor
        self.assertEqual(parse(page([receipt(a, 1), receipt(b, 2), receipt(c, 3)])).next_seq, 3)

    def test_next_seq_and_has_more_must_agree_with_the_receipts(self):
        a = lease()
        self.refused(page([receipt(a, 1)], next_seq=2), "bad_envelope")
        self.refused(page([receipt(a, 1)], next_seq=0), "bad_envelope")
        self.refused(page([], after=3, next_seq=4), "bad_envelope", after=3)
        self.refused(page([], has_more=True), "bad_envelope")
        self.assertTrue(parse(page([receipt(a, 1)], has_more=True)).has_more)

    def test_the_receipt_keys_are_closed(self):
        good = receipt(lease(), 1)
        for doc in (dict(good, agent=LOUD), dict(good, generation=GENERATION), {k: v for k, v in good.items()
                                                                                   if k != "closed_at"},
                    {}, [], "x", None):
            self.refused(page([doc], next_seq=1), "bad_row")

    def test_every_number_must_be_a_plain_whole_number(self):
        for key in ("seq", "calls", "actual_micro_eur"):
            for bad in (True, False, 1.0, 1.5, "1", None, [1], {"a": 1}, -1):
                self.refused(page([receipt(lease(), **{key: bad})], next_seq=1), "bad_row")
        for key in ("input_tokens", "output_tokens"):
            for bad in (True, False, 1.0, 1.5, "1", [1], {"a": 1}, -1):
                self.refused(page([receipt(lease(), **{key: bad})], next_seq=1), "bad_row")

    def test_the_bounds_are_enforced(self):
        top = {"calls": receipts.MAX_CALLS + 1, "input_tokens": receipts.MAX_TOKENS + 1,
               "output_tokens": receipts.MAX_TOKENS + 1, "actual_micro_eur": receipts.MAX_MICRO_EUR + 1}
        for key, bad in top.items():
            self.refused(page([receipt(lease(), **{key: bad})], next_seq=1), "bad_row")
        self.refused(page([receipt(lease(), 0)], next_seq=0), "bad_row")
        edge = receipt(lease(), 1, calls=receipts.MAX_CALLS, input_tokens=receipts.MAX_TOKENS,
                       output_tokens=receipts.MAX_TOKENS, actual_micro_eur=receipts.MAX_MICRO_EUR)
        self.assertEqual(parse(page([edge])).next_seq, 1)

    def test_the_outcome_is_one_of_the_four_words(self):
        for word in ("completed", "cancelled", "failed", "provider_limit"):
            self.assertEqual(parse(page([receipt(lease(), outcome=word)])).next_seq, 1, word)
        for bad in ("done", "COMPLETED", "", " completed", None, 1, ["failed"], LOUD):
            self.refused(page([receipt(lease(), outcome=bad)], next_seq=1), "bad_row")

    def test_the_reference_has_an_exact_shape(self):
        for bad in ("", "A" * 64, "g" * 64, "a" * 63, "a" * 65, "a" * 64 + "\n", None, 7):
            self.refused(page([receipt(lease(), quota_lease_ref_sha256=bad)], next_seq=1), "bad_row")

    def test_the_close_time_is_the_ledgers_own_iso_format(self):
        for bad in (1_788_224_400_000, "2026-10-03", "2026-10-03T10:00:00Z", "2026-10-03T10:00:00.000Z",
                    "2026-10-03 10:00:00.000000Z", "2026-13-03T10:00:00.000000Z", "2026-10-03T25:00:00.000000Z",
                    "2026-10-03T10:00:00.000000", "", None, "2026-10-03T10:00:00.000000Z\n"):
            self.refused(page([receipt(lease(), closed_at=bad)], next_seq=1), "bad_row")

    def test_a_receipt_with_no_calls_may_not_carry_spend(self):
        self.refused(page([receipt(lease(), calls=0, input_tokens=5, output_tokens=0, actual_micro_eur=0)],
                          next_seq=1), "bad_row")
        self.refused(page([receipt(lease(), calls=0, input_tokens=0, output_tokens=0, actual_micro_eur=1)],
                          next_seq=1), "bad_row")
        self.assertEqual(parse(page([receipt(lease(), calls=0, input_tokens=0, output_tokens=0,
                                             actual_micro_eur=0)])).next_seq, 1)

    def test_a_repeated_key_is_refused(self):
        good = encode(page([receipt(lease(), 1)])).decode()
        for text in (good.replace('"has_more": false', '"has_more": false, "has_more": true'),
                     good[:-3] + ', "calls": 9}]}'):
            with self.subTest(text=text[-40:]):
                self.assertEqual(self.raw(text), (receipts.REFUSED, "duplicate_key"))

    def raw(self, text, after=0):
        try:
            receipts.parse_page(text.encode(), after)
        except receipts._Refused as exc:
            return (receipts.REFUSED, exc.args[0])
        except receipts._Later as exc:
            return (receipts.READ_LATER, exc.args[0])

    def test_nan_and_the_infinities_are_refused(self):
        good = encode(page([receipt(lease(), 1)])).decode()
        for token in ("NaN", "Infinity", "-Infinity"):
            self.assertEqual(self.raw(good.replace('"calls": 2', '"calls": ' + token)),
                             (receipts.REFUSED, "bad_row"), token)

    def test_text_that_is_not_json_is_read_later(self):
        for raw in (b'{"envelope_version": 1', b"", b"\xff\xfe\x00", b"not json", b"[1,]"):
            try:
                receipts.parse_page(raw, 0)
            except receipts._Later as exc:
                self.assertEqual(exc.args[0], "not_json", raw)

    def test_too_many_receipts_are_refused(self):
        rows = [receipt(lease(), n) for n in range(1, receipts.MAX_ROWS + 2)]
        self.refused(page(rows), "too_many")

    def test_zero_calls_means_zero_counts_and_zero_money_and_null_is_not_zero(self):
        """F4 (spec 3.4): with no call, both token counts and the money must be the number 0."""
        zero = dict(calls=0, input_tokens=0, output_tokens=0, actual_micro_eur=0)
        self.assertEqual(len(parse(page([receipt(lease(), 1, **zero)])).receipts), 1)
        for over in (dict(input_tokens=None), dict(output_tokens=None), dict(input_tokens=None, output_tokens=None),
                     dict(input_tokens=1), dict(output_tokens=1), dict(actual_micro_eur=1)):
            with self.subTest(over=over):
                self.refused(page([receipt(lease(), 1, **dict(zero, **over))]), "bad_row")
        self.refused(page([receipt(lease(), 1), receipt(lease(), 2, **dict(zero, input_tokens=None))]), "bad_row")

    def test_null_counts_stay_legal_when_there_was_at_least_one_call(self):
        got = parse(page([receipt(lease(), 1, calls=1, input_tokens=None, output_tokens=None)]))
        self.assertEqual((got.receipts[0].input_tokens, got.receipts[0].output_tokens), (None, None))

    def test_a_page_longer_than_the_requested_limit_is_refused_before_anything_is_stored(self):
        """F5: the limit asked for is the limit allowed, not just the 1000 ceiling."""
        rows = [receipt(lease(), n) for n in range(1, 151)]
        self.assertEqual(parse(page(rows), limit=150).next_seq, 150)
        self.assertEqual(parse(page(rows), limit=100), (receipts.REFUSED, "too_many"))     # shorter than 1000, longer than asked
        self.assertEqual(parse(page(rows[:1]), limit=1).next_seq, 1)
        self.assertEqual(parse(page(rows[:2]), limit=1), (receipts.REFUSED, "too_many"))
        big = [receipt(lease(), n) for n in range(1, 1001)]
        self.assertEqual(parse(page(big), limit=1000).next_seq, 1000)
        self.assertEqual(parse(page(big + [receipt(lease(), 1001)]), limit=1000), (receipts.REFUSED, "too_many"))
        self.assertEqual(parse(page([]), limit=1).receipts, ())                              # an empty page is always fine

    def test_one_bad_receipt_refuses_the_whole_page(self):
        got = parse(page([receipt(lease(), 1), receipt(lease(), 2, outcome="nope")]))
        self.assertEqual(got, (receipts.REFUSED, "bad_row"))


class _Dir(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(dir=os.environ.get("AGENTTALK_SCRATCH") or None,
                                                prefix="receipts-l6a1-")
        self.addCleanup(self._dir.cleanup)
        self.dir = self._dir.name
        self.fixture = os.path.join(self.dir, "receipts.json")


class Fixture(_Dir):

    def read(self, doc, after=0, **over):
        write_json(self.fixture, doc)
        return receipts.read_page(fixture_source(self.fixture, **over), after)

    def test_the_fixture_page_has_the_configured_length_and_says_there_is_more(self):
        rows = [receipt(lease(), n) for n in range(1, 151)]
        for limit, expected in ((1, 1), (100, 100), (150, 150), (1000, 150)):
            with self.subTest(limit=limit):
                result = self.read(ledger(rows), page_limit=limit)
                self.assertEqual(len(result.page.receipts), expected)
                self.assertEqual(result.page.has_more, expected < 150)

    def test_a_ready_made_fixture_page_longer_than_the_configured_length_is_refused(self):
        rows = [receipt(lease(), n) for n in range(1, 151)]
        result = self.read(page(rows), page_limit=100)
        self.assertEqual((result.status, result.reason), (receipts.REFUSED, "too_many"))
        self.assertEqual(self.read(page(rows), page_limit=150).status, receipts.OK)

    def test_a_ledger_makes_the_page_after_the_asked_for_number(self):
        rows = [receipt(lease(), n) for n in (1, 2, 3, 4)]
        result = self.read(ledger(rows), after=2)
        self.assertEqual((result.status, result.reason), (receipts.OK, None))
        self.assertEqual([r.seq for r in result.page.receipts], [3, 4])
        self.assertEqual((result.page.generation, result.page.after_seq, result.page.next_seq),
                         (GENERATION, 2, 4))
        self.assertEqual(self.read(ledger(rows), after=4).page.receipts, ())

    def test_a_long_ledger_is_paged_with_has_more(self):
        rows = [receipt(lease(), n) for n in range(1, 251)]
        first = self.read(ledger(rows)).page
        self.assertEqual((len(first.receipts), first.next_seq, first.has_more), (100, 100, True))
        last = self.read(ledger(rows), after=200).page
        self.assertEqual((len(last.receipts), last.next_seq, last.has_more), (50, 250, False))

    def test_a_made_page_is_served_as_it_is_and_still_checked(self):
        result = self.read(page([receipt(lease(), 1)]))
        self.assertEqual(result.status, receipts.OK)
        bad = self.read(page([receipt(lease(), 1)], envelope_version=2))
        self.assertEqual((bad.status, bad.reason), (receipts.REFUSED, "bad_version"))
        wrong = self.read(page([receipt(lease(), 6)], after=5), after=0)
        self.assertEqual((wrong.status, wrong.reason), (receipts.REFUSED, "wrong_after"))

    def test_a_ledger_with_a_gap_is_refused(self):
        result = self.read(ledger([receipt(lease(), 1), receipt(lease(), 3)]))
        self.assertEqual((result.status, result.reason), (receipts.REFUSED, "bad_order"))

    def test_a_missing_file_is_read_later(self):
        result = receipts.read_page(fixture_source(os.path.join(self.dir, "nope.json")), 0)
        self.assertEqual((result.status, result.reason), (receipts.READ_LATER, "source_missing"))

    def test_text_that_is_not_json_is_read_later_and_too_large_is_refused(self):
        write_raw(self.fixture, b'{"envelope_version": 1, "genera')
        result = receipts.read_page(fixture_source(self.fixture), 0)
        self.assertEqual((result.status, result.reason), (receipts.READ_LATER, "not_json"))
        write_json(self.fixture, ledger([receipt(lease(), 1)]))
        result = receipts.read_page(fixture_source(self.fixture, max_bytes=100), 0)
        self.assertEqual((result.status, result.reason), (receipts.REFUSED, "too_large"))


if __name__ == "__main__":
    unittest.main()
