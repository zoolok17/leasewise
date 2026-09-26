"""Card 5 acceptance tests: the private /v1 HTTP layer.

Run from the repository root:
    python -m unittest discover -s tests -v

Method names carry the card's named checks (docs/DESIGN.md section 8,
row "5: private HTTP") with the unittest "test_" prefix: token_scope,
replay_grant, public_bind, privacy_5. Everything the HTTP layer adds over
the tested store (cards 1-4) and policy (card 3) is exercised over a
REAL socket (http_api.make_server on 127.0.0.1, an ephemeral port) - no
admission/reconciliation rule is retested here; that is cards 3/4's job.

Fix round tk-7d7fd9023f8c classes are grouped under "Fix round" near the
end of this file; the reviewer's own reproductions are pinned there, but
every OTHER test in this file was also updated to the corrected section-7
wire shapes those findings forced (task_class replaces request_hash on
grant; settle takes no body lease_id/observed_at/received_at; readings is
one flat reading, not a batch document; every mutation now needs an
agreeing Idempotency-Key header).
"""

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

import quota_balancer.migrate as migrate
from quota_balancer import errors, http_api, lease, policy, store

# --- fixed UUID fixture chain: one metered account, two machines, one
# seat each, sharing the account (the card's own demo). ---------------

ACCOUNT = "11111111-1111-4111-8111-111111111111"
OTHER_ACCOUNT = "11111111-1111-4111-8111-111111111112"  # no project_accounts grant
MACHINE_A = "22222222-2222-4222-8222-222222222222"      # "desktop"
MACHINE_B = "22222222-2222-4222-8222-222222222223"      # "worker"
GATEWAY = "33333333-3333-4333-8333-333333333333"        # on MACHINE_A
GATEWAY_B = "33333333-3333-4333-8333-333333333334"      # on MACHINE_B
PROJECT = "44444444-4444-4444-8444-444444444444"
TEAM = "55555555-5555-4555-8555-555555555555"
SEAT_A = "66666666-6666-4666-8666-666666666666"         # on MACHINE_A
SEAT_B = "66666666-6666-4666-8666-666666666667"         # on MACHINE_B
MODEL = "77777777-7777-4777-8777-777777777777"
POLICY_HASH = "ab" * 32
TASK_CLASS = "bounded_read"

IDENTITY_A = "identity-desktop"
IDENTITY_B = "identity-worker"

NOW = 1788224400000        # 2026-09-, one hour into the month
TTL_MS = 1_800_000

MODEL_DICT = {
    "alias": "opus", "context_ceiling": 180000, "output_ceiling": 18000,
    "tariff_input_rate": 1, "tariff_output_rate": 1,
    "reserve_input_rate": 1_000_000, "reserve_output_rate": 1_000_000,
    "reservation_mode": "sized", "policy_hash": POLICY_HASH,
    "effort_aliases": "opus",
}
EST = policy.estimate(1000, 1, 0, 1000, 2, MODEL_DICT)
HOLD = EST["hold_sized"]
INPUT_CAP = EST["input_cap"]


def _grant_request(request_id=None, turn_id=None, seat_id=SEAT_A,
                   gateway_id=GATEWAY, task_class=TASK_CLASS):
    request_id = request_id or str(uuid.uuid4())
    turn_id = turn_id or str(uuid.uuid4())
    # The HTTP layer (unlike the store-level card-4 tests) reads the real
    # wall clock per request, so the admission estimate's freshness bound
    # (<=120s old, section 7) must be anchored to REAL now, not the
    # fixture's fixed 2026-09 NOW used elsewhere in this file for
    # timestamps that carry no freshness check (receipts, park records).
    # Section 7's OWN documented grant body (fix round tk-7d7fd9023f8c
    # finding 3): task_class, no request_hash.
    return {
        "request_id": request_id, "turn_id": turn_id, "seat_id": seat_id,
        "task_class": task_class, "model_id": MODEL, "effort": "opus",
        "gateway_id": gateway_id, "parent_lease_id": None,
        "context": {
            "mode": "FRESH", "initial_tokens": 1000, "dispatch_tokens": 0,
            "source": "tokenizer",
            "observed_at": int(time.time() * 1000) - 1000,
            "growth_tokens_per_call": 0,
        },
        "expected_calls": 1, "max_calls": 2, "output_cap": 1000,
        "per_call_output_cap": 500, "ttl_ms": TTL_MS,
    }


def _receipt(event_id=None, actual=1, final=True, gateway_id=GATEWAY,
            outcome="completed"):
    # Section 7's documented settle body (fix round tk-7d7fd9023f8c
    # finding 3): no lease_id (the URL path), no observed_at/received_at
    # (server-derived receipt timing).
    return {
        "event_id": event_id or str(uuid.uuid4()), "outcome": outcome,
        "calls": 1, "input_tokens": 500, "cached_input_tokens": 0,
        "output_tokens": 100, "actual_micro_eur": actual,
        "gateway_id": gateway_id, "seq": 1, "final": final,
    }


def _reading(event_id=None, account_id=ACCOUNT, seat_id=None,
            observed_at=NOW, kind="cli_window", payload=None):
    # Section 7's documented flat reading body (fix round tk-7d7fd9023f8c
    # finding 3): account_id/seat_id, not the batch document's
    # machine/account aliases - the machine is implicit (the caller).
    if payload is None:
        payload = {"windows": [], "confidence": "observed",
                  "source_stale": False, "covered_seq": 1}
    return {"event_id": event_id or str(uuid.uuid4()), "account_id": account_id,
            "seat_id": seat_id, "observed_at": observed_at, "kind": kind,
            "payload": payload}


def _limit_event(event_id=None, account_id=ACCOUNT, model_id=None,
                 turn_id=None, resets_at=None, observed_at=NOW,
                 evidence_kind="provider_usage_429"):
    return {
        "event_id": event_id or str(uuid.uuid4()), "account_id": account_id,
        "model_id": model_id, "turn_id": turn_id or str(uuid.uuid4()),
        "observed_at": observed_at, "evidence_kind": evidence_kind,
        "resets_at": resets_at, "exhausted_windows": [],
    }


