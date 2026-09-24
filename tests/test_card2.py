"""Card 2 acceptance tests: the reading reducer, its fixture CLI, and the
named checks.

Run from the repository root:
    python -m unittest discover -s tests -v

Method names carry the card's check names with the unittest "test_" prefix:
reading_replay, stale_mapping, coverage_fallback, account_isolation,
privacy_2, plus the reducer rules the section-8 row calls out (same-epoch
high-water never decreases, a new epoch needs a post-reset observation,
reordered samples, and a stale gateway report resolves to METER_STALE).
"""

import copy
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

# The standard card-1 inventory: one metered account, two machines, two
# gateways (reused so the reducer tests start from a populated store).
VALID_DOC = {
    "version": 1,
    "accounts": [
        {"alias": "meter", "vendor": "ovh", "kind": "metered", "budget_micro_eur": 100000000}
    ],
    "machines": [{"alias": "desktop"}, {"alias": "worker"}],
    "gateways": [
        {"alias": "g1", "account": "meter", "machine": "desktop",
         "share_micro_eur": 60000000, "period": "utc_month"},
        {"alias": "g2", "account": "meter", "machine": "worker",
         "share_micro_eur": 40000000, "period": "utc_month"},
    ],
}

ERROR_IDEMPOTENCY = (
    '{"error":{"code":"IDEMPOTENCY_CONFLICT",'
    '"message":"a reading with the same id has different content"}}'
)
ERROR_INVALID_CONFIG = '{"error":{"code":"INVALID_CONFIG","message":"config document is invalid"}}'
ERROR_STORE_NOT_FOUND = '{"error":{"code":"STORE_ERROR","message":"database not found"}}'
ERROR_STORE_FAILED = '{"error":{"code":"STORE_ERROR","message":"store operation failed"}}'

# A 64-char (SHA-256-style) policy hash: the fixed-length hex the reducer
# requires. Free text and paths cannot masquerade as one.
PRICE_HASH = "0123456789abcdef" * 4
CAP_HASH = "fedcba9876543210" * 4


def _now_ms():
    return int(time.time() * 1000)


# Section 7: reading IDs are local UUIDs (operator aliases are the
# exception). A deterministic UUID per label: a valid local UUID, stable
# across runs so the replay/collision tests read naturally.
_EID_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # NAMESPACE_URL


def _eid(label):
    return str(uuid.uuid5(_EID_NS, "quota-card2/" + label))


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


def _status(db):
    return run_cli("--db", db, "status", "--json")


def _set_coverage(db, account, complete):
    return run_cli("--db", db, "set-coverage", "--account", account,
                   "--complete", "true" if complete else "false")


def _reconcile(db, gateway):
    return run_cli("--db", db, "reconcile", "--gateway", gateway)


def _cli_window(event_id, machine, account, key, used, resets_at, observed_at,
                confidence="observed", stale=False, covered_seq=None):
    return {
        "event_id": event_id,
        "machine": machine,
        "account": account,
        "kind": "cli_window",
        "observed_at": observed_at,
        "payload": {
            "windows": [{"key": key, "used_bps": used, "resets_at": resets_at}],
            "confidence": confidence,
            "source_stale": stale,
            "covered_seq": covered_seq,
        },
    }


def _gateway_snapshot(event_id, machine, account, gateway, ledger_epoch, seq,
                      observed_at, period_start=None, period_end=None,
                      policy_hashes=None, sized_ready=False,
                      reconciliation_required=False):
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


def _doc(readings):
    return {"version": 2, "readings": readings}


