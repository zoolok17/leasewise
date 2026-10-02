"""Card L1c: honest unknown usage for shadow leases (migration v9) and the
`policy-ids` lookup (docs/STEP-CARD-L1C.md).

Rows: shadow_receipt_null_tokens, non_shadow_requires_tokens, migration_v9,
admission_ignores_unknown_shadow_usage, policy_ids_projection, privacy_l1c.
Stores are built through config-import v2 (the migration rows excepted: a
version-8 store has to be built by hand). Synthetic data only.
"""

import copy
import json
import os
import sqlite3
import time
import unittest
import uuid
from unittest import mock

from quota_balancer import errors, http_api, lease, migrate
from quota_balancer.errors import QuotaError

from tests.test_cardl1a import _Base, _ids, run_cli
from tests.test_cardl1b import _L1b, doc, unknown_context
from tests.test_cardl1a_t2 import request

ERROR_INVALID_REQUEST = ('{"error":{"code":"INVALID_REQUEST","message":"the '
                         'request is invalid"}}')
TOKENS = ("input_tokens", "cached_input_tokens", "output_tokens")


class _L1c(_L1b):
    def shadow_lease(self, now_ms=None):
        body, now = self.unknown(now_ms=now_ms)
        return self.grant(body, now), now

    def settle(self, lease_id, final, tokens=None, **kw):
        receipt = self.receipt(lease_id, final, **kw)
        receipt.update(tokens or {})
        return lease.settle(self.db, receipt, receipt["received_at"]), receipt

    def stored(self, event_id):
        return self.rows(
            "SELECT input_tokens, cached_input_tokens, output_tokens, calls, "
            "final FROM receipts WHERE event_id=?", (event_id,))[0]


