"""Card 4 fix-round tests, turn C: settle binding and the minors.

Run from the repository root:
    python -m unittest discover -s tests -v

Each test pins one fix-round finding (READ A / READ B cold reads of
head 811fae3) that turns A/B left open:

  test_settle_foreign_gateway_conflicts   (READ A MAJOR 5, settle side):
      a receipt naming a gateway the lease does not hold is a static
      RECEIPT_CONFLICT, never a settle.
  test_settle_release_retained_per_lease  (READ A MINOR 8 / READ B m1):
      the settle response is per lease: with two money holds,
      released/retained are the lease's own hold figures, not the
      sum over the holds rows.
  test_parked_replay_returns_stored_lease (READ A MINOR 11 / READ B m2):
      a lost-response retry of a granted lease replays the stored
      lease (section 7: replay returns 200) even if the account is
      parked since.
  test_stale_estimate_refused             (READ A MINOR 10 / READ B m3):
      an estimate more than 120 seconds old is a static
      INVALID_REQUEST.
  test_shadow_consumes_no_concurrency_slot (READ A MINOR 12):
      a shadow lease consumes no slot: it is not counted against a
      scope concurrency cap.
  test_nonfinal_past_ttl_refused          (READ B m6):
      a non-final receipt on a lease whose TTL has elapsed (not yet
      explicitly expired) is refused LEASE_EXPIRED; a final receipt
      still settles it.

The fixture chain and request shapes mirror tests/test_card4_enforce_fix.py
so the holds line up: HOLD per grant.
"""

import hashlib
import os
import json
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
# Foreign gateway: another account's, sized ready.
ACCOUNT2 = "88888888-8888-4888-8888-888888888888"
GATEWAY2 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
# A shadow seat on the same team/machine: consumes no concurrency slot.
SEAT3 = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
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


