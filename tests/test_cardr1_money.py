"""Card R1-M (steps R1-M1 and R1-M2): one money basis with checked arithmetic (DESIGN-R1-MONEY.md sections 2 to 5).

* `quota_balancer/money.py` is pure: exact integers, a range check at the edges, amounts that are UNKNOWN (never 0) when a term is
  unknown, conversions between tax bases rounded by the role of the amount (up for an exposure, down for a limit).
* `exact_sums` adds amounts in the database WITHOUT the database adding them: the high and the low half of each number are added
  separately and put together in Python. It equals a Python sum on adversarial rows, and a store that breaks its own rules is refused.
* The four admission sums (S1 to S4 of the design) use it: a total larger than the database can add is now the closed refusal
  (`MONEY_LIMIT`, `INPUT_LIMIT`), never `STORE_ERROR` (residual risk R-013). Results at normal sizes are unchanged.
Synthetic data only.
"""

import json
import random
import sqlite3
import unittest
from unittest import mock

from quota_balancer import errors, lease, money, policy, reduce
from quota_balancer.reporter import admit_wire, receipts

from tests.test_card4_seat_kind_fix import ACCOUNT, NOW, PROJECT, SEAT, TEAM
from tests.test_cardr1_expiry import _Case

TOP = 2 ** 63 - 1
EX, INV, UNK = reduce.BASIS_EX_VAT, reduce.BASIS_INVOICE_TOTAL, reduce.BASIS_UNKNOWN
EDGES = (0, 1, 2 ** 31 - 1, 2 ** 31, 2 ** 32 - 1, 2 ** 32, 2 ** 62, TOP - 1, TOP)


class Range(unittest.TestCase):

    def test_check_accepts_zero_and_the_largest_stored_amount_and_nothing_beyond(self):
        self.assertEqual((money.check(0), money.check(TOP)), (0, TOP))
        for bad in (-1, TOP + 1, 2 ** 64, True, False, 1.0, "1", None, b"1", [1]):
            with self.subTest(bad=bad):
                with self.assertRaises(money.MoneyError):
                    money.check(bad)

    def test_the_interface_bounds_are_checked_where_they_apply_and_both_are_pinned(self):
        self.assertEqual((money.WIRE_MAX, money.ADMIT_WIRE_MAX), (10 ** 12, 10 ** 12 - 1))
        self.assertEqual((receipts.MAX_MICRO_EUR, admit_wire.MAX_MICRO_EUR), (money.WIRE_MAX, money.ADMIT_WIRE_MAX))
        self.assertEqual(money.check(10 ** 12, maximum=money.WIRE_MAX), 10 ** 12)
        with self.assertRaises(money.MoneyError):
            money.check(10 ** 12 + 1, maximum=money.WIRE_MAX)
        with self.assertRaises(money.MoneyError):
            money.check(10 ** 12, maximum=money.ADMIT_WIRE_MAX)

    def test_the_reporters_receipt_wire_takes_10_to_the_12_and_refuses_one_more_while_storage_takes_2_to_the_63_minus_1(self):
        from tests.l6a1_env import lease as new_lease, page, receipt
        one = new_lease()
        for amount, accepted in ((10 ** 12, True), (10 ** 12 + 1, False)):
            doc = json.dumps(page([receipt(one, 1, actual_micro_eur=amount)])).encode()
            with self.subTest(amount=amount):
                if accepted:
                    self.assertEqual(receipts.parse_page(doc, 0).receipts[0].actual_micro_eur, amount)
                else:
                    with self.assertRaises(Exception):                         # noqa: B017 - the adapter's own closed refusal
                        receipts.parse_page(doc, 0)
        self.assertEqual(policy._check_int(TOP), TOP)                          # what the coordinator accepts and stores
        with self.assertRaises(errors.QuotaError):
            policy._check_int(TOP + 1)

    def test_saturation_keeps_small_totals_and_caps_big_ones_one_above_every_cap(self):
        self.assertEqual(money.SATURATION, TOP + 1)
        self.assertEqual(policy.USAGE_MAX, money.SATURATION)
        for value in (0, 1, TOP, TOP + 1):
            self.assertEqual(money.saturate(value), value)
        self.assertEqual(money.saturate(TOP + 2), TOP + 1)
        self.assertEqual(money.saturate(5 * TOP), TOP + 1)


