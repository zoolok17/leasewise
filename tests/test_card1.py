"""Card 1 acceptance tests: standalone inventory, exactly three tables.

Run from the repository root:
    python -m unittest discover -s tests -v

Method names carry the card's check names with the unittest "test_" prefix,
e.g. card check `init_is_idempotent` is method `test_init_is_idempotent`.
"""

import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.environ.get("AGENTTALK_PY") or sys.executable

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

# A card-1 store (no readings imported) projects an empty report per gateway.
EMPTY_REPORT = (
    '{"state":"none","last_report_at":null,"ledger_epoch":null,"last_seq":null,'
    '"period_start":null,"period_end":null,"policy_hashes":null,"sized_ready":false}'
)

EXPECTED_STATUS = (
    '{"schema_version":5,"accounts":[{"alias":"meter","kind":"metered",'
    '"budget_micro_eur":100000000,"allocated_micro_eur":100000000,'
    '"coverage_complete":false,"windows":[]}],'
    '"machines":["desktop","worker"],"gateways":[{"alias":"g1","account":"meter",'
    '"machine":"desktop","share_micro_eur":60000000,"report":' + EMPTY_REPORT + '},'
    '{"alias":"g2","account":"meter","machine":"worker","share_micro_eur":40000000,'
    '"report":' + EMPTY_REPORT + '}],"billing":[]}'
)

ERROR_INVALID_CONFIG = '{"error":{"code":"INVALID_CONFIG","message":"config document is invalid"}}'
ERROR_SCHEMA = '{"error":{"code":"SCHEMA_UNSUPPORTED","message":"schema version is not supported"}}'
ERROR_STORE_NOT_FOUND = '{"error":{"code":"STORE_ERROR","message":"database not found"}}'
ERROR_STORE_FAILED = '{"error":{"code":"STORE_ERROR","message":"store operation failed"}}'


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


def _import(db, tmp, doc):
    cfg = os.path.join(tmp, "config.json")
    _write_json(cfg, doc)
    return run_cli("--db", db, "config-import", "--file", cfg)


def _status(db):
    return run_cli("--db", db, "status", "--json")


class _Base(unittest.TestCase):
    def setUp(self):
        # Temporary work goes under $AGENTTALK_SCRATCH when the operator set it.
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-card1-")
        self.tmp = self._tmp.name
        self.db = os.path.join(self.tmp, "inventory.db")

    def tearDown(self):
        self._tmp.cleanup()


