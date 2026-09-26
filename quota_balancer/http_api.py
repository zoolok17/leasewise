"""Card 5: thin /v1 HTTP handlers over the tested store (cards 1-4) and
policy (card 3), an identity allowlist and the host ingress adapter
(docs/DESIGN.md sections 6-7).

This module adds NOTHING to admission/reconciliation logic: every route
authenticates the caller, enforces the authorization rules the store
itself does not (a caller may only act as its own machine; a reporter
must be authorized for the account it names), adapts the section-7 wire
shape to the tested store's internal request shape (never exposing the
store's own implementation-only fields), and then calls the exact same
lease.grant/check/settle/record_limit_event or store.reading_import/
status the CLI and the card-3/4 tests already exercise. No admission,
freshness or reconciliation rule is reimplemented here.

Identity allowlist: docs/DESIGN.md section 6 says "Ingress replaces
identity headers" and "loopback writes also require client identity".
Section 6 also says this product is COOPERATIVE, not adversarial, against
a co-resident process ("Normal agent shells often share the wrapper/
reporter OS owner..."); this module's threat model matches that: an
`X-Machine-Identity` header names the caller, looked up against
machines.identity_ref/enabled (card 5's migration-6 columns). A real
private tailnet ingress terminates TLS and OVERWRITES that header with
its own verified value before forwarding (out of this card's scope: "no
custom TLS stack"); a loopback caller's header is trusted as presented,
same as any other same-host cooperative signal. 401 UNAUTHENTICATED is
the ONE code for "no identity presented at all"; every other identity or
ownership failure (unknown/disabled machine, wrong machine for this
lease/seat/report, a lease token that does not match, an unauthorized
reporter) is 403 FORBIDDEN - "cross-machine denial" in the card row's own
words.

Lease tokens: docs/DESIGN.md section 6 ("Opaque random 256-bit lease
tokens are hashed, scoped to machine/seat/account/turn, redacted and
revocable") and section 7 ("Replay returns 200 and rotates a non-null
token hash... Terminal replay returns terminal state without a token...
metered_advisory... has null token/money_hold and cannot pass /check...
Shadow records have null token/money_hold... and cannot pass /check")
describe a token the schema has a column for (leases.token_hash) but
that no store function mints (STEP-CARD-4.md: "token_hash is card 5").
This module mints it: a fresh 256-bit token on every grant response whose
lease is both non-terminal AND enforcing (fresh or replayed), hashed
(sha256) into token_hash, the raw value returned to the caller exactly
once and never stored. `check` verifies the presented token hashes to the
SAME value, that the caller is the lease's own machine, AND that the
lease's enforcement is not metered_advisory/shadow (fix round
tk-7d7fd9023f8c finding 1: gate BOTH token issuance and /check by
enforcement mode, not merely by state). `settle` does NOT require the
token at all - "Reporter identity, not a wrapper token alone, authorizes
money settlement" (section 6): a stolen/leaked token cannot settle money
by itself.

No custom TLS stack, no admin API (identity/allowlist enrollment is a
local, operator-only store function - store.set_machine_identity - never
exposed here), no live provider calls, no wrapper/gateway integration
(cards 6a/6b/7): out of scope per docs/DESIGN.md and the work order.
"""

import hashlib
import http.server
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import tempfile
import time
import urllib.parse

from . import errors, lease, migrate, policy, store
from .errors import QuotaError
from .validate_readings import (
    DOC_VERSION,
    DOC_VERSION_BILLING,
    KIND_CLI_WINDOW,
    KIND_GATEWAY_SNAPSHOT,
    KIND_PROVIDER_BILLING,
    _is_alias,
    _is_uuid,
)

# Section 7: "reject...unknown fields/enums", extended here to the wire
# transport itself. A body above this bound is refused before it is even
# parsed (design is silent on the exact bound or its wire status; this is
# a decision, recorded in docs/STEP-CARD-5.md).
MAX_BODY_BYTES = 65536

IDENTITY_HEADER = "X-Machine-Identity"
TOKEN_HEADER = "Authorization"
TOKEN_SCHEME = "Bearer "
IDEMPOTENCY_HEADER = "Idempotency-Key"