class Sums(unittest.TestCase):

    def test_add_is_exact_far_beyond_the_stored_range(self):
        self.assertEqual(money.add(TOP, TOP, TOP), 3 * TOP)
        self.assertEqual(money.add(), 0)

    def test_exceeds_and_reaches_stop_at_the_first_point_that_decides(self):
        seen = []

        def terms():
            for value in (TOP, TOP, 1):
                seen.append(value)
                yield value
        self.assertIs(money.exceeds(terms(), TOP), True)
        self.assertEqual(seen, [TOP, TOP])                                    # it never asked for the third
        self.assertIs(money.reaches([TOP], TOP), True)
        self.assertIs(money.exceeds([TOP], TOP), False)                       # strict: equal is not exceeding
        self.assertIs(money.reaches([TOP - 1], TOP), False)
        self.assertIs(money.exceeds([], 0), False)

    def test_a_total_above_every_bound_is_answered_not_raised(self):
        self.assertIs(money.exceeds([TOP, TOP, TOP, TOP], TOP), True)
        self.assertEqual(money.headroom(TOP, [1, 2]), TOP - 3)
        self.assertEqual(money.headroom(10, [TOP]), 10 - TOP)                 # negative headroom is an exact number

    def test_a_term_that_is_not_an_amount_is_refused(self):
        for bad in (-1, 1.5, True, "3", None):
            with self.subTest(bad=bad):
                with self.assertRaises(money.MoneyError):
                    money.add(1, bad)
                with self.assertRaises(money.MoneyError):
                    money.exceeds([bad], 5)


class Unknown(unittest.TestCase):
    """Unknown is never zero."""

    def test_a_sum_with_an_unknown_term_is_unknown_never_the_known_part(self):
        self.assertIs(money.add(2, money.UNKNOWN), money.UNKNOWN)
        self.assertIs(money.add(money.UNKNOWN), money.UNKNOWN)
        self.assertEqual(money.lower_bound([2, money.UNKNOWN, 5]), 7)            # a floor, never the answer

    def test_the_unknown_object_cannot_be_used_as_a_number_or_a_truth_value(self):
        for use in (lambda: bool(money.UNKNOWN), lambda: money.UNKNOWN + 1, lambda: 1 < money.UNKNOWN):
            with self.assertRaises(TypeError):
                use()
        self.assertEqual(repr(money.UNKNOWN), "UNKNOWN")
        self.assertFalse(money.is_known(money.UNKNOWN))
        self.assertTrue(money.is_known(0))
        self.assertIsNot(money.UNKNOWN, 0)
        self.assertIs(money.UNKNOWN, type(money.UNKNOWN)())                       # one object

    def test_a_known_part_that_already_passes_the_limit_still_decides(self):
        self.assertIs(money.exceeds([6, money.UNKNOWN], 5), True)
        self.assertIs(money.reaches([5, money.UNKNOWN], 5), True)
        self.assertIs(money.exceeds([3, money.UNKNOWN], 5), money.UNKNOWN)         # not False: missing evidence is not "fits"
        self.assertIs(money.exceeds([money.UNKNOWN], 0), money.UNKNOWN)
        self.assertIs(money.exceeds([2, 3], 5), False)

    def test_an_unknown_limit_makes_the_comparison_unknown(self):
        self.assertIs(money.exceeds([1], money.UNKNOWN), money.UNKNOWN)
        self.assertIs(money.reaches([TOP], money.UNKNOWN), money.UNKNOWN)
        self.assertIs(money.headroom(money.UNKNOWN, [1]), money.UNKNOWN)
        self.assertIs(money.headroom(10, [1, money.UNKNOWN]), money.UNKNOWN)


