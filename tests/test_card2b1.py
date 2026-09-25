"""Card 2b-1 acceptance tests: provider billing ingestion.

Run from the repository root:
    python -m unittest discover -s tests -v

Method names carry the card's check names with the unittest "test_" prefix:
billing_snapshot_schema, billing_migration_rollback, billing_retention,
billing_validation, billing_policy_enums, billing_owner_scope_revision,
billing_scope, billing_replay, billing_same_time_conflict,
billing_older_high_water, billing_time, billing_rollover, billing_tax_basis,
billing_output_errors, privacy_2b.

The CLI-level tests use now-relative source times with a margin where the
wall clock is involved; the exact boundaries (freshness at exactly
max_age, the 30-second future slop, the 13-month retention mark, the
half-micro rounding) are pinned against the pure functions with the clock
injected, as the design requires ("every row uses injected time/fixtures").
"""

import calendar
import copy
import datetime
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.environ.get("AGENTTALK_PY") or sys.executable

# The standard card-1 inventory: one metered account (a 100 EUR cap with the
# two gateway shares filling it exactly - the B06 whole-project shape), two
# machines, two gateways.
VALID_DOC = {
    "version": 1,
    "accounts": [
        {"alias": "meter", "vendor": "ovh", "kind": "metered",
         "budget_micro_eur": 100000000}
    ],
    "machines": [{"alias": "desktop"}, {"alias": "worker"}],
    "gateways": [
        {"alias": "g1", "account": "meter", "machine": "desktop",
         "share_micro_eur": 60000000, "period": "utc_month"},
        {"alias": "g2", "account": "meter", "machine": "worker",
         "share_micro_eur": 40000000, "period": "utc_month"},
    ],
}

ERROR_INVALID_CONFIG = '{"error":{"code":"INVALID_CONFIG","message":"config document is invalid"}}'
ERROR_SCHEMA_UNSUPPORTED = '{"error":{"code":"SCHEMA_UNSUPPORTED","message":"schema version is not supported"}}'
ERROR_IDEMPOTENCY = '{"error":{"code":"IDEMPOTENCY_CONFLICT","message":"a reading with the same id has different content"}}'
ERROR_STORE_NOT_FOUND = '{"error":{"code":"STORE_ERROR","message":"database not found"}}'
ERROR_STORE_FAILED = '{"error":{"code":"STORE_ERROR","message":"store operation failed"}}'

# Fixed UTC month bounds (epoch ms): the provider period of the fixture.
_MS_PER_DAY = 86_400_000


def _ms(year, month, day=1):
    return int(datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc)
               .timestamp() * 1000)


AUG = _ms(2026, 8)
SEP = _ms(2026, 9)
OCT = _ms(2026, 10)


def _add_months(ms, months):
    """Add (or subtract) calendar months to a UTC epoch-ms timestamp."""
    dt = datetime.datetime.fromtimestamp(ms // 1000, datetime.timezone.utc)
    idx = dt.year * 12 + dt.month - 1 + months
    year, month = divmod(idx, 12)
    month += 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return int(datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc)
               .timestamp() * 1000)


def _month_start(ms):
    """The UTC month start (the 1st, 00:00:00Z) at or before `ms`."""
    dt = datetime.datetime.fromtimestamp(ms // 1000, datetime.timezone.utc)
    return int(datetime.datetime(dt.year, dt.month, 1,
                                 tzinfo=datetime.timezone.utc)
               .timestamp() * 1000)


def _month_bounds_of(ms):
    """The UTC month period [start, end) containing `ms`."""
    start = _month_start(ms)
    return start, _add_months(start, 1)


def _now_ms():
    return int(time.time() * 1000)


# Section 7: reading IDs are local UUIDs. A deterministic UUID per label.
_EID_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # NAMESPACE_URL


def _eid(label):
    return str(uuid.uuid5(_EID_NS, "quota-card2b1/" + label))


def _write_json(path, doc):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)


