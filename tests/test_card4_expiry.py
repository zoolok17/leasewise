"""Card 4 acceptance tests: lease store, turn 3 - expiry, late receipts,
privacy_4.

Run from the repository root:
    python -m unittest discover -s tests -v

Method names carry the card's check names with the unittest "test_"
prefix: late_receipt (via test_late_receipt_*), privacy_4, plus the
section-4 expiry rules (expiry -> uncertain, never a refund; uncertain
retains its liability; terminal rows cannot be reopened). The fixture
chain and request shapes mirror tests/test_card4_ops.py (turn 2) so the
holds line up: HOLD per grant.
"""

import copy
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

# --- fixed UUID fixture chain (same as turn 2) ------------------------------------

ACCOUNT = "11111111-1111-4111-8111-111111111111"
MACHINE = "22222222-2222-4222-8222-222222222222"
GATEWAY = "33333333-3333-4333-8333-333333333333"
PROJECT = "44444444-4444-4444-8444-444444444444"
TEAM = "55555555-5555-4555-8555-555555555555"
SEAT = "66666666-6666-4666-8666-666666666666"
MODEL = "77777777-7777-4777-8777-777777777777"
POLICY_HASH = "ab" * 32

# 2026-09-01T01:00:00Z: the grant time (one hour into the month and the
# day). The buckets the holds settle against.
NOW = 1788224400000
MONTH_START = 1788220800000      # 2026-09-01T00:00:00Z
DAY_START_0901 = 1788220800000   # the grant day
DAY_START_0902 = 1788297600000   # the next day
OCT_START = 1790812800000        # 2026-10-01T00:00:00Z
NOW_LATE = 1790827200000         # 2026-10-04T04:00:00Z (late month)
LATE_DAY = 1788318000000         # 2026-09-02T03:00:00Z (late day)
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

ERROR_INVALID_REQUEST = (
    '{"error":{"code":"INVALID_REQUEST","message":"the request is invalid"}}')
ERROR_STORE_NOT_FOUND = (
    '{"error":{"code":"STORE_ERROR","message":"database not found"}}')


def _error_payload(exc):
    """Render the {error:{code,message}} contract the CLI would emit."""
    return json.dumps({"error": {"code": exc.code,
                                 "message": exc.message}},
                      separators=(",", ":"))


def _grant_request(request_id, turn_id, request_hash):
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
            "observed_at": NOW - 1000,
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