class Card1Tests(_Base):
    def test_init_is_idempotent(self):
        first = run_cli("--db", self.db, "init")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = run_cli("--db", self.db, "init")  # reopen
        self.assertEqual(second.returncode, 0, second.stderr)
        conn = sqlite3.connect(self.db)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        finally:
            conn.close()
        self.assertEqual(version, 5)
        self.assertEqual(
            tables,
            {"accounts", "machines", "gateways", "readings", "windows",
             "projects", "teams", "project_accounts", "seats", "models",
             "profiles", "limits", "billing_policies", "billing_totals",
             "blocks", "leases", "holds", "receipts", "adjustments",
             "audit", "daily_totals"},
        )
        self.assertEqual(journal.lower(), "wal")

    def test_shares_cannot_exceed_account(self):
        run_cli("--db", self.db, "init")
        ok = _import(self.db, self.tmp, copy.deepcopy(VALID_DOC))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        invalid = copy.deepcopy(VALID_DOC)
        invalid["gateways"][1]["share_micro_eur"] = 40000001  # sum 100000001 > 100000000
        bad = _import(self.db, self.tmp, invalid)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG)
        self.assertEqual(bad.stdout, "")
        # Prior data intact.
        st = _status(self.db)
        self.assertEqual(st.returncode, 0)
        self.assertEqual(st.stdout.strip(), EXPECTED_STATUS)

    def test_invalid_config_rolls_back(self):
        run_cli("--db", self.db, "init")
        _import(self.db, self.tmp, copy.deepcopy(VALID_DOC))
        baseline = _status(self.db).stdout.strip()

        doc = copy.deepcopy(VALID_DOC)
        doc["machines"][0]["alias"] = "DeskTop"  # alias regex
        cases = {"bad_alias": doc}

        doc = copy.deepcopy(VALID_DOC)
        doc["gateways"][0]["account"] = "ghost"  # dangling reference
        cases["bad_reference"] = doc

        doc = copy.deepcopy(VALID_DOC)
        doc["gateways"][0]["weight"] = 1  # unknown key
        cases["unknown_key"] = doc

        doc = copy.deepcopy(VALID_DOC)
        doc["gateways"][1]["period"] = "weekly"  # wrong period
        cases["bad_period"] = doc

        for name, mutated in cases.items():
            bad = _import(self.db, self.tmp, mutated)
            self.assertEqual(bad.returncode, 2, name)
            self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG, name)
            self.assertEqual(_status(self.db).stdout.strip(), baseline, name)

    def test_money_is_integer(self):
        run_cli("--db", self.db, "init")
        for value in (100000000.0, True, -5, 2 ** 63, 10 ** 20):
            doc = copy.deepcopy(VALID_DOC)
            doc["accounts"][0]["budget_micro_eur"] = value
            bad = _import(self.db, self.tmp, doc)
            self.assertEqual(bad.returncode, 2, repr(value))
            self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG, repr(value))
        doc = copy.deepcopy(VALID_DOC)
        doc["gateways"][0]["share_micro_eur"] = 60000000.0  # float share
        bad = _import(self.db, self.tmp, doc)
        self.assertEqual(bad.returncode, 2)
        # The integer fixture succeeds.
        ok = _import(self.db, self.tmp, copy.deepcopy(VALID_DOC))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(_status(self.db).stdout.strip(), EXPECTED_STATUS)

    def test_privacy_1(self):
        # Sentinel aliases are operator aliases: they may survive in status.
        alias_sentinel = "priv-acct-7"
        machine_sentinel = "priv-mach-3"
        gateway_sentinel = "priv-gw-9"
        doc = copy.deepcopy(VALID_DOC)
        doc["accounts"][0]["alias"] = alias_sentinel
        doc["machines"][0]["alias"] = machine_sentinel
        doc["gateways"][0]["alias"] = gateway_sentinel
        doc["gateways"][0]["account"] = alias_sentinel
        doc["gateways"][0]["machine"] = machine_sentinel
        doc["gateways"][1]["account"] = alias_sentinel
        run_cli("--db", self.db, "init")
        ok = _import(self.db, self.tmp, doc)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(ok.stdout, "")  # import prints nothing
        st = _status(self.db)
        self.assertEqual(st.returncode, 0)
        # The status is exactly the documented projection: nothing beyond the
        # approved aliases and the static schema/field names may appear.
        expected = (
            '{"schema_version":5,"accounts":[{"alias":"priv-acct-7","kind":"metered",'
            '"budget_micro_eur":100000000,"allocated_micro_eur":100000000,'
            '"coverage_complete":false,"windows":[]}],'
            '"machines":["priv-mach-3","worker"],"gateways":[{"alias":"g2",'
            '"account":"priv-acct-7","machine":"worker","share_micro_eur":40000000,'
            '"report":' + EMPTY_REPORT + '},'
            '{"alias":"priv-gw-9","account":"priv-acct-7","machine":"priv-mach-3",'
            '"share_micro_eur":60000000,"report":' + EMPTY_REPORT + '}],'
            '"billing":[]}'
        )
        self.assertEqual(st.stdout.strip(), expected)
        self.assertNotIn("vendor", st.stdout)
        # Error path: a secret in a non-alias field must not be echoed.
        secret = "secret-provider-id-zz9"
        bad_doc = copy.deepcopy(VALID_DOC)
        bad_doc["gateways"][0]["region"] = secret  # unknown key
        bad = _import(self.db, self.tmp, bad_doc)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG)
        self.assertNotIn(secret, bad.stderr)
        self.assertNotIn(secret, bad.stdout)
        # A secret used as a bad reference must not be echoed either.
        bad_doc = copy.deepcopy(VALID_DOC)
        bad_doc["gateways"][0]["account"] = secret
        bad = _import(self.db, self.tmp, bad_doc)
        self.assertEqual(bad.returncode, 2)
        self.assertNotIn(secret, bad.stderr + bad.stdout)
        # Store-IO path: a sentinel in the DB path must not be echoed.
        path_sentinel = "db-path-sentinel-5f2e"
        bad_db = os.path.join(self.tmp, path_sentinel, "inventory.db")
        bad = run_cli("--db", bad_db, "init")
        self.assertEqual(bad.returncode, 3)
        self.assertEqual(bad.stderr.strip(), ERROR_STORE_FAILED)
        self.assertNotIn(path_sentinel, bad.stderr + bad.stdout)