def run_cli(*args):
    return subprocess.run(
        [PYTHON, "-m", "quota_balancer", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def _config_import(db, tmp, doc):
    cfg = os.path.join(tmp, "config.json")
    _write_json(cfg, doc)
    return run_cli("--db", db, "config-import", "--file", cfg)


def _readings_import(db, tmp, doc):
    path = os.path.join(tmp, "readings.json")
    _write_json(path, doc)
    return run_cli("--db", db, "reading-import", "--file", path)


def _billing_policy_import(db, tmp, doc):
    path = os.path.join(tmp, "billing-policy.json")
    _write_json(path, doc)
    return run_cli("--db", db, "billing-policy-import", "--file", path)


def _status_json(db):
    result = run_cli("--db", db, "status", "--json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _gateway_snapshot(event_id, machine, account, gateway, ledger_epoch, seq,
                      observed_at, period_start=None, period_end=None,
                      policy_hashes=None, sized_ready=False,
                      reconciliation_required=False):
    """A legacy (document version 2) gateway snapshot: metadata only, no
    money fields, nullable periods. Its money evidence stays unknown."""
    return {
        "event_id": event_id,
        "machine": machine,
        "account": account,
        "kind": "gateway_snapshot",
        "observed_at": observed_at,
        "payload": {
            "gateway": gateway,
            "ledger_epoch": ledger_epoch,
            "seq": seq,
            "period_start": period_start,
            "period_end": period_end,
            "policy_hashes": policy_hashes,
            "sized_ready": sized_ready,
            "reconciliation_required": reconciliation_required,
        },
    }


def _v3_gateway_snapshot(event_id, machine, account, gateway, ledger_epoch,
                         seq, observed_at, settled, reserved, uncertain,
                         unattributed, period_start=None, period_end=None,
                         policy_hashes=None, sized_ready=False,
                         reconciliation_required=False):
    """A document version 3 gateway snapshot: the metadata plus the four
    money fields and the required UTC-month period bounds."""
    if period_start is None:
        period_start, period_end = _month_bounds_of(observed_at)
    payload = {
        "gateway": gateway,
        "ledger_epoch": ledger_epoch,
        "seq": seq,
        "period_start": period_start,
        "period_end": period_end,
        "policy_hashes": policy_hashes,
        "sized_ready": sized_ready,
        "reconciliation_required": reconciliation_required,
        "settled_micro_eur": settled,
        "reserved_micro_eur": reserved,
        "uncertain_micro_eur": uncertain,
        "unattributed_micro_eur": unattributed,
    }
    return {
        "event_id": event_id,
        "machine": machine,
        "account": account,
        "kind": "gateway_snapshot",
        "observed_at": observed_at,
        "payload": payload,
    }


def _billing(event_id, machine, account, scope_alias, scope_revision,
             period_start, period_end, total, non_ai, observed_at,
             currency="EUR"):
    """A provider_billing reading: numbers and local aliases only."""
    return {
        "event_id": event_id,
        "machine": machine,
        "account": account,
        "kind": "provider_billing",
        "observed_at": observed_at,
        "payload": {
            "scope_alias": scope_alias,
            "scope_revision": scope_revision,
            "period_start": period_start,
            "period_end": period_end,
            "currency": currency,
            "total_micro_eur": total,
            "non_ai_spend_present": non_ai,
        },
    }


def _doc_v2(readings):
    return {"version": 2, "readings": readings}


def _doc_v3(readings):
    return {"version": 3, "readings": readings}


def _billing_policy(account="meter", reporter="desktop", scope="proj-ai",
                    revision=1, dedicated=True, allowance=0,
                    cap_basis="ex_vat", provider_basis="ex_vat",
                    ledger_basis="ex_vat", vat_bps=None,
                    poll=900, max_age=7200, lag=14400,
                    abs_tol=50000, rel_bps=100,
                    mode="advisory", stale_action="alert",
                    divergence_action="hold"):
    """The B06 policy shape: a 100 EUR ex-VAT whole-project cap, 19% would
    be the rate but no conversion is needed (all bases ex_vat), so the
    rate may be null."""
    return {
        "account": account,
        "reporter_machine": reporter,
        "scope_alias": scope,
        "scope_revision": revision,
        "dedicated_ai": dedicated,
        "non_ai_allowance_micro_eur": allowance,
        "cap_basis": cap_basis,
        "provider_basis": provider_basis,
        "ledger_basis": ledger_basis,
        "vat_bps": vat_bps,
        "poll_seconds": poll,
        "max_age_seconds": max_age,
        "lag_seconds": lag,
        "abs_tolerance_micro_eur": abs_tol,
        "relative_tolerance_bps": rel_bps,
        "mode": mode,
        "stale_action": stale_action,
        "divergence_action": divergence_action,
    }


def _policy_doc(policies):
    return {"version": 1, "policies": policies}


def _fingerprint(db):
    """Every row of every card-1..3 table: a semantic before/after snapshot
    (the WAL can hold committed pages outside the main file, so file bytes
    are not the right equality for a WAL-mode store)."""
    out = []
    conn = sqlite3.connect(db)
    try:
        for table in ("accounts", "machines", "gateways", "readings",
                      "windows", "billing_policies", "billing_totals"):
            try:
                out.append((table, conn.execute(
                    "SELECT * FROM " + table).fetchall()))
            except sqlite3.OperationalError:
                out.append((table, "<absent>"))
    finally:
        conn.close()
    return out


class _Base(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-card2b1-")
        self.tmp = self._tmp.name
        self.db = os.path.join(self.tmp, "inventory.db")
        run_cli("--db", self.db, "init")
        self.assertEqual(_config_import(self.db, self.tmp,
                                        copy.deepcopy(VALID_DOC)).returncode, 0)

    def tearDown(self):
        self._tmp.cleanup()

    def _status(self):
        return _status_json(self.db)

    def _billing(self):
        """The status billing projection, keyed by (account, period_start)."""
        totals = {}
        for entry in self._status()["billing"]:
            for total in entry["totals"]:
                totals[(entry["account"], total["period_start"])] = total
        return totals

    def _billing_policy_row(self, account_id=None):
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(
                "SELECT * FROM billing_policies").fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            conn.close()
        return rows

    def _fresh_db(self, name):
        """A second store in the same scratch dir, init'd and populated."""
        db = os.path.join(self.tmp, name)
        self.assertEqual(run_cli("--db", db, "init").returncode, 0)
        self.assertEqual(_config_import(db, self.tmp,
                                        copy.deepcopy(VALID_DOC)).returncode, 0)
        return db

    def _import_policy(self, db=None, **kwargs):
        doc = _policy_doc([_billing_policy(**kwargs)])
        return _billing_policy_import(db if db is not None else self.db,
                                      self.tmp, doc)


class BillingSnapshotSchema(_Base):
    """billing_snapshot_schema: legacy metadata-only snapshots keep their
    money evidence unknown; the new closed amounts/periods validate."""

    def test_billing_snapshot_schema(self):
        now = _now_ms()
        # Legacy (v2) metadata-only snapshot: imports; the report carries
        # no money fields at all - the money evidence stays unknown, never
        # zero.
        legacy = _doc_v2([_gateway_snapshot(_eid("snap-legacy"), "desktop",
                                            "meter", "g1", 1, 10, now - 1000)])
        self.assertEqual(_readings_import(self.db, self.tmp, legacy).returncode, 0)
        report = {g["alias"]: g["report"] for g in self._status()["gateways"]}["g1"]
        for key in ("settled_micro_eur", "reserved_micro_eur",
                    "uncertain_micro_eur", "unattributed_micro_eur"):
            self.assertNotIn(key, report)
        self.assertEqual(report["last_seq"], 10)

        # The new closed shape: the money fields are present and the
        # periods are required UTC-month bounds; it imports and reduces.
        ok = _doc_v3([_v3_gateway_snapshot(
            _eid("snap-v3"), "desktop", "meter", "g1", 1, 20, now - 500,
            settled=1_000_000, reserved=200_000, uncertain=0,
            unattributed=100_000)])
        self.assertEqual(_readings_import(self.db, self.tmp, ok).returncode, 0)
        report = {g["alias"]: g["report"] for g in self._status()["gateways"]}["g1"]
        self.assertEqual(report["last_seq"], 20)
        # The money evidence is recorded with the reading, not projected
        # into the report: the report still carries no money fields.
        for key in ("settled_micro_eur", "reserved_micro_eur",
                    "uncertain_micro_eur", "unattributed_micro_eur"):
            self.assertNotIn(key, report)

    def test_v3_snapshot_shape_is_closed(self):
        now = _now_ms()
        # A v3 document with a legacy (money-less) gateway payload: the
        # money fields are REQUIRED in version 3.
        bad = _doc_v3([_gateway_snapshot(_eid("snap-nomoney"), "desktop",
                                         "meter", "g1", 1, 30, now - 400)])
        result = _readings_import(self.db, self.tmp, bad)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)

        # A v3 gateway snapshot with a mid-month bound: the periods are
        # UTC-month bounds, full stop.
        bad = _doc_v3([_v3_gateway_snapshot(
            _eid("snap-midmonth"), "desktop", "meter", "g1", 1, 31, now - 400,
            settled=0, reserved=0, uncertain=0, unattributed=0,
            period_start=SEP + _MS_PER_DAY, period_end=OCT)])
        result = _readings_import(self.db, self.tmp, bad)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)

        # A v2 document does not accept the money fields (extra keys).
        bad = _doc_v2([_v3_gateway_snapshot(
            _eid("snap-v2money"), "desktop", "meter", "g1", 1, 32, now - 400,
            settled=1, reserved=0, uncertain=0, unattributed=0)])
        result = _readings_import(self.db, self.tmp, bad)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)

        # unattributed is a subset of settled (the one arithmetic the
        # closed shape can pin); a disjoint-category violation is rejected.
        bad = _doc_v3([_v3_gateway_snapshot(
            _eid("snap-unattr"), "desktop", "meter", "g1", 1, 33, now - 400,
            settled=100, reserved=0, uncertain=0, unattributed=101)])
        result = _readings_import(self.db, self.tmp, bad)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)

        # A v2 document cannot carry the provider_billing kind.
        bad = _doc_v2([_billing(_eid("snap-v2billing"), "desktop", "meter",
                                "proj-ai", 1, SEP, OCT, 100, False,
                                now - 400)])
        result = _readings_import(self.db, self.tmp, bad)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)


