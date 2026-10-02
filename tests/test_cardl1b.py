"""Card L1b: the shadow-request contract (docs/DESIGN.md sections 3 and 7;
docs/STEP-CARD-L1B.md).

Rows: unknown_context_shadow_granted, unknown_context_non_shadow_refused,
unknown_context_mixed_fields_invalid, unknown_context_no_fabrication,
non_money_reporting, non_money_null_release, the HTTP path and privacy_l1b.
Every store is built through config-import v2. Synthetic data only.
"""

import copy
import json
import sqlite3
import threading
import time
import unittest
import uuid
from unittest import mock

from quota_balancer import errors, http_api, lease
from quota_balancer.errors import QuotaError

from tests.test_cardl1a import _Base, _call, _ids, _model, base_doc, run_cli
from tests.test_cardl1a_t2 import request

MESSAGE = "an unknown context is accepted for shadow leases only"
CONTEXT_CEILING = 180000           # the base_doc model ceiling


def doc():
    """base_doc plus a subscription account with a model, two subscription
    seats and a second shadow seat on the desktop (the HTTP identity)."""
    d = base_doc()
    d["limits"] = []
    d["models"].append(_model("other", "sub-model", ["sub-model"]))
    d["project_accounts"].append({"project": "org", "account": "other"})
    d["profiles"][0]["candidates"] = ["opus", "fast", "sub-model", "park"]
    team = {"project": "org", "team": "dev"}
    d["seats"] += [
        {"team": team, "machine": "worker", "alias": "s4", "role": "main",
         "enforcement": "subscription_admission"},
        {"team": team, "machine": "worker", "alias": "s5", "role": "main",
         "enforcement": "subscription_warn"},
        {"team": team, "machine": "desktop", "alias": "s6", "role": "main",
         "enforcement": "shadow"},
    ]
    return d


def unknown_context(now_ms):
    return {"mode": "UNKNOWN", "initial_tokens": None,
            "dispatch_tokens": None, "source": "unknown",
            "observed_at": now_ms - 1000, "growth_tokens_per_call": None}


class _L1b(_Base):
    def setUp(self):
        super().setUp()
        self.import_ok(doc())

    def now(self):
        return int(time.time() * 1000)

    @staticmethod
    def model_for(seat, model):
        # subscription seats belong to the subscription account's model
        return model or ("sub-model" if seat in ("s4", "s5") else "opus")

    def unknown(self, seat="s6", model=None, now_ms=None, **override):
        """An unknown-context request for `seat` (no gateway: only a
        metered seat on a gateway needs one)."""
        now_ms = now_ms or self.now()
        model = self.model_for(seat, model)
        body = request(_ids(self), seat=seat, model=model, now_ms=now_ms,
                       gateway_id=None, **override)
        body["context"] = unknown_context(now_ms)
        return body, now_ms

    def known(self, seat, model=None, now_ms=None, **override):
        now_ms = now_ms or self.now()
        model = self.model_for(seat, model)
        gateway = _ids(self)["gateways"]["g1"] if seat in ("s1", "s3") else None
        return request(_ids(self), seat=seat, model=model, now_ms=now_ms,
                       gateway_id=gateway, **override), now_ms

    def grant(self, body, now_ms):
        return lease.grant(self.db, body, now_ms)

    def assert_code(self, code, body, now_ms):
        before = self.rows("SELECT COUNT(*) FROM leases")
        with self.assertRaises(QuotaError) as caught:
            lease.grant(self.db, body, now_ms)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM leases"), before)
        return caught.exception

    def receipt(self, lease_id, final, event_id=None, now_ms=None,
                actual=None, gateway=None):
        now_ms = now_ms or self.now()
        return {"event_id": event_id or str(uuid.uuid4()),
                "lease_id": lease_id, "outcome": "completed", "calls": 1,
                "input_tokens": 100, "cached_input_tokens": 0,
                "output_tokens": 10, "actual_micro_eur": actual,
                "gateway_id": gateway, "seq": 1, "final": 1 if final else 0,
                "observed_at": now_ms, "received_at": now_ms}