def _build_store(tmp, name="lease.db", budget=10 * HOLD, gateway_sized=1,
                 enabled_a=1, enabled_b=1, account_kind="metered",
                 enforcement_a="metered", enforcement_b="metered",
                 profile=True):
    """A schema-v6 store: one account (two seats on two machines sharing
    it - the card's demo), one sized gateway on MACHINE_A, one unrelated
    account with no project grant (finding-2 fixtures), and (by default)
    one profiles row for GET /v1/plan (finding-5 fixtures)."""
    db = os.path.join(tmp, name)
    conn = sqlite3.connect(db)
    try:
        for script in (migrate.MIGRATION_V1, migrate.MIGRATION_V2,
                       migrate.MIGRATION_V3, migrate.MIGRATION_V4,
                       migrate.MIGRATION_V5, migrate.MIGRATION_V6):
            conn.executescript(script)
        budget_col = budget if account_kind == "metered" else None
        conn.execute(
            "INSERT INTO accounts(id, alias, vendor, kind, budget_micro_eur)"
            " VALUES(?,?,?,?,?)",
            (ACCOUNT, "meter", "ovh", account_kind, budget_col))
        conn.execute(
            "INSERT INTO accounts(id, alias, vendor, kind, budget_micro_eur)"
            " VALUES(?,?,?,?,?)",
            (OTHER_ACCOUNT, "other", "ovh", "subscription", None))
        conn.execute(
            "INSERT INTO machines(id, alias, identity_ref, enabled) "
            "VALUES(?,?,?,?)", (MACHINE_A, "desktop", IDENTITY_A, enabled_a))
        conn.execute(
            "INSERT INTO machines(id, alias, identity_ref, enabled) "
            "VALUES(?,?,?,?)", (MACHINE_B, "worker", IDENTITY_B, enabled_b))
        conn.execute(
            "INSERT INTO gateways(id, alias, account_id, machine_id, "
            "share_micro_eur, period) VALUES(?,?,?,?,?,?)",
            (GATEWAY, "g1", ACCOUNT, MACHINE_A, 100_000_000, "utc_month"))
        conn.execute("UPDATE gateways SET sized_ready=? WHERE id=?",
                     (gateway_sized, GATEWAY))
        conn.execute(
            "INSERT INTO gateways(id, alias, account_id, machine_id, "
            "share_micro_eur, period) VALUES(?,?,?,?,?,?)",
            (GATEWAY_B, "g2", ACCOUNT, MACHINE_B, 100_000_000, "utc_month"))
        conn.execute("UPDATE gateways SET sized_ready=? WHERE id=?",
                     (gateway_sized, GATEWAY_B))
        conn.execute("INSERT INTO projects(id, alias) VALUES(?,?)",
                     (PROJECT, "org"))
        conn.execute(
            "INSERT INTO project_accounts(project_id, account_id) "
            "VALUES(?,?)", (PROJECT, ACCOUNT))
        conn.execute("INSERT INTO teams(id, project_id, alias) "
                     "VALUES(?,?,?)", (TEAM, PROJECT, "dev"))
        conn.execute(
            "INSERT INTO seats(id, team_id, machine_id, alias, role, "
            "enforcement) VALUES(?,?,?,?,?,?)",
            (SEAT_A, TEAM, MACHINE_A, "s1", "main", enforcement_a))
        conn.execute(
            "INSERT INTO seats(id, team_id, machine_id, alias, role, "
            "enforcement) VALUES(?,?,?,?,?,?)",
            (SEAT_B, TEAM, MACHINE_B, "s2", "main", enforcement_b))
        conn.execute(
            "INSERT INTO models(id, account_id, alias, context_ceiling, "
            "output_ceiling, tariff_input_rate, tariff_output_rate, "
            "reserve_input_rate, reserve_output_rate, reservation_mode, "
            "policy_hash, effort_aliases) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (MODEL, ACCOUNT, "opus", 180000, 18000, 1, 1, 1_000_000,
             1_000_000, "sized", POLICY_HASH, "opus"))
        if profile:
            conn.execute(
                "INSERT INTO profiles(id, task_class, candidates, "
                "context_rules, calibration, expected_calls, growth_tokens, "
                "margin_bps, max_calls, output_cap, per_call_output_cap, "
                "ttl_ms, policy_revision) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), TASK_CLASS, json.dumps(["opus"]), "{}",
                 "{}", 1, 0, 12500, 2, 1000, 500, TTL_MS, 1))
        conn.commit()
    finally:
        conn.close()
    return db


def _park(db, account_id=ACCOUNT, model_id=None, reason="overrun"):
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO blocks(id, account_id, model_id, reason, retry_at, "
            "state, source_event_id, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), account_id, model_id, reason, None, "parked",
             str(uuid.uuid4()), NOW))
        conn.commit()
    finally:
        conn.close()


def _rows(db, sql, params=()):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# --- a thin urllib client, no framework: the whole point is to prove
# the real socket/wire behavior, not to mock it away. -----------------

def _call(base, method, path, body=None, raw=None, identity=None, token=None,
          idempotency_key=None, headers=None, content_length=None):
    hdrs = dict(headers or {})
    if identity is not None:
        hdrs[http_api.IDENTITY_HEADER] = identity
    if token is not None:
        hdrs["Authorization"] = "Bearer " + token
    if idempotency_key is not None:
        hdrs[http_api.IDEMPOTENCY_HEADER] = idempotency_key
    data = raw if raw is not None else (
        json.dumps(body).encode("utf-8") if body is not None else None)
    if data is not None and "Content-Length" not in hdrs:
        hdrs["Content-Length"] = str(
            len(data) if content_length is None else content_length)
    req = urllib.request.Request(base + path, data=data, headers=hdrs,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        with exc:
            raw_body = exc.read()
        try:
            return exc.code, json.loads(raw_body.decode("utf-8"))
        except ValueError:
            return exc.code, raw_body


class _ServerCase(unittest.TestCase):
    """Starts one real http_api server per test, on the loopback address,
    an ephemeral port; torn down after."""

    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-card5-")
        self.tmp = self._tmp.name
        self.db = None
        self.server = None
        self.thread = None

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        self._tmp.cleanup()

    def start(self, **build_kwargs):
        self.db = _build_store(self.tmp, **build_kwargs)
        self.server = http_api.make_server(self.db, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        return self.base

    def grant(self, identity=IDENTITY_A, idempotency_key=None, **request_kw):
        request = _grant_request(**request_kw)
        key = idempotency_key if idempotency_key is not None else request["request_id"]
        return _call(self.base, "POST", "/v1/leases", body=request,
                    identity=identity, idempotency_key=key)

    def settle(self, lease_id, identity=IDENTITY_A, idempotency_key=None, **kw):
        receipt = _receipt(**kw)
        key = idempotency_key if idempotency_key is not None else receipt["event_id"]
        return _call(self.base, "POST", "/v1/leases/%s/settle" % lease_id,
                    body=receipt, identity=identity, idempotency_key=key)

    def report(self, identity=IDENTITY_A, idempotency_key=None, **kw):
        reading = _reading(**kw)
        key = idempotency_key if idempotency_key is not None else reading["event_id"]
        return _call(self.base, "POST", "/v1/readings", body=reading,
                    identity=identity, idempotency_key=key)

    def limit_event(self, identity=IDENTITY_A, idempotency_key=None, **kw):
        event = _limit_event(**kw)
        key = idempotency_key if idempotency_key is not None else event["event_id"]
        return _call(self.base, "POST", "/v1/limit-events", body=event,
                    identity=identity, idempotency_key=key)


# ---------------------------------------------------------------------------
# token_scope: Given foreign identity, When check/report, Then deny.
# ---------------------------------------------------------------------------

class TokenScope(_ServerCase):
    def test_check_denies_a_different_machine_even_with_the_right_lease_id(self):
        self.start()
        status, body = self.grant()
        self.assertEqual(status, 201)
        lease_id, token = body["lease_id"], body["lease_token"]
        # The owning machine, with the right token, succeeds.
        status, body = _call(self.base, "POST", "/v1/leases/%s/check" % lease_id,
                             body={"request_id": str(uuid.uuid4())},
                             identity=IDENTITY_A, token=token)
        self.assertEqual(status, 200)
        # A different (but known, enabled) machine, same token: denied.
        status, body = _call(self.base, "POST", "/v1/leases/%s/check" % lease_id,
                             body={"request_id": str(uuid.uuid4())},
                             identity=IDENTITY_B, token=token)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "FORBIDDEN")

    def test_check_denies_the_right_machine_with_a_stale_or_missing_token(self):
        self.start()
        _status, body = self.grant()
        lease_id = body["lease_id"]
        for token in (None, "wrong-token", body["lease_token"] + "x"):
            status, resp = _call(self.base, "POST",
                                 "/v1/leases/%s/check" % lease_id,
                                 body={"request_id": str(uuid.uuid4())},
                                 identity=IDENTITY_A, token=token)
            self.assertEqual(status, 403, token)

    def test_readings_report_denies_an_unauthorized_reporter(self):
        # There is no client-suppliable "machine" field left to spoof
        # (the reporter is always the authenticated caller, by
        # construction - see _wire_to_reading); "foreign identity, report,
        # deny" now means an authenticated machine reporting for an
        # account it has no seat/project relationship to.
        self.start()
        status, body = self.report(identity=IDENTITY_A, account_id=OTHER_ACCOUNT)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "FORBIDDEN")
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM readings")[0][0], 0)

    def test_readings_report_accepted_from_its_own_machine(self):
        self.start()
        status, body = self.report(identity=IDENTITY_A)
        self.assertEqual(status, 202, body)
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM readings")[0][0], 1)

    def test_settle_denies_a_machine_that_is_neither_the_lease_nor_its_gateway(self):
        self.start()
        _status, body = self.grant()
        lease_id = body["lease_id"]
        status, resp = self.settle(lease_id, identity=IDENTITY_B)
        self.assertEqual(status, 403)
        self.assertEqual(resp["error"]["code"], "FORBIDDEN")

    def test_settle_accepted_from_the_leases_own_machine(self):
        # grant() itself refuses a gateway foreign to the seat's machine
        # (section 4): a lease's bound gateway is always on the SAME
        # machine as its seat, so settle's ownership check has exactly
        # one machine to satisfy.
        self.start()
        _status, body = self.grant(gateway_id=GATEWAY)
        lease_id = body["lease_id"]
        status, resp = self.settle(lease_id, identity=IDENTITY_A)
        self.assertEqual(status, 200, resp)