class _Base(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-card2-")
        self.tmp = self._tmp.name
        self.db = os.path.join(self.tmp, "inventory.db")
        run_cli("--db", self.db, "init")
        self.assertEqual(_config_import(self.db, self.tmp, copy.deepcopy(VALID_DOC)).returncode, 0)

    def tearDown(self):
        self._tmp.cleanup()

    def _status_json(self):
        result = _status(self.db)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def _window(self, key):
        for account in self._status_json()["accounts"]:
            for window in account["windows"]:
                if window["window_key"] == key:
                    return window
        return None


class NamedChecks(_Base):
    def test_reading_replay(self):
        # Given the same ID: a replay (same content) is a no-op, a collision
        # (same id, different content) is IDEMPOTENCY_CONFLICT.
        now = _now_ms()
        base = _doc([_cli_window(_eid("e1"), "desktop", "meter", "wk1", 4000,
                                 now - 50000, now - 10000)])
        self.assertEqual(_readings_import(self.db, self.tmp, base).returncode, 0)
        self.assertEqual(self._window("wk1")["used_bps"], 4000)

        # Replay the identical document: a no-op (exit 0), store unchanged.
        replay = _readings_import(self.db, self.tmp, copy.deepcopy(base))
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(self._window("wk1")["used_bps"], 4000)

        # Collision: same event id, different content -> exit 2, store unchanged.
        collision = copy.deepcopy(base)
        collision["readings"][0]["payload"]["windows"][0]["used_bps"] = 9000
        bad = _readings_import(self.db, self.tmp, collision)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_IDEMPOTENCY)
        self.assertEqual(bad.stdout, "")
        self.assertEqual(self._window("wk1")["used_bps"], 4000)

    def test_stale_mapping(self):
        # Given a legacy "stale" reading: the value, the SOURCE time, and the
        # source_stale flag are all preserved (the ingest time is recorded
        # separately and never refreshes the observation).
        source_time = _now_ms() - 120000  # the sample was taken 2 minutes ago
        doc = _doc([_cli_window(_eid("s1"), "desktop", "meter", "wk-stale", 7500,
                                source_time, source_time, stale=True)])
        self.assertEqual(_readings_import(self.db, self.tmp, doc).returncode, 0)

        window = self._window("wk-stale")
        self.assertEqual(window["used_bps"], 7500)      # value preserved
        self.assertEqual(window["source_stale"], True)  # flag preserved
        self.assertEqual(window["observed_at"], source_time)  # source time preserved

        # The store keeps the source time and a distinct, later ingest time.
        conn = sqlite3.connect(self.db)
        try:
            observed_at, received_at = conn.execute(
                "SELECT observed_at, received_at FROM readings WHERE event_id=?",
                (_eid("s1"),),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(observed_at, source_time)
        self.assertGreater(received_at, source_time)  # ingest is after the sample

    def test_coverage_fallback(self):
        # Given incomplete coverage and a sample older than the 600-second
        # wall cap: the window is stale. Telemetry cannot set coverage_complete.
        old_time = _now_ms() - 700000  # 700 seconds ago, past the 600s cap
        doc = _doc([_cli_window(_eid("c1"), "desktop", "meter", "wk-old", 500,
                                old_time, old_time)])
        self.assertEqual(_readings_import(self.db, self.tmp, doc).returncode, 0)

        stale_window = self._window("wk-old")
        self.assertFalse(stale_window["current"])
        self.assertEqual(stale_window["freshness_reason"], "wall_cap")

        # Incomplete coverage (never set by the operator) and a fresh sample
        # within the cap: the window is current.
        fresh_time = _now_ms() - 5000
        doc2 = _doc([_cli_window(_eid("c2"), "desktop", "meter", "wk-fresh", 500,
                                 fresh_time, fresh_time)])
        self.assertEqual(_readings_import(self.db, self.tmp, doc2).returncode, 0)
        fresh_window = self._window("wk-fresh")
        self.assertTrue(fresh_window["current"])
        self.assertEqual(fresh_window["freshness_reason"], "fresh")

        # Telemetry cannot set coverage_complete: it stays false, and stays
        # whatever the operator last set it to.
        self.assertFalse(self._status_json()["accounts"][0]["coverage_complete"])
        self.assertEqual(_set_coverage(self.db, "meter", True).returncode, 0)
        self.assertTrue(self._status_json()["accounts"][0]["coverage_complete"])
        more = _doc([_cli_window(_eid("c3"), "desktop", "meter", "wk-more", 600,
                                 _now_ms() - 1000, _now_ms() - 1000)])
        self.assertEqual(_readings_import(self.db, self.tmp, more).returncode, 0)
        self.assertTrue(self._status_json()["accounts"][0]["coverage_complete"])

    def test_account_isolation(self):
        # Given an ambiguous source (no account attribution): neither account
        # is updated. Two accounts share the machine; the ambiguous reading is
        # recorded but reduced into no account.
        two_accounts = {
            "version": 1,
            "accounts": [
                {"alias": "acct-a", "vendor": "ovh", "kind": "metered",
                 "budget_micro_eur": 100000000},
                {"alias": "acct-b", "vendor": "ovh", "kind": "metered",
                 "budget_micro_eur": 100000000},
            ],
            "machines": [{"alias": "shared"}],
            "gateways": [],
        }
        self.assertEqual(_config_import(self.db, self.tmp, two_accounts).returncode, 0)

        ambiguous = _doc([_cli_window(_eid("a1"), "shared", None, "wk-shared", 5000,
                                      _now_ms() - 10000, _now_ms() - 10000)])
        self.assertEqual(_readings_import(self.db, self.tmp, ambiguous).returncode, 0)

        # No window was created for either account.
        self.assertIsNone(self._window("wk-shared"))
        for account in self._status_json()["accounts"]:
            self.assertEqual(account["windows"], [])
        # The ambiguous reading is still recorded for dedup/audit.
        conn = sqlite3.connect(self.db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM readings WHERE event_id=?",
                (_eid("a1"),),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_privacy_2(self):
        # Given injected sentinels (secret / provider id / path), when status
        # or error output is rendered, none appear; only local aliases survive.
        status_result = _status(self.db)
        self.assertEqual(status_result.returncode, 0)
        # Local aliases survive in the status.
        for alias in ("meter", "desktop", "g1"):
            self.assertIn('"' + alias + '"', status_result.stdout)

        secret = "secret-provider-id-zz9"
        provider = "prov-id-abc123"
        path_sentinel = "db-path-sentinel-5f2e"

        # A secret used as an account reference is a dangling reference:
        # rejected with a static message, not echoed.
        bad = _readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("p1"), "desktop", secret, "wk-p", 1000,
                              _now_ms() - 10000, _now_ms() - 10000)]))
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG)
        self.assertNotIn(secret, bad.stderr + bad.stdout)

        # A provider id used as a machine reference is likewise not echoed.
        bad2 = _readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("p2"), provider, "meter", "wk-p", 1000,
                              _now_ms() - 10000, _now_ms() - 10000)]))
        self.assertEqual(bad2.returncode, 2)
        self.assertNotIn(provider, bad2.stderr + bad2.stdout)

        # A sentinel in the DB path must not be echoed on the store-IO path.
        bad_db = os.path.join(self.tmp, path_sentinel, "inventory.db")
        bad3 = _status(bad_db)
        self.assertEqual(bad3.returncode, 3)
        self.assertEqual(bad3.stderr.strip(), ERROR_STORE_NOT_FOUND)
        self.assertNotIn(path_sentinel, bad3.stderr + bad3.stdout)

        # None of the sentinels appear in a successful status output.
        for sentinel in (secret, provider, path_sentinel):
            self.assertNotIn(sentinel, status_result.stdout)


