"""Card R1-3e: the pure C3 comparison (`quota_balancer/receipt_coverage.py`) and its two `status` figures (DESIGN-R1-3.md 5, 8.3, 9).

A report's `settled` covers ALL settled spend: the receipts up to its watermark plus spend that has no receipt yet. So the two are not equal and the
difference is not an error. The comparison says, for `status` only, how much of `settled` has no receipt yet (`unreceipted_micro_eur`) and whether the
covered receipts claim more than the books (`receipts_exceed_settled`). Both are information: never an error, never a transition, no effect on usability
or on any decision. Today every receipt lacks a ledger generation, so everything is MODE C: the first figure is null (never 0) and the second false.

Synthetic values only.
"""

import json
import os
import unittest
from unittest import mock

from quota_balancer import evidence_view, money, receipt_coverage as rc
from quota_balancer.receipt_coverage import Receipt, Report

from tests import test_cardr13d_operator as op

GW, OTHER = "gw-1", "gw-2"
GEN, GEN2 = "a" * 32, "b" * 32
MONTH, OTHER_MONTH = 1788220800000, 1785542400000


def receipt(seq, amount, gateway=GW, generation=GEN, month=MONTH):
    return Receipt(gateway, generation, seq, month, amount)


def report(settled=1000, receipts_seq=10, generation=GEN, gateway=GW, month=MONTH):
    return Report(gateway, generation, receipts_seq, month, settled)


class Covered(unittest.TestCase):

    def test_covered_receipts_are_exactly_this_gateway_this_generation_at_most_the_watermark_and_this_month(self):
        rows = [receipt(1, 100), receipt(2, 200), receipt(10, 50),                    # covered: 350
                receipt(11, 7000),                                                    # above the watermark
                receipt(3, 7000, gateway=OTHER),                                      # another gateway
                receipt(4, 7000, generation=GEN2),                                    # another ledger generation
                receipt(5, 7000, month=OTHER_MONTH),                                  # another month
                receipt(6, 7000, month=None)]                                         # spans two months (or unknown): never covered
        got = rc.compare(report(settled=1000), rows)
        self.assertEqual(got, rc.Comparison(rc.MODE_COVERED, 350, 650, False))

    def test_the_boundary_is_inclusive_at_the_watermark_and_exclusive_above(self):
        self.assertEqual(rc.compare(report(receipts_seq=5), [receipt(5, 10), receipt(6, 99)]).covered_sum, 10)
        self.assertEqual(rc.compare(report(receipts_seq=0), [receipt(1, 10)]).covered_sum, 0)

    def test_a_cross_month_receipt_is_never_covered_it_is_counted_from_the_books(self):
        got = rc.compare(report(settled=500), [receipt(1, 300, month=None)])
        self.assertEqual((got.mode, got.covered_sum, got.unreceipted_micro_eur), (rc.MODE_COVERED, 0, 500))      # nothing of it is subtracted

    def test_the_difference_is_information_never_an_error(self):
        rows = [receipt(1, 400)]
        self.assertEqual(rc.compare(report(settled=1000), rows).unreceipted_micro_eur, 600)
        self.assertEqual(rc.compare(report(settled=400), rows), rc.Comparison(rc.MODE_COVERED, 400, 0, False))      # a real zero, not "unknown"
        self.assertEqual(rc.compare(report(settled=0), []), rc.Comparison(rc.MODE_COVERED, 0, 0, False))

    def test_receipts_that_claim_more_than_the_books_raise_only_the_diagnostic(self):
        got = rc.compare(report(settled=100), [receipt(1, 60), receipt(2, 60)])
        self.assertEqual(got, rc.Comparison(rc.MODE_COVERED, 120, None, True))                                    # no exception, no clamp, no negative

    def test_the_largest_amounts_are_exact_and_a_figure_out_of_bounds_is_refused_not_clamped(self):
        top = 2 ** 63 - 1
        self.assertEqual(rc.compare(report(settled=top), [receipt(1, top)]).unreceipted_micro_eur, 0)
        with self.assertRaises(money.MoneyError):
            rc.compare(report(settled=top + 1), [])
        with self.assertRaises(money.MoneyError):
            rc.compare(report(), [receipt(1, -1)])
        with self.assertRaises(money.MoneyError):
            rc.compare(report(), [receipt(1, 1.5)])