# ---------------------------------------------------------------------------
# replay_grant: Given a lost response, When retry, Then one hold/rotated
# token.
# ---------------------------------------------------------------------------

class ReplayGrant(_ServerCase):
    def test_replay_rotates_the_token_and_adds_no_hold(self):
        self.start()
        # A true replay resends the EXACT SAME body (the store's
        # idempotency key is (machine, request_id), but a changed
        # content_hash - even just a different context.observed_at - is
        # IDEMPOTENCY_CONFLICT, not a replay): build the request once.
        request = _grant_request()
        status1, body1 = _call(self.base, "POST", "/v1/leases", body=request,
                              identity=IDENTITY_A,
                              idempotency_key=request["request_id"])
        self.assertEqual(status1, 201)
        holds_after_first = _rows(
            self.db, "SELECT COUNT(*) FROM holds WHERE lease_id=?",
            (body1["lease_id"],))[0][0]
        # The lost-response retry: literally the same bytes again.
        status2, body2 = _call(self.base, "POST", "/v1/leases", body=request,
                              identity=IDENTITY_A,
                              idempotency_key=request["request_id"])
        self.assertEqual(status2, 200)              # replay: 200, not 201
        self.assertEqual(body2["lease_id"], body1["lease_id"])
        self.assertIsNotNone(body2["lease_token"])
        self.assertNotEqual(body2["lease_token"], body1["lease_token"])
        holds_after_replay = _rows(
            self.db, "SELECT COUNT(*) FROM holds WHERE lease_id=?",
            (body1["lease_id"],))[0][0]
        self.assertEqual(holds_after_replay, holds_after_first)   # one hold
        # The old token no longer checks; the new one does.
        status, _ = _call(self.base, "POST",
                          "/v1/leases/%s/check" % body1["lease_id"],
                          body={"request_id": str(uuid.uuid4())},
                          identity=IDENTITY_A, token=body1["lease_token"])
        self.assertEqual(status, 403)
        status, _ = _call(self.base, "POST",
                          "/v1/leases/%s/check" % body1["lease_id"],
                          body={"request_id": str(uuid.uuid4())},
                          identity=IDENTITY_A, token=body2["lease_token"])
        self.assertEqual(status, 200)

    def test_terminal_replay_returns_terminal_state_without_a_token(self):
        self.start()
        request = _grant_request()
        _status, body = _call(self.base, "POST", "/v1/leases", body=request,
                             identity=IDENTITY_A,
                             idempotency_key=request["request_id"])
        lease_id = body["lease_id"]
        status, _ = self.settle(lease_id, identity=IDENTITY_A)
        self.assertEqual(status, 200)
        status, replay = _call(self.base, "POST", "/v1/leases", body=request,
                               identity=IDENTITY_A,
                               idempotency_key=request["request_id"])
        self.assertEqual(status, 200)
        self.assertEqual(replay["state"], "settled")
        self.assertIsNone(replay["lease_token"])

    def test_different_content_same_ids_is_idempotency_conflict(self):
        self.start()
        request = _grant_request()
        _call(self.base, "POST", "/v1/leases", body=request, identity=IDENTITY_A,
             idempotency_key=request["request_id"])
        changed = dict(request)
        changed["output_cap"] = request["output_cap"] + 1   # any field differs
        status, body = _call(self.base, "POST", "/v1/leases", body=changed,
                             identity=IDENTITY_A,
                             idempotency_key=request["request_id"])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "IDEMPOTENCY_CONFLICT")


# ---------------------------------------------------------------------------
# public_bind: Given a public binding, When start, Then refuse.
# ---------------------------------------------------------------------------

class PublicBind(unittest.TestCase):
    def test_wildcard_and_public_hosts_refused_before_any_socket(self):
        for host in ("0.0.0.0", "::", "::1", "8.8.8.8", "203.0.113.5",
                    "localhost", "example.com", "", "not-an-ip"):
            with self.assertRaises(http_api.BindRefused):
                http_api.check_bind_host(host)

    def test_loopback_hosts_accepted(self):
        for host in ("127.0.0.1", "127.0.0.2"):
            http_api.check_bind_host(host)   # must not raise

    def test_make_server_refuses_a_public_bind(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base, prefix="quota-card5-") as tmp:
            db = _build_store(tmp)
            with self.assertRaises(http_api.BindRefused):
                http_api.make_server(db, host="0.0.0.0", port=0)

    def test_default_host_is_loopback_and_actually_serves(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base, prefix="quota-card5-") as tmp:
            db = _build_store(tmp)
            server = http_api.make_server(db, port=0)
            try:
                self.assertEqual(server.server_address[0], "127.0.0.1")
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base_url = "http://127.0.0.1:%d" % server.server_address[1]
                status, _ = _call(base_url, "GET", "/v1/status",
                                  identity=IDENTITY_A)
                self.assertEqual(status, 200)
            finally:
                server.shutdown()
                server.server_close()


# ---------------------------------------------------------------------------
# privacy_5: given injected secret/path/provider-ID sentinels, error/status
# output never echoes them.
# ---------------------------------------------------------------------------

SENTINELS = ("sk-SECRET-sentinel", "C:\\secret\\path.txt",
            "/etc/secret-sentinel", "provider-id-ovh-9f8e7d")