class UnknownContextShadowGranted(_L1b):
    """unknown_context_shadow_granted / unknown_context_no_fabrication."""

    def test_a_shadow_seat_is_granted_an_unknown_context(self):
        body, now = self.unknown()
        granted = self.grant(body, now)
        self.assertEqual(granted["state"], "active")
        self.assertEqual(granted["enforcement"], "shadow")
        self.assertIsNone(granted["money_hold"])
        row = self.rows(
            "SELECT context_mode, token_hash, money_hold, enforcement, "
            "state FROM leases WHERE id=?", (granted["lease_id"],))[0]
        self.assertEqual(row, ("UNKNOWN", None, None, "shadow", "active"))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM holds"), [(0,)])

    def test_nothing_is_fabricated_in_storage(self):
        body, now = self.unknown()
        granted = self.grant(body, now)
        estimate, snapshot, input_cap, max_calls = self.rows(
            "SELECT estimate, policy_snapshot, input_cap, max_calls "
            "FROM leases WHERE id=?", (granted["lease_id"],))[0]
        self.assertEqual(json.loads(estimate), {"context": "unknown"})
        # the honest upper bound: the model's context ceiling per call
        self.assertEqual(input_cap, CONTEXT_CEILING * max_calls)
        self.assertEqual(granted["caps"]["input_cap"],
                         CONTEXT_CEILING * body["max_calls"])
        self.assertIsNone(granted["caps"]["expected_input"])
        snap = json.loads(snapshot)
        self.assertEqual(snap["input_cap_basis"], "model_ceiling")
        self.assertIsNone(snap["caps"]["hold_sized"])
        self.assertEqual(snap["task_class"], "bounded_read")
        for forbidden in ("expected_input", "initial_tokens", "growth"):
            self.assertNotIn(forbidden, estimate)

    def test_the_bound_follows_max_calls(self):
        body, now = self.unknown(max_calls=1)
        granted = self.grant(body, now)
        self.assertEqual(granted["caps"]["input_cap"], CONTEXT_CEILING)

    def test_a_known_context_keeps_the_estimate_and_no_basis_key(self):
        body, now = self.known("s6")
        granted = self.grant(body, now)
        row = self.rows("SELECT context_mode, estimate, policy_snapshot "
                        "FROM leases WHERE id=?", (granted["lease_id"],))[0]
        self.assertEqual(row[0], "FRESH")
        self.assertIn("expected_input", json.loads(row[1]))
        self.assertNotIn("input_cap_basis", json.loads(row[2]))

    def test_a_shadow_lease_takes_no_slot(self):
        for _ in range(3):
            body, now = self.unknown()
            self.assertEqual(self.grant(body, now)["state"], "active")
        self.assertEqual(self.rows(
            "SELECT COUNT(*) FROM leases WHERE enforcement='shadow'"),
            [(3,)])

    def test_the_profile_still_bounds_an_unknown_context_request(self):
        body, now = self.unknown(ttl_ms=1_800_001)
        self.assert_code(errors.CODE_POLICY_DENIED, body, now)
        body, now = self.unknown(task_class="ghost")
        self.assert_code(errors.CODE_NOT_FOUND, body, now)
        def only_fast(d):
            d["profiles"][0]["candidates"] = ["fast", "park"]
        d = doc()
        only_fast(d)
        self.import_ok(d)
        body, now = self.unknown(model="opus")
        self.assert_code(errors.CODE_POLICY_DENIED, body, now)

    def test_the_per_call_cap_and_total_output_bounds_still_apply(self):
        d = doc()
        d["profiles"][0].update(output_cap=10 ** 9, per_call_output_cap=10 ** 9,
                                max_calls=2)
        self.import_ok(d)
        body, now = self.unknown(per_call_output_cap=18001)
        self.assert_code(errors.CODE_INVALID_REQUEST, body, now)
        body, now = self.unknown(output_cap=2 * 18000 + 1)
        self.assert_code(errors.CODE_INVALID_REQUEST, body, now)
        body, now = self.unknown(output_cap=2 * 18000, per_call_output_cap=18000)
        self.assertEqual(self.grant(body, now)["state"], "active")

    def test_observed_at_must_still_be_fresh(self):
        body, now = self.unknown()
        body["context"]["observed_at"] = now - 120_001
        self.assert_code(errors.CODE_ESTIMATE_STALE, body, now)
        body["context"]["observed_at"] = now + 30_001
        self.assert_code(errors.CODE_ESTIMATE_STALE, body, now)
        body["context"]["observed_at"] = now - 120_000
        self.assertEqual(self.grant(body, now)["state"], "active")

    def test_a_replay_returns_the_stored_lease_before_any_context_check(self):
        body, now = self.unknown()
        first = self.grant(body, now)
        # The seat is later reconfigured to a non-shadow mode: the stored
        # lease still comes back (replay is matched first).
        d = doc()
        for seat in d["seats"]:
            if seat["alias"] == "s6":
                seat["enforcement"] = "metered_advisory"
        self.import_ok(d)
        replay = self.grant(copy.deepcopy(body), now + 1000)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["lease_id"], first["lease_id"])
        # A NEW unknown-context request on that seat is refused.
        other, now2 = self.unknown()
        self.assert_code("CONTEXT_UNKNOWN", other, now2)