class BillingMigrationRollback(_Base):
    """billing_migration_rollback: a version-2 store with card-2 rows, an
    interruption after each (externally forceable) DDL of the version-4
    step - the upgrade rolls back whole and the v2 rows, IDs, payloads and
    windows survive; the clean upgrade (v2 -> v3 card 3 -> v4) preserves
    them into v4."""

    def _build_v2_store(self, name):
        import quota_balancer.migrate as migrate
        db = os.path.join(self.tmp, name)
        conn = sqlite3.connect(db)
        conn.executescript(migrate.MIGRATION_V1)
        conn.executescript(migrate.MIGRATION_V2)
        acc, mach, gw = "a-meter", "m-desktop", "g-g1"
        now = _now_ms()
        conn.execute(
            "INSERT INTO accounts(id, alias, vendor, kind, budget_micro_eur) "
            "VALUES(?,?,?,?,?)", (acc, "meter", "ovh", "metered", 100_000_000))
        conn.execute("INSERT INTO machines(id, alias) VALUES(?,?)", (mach, "desktop"))
        conn.execute(
            "INSERT INTO gateways(id, alias, account_id, machine_id, "
            "share_micro_eur, period) VALUES(?,?,?,?,?,?)",
            (gw, "g1", acc, mach, 100_000_000, "utc_month"))
        conn.execute(
            "UPDATE gateways SET ledger_epoch=3, last_seq=500, "
            "period_start=?, period_end=?, policy_hashes=?, last_report_at=?, "
            "state='ok' WHERE id=?",
            (SEP, OCT, '{"child_cap":"cc","price":"pp"}', now - 1000, gw))
        conn.execute(
            "INSERT INTO windows(account_id, window_key, used_bps, resets_at, "
            "observed_at, confidence, source_stale, covered_seq, "
            "source_event_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (acc, "wk", 4200, now - 60_000, now - 60_000, "observed", 0,
             7, "w-event"))
        ev1, ev2 = _eid("v2-r1"), _eid("v2-r2")
        conn.execute(
            "INSERT INTO readings(event_id, machine_id, account_id, seat_id, "
            "kind, observed_at, received_at, payload) VALUES(?,?,?,?,?,?,?,?)",
            (ev1, mach, acc, None, "cli_window", now - 60_000, now - 59_000,
             '{"windows":[{"key":"wk","used_bps":4200,"resets_at":'
             + str(now - 60_000) + '}]}' + ','
             '"confidence":"observed","source_stale":false,"covered_seq":7}'))
        conn.execute(
            "INSERT INTO readings(event_id, machine_id, account_id, seat_id, "
            "kind, observed_at, received_at, payload) VALUES(?,?,?,?,?,?,?,?)",
            (ev2, mach, acc, None, "gateway_snapshot", now - 1000, now,
             '{"gateway":"g1","ledger_epoch":3,"seq":500}'))
        conn.commit()
        conn.close()
        return db, ev1, ev2

    def _assert_v2_intact(self, db, ev1, ev2):
        conn = sqlite3.connect(db)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, 2)
            readings_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='readings'"
            ).fetchone()[0]
            self.assertIn("'cli_window','gateway_snapshot'", readings_sql)
            self.assertNotIn("provider_billing", readings_sql)
            rows = conn.execute(
                "SELECT event_id, kind, payload FROM readings "
                "ORDER BY event_id").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(sorted(r[0] for r in rows), sorted([ev1, ev2]))
            windows = conn.execute(
                "SELECT window_key, used_bps, source_event_id FROM windows"
            ).fetchall()
            self.assertEqual(windows, [("wk", 4200, "w-event")])
            report = conn.execute(
                "SELECT last_seq, ledger_epoch, state FROM gateways"
            ).fetchone()
            self.assertEqual(report, (500, 3, "ok"))
            # The FKs still work: the report's owner joins resolve.
            owner = conn.execute(
                "SELECT a.alias, m.alias FROM gateways g "
                "JOIN accounts a ON a.id=g.account_id "
                "JOIN machines m ON m.id=g.machine_id"
            ).fetchone()
            self.assertEqual(owner, ("meter", "desktop"))
        finally:
            conn.close()

    def test_billing_migration_rollback(self):
        # A foreign object colliding with each forceable DDL of step 4:
        # before the rebuild, after the rebuild, and after billing_policies.
        foreign_tables = (
            "CREATE TABLE readings_new (x INTEGER)",
            "CREATE TABLE billing_policies (x INTEGER)",
            "CREATE TABLE billing_totals (x INTEGER)",
        )
        for index, foreign in enumerate(foreign_tables):
            db, ev1, ev2 = self._build_v2_store("rollback-%d.db" % index)
            conn = sqlite3.connect(db)
            conn.execute(foreign)
            conn.commit()
            conn.close()
            bad = run_cli("--db", db, "init")
            self.assertEqual(bad.returncode, 3)
            self.assertEqual(bad.stderr.strip(), ERROR_STORE_FAILED)
            self._assert_v2_intact(db, ev1, ev2)

        # The clean upgrade: v2 -> v4 (through card 3's v3) preserves rows, IDs, payloads and
        # windows, and the new kind CHECK admits provider_billing while
        # still refusing the future usage kind.
        db, ev1, ev2 = self._build_v2_store("clean.db")
        good = run_cli("--db", db, "init")
        self.assertEqual(good.returncode, 0, good.stderr)
        conn = sqlite3.connect(db)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
            readings_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='readings'"
            ).fetchone()[0]
            self.assertIn("provider_billing", readings_sql)
            self.assertNotIn("'usage'", readings_sql)
            rows = conn.execute(
                "SELECT event_id, kind, payload FROM readings "
                "ORDER BY event_id").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(sorted(r[0] for r in rows), sorted([ev1, ev2]))
            windows = conn.execute(
                "SELECT window_key, used_bps FROM windows").fetchall()
            self.assertEqual(windows, [("wk", 4200)])
            conn.execute(
                "PRAGMA foreign_keys=ON")
            mach = conn.execute(
                "SELECT id FROM machines WHERE alias='desktop'").fetchone()[0]
            acc = conn.execute(
                "SELECT id FROM accounts WHERE alias='meter'").fetchone()[0]
            ev3 = _eid("v3-new")
            conn.execute(
                "INSERT INTO readings(event_id, machine_id, account_id, "
                "seat_id, kind, observed_at, received_at, payload) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (ev3, mach, acc, None, "provider_billing", SEP, SEP, "{}"))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO readings(event_id, machine_id, account_id, "
                    "seat_id, kind, observed_at, received_at, payload) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (_eid("v3-usage"), mach, acc, None, "usage", SEP, SEP,
                     "{}"))
        finally:
            conn.close()


class BillingRetention(_Base):
    """billing_retention: the pure eligibility predicate keeps the
    30-day/13-month/unresolved rules pinned, and 2b does no deletion."""

    def test_billing_retention(self):
        from quota_balancer.reduce import billing_retention_eligible
        # Resolved evidence is eligible only once its period ended at least
        # 13 calendar months before now - the boundary is inclusive.
        end = _ms(2026, 10)
        due = _ms(2027, 11)  # exactly 13 calendar months after 2026-10-01
        self.assertTrue(billing_retention_eligible(end, False, due))
        self.assertFalse(billing_retention_eligible(end, False, due - 1))
        self.assertFalse(
            billing_retention_eligible(end, False, _ms(2027, 10) + _MS_PER_DAY))
        # Calendar months, not 30-day months: 2025-01-01 + 13 is 2026-02-01.
        self.assertTrue(
            billing_retention_eligible(_ms(2025, 1), False, _ms(2026, 2)))
        self.assertFalse(
            billing_retention_eligible(_ms(2025, 1), False, _ms(2026, 2) - 1))
        # A period that has not ended yet is never eligible.
        self.assertFalse(billing_retention_eligible(due + 1, False, due))
        # Unresolved (conflicted) evidence is retained indefinitely.
        self.assertFalse(billing_retention_eligible(end, True, due))
        self.assertFalse(
            billing_retention_eligible(end, True, _add_months(due, 12)))

    def test_no_pruning_in_card_2b_1(self):
        # 2b does no deletion: a reading older than the 30-day readings
        # retention and a resolved total whose period ended more than 13
        # months ago both survive init and status.
        self.assertEqual(self._import_policy().returncode, 0)
        now = _now_ms()
        # Reading one: observed 40 days ago (past the 30-day mark).
        old_obs = now - 40 * _MS_PER_DAY
        start, end = _month_bounds_of(old_obs)
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc_v3([_billing(_eid("ret-r1"), "desktop", "meter", "proj-ai",
                              1, start, end, 1_000_000, False, old_obs)]
        )).returncode, 0)
        # Reading two: a period that ended 14 months ago (past the 13-month
        # financial mark).
        period_end = _add_months(_month_start(now), -14)
        period_start = _add_months(period_end, -1)
        old_obs2 = period_start + 14 * _MS_PER_DAY
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc_v3([_billing(_eid("ret-r2"), "desktop", "meter", "proj-ai",
                              1, period_start, period_end, 2_000_000, False,
                              old_obs2)]
        )).returncode, 0)
        # init (idempotent) and status touch nothing.
        self.assertEqual(run_cli("--db", self.db, "init").returncode, 0)
        self._status()
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 2)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM billing_totals").fetchone()[0], 2)
        finally:
            conn.close()


