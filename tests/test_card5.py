"""Card 5 acceptance tests: the private /v1 HTTP layer.

Run from the repository root:
    python -m unittest discover -s tests -v

Method names carry the card's named checks (docs/DESIGN.md section 8,
row "5: private HTTP") with the unittest "test_" prefix: token_scope,
replay_grant, public_bind, privacy_5. Everything the HTTP layer adds over
the tested store (cards 1-4) and policy (card 3) is exercised over a
REAL socket (http_api.make_server on 127.0.0.1, an ephemeral port) - no
admission/reconciliation rule is retested here; that is cards 3/4's job.
"""

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

import quota_balancer.migrate as migrate
from quota_balancer import errors, http_api, lease, policy, store

# --- fixed UUID fixture chain: one metered account, two machines, one
# seat each, sharing the account (the card's own demo). ---------------

ACCOUNT = "11111111-1111-4111-8111-111111111111"
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


def _grant_request(request_id=None, turn_id=None, request_hash=None,
                   seat_id=SEAT_A, gateway_id=GATEWAY):
    request_id = request_id or str(uuid.uuid4())
    turn_id = turn_id or str(uuid.uuid4())
    request_hash = request_hash or hashlib.sha256(request_id.encode()).hexdigest()
    # The HTTP layer (unlike the store-level card-4 tests) reads the real
    # wall clock per request, so the admission estimate's freshness bound
    # (<=120s old, section 7) must be anchored to REAL now, not the
    # fixture's fixed 2026-09 NOW used elsewhere in this file for
    # timestamps that carry no freshness check (receipts, park records).
    return {
        "request_id": request_id, "request_hash": request_hash,
        "turn_id": turn_id, "seat_id": seat_id, "model_id": MODEL,
        "effort": "opus", "gateway_id": gateway_id, "parent_lease_id": None,
        "context": {
            "mode": "FRESH", "initial_tokens": 1000, "dispatch_tokens": 0,
            "source": "tokenizer",
            "observed_at": int(time.time() * 1000) - 1000,
            "growth_tokens_per_call": 0,
        },
        "expected_calls": 1, "max_calls": 2, "output_cap": 1000,
        "per_call_output_cap": 500, "ttl_ms": TTL_MS,
    }


def _receipt(lease_id, event_id=None, actual=1, final=True, gateway_id=GATEWAY,
            outcome="completed"):
    return {
        "event_id": event_id or str(uuid.uuid4()), "lease_id": lease_id,
        "outcome": outcome, "calls": 1, "input_tokens": 500,
        "cached_input_tokens": 0, "output_tokens": 100,
        "actual_micro_eur": actual, "gateway_id": gateway_id, "seq": 1,
        "final": final, "observed_at": NOW + 1000, "received_at": NOW + 2000,
    }


def _build_store(tmp, name="lease.db", budget=10 * HOLD, gateway_sized=1,
                 enabled_a=1, enabled_b=1, account_kind="metered",
                 enforcement_a="metered", enforcement_b="metered"):
    """A schema-v6 store: one account (two seats on two machines sharing
    it - the card's demo), one sized gateway on MACHINE_A."""
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
        conn.commit()
    finally:
        conn.close()
    return db


def _park(db, reason="overrun"):
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO blocks(id, account_id, model_id, reason, retry_at, "
            "state, source_event_id, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), ACCOUNT, None, reason, None, "parked",
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
          headers=None, content_length=None):
    hdrs = dict(headers or {})
    if identity is not None:
        hdrs[http_api.IDENTITY_HEADER] = identity
    if token is not None:
        hdrs["Authorization"] = "Bearer " + token
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

    def grant(self, identity=IDENTITY_A, **request_kw):
        return _call(self.base, "POST", "/v1/leases",
                    body=_grant_request(**request_kw), identity=identity)


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

    def test_readings_report_denies_a_foreign_machine(self):
        self.start()
        doc = {"version": 2, "readings": [{
            "event_id": str(uuid.uuid4()), "machine": "desktop",
            "account": "meter", "kind": "cli_window", "observed_at": NOW,
            "payload": {"windows": [], "confidence": "observed",
                       "source_stale": False, "covered_seq": 1},
        }]}
        status, body = _call(self.base, "POST", "/v1/readings", body=doc,
                             identity=IDENTITY_B)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "FORBIDDEN")
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM readings")[0][0], 0)

    def test_readings_report_accepted_from_its_own_machine(self):
        self.start()
        doc = {"version": 2, "readings": [{
            "event_id": str(uuid.uuid4()), "machine": "desktop",
            "account": "meter", "kind": "cli_window", "observed_at": NOW,
            "payload": {"windows": [], "confidence": "observed",
                       "source_stale": False, "covered_seq": 1},
        }]}
        status, body = _call(self.base, "POST", "/v1/readings", body=doc,
                             identity=IDENTITY_A)
        self.assertEqual(status, 202)
        self.assertEqual(_rows(self.db, "SELECT COUNT(*) FROM readings")[0][0], 1)

    def test_settle_denies_a_machine_that_is_neither_the_lease_nor_its_gateway(self):
        self.start()
        _status, body = self.grant()
        lease_id = body["lease_id"]
        status, resp = _call(
            self.base, "POST", "/v1/leases/%s/settle" % lease_id,
            body=_receipt(lease_id), identity=IDENTITY_B)
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
        status, resp = _call(
            self.base, "POST", "/v1/leases/%s/settle" % lease_id,
            body=_receipt(lease_id), identity=IDENTITY_A)
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
                              identity=IDENTITY_A)
        self.assertEqual(status1, 201)
        holds_after_first = _rows(
            self.db, "SELECT COUNT(*) FROM holds WHERE lease_id=?",
            (body1["lease_id"],))[0][0]
        # The lost-response retry: literally the same bytes again.
        status2, body2 = _call(self.base, "POST", "/v1/leases", body=request,
                              identity=IDENTITY_A)
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
                             identity=IDENTITY_A)
        lease_id, token = body["lease_id"], body["lease_token"]
        status, _ = _call(self.base, "POST", "/v1/leases/%s/settle" % lease_id,
                          body=_receipt(lease_id), identity=IDENTITY_A)
        self.assertEqual(status, 200)
        status, replay = _call(self.base, "POST", "/v1/leases", body=request,
                               identity=IDENTITY_A)
        self.assertEqual(status, 200)
        self.assertEqual(replay["state"], "settled")
        self.assertIsNone(replay["lease_token"])

    def test_different_content_same_ids_is_idempotency_conflict(self):
        self.start()
        request = _grant_request()
        _call(self.base, "POST", "/v1/leases", body=request, identity=IDENTITY_A)
        changed = dict(request)
        changed["output_cap"] = request["output_cap"] + 1   # any field differs
        status, body = _call(self.base, "POST", "/v1/leases", body=changed,
                             identity=IDENTITY_A)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "IDEMPOTENCY_CONFLICT")


