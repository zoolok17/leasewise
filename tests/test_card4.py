"""Card 4 acceptance tests: lease store, turn 1 - schema v5.

Run from the repository root:
    python -m unittest discover -s tests -v

Method names carry the card's check names with the unittest "test_" prefix:
null_uniqueness (this turn). grant_race, late_receipt and the 409/410/429
contracts land in turns 2 and 3.

Turn 1 scope: the v4 -> v5 migration adds the seven card-4 tables
(blocks, leases, holds, receipts, adjustments, audit, daily_totals) and
the exact partial indexes of section 7 (blocks_live, receipt_final,
lease_live). Every test builds a schema-v4 store (as main ships) and
upgrades it through the CLI, so the whole file is red while the
migration does not exist yet.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.environ.get("AGENTTALK_PY") or sys.executable

ERROR_SCHEMA_UNSUPPORTED = '{"error":{"code":"SCHEMA_UNSUPPORTED","message":"schema version is not supported"}}'

# A fixed UTC month (2026-09): the period the fixture rows settle
# against.
PERIOD_START = 1788220800000  # 2026-09-01T00:00:00Z
PERIOD_END = PERIOD_START + 30 * 86_400_000


def _now_ms():
    return int(time.time() * 1000)


def run_cli(*args):
    return subprocess.run(
        [PYTHON, "-m", "quota_balancer", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def _build_v4_store(tmp, name):
    """A schema-v4 store exactly as main ships it, with the config chain
    the lease store hangs off (account, machine, gateway, project, team,
    seat, model) plus one v2/v3/v4 row per earlier card's tables."""
    import quota_balancer.migrate as migrate
    db = os.path.join(tmp, name)
    conn = sqlite3.connect(db)
    try:
        conn.executescript(migrate.MIGRATION_V1)
        conn.executescript(migrate.MIGRATION_V2)
        conn.executescript(migrate.MIGRATION_V3)
        conn.executescript(migrate.MIGRATION_V4)
        now = _now_ms()
        conn.execute(
            "INSERT INTO accounts(id, alias, vendor, kind, budget_micro_eur) "
            "VALUES(?,?,?,?,?)", ("a1", "meter", "ovh", "metered", 100_000_000))
        conn.execute("INSERT INTO machines(id, alias) VALUES(?,?)",
                     ("m1", "desktop"))
        conn.execute(
            "INSERT INTO gateways(id, alias, account_id, machine_id, "
            "share_micro_eur, period) VALUES(?,?,?,?,?,?)",
            ("g1", "g1", "a1", "m1", 100_000_000, "utc_month"))
        conn.execute("INSERT INTO projects(id, alias) VALUES(?,?)",
                     ("p1", "org"))
        conn.execute("INSERT INTO teams(id, project_id, alias) VALUES(?,?,?)",
                     ("t1", "p1", "dev"))
        conn.execute(
            "INSERT INTO seats(id, team_id, machine_id, alias, role, "
            "enforcement) VALUES(?,?,?,?,?,?)",
            ("s1", "t1", "m1", "s1", "main", "metered"))
        conn.execute(
            "INSERT INTO models(id, account_id, alias, context_ceiling, "
            "output_ceiling, tariff_input_rate, tariff_output_rate, "
            "reserve_input_rate, reserve_output_rate, reservation_mode, "
            "policy_hash, effort_aliases) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("mod1", "a1", "opus", 180_000, 18_000, 1, 1, 1, 1,
             "sized", "ph", '["opus"]'))
        conn.execute(
            "INSERT INTO readings(event_id, machine_id, account_id, "
            "seat_id, kind, observed_at, received_at, payload) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("ev-v4", "m1", "a1", "s1", "cli_window", now - 60_000,
             now - 59_000, '{"windows":[]}'))
        conn.execute(
            "INSERT INTO windows(account_id, window_key, used_bps, "
            "resets_at, observed_at, confidence, source_stale, "
            "covered_seq, source_event_id) VALUES(?,?,?,?,?,?,?,?,?)",
            ("a1", "wk", 4200, now, now - 60_000, "observed", 0, 7,
             "ev-v4"))
        conn.execute(
            "INSERT INTO billing_policies(account_id, reporter_machine_id, "
            "scope_alias, scope_revision, dedicated_ai, "
            "non_ai_allowance_micro_eur, cap_basis, provider_basis, "
            "ledger_basis, vat_bps, poll_seconds, max_age_seconds, "
            "lag_seconds, abs_tolerance_micro_eur, relative_tolerance_bps, "
            "mode, stale_action, divergence_action) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("a1", "m1", "ovh", 1, 0, 5_000_000, "ex_vat", "unknown",
             "unknown", None, 3600, 600, 300, 10_000, 100, "advisory",
             "alert", "hold"))
        conn.execute(
            "INSERT INTO billing_totals(account_id, scope_revision, "
            "period_start, period_end, latest_micro_eur, observed_at, "
            "latest_event_id, high_water_micro_eur, high_water_event_id, "
            "conflict) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("a1", 1, PERIOD_START, PERIOD_END, 5_000_000, now - 60_000,
             "ev-v4", 5_000_000, "ev-v4", 0))
        conn.commit()
    finally:
        conn.close()
    return db


