"""Card 5: thin /v1 HTTP handlers over the tested store (cards 1-4) and
policy (card 3), an identity allowlist and the host ingress adapter
(docs/DESIGN.md sections 6-7).

This module adds NOTHING to admission/reconciliation logic: every route
is a thin wrapper that authenticates the caller, enforces the one
authorization rule the store itself does not (a caller may only act as
its OWN machine - grant/check/settle/report are never store-validated
against the transport identity), and then calls the exact same
lease.grant/check/settle or store.reading_import/status the CLI and the
card-3/4 tests already exercise. No validation is forked; no admission,
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
lease/seat/report, a lease token that does not match) is 403 FORBIDDEN -
"cross-machine denial" in the card row's own words.

Lease tokens: docs/DESIGN.md section 6 ("Opaque random 256-bit lease
tokens are hashed, scoped to machine/seat/account/turn, redacted and
revocable") and section 7 ("Replay returns 200 and rotates a non-null
token hash... Terminal replay returns terminal state without a token")
describe a token the schema has a column for (leases.token_hash) but
that no store function mints (STEP-CARD-4.md: "token_hash is card 5").
This module mints it: a fresh 256-bit token on every non-terminal grant
response (fresh or replayed), hashed (sha256) into token_hash, the raw
value returned to the caller exactly once and never stored. `check`
verifies the presented token hashes to the SAME value AND that the
caller is the lease's own machine ("bound token/machine", section 7).
`settle` does NOT require the token at all - "Reporter identity, not a
wrapper token alone, authorizes money settlement" (section 6): a
stolen/leaked token cannot settle money by itself.

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

from . import errors, lease, migrate, store
from .errors import QuotaError

# Section 7: "reject...unknown fields/enums", extended here to the wire
# transport itself. A body above this bound is refused before it is even
# parsed (design is silent on the exact bound or its wire status; this is
# a decision, recorded in docs/STEP-CARD-5.md).
MAX_BODY_BYTES = 65536

IDENTITY_HEADER = "X-Machine-Identity"
TOKEN_HEADER = "Authorization"
TOKEN_SCHEME = "Bearer "

DEFAULT_HOST = "127.0.0.1"

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
    """Pure: refuse any host that is not a literal loopback address.

    A bare hostname - including "localhost" - is refused too: trusting
    it would add a DNS/hosts-file resolution dependency this product
    does not have (section 6 names an explicit machine ALLOWLIST, not
    name resolution). Callers needing loopback pass DEFAULT_HOST (or a
    literal "127.0.0.1"/"::1") explicitly.
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        raise BindRefused(host)
    if not addr.is_loopback:
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


def _parse_object(raw):
    # Not itself dict-checked: every caller either narrows with its own
    # isinstance(doc, dict) guard, or hands the parsed value straight to
    # a store validator that already rejects a non-dict document
    # (lease._validate_grant_request, validate_readings.validate_readings)
    # - a second check here would be a fork, not a reuse.
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


# ---------------------------------------------------------------------------
# Route handlers (pure of the socket/protocol layer - directly testable)
# ---------------------------------------------------------------------------

def handle_status(db_path, now_ms):
    return 200, store.status(db_path)


