"""Card L1a, turn 1: config-import version 2 (the seven policy tables) and
the R-003 interim fix (docs/DESIGN.md sections 6-8; docs/STEP-CARD-L1A.md).

Rows: policy_import_roundtrip, policy_import_rollback,
policy_import_referenced_removal, version1_still_imports,
grant_after_import, r003_metered_never_enforcing, privacy_l1a.

Every row builds its store through the CLI verbs (init, config-import,
reading-import, identity-set): the tests insert no policy row with SQL.
All data is synthetic.
"""

import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable

from quota_balancer import errors, http_api, lease  # noqa: E402
from quota_balancer.errors import QuotaError  # noqa: E402

ERROR_INVALID_CONFIG = ('{"error":{"code":"INVALID_CONFIG",'
                        '"message":"config document is invalid"}}')
ERROR_SCHEMA_UNSUPPORTED = ('{"error":{"code":"SCHEMA_UNSUPPORTED",'
                            '"message":"schema version is not supported"}}')
PLATFORM_ID = "00000000-0000-0000-0000-000000000000"
POLICY_HASH = "ab" * 32
POLICY_TABLES = ("projects", "teams", "project_accounts", "seats", "models",
                 "profiles", "limits")
INVENTORY_TABLES = ("accounts", "machines", "gateways")


def _model(account, alias, efforts):
    return {
        "account": account, "alias": alias, "context_ceiling": 180000,
        "output_ceiling": 18000, "tariff_input_rate": 1,
        "tariff_output_rate": 1, "reserve_input_rate": 1_000_000,
        "reserve_output_rate": 1_000_000, "reservation_mode": "sized",
        "policy_hash": POLICY_HASH, "effort_aliases": efforts,
    }


def base_doc():
    """A complete version-2 document over synthetic aliases."""
    return {
        "version": 2,
        "accounts": [
            {"alias": "meter", "vendor": "ovh", "kind": "metered",
             "budget_micro_eur": 100_000_000},
            {"alias": "other", "vendor": "ovh", "kind": "subscription",
             "budget_micro_eur": None},
        ],
        "machines": [{"alias": "desktop"}, {"alias": "worker"}],
        "gateways": [
            {"alias": "g1", "account": "meter", "machine": "desktop",
             "share_micro_eur": 100_000_000, "period": "utc_month"}],
        "projects": [{"alias": "org"}],
        "teams": [{"project": "org", "alias": "dev"}],
        "project_accounts": [{"project": "org", "account": "meter"}],
        "seats": [
            {"team": {"project": "org", "team": "dev"}, "machine": "desktop",
             "alias": "s1", "role": "main",
             "enforcement": "metered_advisory"},
            {"team": {"project": "org", "team": "dev"}, "machine": "worker",
             "alias": "s2", "role": "review", "enforcement": "shadow"},
            {"team": {"project": "org", "team": "dev"}, "machine": "desktop",
             "alias": "s3", "role": "main", "enforcement": "metered"},
        ],
        "models": [_model("meter", "opus", ["opus"]),
                   _model("meter", "fast", ["fast"])],
        "profiles": [{
            "task_class": "bounded_read",
            "candidates": ["opus", "fast", "park"],
            "context_rules": {}, "calibration": {},
            "expected_calls": 1, "growth_tokens": 0, "margin_bps": 12500,
            "max_calls": 2, "output_cap": 1000, "per_call_output_cap": 500,
            "ttl_ms": 1_800_000, "policy_revision": 1}],
        "limits": [
            {"scope": "platform", "metric": "input_tokens", "period": "turn",
             "window_key": None, "soft": None, "hard": 1_000_000},
            {"scope": "team", "project": "org", "team": "dev",
             "metric": "money_micro_eur", "period": "month",
             "window_key": None, "soft": 50_000_000, "hard": 100_000_000},
            {"scope": "seat", "machine": "desktop", "seat": "s1",
             "metric": "concurrency", "period": "turn", "window_key": None,
             "soft": None, "hard": 2},
            {"scope": "seat", "machine": "desktop", "seat": "s1",
             "metric": "window_bps", "period": "window",
             "window_key": "five_hour", "soft": None, "hard": 9000},
        ],
    }


def run_cli(*args):
    return subprocess.run([PYTHON, "-m", "quota_balancer", *args],
                          cwd=REPO_ROOT, capture_output=True, text=True)