class ReducerRules(_Base):
    def test_high_water_same_epoch_never_decreases(self):
        # Same epoch: the high-water utilization never decreases.
        now = _now_ms()
        epoch = now - 50000
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("h1"), "desktop", "meter", "wk-h", 4000, epoch,
                              now - 30000)])).returncode, 0)
        # A later observation, higher usage: the mark rises.
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("h2"), "desktop", "meter", "wk-h", 6000, epoch,
                              now - 20000)])).returncode, 0)
        self.assertEqual(self._window("wk-h")["used_bps"], 6000)
        # A later observation, lower usage: the mark does NOT fall.
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("h3"), "desktop", "meter", "wk-h", 2000, epoch,
                              now - 10000)])).returncode, 0)
        self.assertEqual(self._window("wk-h")["used_bps"], 6000)

    def test_new_epoch_requires_post_reset_observation(self):
        # A new, later epoch is restarted by its first post-reset observation;
        # an older epoch is out of order and a no-op.
        now = _now_ms()
        current_epoch = now - 50000
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("n1"), "desktop", "meter", "wk-n", 6000, current_epoch,
                              now - 10000)])).returncode, 0)
        self.assertEqual(self._window("wk-n")["used_bps"], 6000)

        # A later reset epoch with a fresh observation: the mark restarts.
        new_epoch = now + 7200000
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("n2"), "desktop", "meter", "wk-n", 1000, new_epoch,
                              now)])).returncode, 0)
        window = self._window("wk-n")
        self.assertEqual(window["used_bps"], 1000)
        self.assertEqual(window["resets_at"], new_epoch)

        # An older (out-of-order) epoch must not regress the window.
        old_epoch = now - 300000
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("n3"), "desktop", "meter", "wk-n", 9999, old_epoch,
                              now - 200000)])).returncode, 0)
        self.assertEqual(self._window("wk-n")["used_bps"], 1000)

    def test_new_epoch_rejected_before_reset(self):
        # M1: a reset time that has merely jittered a little later
        # (resets_at > stored) is not a genuine new epoch unless the sample
        # was observed at or after the stored reset time. An older sample must
        # not lower the high-water just because its reset timestamp drifted.
        now = _now_ms()
        reset = now + 3_600_000  # the current window resets an hour out
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("e1"), "desktop", "meter", "wk-e", 9500, reset,
                              now - 10000)])).returncode, 0)
        self.assertEqual(self._window("wk-e")["used_bps"], 9500)

        # An older sample (70s ago) whose reset drifted 1ms later: the same
        # logical window, so the high-water is kept, not lowered to 1000.
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("e2"), "desktop", "meter", "wk-e", 1000, reset + 1,
                              now - 70000)])).returncode, 0)
        window = self._window("wk-e")
        self.assertEqual(window["used_bps"], 9500)
        self.assertEqual(window["resets_at"], reset)

    def test_unknown_reading_does_not_refresh_known_window(self):
        # M4: an unknown reading carries no value of its own; it must not
        # move observed_at or relabel the confidence of a window that
        # already has a known value. A stale known value must stay stale and
        # stay "observed" evidence, not look fresh and unknown.
        now = _now_ms()
        reset = now + 3_600_000
        old = now - 700_000  # 700 seconds ago: past the 600s wall cap
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("u1"), "desktop", "meter", "wk-un", 9500,
                              reset, old)])).returncode, 0)
        window = self._window("wk-un")
        self.assertEqual(window["used_bps"], 9500)
        self.assertFalse(window["current"])  # stale under the wall cap

        # An unknown reading observed "now": no value, no reset.
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("u2"), "desktop", "meter", "wk-un", None,
                              None, now, confidence="unknown")])).returncode, 0)
        window = self._window("wk-un")
        self.assertEqual(window["used_bps"], 9500)         # value kept
        self.assertEqual(window["confidence"], "observed")  # not relabelled
        self.assertEqual(window["observed_at"], old)        # not refreshed
        self.assertFalse(window["current"])                 # still stale

    def test_unknown_then_known_upgrades(self):
        # The inverse of M4: an unknown reading alone leaves an unknown
        # window; a later observed reading upgrades it with its value and
        # source time.
        now = _now_ms()
        reset = now + 3_600_000
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("u3"), "desktop", "meter", "wk-up", None,
                              None, now - 20_000, confidence="unknown")])).returncode, 0)
        window = self._window("wk-up")
        self.assertIsNone(window["used_bps"])
        self.assertEqual(window["confidence"], "unknown")

        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("u4"), "desktop", "meter", "wk-up", 3000,
                              reset, now - 10_000)])).returncode, 0)
        window = self._window("wk-up")
        self.assertEqual(window["used_bps"], 3000)
        self.assertEqual(window["confidence"], "observed")
        self.assertEqual(window["observed_at"], now - 10_000)
        self.assertEqual(window["resets_at"], reset)

    def test_reordered_samples(self):
        # A "later" observation arriving first, then an "earlier" one out of
        # order: the window tracks the latest observed_at and the high-water
        # usage, regardless of arrival order.
        now = _now_ms()
        epoch = now - 50000
        # Arrives first but is the LATER observation (higher usage).
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("r2"), "desktop", "meter", "wk-r", 6000, epoch,
                              now - 10000)])).returncode, 0)
        # Arrives second but is the EARLIER observation (lower usage).
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("r1"), "desktop", "meter", "wk-r", 4000, epoch,
                              now - 40000)])).returncode, 0)
        window = self._window("wk-r")
        # The latest observation's time wins; the high-water usage is kept.
        self.assertEqual(window["observed_at"], now - 10000)
        self.assertEqual(window["used_bps"], 6000)

    def test_gateway_report_meter_stale(self):
        # A fresh gateway report resolves to ok; one older than the fail-closed
        # age (120s) resolves to stale (METER_STALE).
        now = _now_ms()
        # Fresh report for g1.
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_gateway_snapshot(_eid("f1"), "desktop", "meter", "g1", 7,
                                    100, now - 5000, policy_hashes={
                                        "price": PRICE_HASH,
                                        "child_cap": CAP_HASH},
                                    sized_ready=True)])).returncode, 0)
        reports = {g["alias"]: g["report"] for g in self._status_json()["gateways"]}
        self.assertEqual(reports["g1"]["state"], "ok")

        # A report for g2 observed 130 seconds ago: stale.
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_gateway_snapshot(_eid("s2"), "worker", "meter", "g2", 3, 10,
                                    now - 130000)])).returncode, 0)
        reports = {g["alias"]: g["report"] for g in self._status_json()["gateways"]}
        self.assertEqual(reports["g2"]["state"], "stale")