class UnknownContextNonShadowRefused(_L1b):
    """unknown_context_non_shadow_refused: CONTEXT_UNKNOWN, static."""

    def test_every_non_shadow_derived_enforcement_is_refused(self):
        for seat in ("s1", "s3", "s4", "s5"):
            body, now = self.unknown(seat=seat)
            if seat in ("s1", "s3"):
                body["gateway_id"] = _ids(self)["gateways"]["g1"]
            exc = self.assert_code("CONTEXT_UNKNOWN", body, now)
            self.assertEqual(exc.message, MESSAGE)
            self.assertIsNone(exc.retry_at)

    def test_a_metered_seat_with_a_ready_gateway_is_still_refused(self):
        from tests.test_cardl1a import _sized_snapshot
        _sized_snapshot(self)
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            body, now = self.unknown(seat="s3")
            body["gateway_id"] = _ids(self)["gateways"]["g1"]
            self.assert_code("CONTEXT_UNKNOWN", body, now)

    def test_the_check_comes_before_the_profile_and_the_arithmetic(self):
        body, now = self.unknown(seat="s1", task_class="ghost",
                                 ttl_ms=1_800_001)
        body["gateway_id"] = _ids(self)["gateways"]["g1"]
        self.assert_code("CONTEXT_UNKNOWN", body, now)

    def test_the_error_is_in_the_closed_set_with_its_mappings(self):
        self.assertEqual(errors.CODE_CONTEXT_UNKNOWN, "CONTEXT_UNKNOWN")
        self.assertEqual(errors.MSG_CONTEXT_UNKNOWN, MESSAGE)
        self.assertEqual(errors.EXIT_CODES["CONTEXT_UNKNOWN"], 2)
        self.assertEqual(http_api._HTTP_STATUS["CONTEXT_UNKNOWN"], 400)

    def test_a_known_context_on_every_seat_still_works(self):
        for seat in ("s1", "s4", "s5", "s6"):
            body, now = self.known(seat)
            self.assertEqual(self.grant(body, now)["state"], "active", seat)