class _Base(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base,
                                                prefix="quota-card4-")
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _v5_store(self, name="lease.db"):
        """Build a v4 store and upgrade it through the CLI. Returns the db
        path; fails this test while the v5 migration does not exist."""
        db = _build_v4_store(self.tmp, name)
        result = run_cli("--db", db, "init")
        self.assertEqual(result.returncode, 0, result.stderr)
        conn = sqlite3.connect(db)
        try:
            self.assertEqual(conn.execute(
                "PRAGMA user_version").fetchone()[0], 5)
        finally:
            conn.close()
        return db

    def _connect(self, db):
        return sqlite3.connect(db)

    def _lease(self, conn, lease_id, seat_id="s1", turn_id="turn-1",
               machine_id="m1", request_id="req-1", state="active"):
        now = _now_ms()
        conn.execute(
            "INSERT INTO leases(id, machine_id, seat_id, account_id, "
            "model_id, turn_id, request_id, request_hash, task_class, "
            "context_mode, enforcement, estimate, policy_snapshot, "
            "input_cap, output_cap, max_calls, per_call_output_cap, "
            "granted_at, expires_at, period_start, state) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lease_id, machine_id, seat_id, "a1", "mod1", turn_id,
             request_id, "rh", "code", "fresh", "metered", "{}", "{}",
             1000, 100, 3, 100, now, now + 60_000, PERIOD_START, state))


class NullUniqueness(_Base):
    """null_uniqueness: Given null-window/model duplicates, When insert,
    Then reject; final-receipt/live-lease duplicates also reject. One case
    per partial index of schema v5, plus the null-window case of the v3
    limits_scope index (COALESCE makes NULL and '' one window key)."""

    def test_null_window_limit_duplicate_rejected(self):
        db = self._v5_store()
        conn = self._connect(db)
        try:
            conn.execute(
                "INSERT INTO limits(id, scope, scope_id, metric, period, "
                "window_key, soft, hard) VALUES(?,?,?,?,?,?,?,?)",
                ("l1", "project", "p1", "input_tokens", "day", None,
                 None, 100))
            # The NULL window_key is its own distinct value; a second NULL
            # and an empty string both COALESCE to '' and collide.
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO limits(id, scope, scope_id, metric, "
                    "period, window_key, soft, hard) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    ("l2", "project", "p1", "input_tokens", "day", None,
                     None, 100))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO limits(id, scope, scope_id, metric, "
                    "period, window_key, soft, hard) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    ("l3", "project", "p1", "input_tokens", "day", "",
                     None, 100))
            # A real window key is a different value: allowed.
            conn.execute(
                "INSERT INTO limits(id, scope, scope_id, metric, period, "
                "window_key, soft, hard) VALUES(?,?,?,?,?,?,?,?)",
                ("l4", "project", "p1", "input_tokens", "day", "w1",
                 None, 100))
            conn.commit()
        finally:
            conn.close()

    def test_null_model_block_duplicate_rejected(self):
        # blocks_live: one parked/probing block per (account, model-or-'');
        # a NULL model_id is the '' bucket.
        db = self._v5_store()
        conn = self._connect(db)
        try:
            now = _now_ms()
            conn.execute(
                "INSERT INTO blocks(id, account_id, model_id, reason, "
                "retry_at, state, source_event_id, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("b1", "a1", None, "exhausted", now + 60_000, "parked",
                 "ev-v4", now))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO blocks(id, account_id, model_id, reason, "
                    "retry_at, state, source_event_id, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    ("b2", "a1", None, "exhausted", now + 60_000, "parked",
                     "ev-v4", now))
            # The index is partial: a cleared block keeps no slot.
            conn.execute(
                "INSERT INTO blocks(id, account_id, model_id, reason, "
                "retry_at, state, source_event_id, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("b3", "a1", None, "cleared", None, "cleared", "ev-v4",
                 now))
            # A named model is a different bucket: allowed.
            conn.execute(
                "INSERT INTO blocks(id, account_id, model_id, reason, "
                "retry_at, state, source_event_id, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("b4", "a1", "mod1", "exhausted", now + 60_000, "parked",
                 "ev-v4", now))
            conn.commit()
        finally:
            conn.close()

    def test_final_receipt_duplicate_rejected(self):
        # receipt_final: at most one final receipt per lease; non-final
        # receipts are outside the index.
        db = self._v5_store()
        conn = self._connect(db)
        try:
            now = _now_ms()
            self._lease(conn, "L1")
            conn.execute(
                "INSERT INTO receipts(event_id, lease_id, gateway_id, seq, "
                "calls, input_tokens, cached_input_tokens, output_tokens, "
                "actual_micro_eur, outcome, final, observed_at, "
                "received_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("r1", "L1", "g1", 1, 1, 100, 0, 50, 1000, "ok", 1, now,
                 now))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO receipts(event_id, lease_id, gateway_id, "
                    "seq, calls, input_tokens, cached_input_tokens, "
                    "output_tokens, actual_micro_eur, outcome, final, "
                    "observed_at, received_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("r2", "L1", "g1", 2, 0, 0, 0, 0, None, None, 1, now,
                     now))
            # A non-final receipt on the same lease is allowed.
            conn.execute(
                "INSERT INTO receipts(event_id, lease_id, gateway_id, seq, "
                "calls, input_tokens, cached_input_tokens, output_tokens, "
                "actual_micro_eur, outcome, final, observed_at, "
                "received_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("r3", "L1", "g1", 3, 1, 10, 0, 5, None, None, 0, now,
                 now))
            conn.commit()
        finally:
            conn.close()

    def test_live_lease_duplicate_rejected(self):
        # lease_live: one active/uncertain lease per (seat, turn); a
        # settled lease keeps no slot.
        db = self._v5_store()
        conn = self._connect(db)
        try:
            self._lease(conn, "L1")
            with self.assertRaises(sqlite3.IntegrityError):
                self._lease(conn, "L2", turn_id="turn-1",
                            request_id="req-2")
            # The index is partial: a settled lease on the same seat+turn
            # keeps no slot.
            self._lease(conn, "L3", turn_id="turn-1", state="settled",
                        request_id="req-3")
            # A different turn on the same seat is a different slot.
            self._lease(conn, "L4", turn_id="turn-2", request_id="req-4")
            conn.commit()
        finally:
            conn.close()


