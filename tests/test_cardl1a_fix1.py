"""Card L1a, fix round 1 (tk-80472a29a673): five findings of the cold read.

F1 an old enforcing lease must not pass /check while the gate is closed;
F2 an unchanged request must still replay a lease stored before L1a;
F3 removing a seat (or team, project) must not orphan logical references;
F4 referenced inventory removals are INVALID_CONFIG, never STORE_ERROR;
F5 a malformed stored profile is STORE_ERROR, not a policy denial.
All data is synthetic.
"""

import copy
import hashlib
import json
import sqlite3
import time
import unittest
import uuid
from unittest import mock

from quota_balancer import errors, http_api, lease, policy_config, store
from quota_balancer.errors import QuotaError

from tests.test_cardl1a import _ids, _sized_snapshot, base_doc, run_cli
from tests.test_cardl1a_t2 import request
from tests.test_cardl1a_t3 import ERROR_PARKED, _T3  # noqa: F401

ERROR_INVALID_CONFIG = ('{"error":{"code":"INVALID_CONFIG",'
                        '"message":"config document is invalid"}}')
ERROR_STORE = ('{"error":{"code":"STORE_ERROR",'
               '"message":"store operation failed"}}')


def doc():
    d = base_doc()
    d["limits"] = []
    return d


def legacy_hash(body):
    """The replay fingerprint exactly as the code before L1a computed it:
    every request field except request_hash, and (that code never saw it)
    task_class."""
    canonical = {k: v for k, v in body.items()
                 if k not in ("request_hash", "task_class")}
    return hashlib.sha256(json.dumps(
        canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class _Fix(_T3):
    def setUp(self):
        super().setUp()
        self.import_ok(doc())          # replaces the t3 document (no limits)

    def wire(self, seat, now_ms=None):
        now_ms = now_ms or self.now()
        gateway = _ids(self)["gateways"]["g1"] if seat in ("s1", "s3") else None
        body = request(_ids(self), seat=seat, now_ms=now_ms, gateway_id=gateway)
        del body["request_hash"]
        return body

    def identity(self):
        return (self.ids("machines")["desktop"], "desktop")

    def make_legacy(self, lease_id, body):
        """Rewrite a stored lease the way the code before L1a stored it: a
        fingerprint without task_class, the effort alias in the task_class
        column, no task_class / policy_revision in the policy snapshot."""
        snapshot = json.loads(self.rows(
            "SELECT policy_snapshot FROM leases WHERE id=?", (lease_id,))[0][0])
        snapshot.pop("task_class")
        snapshot.pop("policy_revision")
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE leases SET request_hash=?, task_class='opus', "
            "policy_snapshot=? WHERE id=?",
            (legacy_hash(body), json.dumps(snapshot, sort_keys=True), lease_id))
        conn.commit()
        conn.close()

    def receipt(self, lease_id, final, actual=None, gateway=None):
        now_ms = self.now()
        return {"event_id": str(uuid.uuid4()), "lease_id": lease_id,
                "outcome": "completed", "calls": 1, "input_tokens": 100,
                "cached_input_tokens": 0, "output_tokens": 10,
                "actual_micro_eur": actual, "gateway_id": gateway, "seq": 1,
                "final": 1 if final else 0, "observed_at": now_ms,
                "received_at": now_ms}

    def assert_unchanged_refusal(self, mutated, stderr=ERROR_INVALID_CONFIG):
        before = self.snapshot_all()
        result = self.import_doc(mutated)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stderr.strip(), stderr)
        self.assertEqual(self.snapshot_all(), before)

    def snapshot_all(self):
        conn = sqlite3.connect(self.db)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            return {t: sorted(conn.execute("SELECT * FROM " + t).fetchall(),
                              key=repr) for t in tables}
        finally:
            conn.close()


