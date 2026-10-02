"""Card 4 acceptance tests: lease store, turn 2 - grant/check/settle.

Run from the repository root:
    python -m unittest discover -s tests -v

Method names carry the card's check names with the unittest "test_" prefix:
grant_race (this turn), plus the 409/410/429 grant/settle contract rows of
section 7 (replay, conflict, limits, parked, expired, closed). late_receipt
and the uncertain-expiry lifecycle land in turn 3.

Every test builds a schema-v5 store with UUID fixture rows (the store-level
request contract validates seat/model/gateway/request ids as UUIDs) and
calls the store operations directly - there is no HTTP layer until card 5.
"""

import hashlib
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from unittest import mock
import uuid

import quota_balancer.migrate as migrate
from quota_balancer import lease, policy
from quota_balancer import errors

# --- fixed UUID fixture chain ----------------------------------------------------

ACCOUNT = "11111111-1111-4111-8111-111111111111"
MACHINE = "22222222-2222-4222-8222-222222222222"
GATEWAY = "33333333-3333-4333-8333-333333333333"
PROJECT = "44444444-4444-4444-8444-444444444444"
TEAM = "55555555-5555-4555-8555-555555555555"
SEAT = "66666666-6666-4666-8666-666666666666"
MODEL = "77777777-7777-4777-8777-777777777777"
POLICY_HASH = "ab" * 32

# 2026-09, one hour into the month: the period every fixture settles
# against.
NOW = 1788224400000
MONTH_START = 1788220800000
TTL_MS = 1800000

# The grant the whole file exercises: c=1000, n=1, g=0, o=1000, m=2, the
# fixture model's reserve rates.
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
HOLD = EST["hold_sized"]          # the sized money hold per grant
INPUT_CAP = EST["input_cap"]      # the input cap per grant


def _grant_request(request_id, turn_id, request_hash):
    """The closed grant shape of this turn's store API."""
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
             outcome="completed"):
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
        "observed_at": NOW + 1000,
        "received_at": NOW + 2000,
    }


def _build_store(tmp, name, budget, gateway_sized=1):
    """A schema-v5 store with the UUID fixture chain and one metered
    account. Returns the db path."""
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
        conn.execute("UPDATE gateways SET sized_ready=? WHERE id=?",
                     (gateway_sized, GATEWAY))
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
    with test.assertRaises(
            errors.QuotaError) as ctx:
        fn()
    test.assertEqual(ctx.exception.code, code)
    return ctx.exception


# --- grant_race worker (module level: spawn-picklable) -----------------------------

WORKERS = 4
ATTEMPTS_PER_WORKER = 3
RACE_BUDGET = 3 * HOLD  # exactly three holds fit


def _race_worker(store_path, worker_id):
    """ATTEMPTS_PER_WORKER sequential grants from one process; unique
    turn/request ids per attempt so only the budget can refuse. Returns
    the per-attempt outcomes."""
    # A spawned process does not inherit the test's mock patch: open the
    # R-003 interim gate here (card L1a; reachable only after card R1).
    lease.METERED_ENFORCEMENT_ACTIVATED = True
    results = []
    for i in range(ATTEMPTS_PER_WORKER):
        tag = "%d-%d" % (worker_id, i)
        request = _grant_request(
            str(uuid.uuid4()), str(uuid.uuid4()),
            hashlib.sha256(tag.encode("utf-8")).hexdigest())
        try:
            result = lease.grant(store_path, request, NOW)
            results.append(("granted", result["money_hold"]))
        except errors.QuotaError as exc:
            results.append(("refused", exc.code))
    return results


