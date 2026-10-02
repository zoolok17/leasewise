"""Card 4 fix-round tests, turn D: final pre-merge read follow-ups.

Run from the repository root:
    python -m unittest discover -s tests -v

Each test pins one finding of the final pre-merge read (the "NOT
READY" follow-up on head 0144e2e):

  test_ttl_24h_boundary              (N1, MAJOR): the absolute maximum
      is 24 HOURS (86_400_000 ms), not 24 days; 24 h is admitted,
      24 h + 1 ms and the old 24-day value are refused.
  test_future_estimate_refused       (N2, MINOR): the estimate
      freshness check is two-sided - an observed_at beyond the +30 s
      clock skew in the future is refused.
  test_conflicting_final_blocks_account /
  test_foreign_gateway_final_blocks  (A-MAJOR 7 part 2, lead ruling):
      a conflicting FINAL receipt parks the account like an overrun
      (card row: "receipt conflict blocks"); a conflicting non-final
      does not.
  test_overrun_adjustment_lease_period (N4): the overrun adjustment
      is stamped with the lease's own grant-time period (the month),
      not the day bucket of a single money/day hold.

The fixture chain and request shapes are the turn-C ones (imported,
identical constants and helpers) so the holds line up: HOLD per
grant.
"""

import hashlib
import os
import sqlite3
import tempfile
import unittest
from unittest import mock
import uuid

from quota_balancer import lease
from quota_balancer import errors
from quota_balancer.errors import QuotaError
from tests.test_card4_turn_c_fix import (
    ACCOUNT,
    GATEWAY2,
    HOLD,
    NOW,
    PROJECT,
    _build_store,
    _grant_request,
    _receipt,
    _rows,
)

# The grant-month and the non-1st grant day (same values as
# test_card4_settle_fix).
MONTH_START = 1788220800000      # 2026-09-01T00:00:00Z
DAY2 = 1788310800000             # 2026-09-02T01:00:00Z (a non-1st day)
DAY_START_0902 = 1788307200000   # 2026-09-02T00:00:00Z


def _assert_quota_error(test, code, fn):
    with test.assertRaises(QuotaError) as ctx:
        fn()
    test.assertEqual(ctx.exception.code, code)
    return ctx.exception