class F1OldEnforcingLeaseCannotCheck(_Fix):
    """While the gate is closed a stored `metered` lease is treated like a
    non-enforcing one: no /check, no token on replay; money untouched."""

    def make_old_enforcing_lease(self):
        _sized_snapshot(self)
        wire = self.wire("s3")
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            status, made = http_api.handle_grant(
                self.db, self.identity(), wire, self.now(), wire["request_id"])
        self.assertEqual(status, 201)
        self.assertEqual(made["enforcement"], "metered")
        self.assertIsNotNone(made["lease_token"])
        return wire, made

    def check_body(self):
        return {"request_id": str(uuid.uuid4())}

    def test_check_is_forbidden_while_the_gate_is_closed(self):
        wire, made = self.make_old_enforcing_lease()
        self.assertFalse(lease.METERED_ENFORCEMENT_ACTIVATED)
        with self.assertRaises(QuotaError) as caught:
            http_api.handle_check(self.db, self.identity(), made["lease_id"],
                                  self.check_body(), made["lease_token"],
                                  self.now())
        self.assertEqual(caught.exception.code, errors.CODE_FORBIDDEN)
        # the same token passes once the gate is open (R1's job)
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            status, _ = http_api.handle_check(
                self.db, self.identity(), made["lease_id"], self.check_body(),
                made["lease_token"], self.now())
        self.assertEqual(status, 200)

    def test_a_replay_returns_no_token_and_never_rotates_one(self):
        wire, made = self.make_old_enforcing_lease()
        before = self.rows("SELECT token_hash FROM leases")
        status, replay = http_api.handle_grant(
            self.db, self.identity(), wire, self.now(), wire["request_id"])
        self.assertEqual(status, 200)
        self.assertEqual(replay["lease_id"], made["lease_id"])
        self.assertEqual(replay["enforcement"], "metered")
        self.assertIsNone(replay["lease_token"])
        self.assertEqual(self.rows("SELECT token_hash FROM leases"), before)
        # with the gate open the replay rotates a token again (unchanged)
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            status, again = http_api.handle_grant(
                self.db, self.identity(), wire, self.now(), wire["request_id"])
        self.assertIsNotNone(again["lease_token"])

    def test_holds_settle_expiry_and_sweep_are_untouched(self):
        wire, made = self.make_old_enforcing_lease()
        holds = self.rows("SELECT * FROM holds ORDER BY rowid")
        self.assertTrue(holds)
        # the gate stays closed throughout
        gateway = self.ids("gateways")["g1"]
        part = lease.settle(self.db, self.receipt(
            made["lease_id"], False, actual=3, gateway=gateway), self.now())
        self.assertEqual((part["released_micro_eur"], part["retained_micro_eur"]),
                         (0, 0))
        self.assertEqual(self.rows("SELECT * FROM holds ORDER BY rowid"), holds)
        done = lease.settle(self.db, self.receipt(
            made["lease_id"], True, actual=5, gateway=gateway), self.now())
        self.assertEqual(done["retained_micro_eur"], 5)
        self.assertEqual(done["released_micro_eur"], made["money_hold"] - 5)
        self.assertEqual(self.rows("SELECT state FROM leases"), [("settled",)])

    def test_expiry_and_sweep_keep_the_holds(self):
        wire, made = self.make_old_enforcing_lease()
        holds = self.rows("SELECT * FROM holds ORDER BY rowid")
        swept = lease.expire_sweep(self.db, made["expires_at"])
        self.assertEqual(swept, {"expired": 1})
        self.assertEqual(self.rows("SELECT state, money_hold FROM leases"),
                         [("uncertain", made["money_hold"])])
        self.assertEqual(self.rows("SELECT * FROM holds ORDER BY rowid"), holds)

    def test_the_condition_lives_in_one_place(self):
        self.assertFalse(lease.token_bearing("metered"))
        self.assertTrue(lease.token_bearing("subscription_admission"))
        self.assertTrue(lease.token_bearing("subscription_warn"))
        self.assertFalse(lease.token_bearing("metered_advisory"))
        self.assertFalse(lease.token_bearing("shadow"))
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            self.assertTrue(lease.token_bearing("metered"))