class GatewayReportReducer(_Base):
    """The gateway-report reducer: sequence/epoch ordering and the sticky
    reconciliation state."""

    def test_new_epoch_lower_seq_accepted(self):
        # m1: a changed ledger epoch is a new ledger with its own sequence
        # counter, so it is accepted even when its first sequence is lower
        # than the old ledger's; only a same-epoch non-increasing sequence is
        # an out-of-order no-op.
        now = _now_ms()
        hashes = {"price": PRICE_HASH, "child_cap": CAP_HASH}
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_gateway_snapshot(_eid("g1a"), "desktop", "meter", "g1", 1, 100,
                                    now - 2000, policy_hashes=hashes)])
            ).returncode, 0)
        # A re-initialized ledger (new epoch) whose first sequence is lower:
        # accepted, not discarded; the epoch change needs reconciliation.
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_gateway_snapshot(_eid("g1b"), "desktop", "meter", "g1", 2, 5,
                                    now - 1000, policy_hashes=hashes)])
            ).returncode, 0)
        reports = {g["alias"]: g["report"] for g in self._status_json()["gateways"]}
        self.assertEqual(reports["g1"]["last_seq"], 5)
        self.assertEqual(reports["g1"]["ledger_epoch"], 2)
        self.assertEqual(reports["g1"]["state"], "reconciliation_required")

    def test_reconciliation_sticky_epoch(self):
        # M2: an epoch change sets reconciliation_required, and it stays set
        # until the operator's explicit reconcile verb clears it; a matching
        # later report does NOT clear it.
        now = _now_ms()
        hashes = {"price": PRICE_HASH, "child_cap": CAP_HASH}
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_gateway_snapshot(_eid("s1"), "desktop", "meter", "g1", 1, 1,
                                    now - 5000, policy_hashes=hashes)])).returncode, 0)
        # Epoch change: reconciliation required.
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_gateway_snapshot(_eid("s2"), "desktop", "meter", "g1", 2, 2,
                                    now - 4000, policy_hashes=hashes)])).returncode, 0)
        # A further report on the new epoch: still required (no self-clear).
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_gateway_snapshot(_eid("s3"), "desktop", "meter", "g1", 2, 3,
                                    now - 3000, policy_hashes=hashes)])).returncode, 0)
        reports = {g["alias"]: g["report"] for g in self._status_json()["gateways"]}
        self.assertEqual(reports["g1"]["state"], "reconciliation_required")

        # Only the explicit operator verb clears it.
        self.assertEqual(_reconcile(self.db, "g1").returncode, 0)
        reports = {g["alias"]: g["report"] for g in self._status_json()["gateways"]}
        self.assertEqual(reports["g1"]["state"], "ok")

    def test_reconciliation_sticky_hash(self):
        # M2: a policy-hash change (same epoch) sets reconciliation_required,
        # and a matching later report does not clear it.
        now = _now_ms()
        h1 = {"price": PRICE_HASH, "child_cap": CAP_HASH}
        h2 = {"price": CAP_HASH, "child_cap": PRICE_HASH}
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_gateway_snapshot(_eid("h1"), "desktop", "meter", "g1", 1, 1,
                                    now - 5000, policy_hashes=h1)])).returncode, 0)
        # Hash change (same epoch): reconciliation required.
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_gateway_snapshot(_eid("h2"), "desktop", "meter", "g1", 1, 2,
                                    now - 4000, policy_hashes=h2)])).returncode, 0)
        # A matching report (same hash): still required (no self-clear).
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_gateway_snapshot(_eid("h3"), "desktop", "meter", "g1", 1, 3,
                                    now - 3000, policy_hashes=h2)])).returncode, 0)
        reports = {g["alias"]: g["report"] for g in self._status_json()["gateways"]}
        self.assertEqual(reports["g1"]["state"], "reconciliation_required")
        self.assertEqual(_reconcile(self.db, "g1").returncode, 0)
        reports = {g["alias"]: g["report"] for g in self._status_json()["gateways"]}
        self.assertEqual(reports["g1"]["state"], "ok")

    def test_gateway_report_bound_to_owner(self):
        # m2: a gateway report binds to its owner. Section 5: both sources
        # bind seat/machine/account explicitly. A snapshot for g1 tagged
        # with a different machine is a mismatch, not a different gateway:
        # INVALID_CONFIG, store untouched. An ambiguous source (account
        # null) cannot bind to a specific gateway either.
        now = _now_ms()
        hashes = {"price": PRICE_HASH, "child_cap": CAP_HASH}
        # A correctly-bound report is accepted first.
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_gateway_snapshot(_eid("own1"), "desktop", "meter", "g1", 1,
                                    1, now - 5000, policy_hashes=hashes)]))
            .returncode, 0)
        # Mismatched machine (g1's own machine is "desktop"): rejected.
        bad = _readings_import(
            self.db, self.tmp,
            _doc([_gateway_snapshot(_eid("own2"), "worker", "meter", "g1", 1,
                                    2, now - 4000, policy_hashes=hashes)]))
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG)
        reports = {g["alias"]: g["report"] for g in self._status_json()["gateways"]}
        self.assertEqual(reports["g1"]["last_seq"], 1)  # unchanged
        # Ambiguous source (account null): cannot bind, rejected.
        bad2 = _readings_import(
            self.db, self.tmp,
            _doc([_gateway_snapshot(_eid("own3"), "desktop", None, "g1", 1,
                                    3, now - 3000, policy_hashes=hashes)]))
        self.assertEqual(bad2.returncode, 2)
        self.assertEqual(bad2.stderr.strip(), ERROR_INVALID_CONFIG)


