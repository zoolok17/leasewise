"""Card R1-M, step R1-M4: the declared basis on the lease side (DESIGN-R1-MONEY.md sections 2 and 6).

An account with a billing policy has declared its tax bases: the gateway's money (the receipts, and the model rates the holds are
sized at) is in the policy's LEDGER basis, the account budget and the caps are in its CAP basis. The lease books are converted to
the cap basis, ROUNDED UP (they are exposures), before they are compared. These tests use synthetic policies only.

* with different bases the conversion decides (a cap in `invoice_total`, receipts in `ex_vat`), for every lease-side term: the
  receipts of the account and of a scope, the open holds, the new hold, and the advisory check;
* with equal bases (`ex_vat` and `ex_vat`) and with no policy at all, every answer is the answer it always was;
* when the policy cannot convert (a basis is `unknown`, or the bases differ and no rate is given) the books are compared as they
  were, which the README says out loud;
* money only: token limits are never converted;
* the conversion helpers themselves: the rounding role of `reduce.convert_basis`, and `money.convert_total` for an exact total.
"""

import sqlite3
import unittest

from quota_balancer import errors, lease, money, reduce
from quota_balancer.errors import QuotaError

from tests.test_card4_seat_kind_fix import ACCOUNT, NOW, PROJECT, SEAT, TEAM
from tests.test_cardr1_money import TOP, _Admission

EX, INV, UNK = reduce.BASIS_EX_VAT, reduce.BASIS_INVOICE_TOTAL, reduce.BASIS_UNKNOWN
MONEY = "money_micro_eur"


class _Basis(_Admission):

    def policy(self, cap=INV, ledger=EX, vat=2000):
        machine = self.sql("SELECT id FROM machines")[0][0]
        self.sql("INSERT INTO billing_policies VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            ACCOUNT, machine, "proj-ai", 1, 1, 0, cap, "ex_vat", ledger, vat, 900, 7200, 14400, 50000, 100,
            "advisory", "alert", "hold"))

    def hold(self, lease_id, reserved, settled=None):
        self.sql("INSERT INTO holds(lease_id, limit_id, period_start, reserved, settled_actual) VALUES(?,?,?,?,?)",
                 (lease_id, lease._account_limit_id(ACCOUNT), self.month(), reserved, settled))

    def decide(self, metric, budget=None, new=1):
        """What admission answers for this group, with the account's real conversion: "ok" or the refusal's closed word."""
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            chain = {"platform": lease._PLATFORM_ID, "project": PROJECT, "team": TEAM, "seat": SEAT}
            rows = conn.execute("SELECT * FROM limits").fetchall()
            lease._admit_group(conn, (metric, "month", None), rows, chain, ACCOUNT, budget, True, new, new, 0, NOW,
                               basis=lease._lease_basis(conn, ACCOUNT))
            return "ok"
        except QuotaError as exc:
            return exc.code
        finally:
            conn.close()