class UnknownContextMixedFieldsInvalid(_L1b):
    """unknown_context_mixed_fields_invalid: the shape stays closed."""

    def test_every_mixture_is_invalid_request(self):
        now = self.now()

        def variants():
            def mut(fn):
                body, _ = self.unknown(now_ms=now)
                fn(body["context"])
                return body
            yield "tokens with UNKNOWN", mut(lambda c: c.update(initial_tokens=5))
            yield "dispatch with UNKNOWN", mut(lambda c: c.update(dispatch_tokens=0))
            yield "growth with UNKNOWN", mut(lambda c: c.update(growth_tokens_per_call=0))
            yield "known source", mut(lambda c: c.update(source="tokenizer"))
            yield "conservative_bound", mut(lambda c: c.update(source="conservative_bound"))
            yield "no observed_at", mut(lambda c: c.pop("observed_at"))
            yield "null observed_at", mut(lambda c: c.update(observed_at=None))
            yield "string observed_at", mut(lambda c: c.update(observed_at="5"))
            yield "bool observed_at", mut(lambda c: c.update(observed_at=True))
            yield "extra key", mut(lambda c: c.update(extra=1))
            yield "lowercase mode", mut(lambda c: c.update(mode="unknown"))
            yield "list mode", mut(lambda c: c.update(mode=["UNKNOWN"]))
            yield "missing growth", mut(lambda c: c.pop("growth_tokens_per_call"))

            def known(fn):
                body, _ = self.known("s6", now_ms=now)
                fn(body["context"])
                return body
            yield "FRESH with null initial", known(lambda c: c.update(initial_tokens=None))
            yield "FRESH with null dispatch", known(lambda c: c.update(dispatch_tokens=None))
            yield "FRESH with null growth", known(lambda c: c.update(growth_tokens_per_call=None))
            yield "FRESH with source unknown", known(lambda c: c.update(source="unknown"))
            yield "RESUME with source unknown", known(lambda c: c.update(mode="RESUME", source="unknown"))

        count = 0
        for label, body in variants():
            exc = self.assert_code(errors.CODE_INVALID_REQUEST, body, now)
            self.assertEqual(exc.message, errors.MSG_INVALID_REQUEST, label)
            count += 1
        self.assertGreaterEqual(count, 18)

    def test_a_mixture_is_invalid_even_on_a_non_shadow_seat(self):
        body, now = self.unknown(seat="s4")
        body["context"]["initial_tokens"] = 5
        self.assert_code(errors.CODE_INVALID_REQUEST, body, now)

    def test_unknown_values_never_validate_as_known_ones(self):
        body, now = self.known("s6")
        body["context"]["mode"] = "UNKNOWN"
        self.assert_code(errors.CODE_INVALID_REQUEST, body, now)