class SchemaV5Migration(_Base):
    """The v4 -> v5 migration: a v4 store from main upgrades cleanly (v4
    rows survive, the seven card-4 tables and the exact partial indexes
    appear); a newer, unknown version is left byte-for-byte unchanged."""

    CARD4_TABLES = frozenset(
        ("blocks", "leases", "holds", "receipts", "adjustments",
         "audit", "daily_totals"))
    PARTIAL_INDEXES = frozenset(
        ("blocks_live", "receipt_final", "lease_live"))

    def _assert_v5_shape(self, conn):
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(self.CARD4_TABLES <= tables,
                        sorted(self.CARD4_TABLES - tables))
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name IN ('blocks_live','receipt_final','lease_live')")}
        self.assertEqual(indexes, self.PARTIAL_INDEXES)

    def test_v4_store_upgrades_cleanly(self):
        db = _build_v4_store(self.tmp, "upgrade.db")
        result = run_cli("--db", db, "init")
        self.assertEqual(result.returncode, 0, result.stderr)
        conn = self._connect(db)
        try:
            self.assertEqual(conn.execute(
                "PRAGMA user_version").fetchone()[0], 5)
            self._assert_v5_shape(conn)
            # The v4 rows survive the upgrade: one row per earlier card's
            # table, IDs intact.
            self.assertEqual(conn.execute(
                "SELECT id FROM accounts").fetchall(), [("a1",)])
            self.assertEqual(conn.execute(
                "SELECT id FROM machines").fetchall(), [("m1",)])
            self.assertEqual(conn.execute(
                "SELECT id, share_micro_eur FROM gateways").fetchall(),
                [("g1", 100_000_000)])
            self.assertEqual(conn.execute(
                "SELECT id, alias FROM projects").fetchall(),
                [("p1", "org")])
            self.assertEqual(conn.execute(
                "SELECT id FROM teams").fetchall(), [("t1",)])
            self.assertEqual(conn.execute(
                "SELECT id FROM seats").fetchall(), [("s1",)])
            self.assertEqual(conn.execute(
                "SELECT id FROM models").fetchall(), [("mod1",)])
            self.assertEqual(conn.execute(
                "SELECT event_id, kind FROM readings").fetchall(),
                [("ev-v4", "cli_window")])
            self.assertEqual(conn.execute(
                "SELECT window_key, used_bps FROM windows").fetchall(),
                [("wk", 4200)])
            self.assertEqual(conn.execute(
                "SELECT account_id, scope_revision FROM "
                "billing_policies").fetchall(), [("a1", 1)])
            totals = conn.execute(
                "SELECT account_id, period_start, latest_micro_eur, "
                "high_water_micro_eur, conflict FROM billing_totals"
            ).fetchall()
            self.assertEqual(
                totals, [("a1", PERIOD_START, 5_000_000, 5_000_000, 0)])
            # The new tables start empty.
            for table in sorted(self.CARD4_TABLES):
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
        finally:
            conn.close()

    def test_newer_version_byte_untouched(self):
        db = self._v5_store("newer.db")
        conn = self._connect(db)
        try:
            conn.execute("PRAGMA user_version=6")
            conn.commit()
        finally:
            conn.close()
        with open(db, "rb") as handle:
            before = handle.read()
        result = run_cli("--db", db, "init")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_SCHEMA_UNSUPPORTED)
        with open(db, "rb") as handle:
            after = handle.read()
        self.assertEqual(after, before)