class Conversion(unittest.TestCase):
    """The tax bases, rounded by the role of the amount."""

    def test_equal_known_bases_return_the_amount_unchanged_in_both_roles(self):
        for role in money.ROLES:
            for basis in (EX, INV):
                self.assertEqual(money.convert(TOP, basis, basis, None, role), TOP)

    def test_hand_computed_table_for_both_directions_and_both_roles(self):
        # (amount, source, target, vat_bps, exposure, limit)
        table = (
            (10, EX, INV, 2000, 12, 12),                  # 10 * 1.2 = 12 exactly: no rounding
            (1, EX, INV, 2000, 2, 1),                     # 1.2: up 2, down 1
            (7, EX, INV, 2000, 9, 8),                     # 8.4
            (1, INV, EX, 2000, 1, 0),                     # 0.8333
            (12, INV, EX, 2000, 10, 10),                  # exactly 10
            (3, INV, EX, 10000, 2, 1),                    # 1.5
            (3, EX, INV, 10000, 6, 6),
            (5, EX, INV, 0, 5, 5),                        # no VAT: identity
            (5, INV, EX, 0, 5, 5),
            (0, EX, INV, 2000, 0, 0),
        )
        for amount, source, target, vat, up, down in table:
            with self.subTest(amount=amount, source=source, target=target, vat=vat):
                self.assertEqual(money.convert(amount, source, target, vat, money.EXPOSURE), up)
                self.assertEqual(money.convert(amount, source, target, vat, money.LIMIT), down)

    def test_a_limit_is_never_larger_and_an_exposure_never_smaller_than_the_exact_value(self):
        rng = random.Random(7)
        for _ in range(500):
            amount, vat = rng.randrange(0, 10 ** 12), rng.choice((0, 1, 1999, 2000, 2500, 10000))
            for source, target in ((EX, INV), (INV, EX)):
                if source == EX:
                    num, den = amount * (10_000 + vat), 10_000
                else:
                    num, den = amount * 10_000, 10_000 + vat
                up = money.convert(amount, source, target, vat, money.EXPOSURE)
                down = money.convert(amount, source, target, vat, money.LIMIT)
                self.assertGreaterEqual(up * den, num)
                self.assertLessEqual(down * den, num)
                self.assertLessEqual(up - down, 1)

    def test_the_exposure_rule_is_the_existing_conversion(self):
        rng = random.Random(11)
        for _ in range(500):
            amount, vat = rng.randrange(0, 10 ** 15), rng.choice((0, 1, 2000, 10000))
            for source, target in ((EX, INV), (INV, EX), (EX, EX)):
                self.assertEqual(money.convert(amount, source, target, vat, money.EXPOSURE),
                                 reduce.convert_basis(amount, source, target, vat))

    def test_what_cannot_be_converted_is_unknown_never_zero(self):
        for source, target in ((UNK, EX), (EX, UNK), (UNK, UNK), (INV, UNK)):
            for role in money.ROLES:
                self.assertIs(money.convert(5, source, target, 2000, role), money.UNKNOWN)
        for rate in (None, -1, 10001, 2000.0, True, "2000"):
            self.assertIs(money.convert(5, EX, INV, rate, money.EXPOSURE), money.UNKNOWN, rate)
        self.assertIs(money.convert(TOP, EX, INV, 2000, money.EXPOSURE), money.UNKNOWN)       # the result would not fit
        self.assertEqual(money.convert(TOP // 2, EX, INV, 10000, money.EXPOSURE), TOP - 1)    # one that does fit

    def test_a_value_that_is_not_an_amount_or_a_basis_that_is_not_closed_is_refused(self):
        for bad in (-1, 1.5, True, "5", None, TOP + 1):
            with self.assertRaises(money.MoneyError):
                money.convert(bad, EX, INV, 2000, money.EXPOSURE)
        with self.assertRaises(money.MoneyError):
            money.convert(5, "gross", INV, 2000, money.EXPOSURE)
        with self.assertRaises(money.MoneyError):
            money.convert(5, EX, INV, 2000, "average")

    def test_the_declared_currency_is_euro_only_at_the_door(self):
        """Another currency never reaches the arithmetic: the provider reading is refused before (validate_readings)."""
        from quota_balancer import validate_readings
        self.assertEqual(validate_readings.CURRENCY_EUR, "EUR")


class Table(unittest.TestCase):
    """A table that keeps the schema's rule (no negative amounts), and one that does not."""

    def conn(self, check=True):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, a INTEGER %s, b INTEGER %s) STRICT"
                     % (("CHECK(a IS NULL OR a>=0)", "CHECK(b IS NULL OR b>=0)") if check else ("", "")))
        self.addCleanup(conn.close)
        return conn

    def fill(self, conn, rows):
        conn.executemany("INSERT INTO t(a, b) VALUES(?,?)", rows)

    def test_a_table_with_the_check_refuses_a_negative_amount(self):
        conn = self.conn()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO t(a) VALUES(-1)")

    def test_it_equals_a_python_sum_on_adversarial_rows(self):
        rng = random.Random(2026)
        rows = [(rng.choice(EDGES + (rng.randrange(0, TOP),)), rng.choice(EDGES)) for _ in range(2000)]
        rows += [(TOP, TOP)] * 1000                                        # many rows at the largest amount
        conn = self.conn()
        self.fill(conn, rows)
        with self.assertRaises(sqlite3.OperationalError):                  # the database's own SUM cannot do this
            conn.execute("SELECT SUM(a) FROM t").fetchone()
        sums = money.exact_sums(conn, "t WHERE id > ?", (0,), ["a", "b"])
        self.assertEqual(sums.totals, [sum(r[0] for r in rows), sum(r[1] for r in rows)])
        self.assertEqual(sums.count, 3000)

    def test_one_row_no_row_a_null_and_a_total_of_exactly_two_to_the_63(self):
        conn = self.conn()
        self.assertEqual(money.exact_sum(conn, "t WHERE id > ?", (0,), "a"), 0)             # 0, not NULL
        self.fill(conn, [(TOP, None)])
        self.assertEqual(money.exact_sum(conn, "t WHERE id > ?", (0,), "a"), TOP)
        self.assertEqual(money.exact_sum(conn, "t WHERE id > ?", (0,), "b"), 0)             # a NULL adds nothing, as SUM did
        self.fill(conn, [(1, 5)])
        self.assertEqual(money.exact_sum(conn, "t WHERE id > ?", (0,), "a"), 2 ** 63)       # one past the stored range
        self.assertEqual(money.saturate(money.exact_sum(conn, "t WHERE id > ?", (0,), "a")), money.SATURATION)

    def test_a_filter_with_bound_values_and_a_join_work_as_in_the_queries_it_replaces(self):
        conn = self.conn()
        self.fill(conn, [(1, 0), (10, 0), (100, 0), (TOP, 0)])
        self.assertEqual(money.exact_sum(conn, "t WHERE a < ? AND id <> ?", (TOP, 2), "a"), 101)

    def test_more_rows_than_the_split_sum_can_add_is_refused_by_the_guard(self):
        conn = self.conn()
        self.fill(conn, [(1, 1)] * 4)
        with mock.patch.object(money, "ROW_LIMIT", 3):
            with self.assertRaises(money.RowCountError):
                money.exact_sum(conn, "t WHERE id > ?", (0,), "a")
        self.assertEqual(money.exact_sum(conn, "t WHERE id > ?", (0,), "a"), 4)        # at the real limit it is fine
        self.assertEqual(money.ROW_LIMIT, 2 ** 31 - 1)
        self.assertLess(money.ROW_LIMIT * (2 ** 32 - 1), 2 ** 63)                      # neither half can overflow below it
        self.assertLess(money.ROW_LIMIT * (2 ** 31 - 1), 2 ** 63)

    def test_a_negative_amount_in_a_store_without_the_check_is_refused_never_summed(self):
        conn = self.conn(check=False)
        self.fill(conn, [(5, 1), (-1, 1)])
        with self.assertRaises(money.MoneyError):
            money.exact_sum(conn, "t WHERE id > ?", (0,), "a")

    def test_a_guard_counts_the_rows_that_break_a_rule(self):
        conn = self.conn()
        self.fill(conn, [(5, 3), (2, 9), (4, 4)])
        sums = money.exact_sums(conn, "t WHERE id > ?", (0,), ["a", "b"], guards=["b > a"])
        self.assertEqual((sums.totals, sums.guards), ([11, 16], [1]))