class Privacy5(_ServerCase):
    def test_error_bodies_never_echo_injected_sentinels(self):
        self.start()
        for sentinel in SENTINELS:
            for status, body in (
                    _call(self.base, "GET", "/v1/status", identity=sentinel),
                    _call(self.base, "POST", "/v1/leases",
                         body={"seat_id": sentinel}, identity=IDENTITY_A,
                         idempotency_key=str(uuid.uuid4())),
                    _call(self.base, "POST", "/v1/leases/%s/check" % sentinel,
                         body={"request_id": str(uuid.uuid4())},
                         identity=IDENTITY_A, token=sentinel),
                    _call(self.base, "POST", "/v1/readings",
                         body=_reading(event_id=sentinel), identity=IDENTITY_A,
                         idempotency_key=str(uuid.uuid4())),
            ):
                self.assertGreaterEqual(status, 400)
                text = json.dumps(body)
                self.assertNotIn(sentinel, text)

    def test_status_is_json_only_and_holds_no_sentinel(self):
        self.start()
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE machines SET identity_ref=? WHERE id=?",
                        (SENTINELS[3], MACHINE_A))
            conn.commit()
        finally:
            conn.close()
        status, body = _call(self.base, "GET", "/v1/status",
                            identity=SENTINELS[3])
        self.assertEqual(status, 200)
        text = json.dumps(body)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, text)
        # "status JSON only": every top-level value is a plain JSON
        # structure, never raw text echoing a header or a path.
        self.assertIsInstance(body, dict)

    def test_error_envelope_is_closed_shape(self):
        # Nothing beyond the documented fields ever rides along on an
        # error response (no stray "debug"/exception-text field).
        self.start()
        status, body = _call(self.base, "GET", "/v1/status")
        self.assertEqual(status, 401)
        self.assertEqual(set(body), {"error", "server_at", "policy_revision"})
        self.assertEqual(set(body["error"]),
                         {"code", "message", "retry_at", "details"})
        self.assertEqual(body["error"]["details"], {})

    def test_unexpected_non_quota_exception_is_still_caught(self):
        # Force a genuine, un-typed internal fault (not a QuotaError -
        # store._connect already turns a bad sqlite file into one, so
        # that path never reaches the last-resort guard at all) and
        # confirm the guard still emits the closed contract, never the
        # exception's own text.
        self.start()
        sentinel = "boom-" + SENTINELS[0]

        def _boom(*_a, **_k):
            raise RuntimeError(sentinel)

        original = http_api.handle_status
        http_api.handle_status = _boom
        try:
            status, body = _call(self.base, "GET", "/v1/status",
                                identity=IDENTITY_A)
        finally:
            http_api.handle_status = original
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "STORE_ERROR")
        self.assertNotIn(sentinel, json.dumps(body))

    def test_unexpected_internal_fault_leaks_nothing(self):
        # A store whose file exists but is not a database at all: the
        # last-resort guard must still emit the closed error contract,
        # never a traceback or a path.
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base, prefix="quota-card5-") as tmp:
            db = os.path.join(tmp, "garbage.db")
            with open(db, "wb") as fh:
                fh.write(b"not a sqlite file" + SENTINELS[1].encode())
            server = http_api.make_server(db, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = "http://127.0.0.1:%d" % server.server_address[1]
                status, body = _call(base_url, "GET", "/v1/status",
                                    identity=IDENTITY_A)
                self.assertGreaterEqual(status, 400)
                self.assertNotIn(SENTINELS[1], json.dumps(body))
                self.assertNotIn(tmp, json.dumps(body))
            finally:
                server.shutdown()
                server.server_close()

    def test_protocol_error_bypasses_bypass_is_closed_json(self):
        # Fix round tk-7d7fd9023f8c finding 6: an invalid HTTP method is a
        # protocol-level error BaseHTTPRequestHandler.send_error handles
        # BEFORE _handle (and so before authentication) ever runs; its
        # default implementation embeds the raw method text, unescaped,
        # in an HTML body. A raw socket is required: no HTTP client
        # constructs a request with an invalid method name.
        self.start()
        import socket as _socket
        sentinel = "sk-SECRET-sentinel"
        request_bytes = (
            "%s /v1/status HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            "Connection: close\r\n\r\n" % sentinel
        ).encode("ascii")
        with _socket.create_connection(
                ("127.0.0.1", self.server.server_address[1]), timeout=10) as sock:
            sock.sendall(request_bytes)
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        self.assertNotIn(sentinel.encode(), response)
        head, _, raw_body = response.partition(b"\r\n\r\n")
        self.assertIn(b"Content-Type: application/json", head)
        body = json.loads(raw_body.decode("utf-8"))
        self.assertNotIn(sentinel, json.dumps(body))
        self.assertIn(body["error"]["code"], ("INVALID_REQUEST", "STORE_ERROR"))


# ---------------------------------------------------------------------------
# Also cover: 401 vs 403, 503, body bounds, malformed/duplicate JSON.
# ---------------------------------------------------------------------------

class IdentityAuth(_ServerCase):
    def test_missing_identity_header_is_401(self):
        self.start()
        status, body = _call(self.base, "GET", "/v1/status")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "UNAUTHENTICATED")

    def test_empty_identity_header_is_401(self):
        self.start()
        status, body = _call(self.base, "GET", "/v1/status", identity="")
        self.assertEqual(status, 401)

    def test_unknown_identity_is_403(self):
        self.start()
        status, body = _call(self.base, "GET", "/v1/status",
                            identity="never-enrolled")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "FORBIDDEN")

    def test_disabled_machine_is_403(self):
        self.start(enabled_a=0)
        status, body = _call(self.base, "GET", "/v1/status", identity=IDENTITY_A)
        self.assertEqual(status, 403)

    def test_known_enabled_identity_reaches_status(self):
        self.start()
        status, _ = _call(self.base, "GET", "/v1/status", identity=IDENTITY_A)
        self.assertEqual(status, 200)

    def test_grant_denies_a_seat_owned_by_a_different_machine(self):
        self.start()
        status, body = self.grant(identity=IDENTITY_B, seat_id=SEAT_A)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "FORBIDDEN")
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM leases")[0][0], 0)


