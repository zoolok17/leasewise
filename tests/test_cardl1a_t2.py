"""Card L1a, turn 2: task_class carried end to end and the profile applied at
grant (docs/DESIGN.md sections 6-7; docs/STEP-CARD-L1A.md, turn 2).

Rows: task_class_stored, unknown_task_class, candidate_denied,
profile_margin_applied, profile_ceilings, replay_after_profile_edit,
content_hash_includes_task_class, plus the HTTP path. Every store is built
through config-import v2 (no policy row inserted with SQL); synthetic data.
"""

import copy
import hashlib
import json
import threading
import time
import unittest
import uuid

from quota_balancer import errors, http_api, lease, policy
from quota_balancer.errors import QuotaError

from tests.test_cardl1a import _Base, _call, _ids, base_doc, run_cli

# The profile of base_doc(): expected 1, max 2, output 1000, per-call 500,
# ttl 30 min, margin 12500 bps; candidates opus and fast.
PROFILE = {"expected_calls": 1, "max_calls": 2, "output_cap": 1000,
           "per_call_output_cap": 500, "ttl_ms": 1_800_000}
MODEL = {
    "alias": "opus", "context_ceiling": 180000, "output_ceiling": 18000,
    "tariff_input_rate": 1, "tariff_output_rate": 1,
    "reserve_input_rate": 1_000_000, "reserve_output_rate": 1_000_000,
    "reservation_mode": "sized", "policy_hash": "ab" * 32,
    "effort_aliases": "opus",
}


def doc_without_limits():
    """base_doc() minus its limits (so repeated grants meet no concurrency
    cap): these rows are about the profile, not about limits."""
    doc = base_doc()
    doc["limits"] = []
    return doc


def request(ids, seat="s1", model="opus", task_class="bounded_read",
            now_ms=None, **override):
    now_ms = now_ms or int(time.time() * 1000)
    body = {
        "request_id": str(uuid.uuid4()),
        "request_hash": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        "turn_id": str(uuid.uuid4()),
        "seat_id": ids["seats"][seat],
        "model_id": ids["models"][model],
        "task_class": task_class,
        "effort": model,
        "gateway_id": ids["gateways"]["g1"],
        "parent_lease_id": None,
        "context": {"mode": "FRESH", "initial_tokens": 1000,
                    "dispatch_tokens": 0, "source": "tokenizer",
                    "observed_at": now_ms - 1000,
                    "growth_tokens_per_call": 0},
        "expected_calls": PROFILE["expected_calls"],
        "max_calls": PROFILE["max_calls"],
        "output_cap": PROFILE["output_cap"],
        "per_call_output_cap": PROFILE["per_call_output_cap"],
        "ttl_ms": PROFILE["ttl_ms"],
    }
    body.update(override)
    return body


class _T2(_Base):
    def setUp(self):
        super().setUp()
        self.import_ok(doc_without_limits())

    def grant(self, **kw):
        now = int(time.time() * 1000)
        return lease.grant(self.db, request(_ids(self), now_ms=now, **kw), now)

    def assert_code(self, code, **kw):
        before = self.rows("SELECT COUNT(*) FROM leases")
        with self.assertRaises(QuotaError) as caught:
            self.grant(**kw)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM leases"), before)

    def reimport(self, mutate):
        doc = doc_without_limits()
        mutate(doc)
        self.import_ok(doc)


