"""Card R1-M, step R1-M5: unknown flows through the sums, and the conservative inequality (DESIGN-R1-MONEY.md sections 4 and 6).

* the evaluator's `_sum` and `_checked` and the opening planner's sums are `money`'s: an unknown term makes a sum unknown and is
  never the known part or 0; an overflow is still the closed `INVALID_CONFIG`;
* the evaluator's headroom with an unknown term is "unavailable", never a manufactured room;
* a gateway share read as its own ceiling rounds DOWN (`share_limit_micro_eur`), as an allocation it still rounds UP;
* the accounting terms of DESIGN-R1 3.3 (`V`, `Q`) are known or unknown, and missing coverage is MODE C: an upper bound, never
  unknown and never 0;
* DESIGN-R1 12.5: the coordinator's headroom (`cap - (V + Q + H)`) and the evaluator's (`cap - (V + allowance)`, no `Q`) agree on
  every component, and the coordinator's is never the larger. A test that passed with EQUAL headroom would be wrong by
  construction, so the grid includes `Q > 0`.

Synthetic amounts only.
"""

import unittest

from quota_balancer import billing_eval, billing_open, money, reduce
from quota_balancer.errors import CODE_INVALID_CONFIG, QuotaError

U = money.UNKNOWN
EX, INV = reduce.BASIS_EX_VAT, reduce.BASIS_INVOICE_TOTAL
TOP = 2 ** 63 - 1


def context(cap_basis, ledger_basis, vat_bps, shares=(), rows=()):
    return {
        "policy": {"cap_basis": cap_basis, "provider_basis": cap_basis, "ledger_basis": ledger_basis, "vat_bps": vat_bps},
        "provider_events": [],
        "chains_by_gateway": {"g%d" % i: [row] for i, row in enumerate(rows)},
        "gateways": [{"gateway": "g%d" % i, "share_micro_eur": share} for i, share in enumerate(shares)],
        "boundary_exposure_micro_eur": None,
    }


class EvaluatorSums(unittest.TestCase):

    def test_an_unknown_term_makes_the_sum_unknown_and_never_the_known_part(self):
        self.assertIs(billing_eval._sum([5, U, 7]), U)
        self.assertIs(billing_eval._sum([U]), U)
        self.assertEqual(billing_eval._sum([5, 7]), 12)
        self.assertEqual(billing_eval._sum(x for x in (1, 2, 3)), 6)                 # a generator still works
        self.assertEqual(billing_eval._sum([]), 0)                                   # nothing to add is 0, not unknown

    def test_an_overflow_is_still_invalid_config_and_so_is_a_value_that_is_not_an_amount(self):
        for bad in ([TOP, 1], [2 ** 70], [-1], [1.5], [True], ["1"]):
            with self.subTest(bad=bad):
                with self.assertRaises(QuotaError) as caught:
                    billing_eval._sum(bad)
                self.assertEqual(caught.exception.code, CODE_INVALID_CONFIG)
        self.assertEqual(billing_eval._sum([TOP]), TOP)

    def test_checked_lets_unknown_through_and_refuses_what_the_store_could_not_keep(self):
        self.assertIs(billing_eval._checked(U), U)
        self.assertEqual(billing_eval._checked(0), 0)
        self.assertEqual(billing_eval._checked(TOP), TOP)
        for bad in (-1, TOP + 1, 1.0, True, None):
            with self.subTest(bad=bad):
                with self.assertRaises(QuotaError):
                    billing_eval._checked(bad)

    def test_the_headroom_of_an_unknown_total_is_unknown_not_a_number(self):
        self.assertIs(money.headroom(100, [billing_eval._sum([5, U])]), U)
        self.assertEqual(money.headroom(100, [billing_eval._sum([5, 7])]), 88)

    def test_a_known_part_that_passes_the_limit_still_decides_through_the_module(self):
        self.assertIs(money.exceeds([6, U], 5), True)
        self.assertIs(money.exceeds([3, U], 5), U)                                    # not False: missing is not "fits"