class F2UnchangedRequestReplaysAfterTheUpgrade(_Fix):
    """A lease stored before L1a (fingerprint without task_class) still
    replays for its identical request."""

    def grant_legacy(self, seat="s1"):
        now = self.now()
        body = request(_ids(self), seat=seat, now_ms=now, gateway_id=(
            _ids(self)["gateways"]["g1"] if seat == "s1" else None))
        made = lease.grant(self.db, body, now)
        self.make_legacy(made["lease_id"], body)
        return body, made, now

    def test_the_identical_request_replays_the_stored_lease(self):
        body, made, now = self.grant_legacy()
        stored = self.rows("SELECT * FROM leases")
        replay = lease.grant(self.db, copy.deepcopy(body), now + 1000)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["lease_id"], made["lease_id"])
        self.assertEqual(self.rows("SELECT * FROM leases"), stored)

    def test_a_changed_body_is_a_conflict(self):
        body, made, now = self.grant_legacy()
        for field, value in (("output_cap", 999), ("ttl_ms", 1_000),
                             ("turn_id", str(uuid.uuid4())),
                             ("effort", "fast"),
                             ("model_id", _ids(self)["models"]["fast"])):
            changed = dict(copy.deepcopy(body), **{field: value})
            with self.assertRaises(QuotaError) as caught:
                lease.grant(self.db, changed, now)
            self.assertEqual(caught.exception.code,
                             errors.CODE_IDEMPOTENCY_CONFLICT, field)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM leases"), [(1,)])

    def test_a_changed_task_class_cannot_be_told_apart_on_a_legacy_lease(self):
        # The old fingerprint never saw task_class, so a legacy lease can
        # only compare the fields it did hash (decision recorded in the
        # step record); the wire-identity ledger still covers the HTTP path.
        body, made, now = self.grant_legacy()
        other = dict(copy.deepcopy(body), task_class="implement")
        self.assertTrue(lease.grant(self.db, other, now)["replayed"])

    def test_a_current_format_lease_with_another_task_class_conflicts(self):
        def two(d):
            second = copy.deepcopy(d["profiles"][0])
            second["task_class"] = "implement"
            d["profiles"].append(second)
        d = doc()
        two(d)
        self.import_ok(d)
        now = self.now()
        body = request(_ids(self), seat="s1", now_ms=now,
                       gateway_id=_ids(self)["gateways"]["g1"])
        lease.grant(self.db, body, now)
        other = dict(copy.deepcopy(body), task_class="implement")
        with self.assertRaises(QuotaError) as caught:
            lease.grant(self.db, other, now)
        self.assertEqual(caught.exception.code, errors.CODE_IDEMPOTENCY_CONFLICT)
        self.assertTrue(lease.grant(self.db, copy.deepcopy(body),
                                    now)["replayed"])

    def test_new_grants_use_the_current_fingerprint_and_marker(self):
        now = self.now()
        body = request(_ids(self), seat="s1", now_ms=now,
                       gateway_id=_ids(self)["gateways"]["g1"])
        made = lease.grant(self.db, body, now)
        stored_hash, snapshot = self.rows(
            "SELECT request_hash, policy_snapshot FROM leases WHERE id=?",
            (made["lease_id"],))[0]
        self.assertNotEqual(stored_hash, legacy_hash(body))
        self.assertEqual(stored_hash, lease._content_hash(body))
        self.assertIn("task_class", json.loads(snapshot))

    def test_over_http_the_identical_wire_body_replays(self):
        wire = self.wire("s1")
        status, first = http_api.handle_grant(
            self.db, self.identity(), wire, self.now(), wire["request_id"])
        self.assertEqual(status, 201)
        self.make_legacy(first["lease_id"], dict(wire, request_hash="0" * 64))
        status, replay = http_api.handle_grant(
            self.db, self.identity(), wire, self.now(), wire["request_id"])
        self.assertEqual(status, 200)
        self.assertEqual(replay["lease_id"], first["lease_id"])