class TaskClassStored(_T2):
    """task_class_stored: the task class is persisted; the effort is not
    stored in its place."""

    def test_store_level_task_class_is_persisted(self):
        granted = self.grant()
        row = self.rows("SELECT task_class, policy_snapshot FROM leases "
                        "WHERE id=?", (granted["lease_id"],))[0]
        self.assertEqual(row[0], "bounded_read")
        snapshot = json.loads(row[1])
        self.assertEqual(snapshot["effort"], "opus")
        self.assertEqual(snapshot["task_class"], "bounded_read")
        self.assertEqual(snapshot["policy_revision"], 1)

    def test_policy_revision_follows_the_profile(self):
        def bump(doc):
            doc["profiles"][0]["policy_revision"] = 7
        self.reimport(bump)
        granted = self.grant()
        snapshot = json.loads(self.rows(
            "SELECT policy_snapshot FROM leases WHERE id=?",
            (granted["lease_id"],))[0][0])
        self.assertEqual(snapshot["policy_revision"], 7)

    def test_task_class_is_a_required_alias_in_the_closed_request(self):
        ids = _ids(self)
        now = int(time.time() * 1000)
        missing = request(ids, now_ms=now)
        del missing["task_class"]
        with self.assertRaises(QuotaError) as caught:
            lease.grant(self.db, missing, now)
        self.assertEqual(caught.exception.code, errors.CODE_INVALID_REQUEST)
        for bad in ("Bounded Read", "", None, 5, ["bounded_read"]):
            with self.assertRaises(QuotaError) as caught:
                lease.grant(self.db, request(ids, now_ms=now,
                                             task_class=bad), now)
            self.assertEqual(caught.exception.code,
                             errors.CODE_INVALID_REQUEST, bad)


class UnknownTaskClass(_T2):
    """unknown_task_class: refused exactly like an unknown model id."""

    def test_refused_like_an_unknown_model(self):
        now = int(time.time() * 1000)
        ids = _ids(self)
        with self.assertRaises(QuotaError) as unknown_model:
            lease.grant(self.db, request(
                ids, now_ms=now, model_id=str(uuid.uuid4())), now)
        with self.assertRaises(QuotaError) as unknown_class:
            lease.grant(self.db, request(
                ids, now_ms=now, task_class="ghost"), now)
        self.assertEqual(unknown_class.exception.code, errors.CODE_NOT_FOUND)
        self.assertEqual(
            (unknown_class.exception.code, unknown_class.exception.message),
            (unknown_model.exception.code, unknown_model.exception.message))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM leases"), [(0,)])

    def test_an_empty_profile_table_refuses_every_class(self):
        self.reimport(lambda doc: doc.update(profiles=[]))
        self.assert_code(errors.CODE_NOT_FOUND)


class CandidateDenied(_T2):
    """candidate_denied: the model must be one of the profile's candidates."""

    def test_model_outside_the_candidates_is_policy_denied(self):
        def only_fast(doc):
            doc["profiles"][0]["candidates"] = ["fast", "park"]
        self.reimport(only_fast)
        self.assert_code(errors.CODE_POLICY_DENIED, model="opus")
        granted = self.grant(model="fast")
        self.assertEqual(granted["state"], "active")

    def test_every_listed_candidate_is_allowed(self):
        self.assertEqual(self.grant(model="opus")["state"], "active")
        self.assertEqual(self.grant(model="fast")["state"], "active")

    def test_a_model_of_another_profile_is_denied_here(self):
        def two(doc):
            second = copy.deepcopy(doc["profiles"][0])
            second.update(task_class="implement", candidates=["fast"])
            doc["profiles"].append(second)
        self.reimport(two)
        self.assert_code(errors.CODE_POLICY_DENIED, model="opus",
                         task_class="implement")
        self.assertEqual(self.grant(model="fast",
                                    task_class="implement")["state"],
                         "active")


