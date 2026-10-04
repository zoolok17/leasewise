"""Card R1-RC: the reporter's receipt path reads receipt pages with or without the month (closes R-017), and the digit checks.

A gateway at agenttalk `921c82d4` or later prints `charge_period` on every receipt row; an older one does not. The reporter's own receipt
path (`reporter/run.py`, `_take_pages`) read only the old shape and would refuse every row after the gateway upgrade. It now reads EITHER shape:

* a page whose rows ALL have the month, or NONE has it, is read; a page that mixes the two is `bad_row`, and nothing of it is stored;
* month rows are checked like any month row (a receipt with no calls has no month);
* the month is read and NOT kept: the stored receipts and what Leasewise receives are identical for the same receipt in both shapes;
* the strict shapes stay as they were for their callers (the R1 reader still accepts only month rows).

The close-time check and three more reporter checks used the digit shorthand of the regular expression engine, which takes the digits of other
scripts; they now accept ASCII digits only.

Synthetic receipts and agenttalk's documented page only. No network beyond localhost, no real gateway, no real provider.
"""

import json
import os
import tempfile
import unittest

from quota_balancer.reporter import config, events, parkreader, receipts, run

from tests.l6a1_env import CLOSED_AT, GENERATION, ledger, page as made_page, receipt, write_json
from tests import test_card7l6a1_e2e as e2e
from tests.test_cardr1reader1_report import AGENTTALK_PAGE, GEN, P

REF = "c" * 64
FULL_WIDTH = "".join(chr(0xFF10 + int(digit)) for digit in "2026")          # "２０２６"


def row(seq, month="absent", calls=1, cost=450, closed=CLOSED_AT):
    """A receipt row; `month` is a month, None (a null month) or "absent" (an existing-shape row)."""
    doc = {"seq": seq, "quota_lease_ref_sha256": REF, "outcome": "completed", "calls": calls,
           "input_tokens": 0 if calls == 0 else 1200, "output_tokens": 0 if calls == 0 else 300,
           "actual_micro_eur": cost, "closed_at": closed}
    if month != "absent":
        doc["charge_period"] = month
    return doc


def parse(rows, shape=receipts.EITHER, after=0):
    doc = made_page(rows, after=after, generation=GENERATION)
    return receipts.parse_page(json.dumps(doc).encode("utf-8"), after, month_rows=shape)


def refusal(test, rows, shape=receipts.EITHER):
    with test.assertRaises(receipts._Refused) as caught:
        parse(rows, shape)
    return caught.exception.args[0]