class Card4TurnDFixTest(unittest.TestCase):

    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base,
                                                prefix="quota-card4-fd-")
        self.tmp = self._tmp.name
        # R-003 interim gate (card L1a): this test exercises enforcing
        # metered leases (holds, tokens, settlement), reachable only once
        # card R1's activation gate replaces the interim constant.
        _gate = mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True)
        _gate.start()
        self.addCleanup(_gate.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _store(self, name, budget):
        return _build_store(self.tmp, name, budget)

    def _grant(self, db, now_ms, **overrides):
        return lease.grant(
            db, _grant_request(
                str(uuid.uuid4()), str(uuid.uuid4()),
                hashlib.sha256(
                    ("x" + uuid.uuid4().hex).encode()).hexdigest(),
                **overrides),
            now_ms)

    # --- N1 (MAJOR): the absolute maximum is 24 h -------------------------------

    def test_ttl_24h_boundary(self):
        # Section 6: 24 h absolute maximum = 86_400_000 ms. 24 h is
        # admitted; 24 h + 1 ms, the old 24-day value, and the int64
        # overflow case are refused.
        db = self._store("ttl.db", 10 * HOLD)
        ok = self._grant(db, NOW, ttl_ms=86_400_000)
        self.assertEqual(ok["state"], "active")
        self.assertEqual(ok["expires_at"], NOW + 86_400_000)
        for bad in (86_400_001, 2_073_600_000, 2 ** 63 - 1):
            _assert_quota_error(
                self, errors.CODE_INVALID_REQUEST,
                lambda b=bad: self._grant(db, NOW, ttl_ms=b))

    # --- N2 (MINOR): the freshness check is two-sided ---------------------------

    def test_future_estimate_refused(self):
        # The readings validator's +30 s clock skew: an observed_at at
        # most 30 s in the future is tolerated, anything further (a
        # future-dated estimate) is a stale admission basis.
        db = self._store("future.db", 10 * HOLD)
        ok = self._grant(db, NOW, observed_at=NOW + 30_000)
        self.assertEqual(ok["state"], "active")
        _assert_quota_error(
            self, errors.CODE_INVALID_REQUEST,
            lambda: self._grant(db, NOW, observed_at=NOW + 30_001))
        _assert_quota_error(
            self, errors.CODE_INVALID_REQUEST,
            lambda: self._grant(
                db, NOW, observed_at=NOW + 365 * 86_400_000))

    # --- A-MAJOR 7 part 2 (lead ruling): receipt conflict blocks -----------------

    def _blocks(self, db):
        return _rows(db, "SELECT account_id, model_id, reason, "
                         "source_event_id, state FROM blocks")

    def test_conflicting_final_blocks_account(self):
        # A 2nd, different FINAL on a settled lease is a RECEIPT_CONFLICT
        # AND parks the account (recorded like the overrun park, keyed
        # to the conflicting event); the next grant is refused.
        db = self._store("cblock.db", 10 * HOLD)
        lease_id = self._grant(db, NOW)["lease_id"]
        first = str(uuid.uuid4())
        settled = lease.settle(
            db, _receipt(first, lease_id, HOLD), NOW)
        self.assertEqual(settled["state"], "settled")
        second = str(uuid.uuid4())
        _assert_quota_error(
            self, errors.CODE_RECEIPT_CONFLICT,
            lambda: lease.settle(
                db, _receipt(second, lease_id, 2 * HOLD), NOW))
        # Recorded like the overrun block: one live NULL-model parked
        # block keyed to the conflicting event.
        self.assertEqual(
            self._blocks(db),
            [(ACCOUNT, None, "receipt_conflict", second, "parked")])
        self.assertIn("receipt_conflict_block",
                      [r[0] for r in _rows(db,
                                           "SELECT action FROM audit")])
        # The settled liability is untouched.
        self.assertEqual(_rows(db, "SELECT state FROM leases"),
                         [("settled",)])
        _assert_quota_error(
            self, errors.CODE_ACCOUNT_PARKED,
            lambda: self._grant(db, NOW))

    def test_foreign_gateway_final_blocks(self):
        # The foreign-gateway conflict is a conflicting final too: it
        # parks as well. A conflicting NON-final is refused but parks
        # nothing (the ruling covers finals).
        db = self._store("fgblock.db", 10 * HOLD)
        lease_id = self._grant(db, NOW)["lease_id"]
        _assert_quota_error(
            self, errors.CODE_RECEIPT_CONFLICT,
            lambda: lease.settle(
                db, _receipt(str(uuid.uuid4()), lease_id, None,
                             final=0, gateway_id=GATEWAY2), NOW))
        self.assertEqual(self._blocks(db), [])
        event = str(uuid.uuid4())
        _assert_quota_error(
            self, errors.CODE_RECEIPT_CONFLICT,
            lambda: lease.settle(
                db, _receipt(event, lease_id, 10, gateway_id=GATEWAY2),
                NOW))
        self.assertEqual(
            self._blocks(db),
            [(ACCOUNT, None, "receipt_conflict", event, "parked")])
        _assert_quota_error(
            self, errors.CODE_ACCOUNT_PARKED,
            lambda: self._grant(db, NOW))

    # --- N4: the adjustment takes the lease's period ----------------------------

    def test_overrun_adjustment_lease_period(self):
        # A single day-bucket money hold: the account is a
        # subscription (no account-month hold) and the project cap is
        # money/DAY. The overrun adjustment is stamped with the
        # lease's own grant-time period (the month), not the hold's
        # day bucket.
        db = self._store("dayover.db", 10 * HOLD)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "UPDATE accounts SET kind='subscription', "
                "budget_micro_eur=NULL WHERE id=?", (ACCOUNT,))
            conn.execute(
                "INSERT INTO limits(id, scope, scope_id, metric, period, "
                "window_key, soft, hard) VALUES(?,?,?,?,?,?,?,?)",
                ("lim-day", "project", PROJECT, "money_micro_eur", "day",
                 None, None, HOLD + 1000))
            conn.commit()
        finally:
            conn.close()
        result = self._grant(db, DAY2, observed_at=DAY2 - 1000)
        event = str(uuid.uuid4())
        settled = lease.settle(
            db, _receipt(event, result["lease_id"], HOLD + 500,
                         at=DAY2), DAY2)
        self.assertEqual(settled["state"], "settled")
        self.assertEqual(
            _rows(db, "SELECT account_id, source_event_id, period_start,"
                      " unattributed_micro_eur, reason, resolved_at "
                      "FROM adjustments"),
            [(ACCOUNT, event, MONTH_START, 500, "overrun", None)])


if __name__ == "__main__":
    unittest.main()