DEFAULT_HOST = "127.0.0.1"

# metered_advisory/shadow are non-enforcing: section 7 requires a null
# token and a refused /check for both, not merely for a terminal state
# (fix round tk-7d7fd9023f8c finding 1).
_NON_ENFORCING = frozenset({"metered_advisory", "shadow"})

_LEASE_ACTION = re.compile(r"^/v1/leases/([^/]+)/(check|settle)$")

# The section-7 error-code -> wire-status mapping, extended with the two
# card-5 transport-only codes (401/403 are never a CLI exit path: there
# is no client identity to authenticate off the CLI).
_HTTP_STATUS = {
    errors.CODE_INVALID_CONFIG: 400,
    errors.CODE_INVALID_REQUEST: 400,
    errors.CODE_SCHEMA_UNSUPPORTED: 400,
    errors.CODE_CONTEXT_PROJECTION_EXCEEDED: 400,
    errors.CODE_UNAUTHENTICATED: 401,
    errors.CODE_POLICY_DENIED: 403,
    errors.CODE_FORBIDDEN: 403,
    errors.CODE_NOT_FOUND: 404,
    errors.CODE_IDEMPOTENCY_CONFLICT: 409,
    errors.CODE_RECEIPT_CONFLICT: 409,
    errors.CODE_LEASE_EXPIRED: 410,
    errors.CODE_LEASE_CLOSED: 410,
    errors.CODE_INPUT_LIMIT: 429,
    errors.CODE_MONEY_LIMIT: 429,
    errors.CODE_WINDOW_LIMIT: 429,
    errors.CODE_ACCOUNT_PARKED: 429,
    errors.CODE_CONCURRENCY_LIMIT: 429,
    errors.CODE_STORE_ERROR: 503,
}


class BindRefused(Exception):
    """Raised before any socket is created: docs/DESIGN.md section 6,
    "No public/wildcard listener"."""


def check_bind_host(host):
    """Pure: refuse any host that is not a literal AF_INET loopback
    address.

    A bare hostname - including "localhost" - is refused too: trusting
    it would add a DNS/hosts-file resolution dependency this product
    does not have (section 6 names an explicit machine ALLOWLIST, not
    name resolution). IPv6 (including "::1") is refused too (fix round
    tk-7d7fd9023f8c finding 8): `make_server` builds an AF_INET-only
    `ThreadingHTTPServer`, so accepting an IPv6 literal here and failing
    at construction (or worse, at the first connection) is worse than
    refusing it up front; serving IPv6 is a real but separate feature,
    not implemented by this build. Callers needing loopback pass
    DEFAULT_HOST (or a literal "127.x.x.x") explicitly.
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        raise BindRefused(host)
    if not isinstance(addr, ipaddress.IPv4Address) or not addr.is_loopback:
        raise BindRefused(host)


# ---------------------------------------------------------------------------
# Small, reusable primitives
# ---------------------------------------------------------------------------

def _hash_token(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _new_token():
    # Opaque random 256-bit lease token (docs/DESIGN.md section 6).
    return secrets.token_hex(32)


def _store_fault(exc):
    raise QuotaError(errors.CODE_STORE_ERROR, errors.MSG_STORE_FAILED) from exc


def _authenticate(conn, header_value):
    """The identity allowlist gate. Missing header -> 401; unknown or
    disabled machine -> 403 (never distinguished on the wire - "foreign
    identity" covers both, section 8 row 5)."""
    if not header_value:
        raise QuotaError(errors.CODE_UNAUTHENTICATED, errors.MSG_UNAUTHENTICATED)
    try:
        row = conn.execute(
            "SELECT id, alias, enabled FROM machines WHERE identity_ref=?",
            (header_value,)).fetchone()
    except sqlite3.Error as exc:
        _store_fault(exc)
    if row is None or not row[2]:
        raise QuotaError(errors.CODE_FORBIDDEN, errors.MSG_FORBIDDEN)
    return row[0], row[1]  # (machine_id, alias)


def _authorized_reporter_for_account(conn, machine_id, account_id):
    """A machine is an authorized reporter for an account if one of its
    seats' project has been given that account (the same project_accounts
    chain grant()'s own authorization already uses, section 7: "children
    share project authorization"). This is the missing per-account
    authorization for reporting a reading (fix round tk-7d7fd9023f8c
    finding 2): the gateway_snapshot reducer already binds to its
    gateway's own machine+account, and provider_billing to the account's
    operator-configured reporter_machine; cli_window has neither, so it
    is the one kind this check gates."""
    try:
        row = conn.execute(
            "SELECT 1 FROM seats s JOIN teams t ON t.id = s.team_id "
            "JOIN project_accounts pa ON pa.project_id = t.project_id "
            "WHERE s.machine_id=? AND pa.account_id=? LIMIT 1",
            (machine_id, account_id)).fetchone()
    except sqlite3.Error as exc:
        _store_fault(exc)
    return row is not None


def _parse_object(raw):
    # Not itself dict-checked: every caller either narrows with its own
    # isinstance(doc, dict) guard, or hands the parsed value straight to
    # a store validator that already rejects a non-dict document.
    try:
        return json.loads(raw.decode("utf-8"),
                          object_pairs_hook=store._reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError, QuotaError):
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)