class ProfileMarginApplied(_T2):
    """profile_margin_applied: margin_bps comes from the profile."""

    def expected(self, margin_bps):
        est = policy.estimate(1000, 1, 0, 1000, 2, dict(MODEL), margin_bps)
        return est["input_cap"], est["hold_sized"], est["expected_input"]

    def test_the_pinned_card3_numbers(self):
        # E = 1000; I = ceil(E*margin/10000); H = I + 1000 + 2*2 - 1 at
        # reserve rate 1 EUR per 1M tokens (1000000 micro-EUR per 1M).
        self.assertEqual(self.expected(12500), (1250, 2253, 1000))
        self.assertEqual(self.expected(20000), (2000, 3003, 1000))
        self.assertEqual(self.expected(10000), (1000, 2003, 1000))

    def test_the_lease_follows_the_profile_margin(self):
        for margin in (12500, 20000, 10000):
            def set_margin(doc, value=margin):
                doc["profiles"][0]["margin_bps"] = value
            self.reimport(set_margin)
            granted = self.grant()
            input_cap, hold, e = self.expected(margin)
            self.assertEqual(granted["caps"]["input_cap"], input_cap, margin)
            self.assertEqual(granted["caps"]["expected_input"], e)
            estimate = json.loads(self.rows(
                "SELECT estimate FROM leases WHERE id=?",
                (granted["lease_id"],))[0][0])
            self.assertEqual(estimate["hold_sized"], hold, margin)
            self.assertEqual(estimate["input_cap"], input_cap, margin)
            snapshot = json.loads(self.rows(
                "SELECT policy_snapshot FROM leases WHERE id=?",
                (granted["lease_id"],))[0][0])
            self.assertEqual(snapshot["caps"]["hold_sized"], hold)

    def test_the_request_cannot_set_the_margin(self):
        ids = _ids(self)
        now = int(time.time() * 1000)
        body = request(ids, now_ms=now)
        body["margin_bps"] = 20000
        with self.assertRaises(QuotaError) as caught:
            lease.grant(self.db, body, now)
        self.assertEqual(caught.exception.code, errors.CODE_INVALID_REQUEST)


class ProfileCeilings(_T2):
    """profile_ceilings: a request may ask for less than the profile,
    never more."""

    FIELDS = ("expected_calls", "max_calls", "output_cap",
              "per_call_output_cap", "ttl_ms")

    def test_at_the_profile_value_is_accepted_and_plus_one_is_denied(self):
        for field in self.FIELDS:
            self.assertEqual(self.grant(**{field: PROFILE[field]})["state"],
                             "active", field)
            self.assert_code(errors.CODE_POLICY_DENIED,
                             **{field: PROFILE[field] + 1})

    def test_less_than_the_profile_is_accepted(self):
        for field, value in (("output_cap", 10), ("per_call_output_cap", 1),
                             ("ttl_ms", 1)):
            self.assertEqual(self.grant(**{field: value})["state"], "active",
                             field)

    def test_raising_the_profile_raises_the_ceiling(self):
        def raise_all(doc):
            doc["profiles"][0].update(expected_calls=2, max_calls=3,
                                      output_cap=1001, per_call_output_cap=501,
                                      ttl_ms=1_800_001)
        self.reimport(raise_all)
        for field in self.FIELDS:
            value = {"expected_calls": 2, "max_calls": 3, "output_cap": 1001,
                     "per_call_output_cap": 501, "ttl_ms": 1_800_001}[field]
            self.assertEqual(self.grant(**{field: value})["state"], "active",
                             field)

    def test_the_absolute_bounds_still_apply_above_a_generous_profile(self):
        def generous(doc):
            doc["profiles"][0].update(ttl_ms=90_000_000, max_calls=100,
                                      expected_calls=100)
        self.reimport(generous)
        self.assert_code(errors.CODE_INVALID_REQUEST, ttl_ms=90_000_000)
        self.assert_code(errors.CODE_INVALID_REQUEST, max_calls=101)
        self.assert_code(errors.CODE_INVALID_REQUEST, expected_calls=3,
                         max_calls=2)

    def test_ceiling_is_checked_after_unknown_class_and_candidate(self):
        self.assert_code(errors.CODE_NOT_FOUND, task_class="ghost",
                         ttl_ms=PROFILE["ttl_ms"] + 1)