def handle_grant(db_path, identity, body, now_ms):
    """POST /v1/leases. Ownership: the request's seat must belong to the
    authenticated machine (the store derives machine_id from the seat,
    section 4/7; nothing there checks who is CALLING). Token: minted (and
    rotated) on every non-terminal response, fresh or replayed; a
    terminal replay (an already-settled lease) returns no token (section
    7: "Terminal replay returns terminal state without a token")."""
    machine_id, _alias = identity
    seat_id = body.get("seat_id") if isinstance(body, dict) else None
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
    result = lease.grant(db_path, body, now_ms)
    lease_token = None
    if result["state"] != "settled":
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
    caller must be the lease's own machine AND present the current lease
    token; either failing is 403 (never distinguished from an unknown
    machine on the wire)."""
    machine_id, _alias = identity
    if not isinstance(body, dict) or set(body) != {"request_id"} \
            or not isinstance(body["request_id"], str):
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    conn = store._connect(db_path, must_exist=True)
    try:
        row = conn.execute(
            "SELECT machine_id, token_hash FROM leases WHERE id=?",
            (lease_id,)).fetchone()
    except sqlite3.Error as exc:
        _store_fault(exc)
    finally:
        conn.close()
    if row is not None:
        owner_machine, token_hash = row
        presented = _hash_token(token) if token else None
        if (owner_machine != machine_id or token_hash is None
                or presented != token_hash):
            raise QuotaError(errors.CODE_FORBIDDEN, errors.MSG_FORBIDDEN)
    result = lease.check(db_path, lease_id, now_ms)
    wire = {k: result[k] for k in ("state", "expires_at", "caps")}
    return 200, wire


def handle_settle(db_path, identity, lease_id, body, now_ms):
    """POST /v1/leases/{id}/settle. Ownership: the caller must be the
    lease's own machine - "Reporter identity, not a wrapper token alone,
    authorizes money settlement" (section 6): no lease token is checked
    here at all. (A lease's bound gateway is always on that SAME machine
    - grant() itself refuses a foreign one, section 4 - so there is no
    separate "gateway machine" to consider here.)

    The wire's `final` is a JSON boolean (STEP-CARD-4.md: "the wire's
    `true` converts in card 5's HTTP layer"); the store rejects a bool
    outright (booleans-as-integers, section 7), so it is converted to
    0/1 here, after the strict type check.
    """
    machine_id, _alias = identity
    if not isinstance(body, dict) or "lease_id" not in body \
            or body["lease_id"] != lease_id:
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    if "final" not in body or type(body["final"]) is not bool:
        raise QuotaError(errors.CODE_INVALID_REQUEST, errors.MSG_INVALID_REQUEST)
    receipt = dict(body)
    receipt["final"] = int(receipt["final"])
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


def handle_readings(db_path, identity, raw, doc, now_ms):
    """POST /v1/readings. "Owner reporter" (section 7): every reading in
    the batch must name the AUTHENTICATED caller's own machine alias; a
    batch naming any other machine is refused whole (403), before the
    document ever reaches store.reading_import.

    The section-7 prose shows a flat single-reading body
    ({event_id,account_id,seat_id?,observed_at,kind,payload}); the only
    tested, store-backed ingestion path is the card-2/2b-1 BATCH document
    ({version,readings:[{event_id,machine,account,kind,observed_at,
    payload}]}), which has no seat_id concept at all. Reusing the tested
    path rather than forking new validation, this endpoint accepts that
    batch document unchanged (a decision, recorded in
    docs/STEP-CARD-5.md); the flat single-reading wire shape is not
    reachable yet.
    """
    _machine_id, alias = identity
    if isinstance(doc, dict) and isinstance(doc.get("readings"), list):
        for item in doc["readings"]:
            if (isinstance(item, dict) and "machine" in item
                    and item["machine"] != alias):
                raise QuotaError(errors.CODE_FORBIDDEN, errors.MSG_FORBIDDEN)
    fd, tmp_path = tempfile.mkstemp(prefix="quota-reading-", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        store.reading_import(db_path, tmp_path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return 202, {"accepted": True}


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


def _send_error(handler, now_ms, exc):
    # No raw exception text, path or header value ever reaches the wire
    # (privacy_5): only the typed code, its static message and retry_at.
    status_code = _HTTP_STATUS.get(exc.code, 500)
    body = _envelope(now_ms, {"error": {
        "code": exc.code, "message": exc.message,
        "retry_at": exc.retry_at, "details": {}}})
    _send(handler, status_code, body)


class RequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "quota-http/1"

    def log_message(self, format, *args):
        # Never write request lines (paths, headers, query strings) to
        # any stream: privacy_5 applies to logging, not only responses.
        return

    def _bearer(self):
        raw = self.headers.get(TOKEN_HEADER)
        if raw and raw.startswith(TOKEN_SCHEME):
            return raw[len(TOKEN_SCHEME):]
        return None

    def _handle(self, method):
        now_ms = int(time.time() * 1000)
        db_path = self.server.db_path
        try:
            path = self.path.split("?", 1)[0]
            conn = store._connect(db_path, must_exist=True)
            try:
                identity = _authenticate(conn, self.headers.get(IDENTITY_HEADER))
            finally:
                conn.close()
            if method == "GET" and path == "/v1/status":
                status_code, wire = handle_status(db_path, now_ms)
            elif method == "POST" and path == "/v1/leases":
                body = _parse_object(_read_body(self))
                status_code, wire = handle_grant(db_path, identity, body, now_ms)
            elif method == "POST" and path == "/v1/readings":
                raw = _read_body(self)
                doc = _parse_object(raw)
                status_code, wire = handle_readings(
                    db_path, identity, raw, doc, now_ms)
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
                            db_path, identity, lease_id, body, now_ms)
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
    bind BEFORE any socket is created (docs/DESIGN.md section 6)."""
    check_bind_host(host)
    httpd = http.server.ThreadingHTTPServer((host, port), RequestHandler)
    httpd.db_path = db_path
    return httpd