def _envelope(now_ms, body):
    # Section 7: "All /v1 routes... responses contain server_at,
    # policy_revision." No policy-revision concept exists in any card
    # built so far; SCHEMA_VERSION is the closest stable integer this
    # build can honestly report (a decision, recorded in
    # docs/STEP-CARD-5.md).
    out = dict(body)
    out.setdefault("server_at", now_ms)
    out.setdefault("policy_revision", migrate.SCHEMA_VERSION)
    return out


def _check_idempotency_key(key, expected_id):
    """Section 7: "Mutations use Idempotency-Key=request_id/event_id;
    same ID/content replays, different content =>409." Fix round
    tk-7d7fd9023f8c finding 4: the header was never inspected at all, so
    a client could reuse the same key across genuinely different
    requests undetected. Validated here, BEFORE the store's own
    transactional replay/conflict logic runs: format (a UUID, like every
    other id in this contract) and agreement with the request's own
    id - a mismatch is a malformed request (400), never silently
    resolved by preferring one value over the other. Content reuse under
    an agreeing key is still the store's own job (IDEMPOTENCY_CONFLICT,
    409) - this check cannot and does not replace it."""
    if not key or not _is_uuid(key) or key != expected_id:
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)


# ---------------------------------------------------------------------------
# Wire <-> store adapters (fix round tk-7d7fd9023f8c finding 3)
#
# Section 7's request/response bodies are never the store's own internal
# shapes (request_hash, task_class, batch reading documents, settle's
# lease_id/observed_at/received_at are store/CLI implementation details,
# not wire fields); each adapter below is the ONLY place that bridges the
# two, deriving server-owned fields itself rather than requiring the
# client to supply them.
# ---------------------------------------------------------------------------

_WIRE_GRANT_KEYS = frozenset({
    "request_id", "turn_id", "seat_id", "task_class", "model_id", "effort",
    "gateway_id", "parent_lease_id", "context", "expected_calls", "max_calls",
    "output_cap", "per_call_output_cap", "ttl_ms"})
_WIRE_SETTLE_KEYS = frozenset({
    "event_id", "outcome", "calls", "input_tokens", "cached_input_tokens",
    "output_tokens", "actual_micro_eur", "gateway_id", "seq", "final"})
_WIRE_READING_KEYS = frozenset({
    "event_id", "account_id", "seat_id", "observed_at", "kind", "payload"})
_WIRE_LIMIT_EVENT_KEYS = frozenset({
    "event_id", "account_id", "model_id", "turn_id", "observed_at",
    "evidence_kind", "resets_at", "exhausted_windows"})