class BillingValidation(_Base):
    """billing_validation: the closed provider_billing shape - bool-as-int,
    floats, negatives, overflow, extra keys, a wire seat field, wrong
    owner/type - is rejected with exit 2 and an unchanged store; telemetry
    never changes a policy."""

    def _good(self, label, observed=None, now=None):
        now = now if now is not None else _now_ms()
        observed = observed if observed is not None else now - 1000
        start, end = _month_bounds_of(observed)
        return _billing(_eid(label), "desktop", "meter", "proj-ai", 1,
                        start, end, 100, False, observed)

    def test_billing_validation(self):
        # Baseline: the policy and a valid reading import.
        self.assertEqual(self._import_policy().returncode, 0)
        before = _fingerprint(self.db)
        mutations = []
        good = self._good("val-baseline")

        def mutated(payload_changes=None, reading_changes=None):
            doc = copy.deepcopy(good)
            if payload_changes:
                doc["payload"].update(payload_changes)
            if reading_changes:
                doc.update(reading_changes)
            return doc

        # bool-as-int, float, negative, and overflow money.
        mutations.append(mutated({"total_micro_eur": True}))
        mutations.append(mutated({"total_micro_eur": 14.3}))
        mutations.append(mutated({"total_micro_eur": -1}))
        mutations.append(mutated({"total_micro_eur": 2 ** 63}))
        # Extra keys at every level: a provider ID on the wire, and a wire
        # seat field on the reading (the closed shape has no seat key).
        mutations.append(mutated({"provider_id": "PROVIDER-9f8e7d6c5b4a"}))
        mutations.append(mutated(reading_changes={"seat": "seat-1"}))
        # Wrong owner/type: a currency that is not the provider's one, a
        # non-boolean flag, an account not in the store, revision 0.
        mutations.append(mutated({"currency": "USD"}))
        mutations.append(mutated({"non_ai_spend_present": "yes"}))
        mutations.append(mutated(reading_changes={"account": "ghost"}))
        mutations.append(mutated({"scope_revision": 0}))
        # A mid-month period bound (the source time sits inside the bogus
        # bounds, so this is the only violation in the document).
        mid = self._good("val-mid", observed=SEP + 2 * _MS_PER_DAY)
        mid["payload"]["period_start"] = SEP + _MS_PER_DAY
        mutations.append(mid)
        # A future source time beyond the 30-second slop: the slop is
        # checked against the CLI's own now, which advances while the
        # earlier imports run, so the future reading is built at the moment
        # of its own import (the exact 30 s / +1 ms boundary is pinned with
        # an injected clock in billing_time).
        def _future_reading():
            return self._good("val-future", observed=_now_ms() + 31_000)

        mutations.append(_future_reading)

        for doc in mutations:
            if callable(doc):
                doc = doc()
            result = _readings_import(self.db, self.tmp, _doc_v3([doc]))
            self.assertEqual(result.returncode, 2,
                             json.dumps(doc["payload"]))
            self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)
        # Every rejection left the store untouched.
        self.assertEqual(_fingerprint(self.db), before)

        # A valid reading still imports, and telemetry (a good or a bad
        # reading import) never changes the operator-owned policy: the
        # operator's own update (max_age 3600) must survive both.
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc_v3([self._good("val-after")])).returncode, 0)
        self.assertEqual(self._import_policy(max_age=3600).returncode, 0)
        bad = _readings_import(
            self.db, self.tmp,
            _doc_v3([mutations[0]]))
        self.assertEqual(bad.returncode, 2)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT max_age_seconds FROM billing_policies").fetchone()[0],
                3600)
        finally:
            conn.close()


class BillingPolicyEnums(_Base):
    """billing_policy_enums: the closed policy enums, the dedicated-only
    designation, the bounded bps/seconds/money fields; an unknown basis is
    permitted configuration."""

    def test_billing_policy_enums(self):
        # The B06 policy (ex_vat everywhere, no rate needed) imports.
        self.assertEqual(self._import_policy().returncode, 0)

        # Closed enums: every other value is rejected.
        for field, bad_value in (
            ("mode", "enforcing"),
            ("stale_action", "hold"),
            ("divergence_action", "alert"),
            ("cap_basis", "gross"),
            ("provider_basis", "net"),
            ("ledger_basis", "vat_net"),
        ):
            result = self._import_policy(**{field: bad_value})
            self.assertEqual(result.returncode, 2, field)
            self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)

        # dedicated_ai must be exactly true (a non-dedicated project never
        # gets a whole-project cap).
        for bad_value in (False, "true", 1):
            result = self._import_policy(dedicated=bad_value)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)

        # Bounded fields at their edges.
        self.assertEqual(self._import_policy(vat_bps=10000).returncode, 0)
        for bad_value in (10001, -1, "1900", True):
            result = self._import_policy(vat_bps=bad_value)
            self.assertEqual(result.returncode, 2, repr(bad_value))
        self.assertEqual(self._import_policy(rel_bps=10000).returncode, 0)
        result = self._import_policy(rel_bps=10001)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self._import_policy(poll=1).returncode, 0)
        for field in ("poll", "max_age", "lag"):
            result = self._import_policy(**{field: 0})
            self.assertEqual(result.returncode, 2, field)
        self.assertEqual(
            self._import_policy(abs_tol=0).returncode, 0)
        result = self._import_policy(abs_tol=-1)
        self.assertEqual(result.returncode, 2)

        # Closed document shape: a duplicate account in one document, an
        # unknown account, an unknown reporter machine, a missing key.
        dup = _policy_doc([_billing_policy(), _billing_policy()])
        result = _billing_policy_import(self.db, self.tmp, dup)
        self.assertEqual(result.returncode, 2)
        result = self._import_policy(account="ghost")
        self.assertEqual(result.returncode, 2)
        result = self._import_policy(reporter="ghost")
        self.assertEqual(result.returncode, 2)
        doc = _policy_doc([_billing_policy()])
        del doc["policies"][0]["lag_seconds"]
        result = _billing_policy_import(self.db, self.tmp, doc)
        self.assertEqual(result.returncode, 2)

        # Unknown basis is permitted configuration (no readings exist, so
        # no comparison was owed): every basis may be unknown.
        self.assertEqual(self._import_policy(
            cap_basis="unknown", provider_basis="unknown",
            ledger_basis="unknown").returncode, 0)


