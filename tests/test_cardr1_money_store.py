"""Card R1-M, step R1-M3: the gateway shares are added exactly (S5 to S7 of DESIGN-R1-MONEY.md).

A gateway's share is the largest number the store keeps at most, and several gateways of one account can add up to more than that.
The database's own `SUM` then stops with an error, which came out as `STORE_ERROR` in three places:

* S5, importing a billing policy (`store._apply_billing_policies`): the allocation check "shares + allowance <= cap";
* S6, ingesting a provider reading (`store._reduce_provider_billing`): the same check, again, against the current config;
* S7, `status`: the account's allocated total.

The shares are now added by `money.exact_sum`. The total is an ordinary (huge) number: the allocation check refuses it as too large
(`INVALID_CONFIG`, the existing sentence), and `status` shows it exactly with `allocated_above_int64: true` (a key that is absent for
every ordinary account). Results at normal sizes are unchanged. Synthetic data only; the CLI runs against a temporary store.
"""

import json
import sqlite3
import unittest

from quota_balancer import money

from tests.test_card2b1 import (ERROR_INVALID_CONFIG, ERROR_STORE_FAILED, _Base, _billing, _billing_policy, _billing_policy_import,
                                _doc_v3, _eid, _month_bounds_of, _now_ms, _policy_doc, _readings_import, run_cli)

TOP = 2 ** 63 - 1


class _Shares(_Base):

    def set_shares(self, first, second):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE gateways SET share_micro_eur=? WHERE alias='g1'", (first,))
            conn.execute("UPDATE gateways SET share_micro_eur=? WHERE alias='g2'", (second,))
            conn.commit()
        finally:
            conn.close()

    def status(self):
        result = run_cli("--db", self.db, "status", "--json")
        return result, (json.loads(result.stdout) if result.returncode == 0 else None)

    def account(self):
        result, doc = self.status()
        self.assertEqual(result.returncode, 0, result.stderr)
        return next(entry for entry in doc["accounts"] if entry["alias"] == "meter")

    def policy_import(self):
        return _billing_policy_import(self.db, self.tmp, _policy_doc([_billing_policy()]))

    def reading(self, label="r"):
        observed = _now_ms() - 1000
        start, end = _month_bounds_of(observed)
        return _doc_v3([_billing(_eid(label), "desktop", "meter", "proj-ai", 1, start, end, 100, False, observed)])


class PolicyImport(_Shares):

    def test_s5_shares_that_add_up_above_the_largest_number_are_invalid_config_not_a_store_fault(self):
        self.set_shares(TOP, TOP)
        result = self.policy_import()
        self.assertEqual((result.returncode, result.stderr.strip()), (2, ERROR_INVALID_CONFIG))
        self.assertNotEqual(result.stderr.strip(), ERROR_STORE_FAILED)
        self.assertEqual(self.sql_count("billing_policies"), 0)                       # nothing was written

    def test_s5_shares_that_together_are_exactly_the_largest_number_are_checked_against_the_cap_as_numbers(self):
        self.set_shares(2 ** 62, 2 ** 62 - 1)                                         # a total of TOP: still a number
        result = self.policy_import()
        self.assertEqual((result.returncode, result.stderr.strip()), (2, ERROR_INVALID_CONFIG))   # above the 100 EUR cap

    def test_s5_normal_shares_import_exactly_as_before(self):
        self.assertEqual(self.policy_import().returncode, 0)                          # 60 + 40 = the 100 EUR cap, exactly
        self.assertEqual(self.sql_count("billing_policies"), 1)

    def test_s5_the_boundary_at_the_cap_is_unchanged(self):
        self.set_shares(60_000_000, 40_000_001)                                       # one micro-EUR above the cap
        self.assertEqual(self.policy_import().returncode, 2)

    def sql_count(self, table):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
        finally:
            conn.close()


class Ingest(_Shares):

    def test_s6_a_reading_after_the_shares_grew_past_the_largest_number_is_refused_as_invalid_config_not_a_store_fault(self):
        self.assertEqual(self.policy_import().returncode, 0)                          # a policy exists while the shares are normal
        self.set_shares(TOP, TOP)                                                     # then the config moves (a restore, a hand edit)
        result = _readings_import(self.db, self.tmp, self.reading())
        self.assertEqual((result.returncode, result.stderr.strip()), (2, ERROR_INVALID_CONFIG))

    def test_s6_a_normal_reading_is_accepted_exactly_as_before(self):
        self.assertEqual(self.policy_import().returncode, 0)
        self.assertEqual(_readings_import(self.db, self.tmp, self.reading()).returncode, 0)


class Inventory(_Shares):

    def test_s7_the_allocated_total_above_the_largest_number_is_exact_and_flagged(self):
        self.set_shares(TOP, TOP)
        entry = self.account()
        self.assertEqual(entry["allocated_micro_eur"], 2 * TOP)
        self.assertIs(entry["allocated_above_int64"], True)

    def test_s7_a_total_of_exactly_two_to_the_63_is_flagged_and_one_less_is_not(self):
        self.set_shares(TOP, 1)
        entry = self.account()
        self.assertEqual((entry["allocated_micro_eur"], entry.get("allocated_above_int64")), (2 ** 63, True))
        self.set_shares(TOP - 1, 1)
        entry = self.account()
        self.assertEqual(entry["allocated_micro_eur"], TOP)
        self.assertNotIn("allocated_above_int64", entry)

    def test_s7_an_ordinary_account_has_exactly_the_old_keys_and_the_old_figure(self):
        entry = self.account()
        self.assertEqual(entry["allocated_micro_eur"], 100_000_000)
        self.assertEqual(set(entry), {"alias", "kind", "budget_micro_eur", "allocated_micro_eur", "coverage_complete", "windows"})

    def test_s7_an_account_with_no_gateway_allocates_zero(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("DELETE FROM gateways")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.account()["allocated_micro_eur"], 0)

    def test_the_helper_refuses_a_store_that_breaks_its_own_rule_with_a_closed_error(self):
        from quota_balancer import errors, store
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE gateways(account_id TEXT, share_micro_eur INTEGER)")
            conn.execute("INSERT INTO gateways VALUES('a', -1)")                      # no CHECK here: a damaged store
            with self.assertRaises(errors.QuotaError) as caught:
                store._allocated(conn, "a")
            self.assertEqual(caught.exception.code, errors.CODE_STORE_ERROR)
        finally:
            conn.close()
        self.assertEqual(money.INT64_MAX, TOP)


if __name__ == "__main__":
    unittest.main()