def _wire_to_grant(body):
    """POST /v1/leases: section 7's documented body has no request_hash
    (a card-4 implementation artifact - the store computes its OWN
    content hash over every field except this one and uses THAT for
    idempotency; the client-visible field only needs to be shaped like a
    hash, its value is never compared) and adds task_class (no card
    built gives it an independent store slot - STEP-CARD-4.md: "task_class
    has no design taxonomy in card-4 scope - the column stores the effort
    alias"). task_class is validated for shape and then dropped: accepted
    for wire compliance, not silently ignored without record (the drop is
    documented here and in docs/STEP-CARD-5.md as the exact missing
    dependency)."""
    if not isinstance(body, dict) or frozenset(body) != _WIRE_GRANT_KEYS:
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    if not isinstance(body["task_class"], str) or not _is_alias(body["task_class"]):
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    internal = {k: v for k, v in body.items() if k != "task_class"}
    internal["request_hash"] = "0" * 64
    return internal


def _wire_to_settle(lease_id, body, now_ms):
    """POST /v1/leases/{id}/settle: the lease id is the URL path, not a
    body field; observed_at/received_at are server-derived receipt
    timing - the wire body has no place for the client to state them, so
    both collapse to the server's receipt instant (a decision, recorded
    in docs/STEP-CARD-5.md: this loses any client-side timing precision
    the design might separately have wanted)."""
    if not isinstance(body, dict) or frozenset(body) != _WIRE_SETTLE_KEYS:
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    if "final" not in body or type(body["final"]) is not bool:
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    receipt = dict(body)
    receipt["final"] = int(receipt["final"])
    receipt["lease_id"] = lease_id
    receipt["observed_at"] = now_ms
    receipt["received_at"] = now_ms
    return receipt


def _wire_to_reading(body, alias, conn):
    """POST /v1/readings: the documented body is one flat reading
    ({event_id,account_id,seat_id?,observed_at,kind,payload}); the only
    tested, store-backed path is the card-2/2b-1 BATCH document
    ({version,readings:[{event_id,machine,account,kind,observed_at,
    payload}]}). This adapter resolves account_id (a store UUID) to its
    alias (what the batch document actually carries) and supplies
    `machine` implicitly as the AUTHENTICATED caller's own alias - never
    a client-suppliable field, which is exactly the "owner reporter" rule
    (section 7) enforced by construction rather than by a separate check.

    seat_id is validated (null, or an existing seat) but NOT persisted:
    store._apply_reading hardcodes readings.seat_id to NULL - no card
    built so far threads a seat_id through the reduction pipeline. This
    is the exact missing dependency reported (not silently omitted) in
    docs/STEP-CARD-5.md; forking validate_readings/store's tested reading
    pipeline to add it is out of this fix round's scope.
    """
    if not isinstance(body, dict) or frozenset(body) != _WIRE_READING_KEYS:
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    kind = body["kind"]
    if kind not in (KIND_CLI_WINDOW, KIND_GATEWAY_SNAPSHOT, KIND_PROVIDER_BILLING):
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    if not _is_uuid(body["event_id"]):
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    account_id = body["account_id"]
    if account_id is not None and not _is_uuid(account_id):
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    seat_id = body["seat_id"]
    if seat_id is not None and not _is_uuid(seat_id):
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    account_alias = None
    if account_id is not None:
        try:
            row = conn.execute("SELECT alias FROM accounts WHERE id=?",
                               (account_id,)).fetchone()
        except sqlite3.Error as exc:
            _store_fault(exc)
        if row is None:
            raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
        account_alias = row[0]
    if seat_id is not None:
        try:
            row = conn.execute("SELECT 1 FROM seats WHERE id=?",
                               (seat_id,)).fetchone()
        except sqlite3.Error as exc:
            _store_fault(exc)
        if row is None:
            raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    version = DOC_VERSION
    payload = body["payload"]
    if kind == KIND_PROVIDER_BILLING or (
            kind == KIND_GATEWAY_SNAPSHOT and isinstance(payload, dict)
            and "settled_micro_eur" in payload):
        version = DOC_VERSION_BILLING
    reading = {
        "event_id": body["event_id"], "machine": alias, "account": account_alias,
        "kind": kind, "observed_at": body["observed_at"], "payload": payload}
    return account_id, {"version": version, "readings": [reading]}


def _wire_to_limit_event(body):
    if not isinstance(body, dict) or frozenset(body) != _WIRE_LIMIT_EVENT_KEYS:
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    return dict(body)


# ---------------------------------------------------------------------------
# Route handlers (pure of the socket/protocol layer - directly testable)
# ---------------------------------------------------------------------------