class BillingOwnerScopeRevision(_Base):
    """billing_owner_scope_revision: a reading for an account with no
    policy, a foreign machine, an inactive revision, a bad alias, a
    non-dedicated project, or shares+allowance>cap is rejected on
    import."""

    def test_billing_owner_scope_revision(self):
        # A provider_billing reading for an account with NO policy is
        # rejected from any machine, scope alias or revision: with no
        # policy the owner is unknown, so no foreign total may enter
        # billing_totals (section 11: one active scope/reporter owner per
        # account).
        now = _now_ms()
        start, end = _month_bounds_of(now - 1000)
        before = _fingerprint(self.db)
        no_policy = _billing(_eid("osr-nopolicy"), "worker", "meter",
                             "other-proj", 7, start, end, 50_000_000,
                             False, now - 1000)
        result = _readings_import(self.db, self.tmp, _doc_v3([no_policy]))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)
        self.assertEqual(_fingerprint(self.db), before)

        # Policy-import rejections, before any policy exists.
        result = self._import_policy(dedicated=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)
        # shares (60+40 EUR) + allowance (1 micro-EUR) > cap (100 EUR).
        result = self._import_policy(allowance=1)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)
        # A subscription account has no cap to bound an allowance.
        doc = copy.deepcopy(VALID_DOC)
        doc["accounts"].append(
            {"alias": "subs", "vendor": "anthropic", "kind": "subscription",
             "budget_micro_eur": None})
        self.assertEqual(_config_import(self.db, self.tmp, doc).returncode, 0)
        result = self._import_policy(account="subs")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)

        # The good B06 policy imports; then the reading-side owner/scope
        # checks.
        self.assertEqual(self._import_policy().returncode, 0)
        now = _now_ms()
        start, end = _month_bounds_of(now - 1000)
        before = _fingerprint(self.db)
        rejections = [
            # A foreign machine (the policy's reporter is desktop).
            _billing(_eid("osr-foreign"), "worker", "meter", "proj-ai", 1,
                     start, end, 100, False, now - 1000),
            # An inactive scope revision (the policy's is 1).
            _billing(_eid("osr-revision"), "desktop", "meter", "proj-ai", 2,
                     start, end, 100, False, now - 1000),
            # A scope alias the policy does not designate.
            _billing(_eid("osr-alias"), "desktop", "meter", "other-scope", 1,
                     start, end, 100, False, now - 1000),
            # An account that is not in the store.
            _billing(_eid("osr-account"), "desktop", "ghost", "proj-ai", 1,
                     start, end, 100, False, now - 1000),
        ]
        for reading in rejections:
            result = _readings_import(self.db, self.tmp, _doc_v3([reading]))
            self.assertEqual(result.returncode, 2, json.dumps(reading))
            self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)
        self.assertEqual(_fingerprint(self.db), before)

        # A valid reading (the policy's own machine, revision and alias)
        # imports.
        ok = _billing(_eid("osr-ok"), "desktop", "meter", "proj-ai", 1,
                      start, end, 100, False, now - 1000)
        self.assertEqual(_readings_import(
            self.db, self.tmp, _doc_v3([ok])).returncode, 0)
        self.assertEqual(self._billing()[("meter", start)]["latest_micro_eur"],
                         100)


class BillingConfigInvariant(_Base):
    """billing_config_invariant: config-import cannot break the invariants
    a billing policy depends on - the account stays metered, its gateway
    shares plus the allowance stay within the cap (B01, re-checked on the
    policy's cap basis), and an account or machine the billing tables
    reference cannot be removed (INVALID_CONFIG, not a store fault)."""

    def test_billing_config_invariant(self):
        # Start with headroom: shares 70M (30 + 40) leave room for a
        # 20M allowance - 70 + 20 <= 100, so the policy imports.
        doc = copy.deepcopy(VALID_DOC)
        doc["gateways"][0]["share_micro_eur"] = 30_000_000
        self.assertEqual(_config_import(self.db, self.tmp, doc).returncode, 0)
        self.assertEqual(self._import_policy(
            allowance=20_000_000).returncode, 0)
        before = _fingerprint(self.db)

        # B01 at config-import: raising g1 to 50M keeps the shares within
        # the account cap (card 1: 50 + 40 = 90 <= 100) but
        # shares + allowance (90 + 20 = 110) exceeds it - the policy's
        # allocation invariant must refuse it.
        doc = copy.deepcopy(VALID_DOC)
        doc["gateways"][0]["share_micro_eur"] = 50_000_000
        result = _config_import(self.db, self.tmp, doc)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)
        # Lowering the cap below shares + allowance (while still above the
        # shares: 70 <= 85) is refused the same way: 70 + 20 > 85.
        doc = copy.deepcopy(VALID_DOC)
        doc["gateways"][0]["share_micro_eur"] = 30_000_000
        doc["accounts"][0]["budget_micro_eur"] = 85_000_000
        result = _config_import(self.db, self.tmp, doc)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)
        # A share change that keeps shares + allowance within the cap
        # imports: 20 + 40 = 60, 60 + 20 <= 100.
        doc = copy.deepcopy(VALID_DOC)
        doc["gateways"][0]["share_micro_eur"] = 20_000_000
        self.assertEqual(_config_import(self.db, self.tmp, doc).returncode, 0)
        # An account with a policy cannot be turned into a subscription
        # (a non-metered account has no cap to bound an allowance; policy
        # import itself would refuse it).
        before = _fingerprint(self.db)
        doc = copy.deepcopy(VALID_DOC)
        doc["accounts"][0]["kind"] = "subscription"
        doc["accounts"][0]["budget_micro_eur"] = None
        result = _config_import(self.db, self.tmp, doc)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)
        self.assertEqual(_fingerprint(self.db), before)

        # A reduced total exists now, so the billing tables reference the
        # account (and the policy references its reporter machine): a
        # config-import removing either is INVALID_CONFIG (exit 2), not a
        # foreign-key fault (exit 3).
        now = _now_ms()
        start, end = _month_bounds_of(now - 1000)
        ok = _billing(_eid("cfg-ok"), "desktop", "meter", "proj-ai", 1,
                      start, end, 100, False, now - 1000)
        self.assertEqual(_readings_import(
            self.db, self.tmp, _doc_v3([ok])).returncode, 0)
        before = _fingerprint(self.db)
        # Remove the policy's reporter machine: on a store where no
        # reading references it (the reference is the policy row alone),
        # the removal is INVALID_CONFIG (exit 2), not a foreign-key fault
        # (exit 3).
        db = self._fresh_db("cfg-machine.db")
        self.assertEqual(self._import_policy(db=db,
                                             reporter="worker").returncode, 0)
        doc = copy.deepcopy(VALID_DOC)
        doc["machines"] = [{"alias": "desktop"}]
        doc["gateways"] = [g for g in doc["gateways"]
                           if g["machine"] == "desktop"]
        result = _config_import(db, self.tmp, doc)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)
        # Remove the account the billing tables reference (the document
        # itself stays valid with an unrelated account).
        doc = copy.deepcopy(VALID_DOC)
        doc["accounts"] = [
            {"alias": "other", "vendor": "ovh", "kind": "metered",
             "budget_micro_eur": 1_000_000}
        ]
        doc["gateways"] = []
        result = _config_import(self.db, self.tmp, doc)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)
        self.assertEqual(_fingerprint(self.db), before)


class BillingScope(_Base):
    """billing_scope: a replica copy never adds totals, and a policy
    change with history is refused (owner/scope/basis are frozen until the
    later resolution integration)."""

    def test_billing_scope(self):
        self.assertEqual(self._import_policy().returncode, 0)
        now = _now_ms()
        t = now - 1000
        start, end = _month_bounds_of(t)
        ev_a = _eid("scope-a")

        # The reporter's reading reduces.
        reading = _billing(ev_a, "desktop", "meter", "proj-ai", 1, start,
                           end, 100, False, t)
        self.assertEqual(_readings_import(
            self.db, self.tmp, _doc_v3([reading])).returncode, 0)
        before = _fingerprint(self.db)

        # A replica copy (the same observation, a different event, sent by
        # the other machine) is a foreign machine: rejected, and it never
        # adds totals.
        replica = _billing(_eid("scope-replica"), "worker", "meter",
                           "proj-ai", 1, start, end, 100, False, t)
        result = _readings_import(self.db, self.tmp, _doc_v3([replica]))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)
        self.assertEqual(_fingerprint(self.db), before)
        total = self._billing()[("meter", start)]
        self.assertEqual(total["latest_micro_eur"], 100)
        self.assertEqual(total["conflict"], False)

        # Policy changes with history are refused: owner (reporter), scope
        # (alias, revision) and basis changes are frozen once a billing
        # reading exists.
        for change in (
            {"reporter": "worker"},
            {"scope": "other-scope"},
            {"revision": 2},
            {"cap_basis": "invoice_total"},
            {"provider_basis": "invoice_total"},
            {"ledger_basis": "invoice_total"},
        ):
            result = self._import_policy(**change)
            self.assertEqual(result.returncode, 2, change)
            self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)
        self.assertEqual(_fingerprint(self.db), before)

        # The same policy re-imports (an upsert, unchanged), and the
        # operator-tunable fields still update.
        self.assertEqual(self._import_policy().returncode, 0)
        self.assertEqual(self._import_policy(max_age=3600).returncode, 0)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT max_age_seconds FROM billing_policies"
                ).fetchone()[0], 3600)
        finally:
            conn.close()