class ReducerValidation(_Base):
    def test_rejects_future_observation(self):
        # A reading more than 30 seconds in the future is rejected.
        future = _now_ms() + 120000
        bad = _readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("fv"), "desktop", "meter", "wk-f", 1000,
                              future, future)]))
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG)

    def test_rejects_unknown_window_field(self):
        # The payload shape is closed: an unknown window field is rejected.
        doc = _doc([_cli_window(_eid("u1"), "desktop", "meter", "wk-u", 1000,
                                _now_ms() - 10000, _now_ms() - 10000)])
        doc["readings"][0]["payload"]["windows"][0]["extra"] = 1
        bad = _readings_import(self.db, self.tmp, doc)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG)

    def test_integers_bounded_to_signed_64(self):
        # M3: every integer field is checked against the signed-64 range
        # (section 7: checked signed-64-bit arithmetic, reject overflow)
        # before it can reach SQLite binding. Out-of-range input is
        # INVALID_CONFIG (exit 2) - never a driver overflow traceback.
        now = _now_ms()
        cases = [
            _cli_window(_eid("b1"), "desktop", "meter", "wk-b", 1000,
                        now - 10_000, -(2 ** 70)),         # observed_at
            _cli_window(_eid("b2"), "desktop", "meter", "wk-b", 1000,
                        2 ** 64, now - 10_000),            # resets_at
            _cli_window(_eid("b3"), "desktop", "meter", "wk-b", 1000,
                        now - 10_000, now - 10_000,
                        covered_seq=2 ** 64),               # covered_seq
            _gateway_snapshot(_eid("b4"), "desktop", "meter", "g1", 2 ** 64, 1,
                              now - 10_000),                # ledger_epoch
            _gateway_snapshot(_eid("b5"), "desktop", "meter", "g1", 1, 2 ** 64,
                              now - 10_000),                # seq
            _gateway_snapshot(_eid("b6"), "desktop", "meter", "g1", 1, 1,
                              now - 10_000, period_start=2 ** 64),  # period_start
            _gateway_snapshot(_eid("b7"), "desktop", "meter", "g1", 1, 1,
                              now - 10_000, period_end=2 ** 64),    # period_end
        ]
        for case in cases:
            bad = _readings_import(self.db, self.tmp, _doc([case]))
            self.assertEqual(bad.returncode, 2, case)
            self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG)
            self.assertNotIn("Traceback", bad.stderr + bad.stdout)

    def test_rejects_negative_timestamps_and_sequences(self):
        # m3: section 7 rejects negative values; a negative timestamp or
        # sequence is INVALID_CONFIG, not stored as-is.
        now = _now_ms()
        cases = [
            _cli_window(_eid("neg1"), "desktop", "meter", "wk-neg", 1000,
                        now - 10_000, -1),                 # observed_at
            _cli_window(_eid("neg2"), "desktop", "meter", "wk-neg", 1000,
                        -5, now - 10_000),                 # resets_at
            _cli_window(_eid("neg3"), "desktop", "meter", "wk-neg", 1000,
                        now - 10_000, now - 10_000,
                        covered_seq=-1),                    # covered_seq
            _gateway_snapshot(_eid("neg4"), "desktop", "meter", "g1", -1, 1,
                              now - 10_000),                # ledger_epoch
            _gateway_snapshot(_eid("neg5"), "desktop", "meter", "g1", 1, -1,
                              now - 10_000),                # seq
            _gateway_snapshot(_eid("neg6"), "desktop", "meter", "g1", 1, 1,
                              now - 10_000,
                              period_start=-1, period_end=-1),  # period_*
        ]
        for case in cases:
            bad = _readings_import(self.db, self.tmp, _doc([case]))
            self.assertEqual(bad.returncode, 2, case)
            self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG)

    def test_observed_confidence_requires_values(self):
        # m3: section 7 - "null percentage/reset only when unknown". An
        # observed reading must carry both a value and a reset; an unknown
        # reading may carry neither.
        now = _now_ms()
        reset = now + 3_600_000
        bad1 = _readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("val1"), "desktop", "meter", "wk-val", None,
                              reset, now - 10_000)]))
        self.assertEqual(bad1.returncode, 2)
        self.assertEqual(bad1.stderr.strip(), ERROR_INVALID_CONFIG)
        bad2 = _readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("val2"), "desktop", "meter", "wk-val", 1000,
                              None, now - 10_000)]))
        self.assertEqual(bad2.returncode, 2)
        self.assertEqual(bad2.stderr.strip(), ERROR_INVALID_CONFIG)
        # unknown with both null: accepted, stored as unknown.
        ok = _readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("val3"), "desktop", "meter", "wk-val", None,
                              None, now - 10_000, confidence="unknown")]))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        window = self._window("wk-val")
        self.assertEqual(window["confidence"], "unknown")
        self.assertIsNone(window["used_bps"])

    def test_window_key_and_policy_hash_are_bounded(self):
        # m4 (privacy_2): a window key is a local alias and a policy hash is
        # a fixed-length hex digest. Reporter free text (a path with a user
        # directory, a short token) cannot reach the status output.
        now = _now_ms()
        path_key = "/home/user/.secret/wk"
        bad = _readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("pk1"), "desktop", "meter", path_key,
                              1000, now - 10_000, now - 10_000)]))
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG)
        self.assertNotIn(path_key, bad.stderr + bad.stdout)

        # A free-text (non-hex, wrong-length) policy hash is rejected too,
        # and nothing of it reaches the store or the error output.
        bad2 = _readings_import(
            self.db, self.tmp,
            _doc([_gateway_snapshot(_eid("pk2"), "desktop", "meter", "g1", 1,
                                    1, now - 10_000, policy_hashes={
                                        "price": "p1", "child_cap": CAP_HASH})]))
        self.assertEqual(bad2.returncode, 2)
        self.assertEqual(bad2.stderr.strip(), ERROR_INVALID_CONFIG)
        self.assertNotIn("p1", bad2.stderr + bad2.stdout)
        reports = {g["alias"]: g["report"] for g in self._status_json()["gateways"]}
        self.assertIsNone(reports["g1"]["policy_hashes"])  # untouched

    def test_rejects_non_uuid_event_id(self):
        # n3: section 7 - IDs are local UUIDs (operator aliases are the
        # exception). An event_id in a non-UUID form is INVALID_CONFIG.
        now = _now_ms()
        bad = _readings_import(
            self.db, self.tmp,
            _doc([_cli_window("not-a-uuid", "desktop", "meter", "wk-uuid",
                              1000, now - 10_000, now - 10_000)]))
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG)
        # A canonical lowercase local UUID is accepted.
        ok = _readings_import(
            self.db, self.tmp,
            _doc([_cli_window(_eid("uuid1"), "desktop", "meter", "wk-uuid",
                              1000, now - 10_000, now - 10_000)]))
        self.assertEqual(ok.returncode, 0, ok.stderr)


