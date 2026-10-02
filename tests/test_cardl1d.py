"""Card L1d: a granted lease always replays, and a stale estimate has its own
error (docs/STEP-CARD-L1D.md; docs/DESIGN.md section 7).

Rows: replay_survives_policy_edits, new_requests_keep_old_errors,
estimate_stale_new_request, estimate_stale_closed, other_invalid_unchanged,
changed_replay_conflicts, legacy_replay, plus the same rows over HTTP.
Every store is built through config-import v2; the policy edits that follow a
grant are direct SQL (the operator-removal guards of the importer would
refuse most of them). Synthetic data only.
"""

import copy
import json
import sqlite3
import threading
import time
import unittest
import uuid

from quota_balancer import errors, http_api, lease
from quota_balancer.errors import QuotaError

from tests.test_cardl1a import _call, _ids, run_cli
from tests.test_cardl1a_fix1 import legacy_hash
from tests.test_cardl1b import _L1b

STALE_MESSAGE = "the admission estimate is stale"


class _L1d(_L1b):
    def edit(self, *statements):
        """Apply operator policy edits after the grant."""
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            for sql, params in statements:
                conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def mutations(self):
        """name -> (seat, statements, error a NEW request then gets)."""
        ids = _ids(self)
        opus = ids["models"]["opus"]
        s6 = ids["seats"]["s6"]
        meter = self.ids("accounts")["meter"]
        other = self.ids("accounts")["other"]
        org = self.ids("projects")["org"]
        return {
            "model_removed": ("s6", [
                ("DELETE FROM models WHERE id=?", (opus,))],
                errors.CODE_NOT_FOUND),
            "effort_removed": ("s6", [
                ("UPDATE models SET effort_aliases='fast' WHERE id=?",
                 (opus,))], errors.CODE_INVALID_REQUEST),
            "project_link_removed": ("s6", [
                ("DELETE FROM project_accounts WHERE project_id=? AND "
                 "account_id=?", (org, meter))], errors.CODE_POLICY_DENIED),
            "account_kind_changed": ("s4", [
                ("UPDATE accounts SET kind='metered', "
                 "budget_micro_eur=1000000 WHERE id=?", (other,))],
                errors.CODE_POLICY_DENIED),
            "profile_deleted": ("s6", [
                ("DELETE FROM profiles", ())], errors.CODE_NOT_FOUND),
            "candidates_changed": ("s6", [
                ("UPDATE profiles SET candidates='[\"fast\",\"park\"]'", ())],
                errors.CODE_POLICY_DENIED),
            "profile_ceiling_lowered": ("s6", [
                ("UPDATE profiles SET ttl_ms=1000", ())],
                errors.CODE_POLICY_DENIED),
            "seat_reconfigured": ("s6", [
                ("UPDATE seats SET enforcement='subscription_admission' "
                 "WHERE id=?", (s6,))], errors.CODE_POLICY_DENIED),
            "account_parked": ("s6", [
                ("INSERT INTO blocks(id, account_id, model_id, reason, "
                 "retry_at, state, source_event_id, updated_at) "
                 "VALUES('b1',?,NULL,'exhausted',NULL,'parked','ev',1)",
                 (meter,))], errors.CODE_ACCOUNT_PARKED),
        }

    def fresh(self, seat, **override):
        body, now = self.known(seat, **override)
        return body, now


class ReplaySurvivesPolicyEdits(_L1d):
    """replay_survives_policy_edits / new_requests_keep_old_errors, store."""

    def test_a_granted_lease_replays_after_each_policy_edit(self):
        for name in sorted(self.mutations()):
            with self.subTest(name):
                self.setUp()
                seat, edits, _code = self.mutations()[name]
                body, now = self.fresh(seat)
                first = self.grant(body, now)
                self.edit(*edits)
                replay = self.grant(copy.deepcopy(body), now + 5000)
                self.assertTrue(replay["replayed"], name)
                self.assertEqual(replay["lease_id"], first["lease_id"])
                self.assertEqual(self.rows("SELECT COUNT(*) FROM leases"),
                                 [(1,)])

    def test_a_new_request_gets_the_old_error_after_each_edit(self):
        for name in sorted(self.mutations()):
            with self.subTest(name):
                self.setUp()
                seat, edits, code = self.mutations()[name]
                body, now = self.fresh(seat)
                self.grant(body, now)
                again, now2 = self.fresh(seat)    # built before the edit
                self.edit(*edits)
                self.assert_code(code, again, now2)

    def test_a_replay_is_found_before_the_stale_check_too(self):
        body, now = self.fresh("s6")
        first = self.grant(body, now)
        replay = self.grant(copy.deepcopy(body), now + 10 * 60_000)
        self.assertEqual(replay["lease_id"], first["lease_id"])
        self.assertTrue(replay["replayed"])