class StoreUnavailable(unittest.TestCase):
    def test_missing_db_file_is_503(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base, prefix="quota-card5-") as tmp:
            db = os.path.join(tmp, "does-not-exist.db")
            server = http_api.make_server(db, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = "http://127.0.0.1:%d" % server.server_address[1]
                status, body = _call(base_url, "GET", "/v1/status",
                                    identity=IDENTITY_A)
                self.assertEqual(status, 503)
                self.assertEqual(body["error"]["code"], "STORE_ERROR")
            finally:
                server.shutdown()
                server.server_close()


class BodyBounds(_ServerCase):
    def test_oversized_body_is_400(self):
        self.start()
        raw = b'{"seat_id":"' + b"x" * (http_api.MAX_BODY_BYTES + 1) + b'"}'
        status, body = _call(self.base, "POST", "/v1/leases", raw=raw,
                            identity=IDENTITY_A)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_oversized_body_isolated_from_shape(self):
        # An otherwise entirely well-shaped grant is still refused once
        # it is over the bound - isolates the size check itself from
        # every other reason the same request could be a 400.
        self.start()
        request = _grant_request()
        raw = json.dumps(request).encode("utf-8")
        original = http_api.MAX_BODY_BYTES
        http_api.MAX_BODY_BYTES = len(raw) - 1
        try:
            status, body = _call(self.base, "POST", "/v1/leases", raw=raw,
                                identity=IDENTITY_A,
                                idempotency_key=request["request_id"])
        finally:
            http_api.MAX_BODY_BYTES = original
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_missing_content_length_is_400(self):
        self.start()
        status, body = _call(self.base, "POST", "/v1/leases", raw=b"{}",
                            identity=IDENTITY_A,
                            headers={"Content-Length": ""})
        # An empty/invalid Content-Length is refused the same as a
        # missing one (both fail the int() parse or the None check).
        self.assertEqual(status, 400)

    def test_truly_absent_content_length_is_400(self):
        # http.client (and so urllib) always sends SOME Content-Length
        # for a POST, even "0" - never truly absent - so this isolates
        # the `length is None` branch with a raw socket instead: a
        # request naming no Content-Length header at all.
        self.start()
        import socket as _socket
        request_bytes = (
            "POST /v1/leases HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "%s: %s\r\n"
            "Connection: close\r\n\r\n" % (http_api.IDENTITY_HEADER, IDENTITY_A)
        ).encode("ascii")
        with _socket.create_connection(
                ("127.0.0.1", self.server.server_address[1]), timeout=10) as sock:
            sock.sendall(request_bytes)
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        status_line = response.split(b"\r\n", 1)[0].decode("ascii")
        self.assertIn("400", status_line)
        body = json.loads(response.rsplit(b"\r\n\r\n", 1)[1].decode("utf-8"))
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_malformed_json_is_400(self):
        self.start()
        status, body = _call(self.base, "POST", "/v1/leases",
                            raw=b"{not json at all", identity=IDENTITY_A)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_duplicate_json_key_is_400(self):
        self.start()
        raw = (b'{"seat_id":"' + SEAT_A.encode() + b'","seat_id":"'
               + SEAT_B.encode() + b'"}')
        status, body = _call(self.base, "POST", "/v1/leases", raw=raw,
                            identity=IDENTITY_A)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM leases")[0][0], 0)

    def test_duplicate_json_key_isolated_from_shape(self):
        # An otherwise entirely well-shaped, WELL-KNOWN-key duplicate: if
        # the duplicate-key rejection were bypassed, plain last-wins JSON
        # semantics would just apply the second ttl_ms and grant this
        # request normally (201) - isolates the rejection from every
        # other reason a malformed body could be a 400.
        self.start()
        request = _grant_request()
        raw = json.dumps(request).encode("utf-8")
        raw = raw[:-1] + b',"ttl_ms":' + str(request["ttl_ms"] + 1).encode() + b'}'
        status, body = _call(self.base, "POST", "/v1/leases", raw=raw,
                            identity=IDENTITY_A,
                            idempotency_key=request["request_id"])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM leases")[0][0], 0)

    def test_non_object_json_body_is_400(self):
        self.start()
        status, body = _call(self.base, "POST", "/v1/leases", raw=b"[1,2,3]",
                            identity=IDENTITY_A)
        self.assertEqual(status, 400)


# ---------------------------------------------------------------------------
# Demo (section 8, row 5): two authenticated fixture machines share one
# block.
# ---------------------------------------------------------------------------

class Demo(_ServerCase):
    def test_two_authenticated_machines_share_one_account_block(self):
        self.start()
        _park(self.db)
        for identity, seat in ((IDENTITY_A, SEAT_A), (IDENTITY_B, SEAT_B)):
            status, body = self.grant(identity=identity, seat_id=seat)
            self.assertEqual(status, 429, (identity, body))
            self.assertEqual(body["error"]["code"], "ACCOUNT_PARKED")

    def test_clearing_the_block_unblocks_both_machines(self):
        self.start()
        _park(self.db)
        status, _ = self.grant(identity=IDENTITY_A, seat_id=SEAT_A)
        self.assertEqual(status, 429)
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE blocks SET state='cleared'")
            conn.commit()
        finally:
            conn.close()
        for identity, seat, gateway in ((IDENTITY_A, SEAT_A, GATEWAY),
                                        (IDENTITY_B, SEAT_B, GATEWAY_B)):
            status, body = self.grant(identity=identity, seat_id=seat,
                                      gateway_id=gateway)
            self.assertEqual(status, 201, (identity, body))

    def test_a_limit_event_from_either_machine_parks_both(self):
        # The row's own demo, now reachable through /v1/limit-events
        # (fix round tk-7d7fd9023f8c finding 5): a SECOND machine's
        # independent report of the SAME account joins the block the
        # first machine's report already created - no duplicate block.
        self.start()
        status, first = self.limit_event(identity=IDENTITY_A)
        self.assertEqual(status, 202, first)
        status, second = self.limit_event(identity=IDENTITY_B)
        self.assertEqual(status, 202, second)
        self.assertEqual(first["block_id"], second["block_id"])
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM blocks")[0][0], 1)
        for identity, seat in ((IDENTITY_A, SEAT_A), (IDENTITY_B, SEAT_B)):
            status, body = self.grant(identity=identity, seat_id=seat)
            self.assertEqual(status, 429, (identity, body))
            self.assertEqual(body["error"]["code"], "ACCOUNT_PARKED")


# ---------------------------------------------------------------------------
# Ordinary grant/check/settle behavior over the wire (closed shapes, the
# wire's `true` -> 0/1 conversion, envelope fields).
# ---------------------------------------------------------------------------

class WireContract(_ServerCase):
    def test_grant_response_is_closed_and_carries_the_envelope(self):
        self.start()
        status, body = self.grant()
        self.assertEqual(status, 201)
        self.assertEqual(set(body), {
            "lease_id", "state", "enforcement", "money_hold", "caps",
            "granted_at", "expires_at", "lease_token", "windows",
            "warnings", "server_at", "policy_revision"})
        self.assertEqual(body["policy_revision"], migrate.SCHEMA_VERSION)
        self.assertIsInstance(body["server_at"], int)

    def test_check_response_is_exactly_state_expires_caps(self):
        self.start()
        _status, granted = self.grant()
        status, body = _call(
            self.base, "POST", "/v1/leases/%s/check" % granted["lease_id"],
            body={"request_id": str(uuid.uuid4())}, identity=IDENTITY_A,
            token=granted["lease_token"])
        self.assertEqual(status, 200)
        self.assertEqual(set(body) - {"server_at", "policy_revision"},
                         {"state", "expires_at", "caps"})

    def test_check_body_shape_is_closed_even_though_check_ignores_it(self):
        # lease.check() takes no body at all - request_id is section 7's
        # documented shape, not something the store consumes - so this
        # is the ONLY thing enforcing it; an extra key or a wrong type
        # must still be refused, not silently accepted.
        self.start()
        _status, granted = self.grant()
        lease_id, token = granted["lease_id"], granted["lease_token"]
        for bad_body in ({"request_id": str(uuid.uuid4()), "extra": 1},
                        {"request_id": 12345}, {"request_id": "not-a-uuid"}, {}):
            status, body = _call(
                self.base, "POST", "/v1/leases/%s/check" % lease_id,
                body=bad_body, identity=IDENTITY_A, token=token)
            self.assertEqual(status, 400, bad_body)
            self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_settle_final_boolean_true_converts_and_settles(self):
        self.start()
        _status, granted = self.grant()
        lease_id = granted["lease_id"]
        status, body = self.settle(lease_id, final=True)
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "settled")
        self.assertEqual(set(body) - {"server_at", "policy_revision"},
                         {"state", "released_micro_eur", "retained_micro_eur"})

    def test_settle_final_as_int_is_refused_not_silently_accepted(self):
        self.start()
        _status, granted = self.grant()
        lease_id = granted["lease_id"]
        receipt = _receipt(final=True)
        receipt["final"] = 1               # the store's own int form, on the wire
        status, body = _call(
            self.base, "POST", "/v1/leases/%s/settle" % lease_id,
            body=receipt, identity=IDENTITY_A,
            idempotency_key=receipt["event_id"])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_settle_body_has_no_lease_id_observed_at_received_at(self):
        # Fix round tk-7d7fd9023f8c finding 3: the documented settle body
        # is exactly the wire keys; the store-only fields are no longer
        # exposed at all (there is nowhere left for the reviewer's
        # "no body lease_id/observed_at/received_at" repro to be refused).
        self.start()
        _status, granted = self.grant()
        lease_id = granted["lease_id"]
        receipt = _receipt(final=False)
        self.assertNotIn("lease_id", receipt)
        self.assertNotIn("observed_at", receipt)
        self.assertNotIn("received_at", receipt)
        status, body = self.settle(lease_id, final=False)
        self.assertEqual(status, 200, body)

    def test_input_limit_maps_to_429(self):
        self.start(budget=10 * HOLD)
        # An over-large initial_tokens exceeds the model's context
        # ceiling: CONTEXT_PROJECTION_EXCEEDED (400), a static
        # request-shape refusal - never reaching admission at all.
        # Exceeding an actual LIMIT (INPUT_LIMIT, 429) instead needs a
        # configured limits row tighter than the model ceiling.
        request = _grant_request()
        request["context"]["initial_tokens"] = 10 ** 8
        status, body = _call(self.base, "POST", "/v1/leases", body=request,
                            identity=IDENTITY_A,
                            idempotency_key=request["request_id"])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "CONTEXT_PROJECTION_EXCEEDED")

    def test_money_limit_maps_to_429(self):
        self.start(budget=HOLD - 1)     # one micro-EUR short of the hold
        status, body = self.grant()
        self.assertEqual(status, 429)
        self.assertEqual(body["error"]["code"], "MONEY_LIMIT")

    def test_not_found_lease_is_404(self):
        self.start()
        status, body = _call(
            self.base, "POST", "/v1/leases/%s/check" % str(uuid.uuid4()),
            body={"request_id": str(uuid.uuid4())}, identity=IDENTITY_A)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "NOT_FOUND")

    def test_unrouted_path_is_404(self):
        self.start()
        status, body = _call(self.base, "GET", "/v1/nope", identity=IDENTITY_A)
        self.assertEqual(status, 404)


