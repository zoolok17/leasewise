"""Card R1, step R1-2: the expiry clamp, the advisory budget and the enforcement proof (DESIGN-R1 12.3, 6.3, 11.6).

* m3: a lease with a gateway that is not shadow never lives past the start of the next UTC month
  (`expires_at = min(now + ttl, next month start)`). A replay returns the stored deadline; a late final is accepted
  after the rollover. Subscription and shadow leases are not clamped.
* m7: a `metered_advisory` lease is refused `MONEY_LIMIT` when the account's KNOWN final receipt money for the month
  is at or above its cap. Holds are not counted, a null-money final counts as nothing, no money limit row is needed,
  and `INPUT_USAGE_UNKNOWN` keeps its own place after it.
* The enforcement proof: while `METERED_ENFORCEMENT_ACTIVATED` is false a metered seat on a ready gateway is always
  granted `metered_advisory`, with no money hold, no holds and no lease token.
* N5, N3 and m4 stay closed (regression tests).
Synthetic data only.
"""

import datetime
import os
import sqlite3
import tempfile
import unittest
import uuid
from unittest import mock

from quota_balancer import errors, lease

from tests.test_card4_seat_kind_fix import GATEWAY, NOW, PROJECT, TTL_MS, _build_store, _grant_request
from tests.test_cardl1a_t3 import _T3


def month_start(year, month):
    return int(datetime.datetime(year, month, 1, tzinfo=datetime.timezone.utc).timestamp()) * 1000


SEPTEMBER = month_start(2026, 9)
OCTOBER = month_start(2026, 10)
DECEMBER = month_start(2026, 12)
JANUARY = month_start(2027, 1)