class ModeC(unittest.TestCase):

    def test_a_report_without_a_watermark_is_mode_c(self):
        got = rc.compare(report(receipts_seq=None), [receipt(1, 10)])
        self.assertEqual(got, rc.Comparison(rc.MODE_C, None, None, False))

    def test_a_receipt_without_a_generation_is_mode_c_and_so_is_every_receipt_today(self):
        for rows in ([receipt(1, 10, generation=None)], [receipt(1, 10), receipt(2, 10, generation=None)],
                     [receipt(1, 10, generation=None, month=None)]):
            with self.subTest(rows=rows):
                self.assertEqual(rc.compare(report(), rows), rc.Comparison(rc.MODE_C, None, None, False))

    def test_a_receipt_that_could_never_be_covered_does_not_force_mode_c(self):
        rows = [receipt(1, 10), receipt(2, 99, gateway=OTHER, generation=None), receipt(3, 99, month=OTHER_MONTH, generation=None)]
        self.assertEqual(rc.compare(report(settled=100), rows).mode, rc.MODE_COVERED)

    def test_a_covered_receipt_with_no_known_cost_cannot_be_summed_so_it_is_mode_c(self):
        self.assertEqual(rc.compare(report(), [receipt(1, None)]), rc.Comparison(rc.MODE_C, None, None, False))

    def test_a_source_that_records_no_generation_is_mode_c_even_with_no_receipt_at_all(self):
        self.assertEqual(rc.compare(report(settled=100), [], receipts_carry_generation=False), rc.Comparison(rc.MODE_C, None, None, False))
        self.assertEqual(rc.compare(report(settled=100), [receipt(1, 10)], receipts_carry_generation=False).mode, rc.MODE_C)
        self.assertEqual(rc.compare(report(settled=100), []), rc.Comparison(rc.MODE_COVERED, 0, 100, False))        # with a generation it is covered

    def test_unknown_is_null_never_zero(self):
        got = rc.compare(report(receipts_seq=None), [])
        self.assertIsNone(got.unreceipted_micro_eur)
        self.assertIsNot(got.unreceipted_micro_eur, 0)

    def test_the_over_count_of_mode_c_is_stated_a_settled_lease_is_counted_twice(self):
        """`settled` 100 already holds the 60 of the lease whose receipt is stored. Mode C adds every final receipt of the month on top of `V`
        (which includes all of `settled`), so the lease is counted twice: 160 against a true 100. By design, and safe; nothing here hides it."""
        rows = [receipt(1, 60, generation=None)]
        mode, q = money.unreported_receipts([(r.seq, r.amount) for r in rows], None)
        self.assertEqual((mode, q), (money.MODE_C, 60))
        v = money.gateway_exposure((100, 0, 0))
        self.assertEqual(money.add(v, q), 160)
        self.assertGreater(money.add(v, q), 100)
        self.assertEqual(rc.compare(report(settled=100), rows), rc.Comparison(rc.MODE_C, None, None, False))      # and the comparison claims nothing


class Pure(unittest.TestCase):

    def test_the_module_touches_no_database_clock_file_or_process(self):
        for name in ("sqlite3", "time", "os", "subprocess", "socket", "store"):
            self.assertNotIn(name, vars(rc), name)

    def test_the_same_inputs_give_the_same_answer_and_the_inputs_are_not_changed(self):
        rows = [receipt(1, 10), receipt(2, 20)]
        before = list(rows)
        self.assertEqual(rc.compare(report(), rows), rc.compare(report(), rows))
        self.assertEqual(rows, before)