class _Admission(_Case):
    """The four admission sums (S1 to S4), through `lease._admit_group` with the real queries."""

    def start(self, budget=None):
        self.store(budget=TOP if budget is None else budget)
        first, second = self.grant(), self.grant()                       # both granted before either settles
        self.leases = (first["lease_id"], second["lease_id"])
        return self.leases

    def settle_both(self, money_amounts=(TOP, 1), tokens=(100, 100)):
        for lease_id, amount, tok in zip(self.leases, money_amounts, tokens, strict=True):
            self.settle(lease_id, NOW + 1000, money=amount, input_tokens=tok)

    def month(self):
        return self.sql("SELECT period_start FROM leases LIMIT 1")[0][0]

    def limit(self, metric, hard, scope="team", scope_id=TEAM):
        self.sql("INSERT INTO limits(id, scope, scope_id, metric, period, window_key, soft, hard) "
                 "VALUES('limit-" + metric + "', ?, ?, ?, 'month', NULL, NULL, ?)", (scope, scope_id, metric, hard))
        return "limit-" + metric

    def admit(self, metric, budget=None, new=1):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            chain = {"platform": lease._PLATFORM_ID, "project": PROJECT, "team": TEAM, "seat": SEAT}
            rows = conn.execute("SELECT * FROM limits").fetchall()
            return lease._admit_group(conn, (metric, "month", None), rows, chain, ACCOUNT, budget, True, new, new, 0, NOW)
        finally:
            conn.close()

    def refused(self, code, *args, **kw):                                  # noqa: D102 - same name, other meaning below
        with self.assertRaises(errors.QuotaError) as caught:
            self.admit(*args, **kw)
        self.assertEqual(caught.exception.code, code)