class ChangedReplayConflicts(_L1d):
    """changed_replay_conflicts: policy edits never turn a changed body
    for a granted request id into anything else."""

    def test_changed_content_conflicts_even_after_each_edit(self):
        for name in sorted(self.mutations()):
            with self.subTest(name):
                self.setUp()
                seat, edits, _code = self.mutations()[name]
                body, now = self.fresh(seat)
                self.grant(body, now)
                self.edit(*edits)
                changed = copy.deepcopy(body)
                changed["max_calls"] = 1
                self.assert_code(errors.CODE_IDEMPOTENCY_CONFLICT, changed,
                                 now + 1000)

    def test_a_stale_changed_replay_is_a_conflict_not_stale(self):
        body, now = self.fresh("s6")
        self.grant(body, now)
        changed = copy.deepcopy(body)
        changed["output_cap"] = 1
        self.assert_code(errors.CODE_IDEMPOTENCY_CONFLICT, changed,
                         now + 10 * 60_000)


class EstimateStale(_L1d):
    """estimate_stale_new_request / estimate_stale_closed."""

    def test_too_old_and_too_new_are_estimate_stale(self):
        for observed in (-120_001, +30_001):
            with self.subTest(observed):
                body, now = self.fresh("s6")
                body["context"]["observed_at"] = now + observed
                exc = self.assert_code("ESTIMATE_STALE", body, now)
                self.assertEqual(exc.message, STALE_MESSAGE)
                self.assertIsNone(exc.retry_at)

    def test_the_boundaries_still_pass(self):
        for observed in (-120_000, +30_000):
            body, now = self.fresh("s6")
            body["context"]["observed_at"] = now + observed
            self.assertEqual(self.grant(body, now)["state"], "active")

    def test_an_unknown_context_is_checked_the_same_way(self):
        body, now = self.unknown()
        body["context"]["observed_at"] = now - 120_001
        self.assert_code("ESTIMATE_STALE", body, now)

    def test_the_code_is_closed_with_exit_2_http_400_and_a_static_text(self):
        self.assertEqual(errors.CODE_ESTIMATE_STALE, "ESTIMATE_STALE")
        self.assertEqual(errors.MSG_ESTIMATE_STALE, STALE_MESSAGE)
        self.assertEqual(errors.EXIT_CODES["ESTIMATE_STALE"], 2)
        self.assertEqual(http_api._HTTP_STATUS["ESTIMATE_STALE"], 400)

    def test_the_stale_check_still_comes_after_every_policy_check(self):
        for kwargs, code in (({"task_class": "ghost"}, errors.CODE_NOT_FOUND),
                             ({"ttl_ms": 1_800_001},
                              errors.CODE_POLICY_DENIED)):
            body, now = self.fresh("s6", **kwargs)
            body["context"]["observed_at"] = now - 500_000
            self.assert_code(code, body, now)
        self.edit(("INSERT INTO blocks(id, account_id, model_id, reason, "
                   "retry_at, state, source_event_id, updated_at) "
                   "VALUES('b1',?,NULL,'exhausted',NULL,'parked','ev',1)",
                   (self.ids("accounts")["meter"],)))
        body, now = self.fresh("s6")
        body["context"]["observed_at"] = now - 500_000
        self.assert_code(errors.CODE_ACCOUNT_PARKED, body, now)

    def test_the_exact_message_is_what_the_cli_style_error_carries(self):
        body, now = self.fresh("s6")
        body["context"]["observed_at"] = now - 500_000
        with self.assertRaises(QuotaError) as caught:
            lease.grant(self.db, body, now)
        self.assertEqual(str(caught.exception), STALE_MESSAGE)


class OtherInvalidRequestsUnchanged(_L1d):
    """other_invalid_unchanged: nothing but freshness moved to the new code."""

    def test_each_other_cause_is_still_invalid_request(self):
        cases = []
        body, now = self.fresh("s6")
        body["effort"] = "nope"
        cases.append(body)
        body, now = self.unknown()
        body["context"]["initial_tokens"] = 5          # mixed fields
        cases.append(body)
        body, now = self.fresh("s6")
        body["max_calls"] = "2"                        # bad shape
        cases.append(body)
        body, now = self.fresh("s6")
        del body["ttl_ms"]                             # missing key
        cases.append(body)
        for body in cases:
            with self.assertRaises(QuotaError) as caught:
                lease.grant(self.db, body, now)
            self.assertEqual(caught.exception.code,
                             errors.CODE_INVALID_REQUEST)
            self.assertEqual(caught.exception.message,
                             errors.MSG_INVALID_REQUEST)

    def test_the_two_codes_are_distinct_strings(self):
        self.assertNotEqual(errors.CODE_ESTIMATE_STALE,
                            errors.CODE_INVALID_REQUEST)
        self.assertNotEqual(errors.MSG_ESTIMATE_STALE,
                            errors.MSG_INVALID_REQUEST)


