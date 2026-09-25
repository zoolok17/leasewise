"""Card 4 fix-round tests, turn A: settlement and usage accounting.

Run from the repository root:
    python -m unittest discover -s tests -v

Each test pins one fix-round finding (READ A / READ B cold reads of
head 811fae3):

  test_settled_lease_releases_hold_headroom   (READ B B1 / READ A BLOCKER 1):
      after settle, admission counts the ACTUAL, not the full
      reservation - a 4th grant fits once 3 leases settle small.
  test_day_limit_counts_settled_spend         (READ B B2 / READ A MAJOR 3):
      a (money, day) cap counts settled spend on the grant day, not
      only on the 1st of the month.
  test_overrun_two_money_holds_one_adjustment (READ A BLOCKER 2 / READ B M1):
      a final receipt above the holds on a lease with two money holds
      records exactly ONE adjustments row (source_event_id is UNIQUE),
      not one per hold.
  test_overrun_blocks_account                 (READ A MAJOR 7):
      an observed money overrun parks the account - the next grant on
      it refuses with ACCOUNT_PARKED until reconciliation.

The fixture chain and request shapes mirror tests/test_card4_ops.py so
the holds line up: HOLD per grant.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
import uuid

import quota_balancer.migrate as migrate
from quota_balancer import lease, policy
from quota_balancer import errors
from quota_balancer.errors import QuotaError

# --- fixed UUID fixture chain (same as turns 1-2) --------------------------------

ACCOUNT = "11111111-1111-4111-8111-111111111111"
MACHINE = "22222222-2222-4222-8222-222222222222"
GATEWAY = "33333333-3333-4333-8333-333333333333"
PROJECT = "44444444-4444-4444-8444-444444444444"
TEAM = "55555555-5555-4555-8555-555555555555"
SEAT = "66666666-6666-4666-8666-666666666666"
MODEL = "77777777-7777-4777-8777-777777777777"
POLICY_HASH = "ab" * 32

# 2026-09-01T01:00:00Z: the grant time (one hour into the month and the
# day - a non-1st-day check would need the 2nd).
NOW = 1788224400000
MONTH_START = 1788220800000      # 2026-09-01T00:00:00Z
DAY_START_0901 = 1788220800000   # the grant day
DAY2 = 1788310800000             # 2026-09-02T01:00:00Z (a non-1st day)
DAY_START_0902 = 1788307200000   # 2026-09-02T00:00:00Z
TTL_MS = 1800000

MODEL_DICT = {
    "alias": "opus",
    "context_ceiling": 180000,
    "output_ceiling": 18000,
    "tariff_input_rate": 1,
    "tariff_output_rate": 1,
    "reserve_input_rate": 1000000,
    "reserve_output_rate": 1000000,
    "reservation_mode": "sized",
    "policy_hash": POLICY_HASH,
    "effort_aliases": "opus",
}
EST = policy.estimate(1000, 1, 0, 1000, 2, MODEL_DICT)
HOLD = EST["hold_sized"]


def _grant_request(request_id, turn_id, request_hash, observed_at=NOW - 1000):
    return {
        "request_id": request_id,
        "request_hash": request_hash,
        "turn_id": turn_id,
        "seat_id": SEAT,
        "model_id": MODEL,
        "effort": "opus",
        "gateway_id": GATEWAY,
        "parent_lease_id": None,
        "context": {
            "mode": "FRESH",
            "initial_tokens": 1000,
            "dispatch_tokens": 0,
            "source": "tokenizer",
            "observed_at": observed_at,
            "growth_tokens_per_call": 0,
        },
        "expected_calls": 1,
        "max_calls": 2,
        "output_cap": 1000,
        "per_call_output_cap": 500,
        "ttl_ms": TTL_MS,
    }


def _receipt(event_id, lease_id, actual, final=1, input_tokens=500,
             outcome="completed", at=NOW):
    return {
        "event_id": event_id,
        "lease_id": lease_id,
        "outcome": outcome,
        "calls": 1,
        "input_tokens": input_tokens,
        "cached_input_tokens": 0,
        "output_tokens": 100,
        "actual_micro_eur": actual,
        "gateway_id": GATEWAY,
        "seq": 1,
        "final": final,
        "observed_at": at,
        "received_at": at,
    }


def _build_store(tmp, name, budget):
    db = os.path.join(tmp, name)
    conn = sqlite3.connect(db)
    try:
        for script in (migrate.MIGRATION_V1, migrate.MIGRATION_V2,
                       migrate.MIGRATION_V3, migrate.MIGRATION_V4,
                       migrate.MIGRATION_V5):
            conn.executescript(script)
        conn.execute(
            "INSERT INTO accounts(id, alias, vendor, kind, budget_micro_eur)"
            " VALUES(?,?,?,?,?)",
            (ACCOUNT, "meter", "ovh", "metered", budget))
        conn.execute("INSERT INTO machines(id, alias) VALUES(?,?)",
                     (MACHINE, "desktop"))
        conn.execute(
            "INSERT INTO gateways(id, alias, account_id, machine_id, "
            "share_micro_eur, period) VALUES(?,?,?,?,?,?)",
            (GATEWAY, "g1", ACCOUNT, MACHINE, 100_000_000, "utc_month"))
        conn.execute("UPDATE gateways SET sized_ready=1 WHERE id=?",
                     (GATEWAY,))
        conn.execute("INSERT INTO projects(id, alias) VALUES(?,?)",
                     (PROJECT, "org"))
        # Project authorization: the project is given the fixture
        # account (section 7).
        conn.execute(
            "INSERT INTO project_accounts(project_id, account_id) "
            "VALUES(?,?)", (PROJECT, ACCOUNT))
        conn.execute("INSERT INTO teams(id, project_id, alias) "
                     "VALUES(?,?,?)", (TEAM, PROJECT, "dev"))
        conn.execute(
            "INSERT INTO seats(id, team_id, machine_id, alias, role, "
            "enforcement) VALUES(?,?,?,?,?,?)",
            (SEAT, TEAM, MACHINE, "s1", "main", "metered"))
        conn.execute(
            "INSERT INTO models(id, account_id, alias, context_ceiling, "
            "output_ceiling, tariff_input_rate, tariff_output_rate, "
            "reserve_input_rate, reserve_output_rate, reservation_mode, "
            "policy_hash, effort_aliases) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (MODEL, ACCOUNT, "opus", 180000, 18000, 1, 1, 1000000,
             1000000, "sized", POLICY_HASH, "opus"))
        conn.commit()
    finally:
        conn.close()
    return db


def _rows(db, sql, params=()):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _assert_quota_error(test, code, fn):
    with test.assertRaises(QuotaError) as ctx:
        fn()
    test.assertEqual(ctx.exception.code, code)
    return ctx.exception


class Card4SettleFixTest(unittest.TestCase):

    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base,
                                                prefix="quota-card4-fa-")
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _store(self, name, budget):
        return _build_store(self.tmp, name, budget)

    def _grant(self, db, now_ms):
        return lease.grant(
            db, _grant_request(
                str(uuid.uuid4()), str(uuid.uuid4()),
                hashlib.sha256(
                    ("x" + uuid.uuid4().hex).encode()).hexdigest(),
                observed_at=now_ms - 1000),
            now_ms)

    # --- READ B B1 / READ A BLOCKER 1 ---------------------------------------------

    def test_settled_lease_releases_hold_headroom(self):
        # Budget fits exactly 3 holds. Three leases settle with small
        # actuals: the released headroom returns, so a 4th grant fits.
        db = self._store("release.db", 3 * HOLD)
        ids = [self._grant(db, NOW)["lease_id"] for _ in range(3)]
        actual = HOLD // 10
        for lease_id in ids:
            settled = lease.settle(
                db, _receipt(str(uuid.uuid4()), lease_id, actual), NOW)
            self.assertEqual(settled["state"], "settled")
            self.assertEqual(settled["released_micro_eur"], HOLD - actual)
        # Usage is 3*actual settled + 0 unresolved holds: the 4th grant
        # (3*actual + HOLD <= 3*HOLD) must be admitted.
        fourth = self._grant(db, NOW)
        self.assertEqual(fourth["state"], "active")

    # --- READ B B2 / READ A MAJOR 3 ------------------------------------------------

    def test_day_limit_counts_settled_spend(self):
        # A (money, day) cap on a NON-1st day: settled spend of a lease
        # granted that day counts against the day cap.
        db = self._store("daycap.db", 10 * HOLD)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO limits(id, scope, scope_id, metric, period, "
                "window_key, soft, hard) VALUES(?,?,?,?,?,?,?,?)",
                ("lim-day", "project", PROJECT, "money_micro_eur", "day",
                 None, None, HOLD + 1000))
            conn.commit()
        finally:
            conn.close()
        first = self._grant(db, DAY2)
        lease.settle(db, _receipt(str(uuid.uuid4()),
                                  first["lease_id"], HOLD, at=DAY2), DAY2)
        # The day now carries HOLD settled: a second grant (HOLD) the
        # same day must be refused (2*HOLD > HOLD + 1000).
        _assert_quota_error(
            self, errors.CODE_MONEY_LIMIT, lambda: self._grant(db, DAY2))
        # ...and the day-limit hold settled against the GRANT day, not
        # the month start.
        self.assertEqual(
            _rows(db, "SELECT period_start, settled_actual FROM holds "
                      "WHERE limit_id='lim-day'"),
            [(DAY_START_0902, HOLD)])

    # --- READ A BLOCKER 2 / READ B M1 ----------------------------------------------

    def test_overrun_two_money_holds_one_adjustment(self):
        # Two money holds (account budget + a project money/month
        # limit): an overrun final receipt records ONE adjustments row,
        # not one per hold (source_event_id is UNIQUE).
        db = self._store("twohold.db", 10 * HOLD)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO limits(id, scope, scope_id, metric, period, "
                "window_key, soft, hard) VALUES(?,?,?,?,?,?,?,?)",
                ("lim-month", "project", PROJECT, "money_micro_eur",
                 "month", None, None, 10 * HOLD))
            conn.commit()
        finally:
            conn.close()
        result = self._grant(db, NOW)
        lease_id = result["lease_id"]
        # Two money holds: the account budget and the project month.
        self.assertEqual(
            sorted(r[0] for r in _rows(db, "SELECT limit_id FROM holds")),
            sorted(["account:" + ACCOUNT, "lim-month"]))
        settled = lease.settle(
            db, _receipt(str(uuid.uuid4()), lease_id, HOLD + 500), NOW)
        self.assertEqual(settled["state"], "settled")
        # The response is per lease: the lease's own hold (the 500
        # overrun is the adjustments row below, not retained).
        self.assertEqual(settled["retained_micro_eur"], HOLD)
        # Exactly one adjustment: the max overrun over the money holds,
        # in the original period.
        self.assertEqual(
            _rows(db, "SELECT period_start, unattributed_micro_eur, "
                      "reason, resolved_at FROM adjustments"),
            [(MONTH_START, 500, "overrun", None)])

    # --- READ A MAJOR 7 -------------------------------------------------------------

    def test_overrun_blocks_account(self):
        # Section 4: "Observed overrun records actual liability ...
        # blocks further work and needs reconciliation." A money
        # overrun parks the account; the next grant refuses.
        db = self._store("block.db", 10 * HOLD)
        result = self._grant(db, NOW)
        lease.settle(db, _receipt(str(uuid.uuid4()),
                                  result["lease_id"], HOLD + 500), NOW)
        self.assertEqual(
            _rows(db, "SELECT reason, state, retry_at, model_id, "
                      "account_id FROM blocks"),
            [("overrun", "parked", None, None, ACCOUNT)])
        _assert_quota_error(
            self, errors.CODE_ACCOUNT_PARKED, lambda: self._grant(db, NOW))


if __name__ == "__main__":
    unittest.main()