class BillingReplay(_Base):
    """billing_replay: a same-ID replay is a no-op; a same-ID collision
    rolls back; an unchanged observation under a new ID never refreshes
    the reduced total."""

    def test_billing_replay(self):
        self.assertEqual(self._import_policy().returncode, 0)
        now = _now_ms()
        t = now - 1000
        start, end = _month_bounds_of(t)
        reading = _billing(_eid("replay-a"), "desktop", "meter", "proj-ai",
                           1, start, end, 100, False, t)
        doc = _doc_v3([reading])
        self.assertEqual(_readings_import(self.db, self.tmp, doc).returncode, 0)
        before = _fingerprint(self.db)
        total = self._billing()[("meter", start)]
        self.assertEqual(total["latest_micro_eur"], 100)
        self.assertEqual(total["observed_at"], t)

        # Same ID, same content: a no-op (no double counting).
        self.assertEqual(_readings_import(
            self.db, self.tmp, doc).returncode, 0)
        self.assertEqual(_fingerprint(self.db), before)

        # Same ID, different content: the whole batch rolls back.
        collision = copy.deepcopy(reading)
        collision["payload"]["total_micro_eur"] = 150
        result = _readings_import(
            self.db, self.tmp, _doc_v3([collision]))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_IDEMPOTENCY)
        self.assertEqual(_fingerprint(self.db), before)

        # A new ID for the same unchanged observation: recorded, but the
        # reduced total keeps its amount, its source time and its event.
        unchanged = _billing(_eid("replay-b"), "desktop", "meter", "proj-ai",
                             1, start, end, 100, False, t)
        self.assertEqual(_readings_import(
            self.db, self.tmp, _doc_v3([unchanged])).returncode, 0)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0],
                2)
        finally:
            conn.close()
        total = self._billing()[("meter", start)]
        self.assertEqual(total["latest_micro_eur"], 100)
        self.assertEqual(total["observed_at"], t)
        self.assertFalse(total["conflict"])


class BillingSameTimeConflict(_Base):
    """billing_same_time_conflict: equal source times with unequal totals
    latch a conflict; the latest event is the UUID-sort winner, so the
    reduced display is deterministic regardless of arrival order."""

    def test_billing_same_time_conflict(self):
        t = SEP + 10 * _MS_PER_DAY  # a fixed source time in the past
        start, end = _month_bounds_of(t)
        ev_a, ev_b = _eid("stc-a"), _eid("stc-b")
        outcomes = []
        # Both arrival orders, on two separate stores, must reduce to the
        # same row: (latest_event, latest, high_water, conflict, observed).
        for first_ev, first_amount in ((ev_a, 100), (ev_b, 150)):
            other_ev = ev_b if first_ev == ev_a else ev_a
            other_amount = 100 if first_amount == 150 else 150
            db = self._fresh_db("stc-%s.db" % first_ev[:8])
            self.assertEqual(_billing_policy_import(
                db, self.tmp, _policy_doc([_billing_policy()])
            ).returncode, 0)
            for ev, amount in ((first_ev, first_amount),
                               (other_ev, other_amount)):
                reading = _billing(ev, "desktop", "meter", "proj-ai", 1,
                                   start, end, amount, False, t)
                self.assertEqual(_readings_import(
                    db, self.tmp, _doc_v3([reading])).returncode, 0,
                    (ev, amount))
            conn = sqlite3.connect(db)
            try:
                row = conn.execute(
                    "SELECT latest_event_id, latest_micro_eur, "
                    "high_water_micro_eur, conflict, observed_at "
                    "FROM billing_totals").fetchone()
            finally:
                conn.close()
            outcomes.append(row)
        # Arrival-order independent: the same reduced row either way.
        self.assertEqual(outcomes[0], outcomes[1])
        latest_event, latest, high_water, conflict, observed = outcomes[0]
        self.assertEqual(latest_event, max(ev_a, ev_b))
        self.assertEqual(latest, 150 if ev_b > ev_a else 100)
        self.assertEqual(high_water, 150)
        self.assertEqual(conflict, 1)
        self.assertEqual(observed, t)


class BillingOlderHighWater(_Base):
    """billing_older_high_water: an older higher total raises the
    high-water and latches a conflict without refreshing the stored
    (latest) source time; an older lower total changes nothing."""

    def test_billing_older_high_water(self):
        self.assertEqual(self._import_policy().returncode, 0)
        t2 = SEP + 20 * _MS_PER_DAY
        t1 = SEP + 10 * _MS_PER_DAY
        t0 = SEP + 1 * _MS_PER_DAY
        start, end = _month_bounds_of(t1)
        ev_new = _eid("hw-new")
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc_v3([_billing(ev_new, "desktop", "meter", "proj-ai", 1,
                              start, end, 100, False, t2)]
        )).returncode, 0)

        # An OLDER observation reporting a HIGHER total: the high-water
        # rises to the older amount (with its own event), the latest keeps
        # the newer amount and its source time, and the conflict latches.
        ev_high = _eid("hw-high")
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc_v3([_billing(ev_high, "desktop", "meter", "proj-ai", 1,
                              start, end, 150, False, t1)]
        )).returncode, 0)
        total = self._billing()[("meter", start)]
        self.assertEqual(total["latest_micro_eur"], 100)
        self.assertEqual(total["observed_at"], t2)
        self.assertEqual(total["high_water_micro_eur"], 150)
        self.assertTrue(total["conflict"])

        # An older LOWER total: the projection changes nothing and no
        # other table is touched; the new evidence is still recorded as
        # a raw reading with its own source time (section 11). (The
        # status projection carries wall-clock age, so the tables - not
        # the status - are the right equality here.)
        other_before = {t: rows for t, rows in _fingerprint(self.db)
                        if t != "readings"}
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc_v3([_billing(_eid("hw-low"), "desktop", "meter", "proj-ai",
                              1, start, end, 50, False, t0)]
        )).returncode, 0)
        other_after = {t: rows for t, rows in _fingerprint(self.db)
                       if t != "readings"}
        self.assertEqual(other_after, other_before)
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT observed_at, payload FROM readings "
                "WHERE event_id = ?", (_eid("hw-low"),)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], t0)
        self.assertEqual(json.loads(row[1])["total_micro_eur"], 50)