class Pages(unittest.TestCase):

    def test_an_existing_shape_page_is_read(self):
        got = parse([row(1), row(2)])
        self.assertEqual([r.seq for r in got.receipts], [1, 2])
        self.assertTrue(all(type(r) is receipts.Receipt for r in got.receipts))

    def test_a_month_page_is_read_and_agenttalks_own_documented_page_too(self):
        got = parse([row(1, P), row(2, None)])
        self.assertEqual([r.seq for r in got.receipts], [1, 2])
        documented = receipts.parse_page(AGENTTALK_PAGE, 0, month_rows=receipts.EITHER)       # byte for byte as agenttalk prints it
        self.assertEqual((documented.generation, documented.receipts[0].actual_micro_eur, documented.receipts[0].calls), (GEN, 1290, 1))

    def test_an_empty_page_is_read(self):
        self.assertEqual(parse([]).receipts, ())

    def test_a_page_that_mixes_the_two_shapes_is_bad_row_in_either_order(self):
        self.assertEqual(refusal(self, [row(1), row(2, P)]), "bad_row")
        self.assertEqual(refusal(self, [row(1, P), row(2)]), "bad_row")
        self.assertEqual(refusal(self, [row(1, None), row(2), row(3, P)]), "bad_row")

    def test_a_month_row_with_no_calls_and_a_month_is_refused_on_this_path_too(self):
        self.assertEqual(refusal(self, [row(1, P, calls=0, cost=0)]), "bad_row")
        self.assertEqual(parse([row(1, None, calls=0, cost=0)]).receipts[0].calls, 0)              # no calls and a null month: accepted
        self.assertEqual(parse([row(1, P, calls=1, cost=0)]).receipts[0].actual_micro_eur, 0)       # calls above 0, cost 0: may name a month

    def test_the_same_receipt_in_both_shapes_has_the_same_fields(self):
        old, new = parse([row(1)]).receipts[0], parse([row(1, P)]).receipts[0]
        for name in receipts.Receipt.__slots__:
            self.assertEqual(getattr(old, name), getattr(new, name), name)

    def test_the_strict_shapes_are_unchanged_for_their_callers(self):
        self.assertEqual(refusal(self, [row(1, P)], shape=False), "bad_row")           # the existing strict reader still refuses a month row
        self.assertEqual(refusal(self, [row(1)], shape=True), "bad_row")               # the R1 reader still needs the month
        self.assertEqual(parse([row(1)], shape=False).receipts[0].seq, 1)
        self.assertEqual(parse([row(1, P)], shape=True).receipts[0].seq, 1)

    def test_the_other_checks_still_apply(self):
        self.assertEqual(refusal(self, [row(2)]), "bad_order")
        bad = row(1, P)
        bad["agent"] = "alice"
        self.assertEqual(refusal(self, [bad]), "bad_row")

    def test_the_run_path_asks_for_either_shape(self):
        with open(run.__file__, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("receipts.read_page(source, after, self.stop, month_rows=receipts.EITHER)", text)


class DigitChecks(unittest.TestCase):
    """The class of mistake: a regular expression written with the digit shorthand takes the digits of other scripts."""

    def test_a_close_time_in_full_width_digits_is_refused_and_ascii_is_accepted(self):
        wide = CLOSED_AT.replace("2026", FULL_WIDTH)
        self.assertNotEqual(wide, CLOSED_AT)
        for shape in (False, True, receipts.EITHER):
            with self.subTest(shape=shape):
                rows = [row(1, P if shape is True else "absent", closed=wide)]
                self.assertEqual(refusal(self, rows, shape), "bad_row")
                good = [row(1, P if shape is True else "absent")]
                self.assertEqual(parse(good, shape).receipts[0].closed_at, CLOSED_AT)

    def test_the_time_of_a_park_marker_the_sid_text_and_a_segment_number_take_ascii_digits_only(self):
        self.assertIsNotNone(parkreader._PARKED_AT.fullmatch("2026-10-03T10:00:00.123456Z"))
        self.assertIsNone(parkreader._PARKED_AT.fullmatch("2026-10-03T10:00:00.123456Z".replace("2026", FULL_WIDTH)))
        self.assertIsNotNone(config._SID_TEXT.fullmatch("S-1-5-21-123-456"))
        self.assertIsNone(config._SID_TEXT.fullmatch("S-1-5-21-" + FULL_WIDTH + "-456"))
        with tempfile.TemporaryDirectory(dir=os.environ.get("AGENTTALK_SCRATCH") or None) as root:
            directory = os.path.join(root, "alpha")
            os.makedirs(directory)
            for name in ("gen-1.jsonl", "gen-².jsonl", "gen-" + FULL_WIDTH + ".jsonl"):         # an ASCII number, a superscript, full width
                with open(os.path.join(directory, name), "w", encoding="utf-8"):
                    pass
            self.assertEqual([seg.segment for seg in events.take_snapshot(root)["alpha"].segments], [1])      # not 2 (a superscript) nor 2026


class Cycle(unittest.TestCase):
    """The reporter cycle against a real in-process Leasewise: the same receipt in both shapes."""

    def run_once(self, shape="absent"):
        case = e2e.EndToEnd("test_a_receipt_settles_the_lease_with_the_mapped_values")
        case.setUp()
        self.addCleanup(case.rig.cleanup)
        lease_id = case.grant()["lease_id"]
        extra = {} if shape == "absent" else {"charge_period": shape}
        reporter = case.start([receipt(lease_id, 1, calls=2, input_tokens=1200, output_tokens=300, actual_micro_eur=450, **extra)])
        case.know(lease_id)
        result = reporter.cycle()
        captured = case.rig.state.all("SELECT generation, seq, outcome, calls, input_tokens, output_tokens, actual_micro_eur, "
                                      "closed_at FROM receipt_capture")
        states = case.rig.state.all("SELECT state, tries, last_error_code FROM receipt_state")
        sent = case.rig.lw.rows("SELECT seq, calls, input_tokens, cached_input_tokens, output_tokens, actual_micro_eur, "
                                "outcome, final FROM receipts WHERE lease_id=?", (lease_id,))
        paths = [call[0].replace(lease_id, "LEASE") for call in case.rig.net.calls]
        return (result.new_receipts, result.sent, result.blocked), captured, states, sent, paths, case

    def test_the_same_receipt_gives_identical_stored_rows_and_uploads_in_both_shapes(self):
        old = self.run_once()
        month = self.run_once(P)
        self.assertEqual(old[0], (1, 1, None))
        for number in range(5):
            self.assertEqual(month[number], old[number], number)
        self.assertEqual(old[3][0][5], 450)

    def test_a_null_month_is_read_the_same_way(self):
        old, null_month = self.run_once(), self.run_once(None)
        for number in range(5):
            self.assertEqual(null_month[number], old[number], number)

    def test_a_gateway_that_changes_shape_between_two_pages_is_read_both_times(self):
        case = e2e.EndToEnd("test_a_receipt_settles_the_lease_with_the_mapped_values")
        case.setUp()
        self.addCleanup(case.rig.cleanup)
        first, second = case.grant()["lease_id"], case.grant()["lease_id"]
        reporter = case.start([receipt(first, 1)])                                             # an old-shape page
        case.know(first, second)
        self.assertEqual(reporter.cycle().new_receipts, 1)
        write_json(case.fixture, ledger([receipt(first, 1), receipt(second, 2, charge_period=P)]))   # the gateway was upgraded
        result = reporter.cycle()
        self.assertEqual((result.new_receipts, result.sent, result.blocked), (1, 1, None))
        self.assertEqual(case.rig.state.all("SELECT seq FROM receipt_capture ORDER BY seq"), [(1,), (2,)])
        self.assertEqual(case.rig.state.current_cursor(1)[1], 2)

    def test_a_page_that_mixes_the_shapes_is_refused_and_nothing_of_it_is_stored(self):
        case = e2e.EndToEnd("test_a_receipt_settles_the_lease_with_the_mapped_values")
        case.setUp()
        self.addCleanup(case.rig.cleanup)
        first, second = case.grant()["lease_id"], case.grant()["lease_id"]
        reporter = case.start([receipt(first, 1), receipt(second, 2, charge_period=P)])
        case.know(first, second)
        result = reporter.cycle()
        self.assertEqual((result.new_receipts, result.sent), (0, 0))
        self.assertEqual(case.rig.state.all("SELECT COUNT(*) FROM receipt_capture"), [(0,)])
        self.assertEqual(case.rig.state.all("SELECT COUNT(*) FROM receipt_state"), [(0,)])
        self.assertEqual(case.rig.state.current_cursor(1)[1], 0)
        self.assertEqual(case.rig.net.calls, [])

    def test_a_month_row_with_no_calls_and_a_month_is_refused_in_the_cycle_too(self):
        case = e2e.EndToEnd("test_a_receipt_settles_the_lease_with_the_mapped_values")
        case.setUp()
        self.addCleanup(case.rig.cleanup)
        lease = case.grant()["lease_id"]
        reporter = case.start([receipt(lease, 1, calls=0, input_tokens=0, output_tokens=0, actual_micro_eur=0, charge_period=P)])
        case.know(lease)
        self.assertEqual(reporter.cycle().new_receipts, 0)
        self.assertEqual(case.rig.state.all("SELECT COUNT(*) FROM receipt_capture"), [(0,)])


if __name__ == "__main__":
    unittest.main()