class ConfigImportUpsert(_Base):
    """BLOCKER: config-import must be an upsert, not a delete-and-reinsert.

    A delete-and-reinsert fails once any reading exists (a foreign key to the
    re-inserted account/machine) and silently resets the operator-owned
    coverage flag and the gateway report columns. The upsert updates retained
    aliases, inserts new ones, and deletes a removed alias only when nothing
    references it.
    """

    def test_reimport_after_reading(self):
        # After a reading references an account, a re-import must not fail on
        # a foreign key: it keeps the inventory and the reading's window.
        now = _now_ms()
        doc = _doc([_cli_window(_eid("ci1"), "desktop", "meter", "wk-ci", 4000,
                                now - 10000, now - 10000)])
        self.assertEqual(_readings_import(self.db, self.tmp, doc).returncode, 0)
        self.assertEqual(self._window("wk-ci")["used_bps"], 4000)

        # Re-import the same inventory: exit 0 (no FK violation), intact.
        again = _config_import(self.db, self.tmp, copy.deepcopy(VALID_DOC))
        self.assertEqual(again.returncode, 0, again.stderr)
        status = self._status_json()
        self.assertEqual([a["alias"] for a in status["accounts"]], ["meter"])
        self.assertEqual(status["machines"], ["desktop", "worker"])
        self.assertEqual([g["alias"] for g in status["gateways"]], ["g1", "g2"])
        self.assertEqual(self._window("wk-ci")["used_bps"], 4000)

    def test_reimport_preserves_coverage_and_report(self):
        # The operator's coverage_complete flag and the gateway report survive
        # a re-import: the upsert touches only config-owned columns.
        now = _now_ms()
        self.assertEqual(_set_coverage(self.db, "meter", True).returncode, 0)
        snap = _doc([_gateway_snapshot(_eid("ci2"), "desktop", "meter", "g1", 1, 10,
                                       now - 1000,
                                       policy_hashes={"price": PRICE_HASH,
                                                      "child_cap": CAP_HASH},
                                       sized_ready=True)])
        self.assertEqual(_readings_import(self.db, self.tmp, snap).returncode, 0)
        reports = {g["alias"]: g["report"] for g in self._status_json()["gateways"]}
        self.assertEqual(reports["g1"]["last_seq"], 10)

        again = _config_import(self.db, self.tmp, copy.deepcopy(VALID_DOC))
        self.assertEqual(again.returncode, 0, again.stderr)
        status = self._status_json()
        self.assertTrue(status["accounts"][0]["coverage_complete"])
        reports = {g["alias"]: g["report"] for g in status["gateways"]}
        self.assertEqual(reports["g1"]["last_seq"], 10)
        self.assertEqual(reports["g1"]["state"], "ok")

    def test_reimport_removed_alias_referenced(self):
        # A machine that a reading references cannot be removed from the
        # inventory: the re-import is INVALID_CONFIG, store untouched.
        doc = _doc([_cli_window(_eid("ci3"), "worker", "meter", "wk-ref", 500,
                                _now_ms() - 10000, _now_ms() - 10000)])
        self.assertEqual(_readings_import(self.db, self.tmp, doc).returncode, 0)

        # Remove "worker" (and its gateway g2) from the inventory.
        without_worker = {
            "version": 1,
            "accounts": [
                {"alias": "meter", "vendor": "ovh", "kind": "metered",
                 "budget_micro_eur": 100000000},
            ],
            "machines": [{"alias": "desktop"}],
            "gateways": [
                {"alias": "g1", "account": "meter", "machine": "desktop",
                 "share_micro_eur": 60000000, "period": "utc_month"},
            ],
        }
        bad = _config_import(self.db, self.tmp, without_worker)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG)
        # The store is untouched: worker and g2 are still present.
        status = self._status_json()
        self.assertEqual(status["machines"], ["desktop", "worker"])
        self.assertEqual([g["alias"] for g in status["gateways"]], ["g1", "g2"])

    def test_reimport_removed_alias_unreferenced(self):
        # A removed alias that nothing references IS deleted; the rest of the
        # inventory is retained (with its UUIDs).
        with_spare = {
            "version": 1,
            "accounts": [
                {"alias": "meter", "vendor": "ovh", "kind": "metered",
                 "budget_micro_eur": 100000000},
            ],
            "machines": [{"alias": "desktop"}, {"alias": "spare"}],
            "gateways": [
                {"alias": "g1", "account": "meter", "machine": "desktop",
                 "share_micro_eur": 60000000, "period": "utc_month"},
            ],
        }
        self.assertEqual(_config_import(self.db, self.tmp, with_spare).returncode, 0)
        conn = sqlite3.connect(self.db)
        try:
            ids = dict(conn.execute("SELECT alias, id FROM machines"))
        finally:
            conn.close()
        self.assertIn("spare", ids)

        without_spare = {
            "version": 1,
            "accounts": with_spare["accounts"],
            "machines": [{"alias": "desktop"}],
            "gateways": with_spare["gateways"],
        }
        self.assertEqual(_config_import(self.db, self.tmp, without_spare).returncode, 0)
        status = self._status_json()
        self.assertEqual(status["machines"], ["desktop"])
        # The retained machine keeps its UUID.
        conn = sqlite3.connect(self.db)
        try:
            retained = dict(conn.execute("SELECT alias, id FROM machines"))
        finally:
            conn.close()
        self.assertNotIn("spare", retained)
        self.assertEqual(retained["desktop"], ids["desktop"])