def handle_status(db_path, now_ms):
    return 200, store.status(db_path)


def handle_grant(db_path, identity, body, now_ms, idempotency_key):
    """POST /v1/leases. Ownership: the request's seat must belong to the
    authenticated machine (the store derives machine_id from the seat,
    section 4/7; nothing there checks who is CALLING). Token: minted (and
    rotated) on every non-terminal, ENFORCING response, fresh or
    replayed; a terminal replay, or a non-enforcing enforcement
    (metered_advisory/shadow), returns no token (section 7: "Terminal
    replay returns terminal state without a token"; "[metered_advisory/
    shadow]... has null token/money_hold and cannot pass /check" - fix
    round tk-7d7fd9023f8c finding 1)."""
    machine_id, _alias = identity
    internal = _wire_to_grant(body)
    _check_idempotency_key(idempotency_key, internal["request_id"])
    seat_id = internal.get("seat_id")
    if isinstance(seat_id, str):
        conn = store._connect(db_path, must_exist=True)
        try:
            row = conn.execute("SELECT machine_id FROM seats WHERE id=?",
                               (seat_id,)).fetchone()
        except sqlite3.Error as exc:
            _store_fault(exc)
        finally:
            conn.close()
        if row is not None and row[0] != machine_id:
            raise QuotaError(errors.CODE_FORBIDDEN, errors.MSG_FORBIDDEN)
    result = lease.grant(db_path, internal, now_ms)
    lease_token = None
    if result["state"] != "settled" and result["enforcement"] not in _NON_ENFORCING:
        lease_token = _new_token()
        _set_token_hash(db_path, result["lease_id"], _hash_token(lease_token))
    wire = {k: result[k] for k in (
        "lease_id", "state", "enforcement", "money_hold", "caps",
        "granted_at", "expires_at")}
    wire["lease_token"] = lease_token
    # Not populated by the tested store surface (section 7 lists them in
    # the /leases response; no card built so far computes them for a
    # fresh grant). Emitted empty rather than fabricated - a residual gap,
    # recorded in docs/STEP-CARD-5.md.
    wire["windows"] = []
    wire["warnings"] = []
    status_code = 200 if result.get("replayed") else 201
    return status_code, wire