class Different(_Basis):
    """Cap in `invoice_total`, receipts and rates in `ex_vat`, a rate of 20%: 1000 in the ledger is 1200 in the cap."""

    def test_the_account_receipts_are_converted_up_before_the_budget_is_compared(self):
        self.start()
        self.settle_both(money_amounts=(600, 400))                       # 1000 ex_vat = 1200 invoice_total
        self.assertEqual(self.decide(MONEY, budget=1000, new=0), "ok")                # no policy: the books as they are
        self.policy()
        self.assertEqual(self.decide(MONEY, budget=1199, new=0), errors.CODE_MONEY_LIMIT)
        self.assertEqual(self.decide(MONEY, budget=1200, new=0), "ok")

    def test_a_fraction_of_a_micro_euro_is_counted_up_never_dropped(self):
        self.start()
        self.settle_both(money_amounts=(1, 0))                           # 1 * 1.2 = 1.2, counted as 2
        self.assertEqual(self.decide(MONEY, budget=1, new=0), "ok")                   # no policy: 1 <= 1
        self.policy()
        self.assertEqual(self.decide(MONEY, budget=1, new=0), errors.CODE_MONEY_LIMIT)
        self.assertEqual(self.decide(MONEY, budget=2, new=0), "ok")
        self.assertEqual(self.decide(MONEY, budget=3, new=1), errors.CODE_MONEY_LIMIT)  # 2 + ceil(1.2) = 4 > 3
        self.assertEqual(self.decide(MONEY, budget=4, new=1), "ok")

    def test_the_open_holds_and_the_new_hold_are_converted_up_too(self):
        self.start()
        for lease_id in self.leases:
            self.hold(lease_id, 300)                                      # two holds of 300 = 600 ex_vat = 720
        self.assertEqual(self.decide(MONEY, budget=1000, new=300), "ok")              # no policy: 600 + 300 fits
        self.policy()
        self.assertEqual(self.decide(MONEY, budget=1000, new=300), errors.CODE_MONEY_LIMIT)    # 720 + 360 > 1000
        self.assertEqual(self.decide(MONEY, budget=1080, new=300), "ok")

    def test_a_scope_cap_compares_its_receipts_and_holds_in_the_cap_basis(self):
        self.start()
        self.settle_both(money_amounts=(500, 0))
        self.limit(MONEY, 700)                                            # a team cap of 700 (cap basis)
        self.assertEqual(self.decide(MONEY, new=100), "ok")               # no policy: 500 + 100 <= 700
        self.policy()
        self.assertEqual(self.decide(MONEY, new=100), errors.CODE_MONEY_LIMIT)       # 600 + 120 > 700
        self.assertEqual(self.decide(MONEY, new=0), "ok")                             # 600 <= 700

    def test_the_other_direction_is_rounded_up_as_well(self):
        self.start()
        self.settle_both(money_amounts=(1201, 0))                         # invoice_total 1201 -> ex_vat ceil(1000.83) = 1001
        self.policy(cap=EX, ledger=INV)
        self.assertEqual(self.decide(MONEY, budget=1001, new=0), "ok")
        self.assertEqual(self.decide(MONEY, budget=1000, new=0), errors.CODE_MONEY_LIMIT)    # not 1000: rounded up
        self.assertEqual(self.decide(MONEY, budget=1001, new=1), errors.CODE_MONEY_LIMIT)

    def test_one_new_hold_that_is_above_every_cap_after_conversion_is_refused_as_the_limit_it_is(self):
        self.start()
        self.policy()
        self.assertEqual(self.decide(MONEY, budget=TOP, new=TOP), errors.CODE_MONEY_LIMIT)

    def test_a_huge_total_stays_above_every_cap_whichever_way_it_is_converted(self):
        self.start()
        self.settle_both(money_amounts=(TOP, TOP))                        # 2^64 - 2: invoice_total -> ex_vat is still huge
        self.policy(cap=EX, ledger=INV)
        self.assertEqual(self.decide(MONEY, budget=TOP, new=0), errors.CODE_MONEY_LIMIT)

    def test_the_advisory_check_of_a_whole_grant_converts_each_receipt_up(self):
        self.start(budget=1200)
        self.settle_both(money_amounts=(600, 400))                        # 1000 raw: below 1200; 1200 converted: reached
        self.grant(NOW + 2000)                                            # no policy: admitted, as always
        self.policy()
        with self.assertRaises(QuotaError) as caught:
            self.grant(NOW + 3000)
        self.assertEqual(caught.exception.code, errors.CODE_MONEY_LIMIT)

    def test_tokens_are_never_converted(self):
        self.start()
        self.settle_both(money_amounts=(1, 1), tokens=(600, 400))
        self.limit("input_tokens", 1100)
        self.policy()
        self.assertEqual(self.decide("input_tokens", new=100), "ok")                    # 1000 + 100 <= 1100, not 1200 + 100
        self.assertEqual(self.decide("input_tokens", new=101), errors.CODE_INPUT_LIMIT)

    def test_the_conversion_the_lease_books_use_is_read_from_the_policy(self):
        self.start()
        conn = sqlite3.connect(self.db)
        try:
            self.assertIsNone(lease._lease_basis(conn, ACCOUNT))
        finally:
            conn.close()
        self.policy()
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(lease._lease_basis(conn, ACCOUNT), (EX, INV, 2000))
        finally:
            conn.close()