class Card4Turn2Test(unittest.TestCase):

    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base,
                                                prefix="quota-card4-t2-")
        self.tmp = self._tmp.name
        # R-003 interim gate (card L1a): this test exercises enforcing
        # metered leases (holds, tokens, settlement), reachable only once
        # card R1's activation gate replaces the interim constant.
        _gate = mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True)
        _gate.start()
        self.addCleanup(_gate.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _store(self, budget=3 * HOLD, gateway_sized=1):
        return _build_store(self.tmp, "ops.db", budget, gateway_sized)

    def _grant(self, db, request_id=None, turn_id=None, request_hash=None,
               now_ms=NOW):
        return lease.grant(
            db, _grant_request(
                request_id or str(uuid.uuid4()),
                turn_id or str(uuid.uuid4()),
                request_hash or hashlib.sha256(
                    (request_id or "x").encode("utf-8")).hexdigest()),
            now_ms)

    # --- grant: admission and holds -------------------------------------------------

    def test_grant_admits_and_records_holds(self):
        db = self._store()
        result = self._grant(db)
        self.assertEqual(result["state"], "active")
        self.assertEqual(result["enforcement"], "metered")
        self.assertEqual(result["money_hold"], HOLD)
        self.assertEqual(result["caps"]["expected_input"],
                         EST["expected_input"])
        self.assertEqual(result["caps"]["input_cap"], INPUT_CAP)
        self.assertFalse(result["replayed"])
        self.assertEqual(_rows(db, "SELECT COUNT(*) FROM leases"),
                         [(1,)])
        # One hold: the synthetic account-limit row, reserved at H.
        holds = _rows(db, "SELECT limit_id, reserved, settled_actual "
                         "FROM holds")
        self.assertEqual(holds, [("account:" + ACCOUNT, HOLD, None)])
        actions = [r[0] for r in
                   _rows(db, "SELECT action FROM audit")]
        self.assertIn("lease_granted", actions)

    def test_grant_money_limit(self):
        db = self._store(budget=HOLD - 1)
        exc = _assert_quota_error(
            self, errors.CODE_MONEY_LIMIT, lambda: self._grant(db))
        self.assertIsNone(exc.retry_at)
        self.assertEqual(_rows(db, "SELECT COUNT(*) FROM leases"),
                         [(0,)])
        self.assertEqual(_rows(db, "SELECT COUNT(*) FROM holds"),
                         [(0,)])

    def test_grant_input_limit(self):
        db = self._store()
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO limits(id, scope, scope_id, metric, period, "
                "window_key, soft, hard) VALUES(?,?,?,?,?,?,?,?)",
                ("lim-in", "project", PROJECT, "input_tokens", "month",
                 None, None, INPUT_CAP - 1))
            conn.commit()
        finally:
            conn.close()
        _assert_quota_error(
            self, errors.CODE_INPUT_LIMIT, lambda: self._grant(db))
        self.assertEqual(_rows(db, "SELECT COUNT(*) FROM holds"),
                         [(0,)])

    def test_grant_window_limit(self):
        db = self._store()
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO limits(id, scope, scope_id, metric, period, "
                "window_key, soft, hard) VALUES(?,?,?,?,?,?,?,?)",
                ("lim-wk", "project", PROJECT, "window_bps", "window",
                 "wk", None, 4000))
            conn.execute(
                "INSERT INTO windows(account_id, window_key, used_bps, "
                "resets_at, observed_at, confidence, source_stale, "
                "covered_seq, source_event_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (ACCOUNT, "wk", 4001, NOW + 3600000, NOW, "observed",
                 0, 1, "ev-wk"))
            conn.commit()
        finally:
            conn.close()
        _assert_quota_error(
            self, errors.CODE_WINDOW_LIMIT, lambda: self._grant(db))

    def test_grant_account_parked(self):
        db = self._store()
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO blocks(id, account_id, model_id, reason, "
                "retry_at, state, source_event_id, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), ACCOUNT, MODEL, "codex_exhausted",
                 NOW + 60000, "parked", "ev-block", NOW))
            conn.commit()
        finally:
            conn.close()
        exc = _assert_quota_error(
            self, errors.CODE_ACCOUNT_PARKED, lambda: self._grant(db))
        self.assertEqual(exc.retry_at, NOW + 60000)

    def test_grant_advisory_reserves_nothing(self):
        # Not sized-ready: the metered seat degrades to advisory - no
        # enforceable hold, no holds row, no budget exposure.
        db = self._store(gateway_sized=0)
        result = self._grant(db)
        self.assertEqual(result["enforcement"], "metered_advisory")
        self.assertIsNone(result["money_hold"])
        self.assertEqual(_rows(db, "SELECT COUNT(*) FROM holds"),
                         [(0,)])

    # --- idempotency: replay and conflict ------------------------------------------

    def test_replay_same_content_replays(self):
        db = self._store()
        request = _grant_request(str(uuid.uuid4()), str(uuid.uuid4()),
                                 "cd" * 32)
        first = lease.grant(db, request, NOW)
        second = lease.grant(db, request, NOW)
        self.assertEqual(second["lease_id"], first["lease_id"])
        self.assertTrue(second["replayed"])
        self.assertEqual(_rows(db, "SELECT COUNT(*) FROM leases"),
                         [(1,)])
        self.assertEqual(_rows(db, "SELECT COUNT(*) FROM holds"),
                         [(1,)])

    def test_conflict_same_id_different_content(self):
        db = self._store()
        request_id = str(uuid.uuid4())
        self._grant(db, request_id=request_id, request_hash="cd" * 32)
        _assert_quota_error(
            self, errors.CODE_IDEMPOTENCY_CONFLICT,
            lambda: self._grant(db, request_id=request_id,
                                request_hash="ce" * 32))
        self.assertEqual(_rows(db, "SELECT COUNT(*) FROM leases"),
                         [(1,)])

    def test_conflict_live_slot_taken(self):
        # Same (seat, turn), a different request: the lease_live slot is
        # taken, so this is a conflict, not a second lease.
        db = self._store()
        turn_id = str(uuid.uuid4())
        self._grant(db, turn_id=turn_id, request_hash="cd" * 32)
        _assert_quota_error(
            self, errors.CODE_IDEMPOTENCY_CONFLICT,
            lambda: self._grant(db, turn_id=turn_id,
                                request_hash="ce" * 32))

    # --- check -----------------------------------------------------------------------

    def test_check_lifecycle(self):
        db = self._store()
        result = self._grant(db)
        lease_id = result["lease_id"]
        active = lease.check(db, lease_id, NOW)
        self.assertEqual(active["state"], "active")
        self.assertEqual(active["caps"]["input_cap"], INPUT_CAP)
        # Expired (TTL passed): 410.
        _assert_quota_error(
            self, errors.CODE_LEASE_EXPIRED,
            lambda: lease.check(db, lease_id, NOW + TTL_MS + 1))
        _assert_quota_error(
            self, errors.CODE_NOT_FOUND,
            lambda: lease.check(db, str(uuid.uuid4()), NOW))

    def test_check_settled_is_closed(self):
        db = self._store()
        result = self._grant(db)
        lease.settle(
            db, _receipt(str(uuid.uuid4()), result["lease_id"], HOLD // 2),
            NOW)
        _assert_quota_error(
            self, errors.CODE_LEASE_CLOSED,
            lambda: lease.check(db, result["lease_id"], NOW))

    # --- settle ----------------------------------------------------------------------

    def test_settle_final_releases_and_retains(self):
        db = self._store()
        result = self._grant(db)
        lease_id = result["lease_id"]
        event_id = str(uuid.uuid4())
        actual = HOLD // 2
        settled = lease.settle(db, _receipt(event_id, lease_id, actual),
                               NOW)
        self.assertEqual(settled["state"], "settled")
        self.assertEqual(settled["retained_micro_eur"], actual)
        self.assertEqual(settled["released_micro_eur"], HOLD - actual)
        self.assertFalse(settled["replayed"])
        self.assertEqual(_rows(db,
                               "SELECT state, outcome FROM leases"),
                         [("settled", "completed")])
        self.assertEqual(
            _rows(db, "SELECT settled_actual FROM holds"), [(actual,)])
        # Identical duplicate: a no-op replay, nothing moves again in the
        # STORE, but the response reports the same released/retained the
        # first settle did (fix round 2, tk-dae9aab24ef8 finding N1: "the
        # first receipt's own response unchanged" - not a flat zero, which
        # would make a lost settlement response unrecoverable by retry).
        again = lease.settle(db, _receipt(event_id, lease_id, actual),
                             NOW)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["released_micro_eur"], HOLD - actual)
        self.assertEqual(again["retained_micro_eur"], actual)
        # A different receipt on a settled lease: it blocks, the
        # recorded liability is preserved.
        _assert_quota_error(
            self, errors.CODE_RECEIPT_CONFLICT,
            lambda: lease.settle(
                db, _receipt(str(uuid.uuid4()), lease_id, 0), NOW))

    def test_settle_overrun_records_liability(self):
        db = self._store()
        result = self._grant(db)
        lease_id = result["lease_id"]
        actual = HOLD + 50
        settled = lease.settle(db, _receipt(str(uuid.uuid4()), lease_id,
                                            actual), NOW)
        # The hold caps the settled actual; the overrun is liability.
        self.assertEqual(settled["retained_micro_eur"], HOLD)
        self.assertEqual(settled["released_micro_eur"], 0)
        self.assertEqual(_rows(db, "SELECT settled_actual FROM holds"),
                         [(HOLD,)])
        self.assertEqual(
            _rows(db,
                  "SELECT unattributed_micro_eur, reason, resolved_at, "
                  "account_id, gateway_id FROM adjustments"),
            [(50, "overrun", None, ACCOUNT, GATEWAY)])
        actions = [r[0] for r in _rows(db, "SELECT action FROM audit")]
        self.assertIn("overrun_recorded", actions)
        self.assertIn("lease_settled", actions)

    def test_settle_zero_actual_never_below_zero(self):
        db = self._store()
        result = self._grant(db)
        settled = lease.settle(
            db, _receipt(str(uuid.uuid4()), result["lease_id"], 0), NOW)
        self.assertEqual(settled["retained_micro_eur"], 0)
        self.assertEqual(settled["released_micro_eur"], HOLD)
        self.assertEqual(_rows(db, "SELECT settled_actual FROM holds"),
                         [(0,)])
        self.assertEqual(_rows(db, "SELECT COUNT(*) FROM adjustments"),
                         [(0,)])

    def test_settle_nonfinal_then_final(self):
        db = self._store()
        result = self._grant(db)
        lease_id = result["lease_id"]
        partial = lease.settle(
            db, _receipt(str(uuid.uuid4()), lease_id, None, final=0,
                         input_tokens=200), NOW)
        self.assertEqual(partial["state"], "active")
        self.assertEqual(partial["released_micro_eur"], 0)
        self.assertEqual(_rows(db, "SELECT settled_actual FROM holds"),
                         [(None,)])
        # Unknown spend is not a zero final receipt.
        _assert_quota_error(
            self, errors.CODE_INVALID_REQUEST,
            lambda: lease.settle(
                db, _receipt(str(uuid.uuid4()), lease_id, None), NOW))
        final = lease.settle(
            db, _receipt(str(uuid.uuid4()), lease_id, HOLD), NOW)
        self.assertEqual(final["state"], "settled")
        self.assertEqual(final["retained_micro_eur"], HOLD)

    # --- grant_race ------------------------------------------------------------------

    def test_grant_race(self):
        # Given one budget (exactly 3*HOLD), When WORKERS processes grant
        # ATTEMPTS_PER_WORKER times each in parallel, Then the sum of the
        # granted holds never exceeds the budget: exactly three fits,
        # every other attempt is refused MONEY_LIMIT.
        db = _build_store(self.tmp, "race.db", RACE_BUDGET)
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(WORKERS) as pool:
            outcomes = pool.starmap(
                _race_worker, [(db, worker) for worker in range(WORKERS)])
        granted = [hold for pair in outcomes
                   for status, hold in pair if status == "granted"]
        refused = [code for pair in outcomes
                   for status, code in pair if status == "refused"]
        self.assertEqual(len(granted), 3)
        self.assertEqual(len(refused),
                         WORKERS * ATTEMPTS_PER_WORKER - 3)
        self.assertTrue(all(code == errors.CODE_MONEY_LIMIT
                            for code in refused), refused)
        self.assertEqual(sum(granted), 3 * HOLD)
        # Ground truth in the store: the stored holds never exceed the
        # budget, no lease was inserted without its hold.
        total_holds = _rows(db,
                            "SELECT COALESCE(SUM(reserved),0) "
                            "FROM holds")[0][0]
        self.assertLessEqual(total_holds, RACE_BUDGET)
        self.assertEqual(total_holds, sum(granted))
        live = _rows(db, "SELECT COALESCE(SUM(money_hold),0) FROM leases "
                         "WHERE state IN ('active','uncertain')")[0][0]
        self.assertLessEqual(live, RACE_BUDGET)
        self.assertEqual(live, sum(granted))


if __name__ == "__main__":
    unittest.main()