class F3LogicalReferencesGuardRemoval(_Fix):
    """Removing a seat / team / project that something still points at (with
    or without a foreign key) refuses and rolls back everything."""

    def without_seat(self, alias):
        d = doc()
        d["seats"] = [s for s in d["seats"] if s["alias"] != alias]
        return d

    def sql(self, statement, params=()):
        conn = sqlite3.connect(self.db)
        conn.execute(statement, params)
        conn.commit()
        conn.close()

    def test_a_seat_with_a_reading_cannot_be_removed(self):
        seat = self.ids("seats")["s2"]
        self.sql("INSERT INTO readings VALUES(?,?,?,?,?,?,?,?)", (
            str(uuid.uuid4()), self.ids("machines")["worker"],
            self.ids("accounts")["meter"], seat, "cli_window", 1, 1, "{}"))
        self.assert_unchanged_refusal(self.without_seat("s2"))
        self.assertEqual(self.rows("SELECT id FROM seats WHERE id=?", (seat,)),
                         [(seat,)])

    def test_a_scope_with_daily_totals_cannot_be_removed(self):
        seat = self.ids("seats")["s2"]
        self.sql("INSERT INTO daily_totals VALUES('seat',?,?,1,1,1,0)",
                 (seat, "2026-10-01"))
        self.assert_unchanged_refusal(self.without_seat("s2"))
        # a team's totals stop the team's removal
        self.sql("DELETE FROM daily_totals")
        team = self.ids("teams", "alias")["dev"]
        self.sql("INSERT INTO daily_totals VALUES('team',?,?,1,1,1,0)",
                 (team, "2026-10-01"))
        d = doc()
        d["seats"], d["teams"], d["limits"] = [], [], []
        self.assert_unchanged_refusal(d)
        # and a project's totals stop the project's removal
        self.sql("DELETE FROM daily_totals")
        project = self.ids("projects")["org"]
        self.sql("INSERT INTO daily_totals VALUES('project',?,?,1,1,1,0)",
                 (project, "2026-10-01"))
        d = doc()
        d["seats"], d["teams"], d["project_accounts"], d["projects"] = [], [], [], []
        self.assert_unchanged_refusal(d)

    def test_a_limit_a_hold_points_at_cannot_be_removed_with_its_seat(self):
        d = doc()
        d["limits"] = [{"scope": "seat", "machine": "worker", "seat": "s2",
                        "metric": "concurrency", "period": "turn",
                        "window_key": None, "soft": None, "hard": 5}]
        self.import_ok(d)
        limit = self.rows("SELECT id FROM limits")[0][0]
        now = self.now()
        body = request(_ids(self), seat="s1", now_ms=now,
                       gateway_id=_ids(self)["gateways"]["g1"])
        made = lease.grant(self.db, body, now)
        self.sql("INSERT INTO holds(lease_id, limit_id, period_start, "
                 "reserved) VALUES(?,?,?,?)", (made["lease_id"], limit, 1, 1))
        self.assert_unchanged_refusal(self.without_seat("s2"))
        # without the hold the same removal succeeds, limit and all
        self.sql("DELETE FROM holds")
        self.import_ok(self.without_seat("s2"))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM limits"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM seats WHERE alias='s2'"),
                         [(0,)])

    def test_a_removed_seat_with_nothing_pointing_at_it_is_deleted(self):
        self.import_ok(self.without_seat("s2"))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM seats"), [(2,)])

    def test_a_leased_seat_cannot_be_removed(self):
        now = self.now()
        lease.grant(self.db, request(_ids(self), seat="s2", now_ms=now,
                                     gateway_id=None), now)
        self.assert_unchanged_refusal(self.without_seat("s2"))

    def test_a_profile_may_be_removed_while_leases_name_its_class(self):
        # Decision: leases.task_class is history, not a live reference (a
        # replay needs no profile; leases stored before L1a hold the effort
        # alias there), so removing the profile is allowed and the lease
        # is untouched.
        now = self.now()
        made = lease.grant(self.db, request(
            _ids(self), seat="s1", now_ms=now,
            gateway_id=_ids(self)["gateways"]["g1"]), now)
        stored = self.rows("SELECT * FROM leases")
        d = doc()
        d["profiles"] = []
        self.import_ok(d)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM profiles"), [(0,)])
        self.assertEqual(self.rows("SELECT * FROM leases"), stored)
        self.assertEqual(made["state"], "active")

    def test_the_policy_guard_list_covers_every_reference(self):
        self.assert_guards_cover(policy_config.REMOVAL_GUARDS,
                                 ("projects", "teams", "seats", "models"))

    def assert_guards_cover(self, guards, targets):
        conn = sqlite3.connect(self.db)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            declared = {t: set() for t in targets}
            for table in tables:
                for row in conn.execute(
                        "PRAGMA foreign_key_list(%s)" % table):
                    target, column = row[2], row[3]
                    if target in declared:
                        declared[target].add((table, column))
        finally:
            conn.close()
        for target in targets:
            self.assertTrue(declared[target] <= set(guards[target]),
                            (target, declared[target] - set(guards[target])))
            self.assertTrue(declared[target], target)