class _Case(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-r1e-")
        self.addCleanup(self._tmp.cleanup)

    def store(self, kind="metered", mode="metered", budget=None):
        folder = tempfile.mkdtemp(dir=self._tmp.name)
        self.db = _build_store(folder, kind, mode)
        if budget is not None:
            self.sql("UPDATE accounts SET budget_micro_eur=?", (budget,))
        return self.db

    def sql(self, text, params=()):
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(text, params).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def request(self, now, gateway=GATEWAY, ttl=TTL_MS):
        request = _grant_request(gateway)
        request["context"]["observed_at"] = now - 1000
        request["ttl_ms"] = ttl
        return request

    def grant(self, now=NOW, **over):
        request = self.request(now, **over)
        return lease.grant(self.db, request, now)

    def settle(self, lease_id, now, money=450, input_tokens=100, final=True):
        return lease.settle(self.db, {
            "event_id": str(uuid.uuid4()), "lease_id": lease_id, "outcome": "completed", "calls": 1,
            "input_tokens": input_tokens, "cached_input_tokens": 0, "output_tokens": 10, "actual_micro_eur": money,
            "gateway_id": GATEWAY, "seq": 1, "final": 1 if final else 0, "observed_at": now, "received_at": now}, now)

    def refused(self, code, now=NOW):
        with self.assertRaises(errors.QuotaError) as caught:
            self.grant(now)
        self.assertEqual(caught.exception.code, code)


class Clamp(_Case):

    def test_a_grant_at_exact_midnight_utc_is_not_clamped_and_belongs_to_the_new_month(self):
        self.store()
        made = self.grant(OCTOBER)
        self.assertEqual(made["expires_at"], OCTOBER + TTL_MS)
        self.assertEqual(self.sql("SELECT period_start FROM leases"), [(OCTOBER,)])

    def test_a_grant_in_the_last_millisecond_of_a_month_expires_at_the_rollover(self):
        self.store()
        made = self.grant(OCTOBER - 1)
        self.assertEqual(made["expires_at"], OCTOBER)
        self.assertEqual(self.sql("SELECT expires_at, period_start FROM leases"), [(OCTOBER, SEPTEMBER)])

    def test_a_grant_a_few_minutes_before_the_rollover_is_clamped_to_it(self):
        self.store()
        self.assertEqual(self.grant(OCTOBER - 600_000)["expires_at"], OCTOBER)

    def test_a_grant_whose_deadline_is_before_the_rollover_is_not_changed(self):
        self.store()
        self.assertEqual(self.grant(OCTOBER - TTL_MS - 1)["expires_at"], OCTOBER - 1)
        self.assertEqual(self.grant(OCTOBER - TTL_MS)["expires_at"], OCTOBER)       # exactly at the rollover

    def test_december_rolls_into_the_next_year(self):
        self.store()
        self.assertEqual(self.grant(JANUARY - 1000)["expires_at"], JANUARY)

    def test_a_replay_on_either_side_of_the_rollover_returns_the_stored_deadline(self):
        self.store()
        request = self.request(OCTOBER - 1000)
        first = lease.grant(self.db, dict(request), OCTOBER - 1000)
        before = lease.grant(self.db, dict(request), OCTOBER - 500)
        after = lease.grant(self.db, dict(request), OCTOBER + 500)
        self.assertEqual((first["expires_at"], before["expires_at"], after["expires_at"]), (OCTOBER,) * 3)
        self.assertTrue(before["replayed"] and after["replayed"])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM leases"), [(1,)])

    def test_a_late_final_after_the_rollover_is_accepted_and_keeps_its_original_month(self):
        self.store()
        made = self.grant(OCTOBER - 1000)
        self.settle(made["lease_id"], OCTOBER + 600_000)
        self.assertEqual(self.sql("SELECT state, period_start FROM leases"), [("settled", SEPTEMBER)])

    def test_expiry_at_the_rollover_does_not_release_anything_by_itself(self):
        """Expiry only marks a lease uncertain-or-expired through the sweep; the clamp stores a date and nothing else."""
        self.store()
        self.grant(OCTOBER - 1000)
        self.assertEqual(self.sql("SELECT state FROM leases"), [("active",)])

    def test_a_shadow_request_carrying_a_gateway_is_not_clamped(self):
        self.store("metered", "shadow")
        made = self.grant(OCTOBER - 1000)
        self.assertEqual((made["enforcement"], made["expires_at"]), ("shadow", OCTOBER - 1000 + TTL_MS))

    def test_a_subscription_lease_has_no_gateway_and_is_not_clamped(self):
        self.store("subscription", "subscription_admission")
        made = self.grant(OCTOBER - 1000, gateway=None)
        self.assertEqual(made["expires_at"], OCTOBER - 1000 + TTL_MS)

    def test_an_enforcing_lease_is_clamped_too_while_the_gate_is_open_in_a_test(self):
        self.store()
        self.sql("UPDATE gateways SET sized_ready=1")
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            made = self.grant(OCTOBER - 1000)
        self.assertEqual((made["enforcement"], made["expires_at"]), ("metered", OCTOBER))


class AdvisoryBudget(_Case):
    """m7: refused MONEY_LIMIT at or above the cap, on KNOWN final receipt money only."""

    SPEND = 450

    def spend(self, money=SPEND, **over):
        made = self.grant()
        self.settle(made["lease_id"], NOW + 1000, money=money, **over)

    def test_at_cap_minus_one_the_grant_is_admitted_at_the_cap_and_above_it_refused(self):
        self.store(budget=10_000_000)
        self.spend()
        for budget, admitted in ((self.SPEND + 1, True), (self.SPEND, False), (self.SPEND - 1, False)):
            with self.subTest(budget=budget):
                self.sql("UPDATE accounts SET budget_micro_eur=?", (budget,))
                if admitted:
                    self.assertEqual(self.grant(NOW + 2000)["enforcement"], "metered_advisory")
                else:
                    self.refused(errors.CODE_MONEY_LIMIT, NOW + 2000)

    def test_an_account_with_no_spend_is_never_refused(self):
        self.store(budget=1)
        self.assertEqual(self.grant()["enforcement"], "metered_advisory")

    def test_it_needs_no_money_limit_row(self):
        self.store(budget=10_000_000)
        self.spend()
        self.sql("UPDATE accounts SET budget_micro_eur=?", (self.SPEND,))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM limits WHERE metric='money_micro_eur'"), [(0,)])
        self.refused(errors.CODE_MONEY_LIMIT, NOW + 2000)

    def test_holds_of_legacy_enforcing_leases_are_not_counted(self):
        self.store(budget=10_000_000)
        self.sql("UPDATE gateways SET sized_ready=1")
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            held = self.grant()
        self.assertEqual(held["enforcement"], "metered")
        self.sql("UPDATE accounts SET budget_micro_eur=?", (held["money_hold"],))      # holds alone reach the cap
        self.assertTrue(self.sql("SELECT 1 FROM holds"))
        self.assertEqual(self.grant(NOW + 1000)["enforcement"], "metered_advisory")

    def test_a_final_with_unknown_money_counts_as_nothing_and_a_known_one_counts(self):
        self.store(budget=100)
        made = self.grant()
        self.settle(made["lease_id"], NOW + 1000, money=None)
        self.assertEqual(self.grant(NOW + 2000)["enforcement"], "metered_advisory")      # unknown money: not counted
        self.spend(money=100)
        self.refused(errors.CODE_MONEY_LIMIT, NOW + 3000)

    def test_a_non_final_report_and_a_shadow_lease_do_not_count(self):
        self.store(budget=100)
        made = self.grant()
        self.settle(made["lease_id"], NOW + 1000, money=500, final=False)
        self.assertEqual(self.grant(NOW + 2000)["enforcement"], "metered_advisory")

    def test_a_replay_of_an_earlier_grant_is_not_refused_when_the_cap_is_reached_later(self):
        self.store(budget=10_000_000)
        request = self.request(NOW)
        first = lease.grant(self.db, dict(request), NOW)
        self.spend()
        self.sql("UPDATE accounts SET budget_micro_eur=?", (self.SPEND,))
        again = lease.grant(self.db, dict(request), NOW + 5000)
        self.assertEqual((again["replayed"], again["lease_id"]), (True, first["lease_id"]))

    def test_the_budget_is_the_months_own(self):
        self.store(budget=self.SPEND)
        self.spend()
        self.refused(errors.CODE_MONEY_LIMIT, NOW + 2000)
        self.assertEqual(self.grant(OCTOBER + 1000)["enforcement"], "metered_advisory")      # a new month starts clean

    def test_a_subscription_account_and_a_shadow_seat_are_not_asked_about_money(self):
        self.store("metered", "shadow", budget=1)
        self.assertEqual(self.grant()["enforcement"], "shadow")

    def test_money_limit_comes_before_the_unknown_input_wait_and_the_wait_is_unchanged_below_the_cap(self):
        self.store(budget=10_000_000)
        self.sql("INSERT INTO limits(id, scope, scope_id, metric, period, window_key, soft, hard)"
                 " VALUES(?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), "project", PROJECT, "input_tokens", "month", None,
                                               None, 10 ** 9))
        made = self.grant()
        self.settle(made["lease_id"], NOW + 1000, money=self.SPEND, input_tokens=None)
        self.refused(errors.CODE_INPUT_USAGE_UNKNOWN, NOW + 2000)                       # below the cap: the old rule
        self.sql("UPDATE accounts SET budget_micro_eur=?", (self.SPEND,))
        self.refused(errors.CODE_MONEY_LIMIT, NOW + 2000)                               # at the cap: money first


