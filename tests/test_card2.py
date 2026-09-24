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


def _now_ms():
    return int(time.time() * 1000)


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
        base = _doc([_cli_window("e1", "desktop", "meter", "wk1", 4000,
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
        doc = _doc([_cli_window("s1", "desktop", "meter", "wkStale", 7500,
                                source_time, source_time, stale=True)])
        self.assertEqual(_readings_import(self.db, self.tmp, doc).returncode, 0)

        window = self._window("wkStale")
        self.assertEqual(window["used_bps"], 7500)      # value preserved
        self.assertEqual(window["source_stale"], True)  # flag preserved
        self.assertEqual(window["observed_at"], source_time)  # source time preserved

        # The store keeps the source time and a distinct, later ingest time.
        conn = sqlite3.connect(self.db)
        try:
            observed_at, received_at = conn.execute(
                "SELECT observed_at, received_at FROM readings WHERE event_id='s1'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(observed_at, source_time)
        self.assertGreater(received_at, source_time)  # ingest is after the sample

    def test_coverage_fallback(self):
        # Given incomplete coverage and a sample older than the 600-second
        # wall cap: the window is stale. Telemetry cannot set coverage_complete.
        old_time = _now_ms() - 700000  # 700 seconds ago, past the 600s cap
        doc = _doc([_cli_window("c1", "desktop", "meter", "wkOld", 500,
                                old_time, old_time)])
        self.assertEqual(_readings_import(self.db, self.tmp, doc).returncode, 0)

        stale_window = self._window("wkOld")
        self.assertFalse(stale_window["current"])
        self.assertEqual(stale_window["freshness_reason"], "wall_cap")

        # Incomplete coverage (never set by the operator) and a fresh sample
        # within the cap: the window is current.
        fresh_time = _now_ms() - 5000
        doc2 = _doc([_cli_window("c2", "desktop", "meter", "wkFresh", 500,
                                 fresh_time, fresh_time)])
        self.assertEqual(_readings_import(self.db, self.tmp, doc2).returncode, 0)
        fresh_window = self._window("wkFresh")
        self.assertTrue(fresh_window["current"])
        self.assertEqual(fresh_window["freshness_reason"], "fresh")

        # Telemetry cannot set coverage_complete: it stays false, and stays
        # whatever the operator last set it to.
        self.assertFalse(self._status_json()["accounts"][0]["coverage_complete"])
        self.assertEqual(_set_coverage(self.db, "meter", True).returncode, 0)
        self.assertTrue(self._status_json()["accounts"][0]["coverage_complete"])
        more = _doc([_cli_window("c3", "desktop", "meter", "wkMore", 600,
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

        ambiguous = _doc([_cli_window("a1", "shared", None, "wkShared", 5000,
                                      _now_ms() - 10000, _now_ms() - 10000)])
        self.assertEqual(_readings_import(self.db, self.tmp, ambiguous).returncode, 0)

        # No window was created for either account.
        self.assertIsNone(self._window("wkShared"))
        for account in self._status_json()["accounts"]:
            self.assertEqual(account["windows"], [])
        # The ambiguous reading is still recorded for dedup/audit.
        conn = sqlite3.connect(self.db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM readings WHERE event_id='a1'"
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
            _doc([_cli_window("p1", "desktop", secret, "wkP", 1000,
                              _now_ms() - 10000, _now_ms() - 10000)]))
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG)
        self.assertNotIn(secret, bad.stderr + bad.stdout)

        # A provider id used as a machine reference is likewise not echoed.
        bad2 = _readings_import(
            self.db, self.tmp,
            _doc([_cli_window("p2", provider, "meter", "wkP", 1000,
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
            _doc([_cli_window("h1", "desktop", "meter", "wkH", 4000, epoch,
                              now - 30000)])).returncode, 0)
        # A later observation, higher usage: the mark rises.
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window("h2", "desktop", "meter", "wkH", 6000, epoch,
                              now - 20000)])).returncode, 0)
        self.assertEqual(self._window("wkH")["used_bps"], 6000)
        # A later observation, lower usage: the mark does NOT fall.
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window("h3", "desktop", "meter", "wkH", 2000, epoch,
                              now - 10000)])).returncode, 0)
        self.assertEqual(self._window("wkH")["used_bps"], 6000)

    def test_new_epoch_requires_post_reset_observation(self):
        # A new, later epoch is restarted by its first post-reset observation;
        # an older epoch is out of order and a no-op.
        now = _now_ms()
        current_epoch = now - 50000
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window("n1", "desktop", "meter", "wkN", 6000, current_epoch,
                              now - 10000)])).returncode, 0)
        self.assertEqual(self._window("wkN")["used_bps"], 6000)

        # A later reset epoch with a fresh observation: the mark restarts.
        new_epoch = now + 7200000
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window("n2", "desktop", "meter", "wkN", 1000, new_epoch,
                              now)])).returncode, 0)
        window = self._window("wkN")
        self.assertEqual(window["used_bps"], 1000)
        self.assertEqual(window["resets_at"], new_epoch)

        # An older (out-of-order) epoch must not regress the window.
        old_epoch = now - 300000
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window("n3", "desktop", "meter", "wkN", 9999, old_epoch,
                              now - 200000)])).returncode, 0)
        self.assertEqual(self._window("wkN")["used_bps"], 1000)

    def test_reordered_samples(self):
        # A "later" observation arriving first, then an "earlier" one out of
        # order: the window tracks the latest observed_at and the high-water
        # usage, regardless of arrival order.
        now = _now_ms()
        epoch = now - 50000
        # Arrives first but is the LATER observation (higher usage).
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window("r2", "desktop", "meter", "wkR", 6000, epoch,
                              now - 10000)])).returncode, 0)
        # Arrives second but is the EARLIER observation (lower usage).
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_cli_window("r1", "desktop", "meter", "wkR", 4000, epoch,
                              now - 40000)])).returncode, 0)
        window = self._window("wkR")
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
            _doc([_gateway_snapshot("f1", "desktop", "meter", "g1", 7, 100,
                                    now - 5000, policy_hashes={
                                        "price": "p1", "child_cap": "c1"},
                                    sized_ready=True)])).returncode, 0)
        reports = {g["alias"]: g["report"] for g in self._status_json()["gateways"]}
        self.assertEqual(reports["g1"]["state"], "ok")

        # A report for g2 observed 130 seconds ago: stale.
        self.assertEqual(_readings_import(
            self.db, self.tmp,
            _doc([_gateway_snapshot("s2", "worker", "meter", "g2", 3, 10,
                                    now - 130000)])).returncode, 0)
        reports = {g["alias"]: g["report"] for g in self._status_json()["gateways"]}
        self.assertEqual(reports["g2"]["state"], "stale")


class ReducerValidation(_Base):
    def test_rejects_future_observation(self):
        # A reading more than 30 seconds in the future is rejected.
        future = _now_ms() + 120000
        bad = _readings_import(
            self.db, self.tmp,
            _doc([_cli_window("fv", "desktop", "meter", "wkF", 1000,
                              future, future)]))
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG)

    def test_rejects_unknown_window_field(self):
        # The payload shape is closed: an unknown window field is rejected.
        doc = _doc([_cli_window("u1", "desktop", "meter", "wkU", 1000,
                                _now_ms() - 10000, _now_ms() - 10000)])
        doc["readings"][0]["payload"]["windows"][0]["extra"] = 1
        bad = _readings_import(self.db, self.tmp, doc)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG)


if __name__ == "__main__":
    unittest.main()