class NonMoneyReporting(_L1b):
    """non_money_reporting and non_money_null_release: the settle contract
    for leases that reserved no money."""

    KINDS = (("shadow", "s6"), ("advisory", "s1"), ("subscription", "s4"),
             ("subscription_warn", "s5"))

    def grant_for(self, seat, now_ms=None):
        if seat == "s6":
            body, now = self.unknown(now_ms=now_ms)
        else:
            body, now = self.known(seat, now_ms=now_ms)
        return self.grant(body, now), now

    def test_per_attempt_then_final_with_null_money(self):
        for kind, seat in self.KINDS:
            made, now = self.grant_for(seat)
            lease_id = made["lease_id"]
            e1, e2, final = (str(uuid.uuid4()) for _ in range(3))
            r1 = lease.settle(self.db, self.receipt(lease_id, False, e1, now), now)
            self.assertEqual(r1, {"state": "active", "released_micro_eur": None,
                                  "retained_micro_eur": None, "replayed": False},
                             kind)
            lease.settle(self.db, self.receipt(lease_id, False, e2, now), now)
            self.assertEqual(self.rows("SELECT state FROM leases WHERE id=?",
                                       (lease_id,)), [("active",)])
            done = lease.settle(self.db, self.receipt(lease_id, True, final, now), now)
            self.assertEqual(done, {"state": "settled", "released_micro_eur": None,
                                    "retained_micro_eur": None,
                                    "replayed": False}, kind)
            self.assertEqual(self.rows(
                "SELECT COUNT(*), SUM(final) FROM receipts WHERE lease_id=?",
                (lease_id,)), [(3, 1)])
            self.assertEqual(self.rows("SELECT COUNT(*) FROM holds"), [(0,)])

    def test_a_non_final_after_a_final_is_receipt_conflict(self):
        made, now = self.grant_for("s6")
        lease.settle(self.db, self.receipt(made["lease_id"], True, None, now), now)
        before = self.rows("SELECT COUNT(*) FROM receipts")
        with self.assertRaises(QuotaError) as caught:
            lease.settle(self.db, self.receipt(made["lease_id"], False, None, now),
                         now)
        self.assertEqual(caught.exception.code, errors.CODE_RECEIPT_CONFLICT)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM receipts"), before)
        # a non-final conflict does not park the account
        self.assertEqual(self.rows("SELECT COUNT(*) FROM blocks"), [(0,)])

    def test_an_identical_replay_is_a_noop_with_null_money(self):
        for kind, seat in self.KINDS:
            made, now = self.grant_for(seat)
            lease_id = made["lease_id"]
            non_final = self.receipt(lease_id, False, None, now)
            final = self.receipt(lease_id, True, None, now)
            lease.settle(self.db, non_final, now)
            lease.settle(self.db, final, now)
            count = self.rows("SELECT COUNT(*) FROM receipts")
            for receipt in (non_final, final):
                again = lease.settle(self.db, dict(receipt, received_at=now + 5),
                                     now + 5)
                self.assertTrue(again["replayed"], kind)
                self.assertIsNone(again["released_micro_eur"])
                self.assertIsNone(again["retained_micro_eur"])
            self.assertEqual(self.rows("SELECT COUNT(*) FROM receipts"), count)

    def test_a_changed_replay_is_an_idempotency_conflict(self):
        made, now = self.grant_for("s6")
        receipt = self.receipt(made["lease_id"], False, None, now)
        lease.settle(self.db, receipt, now)
        with self.assertRaises(QuotaError) as caught:
            lease.settle(self.db, dict(receipt, output_tokens=11), now)
        self.assertEqual(caught.exception.code, errors.CODE_IDEMPOTENCY_CONFLICT)

    def test_a_non_final_after_the_ttl_is_refused_a_final_is_accepted(self):
        made, now = self.grant_for("s6")
        late = made["expires_at"]
        with self.assertRaises(QuotaError) as caught:
            lease.settle(self.db, self.receipt(made["lease_id"], False, None, late),
                         late)
        self.assertEqual(caught.exception.code, errors.CODE_LEASE_EXPIRED)
        done = lease.settle(self.db, self.receipt(made["lease_id"], True, None, late),
                            late)
        self.assertEqual(done["state"], "settled")
        self.assertIsNone(done["released_micro_eur"])

    def test_metered_leases_keep_integer_money_fields(self):
        from tests.test_cardl1a import _sized_snapshot
        _sized_snapshot(self)
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            body, now = self.known("s3")
            made = self.grant(body, now)
        self.assertEqual(made["enforcement"], "metered")
        gateway = _ids(self)["gateways"]["g1"]
        part = lease.settle(self.db, self.receipt(
            made["lease_id"], False, None, now, actual=5, gateway=gateway), now)
        self.assertEqual((part["released_micro_eur"], part["retained_micro_eur"]),
                         (0, 0))
        final = lease.settle(self.db, self.receipt(
            made["lease_id"], True, None, now, actual=5, gateway=gateway), now)
        self.assertEqual(final["retained_micro_eur"], 5)
        self.assertEqual(final["released_micro_eur"], made["money_hold"] - 5)
        # a final metered receipt without money is still refused
        body, now = self.known("s3", turn_id=str(uuid.uuid4()))
        with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", True):
            second = self.grant(body, now)
        with self.assertRaises(QuotaError) as caught:
            lease.settle(self.db, self.receipt(
                second["lease_id"], True, None, now, gateway=gateway), now)
        self.assertEqual(caught.exception.code, errors.CODE_INVALID_REQUEST)