class Card4Turn3Test(unittest.TestCase):

    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base,
                                                prefix="quota-card4-t3-")
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _store(self, budget=3 * HOLD):
        return _build_store(self.tmp, "exp.db", budget)

    def _grant(self, db, request_id=None, turn_id=None, request_hash=None,
               now_ms=NOW):
        return lease.grant(
            db, _grant_request(
                request_id or str(uuid.uuid4()),
                turn_id or str(uuid.uuid4()),
                request_hash or hashlib.sha256(
                    ("x" + str(uuid.uuid4())).encode("utf-8")).hexdigest()),
            now_ms)

    # --- expiry --------------------------------------------------------------------

    def test_expire_retains_liability_and_budget(self):
        # Budget of exactly one hold: after expiry the hold must still
        # count, or a second grant (different turn) would fit.
        db = self._store(budget=HOLD)
        result = self._grant(db)
        expired = lease.expire(db, result["lease_id"], NOW + TTL_MS)
        self.assertEqual(expired["state"], "uncertain")
        self.assertFalse(expired["replayed"])
        # Never a refund: the hold stays reserved, nothing settled.
        self.assertEqual(
            _rows(db, "SELECT limit_id, reserved, settled_actual "
                      "FROM holds"),
            [("account:" + ACCOUNT, HOLD, None)])
        # The budget still counts it.
        _assert_quota_error(
            self, errors.CODE_MONEY_LIMIT, lambda: self._grant(db))
        self.assertIn("lease_expired",
                      [r[0] for r in _rows(db, "SELECT action FROM audit")])

    def test_expire_retains_live_slot(self):
        # A two-hold budget: admission still fits, so a same-(seat,
        # turn) grant on the expired lease is refused by the lease_live
        # slot (uncertain keeps the slot), not by money.
        db = self._store(budget=2 * HOLD)
        result = self._grant(db)
        lease_id = result["lease_id"]
        lease.expire(db, lease_id, NOW + TTL_MS)
        stored_turn = _rows(db, "SELECT turn_id FROM leases WHERE id=?",
                            (lease_id,))[0][0]
        _assert_quota_error(
            self, errors.CODE_IDEMPOTENCY_CONFLICT,
            lambda: self._grant(db, turn_id=stored_turn,
                                request_hash="ce" * 32))
        self.assertEqual(_rows(db, "SELECT COUNT(*) FROM leases"),
                         [(1,)])

    def test_expire_not_yet_expired_is_invalid(self):
        db = self._store()
        result = self._grant(db)
        _assert_quota_error(
            self, errors.CODE_INVALID_REQUEST,
            lambda: lease.expire(db, result["lease_id"], NOW + 1000))
        self.assertEqual(_rows(db, "SELECT state FROM leases"),
                         [("active",)])

    def test_expire_uncertain_is_idempotent(self):
        db = self._store()
        result = self._grant(db)
        first = lease.expire(db, result["lease_id"], NOW + TTL_MS)
        self.assertFalse(first["replayed"])
        again = lease.expire(db, result["lease_id"], NOW + TTL_MS + 1)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["state"], "uncertain")
        self.assertEqual(
            _rows(db, "SELECT COUNT(*) FROM audit "
                      "WHERE action='lease_expired'"), [(1,)])
        self.assertEqual(
            _rows(db, "SELECT reserved, settled_actual FROM holds"),
            [(HOLD, None)])

    def test_expire_settled_is_terminal(self):
        db = self._store()
        result = self._grant(db)
        lease.settle(
            db, _receipt(str(uuid.uuid4()), result["lease_id"], HOLD), NOW)
        _assert_quota_error(
            self, errors.CODE_RECEIPT_CONFLICT,
            lambda: lease.expire(db, result["lease_id"], NOW + TTL_MS))
        self.assertEqual(_rows(db, "SELECT state FROM leases"),
                         [("settled",)])

    def test_check_after_expire_still_410(self):
        db = self._store()
        result = self._grant(db)
        lease.expire(db, result["lease_id"], NOW + TTL_MS)
        _assert_quota_error(
            self, errors.CODE_LEASE_EXPIRED,
            lambda: lease.check(db, result["lease_id"], NOW + TTL_MS + 1))

    # --- late_receipt ----------------------------------------------------------------

    def test_late_receipt_reconciles_original_period(self):
        # Given expiry, When a final receipt arrives in a LATER month,
        # Then it settles against the ORIGINAL grant period exactly
        # once, and a replay is a no-op.
        db = self._store()
        result = self._grant(db)
        lease_id = result["lease_id"]
        lease.expire(db, lease_id, NOW + TTL_MS)
        actual = HOLD // 2
        settled = lease.settle(db, _receipt(str(uuid.uuid4()), lease_id,
                                            actual, at=NOW_LATE), NOW_LATE)
        self.assertEqual(settled["state"], "settled")
        self.assertEqual(settled["retained_micro_eur"], actual)
        self.assertEqual(settled["released_micro_eur"], HOLD - actual)
        # The hold kept its ORIGINAL (September) bucket and was settled
        # there - nothing landed in October.
        self.assertEqual(
            _rows(db, "SELECT limit_id, period_start, reserved, "
                      "settled_actual FROM holds"),
            [("account:" + ACCOUNT, MONTH_START, HOLD, actual)])
        # Exactly once: one receipt, and a replay is a no-op.
        event_id = _rows(db, "SELECT event_id FROM receipts")[0][0]
        self.assertEqual(_rows(db, "SELECT COUNT(*) FROM receipts"),
                         [(1,)])
        again = lease.settle(db, _receipt(event_id, lease_id, actual,
                                          at=NOW_LATE), NOW_LATE)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["released_micro_eur"], 0)
        self.assertEqual(again["retained_micro_eur"], 0)
        self.assertEqual(_rows(db, "SELECT COUNT(*) FROM receipts"),
                         [(1,)])
        # October admission: the original period is fully reconciled, so
        # a fresh grant in the new month is not blocked by it.
        october = self._grant(db, now_ms=NOW_LATE)
        self.assertEqual(october["state"], "active")
        # Terminal: a different final receipt blocks.
        _assert_quota_error(
            self, errors.CODE_RECEIPT_CONFLICT,
            lambda: lease.settle(
                db, _receipt(str(uuid.uuid4()), lease_id, 0,
                             at=NOW_LATE), NOW_LATE))

    def test_late_receipt_across_day_boundary(self):
        # A (money, day) limit: the day-bucket hold of an expired lease
        # is reconciled at the ORIGINAL day, not the day the receipt
        # arrives.
        db = self._store()
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO limits(id, scope, scope_id, metric, period, "
                "window_key, soft, hard) VALUES(?,?,?,?,?,?,?,?)",
                ("lim-day", "project", PROJECT, "money_micro_eur", "day",
                 None, None, 10 * HOLD))
            conn.commit()
        finally:
            conn.close()
        result = self._grant(db)
        lease_id = result["lease_id"]
        lease.expire(db, lease_id, NOW + TTL_MS)
        # The grant made two holds: the day limit and the account month.
        self.assertEqual(
            sorted(r[0] for r in _rows(
                db, "SELECT limit_id, period_start, reserved, "
                    "settled_actual FROM holds")),
            sorted(["account:" + ACCOUNT, "lim-day"]))
        settled = lease.settle(
            db, _receipt(str(uuid.uuid4()), lease_id, HOLD // 2,
                         at=LATE_DAY), LATE_DAY)
        self.assertEqual(settled["state"], "settled")
        holds = dict((r[0], (r[1], r[3]))
                     for r in _rows(
                         db, "SELECT limit_id, period_start, reserved, "
                             "settled_actual FROM holds"))
        # The day hold reconciled the grant day (09-01), not 09-02.
        self.assertEqual(holds["lim-day"], (DAY_START_0901, HOLD // 2))
        self.assertEqual(holds["account:" + ACCOUNT],
                         (MONTH_START, HOLD // 2))
        # No hold row exists in the receipt's day.
        self.assertEqual(_rows(
            db, "SELECT COUNT(*) FROM holds WHERE period_start=?",
            (DAY_START_0902,)), [(0,)])

    def test_late_receipt_overrun_reconciles_original_period(self):
        db = self._store()
        result = self._grant(db)
        lease_id = result["lease_id"]
        lease.expire(db, lease_id, NOW + TTL_MS)
        settled = lease.settle(
            db, _receipt(str(uuid.uuid4()), lease_id, HOLD + 50,
                         at=NOW_LATE), NOW_LATE)
        self.assertEqual(settled["retained_micro_eur"], HOLD)
        self.assertEqual(settled["released_micro_eur"], 0)
        self.assertEqual(
            _rows(db, "SELECT period_start, unattributed_micro_eur, "
                      "resolved_at FROM adjustments"),
            [(MONTH_START, 50, None)])

    def test_late_receipt_nonfinal_refused(self):
        db = self._store()
        result = self._grant(db)
        lease_id = result["lease_id"]
        lease.expire(db, lease_id, NOW + TTL_MS)
        # Interim usage never settles money; on an uncertain lease only
        # the final receipt resolves it.
        _assert_quota_error(
            self, errors.CODE_LEASE_EXPIRED,
            lambda: lease.settle(
                db, _receipt(str(uuid.uuid4()), lease_id, None, final=0,
                             at=NOW_LATE), NOW_LATE))
        self.assertEqual(_rows(db, "SELECT state FROM leases"),
                         [("uncertain",)])
        # The final receipt still settles it.
        settled = lease.settle(
            db, _receipt(str(uuid.uuid4()), lease_id, HOLD,
                         at=NOW_LATE), NOW_LATE)
        self.assertEqual(settled["state"], "settled")

    # --- privacy_4 -------------------------------------------------------------------

    SENTINELS = (
        "PROVIDERID-4a3b2c1d0e9f",
        "sk-lease-sentinel-0001",
        "D:/provider/lease-secret.pem",
        "lease.provider-host.example",
    )

    def test_privacy_4(self):
        # privacy_4: status/error output never shows provider IDs,
        # secrets, paths or raw request text - only the closed contract.
        # A sentinel planted at every leak point is rejected with a
        # static message (never echoed), and a full scan of a store that
        # ran the whole lifecycle finds no sentinel and no raw request
        # text anywhere.
        db = self._store()
        sentinels = self.SENTINELS
        base_request = _grant_request(str(uuid.uuid4()), str(uuid.uuid4()),
                                      "cd" * 32)
        base_receipt = _receipt(str(uuid.uuid4()),
                                "00000000-0000-0000-0000-000000000000", HOLD)
        for sentinel in sentinels:
            # 1) Every grant string field: static INVALID_REQUEST.
            for field in ("request_id", "request_hash", "turn_id",
                          "seat_id", "model_id", "effort", "gateway_id",
                          "parent_lease_id"):
                bad = copy.deepcopy(base_request)
                bad[field] = sentinel
                with self.assertRaises(QuotaError) as cm:
                    lease.grant(db, bad, NOW)
                self.assertEqual(cm.exception.code,
                                 errors.CODE_INVALID_REQUEST)
                self.assertEqual(_error_payload(cm.exception),
                                 ERROR_INVALID_REQUEST)
                self.assertNotIn(sentinel, _error_payload(cm.exception))
            for field in ("mode", "source"):
                bad = copy.deepcopy(base_request)
                bad["context"][field] = sentinel
                with self.assertRaises(QuotaError) as cm:
                    lease.grant(db, bad, NOW)
                self.assertEqual(_error_payload(cm.exception),
                                 ERROR_INVALID_REQUEST)
            # 2) Every settle string field: static INVALID_REQUEST.
            for field in ("event_id", "lease_id", "gateway_id", "outcome"):
                bad = copy.deepcopy(base_receipt)
                bad[field] = sentinel
                with self.assertRaises(QuotaError) as cm:
                    lease.settle(db, bad, NOW)
                self.assertEqual(_error_payload(cm.exception),
                                 ERROR_INVALID_REQUEST)
            # 3) A sentinel as the store path: static STORE_ERROR, the
            # path never echoed.
            for call in (
                lambda: lease.grant(sentinel, base_request, NOW),
                lambda: lease.check(sentinel, base_receipt["lease_id"],
                                    NOW),
                lambda: lease.settle(sentinel, base_receipt, NOW),
                lambda: lease.expire(sentinel, base_receipt["lease_id"],
                                     NOW),
            ):
                with self.assertRaises(QuotaError) as cm:
                    call()
                self.assertEqual(cm.exception.code,
                                 errors.CODE_STORE_ERROR)
                self.assertEqual(_error_payload(cm.exception),
                                 ERROR_STORE_NOT_FOUND)
                self.assertNotIn(sentinel, _error_payload(cm.exception))
        # 4) Run the full lifecycle; scan EVERY cell of the store: no
        # sentinel, and no raw request text (the store keeps the request
        # hash, not the request).
        raw_request = json.dumps(
            _grant_request(str(uuid.uuid4()), str(uuid.uuid4()), "ce" * 32))
        result = self._grant(db)
        lease.expire(db, result["lease_id"], NOW + TTL_MS)
        lease.settle(
            db, _receipt(str(uuid.uuid4()), result["lease_id"], HOLD + 50,
                         at=NOW_LATE), NOW_LATE)
        conn = sqlite3.connect(db)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                for row in conn.execute("SELECT * FROM " + table):
                    for cell in row:
                        if not isinstance(cell, str):
                            continue
                        for sentinel in sentinels:
                            self.assertNotIn(sentinel, cell)
                        self.assertNotIn(raw_request, cell)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