class SharesAsLimits(unittest.TestCase):
    """Cap in `invoice_total`, ledger in `ex_vat`, a rate of 20%: a share of 1001 is 1201.2 in the cap's terms."""

    def test_a_share_rounds_up_as_an_allocation_and_down_as_its_own_ceiling(self):
        ctx = context(INV, EX, 2000, shares=(1001, 1000, 0))
        self.assertTrue(billing_eval._normalize_money(ctx))
        got = [(g["share_micro_eur"], g["share_limit_micro_eur"]) for g in ctx["gateways"]]
        self.assertEqual(got, [(1202, 1201), (1200, 1200), (0, 0)])

    def test_the_ceiling_is_never_above_the_exact_value_and_the_allocation_never_below(self):
        for share in (0, 1, 3, 7, 999, 123456789):
            for cap, ledger in ((INV, EX), (EX, INV)):
                for vat in (0, 1900, 2000, 10000):
                    ctx = context(cap, ledger, vat, shares=(share,))
                    billing_eval._normalize_money(ctx)
                    gateway = ctx["gateways"][0]
                    self.assertLessEqual(gateway["share_limit_micro_eur"], gateway["share_micro_eur"])
                    self.assertLessEqual(gateway["share_micro_eur"] - gateway["share_limit_micro_eur"], 1)

    def test_equal_bases_give_the_same_figure_for_both_uses(self):
        ctx = context(EX, EX, None, shares=(1001,))
        billing_eval._normalize_money(ctx)
        self.assertEqual((ctx["gateways"][0]["share_micro_eur"], ctx["gateways"][0]["share_limit_micro_eur"]), (1001, 1001))

    def test_an_unknown_basis_converts_nothing(self):
        ctx = context(reduce.BASIS_UNKNOWN, EX, 2000, shares=(10,))
        self.assertFalse(billing_eval._normalize_money(ctx))
        self.assertNotIn("share_limit_micro_eur", ctx["gateways"][0])

    def test_the_opening_planner_reads_a_share_as_its_ceiling(self):
        """baseline + external must stay within the gateway's share: at the ceiling, not at the rounded-up allocation."""
        ctx = context(INV, EX, 2000, shares=(1001,))
        billing_eval._normalize_money(ctx)
        ctx["policy"]["non_ai_allowance_micro_eur"] = 0

        def request(baseline, external):
            return {"baselines": {"g0": {"baseline": baseline}}, "proofs": {}, "consumed": set(),
                    "allocations": {"g0": {"external": external, "proof_sum": external, "ids": []}},
                    "non_ai": {"amount": 0, "proof_sum": 0, "ids": []}}
        self.assertTrue(billing_open._allocations_exact(ctx, request(1000, 201), 1201))      # 1201 <= ceiling 1201
        self.assertFalse(billing_open._allocations_exact(ctx, request(1000, 202), 1202))     # 1202: the allocation would let it


class Terms(unittest.TestCase):
    """DESIGN-R1 3.3 and 3.4: the terms as known or unknown, and mode C."""

    def test_a_gateway_exposure_is_settled_plus_reserved_plus_uncertain(self):
        self.assertEqual(money.gateway_exposure((100, 20, 3)), 123)
        self.assertEqual(money.gateway_exposure((0, 0, 0)), 0)

    def test_no_usable_report_or_an_unknown_part_is_unknown_never_zero(self):
        self.assertIs(money.gateway_exposure(None), U)
        self.assertIs(money.gateway_exposure((100, U, 3)), U)

    def test_one_gateway_without_evidence_makes_the_account_unknown(self):
        self.assertEqual(money.account_exposure([money.gateway_exposure((1, 2, 3)), money.gateway_exposure((10, 0, 0))]), 16)
        self.assertIs(money.account_exposure([money.gateway_exposure((1, 2, 3)), money.gateway_exposure(None)]), U)
        self.assertEqual(money.account_exposure([]), 0)                              # no gateways: nothing to add

    def test_missing_coverage_is_mode_c_an_upper_bound_never_unknown_and_never_zero(self):
        receipts = [(1, 100), (2, 50), (3, 7)]
        self.assertEqual(money.unreported_receipts(receipts, None), (money.MODE_C, 157))
        self.assertEqual(money.unreported_receipts([], None), (money.MODE_C, 0))     # no receipts: a known 0, honestly

    def test_with_coverage_only_the_newer_receipts_are_added(self):
        receipts = [(1, 100), (2, 50), (3, 7)]
        self.assertEqual(money.unreported_receipts(receipts, 2), (money.MODE_COVERED, 7))
        self.assertEqual(money.unreported_receipts(receipts, 3), (money.MODE_COVERED, 0))
        self.assertEqual(money.unreported_receipts(receipts, 0), (money.MODE_COVERED, 157))

    def test_mode_c_is_never_below_the_covered_figure(self):
        receipts = [(n, n * 11) for n in range(1, 9)]
        for covered in range(0, 10):
            self.assertGreaterEqual(money.unreported_receipts(receipts, None)[1], money.unreported_receipts(receipts, covered)[1])

    def test_the_coordinator_headroom_is_unknown_when_any_term_is(self):
        self.assertEqual(money.coordinator_headroom(1000, 100, 50, 10), 840)
        self.assertEqual(money.coordinator_headroom(100, 100, 50, 10), -60)           # negative: no room
        for terms in ((U, 50, 10), (100, U, 10), (100, 50, U)):
            self.assertIs(money.coordinator_headroom(1000, *terms), U)
        self.assertIs(money.coordinator_headroom(U, 1, 1, 1), U)