class BillingTime(_Base):
    """billing_time: the exact freshness and future boundaries (injected
    time), a failed/unchanged poll never refreshes the source time, and
    there is no idle exemption - a just-ingested old reading is still
    stale."""

    def test_billing_time(self):
        # The exact freshness boundary, with the clock injected: fresh iff
        # the source age is at most max_age (7200 s under B02), no idle
        # exemption.
        from quota_balancer.reduce import billing_freshness
        now = _ms(2026, 9) + 24 * _MS_PER_DAY
        self.assertEqual(
            billing_freshness(now - 7_200_000, 7_200_000, now),
            (True, "fresh"))
        self.assertEqual(
            billing_freshness(now - 7_200_001, 7_200_000, now),
            (False, "stale"))
        # Without a policy there is no max_age: the freshness is unknown,
        # never assumed fresh.
        self.assertEqual(billing_freshness(now, None, now), (None, "unknown"))

        # The exact future boundary at ingest (the 30-second slop), with
        # the ingest clock injected.
        from quota_balancer import validate_readings
        from quota_balancer.errors import QuotaError, CODE_INVALID_CONFIG
        fixed_now = _ms(2026, 9) + 24 * _MS_PER_DAY
        start, end = _month_bounds_of(fixed_now - 1000)
        ok = _billing(_eid("time-future-ok"), "desktop", "meter", "proj-ai",
                      1, start, end, 100, False,
                      fixed_now + 30_000)  # exactly 30 s: allowed
        self.assertEqual(
            len(validate_readings.validate_readings(_doc_v3([ok]), fixed_now)),
            1)
        bad = _billing(_eid("time-future-bad"), "desktop", "meter",
                       "proj-ai", 1, start, end, 100, False,
                       fixed_now + 30_001)  # 30 s + 1 ms: rejected
        with self.assertRaises(QuotaError) as ctx:
            validate_readings.validate_readings(
                _doc_v3([bad]), fixed_now)
        self.assertEqual(ctx.exception.code, CODE_INVALID_CONFIG)

        # Through the CLI (wall clock, with margin): a 7198 s old source is
        # fresh under the 7200 s max_age, a months-old source ingested a
        # moment ago is stale - the unchanged/failed poll never refreshes
        # the source time, and there is no idle exemption.
        self.assertEqual(self._import_policy().returncode, 0)
        now = _now_ms()
        fresh_obs = now - 7_198_000
        fresh_start, fresh_end = _month_bounds_of(fresh_obs)
        stale_obs = _add_months(_month_start(now), -1) + 10 * _MS_PER_DAY
        stale_start = _month_start(stale_obs)
        stale_end = _add_months(stale_start, 1)
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc_v3([
                _billing(_eid("time-fresh"), "desktop", "meter", "proj-ai",
                         1, fresh_start, fresh_end, 100, False, fresh_obs),
                _billing(_eid("time-stale"), "desktop", "meter", "proj-ai",
                         1, stale_start, stale_end, 90, False, stale_obs),
            ])).returncode, 0)
        totals = self._billing()
        fresh = totals[("meter", fresh_start)]
        self.assertTrue(fresh["fresh"])
        self.assertEqual(fresh["freshness_reason"], "fresh")
        stale = totals[("meter", stale_start)]
        self.assertFalse(stale["fresh"])
        self.assertEqual(stale["freshness_reason"], "stale")
        # A replay of the fresh reading does not refresh its source time.
        before = _fingerprint(self.db)
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc_v3([_billing(_eid("time-fresh"), "desktop", "meter",
                              "proj-ai", 1, fresh_start, fresh_end, 100,
                              False, fresh_obs)]
        )).returncode, 0)
        self.assertEqual(_fingerprint(self.db), before)


class BillingRollover(_Base):
    """billing_rollover: periods are independent - a late prior-month
    reading updates only that month, and a new month with no reading is
    unknown, never zero."""

    def test_billing_rollover(self):
        self.assertEqual(self._import_policy().returncode, 0)
        # The new month (September) has its first reading...
        sep_obs = SEP + 2 * _MS_PER_DAY
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc_v3([_billing(_eid("ro-s1"), "desktop", "meter", "proj-ai",
                              1, SEP, OCT, 5, False, sep_obs)]
        )).returncode, 0)
        # ...then a LATE prior-month (August) reading arrives. It updates
        # only August.
        aug_obs = AUG + 20 * _MS_PER_DAY
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc_v3([_billing(_eid("ro-a1"), "desktop", "meter", "proj-ai",
                              1, AUG, SEP, 105, False, aug_obs)]
        )).returncode, 0)
        totals = self._billing()
        self.assertEqual(totals[("meter", SEP)]["latest_micro_eur"], 5)
        self.assertEqual(totals[("meter", AUG)]["latest_micro_eur"], 105)
        # A further late update within the prior month: still only August.
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc_v3([_billing(_eid("ro-a2"), "desktop", "meter", "proj-ai",
                              1, AUG, SEP, 108, False, AUG + 25 * _MS_PER_DAY)]
        )).returncode, 0)
        totals = self._billing()
        self.assertEqual(totals[("meter", AUG)]["latest_micro_eur"], 108)
        self.assertEqual(totals[("meter", AUG)]["observed_at"],
                         AUG + 25 * _MS_PER_DAY)
        self.assertEqual(totals[("meter", SEP)]["latest_micro_eur"], 5)
        self.assertEqual(totals[("meter", SEP)]["observed_at"], sep_obs)
        # The next month (October) has no reading: it is unknown, not zero
        # - there is simply no row, and the prior month's floor is never
        # carried into it.
        self.assertNotIn(("meter", OCT), totals)
        conn = sqlite3.connect(self.db)
        try:
            starts = [r[0] for r in conn.execute(
                "SELECT period_start FROM billing_totals ORDER BY period_start"
            )]
        finally:
            conn.close()
        self.assertEqual(starts, [AUG, SEP])


class BillingTaxBasis(_Base):
    """billing_tax_basis: the pure shared conversion - 100 EUR stays 100
    before VAT, half-micro fractions round up, an unknown basis defers -
    and the allocation check (shares + allowance <= cap) runs on one basis."""

    def test_billing_tax_basis(self):
        from quota_balancer.reduce import convert_basis
        from quota_balancer.errors import (
            QuotaError,
            CODE_BILLING_BASIS_UNKNOWN,
            CODE_INVALID_CONFIG,
        )

        # B06: the 100 EUR ex-VAT cap is compared on its own basis - equal
        # bases convert to themselves and need no rate.
        self.assertEqual(
            convert_basis(100_000_000, "ex_vat", "ex_vat", None),
            100_000_000)

        # ex-VAT -> invoice total at 19%: exact, and both directions round
        # UP (a half-micro fraction is never dropped).
        self.assertEqual(
            convert_basis(100_000_000, "ex_vat", "invoice_total", 1900),
            119_000_000)
        self.assertEqual(convert_basis(1, "ex_vat", "invoice_total", 1), 2)
        self.assertEqual(
            convert_basis(10002, "invoice_total", "ex_vat", 1), 10001)
        self.assertEqual(
            convert_basis(10001, "invoice_total", "ex_vat", 1), 10000)

        # An unknown basis defers (a BILLING_BASIS_UNKNOWN), it never
        # approves: no comparison is possible.
        for source, target in (("unknown", "ex_vat"), ("ex_vat", "unknown")):
            with self.assertRaises(QuotaError) as ctx:
                convert_basis(100, source, target, 1900)
            self.assertEqual(ctx.exception.code, CODE_BILLING_BASIS_UNKNOWN)

        # A required rate that is missing or unusable: no implicit VAT
        # default is permitted.
        for rate in (None, -1, 10_001, "1900", True):
            with self.assertRaises(QuotaError) as ctx:
                convert_basis(100, "ex_vat", "invoice_total", rate)
            self.assertEqual(ctx.exception.code, CODE_BILLING_BASIS_UNKNOWN)

        # Checked arithmetic: non-integer/negative/overflowing inputs and
        # results are INVALID_CONFIG.
        for amount in ("100", -1, 2 ** 63, True):
            with self.assertRaises(QuotaError) as ctx:
                convert_basis(amount, "ex_vat", "ex_vat", None)
            self.assertEqual(ctx.exception.code, CODE_INVALID_CONFIG)
        with self.assertRaises(QuotaError) as ctx:
            convert_basis(2 ** 63 - 1, "ex_vat", "invoice_total", 1900)
        self.assertEqual(ctx.exception.code, CODE_INVALID_CONFIG)
        with self.assertRaises(QuotaError) as ctx:
            convert_basis(100, "gross", "ex_vat", 1900)
        self.assertEqual(ctx.exception.code, CODE_INVALID_CONFIG)

        # Import level: the allocation check uses one tax basis. A second,
        # gateway-less metered account with a 100 EUR cap: an allowance of
        # 119 EUR invoice-total converts to exactly 100 EUR ex-VAT (the
        # cap) and passes; one micro-EUR more fails.
        doc = copy.deepcopy(VALID_DOC)
        doc["accounts"].append(
            {"alias": "big", "vendor": "ovh", "kind": "metered",
             "budget_micro_eur": 100_000_000})
        self.assertEqual(_config_import(self.db, self.tmp, doc).returncode, 0)
        ok = _policy_doc([
            _billing_policy(account="big", reporter="worker",
                            provider_basis="invoice_total", vat_bps=1900,
                            allowance=119_000_000),
        ])
        self.assertEqual(_billing_policy_import(
            self.db, self.tmp, ok).returncode, 0)
        over = _policy_doc([
            _billing_policy(account="big", reporter="worker",
                            provider_basis="invoice_total", vat_bps=1900,
                            allowance=119_000_001),
        ])
        result = _billing_policy_import(self.db, self.tmp, over)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)
        # A needed conversion without a rate is an invalid document.
        norate = _policy_doc([
            _billing_policy(account="big", reporter="worker",
                            provider_basis="invoice_total",
                            allowance=0),
        ])
        result = _billing_policy_import(self.db, self.tmp, norate)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)