class Unchanged(_Basis):
    """Equal bases, no policy, and a policy that cannot convert: the answers are the answers they always were."""

    CASES = ((600, 0), (599, 0), (600, 1), (1000, 400), (1000, 401))
    EXPECTED = ["ok", errors.CODE_MONEY_LIMIT, errors.CODE_MONEY_LIMIT, "ok", errors.CODE_MONEY_LIMIT]

    def answers(self, declared=None):
        """The answers for CASES, with the policy declared as `declared` (keyword arguments of `policy`) or none."""
        self.start()
        for lease_id in self.leases:
            self.hold(lease_id, 300)                                      # 600 of open holds
        if declared is not None:
            self.policy(**declared)
        return [self.decide(MONEY, budget=budget, new=new) for budget, new in self.CASES]

    def test_no_policy_compares_the_books_as_they_are(self):
        self.assertEqual(self.answers(), self.EXPECTED)

    def test_equal_bases_change_nothing(self):
        for basis in (EX, INV):
            with self.subTest(basis=basis):
                self.assertEqual(self.answers(dict(cap=basis, ledger=basis, vat=None)), self.EXPECTED)
                self.sql("DELETE FROM billing_policies")
                self.sql("DELETE FROM holds")

    def test_a_policy_that_cannot_convert_leaves_the_books_as_they_were(self):
        for cap, ledger, vat in ((EX, UNK, 2000), (UNK, EX, 2000), (UNK, UNK, None), (INV, EX, None)):
            with self.subTest(cap=cap, ledger=ledger, vat=vat):
                self.assertEqual(self.answers(dict(cap=cap, ledger=ledger, vat=vat)), self.EXPECTED)
                self.sql("DELETE FROM billing_policies")
                self.sql("DELETE FROM holds")

    def test_a_whole_advisory_grant_with_equal_bases_is_unchanged(self):
        self.start(budget=1000)
        self.settle_both(money_amounts=(600, 399))
        self.policy(cap=EX, ledger=EX, vat=None)
        self.grant(NOW + 2000)
        self.settle(self.sql("SELECT id FROM leases ORDER BY granted_at DESC LIMIT 1")[0][0], NOW + 2500, money=1)
        with self.assertRaises(QuotaError) as caught:
            self.grant(NOW + 3000)
        self.assertEqual(caught.exception.code, errors.CODE_MONEY_LIMIT)


class Helpers(unittest.TestCase):

    def test_convert_basis_rounds_up_by_default_and_down_for_a_limit(self):
        self.assertEqual(reduce.convert_basis(1, EX, INV, 2000), 2)                              # 1.2 up (the default)
        self.assertEqual(reduce.convert_basis(1, EX, INV, 2000, reduce.ROLE_UP), 2)
        self.assertEqual(reduce.convert_basis(1, EX, INV, 2000, reduce.ROLE_DOWN), 1)           # 1.2 down
        self.assertEqual(reduce.convert_basis(1201, INV, EX, 2000, reduce.ROLE_UP), 1001)
        self.assertEqual(reduce.convert_basis(1201, INV, EX, 2000, reduce.ROLE_DOWN), 1000)
        self.assertEqual(reduce.convert_basis(1200, EX, INV, 2000, reduce.ROLE_DOWN), 1440)     # exact: the same either way
        self.assertEqual(reduce.convert_basis(7, EX, EX, None, reduce.ROLE_DOWN), 7)
        with self.assertRaises(QuotaError):
            reduce.convert_basis(1, EX, INV, 2000, "sideways")
        with self.assertRaises(QuotaError):
            reduce.convert_basis(1, UNK, INV, 2000, reduce.ROLE_DOWN)

    def test_a_limit_is_never_larger_than_the_exact_value_and_an_exposure_never_smaller(self):
        for amount in (0, 1, 7, 999, 123456789):
            for vat in (0, 1, 1900, 2000, 10000):
                for source, target in ((EX, INV), (INV, EX)):
                    up = reduce.convert_basis(amount, source, target, vat, reduce.ROLE_UP)
                    down = reduce.convert_basis(amount, source, target, vat, reduce.ROLE_DOWN)
                    self.assertLessEqual(down, up)
                    self.assertLessEqual(up - down, 1)

    def test_convert_total_takes_an_exact_total_above_every_stored_bound(self):
        self.assertEqual(money.convert_total(1, EX, INV, 2000, money.EXPOSURE), 2)
        self.assertEqual(money.convert_total(2 ** 70, INV, EX, 2000, money.EXPOSURE), money.SATURATION)
        self.assertEqual(money.convert_total(2 ** 70, EX, EX, None, money.LIMIT), money.SATURATION)
        self.assertIs(money.convert_total(5, UNK, EX, 2000, money.EXPOSURE), money.UNKNOWN)
        self.assertIs(money.convert_total(5, EX, INV, None, money.EXPOSURE), money.UNKNOWN)
        self.assertEqual(money.convert(TOP, EX, INV, 2000, money.EXPOSURE), money.UNKNOWN)   # `convert` still stops at the bound


if __name__ == "__main__":
    unittest.main()