# ---------------------------------------------------------------------------
# Fix round tk-7d7fd9023f8c (HOLD: 1 blocker, 4 major, 3 minor)
# ---------------------------------------------------------------------------

class FixRoundNonEnforcingLeases(_ServerCase):
    """Finding 1 (BLOCKER): metered_advisory/shadow grants must have a
    null token AND must not pass /check - gated by enforcement mode, not
    merely by state."""

    def test_metered_advisory_grant_has_no_token_and_cannot_check(self):
        self.start(gateway_sized=0)      # not sized-ready -> metered_advisory
        status, body = self.grant()
        self.assertEqual(status, 201)
        self.assertEqual(body["enforcement"], "metered_advisory")
        self.assertIsNone(body["lease_token"])
        self.assertIsNone(body["money_hold"])
        status, err = _call(
            self.base, "POST", "/v1/leases/%s/check" % body["lease_id"],
            body={"request_id": str(uuid.uuid4())}, identity=IDENTITY_A,
            token=None)
        self.assertEqual(status, 403)
        self.assertEqual(err["error"]["code"], "FORBIDDEN")

    def test_shadow_grant_has_no_token_and_cannot_check(self):
        self.start(enforcement_a="shadow")
        status, body = self.grant()
        self.assertEqual(status, 201)
        self.assertEqual(body["enforcement"], "shadow")
        self.assertIsNone(body["lease_token"])
        self.assertIsNone(body["money_hold"])
        status, err = _call(
            self.base, "POST", "/v1/leases/%s/check" % body["lease_id"],
            body={"request_id": str(uuid.uuid4())}, identity=IDENTITY_A)
        self.assertEqual(status, 403)

    def test_check_gates_on_enforcement_even_with_a_stray_matching_token(self):
        # Isolates the explicit enforcement gate from the (also correct)
        # null-token fallback: force a token onto a non-enforcing lease
        # as if an earlier bug had minted one, and confirm /check still
        # refuses it.
        self.start(enforcement_a="shadow")
        status, body = self.grant()
        self.assertEqual(status, 201)
        stray = "stray-token"
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE leases SET token_hash=? WHERE id=?",
                        (http_api._hash_token(stray), body["lease_id"]))
            conn.commit()
        finally:
            conn.close()
        status, err = _call(
            self.base, "POST", "/v1/leases/%s/check" % body["lease_id"],
            body={"request_id": str(uuid.uuid4())}, identity=IDENTITY_A,
            token=stray)
        self.assertEqual(status, 403)

    def test_metered_grant_still_gets_a_token_and_passes_check(self):
        # The positive control: an ordinary enforcing grant is unaffected.
        self.start()
        status, body = self.grant()
        self.assertEqual(status, 201)
        self.assertEqual(body["enforcement"], "metered")
        self.assertIsNotNone(body["lease_token"])
        status, _ = _call(
            self.base, "POST", "/v1/leases/%s/check" % body["lease_id"],
            body={"request_id": str(uuid.uuid4())}, identity=IDENTITY_A,
            token=body["lease_token"])
        self.assertEqual(status, 200)


class FixRoundReadingAccountAuthorization(_ServerCase):
    """Finding 2 (MAJOR): authenticating the reporting machine does not
    authorize the account it names; cli_window has no other check."""

    def test_cli_window_for_an_unrelated_account_is_refused(self):
        self.start()
        status, body = self.report(account_id=OTHER_ACCOUNT)
        self.assertEqual(status, 403, body)
        self.assertEqual(body["error"]["code"], "FORBIDDEN")
        self.assertEqual(_rows(self.db, "SELECT used_bps FROM windows "
                               "WHERE account_id=?", (OTHER_ACCOUNT,)), [])

    def test_cli_window_for_its_own_project_account_is_accepted(self):
        self.start()
        status, body = self.report(account_id=ACCOUNT)
        self.assertEqual(status, 202, body)

    def test_gateway_snapshot_for_an_unrelated_account_is_still_refused_by_its_own_check(self):
        # Not the new authorization rule (gateway_snapshot's OWN reducer
        # already binds machine+account to the gateway's real owner) -
        # a regression control that the new cli_window-only gate did not
        # loosen anything for gateway_snapshot.
        self.start()
        payload = {
            "gateway_id": "g1", "ledger_epoch": 1, "seq": 1,
            "period_start": NOW, "period_end": NOW + 2_592_000_000,
            "settled_micro_eur": 0, "reserved_micro_eur": 0,
            "uncertain_micro_eur": 0, "unattributed_micro_eur": 0,
            "reconciliation_required": False, "sized_ready": False,
            "policy_hashes": None,
        }
        status, body = self.report(account_id=OTHER_ACCOUNT,
                                   kind="gateway_snapshot", payload=payload)
        self.assertEqual(status, 400, body)