class StatusFigures(op._Op):

    def final_receipt(self, event, seq, amount, gateway="g1"):
        """A final receipt row of this gateway, written directly (the foreign key to a lease is not enforced on this plain connection)."""
        self.run_sql("INSERT INTO receipts(event_id, lease_id, gateway_id, seq, calls, input_tokens, cached_input_tokens, output_tokens, "
                     "actual_micro_eur, outcome, final, observed_at, received_at) VALUES(?,?,?,?,1,1,0,1,?,'completed',1,1,1)",
                     (event, "lease-" + event, self.gateway_id(gateway), seq, amount))

    def test_the_two_keys_are_there_with_the_block_and_absent_without_a_version_4_report(self):
        self.assertTrue(all("evidence" not in g for g in op.store.status(self.db)["gateways"]))
        self.send(op.rep(seq=1))
        block = self.evidence()
        self.assertIn("unreceipted_micro_eur", block)
        self.assertIn("receipts_exceed_settled", block)
        by_alias = {g["alias"]: g for g in op.store.status(self.db)["gateways"]}
        self.assertNotIn("evidence", by_alias["g2"])

    def test_today_every_receipt_is_mode_c_so_the_figure_is_null_and_never_zero(self):
        self.first()
        self.final_receipt("r1", 1, 60)
        self.final_receipt("r2", 2, 30)
        block = self.evidence()
        self.assertIsNone(block["unreceipted_micro_eur"])
        self.assertIs(block["receipts_exceed_settled"], False)
        self.assertIn('"unreceipted_micro_eur": null', json.dumps(block))                                      # shown as null on the wire

    def test_with_no_receipt_at_all_the_figure_is_still_null_not_the_whole_of_settled(self):
        self.first()
        block = self.evidence()
        self.assertIsNone(block["unreceipted_micro_eur"])
        self.assertIs(block["receipts_exceed_settled"], False)

    def test_the_receipts_table_still_has_no_generation_so_the_caller_still_says_so(self):
        """When a later card adds a generation to the receipts, this fails and `evidence_view.coverage` must start passing it."""
        columns = [r[1] for r in self.rows("PRAGMA table_info(receipts)")]
        self.assertFalse([c for c in columns if "generation" in c], columns)

    def test_the_figures_change_nothing_else_in_the_block_or_in_any_decision(self):
        self.first()
        plain = self.evidence()
        self.final_receipt("r1", 1, 60)
        with_receipts = self.evidence()
        keys = {"unreceipted_micro_eur", "receipts_exceed_settled"}
        self.assertEqual({k: v for k, v in with_receipts.items() if k not in keys}, {k: v for k, v in plain.items() if k not in keys})
        # even a diagnostic that fires does not touch the state, the blockers, the open items or the action
        with mock.patch.object(rc, "compare", return_value=rc.Comparison(rc.MODE_COVERED, 999, None, True)):
            fired = self.evidence()
        self.assertEqual((fired["unreceipted_micro_eur"], fired["receipts_exceed_settled"]), (None, True))
        self.assertEqual({k: v for k, v in fired.items() if k not in keys}, {k: v for k, v in plain.items() if k not in keys})
        with mock.patch.object(rc, "compare", return_value=rc.Comparison(rc.MODE_COVERED, 10, 90, False)):
            self.assertEqual(self.evidence()["unreceipted_micro_eur"], 90)
        # the chain the builder hands the evaluator is the same
        self.assertEqual(op.store.evidence_inputs(self.db, "meter", op.NOW)["states"], {"g1": "usable", "g2": "unadopted"})

    def test_the_comparison_is_made_for_the_report_the_block_describes_and_its_month(self):
        self.first()
        seen = []
        real = rc.compare
        with mock.patch.object(rc, "compare", lambda r, rows, **kw: seen.append((r, list(rows), kw)) or real(r, rows, **kw)):
            self.send(op.rep(seq=2, settled_micro_eur=5100, obs=op.OBS + 10))
            self.final_receipt("r1", 1, 60)
            self.evidence()
        (r, rows, kw) = seen[-1]
        self.assertEqual(kw, {"receipts_carry_generation": False})
        self.assertEqual((r.gateway_id, r.receipts_seq, r.month, r.settled), (self.gateway_id(), 4, op.SEP, 5100))      # the newest report: seq 2
        self.assertEqual([(x.seq, x.amount, x.generation, x.month) for x in rows], [(1, 60, None, None)])

    def test_no_sentence_or_message_carries_a_value(self):
        from quota_balancer import errors
        for text in (evidence_view.ACTION_SENTENCE, errors.MSG_ADOPTION_REFUSED, errors.MSG_EVIDENCE_REQUIRED):
            self.assertNotIn("unreceipted", text)
            self.assertFalse(any(ch.isdigit() for ch in text))

    def test_no_grant_admission_or_reporter_path_reads_the_comparison_or_its_keys(self):
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "quota_balancer")
        names = ["lease.py", "http_api.py", "money.py", "reduce.py", "billing_eval.py", "store.py", "policy.py"]
        names += [os.path.join("reporter", n) for n in os.listdir(os.path.join(root, "reporter")) if n.endswith(".py")]
        for name in names:
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            for needle in ("receipt_coverage", "unreceipted_micro_eur", "receipts_exceed_settled"):
                if needle == "receipts_exceed_settled" and name.endswith("gwreport.py"):
                    continue                                       # the money reader's own refusal word of the same name: an unrelated thing
                self.assertNotIn(needle, text, name + " " + needle)


if __name__ == "__main__":
    unittest.main()
