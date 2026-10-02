"""Card 4 fix-round tests, turn B: enforcement and binding.

Run from the repository root:
    python -m unittest discover -s tests -v

Each test pins one fix-round finding (READ A / READ B cold reads of
head 7d78204):

  test_platform_limit_enforced           (READ B B3):
      a platform (money, month) cap is part of the admission chain:
      a hard=1 singleton refuses a sized grant.
  test_foreign_account_rejected          (READ A MAJOR 5):
      a model whose account the seat's project has not authorized is
      refused (POLICY_DENIED), not admitted.
  test_foreign_gateway_rejected          (READ A MAJOR 5):
      a gateway of another account/machine cannot bind (much less
      upgrade the enforcement of) the lease.
  test_replay_changed_content_conflicts  (READ B M2):
      the idempotency content hash is computed server-side: same
      request id + changed content is IDEMPOTENCY_CONFLICT, same
      id + same content still replays.
  test_advisory_settle_null_money        (READ A MAJOR 4):
      a lease that reserved no money (metered_advisory) settles final
      with money=None instead of INVALID_REQUEST.
  test_ttl_ms_bounded                    (READ A MAJOR 6):
      ttl_ms above the 24h absolute maximum (and the int64 overflow
      case) is a static INVALID_REQUEST, not a raw OverflowError.

The fixture chain and request shapes mirror tests/test_card4_settle_fix.py
so the holds line up: HOLD per grant.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock
import uuid

import quota_balancer.migrate as migrate
from quota_balancer import lease, policy
from quota_balancer import errors
from quota_balancer.errors import QuotaError

# --- fixed UUID fixture chain (same as turns 1-3) --------------------------------

ACCOUNT = "11111111-1111-4111-8111-111111111111"
MACHINE = "22222222-2222-4222-8222-222222222222"
GATEWAY = "33333333-3333-4333-8333-333333333333"
PROJECT = "44444444-4444-4444-8444-444444444444"
TEAM = "55555555-5555-4555-8555-555555555555"
SEAT = "66666666-6666-4666-8666-666666666666"
MODEL = "77777777-7777-4777-8777-777777777777"
# The reserved platform singleton scope id (must match
# lease._PLATFORM_ID): platform caps are the scope='platform' limits
# rows carried by it; the scope spans every seat.
PLATFORM = "00000000-0000-0000-0000-000000000000"
# Foreign account/machine pair: authorized to nothing in this project.
ACCOUNT2 = "88888888-8888-4888-8888-888888888888"
MODEL2 = "99999999-9999-4999-8999-999999999999"
GATEWAY2 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
# An advisory seat: reserves no money, settles with money=None.
SEAT2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
POLICY_HASH = "ab" * 32

# 2026-09-01T01:00:00Z: the grant time.
NOW = 1788224400000
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


def _grant_request(request_id, turn_id, request_hash, model_id=MODEL,
                   gateway_id=GATEWAY, seat_id=SEAT, ttl_ms=TTL_MS,
                   output_cap=1000, observed_at=NOW - 1000):
    return {
        "request_id": request_id,
        "request_hash": request_hash,
        "turn_id": turn_id,
        "seat_id": seat_id,
        "model_id": model_id,
        "task_class": "bounded_read",
        "effort": "opus",
        "gateway_id": gateway_id,
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
        "output_cap": output_cap,
        "per_call_output_cap": 500,
        "ttl_ms": ttl_ms,
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
        conn.execute(
            "INSERT INTO accounts(id, alias, vendor, kind, budget_micro_eur)"
            " VALUES(?,?,?,?,?)",
            (ACCOUNT2, "meter2", "ovh", "metered", 10 * HOLD))
        conn.execute("INSERT INTO machines(id, alias) VALUES(?,?)",
                     (MACHINE, "desktop"))
        conn.execute(
            "INSERT INTO gateways(id, alias, account_id, machine_id, "
            "share_micro_eur, period) VALUES(?,?,?,?,?,?)",
            (GATEWAY, "g1", ACCOUNT, MACHINE, 100_000_000, "utc_month"))
        conn.execute("UPDATE gateways SET sized_ready=1 WHERE id=?",
                     (GATEWAY,))
        conn.execute(
            "INSERT INTO gateways(id, alias, account_id, machine_id, "
            "share_micro_eur, period) VALUES(?,?,?,?,?,?)",
            (GATEWAY2, "g2", ACCOUNT2, MACHINE, 100_000_000, "utc_month"))
        conn.execute("UPDATE gateways SET sized_ready=1 WHERE id=?",
                     (GATEWAY2,))
        conn.execute("INSERT INTO projects(id, alias) VALUES(?,?)",
                     (PROJECT, "org"))
        # Project authorization: the project is given the fixture
        # account - and only it (section 7).
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
            "INSERT INTO seats(id, team_id, machine_id, alias, role, "
            "enforcement) VALUES(?,?,?,?,?,?)",
            (SEAT2, TEAM, MACHINE, "s2", "main", "metered_advisory"))
        conn.execute(
            "INSERT INTO models(id, account_id, alias, context_ceiling, "
            "output_ceiling, tariff_input_rate, tariff_output_rate, "
            "reserve_input_rate, reserve_output_rate, reservation_mode, "
            "policy_hash, effort_aliases) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (MODEL, ACCOUNT, "opus", 180000, 18000, 1, 1, 1000000,
             1000000, "sized", POLICY_HASH, "opus"))
        conn.execute(
            "INSERT INTO models(id, account_id, alias, context_ceiling, "
            "output_ceiling, tariff_input_rate, tariff_output_rate, "
            "reserve_input_rate, reserve_output_rate, reservation_mode, "
            "policy_hash, effort_aliases) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (MODEL2, ACCOUNT2, "opus", 180000, 18000, 1, 1, 1000000,
             1000000, "sized", POLICY_HASH, "opus"))
        # Card L1a turn 2: grant reads the task class's profile, so every
        # fixture carries one (candidates: every model of the fixture).
        conn.execute(
            "INSERT INTO profiles(id, task_class, candidates, "
            "context_rules, calibration, expected_calls, growth_tokens, "
            "margin_bps, max_calls, output_cap, per_call_output_cap, "
            "ttl_ms, policy_revision) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "bounded_read",
             json.dumps([r[0] for r in conn.execute(
                 "SELECT alias FROM models ORDER BY alias")]),
             "{}", "{}", 1, 0, 12500, 2, 1000, 500, 1800000, 1))
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


class Card4EnforceFixTest(unittest.TestCase):

    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base,
                                                prefix="quota-card4-fb-")
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

    # --- READ B B3 ----------------------------------------------------------------

    def test_platform_limit_enforced(self):
        # A platform (money, month) singleton cap of 1 micro-EUR: the
        # sized hold (2253) must not be admitted at all.
        db = self._store("platform.db", 10 * HOLD)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO limits(id, scope, scope_id, metric, period, "
                "window_key, soft, hard) VALUES(?,?,?,?,?,?,?,?)",
                ("lim-platform", "platform", PLATFORM, "money_micro_eur",
                 "month", None, None, 1))
            conn.commit()
        finally:
            conn.close()
        _assert_quota_error(self, errors.CODE_MONEY_LIMIT,
                            lambda: self._grant(db, NOW))

    # --- READ A MAJOR 5 -----------------------------------------------------------

    def test_foreign_account_rejected(self):
        # MODEL2 belongs to ACCOUNT2, which the seat's project has not
        # been given: the grant is refused by policy, not admitted
        # against a foreign budget.
        db = self._store("foreignacct.db", 10 * HOLD)
        _assert_quota_error(
            self, errors.CODE_POLICY_DENIED,
            lambda: self._grant(db, NOW, model_id=MODEL2))

    def test_foreign_gateway_rejected(self):
        # A sized-ready gateway of ANOTHER account: it cannot bind to
        # the lease (and must not upgrade its enforcement to metered).
        db = self._store("foreigngw.db", 10 * HOLD)
        _assert_quota_error(
            self, errors.CODE_POLICY_DENIED,
            lambda: self._grant(db, NOW, gateway_id=GATEWAY2))

    # --- READ B M2 ----------------------------------------------------------------

    def test_replay_changed_content_conflicts(self):
        db = self._store("replay.db", 10 * HOLD)
        request_id, turn_id = str(uuid.uuid4()), str(uuid.uuid4())
        req = _grant_request(request_id, turn_id, "ab" * 32)
        first = lease.grant(db, req, NOW)
        self.assertEqual(first["state"], "active")
        # Same id + same content still replays the stored lease.
        again = lease.grant(db, req, NOW)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["lease_id"], first["lease_id"])
        # Same id + changed content (a new output cap) is a conflict:
        # the id now names different content.
        changed = dict(req, output_cap=5000)
        _assert_quota_error(
            self, errors.CODE_IDEMPOTENCY_CONFLICT,
            lambda: lease.grant(db, changed, NOW))

    # --- READ A MAJOR 4 -----------------------------------------------------------

    def test_advisory_settle_null_money(self):
        # A metered_advisory lease reserves no money: its final
        # settlement carries money=None (the termination confirmation)
        # and still reaches "settled".
        db = self._store("advisory.db", 10 * HOLD)
        result = self._grant(db, NOW, seat_id=SEAT2)
        self.assertEqual(result["enforcement"], "metered_advisory")
        self.assertIsNone(result["money_hold"])
        settled = lease.settle(
            db, _receipt(str(uuid.uuid4()), result["lease_id"], None), NOW)
        self.assertEqual(settled["state"], "settled")
        # Card L1b: a lease that reserved no money reports null money
        # fields (design section 7), not 0/0.
        self.assertIsNone(settled["released_micro_eur"])
        self.assertIsNone(settled["retained_micro_eur"])

    # --- READ A MAJOR 6 -----------------------------------------------------------

    def test_ttl_ms_bounded(self):
        db = self._store("ttl.db", 10 * HOLD)
        # Card L1a turn 2: the profile caps ttl_ms; this test is about the
        # 24 h ABSOLUTE maximum, so its profile allows exactly 24 h.
        conn = sqlite3.connect(db)
        conn.execute("UPDATE profiles SET ttl_ms=86400000")
        conn.commit()
        conn.close()
        # Above the 24h absolute maximum (section 6): a static
        # INVALID_REQUEST; the int64 overflow case must not leak a
        # raw OverflowError.
        for bad in (86_400_001, 2 ** 62, 2 ** 63 - 1):
            with self.assertRaises(QuotaError) as ctx:
                self._grant(db, NOW, ttl_ms=bad)
            self.assertEqual(ctx.exception.code,
                             errors.CODE_INVALID_REQUEST)
        # The 24h maximum itself is still a valid lease.
        ok = self._grant(db, NOW, ttl_ms=86_400_000)
        self.assertEqual(ok["state"], "active")


if __name__ == "__main__":
    unittest.main()