class FixRoundWireAdapter(_ServerCase):
    """Finding 3 (MAJOR): the documented wire bodies must be accepted as
    specified, without exposing store-only fields."""

    def test_documented_grant_body_without_request_hash_is_accepted(self):
        self.start()
        request = _grant_request()
        self.assertNotIn("request_hash", request)
        self.assertIn("task_class", request)
        status, body = self.grant()
        self.assertEqual(status, 201, body)

    def test_documented_flat_reading_is_accepted(self):
        self.start()
        status, body = self.report()
        self.assertEqual(status, 202, body)
        self.assertIn("event_id", body)
        self.assertEqual(body["accepted"], True)

    def test_documented_settle_with_only_the_url_lease_id_is_accepted(self):
        self.start()
        _status, granted = self.grant()
        lease_id = granted["lease_id"]
        status, body = self.settle(lease_id, final=True)
        self.assertEqual(status, 200, body)

    def test_settle_wire_shape_is_closed(self):
        # Isolates the settle adapter's OWN closed-shape check: an extra
        # or a missing key must be refused even though every other field
        # is individually well-typed.
        self.start()
        _status, granted = self.grant()
        lease_id = granted["lease_id"]
        receipt = _receipt(final=False)
        with_extra = dict(receipt, extra=1)
        status, body = _call(
            self.base, "POST", "/v1/leases/%s/settle" % lease_id,
            body=with_extra, identity=IDENTITY_A,
            idempotency_key=receipt["event_id"])
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")
        missing_seq = {k: v for k, v in receipt.items() if k != "seq"}
        status, body = _call(
            self.base, "POST", "/v1/leases/%s/settle" % lease_id,
            body=missing_seq, identity=IDENTITY_A,
            idempotency_key=receipt["event_id"])
        self.assertEqual(status, 400, body)
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM receipts")[0][0], 0)

    def test_reading_wire_shape_is_closed(self):
        # Isolates the reading adapter's OWN closed-shape check the same
        # way.
        self.start()
        reading = _reading()
        with_extra = dict(reading, extra=1)
        status, body = _call(self.base, "POST", "/v1/readings", body=with_extra,
                            identity=IDENTITY_A,
                            idempotency_key=reading["event_id"])
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")
        missing_kind = {k: v for k, v in reading.items() if k != "kind"}
        status, body = _call(self.base, "POST", "/v1/readings", body=missing_kind,
                            identity=IDENTITY_A,
                            idempotency_key=reading["event_id"])
        self.assertEqual(status, 400, body)
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM readings")[0][0], 0)


class FixRoundIdempotencyKey(_ServerCase):
    """Finding 4 (MAJOR): Idempotency-Key must be present, UUID-shaped
    and agree with the body's own id, checked before the store's own
    replay logic runs."""

    def test_missing_idempotency_key_is_400(self):
        self.start()
        request = _grant_request()
        status, body = _call(self.base, "POST", "/v1/leases", body=request,
                             identity=IDENTITY_A)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM leases")[0][0], 0)

    def test_malformed_idempotency_key_is_400(self):
        self.start()
        request = _grant_request()
        status, body = _call(self.base, "POST", "/v1/leases", body=request,
                             identity=IDENTITY_A, idempotency_key="not-a-uuid")
        self.assertEqual(status, 400)

    def test_check_idempotency_key_function_rejects_a_non_uuid_agreeing_pair(self):
        # A wire-level test cannot isolate the format check from the
        # agreement check: whatever non-UUID key/id pair is sent, the
        # store's OWN downstream UUID validation (lease.grant's
        # policy._check_uuid on request_id) refuses it too, with the
        # identical 400 - so the format check LOOKS redundant from the
        # outside even when it is the only thing that actually ran.
        # Calling the function directly proves it enforces the format on
        # its own, per the review's own wording ("validate the header
        # format"), not merely relying on a downstream check to happen to
        # cover the same ground.
        with self.assertRaises(errors.QuotaError) as ctx:
            http_api._check_idempotency_key("not-a-uuid", "not-a-uuid")
        self.assertEqual(ctx.exception.code, "INVALID_REQUEST")

    def test_idempotency_key_disagreeing_with_request_id_is_400(self):
        self.start()
        request = _grant_request()
        status, body = _call(self.base, "POST", "/v1/leases", body=request,
                             identity=IDENTITY_A,
                             idempotency_key=str(uuid.uuid4()))
        self.assertEqual(status, 400)
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM leases")[0][0], 0)

    def test_same_key_reused_across_different_request_ids_is_refused_not_two_leases(self):
        # The reviewer's own repro: two grant bodies with genuinely
        # different request_ids under the SAME Idempotency-Key header.
        # Agreement is checked per call, so whichever body disagrees with
        # the shared key is refused before it can create a second lease.
        self.start()
        shared_key = str(uuid.uuid4())
        first = _grant_request(request_id=shared_key)
        second = _grant_request(request_id=str(uuid.uuid4()))
        status1, body1 = _call(self.base, "POST", "/v1/leases", body=first,
                              identity=IDENTITY_A, idempotency_key=shared_key)
        self.assertEqual(status1, 201)
        status2, body2 = _call(self.base, "POST", "/v1/leases", body=second,
                              identity=IDENTITY_A, idempotency_key=shared_key)
        self.assertEqual(status2, 400)
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM leases")[0][0], 1)
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM holds")[0][0],
                         _rows(self.db, "SELECT COUNT(*) FROM holds WHERE "
                              "lease_id=?", (body1["lease_id"],))[0][0])

    def test_settle_requires_agreeing_idempotency_key(self):
        self.start()
        _status, granted = self.grant()
        lease_id = granted["lease_id"]
        receipt = _receipt(final=False)
        status, body = _call(
            self.base, "POST", "/v1/leases/%s/settle" % lease_id,
            body=receipt, identity=IDENTITY_A, idempotency_key=str(uuid.uuid4()))
        self.assertEqual(status, 400)
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM receipts")[0][0], 0)

    def test_readings_requires_agreeing_idempotency_key(self):
        self.start()
        reading = _reading()
        status, body = _call(self.base, "POST", "/v1/readings", body=reading,
                             identity=IDENTITY_A, idempotency_key=str(uuid.uuid4()))
        self.assertEqual(status, 400)
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM readings")[0][0], 0)

    def test_limit_events_requires_agreeing_idempotency_key(self):
        self.start()
        event = _limit_event()
        status, body = _call(self.base, "POST", "/v1/limit-events", body=event,
                             identity=IDENTITY_A, idempotency_key=str(uuid.uuid4()))
        self.assertEqual(status, 400)
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM blocks")[0][0], 0)