class AdmissionSums(_Admission):

    def test_the_real_schema_forbids_a_negative_amount_in_every_summed_column(self):
        """Mixed signs cannot be stored: the split sum may rely on it, and still counts one if a store ever has it."""
        self.start()
        self.settle_both(money_amounts=(5, 5))
        for statement in (
                "UPDATE receipts SET actual_micro_eur=-1", "UPDATE receipts SET input_tokens=-1",
                "INSERT INTO holds(lease_id, limit_id, period_start, reserved, settled_actual) VALUES('x','y',1,-1,NULL)",
                "INSERT INTO holds(lease_id, limit_id, period_start, reserved, settled_actual) VALUES('x','y',2,1,-1)"):
            with self.subTest(statement=statement[:40]):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.sql(statement)

    def test_s1_a_scope_total_of_money_receipts_above_the_largest_number_is_money_limit_not_store_error(self):
        self.start()
        self.settle_both()
        self.limit("money_micro_eur", TOP)
        self.refused(errors.CODE_MONEY_LIMIT, "money_micro_eur")

    def test_s1_the_same_sum_for_input_tokens_is_input_limit(self):
        self.start()
        self.settle_both(tokens=(TOP, 1))
        self.limit("input_tokens", TOP)
        self.refused(errors.CODE_INPUT_LIMIT, "input_tokens")

    def test_s1_just_below_the_bound_the_sum_is_exact_and_the_decision_is_the_old_one(self):
        self.start()
        self.settle_both(money_amounts=(TOP - 2, 1))                       # a total of TOP - 1
        self.limit("money_micro_eur", TOP)
        self.assertIsNone(self.admit("money_micro_eur", new=1))            # TOP - 1 + 1 = TOP: not above the cap
        self.refused(errors.CODE_MONEY_LIMIT, "money_micro_eur", new=2)    # one more is

    def test_s2_a_scope_total_of_open_holds_above_the_largest_number_is_refused_not_a_store_error(self):
        self.start()
        limit_id = self.limit("money_micro_eur", TOP)
        for lease_id in self.leases:
            self.sql("INSERT INTO holds(lease_id, limit_id, period_start, reserved, settled_actual) VALUES(?,?,?,?,NULL)",
                     (lease_id, limit_id, self.month(), TOP))
        self.refused(errors.CODE_MONEY_LIMIT, "money_micro_eur")

    def test_s2_settled_actual_is_subtracted_exactly_and_the_rest_is_the_exposure(self):
        self.start()
        limit_id = self.limit("money_micro_eur", 1000)
        for lease_id, reserved, settled in zip(self.leases, (600, 700), (None, 250), strict=True):
            self.sql("INSERT INTO holds(lease_id, limit_id, period_start, reserved, settled_actual) VALUES(?,?,?,?,?)",
                     (lease_id, limit_id, self.month(), reserved, settled))
        # exposure = 600 + (700 - 250) = 1050 > 1000 even before the new hold
        self.refused(errors.CODE_MONEY_LIMIT, "money_micro_eur", new=0)
        self.sql("UPDATE holds SET settled_actual=700 WHERE reserved=700")
        self.assertIsNone(self.admit("money_micro_eur", new=400))          # 600 + 0 + 400 = 1000: not above the cap

    def test_s2_a_hold_settled_above_what_it_reserved_is_a_store_fault_not_a_silent_negative(self):
        self.start()
        limit_id = self.limit("money_micro_eur", TOP)
        self.sql("INSERT INTO holds(lease_id, limit_id, period_start, reserved, settled_actual) VALUES(?,?,?,?,?)",
                 (self.leases[0], limit_id, self.month(), 5, 9))
        self.refused(errors.CODE_STORE_ERROR, "money_micro_eur")

    def test_s3_an_account_total_of_receipts_above_the_largest_number_is_money_limit(self):
        self.start()
        self.settle_both()
        self.refused(errors.CODE_MONEY_LIMIT, "money_micro_eur", budget=TOP)

    def test_s3_normal_sizes_decide_exactly_as_before(self):
        self.start()
        self.settle_both(money_amounts=(450, 50))
        self.assertIsNone(self.admit("money_micro_eur", budget=501, new=1))
        self.assertIsNone(self.admit("money_micro_eur", budget=500, new=0))
        self.refused(errors.CODE_MONEY_LIMIT, "money_micro_eur", budget=500, new=1)

    def test_s4_an_account_total_of_open_holds_above_the_largest_number_is_money_limit(self):
        self.start()
        for lease_id in self.leases:
            self.sql("INSERT INTO holds(lease_id, limit_id, period_start, reserved, settled_actual) VALUES(?,?,?,?,NULL)",
                     (lease_id, lease._account_limit_id(ACCOUNT), self.month(), TOP))
        self.refused(errors.CODE_MONEY_LIMIT, "money_micro_eur", budget=TOP)

    def test_s4_the_account_hold_exposure_subtracts_what_settled_exactly(self):
        self.start()
        for lease_id, reserved, settled in zip(self.leases, (600, 700), (None, 250), strict=True):
            self.sql("INSERT INTO holds(lease_id, limit_id, period_start, reserved, settled_actual) VALUES(?,?,?,?,?)",
                     (lease_id, lease._account_limit_id(ACCOUNT), self.month(), reserved, settled))
        self.refused(errors.CODE_MONEY_LIMIT, "money_micro_eur", budget=1049, new=0)
        self.assertIsNone(self.admit("money_micro_eur", budget=1050, new=0))

    def test_a_row_count_above_the_guard_is_a_store_fault_not_a_wrong_total(self):
        self.start()
        self.settle_both(money_amounts=(1, 1))
        with mock.patch.object(money, "ROW_LIMIT", 1):
            self.refused(errors.CODE_STORE_ERROR, "money_micro_eur", budget=TOP)