class HttpPath(_L1b):
    """The wire, end to end, on rows imported by config-import v2."""

    def setUp(self):
        super().setUp()
        self.assertEqual(run_cli(
            "--db", self.db, "identity-set", "--machine", "desktop",
            "--identity-ref", "identity-desktop", "--enabled", "true").returncode, 0)
        self.server = http_api.make_server(self.db, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def wire_grant(self, seat, unknown):
        now = self.now()
        body = request(_ids(self), seat=seat, now_ms=now, gateway_id=(
            None if seat == "s6" else _ids(self)["gateways"]["g1"]))
        del body["request_hash"]
        if unknown:
            body["context"] = unknown_context(now)
        return body

    def post(self, path, body):
        key = body.get("request_id") or body["event_id"]
        return _call(self.base, path, body, key)

    def wire_receipt(self, final):
        return {"event_id": str(uuid.uuid4()), "outcome": "completed",
                "calls": 1, "input_tokens": 5, "cached_input_tokens": 0,
                "output_tokens": 1, "actual_micro_eur": None,
                "gateway_id": None, "seq": 1, "final": final}

    def test_a_shadow_grant_with_an_unknown_context_over_http(self):
        status, body = self.post("/v1/leases", self.wire_grant("s6", True))
        self.assertEqual(status, 201, body)
        self.assertEqual(body["enforcement"], "shadow")
        self.assertIsNone(body["lease_token"])
        self.assertIsNone(body["money_hold"])
        self.assertIsNone(body["caps"]["expected_input"])
        self.assertEqual(body["caps"]["input_cap"], CONTEXT_CEILING * 2)
        self.assertEqual(self.rows("SELECT context_mode FROM leases"),
                         [("UNKNOWN",)])

    def test_a_non_shadow_seat_gets_400_context_unknown_over_http(self):
        status, body = self.post("/v1/leases", self.wire_grant("s1", True))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "CONTEXT_UNKNOWN")
        self.assertEqual(body["error"]["message"], MESSAGE)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM leases"), [(0,)])

    def test_a_mixed_wire_context_is_400_invalid_request(self):
        wire = self.wire_grant("s6", True)
        wire["context"]["initial_tokens"] = 5
        status, body = self.post("/v1/leases", wire)
        self.assertEqual((status, body["error"]["code"]), (400, "INVALID_REQUEST"))

    def test_per_attempt_and_final_reports_over_http(self):
        status, made = self.post("/v1/leases", self.wire_grant("s6", True))
        path = "/v1/leases/%s/settle" % made["lease_id"]
        first = self.wire_receipt(False)
        status, one = self.post(path, first)
        self.assertEqual(status, 200, one)
        self.assertEqual(one["state"], "active")
        self.assertIsNone(one["released_micro_eur"])
        self.assertIsNone(one["retained_micro_eur"])
        status, replay = self.post(path, first)
        self.assertEqual(status, 200)
        self.assertIsNone(replay["released_micro_eur"])
        status, final = self.post(path, self.wire_receipt(True))
        self.assertEqual((status, final["state"]), (200, "settled"))
        self.assertIsNone(final["released_micro_eur"])
        status, after = self.post(path, self.wire_receipt(False))
        self.assertEqual((status, after["error"]["code"]), (409, "RECEIPT_CONFLICT"))

    def test_the_settle_wire_keeps_its_closed_shape(self):
        status, made = self.post("/v1/leases", self.wire_grant("s6", True))
        status, body = self.post("/v1/leases/%s/settle" % made["lease_id"],
                                 self.wire_receipt(True))
        for key in ("state", "released_micro_eur", "retained_micro_eur"):
            self.assertIn(key, body)