class ShadowReceiptNullTokens(_L1c):
    """shadow_receipt_null_tokens: a shadow receipt may say 'unknown'."""

    NULLS = {"input_tokens": None, "cached_input_tokens": None,
             "output_tokens": None}

    def test_null_tokens_are_accepted_and_stored_as_null(self):
        made, now = self.shadow_lease()
        answer, receipt = self.settle(made["lease_id"], False, self.NULLS)
        self.assertEqual(answer, {"state": "active", "released_micro_eur": None,
                                  "retained_micro_eur": None,
                                  "replayed": False})
        self.assertEqual(self.stored(receipt["event_id"]), (None, None, None, 1, 0))
        answer, receipt = self.settle(made["lease_id"], True, self.NULLS)
        self.assertEqual(answer["state"], "settled")
        self.assertEqual(self.stored(receipt["event_id"]), (None, None, None, 1, 1))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM receipts WHERE "
                                   "input_tokens IS NULL"), [(2,)])

    def test_each_token_field_is_independent(self):
        made, now = self.shadow_lease()
        for tokens in ({"input_tokens": None},
                       {"cached_input_tokens": None},
                       {"output_tokens": None},
                       {"input_tokens": 12, "cached_input_tokens": None,
                        "output_tokens": 0}):
            answer, receipt = self.settle(made["lease_id"], False, tokens)
            expected = tuple(
                tokens.get(f, self.receipt(made["lease_id"], False)[f])
                for f in TOKENS)
            self.assertEqual(self.stored(receipt["event_id"])[:3], expected)

    def test_integers_are_still_accepted_and_zero_is_not_null(self):
        made, now = self.shadow_lease()
        answer, receipt = self.settle(
            made["lease_id"], False,
            {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0})
        self.assertEqual(self.stored(receipt["event_id"])[:3], (0, 0, 0))

    def test_a_replay_is_a_noop_and_a_changed_replay_conflicts(self):
        made, now = self.shadow_lease()
        answer, receipt = self.settle(made["lease_id"], False, self.NULLS)
        before = self.rows("SELECT * FROM receipts")
        again = lease.settle(self.db, dict(receipt, received_at=now + 9),
                             now + 9)
        self.assertTrue(again["replayed"])
        self.assertIsNone(again["released_micro_eur"])
        self.assertEqual(self.rows("SELECT * FROM receipts"), before)
        # unknown is not zero: the same event with 0 is a different report
        with self.assertRaises(QuotaError) as caught:
            lease.settle(self.db, dict(receipt, input_tokens=0), now)
        self.assertEqual(caught.exception.code,
                         errors.CODE_IDEMPOTENCY_CONFLICT)
        with self.assertRaises(QuotaError):
            lease.settle(self.db, dict(receipt, input_tokens=5), now)

    def test_calls_and_other_fields_stay_required_integers(self):
        made, now = self.shadow_lease()
        for field, value in (("calls", None), ("calls", -1), ("calls", True),
                             ("calls", 1.5), ("input_tokens", -1),
                             ("input_tokens", True), ("output_tokens", 1.5),
                             ("cached_input_tokens", "7"),
                             ("output_tokens", 2 ** 63)):
            receipt = self.receipt(made["lease_id"], False)
            receipt[field] = value
            with self.assertRaises(QuotaError) as caught:
                lease.settle(self.db, receipt, now)
            self.assertEqual(caught.exception.code,
                             errors.CODE_INVALID_REQUEST, (field, value))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM receipts"), [(0,)])

    def test_the_key_must_still_be_present(self):
        made, now = self.shadow_lease()
        receipt = self.receipt(made["lease_id"], False)
        del receipt["input_tokens"]
        with self.assertRaises(QuotaError) as caught:
            lease.settle(self.db, receipt, now)
        self.assertEqual(caught.exception.code, errors.CODE_INVALID_REQUEST)

    def test_a_late_final_with_unknown_tokens_settles_an_expired_lease(self):
        made, now = self.shadow_lease()
        late = made["expires_at"] + 5
        answer, receipt = self.settle(made["lease_id"], True, self.NULLS,
                                      now_ms=late)
        self.assertEqual(answer["state"], "settled")

    def test_null_tokens_over_http(self):
        self.assertEqual(run_cli(
            "--db", self.db, "identity-set", "--machine", "desktop",
            "--identity-ref", "identity-desktop", "--enabled",
            "true").returncode, 0)
        import threading
        server = http_api.make_server(self.db, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            from tests.test_cardl1a import _call
            base = "http://127.0.0.1:%d" % server.server_address[1]
            now = self.now()
            wire = request(_ids(self), seat="s6", now_ms=now, gateway_id=None)
            del wire["request_hash"]
            wire["context"] = unknown_context(now)
            status, made = _call(base, "/v1/leases", wire, wire["request_id"])
            self.assertEqual(status, 201, made)
            path = "/v1/leases/%s/settle" % made["lease_id"]
            body = {"event_id": str(uuid.uuid4()), "outcome": "failed",
                    "calls": 1, "input_tokens": None,
                    "cached_input_tokens": None, "output_tokens": None,
                    "actual_micro_eur": None, "gateway_id": None, "seq": 1,
                    "final": False}
            status, answer = _call(base, path, body, body["event_id"])
            self.assertEqual(status, 200, answer)
            self.assertIsNone(answer["released_micro_eur"])
            status, replay = _call(base, path, body, body["event_id"])
            self.assertEqual(status, 200)
            final = dict(body, event_id=str(uuid.uuid4()), final=True)
            status, answer = _call(base, path, final, final["event_id"])
            self.assertEqual((status, answer["state"]), (200, "settled"))
            self.assertEqual(self.rows(
                "SELECT COUNT(*) FROM receipts WHERE output_tokens IS NULL"),
                [(2,)])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class NonShadowRequiresTokens(_L1c):
    """non_shadow_requires_tokens: for every other enforcement the three
    fields stay required non-negative integers."""

    def leases(self):
        _sized = None
        out = {}
        body, now = self.known("s1")                       # metered_advisory
        out["advisory"] = (self.grant(body, now), now)
        body, now = self.known("s4")                       # subscription_admission
        out["subscription_admission"] = (self.grant(body, now), now)
        body, now = self.known("s5")                       # subscription_warn
        out["subscription_warn"] = (self.grant(body, now), now)
        from tests.test_cardl1a import _sized_snapshot
        _sized_snapshot(self)
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            body, now = self.known("s3")                   # enforcing metered
            out["metered"] = (self.grant(body, now), now)
        return out

    def test_null_is_refused_for_every_non_shadow_enforcement(self):
        gateway = self.ids("gateways")["g1"]
        for kind, (made, now) in self.leases().items():
            for field in TOKENS:
                for final in (False, True):
                    receipt = self.receipt(
                        made["lease_id"], final, actual=5 if final else None,
                        gateway=gateway if kind in ("advisory", "metered") else None)
                    receipt[field] = None
                    before = self.rows("SELECT COUNT(*) FROM receipts")
                    with self.assertRaises(QuotaError) as caught:
                        lease.settle(self.db, receipt, now)
                    self.assertEqual(caught.exception.code,
                                     errors.CODE_INVALID_REQUEST,
                                     (kind, field, final))
                    self.assertEqual(self.rows("SELECT COUNT(*) FROM receipts"),
                                     before)
            self.assertEqual(self.rows("SELECT state FROM leases WHERE id=?",
                                       (made["lease_id"],)), [("active",)])

    def test_integers_still_settle_those_leases(self):
        for kind, (made, now) in self.leases().items():
            receipt = self.receipt(
                made["lease_id"], True,
                actual=5 if kind == "metered" else None,
                gateway=self.ids("gateways")["g1"]
                if kind in ("advisory", "metered") else None)
            self.assertEqual(lease.settle(self.db, receipt, now)["state"],
                             "settled", kind)

    def test_the_refusal_is_static_over_http(self):
        self.assertEqual(run_cli(
            "--db", self.db, "identity-set", "--machine", "desktop",
            "--identity-ref", "identity-desktop", "--enabled",
            "true").returncode, 0)
        body, now = self.known("s1")
        made = self.grant(body, now)
        wire = {"event_id": str(uuid.uuid4()), "outcome": "completed",
                "calls": 1, "input_tokens": None, "cached_input_tokens": 0,
                "output_tokens": 0, "actual_micro_eur": None,
                "gateway_id": None, "seq": 1, "final": False}
        identity = (self.ids("machines")["desktop"], "desktop")
        with self.assertRaises(QuotaError) as caught:
            http_api.handle_settle(self.db, identity, made["lease_id"], wire,
                                   now, wire["event_id"])
        self.assertEqual(caught.exception.code, errors.CODE_INVALID_REQUEST)


class AdmissionIgnoresUnknownShadowUsage(_L1c):
    """admission_ignores_unknown_shadow_usage: shadow receipts never count
    toward a limit (known or unknown), and a NULL never poisons a sum."""

    HARD = 1000

    def setUp(self):
        super().setUp()
        d = doc()
        d["limits"] = [{"scope": "platform", "metric": "input_tokens",
                        "period": "month", "window_key": None, "soft": None,
                        "hard": self.HARD}]
        self.import_ok(d)

    def subscription_final(self, input_tokens):
        body, now = self.known("s4")
        made = self.grant(body, now)
        self.settle(made["lease_id"], True, {"input_tokens": input_tokens})

    def next_grant_ok(self):
        body, now = self.known("s4")
        self.assertEqual(self.grant(body, now)["state"], "active")

    def next_grant_denied(self):
        body, now = self.known("s4")
        with self.assertRaises(QuotaError) as caught:
            self.grant(body, now)
        self.assertEqual(caught.exception.code, errors.CODE_INPUT_LIMIT)

    def test_unknown_and_known_shadow_tokens_do_not_count(self):
        self.subscription_final(self.HARD)           # exactly at the limit
        for tokens in ({"input_tokens": None, "cached_input_tokens": None,
                        "output_tokens": None},
                       {"input_tokens": 5_000_000}):
            made, now = self.shadow_lease()
            self.settle(made["lease_id"], True, tokens)
        self.assertEqual(self.rows(
            "SELECT COUNT(*) FROM receipts WHERE final=1"), [(3,)])
        self.next_grant_ok()                         # still within the limit

    def test_the_limit_is_still_enforced_exactly(self):
        self.subscription_final(self.HARD)
        made, now = self.shadow_lease()
        self.settle(made["lease_id"], True, {"input_tokens": None})
        self.next_grant_ok()
        self.subscription_final(1)                   # one token over
        self.next_grant_denied()

    def test_only_unknown_receipts_leave_the_usage_at_zero_not_null(self):
        made, now = self.shadow_lease()
        self.settle(made["lease_id"], True,
                    {"input_tokens": None, "cached_input_tokens": None,
                     "output_tokens": None})
        self.next_grant_ok()
        self.subscription_final(self.HARD + 1)
        self.next_grant_denied()

    def test_a_shadow_money_figure_never_counts_toward_the_budget(self):
        # the account budget sums receipt money; shadow leases are exempt
        made, now = self.shadow_lease()
        receipt = self.receipt(made["lease_id"], True, actual=10 ** 12)
        receipt["input_tokens"] = receipt["cached_input_tokens"] = None
        receipt["output_tokens"] = None
        lease.settle(self.db, receipt, now)
        from tests.test_cardl1a import _sized_snapshot
        _sized_snapshot(self)
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            body, now = self.known("s3")
            self.assertEqual(self.grant(body, now)["state"], "active")


class MigrationV9(_Base):
    """migration_v9: receipts is rebuilt so the token columns allow NULL;
    nothing else changes; every interruption rolls back whole."""

    def build_v8(self, name):
        db = os.path.join(self.tmp, name)
        conn = sqlite3.connect(db)
        for script in (migrate.MIGRATION_V1, migrate.MIGRATION_V2,
                       migrate.MIGRATION_V3, migrate.MIGRATION_V4,
                       migrate.MIGRATION_V5, migrate.MIGRATION_V6,
                       migrate.MIGRATION_V7, migrate.MIGRATION_V8):
            conn.executescript(script)
        ids = {k: str(uuid.uuid4()) for k in (
            "acc", "mach", "gw", "proj", "team", "seat", "model", "lease1",
            "lease2")}
        conn.execute("INSERT INTO accounts(id, alias, vendor, kind, "
                     "budget_micro_eur) VALUES(?,?,?,?,?)",
                     (ids["acc"], "meter", "ovh", "metered", 100_000_000))
        conn.execute("INSERT INTO machines(id, alias) VALUES(?,?)",
                     (ids["mach"], "desktop"))
        conn.execute("INSERT INTO gateways(id, alias, account_id, machine_id,"
                     " share_micro_eur, period) VALUES(?,?,?,?,?,?)",
                     (ids["gw"], "g1", ids["acc"], ids["mach"], 100_000_000,
                      "utc_month"))
        conn.execute("INSERT INTO projects(id, alias) VALUES(?,?)",
                     (ids["proj"], "org"))
        conn.execute("INSERT INTO teams(id, project_id, alias) VALUES(?,?,?)",
                     (ids["team"], ids["proj"], "dev"))
        conn.execute("INSERT INTO seats(id, team_id, machine_id, alias, role,"
                     " enforcement) VALUES(?,?,?,?,?,?)",
                     (ids["seat"], ids["team"], ids["mach"], "s1", "main",
                      "metered"))
        conn.execute(
            "INSERT INTO models(id, account_id, alias, context_ceiling, "
            "output_ceiling, tariff_input_rate, tariff_output_rate, "
            "reserve_input_rate, reserve_output_rate, reservation_mode, "
            "policy_hash, effort_aliases) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (ids["model"], ids["acc"], "opus", 1000, 100, 1, 1, 1, 1,
             "sized", "ab" * 32, "opus"))
        for key, turn in (("lease1", "t1"), ("lease2", "t2")):
            conn.execute(
                "INSERT INTO leases(id, machine_id, seat_id, account_id, "
                "model_id, gateway_id, parent_lease_id, turn_id, request_id, "
                "request_hash, token_hash, task_class, context_mode, "
                "enforcement, estimate, policy_snapshot, input_cap, "
                "output_cap, max_calls, per_call_output_cap, money_hold, "
                "granted_at, expires_at, activity_seq, period_start, state, "
                "outcome) VALUES(?,?,?,?,?,?,NULL,?,?,?,NULL,'opus','FRESH',"
                "'metered','{}','{}',1,1,1,1,NULL,1,2,0,1,'active',NULL)",
                (ids[key], ids["mach"], ids["seat"], ids["acc"], ids["model"],
                 ids["gw"], turn, str(uuid.uuid4()), "h" * 64))
        rows = (
            (str(uuid.uuid4()), ids["lease1"], ids["gw"], 1, 1, 100, 5, 7, None,
             "completed", 0, 10, 11),
            (str(uuid.uuid4()), ids["lease1"], ids["gw"], 2, 2, 0, 0, 0, 6,
             "completed", 1, 12, 13),
            (str(uuid.uuid4()), ids["lease2"], None, None, 0, 0, 0, 0, None,
             None, 0, 14, 15),
        )
        for row in rows:
            conn.execute(
                "INSERT INTO receipts(event_id, lease_id, gateway_id, seq, "
                "calls, input_tokens, cached_input_tokens, output_tokens, "
                "actual_micro_eur, outcome, final, observed_at, received_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        conn.commit()
        conn.close()
        return db, rows

    def receipts_dump(self, db):
        conn = sqlite3.connect(db)
        try:
            return conn.execute(
                "SELECT event_id, lease_id, gateway_id, seq, calls, "
                "input_tokens, cached_input_tokens, output_tokens, "
                "actual_micro_eur, outcome, final, observed_at, received_at "
                "FROM receipts ORDER BY event_id").fetchall()
        finally:
            conn.close()

    def schema_of(self, db, name):
        conn = sqlite3.connect(db)
        try:
            return conn.execute("SELECT sql FROM sqlite_master WHERE "
                                "name=?", (name,)).fetchone()
        finally:
            conn.close()

    def test_the_version_is_nine_and_the_script_exists(self):
        self.assertEqual(migrate.SCHEMA_VERSION, 9)
        self.assertIn(9, migrate._STEPS)
        self.assertTrue(migrate.MIGRATION_V9.endswith("PRAGMA user_version=9;\n"))

    def test_a_v8_store_upgrades_and_keeps_every_row(self):
        db, rows = self.build_v8("up.db")
        before = self.receipts_dump(db)
        self.assertEqual(len(before), 3)
        old_index = self.schema_of(db, "receipt_final")
        result = run_cli("--db", db, "init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.receipts_dump(db), before)
        conn = sqlite3.connect(db)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(),
                             [])
            self.assertEqual(conn.execute(
                "PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()
        self.assertEqual(self.schema_of(db, "receipt_final"), old_index)
        # idempotent: a second init changes nothing
        again = run_cli("--db", db, "init")
        self.assertEqual(again.returncode, 0)
        self.assertEqual(self.receipts_dump(db), before)

    def test_the_new_table_allows_null_and_keeps_every_other_rule(self):
        db, rows = self.build_v8("rules.db")
        self.assertEqual(run_cli("--db", db, "init").returncode, 0)
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys=ON")
        lease_id = conn.execute("SELECT id FROM leases LIMIT 1").fetchone()[0]

        def insert(**kw):
            values = dict(event_id=str(uuid.uuid4()), lease_id=lease_id,
                          gateway_id=None, seq=None, calls=1,
                          input_tokens=None, cached_input_tokens=None,
                          output_tokens=None, actual_micro_eur=None,
                          outcome=None, final=0, observed_at=1, received_at=1)
            values.update(kw)
            conn.execute(
                "INSERT INTO receipts(event_id, lease_id, gateway_id, seq, "
                "calls, input_tokens, cached_input_tokens, output_tokens, "
                "actual_micro_eur, outcome, final, observed_at, received_at) "
                "VALUES(:event_id,:lease_id,:gateway_id,:seq,:calls,"
                ":input_tokens,:cached_input_tokens,:output_tokens,"
                ":actual_micro_eur,:outcome,:final,:observed_at,:received_at)",
                values)

        insert()                                           # all NULL: ok
        insert(input_tokens=0, output_tokens=7)
        for bad in (dict(input_tokens=-1), dict(cached_input_tokens=-1),
                    dict(output_tokens=-1), dict(calls=-1), dict(calls=None),
                    dict(final=2), dict(actual_micro_eur=-1),
                    dict(lease_id="no-such-lease"),
                    dict(gateway_id="no-such-gateway"),
                    dict(input_tokens="text")):
            with self.assertRaises(sqlite3.IntegrityError, msg=str(bad)):
                insert(**bad)
        # the final-receipt partial index still allows one final per lease
        other = conn.execute("SELECT id FROM leases WHERE id<>?",
                             (lease_id,)).fetchone()[0]
        insert(lease_id=other, final=1)
        with self.assertRaises(sqlite3.IntegrityError):
            insert(lease_id=other, final=1)
        insert(lease_id=other, final=0)
        conn.rollback()
        conn.close()
        sql = self.schema_of(db, "receipts")[0]
        self.assertIn("STRICT", sql)
        self.assertNotIn("input_tokens INTEGER NOT NULL", sql)

    def test_every_interruption_rolls_back_whole(self):
        statements = migrate._STEPS[9]
        self.assertGreaterEqual(len(statements), 4)
        from quota_balancer import store
        for stop in range(len(statements) + 1):
            db, rows = self.build_v8("rollback-%d.db" % stop)
            before = self.receipts_dump(db)
            old_sql = self.schema_of(db, "receipts")
            old_index = self.schema_of(db, "receipt_final")
            broken = tuple(statements[:stop]) + ("SELECT * FROM no_such_table",)
            with mock.patch.dict(migrate._STEPS, {9: broken}):
                with self.assertRaises(QuotaError) as caught:
                    store.init_db(db)
            self.assertEqual(caught.exception.code, errors.CODE_STORE_ERROR)
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(
                    conn.execute("PRAGMA user_version").fetchone()[0], 8, stop)
                names = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master")}
                self.assertNotIn("receipts_new", names, stop)
            finally:
                conn.close()
            self.assertEqual(self.receipts_dump(db), before, stop)
            self.assertEqual(self.schema_of(db, "receipts"), old_sql, stop)
            self.assertEqual(self.schema_of(db, "receipt_final"), old_index, stop)

    def test_a_newer_unknown_version_is_still_refused(self):
        db = os.path.join(self.tmp, "future.db")
        self.assertEqual(run_cli("--db", db, "init").returncode, 0)
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA user_version=10")
        conn.commit()
        conn.close()
        for verb in (("status", "--json"),):
            result = run_cli("--db", db, *verb)
            self.assertEqual(result.returncode, 2)
            self.assertIn("SCHEMA_UNSUPPORTED", result.stderr)
        result = run_cli("--db", db, "init")
        self.assertEqual(result.returncode, 2)

    def test_a_fresh_store_is_version_nine(self):
        self.assertEqual(self.rows(
            "SELECT 1 WHERE 1"), [(1,)])
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 9)
        finally:
            conn.close()


class PolicyIdsProjection(_L1c):
    """policy_ids_projection: a closed, sorted, read-only listing."""

    def ids_json(self):
        result = run_cli("--db", self.db, "policy-ids", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout, json.loads(result.stdout)

    def test_the_shape_is_closed_and_sorted(self):
        text, data = self.ids_json()
        self.assertEqual(set(data), {"seats", "models", "task_classes"})
        seat_ids = self.ids("seats")
        model_ids = self.ids("models")
        expected_seats = sorted(
            ({"machine": m, "team": t, "project": p, "seat": s,
              "id": seat_ids[s]} for m, t, p, s in (
                  ("desktop", "dev", "org", "s1"),
                  ("worker", "dev", "org", "s2"),
                  ("desktop", "dev", "org", "s3"),
                  ("worker", "dev", "org", "s4"),
                  ("worker", "dev", "org", "s5"),
                  ("desktop", "dev", "org", "s6"))),
            key=lambda e: (e["project"], e["team"], e["machine"], e["seat"]))
        self.assertEqual(data["seats"], expected_seats)
        self.assertEqual(data["models"], sorted(
            ({"account": a, "model": m, "id": model_ids[m]} for a, m in (
                ("meter", "opus"), ("meter", "fast"),
                ("other", "sub-model"))),
            key=lambda e: (e["account"], e["model"])))
        self.assertEqual(data["task_classes"], ["bounded_read"])
        for entry in data["seats"]:
            self.assertEqual(set(entry), {"machine", "team", "project",
                                          "seat", "id"})
        for entry in data["models"]:
            self.assertEqual(set(entry), {"account", "model", "id"})
        # compact, sorted-key, one line: byte-stable
        self.assertEqual(text.strip(), json.dumps(
            data, sort_keys=True, separators=(",", ":")))

    def test_it_is_stable_and_read_only(self):
        first, _ = self.ids_json()
        before = {t: self.rows("SELECT * FROM " + t) for t in (
            "seats", "models", "profiles", "machines", "accounts", "audit")}
        second, _ = self.ids_json()
        self.assertEqual(first, second)
        after = {t: self.rows("SELECT * FROM " + t) for t in before}
        self.assertEqual(before, after)

    def test_no_secret_or_external_value_is_listed(self):
        self.assertEqual(run_cli(
            "--db", self.db, "identity-set", "--machine", "desktop",
            "--identity-ref", "IDENTITY-REF-SENTINEL", "--enabled",
            "true").returncode, 0)
        text, data = self.ids_json()
        for needle in ("IDENTITY-REF-SENTINEL", "identity_ref", "ovh",
                       "vendor", "budget", "policy_hash", "ab" * 32,
                       self.tmp, "enforcement", "role", "100000000"):
            self.assertNotIn(needle, text)

    def test_ids_match_what_a_lease_request_needs(self):
        _, data = self.ids_json()
        seat = next(s for s in data["seats"] if s["seat"] == "s6")["id"]
        model = next(m for m in data["models"] if m["model"] == "opus")["id"]
        body, now = self.unknown()
        body["seat_id"], body["model_id"] = seat, model
        self.assertEqual(self.grant(body, now)["state"], "active")

    def test_an_empty_store_lists_nothing(self):
        db = os.path.join(self.tmp, "empty.db")
        self.assertEqual(run_cli("--db", db, "init").returncode, 0)
        result = run_cli("--db", db, "policy-ids", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {"models": [], "seats": [], "task_classes": []})

    def test_the_errors_are_static(self):
        missing = run_cli("--db", os.path.join(self.tmp, "SENTINEL-DIR",
                                               "x.db"), "policy-ids", "--json")
        self.assertEqual(missing.returncode, 3)
        self.assertEqual(missing.stdout, "")
        self.assertNotIn("SENTINEL-DIR", missing.stderr)
        self.assertIn("STORE_ERROR", missing.stderr)
        # the flag is required, as for status
        bare = run_cli("--db", self.db, "policy-ids")
        self.assertNotEqual(bare.returncode, 0)
        # an old store is refused, not read
        old = os.path.join(self.tmp, "old.db")
        conn = sqlite3.connect(old)
        for script in (migrate.MIGRATION_V1, migrate.MIGRATION_V2):
            conn.executescript(script)
        conn.close()
        refused = run_cli("--db", old, "policy-ids", "--json")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("SCHEMA_UNSUPPORTED", refused.stderr)

    def test_status_is_unchanged(self):
        result = run_cli("--db", self.db, "status", "--json")
        self.assertEqual(result.returncode, 0)
        data = json.loads(result.stdout)
        self.assertNotIn("seats", data)
        self.assertNotIn("models", data)


class PrivacyL1c(_L1c):
    """privacy_l1c: nothing from a rejected receipt is echoed or stored."""

    SENTINELS = ("SENTINEL-SECRET-9f2c", "C:\\Users\\sentinel\\secret.txt",
                 "/etc/sentinel-passwd", "sk-sentinel-key-0001")

    def test_sentinels_in_the_token_fields_are_not_echoed_or_stored(self):
        made, now = self.shadow_lease()
        for sentinel in self.SENTINELS:
            for field in TOKENS + ("calls", "outcome"):
                receipt = self.receipt(made["lease_id"], False)
                receipt[field] = sentinel
                with self.assertRaises(QuotaError) as caught:
                    lease.settle(self.db, receipt, now)
                text = caught.exception.message + repr(caught.exception)
                self.assertNotIn(sentinel, text)
        conn = sqlite3.connect(self.db)
        try:
            for (table,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"):
                for row in conn.execute("SELECT * FROM " + table):
                    for sentinel in self.SENTINELS:
                        self.assertNotIn(sentinel, repr(row))
        finally:
            conn.close()

    def test_the_non_shadow_refusal_is_static(self):
        body, now = self.known("s1")
        made = self.grant(body, now)
        receipt = self.receipt(made["lease_id"], False)
        receipt["input_tokens"] = None
        with self.assertRaises(QuotaError) as caught:
            lease.settle(self.db, receipt, now)
        self.assertEqual(caught.exception.message, errors.MSG_INVALID_REQUEST)

    def test_policy_ids_with_sentinel_aliases_prints_only_closed_text(self):
        # aliases are regex-checked, so a sentinel cannot be an alias; the
        # listing still carries nothing but aliases and ids
        result = run_cli("--db", self.db, "policy-ids", "--json")
        for sentinel in self.SENTINELS:
            self.assertNotIn(sentinel, result.stdout + result.stderr)
        self.assertNotIn("\\", result.stdout)


if __name__ == "__main__":
    unittest.main()