class BillingOutputErrors(_Base):
    """billing_output_errors: every 2b-1 result/error branch serializes the
    closed {error:{code,message}} shape with the closed exit mapping, and a
    rejected import leaves the store untouched."""

    def test_billing_output_errors(self):
        import quota_balancer.errors as errors
        # The closed exit mapping for the card's error codes.
        self.assertEqual(errors.EXIT_CODES[errors.CODE_INVALID_CONFIG], 2)
        self.assertEqual(errors.EXIT_CODES[errors.CODE_SCHEMA_UNSUPPORTED], 2)
        self.assertEqual(errors.EXIT_CODES[errors.CODE_IDEMPOTENCY_CONFLICT],
                         2)
        self.assertEqual(errors.EXIT_CODES[errors.CODE_STORE_ERROR], 3)
        for code in (errors.CODE_BILLING_SCOPE_MISMATCH,
                     errors.CODE_BILLING_BASIS_UNKNOWN,
                     errors.CODE_BILLING_UNKNOWN,
                     errors.CODE_BILLING_STALE,
                     errors.CODE_BILLING_DIVERGENCE,
                     errors.CODE_OPENING_UNSAFE):
            self.assertIn(code, errors.EXIT_CODES)
            self.assertEqual(errors.EXIT_CODES[code], 2)

        now = _now_ms()
        start, end = _month_bounds_of(now - 1000)
        good = _billing(_eid("err-ok"), "desktop", "meter", "proj-ai", 1,
                        start, end, 100, False, now - 1000)
        before = _fingerprint(self.db)

        # A malformed billing document: exit 2, static INVALID_CONFIG.
        bad = copy.deepcopy(good)
        bad["payload"]["total_micro_eur"] = -5
        result = _readings_import(self.db, self.tmp, _doc_v3([bad]))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)

        # An unknown reading document version: exit 2, SCHEMA_UNSUPPORTED.
        result = _readings_import(
            self.db, self.tmp,
            {"version": 4, "readings": [good]})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_SCHEMA_UNSUPPORTED)

        # A malformed policy document: exit 2, static INVALID_CONFIG.
        result = _billing_policy_import(
            self.db, self.tmp,
            {"version": 1, "policies": [dict(_billing_policy(), mode="nope")]})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)

        # An unknown policy document version: exit 2, SCHEMA_UNSUPPORTED.
        result = _billing_policy_import(
            self.db, self.tmp,
            {"version": 2, "policies": [_billing_policy()]})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_SCHEMA_UNSUPPORTED)

        # Every rejected document above left the store untouched.
        self.assertEqual(_fingerprint(self.db), before)

        # An idempotency collision: exit 2, static IDEMPOTENCY_CONFLICT.
        self.assertEqual(self._import_policy().returncode, 0)
        self.assertEqual(_readings_import(
            self.db, self.tmp, _doc_v3([good])).returncode, 0)
        # The two accepted imports above are the only writes; the
        # collision and the missing-DB status below may add nothing.
        before = _fingerprint(self.db)
        collision = copy.deepcopy(good)
        collision["payload"]["total_micro_eur"] = 200
        result = _readings_import(
            self.db, self.tmp, _doc_v3([collision]))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_IDEMPOTENCY)

        # A missing database: exit 3, static STORE_ERROR (not found).
        result = run_cli("--db", os.path.join(self.tmp, "absent.db"),
                         "status", "--json")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stderr.strip(), ERROR_STORE_NOT_FOUND)

        # The error contract never carries raw input or OS text: every
        # stderr above is one of the five static strings, and nothing the
        # rejected documents contained was echoed (the fingerprints prove
        # the store, and the exact stderr matches prove the wire).
        self.assertEqual(_fingerprint(self.db), before)


class Privacy2b(_Base):
    """privacy_2b: provider IDs, secrets, hostnames and local paths never
    leave the reporter - a sentinel planted at every leak point is either
    rejected with a static message or absent from the status; the billing
    projection is exactly the closed key sets (aliases, numbers, enums);
    and a rejected import has zero financial side effects."""

    SENTINELS = (
        "PROVIDERID-9f8e7d6c5b4a",
        "sk-provider-0123456789",
        "billing.provider-host.example",
        "/home/operator/provider-key.pem",
    )

    def test_privacy_2b(self):
        now = _now_ms()
        start, end = _month_bounds_of(now - 1000)

        # A sentinel as the scope alias (not a local alias): rejected,
        # static message, no echo.
        bad = _billing(_eid("priv-alias"), "desktop", "meter",
                       self.SENTINELS[2], 1, start, end, 100, False,
                       now - 1000)
        result = _readings_import(self.db, self.tmp, _doc_v3([bad]))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)

        # A sentinel provider ID as an extra payload key: the closed shape
        # rejects it; it is never stored or echoed.
        bad = _billing(_eid("priv-pid"), "desktop", "meter", "proj-ai", 1,
                       start, end, 100, False, now - 1000)
        bad["payload"]["provider_id"] = self.SENTINELS[0]
        result = _readings_import(self.db, self.tmp, _doc_v3([bad]))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)
        for sentinel in self.SENTINELS:
            self.assertNotIn(sentinel, result.stderr)

        # A sentinel as the account: rejected, static message.
        bad = _billing(_eid("priv-acct"), "desktop", self.SENTINELS[1],
                       "proj-ai", 1, start, end, 100, False, now - 1000)
        result = _readings_import(self.db, self.tmp, _doc_v3([bad]))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)

        # A sentinel as a file path: a missing file is INVALID_CONFIG with
        # the static message - the path is never in the output.
        result = run_cli("--db", self.db, "reading-import", "--file",
                         self.SENTINELS[3])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)

        # The good import: the status carries only aliases, numbers and
        # enums - no sentinel anywhere, and the billing projection is
        # exactly the closed key sets.
        self.assertEqual(self._import_policy().returncode, 0)
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc_v3([_billing(_eid("priv-ok"), "desktop", "meter", "proj-ai",
                              1, start, end, 100, False, now - 1000)]
        )).returncode, 0)
        out = run_cli("--db", self.db, "status", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        for sentinel in self.SENTINELS:
            self.assertNotIn(sentinel, out.stdout)
            self.assertNotIn(sentinel, out.stderr)
        entries = json.loads(out.stdout)["billing"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(set(entry), {"account", "policy", "totals"})
        self.assertEqual(set(entry["policy"]), {
            "reporter_machine", "scope_alias", "scope_revision",
            "dedicated_ai", "non_ai_allowance_micro_eur", "cap_basis",
            "provider_basis", "ledger_basis", "vat_bps", "poll_seconds",
            "max_age_seconds", "lag_seconds", "abs_tolerance_micro_eur",
            "relative_tolerance_bps", "mode", "stale_action",
            "divergence_action"})
        for total in entry["totals"]:
            self.assertEqual(set(total), {
                "scope_revision", "period_start", "period_end",
                "latest_micro_eur", "observed_at", "high_water_micro_eur",
                "conflict", "age_ms", "fresh", "freshness_reason"})

        # Zero financial side effects: a rejected document leaves the
        # before/after snapshots identical.
        before = _fingerprint(self.db)
        rejected = _billing(_eid("priv-side"), "worker", "meter", "proj-ai",
                            1, start, end, 999_999, False, now - 1000)
        result = _readings_import(self.db, self.tmp, _doc_v3([rejected]))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(_fingerprint(self.db), before)


if __name__ == "__main__":
    unittest.main()