class Card1CarryOverTests(_Base):
    """Carry-over (card 1): concurrent `init` on a fresh store is idempotent.

    Before the fix the WAL switch in `migrate.set_wal` ran outside the write
    lock and, unlike ordinary statements, does not wait through the busy
    handler: a concurrent init's DDL transaction could make it raise
    "database is locked", so one of the parallel inits exited 3 (STORE_ERROR).
    The fix retries the WAL switch on the busy condition until a bounded
    deadline, so parallel inits serialize on the write lock and a latecomer
    observes the current schema version as a no-op. The race is
    timing-dependent, so the check
    runs several init processes in parallel over repeated rounds on a fresh
    store and requires every process to exit 0.
    """

    PROCS = 16
    ROUNDS = 20

    def _init_parallel(self):
        """Launch PROCS `init` processes against the store; return exit codes."""
        procs = [
            subprocess.Popen(
                [PYTHON, "-m", "quota_balancer", "--db", self.db, "init"],
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(self.PROCS)
        ]
        codes = []
        for p in procs:
            # communicate() drains the pipes and waits, keeping the others
            # running in parallel rather than serializing on the pipe buffer.
            p.communicate()
            codes.append(p.returncode)
        return codes

    def test_concurrent_init_all_exit_zero(self):
        for round_no in range(self.ROUNDS):
            # Fresh store each round: the race is on a version-0 file, so the
            # -wal/-shm side files are removed too.
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(self.db + suffix)
                except FileNotFoundError:
                    pass
            codes = self._init_parallel()
            self.assertEqual(
                codes,
                [0] * self.PROCS,
                f"round {round_no}: a parallel init did not exit 0 "
                f"(sorted codes {sorted(codes)})",
            )
        # Converged on the current store: version 5, WAL, all tables.
        conn = sqlite3.connect(self.db)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        finally:
            conn.close()
        self.assertEqual(version, 5)
        self.assertEqual(journal.lower(), "wal")
        self.assertEqual(
            tables,
            {"accounts", "machines", "gateways", "readings", "windows",
             "projects", "teams", "project_accounts", "seats", "models",
             "profiles", "limits", "billing_policies", "billing_totals",
             "blocks", "leases", "holds", "receipts", "adjustments",
             "audit", "daily_totals"},
        )


class Card1FixRoundTests(_Base):
    """Fix-round tests for the 350478c cold reads.

    Mutation pins: the named checks go red under frontend-dev's surviving
    mutations (M2: drop the metered-only gateway check; M5: drop duplicate
    gateway alias check; M6: drop duplicate account alias check).
    """

    def test_rejected_schema_store_left_unchanged(self):
        # A store at a newer, unknown user_version must be rejected by every
        # verb and left byte-for-byte unchanged: no header rewrite, no
        # -wal/-shm side files (the tool never modifies a rejected store).
        # Version 6 is one newer than the current version (5).
        run_cli("--db", self.db, "init")
        _import(self.db, self.tmp, copy.deepcopy(VALID_DOC))
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA user_version=6")
        # Downgrade to delete journal mode: the tool must not switch a
        # rejected store to WAL either (that rewrites the file header).
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.close()
        with open(self.db, "rb") as handle:
            before = handle.read()
        for suffix in ("-wal", "-shm"):
            self.assertFalse(os.path.exists(self.db + suffix))

        st = _status(self.db)
        self.assertEqual(st.returncode, 2)
        self.assertEqual(st.stderr.strip(), ERROR_SCHEMA)
        bad = _import(self.db, self.tmp, copy.deepcopy(VALID_DOC))
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_SCHEMA)
        bad = run_cli("--db", self.db, "init")
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_SCHEMA)

        with open(self.db, "rb") as handle:
            self.assertEqual(handle.read(), before)
        for suffix in ("-wal", "-shm"):
            self.assertFalse(os.path.exists(self.db + suffix))

    def test_mid_migration_failure_leaves_no_half_store(self):
        # A foreign table named like the third CREATE triggers a failure
        # mid-migration (after accounts/machines were created). The failed
        # migration must roll back: only the foreign table remains, and the
        # store is still version 0.
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE gateways (x INTEGER)")
        conn.close()
        bad = run_cli("--db", self.db, "init")
        self.assertEqual(bad.returncode, 3)
        self.assertEqual(bad.stderr.strip(), ERROR_STORE_FAILED)
        conn = sqlite3.connect(self.db)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            foreign_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='gateways'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(tables, {"gateways"})
        self.assertEqual(version, 0)
        self.assertEqual(foreign_sql, "CREATE TABLE gateways (x INTEGER)")

    def test_gateway_on_subscription_account_rejected(self):
        # M2 pin: only metered accounts may carry gateway shares.
        run_cli("--db", self.db, "init")
        _import(self.db, self.tmp, copy.deepcopy(VALID_DOC))
        baseline = _status(self.db).stdout.strip()
        doc = copy.deepcopy(VALID_DOC)
        doc["accounts"][0]["kind"] = "subscription"
        doc["accounts"][0]["budget_micro_eur"] = None
        bad = _import(self.db, self.tmp, doc)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG)
        self.assertEqual(_status(self.db).stdout.strip(), baseline)

    def test_duplicate_gateway_alias_rejected(self):
        # M5 pin: gateway aliases are unique.
        run_cli("--db", self.db, "init")
        _import(self.db, self.tmp, copy.deepcopy(VALID_DOC))
        baseline = _status(self.db).stdout.strip()
        doc = copy.deepcopy(VALID_DOC)
        doc["gateways"][1]["alias"] = "g1"
        bad = _import(self.db, self.tmp, doc)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG)
        self.assertEqual(_status(self.db).stdout.strip(), baseline)

    def test_duplicate_account_alias_rejected(self):
        # M6 pin: account aliases are unique (machine aliases likewise).
        run_cli("--db", self.db, "init")
        _import(self.db, self.tmp, copy.deepcopy(VALID_DOC))
        baseline = _status(self.db).stdout.strip()
        doc = copy.deepcopy(VALID_DOC)
        doc["accounts"].append(copy.deepcopy(doc["accounts"][0]))
        bad = _import(self.db, self.tmp, doc)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG)
        doc = copy.deepcopy(VALID_DOC)
        doc["machines"].append(copy.deepcopy(doc["machines"][0]))
        bad = _import(self.db, self.tmp, doc)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG)
        self.assertEqual(_status(self.db).stdout.strip(), baseline)

    def test_subscription_account_import(self):
        # The kind='subscription' branch: budget must be null, and the
        # status projects it as null with zero allocation.
        run_cli("--db", self.db, "init")
        doc = {
            "version": 1,
            "accounts": [
                {"alias": "seat", "vendor": "openai", "kind": "subscription",
                 "budget_micro_eur": None},
                {"alias": "meter", "vendor": "ovh", "kind": "metered",
                 "budget_micro_eur": 100000000},
            ],
            "machines": [{"alias": "desktop"}],
            "gateways": [
                {"alias": "g1", "account": "meter", "machine": "desktop",
                 "share_micro_eur": 100000000, "period": "utc_month"},
            ],
        }
        ok = _import(self.db, self.tmp, doc)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        expected = (
            '{"schema_version":5,"accounts":['
            '{"alias":"meter","kind":"metered","budget_micro_eur":100000000,'
            '"allocated_micro_eur":100000000,"coverage_complete":false,"windows":[]},'
            '{"alias":"seat","kind":"subscription","budget_micro_eur":null,'
            '"allocated_micro_eur":0,"coverage_complete":false,"windows":[]}],'
            '"machines":["desktop"],"gateways":['
            '{"alias":"g1","account":"meter","machine":"desktop",'
            '"share_micro_eur":100000000,"report":' + EMPTY_REPORT + '}]'
            ',"billing":[]}'
        )
        self.assertEqual(_status(self.db).stdout.strip(), expected)
        # A subscription account carrying a budget is invalid.
        bad_doc = copy.deepcopy(doc)
        bad_doc["accounts"][0]["budget_micro_eur"] = 5
        bad = _import(self.db, self.tmp, bad_doc)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG)
        self.assertEqual(_status(self.db).stdout.strip(), expected)

    def test_validation_edges(self):
        # Remaining closed-shape edges: vendor enum, a missing key, and
        # zero money values.
        run_cli("--db", self.db, "init")
        _import(self.db, self.tmp, copy.deepcopy(VALID_DOC))
        baseline = _status(self.db).stdout.strip()

        doc = copy.deepcopy(VALID_DOC)
        doc["accounts"][0]["vendor"] = "aws"
        cases = {"vendor_enum": doc}

        doc = copy.deepcopy(VALID_DOC)
        del doc["accounts"][0]["vendor"]
        cases["missing_key"] = doc

        doc = copy.deepcopy(VALID_DOC)
        doc["accounts"][0]["budget_micro_eur"] = 0
        cases["zero_budget"] = doc

        doc = copy.deepcopy(VALID_DOC)
        doc["gateways"][0]["share_micro_eur"] = 0
        cases["zero_share"] = doc

        for name, mutated in cases.items():
            bad = _import(self.db, self.tmp, mutated)
            self.assertEqual(bad.returncode, 2, name)
            self.assertEqual(bad.stderr.strip(), ERROR_INVALID_CONFIG, name)
        self.assertEqual(_status(self.db).stdout.strip(), baseline)