class ConfigImportRepoint(_Base):
    """Gate fix: re-pointing a retained gateway to another machine or
    account must reset its report state. Carrying the old owner's
    ledger_epoch/last_seq/policy_hashes/state over would let the new
    owner's first report (a new ledger, typically seq 1) be dropped as a
    smaller seq, then accepted as a continuation without reconciliation.
    The new owner's seq 1 must be accepted and the gateway must stay
    reconciliation_required until an operator runs reconcile."""

    def _reports(self):
        return {g["alias"]: g["report"] for g in self._status_json()["gateways"]}

    def test_repoint_machine_resets_report_state(self):
        # g1 (desktop/meter) has reported seq 500, state ok.
        now = _now_ms()
        self.assertEqual(_readings_import(self.db, self.tmp,
            _doc([_gateway_snapshot(_eid("rp1"), "desktop", "meter", "g1", 7, 500,
                                     now - 1000,
                                     policy_hashes={"price": PRICE_HASH,
                                                    "child_cap": CAP_HASH})])).returncode, 0)
        reports = self._reports()
        self.assertEqual(reports["g1"]["last_seq"], 500)
        self.assertEqual(reports["g1"]["state"], "ok")

        # Re-point the retained gateway to the other machine (same account).
        re_pointed = copy.deepcopy(VALID_DOC)
        re_pointed["gateways"][0]["machine"] = "worker"
        self.assertEqual(_config_import(self.db, self.tmp, re_pointed).returncode, 0)

        # The old owner's report state is gone: no seq, epoch, or hashes
        # are carried over, and read-time the gateway has no report yet.
        reports = self._reports()
        self.assertIsNone(reports["g1"]["last_seq"])
        self.assertIsNone(reports["g1"]["ledger_epoch"])
        self.assertIsNone(reports["g1"]["policy_hashes"])
        self.assertIsNone(reports["g1"]["last_report_at"])
        self.assertEqual(reports["g1"]["state"], "none")

        # The new owner's first report (a new ledger, seq 1) is neither
        # silently dropped as a smaller seq nor silently continued: it is
        # accepted, and the gateway stays reconciliation_required until an
        # operator runs reconcile.
        self.assertEqual(_readings_import(self.db, self.tmp,
            _doc([_gateway_snapshot(_eid("rp2"), "worker", "meter", "g1", 7, 1,
                                     now - 500,
                                     policy_hashes={"price": PRICE_HASH,
                                                    "child_cap": CAP_HASH})])).returncode, 0)
        reports = self._reports()
        self.assertEqual(reports["g1"]["last_seq"], 1)
        self.assertEqual(reports["g1"]["state"], "reconciliation_required")

    def test_repoint_account_resets_report_state(self):
        doc = {
            "version": 1,
            "accounts": [
                {"alias": "meter", "vendor": "ovh", "kind": "metered",
                 "budget_micro_eur": 100_000_000},
                {"alias": "meter2", "vendor": "ovh", "kind": "metered",
                 "budget_micro_eur": 100_000_000},
            ],
            "machines": [{"alias": "desktop"}, {"alias": "worker"}],
            "gateways": [
                {"alias": "g1", "account": "meter", "machine": "desktop",
                 "share_micro_eur": 60_000_000, "period": "utc_month"},
                {"alias": "g2", "account": "meter", "machine": "worker",
                 "share_micro_eur": 40_000_000, "period": "utc_month"},
            ],
        }
        self.assertEqual(_config_import(self.db, self.tmp, doc).returncode, 0)

        # g1 (meter/desktop) has reported seq 500.
        now = _now_ms()
        self.assertEqual(_readings_import(self.db, self.tmp,
            _doc([_gateway_snapshot(_eid("ra1"), "desktop", "meter", "g1", 7, 500,
                                     now - 1000,
                                     policy_hashes={"price": PRICE_HASH,
                                                    "child_cap": CAP_HASH})])).returncode, 0)
        self.assertEqual(self._reports()["g1"]["last_seq"], 500)

        # Re-point the retained gateway to the other account (same machine).
        re_pointed = copy.deepcopy(doc)
        re_pointed["gateways"][0]["account"] = "meter2"
        self.assertEqual(_config_import(self.db, self.tmp, re_pointed).returncode, 0)
        self.assertIsNone(self._reports()["g1"]["last_seq"])

        # The new account's reporter is the same machine; its first report
        # (seq 1) is accepted and latches reconciliation_required.
        self.assertEqual(_readings_import(self.db, self.tmp,
            _doc([_gateway_snapshot(_eid("ra2"), "desktop", "meter2", "g1", 7, 1,
                                     now - 500,
                                     policy_hashes={"price": PRICE_HASH,
                                                    "child_cap": CAP_HASH})])).returncode, 0)
        reports = self._reports()
        self.assertEqual(reports["g1"]["last_seq"], 1)
        self.assertEqual(reports["g1"]["state"], "reconciliation_required")

    def test_repoint_without_reports_keeps_none(self):
        # Re-pointing a gateway that never reported stays clean: no report
        # state to reset, state none.
        re_pointed = copy.deepcopy(VALID_DOC)
        re_pointed["gateways"][0]["machine"] = "worker"
        self.assertEqual(_config_import(self.db, self.tmp, re_pointed).returncode, 0)
        self.assertEqual(self._reports()["g1"]["state"], "none")
        self.assertIsNone(self._reports()["g1"]["last_seq"])