def _set_token_hash(db_path, lease_id, token_hash):
    conn = store._connect(db_path, must_exist=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("UPDATE leases SET token_hash=? WHERE id=?",
                         (token_hash, lease_id))
            conn.execute("COMMIT")
        except BaseException:
            migrate.rollback_quietly(conn)
            raise
    except sqlite3.Error as exc:
        _store_fault(exc)
    finally:
        conn.close()


def handle_check(db_path, identity, lease_id, body, token, now_ms):
    """POST /v1/leases/{id}/check. "Bound token/machine" (section 7): the
    caller must be the lease's own machine, present the current lease
    token, AND the lease's enforcement must be an enforcing mode (fix
    round tk-7d7fd9023f8c finding 1: metered_advisory/shadow "cannot pass
    /check", gated explicitly here rather than relying only on their
    token always being null). Any of these failing is 403 (never
    distinguished from an unknown machine on the wire)."""
    machine_id, _alias = identity
    if not isinstance(body, dict) or set(body) != {"request_id"} \
            or not isinstance(body["request_id"], str) \
            or not _is_uuid(body["request_id"]):
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    conn = store._connect(db_path, must_exist=True)
    try:
        row = conn.execute(
            "SELECT machine_id, token_hash, enforcement FROM leases WHERE id=?",
            (lease_id,)).fetchone()
    except sqlite3.Error as exc:
        _store_fault(exc)
    finally:
        conn.close()
    if row is not None:
        owner_machine, token_hash, enforcement = row
        presented = _hash_token(token) if token else None
        if (owner_machine != machine_id or enforcement in _NON_ENFORCING
                or token_hash is None or presented != token_hash):
            raise QuotaError(errors.CODE_FORBIDDEN, errors.MSG_FORBIDDEN)
    result = lease.check(db_path, lease_id, now_ms)
    wire = {k: result[k] for k in ("state", "expires_at", "caps")}
    return 200, wire


def handle_settle(db_path, identity, lease_id, body, now_ms, idempotency_key):
    """POST /v1/leases/{id}/settle. Ownership: the caller must be the
    lease's own machine - "Reporter identity, not a wrapper token alone,
    authorizes money settlement" (section 6): no lease token is checked
    here at all. (A lease's bound gateway is always on that SAME machine
    - grant() itself refuses a foreign one, section 4 - so there is no
    separate "gateway machine" to consider here.)"""
    machine_id, _alias = identity
    receipt = _wire_to_settle(lease_id, body, now_ms)
    _check_idempotency_key(idempotency_key, receipt["event_id"])
    conn = store._connect(db_path, must_exist=True)
    try:
        row = conn.execute("SELECT machine_id FROM leases WHERE id=?",
                           (lease_id,)).fetchone()
    except sqlite3.Error as exc:
        _store_fault(exc)
    finally:
        conn.close()
    if row is not None and row[0] != machine_id:
        raise QuotaError(errors.CODE_FORBIDDEN, errors.MSG_FORBIDDEN)
    result = lease.settle(db_path, receipt, now_ms)
    wire = {k: result[k] for k in
            ("state", "released_micro_eur", "retained_micro_eur")}
    return 200, wire


def handle_readings(db_path, identity, doc, now_ms, idempotency_key):
    """POST /v1/readings. "Owner reporter": the reporting machine is
    ALWAYS the authenticated caller (never a client-suppliable field, see
    _wire_to_reading). "Reporter authorized for account" additionally
    requires - for cli_window specifically - that the caller's machine is
    an authorized reporter for the named account via the same
    project_accounts chain grant() uses (fix round tk-7d7fd9023f8c
    finding 2): gateway_snapshot already binds to its gateway's own
    machine+account and provider_billing to the account's configured
    reporter_machine, so only cli_window is unprotected."""
    machine_id, alias = identity
    conn = store._connect(db_path, must_exist=True)
    try:
        account_id, translated = _wire_to_reading(doc, alias, conn)
        _check_idempotency_key(idempotency_key, translated["readings"][0]["event_id"])
        if (translated["readings"][0]["kind"] == KIND_CLI_WINDOW
                and account_id is not None
                and not _authorized_reporter_for_account(conn, machine_id, account_id)):
            raise QuotaError(errors.CODE_FORBIDDEN, errors.MSG_FORBIDDEN)
    finally:
        conn.close()
    fd, tmp_path = tempfile.mkstemp(prefix="quota-reading-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(translated, handle)
        store.reading_import(db_path, tmp_path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return 202, {"event_id": translated["readings"][0]["event_id"], "accepted": True}


def handle_limit_event(db_path, identity, body, now_ms, idempotency_key):
    """POST /v1/limit-events. "Reporter authorized for account" (section
    7): the same project_accounts authorization readings' cli_window kind
    now uses (fix round tk-7d7fd9023f8c findings 2 and 5)."""
    machine_id, _alias = identity
    event = _wire_to_limit_event(body)
    _check_idempotency_key(idempotency_key, event.get("event_id")
                          if isinstance(event.get("event_id"), str) else None)
    account_id = event.get("account_id")
    conn = store._connect(db_path, must_exist=True)
    try:
        authorized = (isinstance(account_id, str)
                     and _authorized_reporter_for_account(conn, machine_id, account_id))
    finally:
        conn.close()
    if not authorized:
        raise QuotaError(errors.CODE_FORBIDDEN, errors.MSG_FORBIDDEN)
    result = lease.record_limit_event(db_path, event, now_ms)
    return 202, dict(result)


def handle_plan(db_path, project_id, task_class, now_ms):
    """GET /v1/plan?project_id=...: "ordered candidates/reasons/context/
    caps/retry_at, no hold" (section 7). No card built provides a
    project-scoped request-time context (POST /leases takes context from
    the CLIENT, per request); `context` is honestly reported null here
    rather than fabricated (fix round tk-7d7fd9023f8c finding 5 - "report
    the exact missing dependency"). `task_class` is a required query
    parameter this build adds: section 7's own shown URL is a
    "placeholder, not a runnable command", and a profiles row is keyed
    by task_class with no other way to select one. `fallback` tiers have
    no backing column anywhere in the profiles table (card 3 never added
    one); policy.select_tier is called with fallback=[] - a second
    reported gap, not a silent one.
    """
    if not _is_uuid(project_id) or not isinstance(task_class, str) \
            or not _is_alias(task_class):
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    conn = store._connect(db_path, must_exist=True)
    try:
        if conn.execute("SELECT 1 FROM projects WHERE id=?",
                        (project_id,)).fetchone() is None:
            raise QuotaError(errors.CODE_NOT_FOUND, errors.MSG_NOT_FOUND)
        profile = conn.execute(
            "SELECT candidates, max_calls, output_cap, per_call_output_cap "
            "FROM profiles WHERE task_class=?", (task_class,)).fetchone()
        if profile is None:
            raise QuotaError(errors.CODE_NOT_FOUND, errors.MSG_NOT_FOUND)
        candidates_json, max_calls, output_cap, per_call_output_cap = profile
        try:
            candidates = json.loads(candidates_json)
        except ValueError as exc:
            _store_fault(exc)
        if not isinstance(candidates, list) or not candidates or not all(
                isinstance(a, str) for a in candidates):
            raise QuotaError(errors.CODE_STORE_ERROR, errors.MSG_STORE_FAILED)
        account_ids = [r[0] for r in conn.execute(
            "SELECT account_id FROM project_accounts WHERE project_id=?",
            (project_id,)).fetchall()]
        offered = {}
        blocked_account_wide, blocked_model_ids, retry_ats = set(), set(), []
        if account_ids:
            placeholders = ",".join("?" * len(account_ids))
            for alias, model_id, account_id in conn.execute(
                    "SELECT alias, id, account_id FROM models "
                    "WHERE account_id IN (%s)" % placeholders, account_ids):
                offered.setdefault(alias, []).append((account_id, model_id))
            for account_id, model_id, retry_at in conn.execute(
                    "SELECT account_id, model_id, retry_at FROM blocks "
                    "WHERE account_id IN (%s) AND state IN "
                    "('parked','probing')" % placeholders, account_ids):
                if model_id is None:
                    blocked_account_wide.add(account_id)
                else:
                    blocked_model_ids.add(model_id)
                retry_ats.append(retry_at)
        blocked = set()
        for alias in candidates:
            entries = offered.get(alias)
            if not entries or all(
                    a in blocked_account_wide or m in blocked_model_ids
                    for a, m in entries):
                blocked.add(alias)
        decision = policy.select_tier(candidates, [], blocked)
        retry_at = None
        if decision["tier"] == "park":
            known = [r for r in retry_ats if r is not None]
            retry_at = max(known) if known else None
        return 200, {
            "candidates": [{"model": alias, "blocked": alias in blocked}
                          for alias in candidates],
            "reasons": {alias: ("blocked" if alias in blocked else "available")
                       for alias in candidates},
            "context": None,
            "caps": {"max_calls": max_calls, "output_cap": output_cap,
                    "per_call_output_cap": per_call_output_cap},
            "retry_at": retry_at,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The socket/protocol layer
# ---------------------------------------------------------------------------

def _read_body(handler):
    length = handler.headers.get("Content-Length")
    if length is None:
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    try:
        n = int(length)
    except ValueError:
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    if n < 0 or n > MAX_BODY_BYTES:
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    return handler.rfile.read(n) if n else b""


def _send(handler, status_code, payload):
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _error_body(now_ms, code, message, retry_at=None):
    return _envelope(now_ms, {"error": {
        "code": code, "message": message, "retry_at": retry_at, "details": {}}})


def _send_error(handler, now_ms, exc):
    # No raw exception text, path or header value ever reaches the wire
    # (privacy_5): only the typed code, its static message and retry_at.
    status_code = _HTTP_STATUS.get(exc.code, 500)
    _send(handler, status_code,
         _error_body(now_ms, exc.code, exc.message, exc.retry_at))


class RequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "quota-http/1"

    def log_message(self, format, *args):
        # Never write request lines (paths, headers, query strings) to
        # any stream: privacy_5 applies to logging, not only responses.
        return

    def send_error(self, code, message=None, explain=None):
        # Fix round tk-7d7fd9023f8c finding 6: BaseHTTPRequestHandler's
        # OWN protocol-error path (an unsupported method, a malformed
        # request line - raised before _handle ever runs) calls this
        # directly with an attacker-controlled `message` (e.g. the raw
        # method text) and its default implementation embeds that text,
        # UNESCAPED, in an HTML body. `message`/`explain` are deliberately
        # never read: only the typed, static, closed JSON envelope leaves
        # this process, same as every other error response.
        now_ms = int(time.time() * 1000)
        error_code = (errors.CODE_STORE_ERROR if code >= 500
                     else errors.CODE_INVALID_REQUEST)
        error_message = (errors.MSG_STORE_FAILED if code >= 500
                        else errors.MSG_INVALID_REQUEST)
        body = _error_body(now_ms, error_code, error_message)
        try:
            _send(self, code, body)
        except Exception:
            pass

    def _bearer(self):
        raw = self.headers.get(TOKEN_HEADER)
        if raw and raw.startswith(TOKEN_SCHEME):
            return raw[len(TOKEN_SCHEME):]
        return None

    def _handle(self, method):
        now_ms = int(time.time() * 1000)
        db_path = self.server.db_path
        try:
            split = urllib.parse.urlsplit(self.path)
            path = split.path
            idempotency_key = self.headers.get(IDEMPOTENCY_HEADER)
            conn = store._connect(db_path, must_exist=True)
            try:
                identity = _authenticate(conn, self.headers.get(IDENTITY_HEADER))
            finally:
                conn.close()
            if method == "GET" and path == "/v1/status":
                status_code, wire = handle_status(db_path, now_ms)
            elif method == "GET" and path == "/v1/plan":
                query = urllib.parse.parse_qs(split.query)
                project_id = (query.get("project_id") or [None])[0]
                task_class = (query.get("task_class") or [None])[0]
                if project_id is None or task_class is None:
                    raise QuotaError(errors.CODE_INVALID_REQUEST,
                                     errors.MSG_INVALID_REQUEST)
                status_code, wire = handle_plan(db_path, project_id, task_class,
                                                now_ms)
            elif method == "POST" and path == "/v1/leases":
                body = _parse_object(_read_body(self))
                status_code, wire = handle_grant(db_path, identity, body, now_ms,
                                                 idempotency_key)
            elif method == "POST" and path == "/v1/readings":
                doc = _parse_object(_read_body(self))
                status_code, wire = handle_readings(
                    db_path, identity, doc, now_ms, idempotency_key)
            elif method == "POST" and path == "/v1/limit-events":
                body = _parse_object(_read_body(self))
                status_code, wire = handle_limit_event(
                    db_path, identity, body, now_ms, idempotency_key)
            else:
                match = _LEASE_ACTION.match(path)
                if method == "POST" and match:
                    lease_id, action = match.group(1), match.group(2)
                    body = _parse_object(_read_body(self))
                    if action == "check":
                        status_code, wire = handle_check(
                            db_path, identity, lease_id, body,
                            self._bearer(), now_ms)
                    else:
                        status_code, wire = handle_settle(
                            db_path, identity, lease_id, body, now_ms,
                            idempotency_key)
                else:
                    raise QuotaError(errors.CODE_NOT_FOUND, errors.MSG_NOT_FOUND)
            _send(self, status_code, _envelope(now_ms, wire))
        except QuotaError as exc:
            _send_error(self, now_ms, exc)
        except Exception:
            # Last-resort guard (defense in depth): an unexpected internal
            # fault never reaches the wire as a traceback or raw text.
            _send_error(self, now_ms, QuotaError(
                errors.CODE_STORE_ERROR, errors.MSG_STORE_FAILED))

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def make_server(db_path, host=DEFAULT_HOST, port=0):
    """Build (but do not start) the server. Refuses a public/wildcard
    (or IPv6) bind BEFORE any socket is created (docs/DESIGN.md section 6)."""
    check_bind_host(host)
    httpd = http.server.ThreadingHTTPServer((host, port), RequestHandler)
    httpd.db_path = db_path
    return httpd