class LegacyReplay(_L1d):
    """legacy_replay: a lease stored in the pre-L1a format still replays,
    and still does so after a policy edit."""

    def test_a_legacy_format_lease_replays_after_a_policy_edit(self):
        body, now = self.fresh("s6")
        first = self.grant(body, now)
        snapshot = json.loads(self.rows(
            "SELECT policy_snapshot FROM leases WHERE id=?",
            (first["lease_id"],))[0][0])
        snapshot.pop("task_class")
        snapshot.pop("policy_revision")
        self.edit(("UPDATE leases SET request_hash=?, task_class='opus', "
                   "policy_snapshot=? WHERE id=?",
                   (legacy_hash(body), json.dumps(snapshot, sort_keys=True),
                    first["lease_id"])),
                  ("DELETE FROM models WHERE id=?",
                   (_ids(self)["models"]["opus"],)),
                  ("DELETE FROM profiles", ()))
        replay = self.grant(copy.deepcopy(body), now + 1000)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["lease_id"], first["lease_id"])


class OrderOfRefusals(_L1d):
    """The seat is still looked up first; an unknown seat is NOT_FOUND."""

    def test_an_unknown_seat_is_not_found(self):
        body, now = self.fresh("s6")
        body["seat_id"] = str(uuid.uuid4())
        self.assert_code(errors.CODE_NOT_FOUND, body, now)

    def test_a_new_request_failing_two_checks_gets_the_earlier_one(self):
        # model unknown (NOT_FOUND) beats a bad effort (INVALID_REQUEST)
        body, now = self.fresh("s6")
        body["model_id"] = str(uuid.uuid4())
        body["effort"] = "nope"
        self.assert_code(errors.CODE_NOT_FOUND, body, now)


class HttpPath(_L1d):
    """The same rows over the wire."""

    def setUp(self):
        super().setUp()
        self.assertEqual(run_cli(
            "--db", self.db, "identity-set", "--machine", "desktop",
            "--identity-ref", "identity-desktop", "--enabled",
            "true").returncode, 0)
        self.server = http_api.make_server(self.db, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def wire(self, seat="s6", **override):
        now = self.now()
        body, _ = self.known(seat, now_ms=now, **override)
        del body["request_hash"]
        return body

    def post(self, body):
        return _call(self.base, "/v1/leases", body, body["request_id"])

    def test_a_replay_survives_each_policy_edit_over_http(self):
        # s4 is a worker seat: only desktop seats are reachable here.
        for name, (seat, edits, _code) in sorted(self.mutations().items()):
            if seat != "s6":
                continue
            with self.subTest(name):
                self.edit(*[("DELETE FROM blocks", ())])
                body = self.wire(seat)
                status, first = self.post(body)
                self.assertEqual(status, 201, first)
                self.edit(*edits)
                status, again = self.post(copy.deepcopy(body))
                self.assertEqual(status, 200, again)
                self.assertEqual(again["lease_id"], first["lease_id"])
                self.setUp_reset()

    def setUp_reset(self):
        """Put the policy back after an edit (re-import the document)."""
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            for table in ("blocks", "leases", "holds", "http_request_identity"):
                conn.execute("DELETE FROM " + table)
            conn.commit()
        finally:
            conn.close()
        self.import_ok(self.restore_doc())

    def restore_doc(self):
        from tests.test_cardl1b import doc
        return doc()

    def test_a_stale_new_request_is_400_estimate_stale(self):
        for delta in (-120_001, 30_001):
            body = self.wire()
            body["context"]["observed_at"] = self.now() + delta
            status, out = self.post(body)
            self.assertEqual(status, 400, out)
            self.assertEqual(out["error"]["code"], "ESTIMATE_STALE")
            self.assertEqual(out["error"]["message"], STALE_MESSAGE)

    def test_a_replay_over_http_ignores_a_stale_estimate(self):
        body = self.wire()
        status, first = self.post(body)
        self.assertEqual(status, 201, first)
        # the same bytes later: observed_at is now old, but it replays
        time.sleep(0.01)
        status, again = self.post(copy.deepcopy(body))
        self.assertEqual(status, 200, again)

    def test_a_changed_replay_is_409_after_an_edit(self):
        body = self.wire()
        self.assertEqual(self.post(body)[0], 201)
        self.edit(("DELETE FROM models WHERE id=?",
                   (_ids(self)["models"]["opus"],)))
        changed = copy.deepcopy(body)
        changed["max_calls"] = 1
        status, out = _call(self.base, "/v1/leases", changed,
                            body["request_id"])
        self.assertEqual((status, out["error"]["code"]),
                         (409, "IDEMPOTENCY_CONFLICT"))

    def test_other_invalid_requests_stay_400_invalid_request(self):
        body = self.wire()
        body["effort"] = "nope"
        status, out = self.post(body)
        self.assertEqual((status, out["error"]["code"]),
                         (400, "INVALID_REQUEST"))


if __name__ == "__main__":
    unittest.main()