class Card1ContractTests(_Base):
    def test_status_shape_is_exact(self):
        run_cli("--db", self.db, "init")
        _import(self.db, self.tmp, copy.deepcopy(VALID_DOC))
        st = _status(self.db)
        self.assertEqual(st.returncode, 0)
        self.assertEqual(st.stdout.strip(), EXPECTED_STATUS)

    def test_schema_version_rejected(self):
        run_cli("--db", self.db, "init")
        doc = copy.deepcopy(VALID_DOC)
        doc["version"] = 2
        bad = _import(self.db, self.tmp, doc)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_SCHEMA)
        doc["version"] = "1"  # wrong type
        bad = _import(self.db, self.tmp, doc)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_SCHEMA)
        doc["version"] = True  # boolean is not the integer 1
        bad = _import(self.db, self.tmp, doc)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stderr.strip(), ERROR_SCHEMA)

    def test_uuids_retained_for_retained_aliases(self):
        run_cli("--db", self.db, "init")
        _import(self.db, self.tmp, copy.deepcopy(VALID_DOC))
        conn = sqlite3.connect(self.db)
        try:
            account_id = conn.execute(
                "SELECT id FROM accounts WHERE alias='meter'").fetchone()[0]
            machine_id = conn.execute(
                "SELECT id FROM machines WHERE alias='desktop'").fetchone()[0]
        finally:
            conn.close()

        doc = copy.deepcopy(VALID_DOC)
        doc["machines"].append({"alias": "laptop"})
        ok = _import(self.db, self.tmp, doc)
        self.assertEqual(ok.returncode, 0, ok.stderr)

        conn = sqlite3.connect(self.db)
        try:
            same_account = conn.execute(
                "SELECT id FROM accounts WHERE alias='meter'").fetchone()[0]
            same_machine = conn.execute(
                "SELECT id FROM machines WHERE alias='desktop'").fetchone()[0]
            new_machine = conn.execute(
                "SELECT id FROM machines WHERE alias='laptop'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(same_account, account_id)
        self.assertEqual(same_machine, machine_id)
        self.assertTrue(new_machine)
        self.assertNotEqual(new_machine, machine_id)

    def test_uninit_store_errors(self):
        st = _status(self.db)  # no file at all
        self.assertEqual(st.returncode, 3)
        self.assertEqual(st.stderr.strip(), ERROR_STORE_NOT_FOUND)
        bad = _import(self.db, self.tmp, copy.deepcopy(VALID_DOC))  # also no store
        self.assertEqual(bad.returncode, 3)
        self.assertEqual(bad.stderr.strip(), ERROR_STORE_NOT_FOUND)
        # An existing but uninitialized (zero-byte) file is an unknown schema state.
        with open(self.db, "wb"):
            pass
        st = _status(self.db)
        self.assertEqual(st.returncode, 2)
        self.assertEqual(st.stderr.strip(), ERROR_SCHEMA)