class StoreConnectivity(_Base):
    """READ A (MAJOR): the store connection carries an explicit busy
    timeout, so a hot journal left by a crashed writer - or a lock held by
    a concurrent writer - is waited out for a bounded, documented duration
    instead of the driver's implicit default."""

    def test_connect_uses_explicit_busy_timeout(self):
        # _connect must pass an explicit, positive timeout to
        # sqlite3.connect rather than relying on the driver default.
        import quota_balancer.store as store
        captured = {}
        original = sqlite3.connect

        def spy(*args, **kwargs):
            captured.update(kwargs)
            return original(*args, **kwargs)

        store.sqlite3.connect = spy
        try:
            conn = store._connect(self.db, must_exist=True)
        finally:
            store.sqlite3.connect = original
            conn.close()
        self.assertIn("timeout", captured)
        self.assertGreater(float(captured["timeout"]), 0.0)

    def test_open_store_with_held_lock(self):
        # A store whose write lock is held by another connection (the state
        # a crashed writer's hot-journal recovery leaves) is waited out on
        # the busy timeout; the import proceeds once the lock is released
        # instead of failing immediately.
        import threading
        locked = threading.Event()

        def hold():
            holder = sqlite3.connect(self.db, isolation_level=None)
            try:
                holder.execute("BEGIN IMMEDIATE")  # takes the write lock
                locked.set()
                time.sleep(1.0)
                holder.execute("COMMIT")
            finally:
                holder.close()

        holder_thread = threading.Thread(target=hold)
        holder_thread.start()
        try:
            self.assertTrue(locked.wait(10.0))
            now = _now_ms()
            doc = _doc([_cli_window(_eid("lock1"), "desktop", "meter",
                                    "wk-lock", 1000, now - 10_000,
                                    now - 10_000)])
            result = _readings_import(self.db, self.tmp, doc)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self._window("wk-lock")["used_bps"], 1000)
        finally:
            holder_thread.join()


if __name__ == "__main__":
    unittest.main()