class EnforcementProof(_Case):
    """11.6: paid enforcement is impossible today. (Earlier proofs: `test_cardl1a.R003MeteredNeverEnforcing`.)"""

    def test_a_metered_seat_on_a_ready_gateway_is_granted_advisory_with_no_hold_and_no_token(self):
        self.store()
        self.sql("UPDATE gateways SET sized_ready=1")
        self.assertIs(lease.METERED_ENFORCEMENT_ACTIVATED, False)
        made = self.grant()
        self.assertEqual((made["enforcement"], made["money_hold"]), ("metered_advisory", None))
        self.assertEqual(self.sql("SELECT token_hash, money_hold, enforcement FROM leases"),
                         [(None, None, "metered_advisory")])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM holds"), [(0,)])
        self.assertFalse(lease.token_bearing(made["enforcement"]))

    def test_it_stays_advisory_for_every_grant_however_often_it_is_asked(self):
        self.store()
        self.sql("UPDATE gateways SET sized_ready=1")
        for number in range(3):
            self.assertEqual(self.grant(NOW + number)["enforcement"], "metered_advisory")


class Closures(_Case):
    """N5, N3 and m4 are closed; these tests keep them pinned where R1 touches the same code."""

    def test_n5_a_replay_still_returns_its_lease_after_the_project_loses_the_account(self):
        self.store()
        request = self.request(NOW)
        first = lease.grant(self.db, dict(request), NOW)
        self.sql("DELETE FROM project_accounts")
        again = lease.grant(self.db, dict(request), NOW + 10)
        self.assertEqual((again["replayed"], again["lease_id"]), (True, first["lease_id"]))
        with self.assertRaises(errors.QuotaError) as caught:                      # a NEW request is refused
            lease.grant(self.db, self.request(NOW), NOW)
        self.assertEqual(caught.exception.code, errors.CODE_POLICY_DENIED)

    def test_m4_a_subscription_seat_on_a_metered_account_is_refused(self):
        for mode in ("subscription_admission", "subscription_warn"):
            with self.subTest(mode=mode):
                self.store("metered", mode)
                with self.assertRaises(errors.QuotaError) as caught:
                    self.grant(gateway=None)
                self.assertEqual(caught.exception.code, errors.CODE_POLICY_DENIED)
                self.assertEqual(self.sql("SELECT COUNT(*) FROM leases"), [(0,)])


class N3(_T3):
    """N3: an overrun turns a live probing block `parked` (an unreconciled overrun is not cleared by a probe)."""

    def test_an_overrun_turns_a_probing_block_parked(self):
        first = self.enforcing_lease()
        self.limit_block()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE blocks SET state='probing', updated_at=1")
        conn.commit()
        conn.close()
        self.overrun(first["lease_id"], now_ms=self.now())
        self.assertEqual([row[3] for row in self.blocks()], ["parked"])


if __name__ == "__main__":
    unittest.main()