class F4InventoryRemovalsAreInvalidConfig(_Fix):
    """Every referenced inventory removal (version 1 and 2) is INVALID_CONFIG
    with the static message, never STORE_ERROR."""

    def sql(self, statement, params=()):
        conn = sqlite3.connect(self.db)
        conn.execute(statement, params)
        conn.commit()
        conn.close()

    def v1(self, drop_account=None, drop_gateway=False):
        d = base_doc()
        out = {"version": 1, "accounts": d["accounts"],
               "machines": d["machines"], "gateways": d["gateways"]}
        if drop_account:
            out["accounts"] = [a for a in out["accounts"]
                               if a["alias"] != drop_account]
        if drop_gateway:
            out["gateways"] = []
        return out

    def v2_without_account(self, alias):
        d = doc()
        d["accounts"] = [a for a in d["accounts"] if a["alias"] != alias]
        d["gateways"] = [g for g in d["gateways"] if g["account"] != alias]
        d["models"] = [m for m in d["models"] if m["account"] != alias]
        d["project_accounts"] = [p for p in d["project_accounts"]
                                 if p["account"] != alias]
        d["profiles"][0]["candidates"] = [
            c for c in d["profiles"][0]["candidates"]
            if c in ("opus", "fast", "park")]
        return d

    def test_an_account_with_a_limit_event_and_block(self):
        # the reviewer's case
        other = self.ids("accounts")["other"]
        now = self.now()
        lease.record_limit_event(self.db, {
            "event_id": str(uuid.uuid4()), "account_id": other,
            "model_id": None, "turn_id": str(uuid.uuid4()),
            "observed_at": now, "evidence_kind": "provider_usage_429",
            "resets_at": None, "exhausted_windows": []}, now)
        for document in (self.v2_without_account("other"),
                         self.v1(drop_account="other")):
            self.assert_unchanged_refusal(document)

    def test_an_account_with_only_a_block(self):
        other = self.ids("accounts")["other"]
        self.sql("INSERT INTO blocks(id, account_id, model_id, reason, "
                 "retry_at, state, source_event_id, updated_at) "
                 "VALUES(?,?,NULL,'overrun',NULL,'parked',NULL,1)",
                 (str(uuid.uuid4()), other))
        for document in (self.v2_without_account("other"),
                         self.v1(drop_account="other")):
            self.assert_unchanged_refusal(document)

    def test_an_account_with_only_an_adjustment(self):
        other = self.ids("accounts")["other"]
        self.sql("INSERT INTO adjustments(id, account_id, gateway_id, "
                 "source_event_id, period_start, unattributed_micro_eur, "
                 "reason, resolved_at) VALUES(?,?,?,?,1,5,'overrun',1)",
                 (str(uuid.uuid4()), other, self.ids("gateways")["g1"],
                  str(uuid.uuid4())))
        for document in (self.v2_without_account("other"),
                         self.v1(drop_account="other")):
            self.assert_unchanged_refusal(document)

    def test_a_gateway_with_a_lease(self):
        now = self.now()
        lease.grant(self.db, request(_ids(self), seat="s1", now_ms=now,
                                     gateway_id=_ids(self)["gateways"]["g1"]),
                    now)
        d = doc()
        d["gateways"] = []
        for document in (d, self.v1(drop_gateway=True)):
            self.assert_unchanged_refusal(document)

    def test_a_gateway_with_a_receipt(self):
        now = self.now()
        made = lease.grant(self.db, request(_ids(self), seat="s2", now_ms=now,
                                            gateway_id=None), now)
        self.sql("INSERT INTO receipts(event_id, lease_id, gateway_id, seq, "
                 "calls, input_tokens, cached_input_tokens, output_tokens, "
                 "actual_micro_eur, outcome, final, observed_at, received_at)"
                 " VALUES(?,?,?,1,1,1,0,1,NULL,'completed',0,1,1)",
                 (str(uuid.uuid4()), made["lease_id"], self.ids("gateways")["g1"]))
        d = doc()
        d["gateways"] = []
        for document in (d, self.v1(drop_gateway=True)):
            self.assert_unchanged_refusal(document)

    def test_a_gateway_with_an_adjustment(self):
        meter = self.ids("accounts")["meter"]
        self.sql("INSERT INTO adjustments(id, account_id, gateway_id, "
                 "source_event_id, period_start, unattributed_micro_eur, "
                 "reason, resolved_at) VALUES(?,?,?,?,1,5,'overrun',1)",
                 (str(uuid.uuid4()), meter, self.ids("gateways")["g1"],
                  str(uuid.uuid4())))
        d = doc()
        d["gateways"] = []
        for document in (d, self.v1(drop_gateway=True)):
            self.assert_unchanged_refusal(document)

    def test_a_machine_with_a_lease(self):
        now = self.now()
        lease.grant(self.db, request(_ids(self), seat="s2", now_ms=now,
                                     gateway_id=None), now)
        d = doc()
        d["machines"] = [{"alias": "desktop"}]
        d["seats"] = [s for s in d["seats"] if s["machine"] != "worker"]
        self.assert_unchanged_refusal(d)
        v1 = self.v1()
        v1["machines"] = [{"alias": "desktop"}]
        self.assert_unchanged_refusal(v1)

    def test_a_referenced_removal_is_never_a_store_error(self):
        self.limit_block()
        result = self.import_doc(self.v2_without_account("other"))
        self.assertNotEqual(result.stderr.strip(), ERROR_STORE)
        self.assertEqual(result.returncode, 2)

    def test_the_inventory_guard_lists_cover_every_foreign_key(self):
        F3LogicalReferencesGuardRemoval.assert_guards_cover(
            self, store.REMOVAL_GUARDS, ("accounts", "machines", "gateways"))

    def test_unreferenced_inventory_rows_are_still_deleted(self):
        d = self.v2_without_account("other")
        self.import_ok(d)
        self.assertEqual(self.rows("SELECT alias FROM accounts"), [("meter",)])
        v1 = self.v1(drop_gateway=True)
        v1["machines"] = [{"alias": "desktop"}]
        # worker's seats still reference it: refused; desktop-only is fine
        self.assert_unchanged_refusal(v1)