def _receipt(event_id, lease_id, actual, final=1, gateway_id=GATEWAY,
             at=NOW):
    return {
        "event_id": event_id,
        "lease_id": lease_id,
        "outcome": "completed",
        "calls": 1,
        "input_tokens": 500,
        "cached_input_tokens": 0,
        "output_tokens": 100,
        "actual_micro_eur": actual,
        "gateway_id": gateway_id,
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
            "INSERT INTO seats(id, team_id, machine_id, alias, role, "
            "enforcement) VALUES(?,?,?,?,?,?)",
            (SEAT3, TEAM, MACHINE, "s2", "main", "shadow"))
        conn.execute(
            "INSERT INTO models(id, account_id, alias, context_ceiling, "
            "output_ceiling, tariff_input_rate, tariff_output_rate, "
            "reserve_input_rate, reserve_output_rate, reservation_mode, "
            "policy_hash, effort_aliases) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (MODEL, ACCOUNT, "opus", 180000, 18000, 1, 1, 1000000,
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


class Card4TurnCFixTest(unittest.TestCase):

    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base,
                                                prefix="quota-card4-fc-")
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

    # --- READ A MAJOR 5 (settle side) -------------------------------------------

    def test_settle_foreign_gateway_conflicts(self):
        # A receipt attributed to a gateway the lease does not hold is
        # a conflicting account of the lease (the overrun liability
        # would otherwise take a foreign home): a static
        # RECEIPT_CONFLICT, never a settle.
        db = self._store("fgw.db", 10 * HOLD)
        lease_id = self._grant(db, NOW)["lease_id"]
        _assert_quota_error(
            self, errors.CODE_RECEIPT_CONFLICT,
            lambda: lease.settle(
                db, _receipt(str(uuid.uuid4()), lease_id, 10,
                             gateway_id=GATEWAY2), NOW))
        # The lease is untouched; naming the lease's own gateway (or
        # naming none) still settles it.
        self.assertEqual(_rows(db, "SELECT state FROM leases WHERE id=?",
                               (lease_id,)), [("active",)])
        settled = lease.settle(
            db, _receipt(str(uuid.uuid4()), lease_id, 10,
                         gateway_id=None), NOW)
        self.assertEqual(settled["state"], "settled")

    # --- READ A MINOR 8 / READ B m1 ----------------------------------------------

    def test_settle_release_retained_per_lease(self):
        # A project money/month limit so the lease carries two money
        # holds (the account budget and the limit), each reserving
        # HOLD: the settle response is per lease, not the sum over the
        # holds rows.
        db = self._store("perlease.db", 10 * HOLD)
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
        actual = HOLD // 4
        settled = lease.settle(
            db, _receipt(str(uuid.uuid4()), result["lease_id"], actual),
            NOW)
        self.assertEqual(settled["state"], "settled")
        self.assertEqual(settled["retained_micro_eur"], actual)
        self.assertEqual(settled["released_micro_eur"], HOLD - actual)

    # --- READ A MINOR 11 / READ B m2 ----------------------------------------------

    def test_parked_replay_returns_stored_lease(self):
        # A lost-response retry of a granted lease replays the stored
        # lease (section 7: replay returns 200) even if the account is
        # parked since the grant; a NEW request is still refused.
        db = self._store("parked.db", 10 * HOLD)
        request_id, turn_id = str(uuid.uuid4()), str(uuid.uuid4())
        req = _grant_request(request_id, turn_id, "ab" * 32)
        first = lease.grant(db, req, NOW)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO blocks(id, account_id, model_id, reason, "
                "retry_at, state, source_event_id, updated_at) "
                "VALUES(?,?,?,?,NULL,'parked',?,?)",
                (str(uuid.uuid4()), ACCOUNT, None, "probe",
                 str(uuid.uuid4()), NOW))
            conn.commit()
        finally:
            conn.close()
        again = lease.grant(db, req, NOW)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["lease_id"], first["lease_id"])
        _assert_quota_error(
            self, errors.CODE_ACCOUNT_PARKED,
            lambda: self._grant(db, NOW))

    # --- READ A MINOR 10 / READ B m3 ----------------------------------------------

    def test_stale_estimate_refused(self):
        # Section 7: estimates must be at most 120 seconds old.
        db = self._store("stale.db", 10 * HOLD)
        _assert_quota_error(
            self, errors.CODE_ESTIMATE_STALE,
            lambda: self._grant(db, NOW, observed_at=NOW - 121_000))
        # Exactly 120 seconds old is still fresh.
        ok = self._grant(db, NOW, observed_at=NOW - 120_000)
        self.assertEqual(ok["state"], "active")

    # --- READ A MINOR 12 (shadow slot) --------------------------------------------

    def test_shadow_consumes_no_concurrency_slot(self):
        # A team concurrency cap of one: a shadow lease consumes no
        # slot, so the cap is still free for a metered lease of the
        # same team.
        db = self._store("shadow.db", 10 * HOLD)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO limits(id, scope, scope_id, metric, period, "
                "window_key, soft, hard) VALUES(?,?,?,?,?,?,?,?)",
                ("lim-cc", "team", TEAM, "concurrency", "turn",
                 None, None, 1))
            conn.commit()
        finally:
            conn.close()
        shadow = self._grant(db, NOW, seat_id=SEAT3)
        self.assertEqual(shadow["enforcement"], "shadow")
        metered = self._grant(db, NOW, seat_id=SEAT)
        self.assertEqual(metered["state"], "active")

    # --- READ B m6 ------------------------------------------------------------------

    def test_nonfinal_past_ttl_refused(self):
        # At actual expiry the meter's interim usage no longer counts:
        # a non-final receipt after the TTL elapsed (the lease not yet
        # explicitly expired) is refused; the final receipt still
        # settles it.
        db = self._store("nfttl.db", 10 * HOLD)
        lease_id = self._grant(db, NOW)["lease_id"]
        late = NOW + TTL_MS + 1
        _assert_quota_error(
            self, errors.CODE_LEASE_EXPIRED,
            lambda: lease.settle(
                db, _receipt(str(uuid.uuid4()), lease_id, None, final=0,
                             at=late), late))
        settled = lease.settle(
            db, _receipt(str(uuid.uuid4()), lease_id, 100, final=1,
                         at=late), late)
        self.assertEqual(settled["state"], "settled")


if __name__ == "__main__":
    unittest.main()