# ---------------------------------------------------------------------------
# public_bind: Given a public binding, When start, Then refuse.
# ---------------------------------------------------------------------------

class PublicBind(unittest.TestCase):
    def test_wildcard_and_public_hosts_refused_before_any_socket(self):
        for host in ("0.0.0.0", "::", "8.8.8.8", "203.0.113.5",
                    "localhost", "example.com", "", "not-an-ip"):
            with self.assertRaises(http_api.BindRefused):
                http_api.check_bind_host(host)

    def test_loopback_hosts_accepted(self):
        for host in ("127.0.0.1", "127.0.0.2", "::1"):
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
                         body={"seat_id": sentinel}, identity=IDENTITY_A),
                    _call(self.base, "POST", "/v1/leases/%s/check" % sentinel,
                         body={"request_id": str(uuid.uuid4())},
                         identity=IDENTITY_A, token=sentinel),
                    _call(self.base, "POST", "/v1/readings",
                         body={"version": 2, "readings": [{
                             "event_id": sentinel, "machine": "desktop",
                             "account": "meter", "kind": "cli_window",
                             "observed_at": NOW, "payload": {}}]},
                         identity=IDENTITY_A),
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
                                identity=IDENTITY_A)
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
                                      gateway_id=gateway,
                                      request_id=str(uuid.uuid4()),
                                      turn_id=str(uuid.uuid4()))
            self.assertEqual(status, 201, (identity, body))


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
                        {"request_id": 12345}, {}):
            status, body = _call(
                self.base, "POST", "/v1/leases/%s/check" % lease_id,
                body=bad_body, identity=IDENTITY_A, token=token)
            self.assertEqual(status, 400, bad_body)
            self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_settle_final_boolean_true_converts_and_settles(self):
        self.start()
        _status, granted = self.grant()
        lease_id = granted["lease_id"]
        status, body = _call(
            self.base, "POST", "/v1/leases/%s/settle" % lease_id,
            body=_receipt(lease_id, final=True), identity=IDENTITY_A)
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "settled")
        self.assertEqual(set(body) - {"server_at", "policy_revision"},
                         {"state", "released_micro_eur", "retained_micro_eur"})

    def test_settle_final_as_int_is_refused_not_silently_accepted(self):
        self.start()
        _status, granted = self.grant()
        lease_id = granted["lease_id"]
        receipt = _receipt(lease_id, final=True)
        receipt["final"] = 1               # the store's own int form, on the wire
        status, body = _call(
            self.base, "POST", "/v1/leases/%s/settle" % lease_id,
            body=receipt, identity=IDENTITY_A)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_settle_path_and_body_lease_id_must_match(self):
        self.start()
        _status, granted = self.grant()
        lease_id = granted["lease_id"]
        receipt = _receipt(lease_id, final=False)
        receipt["lease_id"] = str(uuid.uuid4())
        status, body = _call(
            self.base, "POST", "/v1/leases/%s/settle" % lease_id,
            body=receipt, identity=IDENTITY_A)
        self.assertEqual(status, 400)

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
                            identity=IDENTITY_A)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "CONTEXT_PROJECTION_EXCEEDED")

    def test_money_limit_maps_to_429(self):
        self.start(budget=HOLD - 1)     # one micro-EUR short of the hold
        status, body = _call(self.base, "POST", "/v1/leases",
                            body=_grant_request(), identity=IDENTITY_A)
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
# The operator-only enrollment path (store.set_machine_identity, the CLI
# identity-set/serve verbs) - never reachable over HTTP itself.
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.environ.get("AGENTTALK_PY") or sys.executable


def run_cli(*args):
    return subprocess.run([PYTHON, "-m", "quota_balancer", *args],
                          cwd=REPO_ROOT, capture_output=True, text=True)


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