class F5BrokenStoredProfile(_Fix):
    """A stored `candidates` that is not a list of strings is a store fault
    (exit 3); a valid list without the model is an ordinary denial."""

    def set_candidates(self, text):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE profiles SET candidates=?", (text,))
        conn.commit()
        conn.close()

    def grant_s1(self):
        now = self.now()
        return lease.grant(self.db, request(
            _ids(self), seat="s1", now_ms=now,
            gateway_id=_ids(self)["gateways"]["g1"]), now)

    def test_malformed_shapes_are_store_errors(self):
        for text in ("{}", '{"a":1}', "5", '"opus"', "null", "true",
                     '["opus", 5]', '[["opus"]]', "[null]", "not json", ""):
            self.set_candidates(text)
            with self.assertRaises(QuotaError) as caught:
                self.grant_s1()
            self.assertEqual(caught.exception.code, errors.CODE_STORE_ERROR,
                             text)
            self.assertEqual(caught.exception.message, errors.MSG_STORE_FAILED)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM leases"), [(0,)])

    def test_the_store_error_is_exit_3_and_reaches_the_wire_layer(self):
        self.assertEqual(errors.EXIT_CODES[errors.CODE_STORE_ERROR], 3)
        self.set_candidates("{}")
        wire = self.wire_s1()
        with self.assertRaises(QuotaError) as caught:
            http_api.handle_grant(self.db, self.identity(), wire, self.now(),
                                  wire["request_id"])
        self.assertEqual(caught.exception.code, errors.CODE_STORE_ERROR)

    def wire_s1(self):
        body = request(_ids(self), seat="s1", now_ms=self.now(),
                       gateway_id=_ids(self)["gateways"]["g1"])
        del body["request_hash"]
        return body

    def identity(self):
        return (self.ids("machines")["desktop"], "desktop")

    def test_a_valid_list_without_the_model_is_policy_denied(self):
        for text in ('["fast"]', "[]", '["fast", "park"]'):
            self.set_candidates(text)
            with self.assertRaises(QuotaError) as caught:
                self.grant_s1()
            self.assertEqual(caught.exception.code, errors.CODE_POLICY_DENIED,
                             text)

    def test_a_valid_list_with_the_model_is_granted(self):
        self.set_candidates('["opus"]')
        self.assertEqual(self.grant_s1()["state"], "active")

    def test_a_replay_still_needs_no_profile(self):
        now = self.now()
        body = request(_ids(self), seat="s1", now_ms=now,
                       gateway_id=_ids(self)["gateways"]["g1"])
        lease.grant(self.db, body, now)
        self.set_candidates("{}")
        self.assertTrue(lease.grant(self.db, copy.deepcopy(body),
                                    now)["replayed"])


if __name__ == "__main__":
    unittest.main()