class UnknownContextInputCapBoundary(_L1b):
    """Fix round 1 (tk-e17c89165391): the unknown-context input bound
    (context ceiling x allowed calls) is checked signed-64 arithmetic, so an
    extreme but accepted ceiling is a controlled refusal, never a raw error."""

    def with_ceiling(self, ceiling):
        d = doc()
        d["models"][0]["context_ceiling"] = ceiling
        self.import_ok(d)

    def counts(self):
        return (self.rows("SELECT COUNT(*) FROM leases"),
                self.rows("SELECT COUNT(*) FROM holds"),
                self.rows("SELECT COUNT(*) FROM audit"))

    def test_a_product_beyond_signed_64_is_invalid_request(self):
        self.with_ceiling(2 ** 62)                  # 4611686018427387904
        before = self.counts()
        body, now = self.unknown(max_calls=2)
        exc = self.assert_code(errors.CODE_INVALID_REQUEST, body, now)
        self.assertEqual(exc.message, errors.MSG_INVALID_REQUEST)
        self.assertEqual(self.counts(), before)

    def test_the_largest_product_that_fits_is_granted(self):
        self.with_ceiling(2 ** 62 - 1)              # 4611686018427387903
        body, now = self.unknown(max_calls=2)
        made = self.grant(body, now)
        self.assertEqual(made["caps"]["input_cap"], 2 ** 63 - 2)
        self.assertEqual(self.rows("SELECT input_cap FROM leases"),
                         [(9223372036854775806,)])

    def test_other_call_counts_at_the_edge(self):
        self.with_ceiling(2 ** 63 - 1)
        body, now = self.unknown(max_calls=1)       # exactly the maximum
        self.assertEqual(self.grant(body, now)["caps"]["input_cap"], 2 ** 63 - 1)
        body, now = self.unknown(max_calls=2)       # one call too many
        self.assert_code(errors.CODE_INVALID_REQUEST, body, now)

    def test_over_http_the_refusal_is_400_not_503(self):
        import threading
        from tests.test_cardl1a import _call
        self.assertEqual(run_cli(
            "--db", self.db, "identity-set", "--machine", "desktop",
            "--identity-ref", "identity-desktop", "--enabled",
            "true").returncode, 0)
        self.with_ceiling(2 ** 62)
        server = http_api.make_server(self.db, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%d" % server.server_address[1]
            now = self.now()
            wire = request(_ids(self), seat="s6", now_ms=now, gateway_id=None)
            del wire["request_hash"]
            wire["context"] = unknown_context(now)
            before = self.counts()
            status, answer = _call(base, "/v1/leases", wire, wire["request_id"])
            self.assertEqual(status, 400, answer)
            self.assertEqual(answer["error"]["code"], "INVALID_REQUEST")
            self.assertEqual(self.counts(), before)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_a_known_context_path_is_unchanged_for_the_same_model(self):
        self.with_ceiling(2 ** 62)
        body, now = self.known("s6", max_calls=2)
        made = self.grant(body, now)             # estimate path, own bounds
        self.assertEqual(made["state"], "active")


class MarkerInteractionWithL1a(_L1b):
    """Card L1a's replay format marker (task_class in the policy snapshot)
    and its token rule must keep working with the unknown-context path."""

    @staticmethod
    def legacy_hash(body):
        import hashlib
        canonical = {k: v for k, v in body.items()
                     if k not in ("request_hash", "task_class")}
        return hashlib.sha256(json.dumps(
            canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def test_an_unknown_context_snapshot_carries_the_l1a_marker(self):
        body, now = self.unknown()
        made = self.grant(body, now)
        snapshot = json.loads(self.rows(
            "SELECT policy_snapshot FROM leases WHERE id=?",
            (made["lease_id"],))[0][0])
        self.assertEqual(snapshot["task_class"], "bounded_read")
        self.assertEqual(snapshot["policy_revision"], 1)
        self.assertEqual(snapshot["input_cap_basis"], "model_ceiling")
        # so the current fingerprint is used on its replay
        stored = self.rows("SELECT request_hash FROM leases")[0][0]
        self.assertEqual(stored, lease._content_hash(body))
        self.assertTrue(self.grant(copy.deepcopy(body), now + 1)["replayed"])
        other = dict(copy.deepcopy(body), task_class="implement")
        with self.assertRaises(QuotaError) as caught:
            self.grant(other, now)
        self.assertEqual(caught.exception.code,
                         errors.CODE_IDEMPOTENCY_CONFLICT)

    def test_a_pre_l1a_shadow_lease_replays_before_the_context_checks(self):
        body, now = self.known("s6")
        made = self.grant(body, now)
        snapshot = json.loads(self.rows(
            "SELECT policy_snapshot FROM leases WHERE id=?",
            (made["lease_id"],))[0][0])
        snapshot.pop("task_class")
        snapshot.pop("policy_revision")
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE leases SET request_hash=?, task_class='opus', "
                     "policy_snapshot=? WHERE id=?",
                     (self.legacy_hash(body),
                      json.dumps(snapshot, sort_keys=True), made["lease_id"]))
        conn.commit()
        conn.close()
        # The seat is later reconfigured to a non-shadow mode: the identical
        # request still replays the stored lease (the versioned comparison
        # runs first, before CONTEXT_UNKNOWN or any profile check).
        d = doc()
        for seat in d["seats"]:
            if seat["alias"] == "s6":
                seat["enforcement"] = "metered_advisory"
        self.import_ok(d)
        replay = self.grant(copy.deepcopy(body), now + 1000)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["lease_id"], made["lease_id"])

    def test_shadow_leases_stay_non_token_bearing(self):
        for gate in (False, True):
            with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", gate):
                self.assertFalse(lease.token_bearing("shadow"))
                self.assertFalse(lease.token_bearing("metered_advisory"))

    def test_a_shadow_unknown_context_lease_cannot_pass_check(self):
        self.assertEqual(run_cli(
            "--db", self.db, "identity-set", "--machine", "desktop",
            "--identity-ref", "identity-desktop", "--enabled",
            "true").returncode, 0)
        now = self.now()
        wire = request(_ids(self), seat="s6", now_ms=now, gateway_id=None)
        del wire["request_hash"]
        wire["context"] = unknown_context(now)
        identity = (self.ids("machines")["desktop"], "desktop")
        status, made = http_api.handle_grant(self.db, identity, wire, now,
                                             wire["request_id"])
        self.assertEqual(status, 201)
        self.assertIsNone(made["lease_token"])
        for gate in (False, True):
            with mock.patch.object(lease, "METERED_ENFORCEMENT_ACTIVATED", gate):
                with self.assertRaises(QuotaError) as caught:
                    http_api.handle_check(
                        self.db, identity, made["lease_id"],
                        {"request_id": str(uuid.uuid4())}, "any-token", now)
                self.assertEqual(caught.exception.code, errors.CODE_FORBIDDEN)
        status, replay = http_api.handle_grant(self.db, identity, wire, now,
                                               wire["request_id"])
        self.assertEqual(status, 200)
        self.assertIsNone(replay["lease_token"])


class PrivacyL1b(_L1b):
    """privacy_l1b: nothing from a rejected request is echoed or stored."""

    SENTINELS = ("SENTINEL-SECRET-9f2c", "C:\\Users\\sentinel\\secret.txt",
                 "/etc/sentinel-passwd", "sk-sentinel-key-0001")

    def test_sentinels_in_the_context_are_never_echoed_or_stored(self):
        for sentinel in self.SENTINELS:
            for field in ("mode", "source", "observed_at", "initial_tokens"):
                body, now = self.unknown()
                body["context"][field] = sentinel
                with self.assertRaises(QuotaError) as caught:
                    lease.grant(self.db, body, now)
                text = (caught.exception.message + repr(caught.exception)
                        + str(caught.exception.code))
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

    def test_the_context_unknown_refusal_carries_no_request_text(self):
        body, now = self.unknown(seat="s4")
        body["task_class"] = "bounded_read"
        exc = self.assert_code("CONTEXT_UNKNOWN", body, now)
        for needle in (body["request_id"], body["turn_id"], "s4", "opus"):
            self.assertNotIn(needle, exc.message)

    def test_stored_unknown_context_rows_hold_only_closed_text(self):
        body, now = self.unknown()
        made = self.grant(body, now)
        estimate, snapshot = self.rows(
            "SELECT estimate, policy_snapshot FROM leases WHERE id=?",
            (made["lease_id"],))[0]
        self.assertEqual(json.loads(estimate), {"context": "unknown"})
        self.assertEqual(json.loads(snapshot)["input_cap_basis"], "model_ceiling")

    def test_a_wire_sentinel_in_the_mode_is_not_echoed(self):
        self.assertEqual(run_cli(
            "--db", self.db, "identity-set", "--machine", "desktop",
            "--identity-ref", "identity-desktop", "--enabled", "true").returncode, 0)
        server = http_api.make_server(self.db, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%d" % server.server_address[1]
            now = self.now()
            body = request(_ids(self), seat="s6", now_ms=now, gateway_id=None)
            del body["request_hash"]
            body["context"] = unknown_context(now)
            body["context"]["mode"] = self.SENTINELS[0]
            status, answer = _call(base, "/v1/leases", body, body["request_id"])
            self.assertEqual(status, 400)
            self.assertNotIn(self.SENTINELS[0], json.dumps(answer))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