class _Base(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base,
                                                prefix="quota-cardl1a-")
        self.tmp = self._tmp.name
        self.db = os.path.join(self.tmp, "inventory.db")
        self.assertEqual(run_cli("--db", self.db, "init").returncode, 0)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, doc, name="config.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)
        return path

    def import_doc(self, doc, db=None):
        return run_cli("--db", db or self.db, "config-import", "--file",
                       self.write(doc))

    def import_ok(self, doc):
        result = self.import_doc(doc)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def rows(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def dump(self, tables=POLICY_TABLES + INVENTORY_TABLES):
        """Every row of the named tables, for before/after equality."""
        conn = sqlite3.connect(self.db)
        try:
            return {t: sorted(conn.execute("SELECT * FROM " + t).fetchall(),
                              key=repr) for t in tables}
        finally:
            conn.close()

    def assert_refused(self, doc, stderr=ERROR_INVALID_CONFIG, label=""):
        before = self.dump()
        result = self.import_doc(doc)
        self.assertEqual(result.returncode, 2, (label, result.stderr))
        self.assertEqual(result.stderr.strip(), stderr, label)
        self.assertEqual(result.stdout, "", label)
        self.assertEqual(self.dump(), before, label)

    def ids(self, table, column="alias"):
        return dict(self.rows("SELECT %s, id FROM %s" % (column, table)))


class PolicyImportRoundtrip(_Base):
    """policy_import_roundtrip: version 2 imports; a re-import is a no-op
    with retained UUIDs; retained aliases update config-owned columns only."""

    def test_import_populates_every_table(self):
        self.import_ok(base_doc())
        counts = {t: self.rows("SELECT COUNT(*) FROM " + t)[0][0]
                  for t in POLICY_TABLES}
        self.assertEqual(counts, {
            "projects": 1, "teams": 1, "project_accounts": 1, "seats": 3,
            "models": 2, "profiles": 1, "limits": 4})
        seat = self.rows("SELECT s.alias, s.role, s.enforcement, "
                         "m.alias, t.alias, p.alias FROM seats s "
                         "JOIN machines m ON m.id=s.machine_id "
                         "JOIN teams t ON t.id=s.team_id "
                         "JOIN projects p ON p.id=t.project_id "
                         "ORDER BY s.alias")
        self.assertEqual(seat[0], ("s1", "main", "metered_advisory",
                                   "desktop", "dev", "org"))
        # Stored the way grant reads them: comma-joined efforts, a JSON
        # candidate list (the terminal 'park' is implicit), '{}' reserved.
        self.assertEqual(self.rows(
            "SELECT effort_aliases FROM models WHERE alias='fast'"),
            [("fast",)])
        profile = self.rows("SELECT candidates, context_rules, calibration "
                            "FROM profiles")[0]
        self.assertEqual(profile, ('["opus", "fast"]', "{}", "{}"))
        # The platform limit carries the fixed platform id.
        self.assertEqual(self.rows(
            "SELECT scope_id FROM limits WHERE scope='platform'"),
            [(PLATFORM_ID,)])
        self.assertEqual(self.rows(
            "SELECT window_key, hard FROM limits WHERE metric='window_bps'"),
            [("five_hour", 9000)])

    def test_reimport_is_a_noop_with_retained_uuids(self):
        self.import_ok(base_doc())
        before = self.dump()
        self.import_ok(base_doc())
        self.assertEqual(self.dump(), before)
        # A new process (every CLI call is one) sees the same ids.
        self.import_ok(base_doc())
        self.assertEqual(self.dump(), before)

    def test_changed_columns_update_in_place_and_keep_ids(self):
        self.import_ok(base_doc())
        seat_ids = self.ids("seats")
        model_ids = self.ids("models")
        limit_ids = {r[0]: r[1] for r in self.rows(
            "SELECT metric || '/' || scope, id FROM limits")}
        # last_seen_at is operational: import never writes it.
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE seats SET last_seen_at=12345")
        conn.commit()
        conn.close()
        doc = base_doc()
        doc["seats"][0].update(role="lead", enforcement="shadow")
        doc["models"][0]["context_ceiling"] = 190000
        doc["models"][0]["effort_aliases"] = ["opus", "deep"]
        doc["profiles"][0].update(ttl_ms=3_600_000, policy_revision=2)
        doc["limits"][0]["hard"] = 777
        self.import_ok(doc)
        self.assertEqual(self.ids("seats"), seat_ids)
        self.assertEqual(self.ids("models"), model_ids)
        self.assertEqual(self.rows(
            "SELECT role, enforcement FROM seats WHERE alias='s1'"),
            [("lead", "shadow")])
        self.assertEqual(self.rows("SELECT DISTINCT last_seen_at FROM seats"),
                         [(12345,)])
        self.assertEqual(self.rows(
            "SELECT context_ceiling, effort_aliases FROM models "
            "WHERE alias='opus'"), [(190000, "opus,deep")])
        self.assertEqual(self.rows("SELECT ttl_ms, policy_revision "
                                   "FROM profiles"), [(3_600_000, 2)])
        self.assertEqual({r[0]: r[1] for r in self.rows(
            "SELECT metric || '/' || scope, id FROM limits")}, limit_ids)
        self.assertEqual(self.rows(
            "SELECT hard FROM limits WHERE scope='platform'"), [(777,)])

    def test_inventory_state_columns_survive_a_v2_reimport(self):
        self.import_ok(base_doc())
        self.assertEqual(run_cli("--db", self.db, "set-coverage", "--account",
                                 "meter", "--complete", "true").returncode, 0)
        self.import_ok(base_doc())
        self.assertEqual(self.rows(
            "SELECT coverage_complete FROM accounts WHERE alias='meter'"),
            [(1,)])

    def test_empty_policy_lists_are_valid(self):
        doc = base_doc()
        for key in ("projects", "teams", "project_accounts", "seats",
                    "models", "profiles", "limits"):
            doc[key] = []
        self.import_ok(doc)
        self.assertEqual(self.dump(POLICY_TABLES),
                         {t: [] for t in POLICY_TABLES})

    def test_a_seat_may_move_team_and_keep_its_id(self):
        doc = base_doc()
        doc["teams"].append({"project": "org", "alias": "ops"})
        self.import_ok(doc)
        seat_ids = self.ids("seats")
        doc["seats"][1]["team"] = {"project": "org", "team": "ops"}
        self.import_ok(doc)
        self.assertEqual(self.ids("seats"), seat_ids)
        self.assertEqual(self.rows(
            "SELECT t.alias FROM seats s JOIN teams t ON t.id=s.team_id "
            "WHERE s.alias='s2'"), [("ops",)])


# Each mutation turns the base document into a document that must be
# refused, whatever the reason: (label, mutation).
def _mut(label, fn):
    return (label, fn)


def _set(path, value):
    def apply(doc):
        node = doc
        for part in path[:-1]:
            node = node[part]
        node[path[-1]] = value
    return apply


def _drop(path):
    def apply(doc):
        node = doc
        for part in path[:-1]:
            node = node[part]
        del node[path[-1]]
    return apply


def _extend(key, item):
    return lambda doc: doc[key].append(item)


MUTATIONS = [
    # bad references
    _mut("team of unknown project", _set(["teams", 0, "project"], "nope")),
    _mut("project_account unknown account",
         _set(["project_accounts", 0, "account"], "nope")),
    _mut("project_account unknown project",
         _set(["project_accounts", 0, "project"], "nope")),
    _mut("seat unknown team", _set(["seats", 0, "team", "team"], "nope")),
    _mut("seat unknown machine", _set(["seats", 0, "machine"], "nope")),
    _mut("model unknown account", _set(["models", 0, "account"], "nope")),
    _mut("candidate unknown model",
         _set(["profiles", 0, "candidates"], ["opus", "ghost"])),
    _mut("limit unknown project",
         _set(["limits", 1, "project"], "nope")),
    _mut("limit unknown team", _set(["limits", 1, "team"], "nope")),
    _mut("limit unknown seat", _set(["limits", 2, "seat"], "nope")),
    _mut("references are not UUIDs",
         _set(["project_accounts", 0, "project"], PLATFORM_ID)),
    # extra / missing keys at every level
    _mut("extra top key", _set(["extra"], 1)),
    _mut("extra project key", _set(["projects", 0, "id"], "x")),
    _mut("extra team key", _set(["teams", 0, "id"], "x")),
    _mut("extra seat key", _set(["seats", 0, "last_seen_at"], 1)),
    _mut("extra seat team key", _set(["seats", 0, "team", "id"], "x")),
    _mut("extra model key", _set(["models", 0, "id"], "x")),
    _mut("extra profile key", _set(["profiles", 0, "id"], "x")),
    _mut("extra limit key", _set(["limits", 0, "id"], "x")),
    _mut("platform limit with a path", _set(["limits", 0, "project"], "org")),
    _mut("project limit with a team path",
         _set(["limits", 1, "scope"], "project")),
    _mut("missing top key", _drop(["limits"])),
    _mut("missing profiles key", _drop(["profiles"])),
    _mut("missing seat key", _drop(["seats", 0, "role"])),
    _mut("missing model key", _drop(["models", 0, "policy_hash"])),
    _mut("missing limit key", _drop(["limits", 0, "soft"])),
    _mut("list key is not a list", _set(["seats"], {})),
    # bool-as-int, floats, ranges
    _mut("bool ttl", _set(["profiles", 0, "ttl_ms"], True)),
    _mut("bool margin", _set(["profiles", 0, "margin_bps"], True)),
    _mut("bool hard", _set(["limits", 0, "hard"], True)),
    _mut("bool context ceiling", _set(["models", 0, "context_ceiling"], True)),
    _mut("float hard", _set(["limits", 0, "hard"], 5.0)),
    _mut("float expected_calls",
         _set(["profiles", 0, "expected_calls"], 1.0)),
    _mut("string revision", _set(["profiles", 0, "policy_revision"], "1")),
    _mut("negative growth", _set(["profiles", 0, "growth_tokens"], -1)),
    _mut("margin below range", _set(["profiles", 0, "margin_bps"], 9999)),
    _mut("margin above range", _set(["profiles", 0, "margin_bps"], 20001)),
    _mut("expected_calls zero", _set(["profiles", 0, "expected_calls"], 0)),
    _mut("max_calls 101", _set(["profiles", 0, "max_calls"], 101)),
    _mut("ttl zero", _set(["profiles", 0, "ttl_ms"], 0)),
    _mut("int64 overflow", _set(["limits", 0, "hard"], 2 ** 63)),
    _mut("soft above hard", _set(["limits", 1, "soft"], 100_000_001)),
    _mut("reserve below tariff", _set(["models", 0, "reserve_input_rate"], 0)),
    # unknown enums and value shapes
    _mut("unknown seat enforcement",
         _set(["seats", 0, "enforcement"], "enforcing")),
    _mut("unknown reservation mode",
         _set(["models", 0, "reservation_mode"], "lazy")),
    _mut("unknown limit scope", _set(["limits", 0, "scope"], "galaxy")),
    _mut("unknown metric", _set(["limits", 0, "metric"], "vibes")),
    _mut("unknown period", _set(["limits", 0, "period"], "decade")),
    _mut("list as scope", _set(["limits", 0, "scope"], ["platform"])),
    _mut("list as machine", _set(["seats", 0, "machine"], ["desktop"])),
    _mut("dict as account", _set(["models", 0, "account"], {"a": 1})),
    _mut("list as enforcement", _set(["seats", 0, "enforcement"], ["metered"])),
    _mut("window_key on a money limit",
         _set(["limits", 1, "window_key"], "five_hour")),
    _mut("window_bps without window_key",
         _set(["limits", 3, "window_key"], None)),
    _mut("window_bps above 10000", _set(["limits", 3, "hard"], 10001)),
    _mut("short policy hash", _set(["models", 0, "policy_hash"], "ab" * 31)),
    _mut("uppercase policy hash",
         _set(["models", 0, "policy_hash"], "AB" * 32)),
    _mut("bad role", _set(["seats", 0, "role"], "Main Role")),
    _mut("bad alias with newline", _set(["projects", 0, "alias"], "org\n")),
    _mut("effort_aliases as a string",
         _set(["models", 0, "effort_aliases"], "opus,fast")),
    _mut("effort_aliases empty", _set(["models", 0, "effort_aliases"], [])),
    _mut("effort alias with a comma",
         _set(["models", 0, "effort_aliases"], ["a,b"])),
    _mut("duplicate effort alias",
         _set(["models", 0, "effort_aliases"], ["opus", "opus"])),
    # the profile JSON fields
    _mut("non-empty context_rules",
         _set(["profiles", 0, "context_rules"], {"mode": "FRESH"})),
    _mut("non-empty calibration",
         _set(["profiles", 0, "calibration"], {"k": 1})),
    _mut("context_rules not an object",
         _set(["profiles", 0, "context_rules"], [])),
    _mut("empty candidates", _set(["profiles", 0, "candidates"], [])),
    _mut("only park", _set(["profiles", 0, "candidates"], ["park"])),
    _mut("park not last", _set(["profiles", 0, "candidates"],
                               ["park", "opus"])),
    _mut("park twice", _set(["profiles", 0, "candidates"],
                            ["opus", "park", "park"])),
    _mut("duplicate candidate",
         _set(["profiles", 0, "candidates"], ["opus", "opus"])),
    _mut("model aliased park", _set(["models", 1, "alias"], "park")),
    # duplicates and ambiguity
    _mut("ambiguous model alias across accounts",
         _extend("models", _model("other", "opus", ["opus"]))),
    _mut("duplicate model alias", _extend("models",
                                          _model("meter", "opus", ["opus"]))),
    _mut("duplicate project", _extend("projects", {"alias": "org"})),
    _mut("duplicate team", _extend("teams", {"project": "org",
                                             "alias": "dev"})),
    _mut("duplicate project_account",
         _extend("project_accounts", {"project": "org", "account": "meter"})),
    _mut("duplicate seat on a machine", _extend("seats", {
        "team": {"project": "org", "team": "dev"}, "machine": "desktop",
        "alias": "s1", "role": "x", "enforcement": "shadow"})),
    _mut("duplicate profile task_class",
         lambda d: d["profiles"].append(copy.deepcopy(d["profiles"][0]))),
    _mut("duplicate limit",
         lambda d: d["limits"].append(copy.deepcopy(d["limits"][0]))),
    # inventory rules still apply under version 2
    _mut("shares above budget",
         _set(["gateways", 0, "share_micro_eur"], 100_000_001)),
    _mut("gateway of unknown machine",
         _set(["gateways", 0, "machine"], "nope")),
]


class PolicyImportRollback(_Base):
    """policy_import_rollback: each bad document leaves the store unchanged
    and exits 2 with the static error."""

    def test_every_bad_document_changes_nothing(self):
        self.import_ok(base_doc())
        for label, mutation in MUTATIONS:
            doc = base_doc()
            mutation(doc)
            self.assert_refused(doc, label=label)

    def test_a_bad_document_on_an_empty_store_changes_nothing(self):
        doc = base_doc()
        doc["models"].append(_model("other", "opus", ["opus"]))
        self.assert_refused(doc)
        self.assertEqual(self.dump(POLICY_TABLES + INVENTORY_TABLES),
                         {t: [] for t in POLICY_TABLES + INVENTORY_TABLES})

    def test_unknown_or_newer_version_is_schema_unsupported(self):
        for version in (0, 3, 99, "2", 2.0, True, None):
            doc = base_doc()
            doc["version"] = version
            self.assert_refused(doc, ERROR_SCHEMA_UNSUPPORTED,
                                label=repr(version))

    def test_a_failure_during_apply_rolls_the_inventory_back_too(self):
        # Valid shape, but the apply refuses (a referenced seat is
        # removed): the new account added by the same document must not
        # survive.
        self.import_ok(base_doc())
        self.grant_one_lease()
        doc = base_doc()
        doc["accounts"].append({"alias": "fresh", "vendor": "ovh",
                                "kind": "subscription",
                                "budget_micro_eur": None})
        doc["machines"].append({"alias": "fresh-box"})
        doc["seats"] = [s for s in doc["seats"] if s["alias"] != "s1"]
        doc["limits"] = [lim for lim in doc["limits"]
                         if lim.get("seat") != "s1"]
        self.assert_refused(doc)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM accounts"), [(2,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM machines"), [(2,)])

    def test_malformed_files_are_refused(self):
        for text in ("", "{", "[]", '{"version": 2', "null",
                     '{"version":2,"version":2}'):
            path = os.path.join(self.tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            before = self.dump()
            result = run_cli("--db", self.db, "config-import", "--file", path)
            self.assertEqual(result.returncode, 2, text)
            self.assertEqual(self.dump(), before)

    def grant_one_lease(self):
        _grant(self, "s1", "g1")


# --- helpers shared by the grant-level rows ------------------------------

def _grant_request(ids, seat, gateway, now_ms, turn=None):
    return {
        "request_id": str(uuid.uuid4()),
        "request_hash": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        "turn_id": turn or str(uuid.uuid4()),
        "seat_id": ids["seats"][seat],
        "model_id": ids["models"]["opus"],
        "task_class": "bounded_read",
        "effort": "opus",
        "gateway_id": ids["gateways"][gateway],
        "parent_lease_id": None,
        "context": {"mode": "FRESH", "initial_tokens": 1000,
                    "dispatch_tokens": 0, "source": "tokenizer",
                    "observed_at": now_ms - 1000,
                    "growth_tokens_per_call": 0},
        "expected_calls": 1, "max_calls": 2, "output_cap": 1000,
        "per_call_output_cap": 500, "ttl_ms": 1_800_000,
    }


def _ids(case):
    return {"seats": case.ids("seats"), "models": case.ids("models"),
            "gateways": case.ids("gateways")}


def _grant(case, seat, gateway, turn=None, now_ms=None):
    now_ms = now_ms or int(time.time() * 1000)
    return lease.grant(case.db, _grant_request(_ids(case), seat, gateway,
                                               now_ms, turn), now_ms)


def _sized_snapshot(case, sized_ready=True):
    """A gateway snapshot (the existing reading-import path) reporting
    sized_ready - telemetry the R-003 fix must not act on."""
    now = int(time.time() * 1000)
    doc = {"version": 2, "readings": [{
        "event_id": str(uuid.uuid4()), "machine": "desktop",
        "account": "meter", "kind": "gateway_snapshot",
        "observed_at": now - 1000,
        "payload": {"gateway": "g1", "ledger_epoch": 1, "seq": 1,
                    "period_start": None, "period_end": None,
                    "policy_hashes": None, "sized_ready": sized_ready,
                    "reconciliation_required": False}}]}
    path = os.path.join(case.tmp, "snapshot.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)
    result = run_cli("--db", case.db, "reading-import", "--file", path)
    case.assertEqual(result.returncode, 0, result.stderr)


class PolicyImportReferencedRemoval(_Base):
    """policy_import_referenced_removal: a referenced row cannot be
    removed; an unreferenced one is deleted."""

    def test_removing_a_referenced_seat_refuses_the_import(self):
        self.import_ok(base_doc())
        _grant(self, "s1", "g1")
        doc = base_doc()
        doc["seats"] = [s for s in doc["seats"] if s["alias"] != "s1"]
        doc["limits"] = [lim for lim in doc["limits"]
                         if lim.get("seat") != "s1"]
        self.assert_refused(doc)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM seats"), [(3,)])

    def test_removing_a_referenced_model_refuses_the_import(self):
        self.import_ok(base_doc())
        _grant(self, "s1", "g1")
        doc = base_doc()
        doc["models"] = [m for m in doc["models"] if m["alias"] != "opus"]
        doc["profiles"][0]["candidates"] = ["fast", "park"]
        self.assert_refused(doc)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM models"), [(2,)])

    def test_removing_a_model_a_block_points_at_refuses(self):
        self.import_ok(base_doc())
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO blocks(id, account_id, model_id, reason, retry_at, "
            "state, source_event_id, updated_at) "
            "SELECT ?, account_id, id, 'overrun', NULL, 'parked', NULL, 1 "
            "FROM models WHERE alias='fast'", (str(uuid.uuid4()),))
        conn.commit()
        conn.close()
        doc = base_doc()
        doc["models"] = [m for m in doc["models"] if m["alias"] != "fast"]
        doc["profiles"][0]["candidates"] = ["opus", "park"]
        self.assert_refused(doc)

    def test_removing_a_limit_a_hold_points_at_refuses(self):
        self.import_ok(base_doc())
        _sized_snapshot(self)
        # Holds exist only for enforcing leases, which no grant produces
        # until card R1's gate; the gate is patched for this one setup.
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            granted = _grant(self, "s3", "g1")
        self.assertEqual(granted["enforcement"], "metered")
        doc = base_doc()
        doc["limits"] = doc["limits"][1:]      # drops the platform limit
        self.assert_refused(doc)

    def test_removing_unreferenced_rows_deletes_them(self):
        self.import_ok(base_doc())
        seat_ids = self.ids("seats")
        model_ids = self.ids("models")
        doc = base_doc()
        doc["seats"] = [s for s in doc["seats"] if s["alias"] != "s2"]
        doc["models"] = [m for m in doc["models"] if m["alias"] != "fast"]
        doc["profiles"][0]["candidates"] = ["opus", "park"]
        doc["limits"] = doc["limits"][:3]
        doc["project_accounts"] = []
        self.import_ok(doc)
        self.assertEqual(self.rows("SELECT alias FROM seats ORDER BY alias"),
                         [("s1",), ("s3",)])
        self.assertEqual(self.rows("SELECT alias FROM models"), [("opus",)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM limits"), [(3,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM project_accounts"),
                         [(0,)])
        self.assertEqual(self.ids("seats")["s1"], seat_ids["s1"])
        self.assertEqual(self.ids("models")["opus"], model_ids["opus"])
        # And the whole policy can be emptied again.
        for key in ("profiles", "limits", "seats", "teams", "models",
                    "projects"):
            doc[key] = []
        self.import_ok(doc)
        self.assertEqual(self.dump(POLICY_TABLES),
                         {t: [] for t in POLICY_TABLES})

    def test_removing_an_account_removes_its_models_first(self):
        # An account the document drops takes its (unreferenced) models
        # and project links with it in one import.
        doc = base_doc()
        doc["models"].append(_model("other", "side", ["side"]))
        doc["project_accounts"].append({"project": "org", "account": "other"})
        self.import_ok(doc)
        doc = base_doc()
        doc["accounts"] = [a for a in doc["accounts"] if a["alias"] != "other"]
        self.import_ok(doc)
        self.assertEqual(self.rows("SELECT alias FROM accounts"),
                         [("meter",)])
        self.assertEqual(self.rows("SELECT alias FROM models ORDER BY alias"),
                         [("fast",), ("opus",)])

    def test_removing_a_machine_with_a_referenced_seat_refuses(self):
        self.import_ok(base_doc())
        _grant(self, "s1", "g1")
        doc = base_doc()
        doc["machines"] = [{"alias": "worker"}]
        doc["gateways"] = []
        doc["seats"] = [s for s in doc["seats"] if s["machine"] != "desktop"]
        doc["limits"] = [lim for lim in doc["limits"]
                         if lim.get("machine") != "desktop"]
        self.assert_refused(doc)


class Version1StillImports(_Base):
    """version1_still_imports: a version-1 document imports exactly as
    before and leaves the seven policy tables untouched."""

    V1 = {
        "version": 1,
        "accounts": [{"alias": "meter", "vendor": "ovh", "kind": "metered",
                      "budget_micro_eur": 100_000_000}],
        "machines": [{"alias": "desktop"}, {"alias": "worker"}],
        "gateways": [{"alias": "g1", "account": "meter",
                      "machine": "desktop", "share_micro_eur": 100_000_000,
                      "period": "utc_month"}],
    }

    def test_v1_on_a_fresh_store_leaves_policy_empty(self):
        self.assertEqual(self.import_doc(copy.deepcopy(self.V1)).returncode, 0)
        self.assertEqual(self.dump(POLICY_TABLES),
                         {t: [] for t in POLICY_TABLES})
        self.assertEqual(self.rows("SELECT COUNT(*) FROM accounts"), [(1,)])

    def test_v1_after_v2_keeps_the_policy_tables_byte_for_byte(self):
        self.import_ok(base_doc())
        before = self.dump(POLICY_TABLES)
        v1 = copy.deepcopy(self.V1)
        v1["accounts"].append({"alias": "other", "vendor": "ovh",
                               "kind": "subscription",
                               "budget_micro_eur": None})
        self.assertEqual(self.import_doc(v1).returncode, 0)
        self.assertEqual(self.dump(POLICY_TABLES), before)

    def test_v1_still_refuses_to_strand_a_policy_row(self):
        self.import_ok(base_doc())
        v1 = copy.deepcopy(self.V1)
        v1["machines"] = [{"alias": "desktop"}]     # drops 'worker' (seat s2)
        self.assert_refused(v1)

    def test_v1_extra_policy_keys_are_still_refused(self):
        v1 = copy.deepcopy(self.V1)
        v1["seats"] = []
        self.assert_refused(v1)

    def test_a_document_without_a_version_is_refused_as_before(self):
        doc = copy.deepcopy(self.V1)
        del doc["version"]
        self.assert_refused(doc)


class GrantAfterImport(_Base):
    """grant_after_import: a lease is granted using only imported rows."""

    def test_grant_uses_imported_rows_and_limits(self):
        self.import_ok(base_doc())
        granted = _grant(self, "s1", "g1")
        self.assertEqual(granted["state"], "active")
        self.assertEqual(granted["enforcement"], "metered_advisory")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM leases"), [(1,)])
        # An imported platform limit (carrying the platform id) is read by
        # admission: one live lease exists, so a platform concurrency cap
        # of 1 denies the next turn; raising it through a re-import admits.
        doc = base_doc()
        doc["limits"].append(
            {"scope": "platform", "metric": "concurrency", "period": "turn",
             "window_key": None, "soft": None, "hard": 1})
        self.import_ok(doc)
        with self.assertRaises(QuotaError) as caught:
            _grant(self, "s1", "g1")
        self.assertEqual(caught.exception.code, errors.CODE_CONCURRENCY_LIMIT)
        doc["limits"][-1]["hard"] = 5
        self.import_ok(doc)
        self.assertEqual(_grant(self, "s1", "g1")["state"], "active")
    def test_grant_needs_the_imported_project_account_link(self):
        doc = base_doc()
        doc["project_accounts"] = []
        self.import_ok(doc)
        with self.assertRaises(QuotaError) as caught:
            _grant(self, "s1", "g1")
        self.assertEqual(caught.exception.code, errors.CODE_POLICY_DENIED)

    def test_a_shadow_seat_on_another_machine_is_granted_too(self):
        self.import_ok(base_doc())
        # s2 (shadow) lives on 'worker'; its gateway must be worker's, and
        # the metered account has none, so a grant without a gateway.
        request = _grant_request(_ids(self), "s2", "g1",
                                 int(time.time() * 1000))
        request["gateway_id"] = None
        granted = lease.grant(self.db, request, int(time.time() * 1000))
        self.assertEqual(granted["enforcement"], "shadow")

    def test_imported_models_carry_the_efforts_grant_reads(self):
        doc = base_doc()
        doc["models"][0]["effort_aliases"] = ["opus", "deep"]
        self.import_ok(doc)
        now = int(time.time() * 1000)
        request = _grant_request(_ids(self), "s1", "g1", now)
        request["effort"] = "deep"
        self.assertEqual(lease.grant(self.db, request, now)["state"],
                         "active")
        request = _grant_request(_ids(self), "s1", "g1", now)
        request["effort"] = "fast"
        with self.assertRaises(QuotaError):
            lease.grant(self.db, request, now)


class R003MeteredNeverEnforcing(_Base):
    """r003_metered_never_enforcing: a gateway's own sized_ready report can
    no longer switch a metered seat to enforcing metered leases."""

    def test_sized_ready_snapshot_leaves_a_metered_seat_advisory(self):
        self.import_ok(base_doc())
        _sized_snapshot(self, sized_ready=True)
        # The report is still stored as telemetry ...
        self.assertEqual(self.rows("SELECT sized_ready FROM gateways"),
                         [(1,)])
        granted = _grant(self, "s3", "g1")
        self.assertEqual(granted["enforcement"], "metered_advisory")
        self.assertIsNone(granted["money_hold"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM holds"), [(0,)])
        self.assertEqual(self.rows("SELECT token_hash, money_hold, "
                                   "enforcement FROM leases"),
                         [(None, None, "metered_advisory")])

    def test_the_gate_is_the_only_switch(self):
        # Positive control: with the interim gate open (card R1's future
        # job) and sized_ready true the old enforcing behaviour returns -
        # so the test above fails exactly because of the gate.
        self.import_ok(base_doc())
        _sized_snapshot(self, sized_ready=True)
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            granted = _grant(self, "s3", "g1")
            self.assertEqual(granted["enforcement"], "metered")
            self.assertIsNotNone(granted["money_hold"])
        self.assertFalse(lease.METERED_ENFORCEMENT_ACTIVATED)

    def test_gate_open_but_not_sized_ready_is_still_advisory(self):
        self.import_ok(base_doc())
        _sized_snapshot(self, sized_ready=False)
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            granted = _grant(self, "s3", "g1")
        self.assertEqual(granted["enforcement"], "metered_advisory")

    def test_http_grant_has_no_token_and_cannot_check(self):
        self.import_ok(base_doc())
        _sized_snapshot(self, sized_ready=True)
        self.assertEqual(run_cli(
            "--db", self.db, "identity-set", "--machine", "desktop",
            "--identity-ref", "identity-desktop", "--enabled",
            "true").returncode, 0)
        server = http_api.make_server(self.db, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%d" % server.server_address[1]
            ids = _ids(self)
            now = int(time.time() * 1000)
            body = {
                "request_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()),
                "seat_id": ids["seats"]["s3"], "task_class": "bounded_read",
                "model_id": ids["models"]["opus"], "effort": "opus",
                "gateway_id": ids["gateways"]["g1"], "parent_lease_id": None,
                "context": {"mode": "FRESH", "initial_tokens": 1000,
                            "dispatch_tokens": 0, "source": "tokenizer",
                            "observed_at": now - 1000,
                            "growth_tokens_per_call": 0},
                "expected_calls": 1, "max_calls": 2, "output_cap": 1000,
                "per_call_output_cap": 500, "ttl_ms": 1_800_000}
            status, granted = _call(base, "/v1/leases", body,
                                    body["request_id"])
            self.assertEqual(status, 201, granted)
            self.assertEqual(granted["enforcement"], "metered_advisory")
            self.assertIsNone(granted["money_hold"])
            self.assertIsNone(granted["lease_token"])
            status, err = _call(
                base, "/v1/leases/%s/check" % granted["lease_id"],
                {"request_id": str(uuid.uuid4())}, None)
            self.assertEqual(status, 403)
            self.assertEqual(err["error"]["code"], "FORBIDDEN")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def _call(base, path, body, idempotency_key):
    headers = {http_api.IDENTITY_HEADER: "identity-desktop",
               "Content-Type": "application/json"}
    if idempotency_key is not None:
        headers[http_api.IDEMPOTENCY_HEADER] = idempotency_key
    data = json.dumps(body).encode("utf-8")
    headers["Content-Length"] = str(len(data))
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class PrivacyL1a(_Base):
    """privacy_l1a: sentinel secrets and paths in a rejected document never
    appear in any output or error."""

    SENTINELS = ("SENTINEL-SECRET-9f2c", "C:\\Users\\sentinel\\secret.txt",
                 "/etc/sentinel-passwd", "sk-sentinel-key-0001")

    def poison(self, doc, sentinel):
        doc["projects"][0]["alias"] = sentinel               # bad alias
        doc["seats"][0]["role"] = sentinel
        doc["seats"][0]["enforcement"] = sentinel
        doc["models"][0]["policy_hash"] = sentinel
        doc["models"][0]["effort_aliases"] = [sentinel]
        doc["profiles"][0]["candidates"] = [sentinel]
        doc["profiles"][0]["context_rules"] = {sentinel: sentinel}
        doc["limits"][0]["scope"] = sentinel
        doc["limits"][1][sentinel] = sentinel
        doc[sentinel] = sentinel

    def test_no_sentinel_in_any_output_or_error(self):
        self.import_ok(base_doc())
        before = self.dump()
        for sentinel in self.SENTINELS:
            doc = base_doc()
            self.poison(doc, sentinel)
            result = self.import_doc(doc)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr.strip(), ERROR_INVALID_CONFIG)
            self.assertEqual(result.stdout, "")
            self.assertNotIn(sentinel, result.stdout + result.stderr)
            self.assertEqual(self.dump(), before)
        # one poisoned value at a time too, so each slot is covered alone
        for sentinel in self.SENTINELS:
            for mutation in (
                    _set(["projects", 0, "alias"], sentinel),
                    _set(["seats", 0, "team", "team"], sentinel),
                    _set(["models", 0, "account"], sentinel),
                    _set(["limits", 2, "seat"], sentinel),
                    _set(["limits", 0, "metric"], sentinel),
                    _set(["version"], sentinel),
                    _set([sentinel], 1)):
                doc = base_doc()
                mutation(doc)
                result = self.import_doc(doc)
                self.assertIn(result.returncode, (2,))
                self.assertNotIn(sentinel, result.stdout + result.stderr)

    def test_sentinel_values_never_reach_the_store(self):
        self.import_ok(base_doc())
        doc = base_doc()
        self.poison(doc, self.SENTINELS[0])
        self.import_doc(doc)
        conn = sqlite3.connect(self.db)
        try:
            for (table,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"):
                for row in conn.execute("SELECT * FROM " + table):
                    self.assertNotIn(self.SENTINELS[0], repr(row))
        finally:
            conn.close()

    def test_a_missing_file_path_is_not_echoed(self):
        missing = os.path.join(self.tmp, "SENTINEL-DIR-77", "nope.json")
        result = run_cli("--db", self.db, "config-import", "--file", missing)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("SENTINEL-DIR-77", result.stdout + result.stderr)

    def test_the_new_modules_do_no_file_or_environment_access(self):
        for name in ("validate_config_v2.py", "policy_config.py"):
            path = os.path.join(REPO_ROOT, "quota_balancer", name)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            for needle in ("import os", "open(", "os.environ", "getenv",
                           "import socket", "print(", "subprocess"):
                self.assertFalse(needle in source, (name, needle))


if __name__ == "__main__":
    unittest.main()