class WholeGrant(_Admission):
    """The same corners through a real grant (the enforcing path, opened in a test as the existing tests do)."""

    def enforced_grant(self, now):
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            return self.grant(now)

    def test_an_enforced_grant_after_receipts_that_overflow_the_database_sum_is_money_limit_not_store_error(self):
        self.start()
        self.settle_both()
        with self.assertRaises(errors.QuotaError) as caught:
            self.enforced_grant(NOW + 2000)
        self.assertEqual(caught.exception.code, errors.CODE_MONEY_LIMIT)

    def test_an_enforced_grant_at_normal_sizes_is_admitted_with_a_hold_as_before(self):
        self.start(budget=TOP)
        self.settle_both(money_amounts=(100, 100))
        made = self.enforced_grant(NOW + 2000)
        self.assertEqual(made["enforcement"], "metered")
        self.assertGreater(made["money_hold"], 0)

    def test_the_advisory_check_still_stops_at_the_cap_and_keeps_its_closed_refusal(self):
        self.start()
        self.settle_both()
        self.refused_grant = None
        with self.assertRaises(errors.QuotaError) as caught:
            self.grant(NOW + 2000)
        self.assertEqual(caught.exception.code, errors.CODE_MONEY_LIMIT)

    def test_the_advisory_check_reads_the_receipts_lazily_and_stops_at_the_cap(self):
        self.start(budget=1000)
        self.settle_both(money_amounts=(600, 400))
        seen = {"n": 0}
        real = money.reaches

        def counting(terms, limit):
            def watch():
                for term in terms:
                    seen["n"] += 1
                    yield term
            return real(watch(), limit)
        with mock.patch.object(money, "reaches", counting):
            with self.assertRaises(errors.QuotaError):
                self.grant(NOW + 2000)
        self.assertEqual(seen["n"], 2)


if __name__ == "__main__":
    unittest.main()