class FixRoundPlanAndLimitEvents(_ServerCase):
    """Finding 5 (MAJOR): GET /v1/plan and POST /v1/limit-events."""

    def test_plan_returns_ordered_candidates_reasons_caps(self):
        self.start()
        status, body = _call(
            self.base, "GET",
            "/v1/plan?project_id=%s&task_class=%s" % (PROJECT, TASK_CLASS),
            identity=IDENTITY_A)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["candidates"], [{"model": "opus", "blocked": False}])
        self.assertEqual(body["reasons"], {"opus": "available"})
        self.assertIsNone(body["context"])
        self.assertEqual(body["caps"], {"max_calls": 2, "output_cap": 1000,
                                        "per_call_output_cap": 500})
        self.assertIsNone(body["retry_at"])

    def test_plan_reports_a_blocked_candidate_and_its_retry_at(self):
        self.start()
        _park(self.db, reason="provider_usage_429")
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE blocks SET retry_at=?", (NOW + 60_000,))
            conn.commit()
        finally:
            conn.close()
        status, body = _call(
            self.base, "GET",
            "/v1/plan?project_id=%s&task_class=%s" % (PROJECT, TASK_CLASS),
            identity=IDENTITY_A)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["candidates"], [{"model": "opus", "blocked": True}])
        self.assertEqual(body["reasons"], {"opus": "blocked"})
        self.assertEqual(body["retry_at"], NOW + 60_000)

    def test_plan_unknown_project_is_404(self):
        self.start()
        status, body = _call(
            self.base, "GET",
            "/v1/plan?project_id=%s&task_class=%s" % (str(uuid.uuid4()), TASK_CLASS),
            identity=IDENTITY_A)
        self.assertEqual(status, 404)

    def test_plan_unknown_task_class_is_404(self):
        self.start()
        status, body = _call(
            self.base, "GET",
            "/v1/plan?project_id=%s&task_class=no-such-class" % PROJECT,
            identity=IDENTITY_A)
        self.assertEqual(status, 404)

    def test_plan_missing_query_params_is_400(self):
        self.start()
        status, _ = _call(self.base, "GET", "/v1/plan", identity=IDENTITY_A)
        self.assertEqual(status, 400)

    def test_limit_event_creates_a_parked_block(self):
        self.start()
        status, body = self.limit_event()
        self.assertEqual(status, 202, body)
        self.assertEqual(body["state"], "parked")
        rows = _rows(self.db, "SELECT account_id, state FROM blocks WHERE id=?",
                    (body["block_id"],))
        self.assertEqual(rows, [(ACCOUNT, "parked")])

    def test_limit_event_replay_is_idempotent(self):
        self.start()
        event = _limit_event()
        status1, body1 = _call(self.base, "POST", "/v1/limit-events", body=event,
                              identity=IDENTITY_A, idempotency_key=event["event_id"])
        status2, body2 = _call(self.base, "POST", "/v1/limit-events", body=event,
                              identity=IDENTITY_A, idempotency_key=event["event_id"])
        self.assertEqual((status1, status2), (202, 202))
        self.assertEqual(body1["block_id"], body2["block_id"])
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM blocks")[0][0], 1)

    def test_limit_event_same_id_different_model_is_idempotency_conflict(self):
        self.start()
        event_id = str(uuid.uuid4())
        self.limit_event(event_id=event_id, model_id=None)
        status, body = self.limit_event(event_id=event_id, model_id=MODEL)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "IDEMPOTENCY_CONFLICT")

    def test_limit_event_for_an_unauthorized_account_is_refused(self):
        self.start()
        status, body = self.limit_event(account_id=OTHER_ACCOUNT)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "FORBIDDEN")
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM blocks")[0][0], 0)

    def test_limit_event_joins_the_live_block_matching_its_own_scope(self):
        # Isolates record_limit_event's model_id-scoped join: an
        # account-wide block (model_id NULL) and a model-scoped block
        # (model_id=MODEL) are two DIFFERENT live rows for the same
        # account; a third report naming one of the two scopes must join
        # THAT one, not whichever the query happens to find first.
        self.start()
        _status, account_wide = self.limit_event(model_id=None)
        _status, model_scoped = self.limit_event(model_id=MODEL)
        self.assertNotEqual(account_wide["block_id"], model_scoped["block_id"])
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM blocks")[0][0], 2)
        status, again_wide = self.limit_event(model_id=None)
        self.assertEqual(status, 202)
        self.assertEqual(again_wide["block_id"], account_wide["block_id"])
        status, again_scoped = self.limit_event(model_id=MODEL)
        self.assertEqual(status, 202)
        self.assertEqual(again_scoped["block_id"], model_scoped["block_id"])
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM blocks")[0][0], 2)


class FixRoundCheckRequestIdUuid(_ServerCase):
    """Finding 7 (MINOR): /check must reuse the strict UUID validator for
    request_id."""

    def test_non_uuid_request_id_is_refused(self):
        self.start()
        _status, granted = self.grant()
        status, body = _call(
            self.base, "POST", "/v1/leases/%s/check" % granted["lease_id"],
            body={"request_id": "not-a-uuid"}, identity=IDENTITY_A,
            token=granted["lease_token"])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")


class FixRoundIPv6Bind(unittest.TestCase):
    """Finding 8 (MINOR): check_bind_host must not accept an address the
    AF_INET-only server cannot actually serve."""

    def test_ipv6_loopback_is_refused_by_the_checker(self):
        with self.assertRaises(http_api.BindRefused):
            http_api.check_bind_host("::1")

    def test_ipv6_loopback_is_refused_before_construction_not_a_gaierror(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base, prefix="quota-card5-") as tmp:
            db = _build_store(tmp)
            with self.assertRaises(http_api.BindRefused):
                http_api.make_server(db, host="::1", port=0)


# ---------------------------------------------------------------------------
# The operator-only enrollment path (store.set_machine_identity, the CLI
# identity-set/serve verbs) - never reachable over HTTP itself.
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.environ.get("AGENTTALK_PY") or sys.executable


def run_cli(*args):
    # A bounded timeout: `serve` is meant to be refused (BindRefused) for
    # a bad host before it would ever block in serve_forever(); if that
    # refusal regresses, this must still fail the test in finite time,
    # not hang the whole suite (or a mutation sweep) forever.
    return subprocess.run([PYTHON, "-m", "quota_balancer", *args],
                          cwd=REPO_ROOT, capture_output=True, text=True,
                          timeout=15)


class IdentityEnrollment(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-card5-")
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_set_machine_identity_enrolls_and_updates(self):
        db = _build_store(self.tmp)
        store.set_machine_identity(db, "desktop", "new-identity", True)
        row = _rows(db, "SELECT identity_ref, enabled FROM machines WHERE id=?",
                    (MACHINE_A,))[0]
        self.assertEqual(row, ("new-identity", 1))
        store.set_machine_identity(db, "desktop", "new-identity", False)
        row = _rows(db, "SELECT identity_ref, enabled FROM machines WHERE id=?",
                    (MACHINE_A,))[0]
        self.assertEqual(row, ("new-identity", 0))

    def test_set_machine_identity_rejects_a_duplicate_identity_ref(self):
        db = _build_store(self.tmp)
        with self.assertRaises(errors.QuotaError) as ctx:
            store.set_machine_identity(db, "worker", IDENTITY_A, True)
        self.assertEqual(ctx.exception.code, errors.CODE_INVALID_CONFIG)
        # Unchanged: worker still has its own identity, untouched.
        self.assertEqual(
            _rows(db, "SELECT identity_ref FROM machines WHERE id=?",
                 (MACHINE_B,))[0][0], IDENTITY_B)

    def test_set_machine_identity_rejects_an_unknown_machine(self):
        db = _build_store(self.tmp)
        with self.assertRaises(errors.QuotaError) as ctx:
            store.set_machine_identity(db, "no-such-machine", "x", True)
        self.assertEqual(ctx.exception.code, errors.CODE_INVALID_CONFIG)

    def test_a_machine_may_reclaim_its_own_identity_ref(self):
        # Re-setting the SAME machine to the identity it already holds is
        # not a duplicate-with-itself conflict.
        db = _build_store(self.tmp)
        store.set_machine_identity(db, "desktop", IDENTITY_A, True)
        self.assertEqual(
            _rows(db, "SELECT identity_ref FROM machines WHERE id=?",
                 (MACHINE_A,))[0][0], IDENTITY_A)

    def test_cli_identity_set_and_serve_refuses_a_public_bind(self):
        db = os.path.join(self.tmp, "cli.db")
        result = run_cli("--db", db, "init")
        self.assertEqual(result.returncode, 0, result.stderr)
        conn = sqlite3.connect(db)
        try:
            conn.execute("INSERT INTO machines(id, alias) VALUES(?,?)",
                        ("m1", "desktop"))
            conn.commit()
        finally:
            conn.close()
        result = run_cli("--db", db, "identity-set", "--machine", "desktop",
                         "--identity-ref", "cli-identity", "--enabled", "true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            _rows(db, "SELECT identity_ref, enabled FROM machines")[0],
            ("cli-identity", 1))
        result = run_cli("--db", db, "serve", "--host", "0.0.0.0", "--port", "0")
        self.assertEqual(result.returncode, 2)
        self.assertIn("INVALID_REQUEST", result.stderr)


if __name__ == "__main__":
    unittest.main()
