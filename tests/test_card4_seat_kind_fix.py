"""Card 4 carry-over fix (closed in card 5): m4 - seat enforcement vs
account kind.

STEP-CARD-4.md: "a subscription_admission seat on a METERED account
skips money admission (a seat setting is a money bypass); needs the
section-7 ruling, and seat enforcement must be validated against the
account kind before card 5 exposes grants." Fixed in quota_balancer/
lease.py's _grant: REJECT (POLICY_DENIED) a subscription_admission or
subscription_warn seat on a metered account - the design gives no
explicit ruling for this combination; docs/STEP-CARD-5.md records the
fail-closed reading.

shadow is deliberately NOT included: it is a generic observe-only mode
with no billing-model claim of its own and is exercised on a metered
account by test_card4_turn_c_fix.test_shadow_consumes_no_concurrency_slot.
"""

import hashlib
import os
import json
import sqlite3
import tempfile
import unittest
import uuid

import quota_balancer.migrate as migrate
from quota_balancer import errors, lease, policy

ACCOUNT = "aaaaaaaa-1111-4111-8111-111111111111"
MACHINE = "aaaaaaaa-2222-4222-8222-222222222222"
GATEWAY = "aaaaaaaa-3333-4333-8333-333333333333"
PROJECT = "aaaaaaaa-4444-4444-8444-444444444444"
TEAM = "aaaaaaaa-5555-4555-8555-555555555555"
SEAT = "aaaaaaaa-6666-4666-8666-666666666666"
MODEL = "aaaaaaaa-7777-4777-8777-777777777777"
POLICY_HASH = "cd" * 32
NOW = 1788224400000
TTL_MS = 1_800_000

MODEL_DICT = {
    "alias": "opus", "context_ceiling": 180000, "output_ceiling": 18000,
    "tariff_input_rate": 1, "tariff_output_rate": 1,
    "reserve_input_rate": 1_000_000, "reserve_output_rate": 1_000_000,
    "reservation_mode": "sized", "policy_hash": POLICY_HASH,
    "effort_aliases": "opus",
}
EST = policy.estimate(1000, 1, 0, 1000, 2, MODEL_DICT)
HOLD = EST["hold_sized"]


def _grant_request(gateway_id=None):
    request_id = str(uuid.uuid4())
    return {
        "request_id": request_id, "request_hash":
            hashlib.sha256(request_id.encode()).hexdigest(),
        "turn_id": str(uuid.uuid4()), "seat_id": SEAT, "model_id": MODEL,
        "task_class": "bounded_read", "effort": "opus", "gateway_id": gateway_id, "parent_lease_id": None,
        "context": {
            "mode": "FRESH", "initial_tokens": 1000, "dispatch_tokens": 0,
            "source": "tokenizer", "observed_at": NOW - 1000,
            "growth_tokens_per_call": 0,
        },
        "expected_calls": 1, "max_calls": 2, "output_cap": 1000,
        "per_call_output_cap": 500, "ttl_ms": TTL_MS,
    }


def _build_store(tmp, account_kind, enforcement, budget=10 * HOLD):
    db = os.path.join(tmp, "lease.db")
    conn = sqlite3.connect(db)
    try:
        for script in (migrate.MIGRATION_V1, migrate.MIGRATION_V2,
                       migrate.MIGRATION_V3, migrate.MIGRATION_V4,
                       migrate.MIGRATION_V5, migrate.MIGRATION_V6, migrate.MIGRATION_V7, migrate.MIGRATION_V8,
                   migrate.MIGRATION_V9):
            conn.executescript(script)
        conn.execute(
            "INSERT INTO accounts(id, alias, vendor, kind, budget_micro_eur)"
            " VALUES(?,?,?,?,?)",
            (ACCOUNT, "meter", "ovh", account_kind,
             budget if account_kind == "metered" else None))
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
        conn.execute(
            "INSERT INTO project_accounts(project_id, account_id) "
            "VALUES(?,?)", (PROJECT, ACCOUNT))
        conn.execute("INSERT INTO teams(id, project_id, alias) "
                     "VALUES(?,?,?)", (TEAM, PROJECT, "dev"))
        conn.execute(
            "INSERT INTO seats(id, team_id, machine_id, alias, role, "
            "enforcement) VALUES(?,?,?,?,?,?)",
            (SEAT, TEAM, MACHINE, "s1", "main", enforcement))
        conn.execute(
            "INSERT INTO models(id, account_id, alias, context_ceiling, "
            "output_ceiling, tariff_input_rate, tariff_output_rate, "
            "reserve_input_rate, reserve_output_rate, reservation_mode, "
            "policy_hash, effort_aliases) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (MODEL, ACCOUNT, "opus", 180000, 18000, 1, 1, 1_000_000,
             1_000_000, "sized", POLICY_HASH, "opus"))
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


class SeatEnforcementVsAccountKind(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-m4-")
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_subscription_admission_on_metered_account_is_policy_denied(self):
        db = _build_store(self.tmp, "metered", "subscription_admission")
        with self.assertRaises(errors.QuotaError) as ctx:
            lease.grant(db, _grant_request(), NOW)
        self.assertEqual(ctx.exception.code, errors.CODE_POLICY_DENIED)
        conn = sqlite3.connect(db)
        try:
            self.assertEqual(
                len(conn.execute("SELECT id FROM leases").fetchall()), 0)
        finally:
            conn.close()

    def test_subscription_warn_on_metered_account_is_policy_denied(self):
        db = _build_store(self.tmp, "metered", "subscription_warn")
        with self.assertRaises(errors.QuotaError) as ctx:
            lease.grant(db, _grant_request(), NOW)
        self.assertEqual(ctx.exception.code, errors.CODE_POLICY_DENIED)

    def test_subscription_admission_on_a_subscription_account_still_grants(self):
        db = _build_store(self.tmp, "subscription", "subscription_admission")
        result = lease.grant(db, _grant_request(), NOW)
        self.assertEqual(result["state"], "active")
        self.assertIsNone(result["money_hold"])

    def test_metered_and_metered_advisory_on_metered_account_still_grant(self):
        for enforcement in ("metered", "metered_advisory"):
            with self.subTest(enforcement):
                tmp = tempfile.mkdtemp(dir=self.tmp)
                db = _build_store(tmp, "metered", enforcement)
                result = lease.grant(db, _grant_request(GATEWAY), NOW)
                self.assertEqual(result["state"], "active")

    def test_shadow_on_metered_account_still_grants_with_no_hold(self):
        # Not part of m4: shadow makes no billing-model claim and is
        # already relied upon (test_card4_turn_c_fix) on a metered
        # account.
        db = _build_store(self.tmp, "metered", "shadow")
        result = lease.grant(db, _grant_request(), NOW)
        self.assertEqual(result["enforcement"], "shadow")
        self.assertIsNone(result["money_hold"])


if __name__ == "__main__":
    unittest.main()