class ReplayAfterProfileEdit(_T2):
    """replay_after_profile_edit: a byte-identical replay returns the
    stored lease before any profile check; a granted lease never changes."""

    def test_replay_survives_profile_changes_and_removal(self):
        ids = _ids(self)
        now = int(time.time() * 1000)
        body = request(ids, now_ms=now)
        first = lease.grant(self.db, body, now)
        stored = self.rows("SELECT * FROM leases")
        self.assertFalse(first["replayed"])

        def tighten(doc):
            doc["profiles"][0].update(candidates=["fast", "park"],
                                      margin_bps=20000, ttl_ms=1,
                                      expected_calls=1, max_calls=1,
                                      output_cap=1, per_call_output_cap=1,
                                      policy_revision=9)
        self.reimport(tighten)
        replay = lease.grant(self.db, copy.deepcopy(body), now + 1000)
        self.assertTrue(replay["replayed"])
        self.assertEqual({k: v for k, v in replay.items() if k != "replayed"},
                         {k: v for k, v in first.items() if k != "replayed"})
        self.assertEqual(self.rows("SELECT * FROM leases"), stored)

        self.reimport(lambda doc: doc.update(profiles=[]))
        replay = lease.grant(self.db, copy.deepcopy(body), now + 2000)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["lease_id"], first["lease_id"])
        self.assertEqual(self.rows("SELECT * FROM leases"), stored)
        # The stored policy snapshot kept the revision it was granted under.
        snapshot = json.loads(self.rows(
            "SELECT policy_snapshot FROM leases")[0][0])
        self.assertEqual(snapshot["policy_revision"], 1)

    def test_a_new_request_after_the_edit_meets_the_new_profile(self):
        self.grant()
        self.reimport(lambda doc: doc["profiles"][0].update(
            candidates=["fast", "park"]))
        self.assert_code(errors.CODE_POLICY_DENIED, model="opus")


class ContentHashIncludesTaskClass(_T2):
    """content_hash_includes_task_class: the same request_id with a
    different task_class is an idempotency conflict."""

    def test_same_request_id_different_task_class_conflicts(self):
        def two(doc):
            second = copy.deepcopy(doc["profiles"][0])
            second["task_class"] = "implement"
            doc["profiles"].append(second)
        self.reimport(two)
        ids = _ids(self)
        now = int(time.time() * 1000)
        first = request(ids, now_ms=now)
        lease.grant(self.db, first, now)
        other = copy.deepcopy(first)
        other["task_class"] = "implement"
        with self.assertRaises(QuotaError) as caught:
            lease.grant(self.db, other, now)
        self.assertEqual(caught.exception.code,
                         errors.CODE_IDEMPOTENCY_CONFLICT)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM leases"), [(1,)])
        # The identical request still replays.
        self.assertTrue(lease.grant(self.db, copy.deepcopy(first),
                                    now)["replayed"])


class HttpPath(_T2):
    """/v1/leases with a profile, end to end on rows imported by
    config-import v2."""

    def start(self):
        self.assertEqual(run_cli(
            "--db", self.db, "identity-set", "--machine", "desktop",
            "--identity-ref", "identity-desktop", "--enabled",
            "true").returncode, 0)
        self.server = http_api.make_server(self.db, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def wire(self, **kw):
        body = request(_ids(self), **kw)
        del body["request_hash"]
        return body

    def post(self, base, body):
        return _call(base, "/v1/leases", body, body["request_id"])

    def test_grant_over_http_stores_the_task_class(self):
        base = self.start()
        status, granted = self.post(base, self.wire())
        self.assertEqual(status, 201, granted)
        self.assertEqual(self.rows(
            "SELECT task_class FROM leases WHERE id=?",
            (granted["lease_id"],)), [("bounded_read",)])

    def test_http_errors_follow_the_profile(self):
        base = self.start()
        status, err = self.post(base, self.wire(task_class="ghost"))
        self.assertEqual((status, err["error"]["code"]), (404, "NOT_FOUND"))
        self.reimport(lambda doc: doc["profiles"][0].update(
            candidates=["fast", "park"]))
        status, err = self.post(base, self.wire())
        self.assertEqual(err["error"]["code"], "POLICY_DENIED")
        self.assertGreaterEqual(status, 400)
        status, err = self.post(base, self.wire(model="fast",
                                                ttl_ms=PROFILE["ttl_ms"] + 1))
        self.assertEqual(err["error"]["code"], "POLICY_DENIED")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM leases"), [(0,)])

    def test_http_replay_returns_the_stored_lease_after_a_profile_edit(self):
        base = self.start()
        body = self.wire()
        status, first = self.post(base, body)
        self.assertEqual(status, 201, first)
        self.reimport(lambda doc: doc.update(profiles=[]))
        status, again = self.post(base, body)
        self.assertEqual(status, 200, again)
        self.assertEqual(again["lease_id"], first["lease_id"])


if __name__ == "__main__":
    unittest.main()