class ConservativeInequality(unittest.TestCase):
    """DESIGN-R1 12.5: component agreement, and the coordinator never has more room than the evaluator. Never equal headroom."""

    REPORTS = ((0, 0, 0), (100, 0, 0), (1000, 200, 30), (123457, 9999, 1), (5, 5, 5))
    GRID = ((EX, EX, None), (INV, EX, 2000), (EX, INV, 2000), (INV, EX, 1900), (EX, INV, 0), (INV, INV, None))

    def evaluator_figures(self, cap_basis, ledger_basis, vat, report, cap, allowance):
        ctx = context(cap_basis, ledger_basis, vat, shares=(10 ** 9,), rows=[(1, 1, *report, "e")])
        self.assertTrue(billing_eval._normalize_money(ctx))
        chain = list(ctx["chains_by_gateway"].values())
        ledger = billing_eval._sum(c[-1][2] for c in chain)
        reserves = billing_eval._sum(v for c in chain for v in c[-1][3:5])
        return ledger, reserves, money.headroom(cap, [billing_eval._sum([allowance, ledger, reserves])])

    def test_the_components_agree_and_the_coordinator_is_never_larger(self):
        for cap_basis, ledger_basis, vat in self.GRID:
            for report in self.REPORTS:
                for q in (0, 1, 777):
                    for h in (0, 5, 1000):
                        with self.subTest(bases=(cap_basis, ledger_basis, vat), report=report, q=q, h=h):
                            allowance, cap_cap = 40, 10 ** 7
                            ledger, reserves, evaluator = self.evaluator_figures(
                                cap_basis, ledger_basis, vat, report, cap_cap, allowance)
                            parts = [money.convert(part, ledger_basis, cap_basis, vat, money.EXPOSURE) for part in report]
                            self.assertEqual((parts[0], parts[1] + parts[2]), (ledger, reserves))          # components agree
                            v = money.gateway_exposure(parts)
                            self.assertEqual(v, ledger + reserves)
                            coordinator = money.coordinator_headroom(cap_cap - allowance, v, q, h)
                            self.assertLessEqual(coordinator, evaluator)
                            self.assertEqual(evaluator - coordinator, q + h)                            # exactly what it adds
                            if q + h > 0:
                                self.assertLess(coordinator, evaluator)                                  # never EQUAL headroom

    def test_a_test_that_passed_with_equal_headroom_would_be_wrong(self):
        ledger, reserves, evaluator = self.evaluator_figures(EX, EX, None, (1000, 200, 30), 10 ** 7, 40)
        coordinator = money.coordinator_headroom(10 ** 7 - 40, ledger + reserves, 777, 5)
        self.assertNotEqual(coordinator, evaluator)

    def test_an_unknown_gateway_makes_both_unknown_so_neither_admits(self):
        v = money.account_exposure([money.gateway_exposure((1, 2, 3)), money.gateway_exposure(None)])
        self.assertIs(money.coordinator_headroom(1000, v, 0, 0), U)


if __name__ == "__main__":
    unittest.main()
