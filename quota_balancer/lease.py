"""Card 4 lease-store operations: transactional grant, check, settle.

Everything here runs against a schema-v5 store (see migrate.py). The
store is a fixture of the coordinator: there is no HTTP layer in this
card (card 5), so these are the store-level operations the HTTP
handlers will wrap. Connections are autocommit (isolation_level=None);
every mutation is one explicit BEGIN IMMEDIATE transaction, so a
concurrent grant serializes behind it and re-reads usage/holds under
the write lock (section 4: "Admission transaction: settled charges +
unresolved holds + new hold <= each applicable cap").

grant: derives the seat's chain (seat -> team -> project), the account
from the model, and the caps from the request context through the
card-3 arithmetic (policy.estimate: E, I, the sized hold H). It then
enforces every applicable limit through policy.check_admission - one
call per (metric, period[, window_key]) - mapping a denial to the
section-7 429 code of the metric that failed (INPUT_LIMIT /
MONEY_LIMIT / WINDOW_LIMIT / CONCURRENCY_LIMIT). The account budget
(accounts.budget_micro_eur, F04) is the one mandatory (money_micro_eur,
month) cap. A parked/probing block on the account (its model, or
model-less) refuses with ACCOUNT_PARKED and the block's retry_at.
Idempotency is by (machine_id, request_id) - the lease's UNIQUE: the
same request_id with the same request_hash replays the stored lease
without adding a hold; a different hash is IDEMPOTENCY_CONFLICT. One
live slot per (seat, turn) is the lease_live partial index: a second
live grant on the same seat+turn is IDEMPOTENCY_CONFLICT. A granted
lease gets one holds row per applicable input/money limit (reserved =
I or H) and, on a metered account, one row for the synthetic
account-limit id ("account:<account_id>" - section 7: account holds use
a reserved synthetic identifier, not a duplicate limits row). Only a
sized metered lease (enforcement=metered) records holds;
metered_advisory / subscription / shadow leases are recorded with
null money_hold and no holds, and admit with zero new hold.

check: re-evaluates on every call (never caches permission, never
extends the TTL): a result only for an active/uncertain lease that is
unexpired and unblocked; settled is LEASE_CLOSED (410), expired is
LEASE_EXPIRED (410), blocked is ACCOUNT_PARKED (429), missing is
NOT_FOUND (404).

settle: records a receipt for the lease. A duplicate event_id with
identical content is a no-op replay; different content is
IDEMPOTENCY_CONFLICT. A final receipt (exactly one per lease - the
receipt_final index) moves the lease to settled, releases every hold
into settled_actual = min(actual, reserved) - never below zero, never
above the hold - and records an observed overrun as liability: a money
overrun becomes an adjustments row (unattributed_micro_eur,
resolved_at NULL: it needs reconciliation, turn 3 blocks on it) plus an
audit row; an input overrun gets an audit row. "Unknown spend is not a
zero final receipt": a final receipt requires actual_micro_eur. A
late final receipt on an expired (uncertain) lease settles it against
the ORIGINAL grant period, exactly once (late_receipt): the holds
keep their grant-time period_start, so the reconciliation lands in
the original bucket even across a day/month boundary; the event_id
makes a replay a no-op and a settled lease is terminal
(RECEIPT_CONFLICT). A conflicting FINAL receipt - a 2nd different
final, or a final naming a foreign gateway - parks the account like
an overrun block (section 4: receipt conflict blocks); a conflicting
non-final is refused without parking. A non-final receipt on an
uncertain lease is still refused (LEASE_EXPIRED): only the gateway's
closed-child final receipt resolves an unprovable outcome.

expire: the explicit expiry path of section 4 ("Expiry -> uncertain,
never a refund"): an active lease whose TTL has elapsed is moved to
uncertain in one BEGIN IMMEDIATE transaction. The outcome cannot be
proved, so the holds stay reserved - the liability is retained,
nothing is released; the lease keeps its live slot (the lease_live
index counts uncertain) and a late final receipt can still settle it.
Idempotent on an already-uncertain lease (replayed=True); a
not-yet-expired lease is INVALID_REQUEST; a settled lease is terminal
(RECEIPT_CONFLICT); a missing lease is NOT_FOUND.

The read-check-insert sequence is one transaction, so the grant_race
property holds: under concurrent grants the sum of unresolved holds
never exceeds a cap, because every grant's admission read happens
under the write lock, after every earlier grant committed.
"""

import datetime
import hashlib
import json
import sqlite3
import uuid as _uuid

from . import errors, policy, reduce
from .errors import QuotaError
from .migrate import rollback_quietly
from .store import _connect
from .validate_config_v2 import PLATFORM_ID

# --- request shapes (closed) ---------------------------------------------------

_GRANT_KEYS = frozenset((
    "request_id", "request_hash", "turn_id", "seat_id", "model_id",
    "task_class", "effort", "gateway_id", "parent_lease_id", "context",
    "expected_calls", "max_calls", "output_cap", "per_call_output_cap",
    "ttl_ms"))
_CONTEXT_KEYS = frozenset((
    "mode", "initial_tokens", "dispatch_tokens", "source", "observed_at",
    "growth_tokens_per_call"))
# Closed enums (section 7): the context mode and its token source.
_MODES = frozenset(("FRESH", "SUMMARY", "RESUME"))
_SOURCES = frozenset(("tokenizer", "cli_observed", "conservative_bound"))
# Card L1b: a wrapper that cannot know the context size says so instead of
# inventing a mode or a number. The unknown form is a closed shape of its
# own (mode UNKNOWN, source unknown, the three sizes null) and is granted to
# shadow seats only.
_MODE_UNKNOWN = "UNKNOWN"
_SOURCE_UNKNOWN = "unknown"

_SETTLE_KEYS = frozenset((
    "event_id", "lease_id", "outcome", "calls", "input_tokens",
    "cached_input_tokens", "output_tokens", "actual_micro_eur",
    "gateway_id", "seq", "final", "observed_at", "received_at"))
_OUTCOMES = frozenset(("completed", "cancelled", "failed",
                       "provider_limit"))

# The 429 code of the metric that failed admission (section 7).
_LIMIT_CODES = {
    "input_tokens": (errors.CODE_INPUT_LIMIT, errors.MSG_INPUT_LIMIT),
    "money_micro_eur": (errors.CODE_MONEY_LIMIT, errors.MSG_MONEY_LIMIT),
    "window_bps": (errors.CODE_WINDOW_LIMIT, errors.MSG_WINDOW_LIMIT),
    "concurrency": (errors.CODE_CONCURRENCY_LIMIT,
                    errors.MSG_CONCURRENCY_LIMIT),
}
# Deterministic refusal order: the mandatory money month first, then
# money, input, windows (by key), concurrency last.
_METRIC_RANK = {"money_micro_eur": 0, "input_tokens": 1, "window_bps": 2,
                "concurrency": 3}
_PERIOD_RANK = {"month": 0, "day": 1, "turn": 2, "window": 0}

_DAY_MS = 86_400_000
# Section 6: profile TTLs are 30m/2h/6h with a 24h absolute maximum;
# ttl_ms can shorten but never exceed it.
_MAX_TTL_MS = _DAY_MS
# The platform singleton scope id: platform caps are the scope='platform'
# limits rows carried by this reserved id. The scope spans every seat,
# so the grant's chain always carries it and every platform row applies
# to every lease (card-3: a chain scope id is always a UUID).
_PLATFORM_ID = PLATFORM_ID  # one definition, shared with config-import v2
# R-003 interim gate (card L1a): False means NO grant is ever an enforcing
# metered lease, whatever a gateway reports. Card R1 replaces this constant
# with a real, explicit activation gate; until it merges the desktop lead's
# decision stands (metered leases stay advisory). Tests that exercise the
# post-activation arithmetic (holds, settlement, expiry) patch it to True;
# nothing in the product sets it.
METERED_ENFORCEMENT_ACTIVATED = False
# Section 7: the admission estimate must be at most 120 seconds old.
_EST_FRESH_MS = 120_000

_LEASE_COLS = (
    "id", "machine_id", "seat_id", "account_id", "model_id", "gateway_id",
    "parent_lease_id", "turn_id", "request_id", "request_hash", "token_hash",
    "task_class", "context_mode", "enforcement", "estimate",
    "policy_snapshot", "input_cap", "output_cap", "max_calls",
    "per_call_output_cap", "money_hold", "granted_at", "expires_at",
    "activity_seq", "period_start", "state", "outcome")


def _account_limit_id(account_id):
    """The synthetic account-limit identifier of section 7 (account holds
    use a reserved identifier, not a duplicate limits row)."""
    return "account:" + account_id


# --- period buckets -------------------------------------------------------------

def _day_start_ms(now_ms):
    return (now_ms // _DAY_MS) * _DAY_MS


def _month_start_ms(now_ms):
    first_day = datetime.datetime.fromtimestamp(
        now_ms // 1000, tz=datetime.timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(first_day.timestamp()) * 1000


def _period_start_for(period, now_ms):
    # The month/day bucket a hold settles against; a turn-period limit is
    # per-request, so its bucket is this grant itself (stored usage for
    # it is always empty: the check reduces to new <= hard).
    if period == "month":
        return _month_start_ms(now_ms)
    if period == "day":
        return _day_start_ms(now_ms)
    return now_ms


# --- small helpers ---------------------------------------------------------------

def _invalid():
    raise QuotaError(errors.CODE_INVALID_REQUEST,
                     errors.MSG_INVALID_REQUEST)


def _not_found():
    raise QuotaError(errors.CODE_NOT_FOUND, errors.MSG_NOT_FOUND)


def _store_error(exc):
    raise QuotaError(errors.CODE_STORE_ERROR,
                     errors.MSG_STORE_FAILED) from exc


def _validate_grant_request(request):
    if not isinstance(request, dict) or set(request) != _GRANT_KEYS:
        _invalid()
    policy._check_uuid(request["request_id"])
    policy._check_uuid(request["turn_id"])
    policy._check_uuid(request["seat_id"])
    policy._check_uuid(request["model_id"])
    if (not isinstance(request["request_hash"], str)
            or not policy._HASH_RE.fullmatch(request["request_hash"])):
        _invalid()
    policy._check_alias(request["task_class"])
    policy._check_alias(request["effort"])
    for key in ("gateway_id", "parent_lease_id"):
        value = request[key]
        if value is not None:
            policy._check_uuid(value)
    context = request["context"]
    if not isinstance(context, dict) or set(context) != _CONTEXT_KEYS:
        _invalid()
    mode = context["mode"]
    if not isinstance(mode, str):
        _invalid()
    if mode == _MODE_UNKNOWN:
        # Never a mixture: every size null and the source "unknown".
        if (context["source"] != _SOURCE_UNKNOWN
                or any(context[key] is not None for key in (
                    "initial_tokens", "dispatch_tokens",
                    "growth_tokens_per_call"))):
            _invalid()
    else:
        if mode not in _MODES:
            _invalid()
        for key in ("initial_tokens", "dispatch_tokens",
                    "growth_tokens_per_call"):
            policy._check_int(context[key])
        if not isinstance(context["source"], str) \
                or context["source"] not in _SOURCES:
            _invalid()
    policy._check_int(context["observed_at"], 1)
    n = policy._check_int(request["expected_calls"], 1, policy.MAX_CALLS)
    m = policy._check_int(request["max_calls"], 1, policy.MAX_CALLS)
    if n > m:
        _invalid()
    policy._check_int(request["output_cap"])
    policy._check_int(request["per_call_output_cap"])
    # Static: a ttl above the 24h maximum is a permanent hold, not a
    # lease (section 6).
    policy._check_int(request["ttl_ms"], 1, _MAX_TTL_MS)


def _validate_settle_receipt(receipt):
    if not isinstance(receipt, dict) or set(receipt) != _SETTLE_KEYS:
        _invalid()
    policy._check_uuid(receipt["event_id"])
    policy._check_uuid(receipt["lease_id"])
    if receipt["outcome"] not in _OUTCOMES:
        _invalid()
    policy._check_int(receipt["calls"])
    policy._check_int(receipt["input_tokens"])
    policy._check_int(receipt["cached_input_tokens"])
    policy._check_int(receipt["output_tokens"])
    if receipt["actual_micro_eur"] is not None:
        policy._check_int(receipt["actual_micro_eur"])
    if receipt["gateway_id"] is not None:
        policy._check_uuid(receipt["gateway_id"])
    if receipt["seq"] is not None:
        policy._check_int(receipt["seq"])
    if isinstance(receipt["final"], bool) or receipt["final"] not in (0, 1):
        _invalid()
    policy._check_int(receipt["observed_at"], 1)
    policy._check_int(receipt["received_at"], 1)


def _chain_pred(scope):
    # A predicate over the leases table `l` selecting the leases of the
    # named scope; the caller binds the scope id last via _chain_bind
    # (the platform scope spans every lease and binds nothing).
    if scope == "platform":
        return "1=1"
    if scope == "seat":
        return "l.seat_id = ?"
    if scope == "team":
        return "l.seat_id IN (SELECT id FROM seats WHERE team_id = ?)"
    return ("l.seat_id IN (SELECT id FROM seats WHERE team_id IN "
            "(SELECT id FROM teams WHERE project_id = ?))")


def _chain_bind(scope, scope_id):
    # The scope id the matching _chain_pred binds (the platform
    # predicate spans every lease and binds nothing).
    return () if scope == "platform" else (scope_id,)


def _content_hash(request):
    # The idempotency content hash, computed server-side: every request
    # field except the client-supplied request_hash, canonically
    # serialized. A changed field changes the hash; the client cannot
    # choose one.
    canonical = {k: v for k, v in request.items()
                 if k != "request_hash"}
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True,
                   separators=(",", ":")).encode()).hexdigest()


def _lease_row(conn, lease_id):
    row = conn.execute(
        "SELECT " + ",".join(_LEASE_COLS) + " FROM leases WHERE id=?",
        (lease_id,)).fetchone()
    if row is None:
        return None
    return dict(zip(_LEASE_COLS, row))


def _lease_dict(row, replayed=False):
    est = json.loads(row["estimate"])
    return {
        "lease_id": row["id"],
        "state": row["state"],
        "enforcement": row["enforcement"],
        "money_hold": row["money_hold"],
        "caps": {
            "expected_input": est.get("expected_input"),
            "input_cap": row["input_cap"],
            "output_cap": row["output_cap"],
            "max_calls": row["max_calls"],
            "per_call_output_cap": row["per_call_output_cap"],
        },
        "granted_at": row["granted_at"],
        "expires_at": row["expires_at"],
        "replayed": replayed,
    }


def _parked_block_error(conn, account_id, model_id):
    # A parked or probing block on the account (its model, or any model)
    # refuses the grant: 429 parks until retry_at or explicit recovery;
    # a NULL retry_at means no timed retry but the block still stands.
    rows = conn.execute(
        "SELECT retry_at FROM blocks WHERE account_id=? AND "
        "(model_id IS NULL OR model_id=?) AND state IN ('parked','probing')",
        (account_id, model_id)).fetchall()
    if not rows:
        return
    retry_ats = [row[0] for row in rows if row[0] is not None]
    raise QuotaError(errors.CODE_ACCOUNT_PARKED, errors.MSG_ACCOUNT_PARKED,
                     max(retry_ats) if retry_ats else None)


# --- grant ----------------------------------------------------------------------

def _check_http_identity(conn, wire_identity):
    """Fix round 3 finding F1: the HTTP wire-identity check/record must
    run INSIDE the same write transaction as the durable business effect
    it accompanies - two separate transactions (fix round 2's own
    design) left a crash/concurrency window where a competing request
    with different content could commit between them. `wire_identity` is
    `(route, key, wire_hash)` or None (a caller with no HTTP identity to
    guard, e.g. every non-HTTP/CLI caller). Called right after BEGIN
    IMMEDIATE: under the write lock, a genuinely concurrent competitor
    blocks here until the first transaction commits or rolls back, then
    sees its committed identity row (or its absence, if it rolled back)."""
    if wire_identity is None:
        return
    route, key, wire_hash = wire_identity
    row = conn.execute(
        "SELECT wire_hash FROM http_request_identity WHERE route=? "
        "AND idempotency_key=?", (route, key)).fetchone()
    if row is not None and row[0] != wire_hash:
        raise QuotaError(errors.CODE_IDEMPOTENCY_CONFLICT,
                         errors.MSG_IDEMPOTENCY_CONFLICT)


def _record_http_identity(conn, wire_identity, now_ms):
    """The write half of _check_http_identity - called right before
    COMMIT, in the SAME transaction as the mutation, so an injected
    failure here rolls back the whole mutation, not merely this row (a
    monkeypatch point for the interrupted-commit regressions)."""
    if wire_identity is None:
        return
    route, key, wire_hash = wire_identity
    conn.execute(
        "INSERT OR IGNORE INTO http_request_identity(route, "
        "idempotency_key, wire_hash, created_at) VALUES(?,?,?,?)",
        (route, key, wire_hash, now_ms))


def grant(path, request, now_ms, wire_identity=None):
    """Admit and record one lease, or refuse it with a section-7 code.

    One BEGIN IMMEDIATE transaction: read the chain, the account and
    the model; replay/refuse by (machine_id, request_id) - a replay
    returns the stored lease even if the account is parked since;
    refuse on a parked block (ACCOUNT_PARKED); compute the caps through
    the card-3
    arithmetic; re-read usage and holds and enforce every applicable
    limit through policy.check_admission (a denial maps to the 429 code
    of the failing metric); refuse a taken (seat, turn) live slot; then
    insert the lease and its holds. Returns a dict {lease_id, state,
    enforcement, money_hold, caps, granted_at, expires_at, replayed};
    a replay re-returns the stored lease with replayed=True and adds no
    hold.

    `wire_identity` (fix round 3 finding F1) is the HTTP layer's own
    `(route, key, wire_hash)` full-wire-body identity, checked and
    recorded atomically with this same transaction - see
    _check_http_identity/_record_http_identity. None for every
    non-HTTP/CLI caller (no HTTP identity to guard).
    """
    policy._check_int(now_ms, 1)
    _validate_grant_request(request)
    conn = _connect(path, must_exist=True)
    try:
        return _grant(conn, request, now_ms, wire_identity)
    except QuotaError:
        raise
    except sqlite3.Error as exc:
        _store_error(exc)
    finally:
        conn.close()


def _admit_group(conn, key, limit_rows, chain, account_id, budget, metered,
                 input_cap, hold_sized, concurrency_new, now_ms):
    """Enforce one (metric, period[, window_key]) group through the
    card-3 admission rule; a POLICY_DENIED maps to the group's 429 code.
    A non-metered lease admits with zero new hold (advisory: no
    reservation), and a shadow lease consumes no concurrency slot."""
    metric, period, window_key = key
    if metric == "window_bps":
        new = 0  # window usage is observed, not reserved
    elif metric == "concurrency":
        new = concurrency_new  # one live lease; a shadow one consumes none
    elif metered:
        new = input_cap if metric == "input_tokens" else hold_sized
    else:
        new = 0  # advisory: no reservation
    usage = {}
    scope_ids = [(s, chain[s]) for s in ("platform", "project",
                                         "team", "seat")
                 if chain[s] is not None]
    if metric == "window_bps":
        # Window usage is account-level (windows table): one usage for
        # every applicable scope; a missing window is no observation
        # (0 bps), a NULL used_bps is unknown usage and fails closed.
        w = conn.execute(
            "SELECT used_bps FROM windows WHERE account_id=? AND "
            "window_key=?", (account_id, window_key)).fetchone()
        if w is None:
            used = 0
        elif w[0] is None:
            code, msg = _LIMIT_CODES["window_bps"]
            raise QuotaError(code, msg)
        else:
            used = w[0]
        for scope, scope_id in scope_ids:
            usage[(scope, scope_id)] = (used, 0)
    elif metric == "concurrency":
        # Concurrency counts the live and uncertain leases of the
        # scope; a shadow lease consumes no slot.
        for scope, scope_id in scope_ids:
            count = conn.execute(
                "SELECT COUNT(*) FROM leases l WHERE l.state IN "
                "('active','uncertain') AND l.enforcement <> 'shadow' "
                "AND " + _chain_pred(scope),
                _chain_bind(scope, scope_id)).fetchone()[0]
            usage[(scope, scope_id)] = (count, 0)
    else:
        column = ("r.actual_micro_eur" if metric == "money_micro_eur"
                  else "r.input_tokens")
        ps = _period_start_for(period, now_ms)
        # Scope accounting uses the grant-day/month (section 4): a
        # lease's period_start is always the grant-month, so a day
        # window buckets settled spend on the day of granted_at.
        if period == "day":
            settled_bucket = ("((l.granted_at/" + str(_DAY_MS) + ")*"
                               + str(_DAY_MS) + ")")
        else:
            settled_bucket = "l.period_start"
        for scope, scope_id in scope_ids:
            settled = conn.execute(
                "SELECT COALESCE(SUM(" + column + "),0) FROM receipts r "
                "JOIN leases l ON l.id=r.lease_id WHERE r.final=1 AND "
                + settled_bucket + "=? AND " + _chain_pred(scope),
                (ps,) + _chain_bind(scope, scope_id)).fetchone()[0]
            # The platform singleton matches every platform row loaded
            # for this group (the query only selects the singleton).
            limit_ids = [r["id"] for r in limit_rows
                         if r["metric"] == metric and r["period"] == period
                         and (scope == "platform"
                              or (r["scope"] == scope
                                  and r["scope_id"] == scope_id))]
            holds = 0
            if limit_ids:
                placeholders = ",".join("?" * len(limit_ids))
                # Only unresolved exposure: a settled lease's spend is
                # counted through its receipt, its released headroom is
                # gone (the hold no longer reserves anything).
                holds = conn.execute(
                    "SELECT COALESCE(SUM(h.reserved - "
                    "COALESCE(h.settled_actual,0)),0) FROM holds h JOIN "
                    "leases l ON l.id=h.lease_id WHERE h.period_start=? "
                    "AND h.limit_id IN (" + placeholders + ") AND "
                    "l.state IN ('active','uncertain') AND "
                    + _chain_pred(scope),
                    (ps, *limit_ids) + _chain_bind(scope,
                                                   scope_id)).fetchone()[0]
            usage[(scope, scope_id)] = (settled, holds)
    # The account budget: the one mandatory (money, month) cap, enforced
    # against the account's own settled charges and synthetic holds.
    with_account = ((metric, period) == ("money_micro_eur", "month")
                    and budget is not None)
    if with_account:
        month = _month_start_ms(now_ms)
        acct_settled = conn.execute(
            "SELECT COALESCE(SUM(r.actual_micro_eur),0) FROM receipts r "
            "JOIN leases l ON l.id=r.lease_id WHERE r.final=1 AND "
            "l.period_start=? AND l.account_id=?",
            (month, account_id)).fetchone()[0]
        acct_holds = conn.execute(
            "SELECT COALESCE(SUM(h.reserved - COALESCE(h.settled_actual,0)),0) "
            "FROM holds h JOIN leases l ON l.id=h.lease_id WHERE "
            "h.period_start=? AND h.limit_id=? AND l.account_id=? AND "
            "l.state IN ('active','uncertain')",
            (month, _account_limit_id(account_id),
             account_id)).fetchone()[0]
        usage[("account", account_id)] = (acct_settled, acct_holds)
    # check_admission validates closed shapes: its rows carry exactly the
    # limit columns (the store's limit id stays out of the arithmetic).
    admission_rows = [
        {"scope": r["scope"], "scope_id": r["scope_id"], "metric": r["metric"],
         "period": r["period"], "window_key": r["window_key"], "soft": r["soft"],
         "hard": r["hard"]} for r in limit_rows]
    try:
        policy.check_admission(
            rows=admission_rows, metric=metric, period=period, usage=usage,
            new=new, window_key=window_key, chain=chain,
            account_id=account_id if with_account else None,
            account_budget=budget if with_account else None)
    except QuotaError as exc:
        if exc.code != errors.CODE_POLICY_DENIED:
            raise  # INVALID_REQUEST: propagate as-is
        code, msg = _LIMIT_CODES[metric]
        raise QuotaError(code, msg)


def _grant(conn, request, now_ms, wire_identity=None):
    conn.execute("BEGIN IMMEDIATE")
    try:
        _check_http_identity(conn, wire_identity)
        seat = conn.execute(
            "SELECT id, machine_id, team_id, enforcement FROM seats "
            "WHERE id=?", (request["seat_id"],)).fetchone()
        if seat is None:
            _not_found()
        team = conn.execute("SELECT id, project_id FROM teams WHERE id=?",
                            (seat[2],)).fetchone()
        if team is None:
            _not_found()
        project = conn.execute("SELECT id FROM projects WHERE id=?",
                               (team[1],)).fetchone()
        if project is None:
            _not_found()
        model = conn.execute(
            "SELECT id, account_id, alias, context_ceiling, output_ceiling, "
            "tariff_input_rate, tariff_output_rate, reserve_input_rate, "
            "reserve_output_rate, reservation_mode, policy_hash, "
            "effort_aliases FROM models WHERE id=?",
            (request["model_id"],)).fetchone()
        if model is None:
            _not_found()
        # privacy_4: the effort is one of the model's closed aliases -
        # free alias-shaped text (a planted secret, path or ID) is
        # refused, never persisted as a task class.
        if request["effort"] not in model[11].split(","):
            _invalid()
        account = conn.execute(
            "SELECT id, kind, budget_micro_eur FROM accounts WHERE id=?",
            (model[1],)).fetchone()
        if account is None:
            _not_found()
        machine_id = seat[1]
        account_id, account_kind, budget = account
        # Project authorization (section 7: children share project
        # authorization; the server derives membership): the model's
        # account must be one the seat's project has been given.
        if conn.execute(
                "SELECT 1 FROM project_accounts WHERE project_id=? "
                "AND account_id=?", (project[0], account_id)).fetchone() \
                is None:
            raise QuotaError(errors.CODE_POLICY_DENIED,
                             errors.MSG_POLICY_DENIED)
        # The platform singleton: platform caps are the
        # scope='platform' rows carried by the reserved singleton id
        # (the scope spans every seat, so it applies to all of them).
        chain = {"platform": _PLATFORM_ID, "project": project[0],
                 "team": team[0], "seat": seat[0]}

        # Idempotency: same (machine, request_id) + same content replays
        # the stored lease without adding a hold; different content is a
        # conflict. The content hash is computed server-side over the
        # canonical request - a client-supplied hash cannot make a
        # changed replay look identical. The replay comes before the
        # parked check: a lost-response retry of a granted lease
        # replays the stored lease (section 7: replay returns 200)
        # even if the account parked since the grant.
        content_hash = _content_hash(request)
        existing = conn.execute(
            "SELECT id, request_hash FROM leases WHERE machine_id=? AND "
            "request_id=?", (machine_id, request["request_id"])).fetchone()
        if existing is not None:
            if existing[1] != content_hash:
                raise QuotaError(errors.CODE_IDEMPOTENCY_CONFLICT,
                                 errors.MSG_IDEMPOTENCY_CONFLICT)
            row = _lease_row(conn, existing[0])
            _record_http_identity(conn, wire_identity, now_ms)
            conn.execute("COMMIT")
            return _lease_dict(row, replayed=True)

        # An unknown context is a shadow-only request (card L1b): refused
        # for every other derived enforcement, after the replay check and
        # before the profile or any arithmetic. Shadow is never derived
        # from anything but the seat's own operator setting.
        unknown_context = request["context"]["mode"] == _MODE_UNKNOWN
        if unknown_context and seat[3] != "shadow":
            raise QuotaError(errors.CODE_CONTEXT_UNKNOWN,
                             errors.MSG_CONTEXT_UNKNOWN)

        # The profile is the server-side policy for the task class
        # (section 6/7). It is read AFTER the replay check: a granted
        # lease is never changed (or lost) by a later profile edit.
        profile = conn.execute(
            "SELECT candidates, expected_calls, margin_bps, max_calls, "
            "output_cap, per_call_output_cap, ttl_ms, policy_revision "
            "FROM profiles WHERE task_class=?",
            (request["task_class"],)).fetchone()
        if profile is None:
            _not_found()   # an unknown task class, like an unknown model
        try:
            candidates = json.loads(profile[0])
        except ValueError as exc:
            _store_error(exc)
        if not isinstance(candidates, list) or model[2] not in candidates:
            # 'park' is implicit and never stored (STEP-CARD-L1A, turn 1).
            raise QuotaError(errors.CODE_POLICY_DENIED,
                             errors.MSG_POLICY_DENIED)
        # A request may ask for less than the profile, never more.
        for key, ceiling in zip(
                ("expected_calls", "max_calls", "output_cap",
                 "per_call_output_cap", "ttl_ms"), profile[1:2] + profile[3:7]):
            if request[key] > ceiling:
                raise QuotaError(errors.CODE_POLICY_DENIED,
                                 errors.MSG_POLICY_DENIED)
        margin_bps, policy_revision = profile[2], profile[7]

        _parked_block_error(conn, account_id, model[0])
        # Section 7: the admission estimate must be at most 120 seconds
        # old - a stale observed_at is a stale admission basis - and at
        # most the readings' 30 s clock skew in the future: a
        # future-dated estimate is no admission basis at all.
        observed_at = request["context"]["observed_at"]
        if (now_ms - observed_at > _EST_FRESH_MS
                or observed_at > now_ms + reduce.FUTURE_SLOP_MS):
            _invalid()

        # Derive the caps through the card-3 arithmetic: the model row
        # feeds the pure estimate; the request carries the context.
        context = request["context"]
        model_dict = {
            "alias": model[2],
            "context_ceiling": model[3],
            "output_ceiling": model[4],
            "tariff_input_rate": model[5],
            "tariff_output_rate": model[6],
            "reserve_input_rate": model[7],
            "reserve_output_rate": model[8],
            "reservation_mode": model[9],
            "policy_hash": model[10],
            "effort_aliases": model[11],
        }
        if unknown_context:
            # Nothing is invented: the estimate says "unknown", and the
            # stored input cap is the model's own honest upper bound, its
            # context ceiling for every allowed call (the column is NOT
            # NULL). No hold is sized (a shadow lease reserves no money).
            if request["output_cap"] > request["max_calls"] * model[4]:
                _invalid()
            policy.check_per_call_output_cap(
                model_dict, request["per_call_output_cap"])
            est = {"context": "unknown"}
            input_cap = model[3] * request["max_calls"]
            hold_sized = None
        else:
            est = policy.estimate(
                context["initial_tokens"] + context["dispatch_tokens"],
                request["expected_calls"], context["growth_tokens_per_call"],
                request["output_cap"], request["max_calls"], model_dict,
                margin_bps)
            policy.check_per_call_output_cap(
                model_dict, request["per_call_output_cap"])
            input_cap, hold_sized = est["input_cap"], est["hold_sized"]

        # Enforcement is derived, never requested: a metered seat grants
        # a sized hold only through a sized-ready gateway; otherwise the
        # lease is metered_advisory (null money_hold, no holds). A
        # supplied gateway must be this account's on this seat's
        # machine: a foreign one cannot bind (much less upgrade the
        # enforcement of) the lease.
        enforcement = seat[3]
        # CARRY-OVER m4 (STEP-CARD-4, closed in card 5): a seat's stored
        # enforcement is an operator setting, not a request the client can
        # shape - but subscription_admission/subscription_warn are
        # declarations that THIS seat is billed by a subscription, not by
        # this account's money admission at all. On a METERED account
        # (one WITH a real budget) that declaration is simply wrong, and
        # silently honoring it would skip money admission entirely (a
        # seat setting acting as a money bypass). shadow is NOT included:
        # it is a generic observe-only mode with no billing-model claim
        # of its own (section 6: "Old wrappers keep working in
        # observe-only deployments and appear unmanaged/unmetered"), and
        # is exercised deliberately on a metered account by
        # test_card4_turn_c_fix.test_shadow_consumes_no_concurrency_slot.
        # The design gives no explicit ruling for this combination
        # (docs/DESIGN.md only states "only metered_advisory is permitted
        # on a metered account" for the pre-6b sized-enforcement path
        # itself); the fail-closed reading, recorded here and in
        # docs/STEP-CARD-5.md, is to REJECT the grant outright rather
        # than silently reinterpret the seat's setting as metered_advisory.
        if account_kind == "metered" and enforcement in (
                "subscription_admission", "subscription_warn"):
            raise QuotaError(errors.CODE_POLICY_DENIED,
                             errors.MSG_POLICY_DENIED)
        sized_ready = None
        if request["gateway_id"] is not None:
            gw = conn.execute(
                "SELECT account_id, machine_id, sized_ready FROM "
                "gateways WHERE id=?",
                (request["gateway_id"],)).fetchone()
            if gw is None:
                _not_found()
            if gw[0] != account_id or gw[1] != machine_id:
                raise QuotaError(errors.CODE_POLICY_DENIED,
                                 errors.MSG_POLICY_DENIED)
            sized_ready = gw[2]
        if enforcement == "metered":
            if sized_ready is None:
                _invalid()  # gateway is null only for subscriptions
            # R-003 interim: a gateway's own report (sized_ready, kept as
            # telemetry) must never switch a seat to enforcing metered
            # leases. The desktop lead decided metered leases stay
            # advisory until card R1 merges its explicit activation gate,
            # which replaces METERED_ENFORCEMENT_ACTIVATED.
            if not (sized_ready and METERED_ENFORCEMENT_ACTIVATED):
                enforcement = "metered_advisory"
        metered = enforcement == "metered"
        money_hold = hold_sized if metered else None

        # Admission: re-read usage and holds under the write lock and
        # enforce every applicable limit (each cap against its own
        # scope's usage, plus the account budget as the mandatory
        # (money, month) cap).
        limit_rows = [
            {"id": r[0], "scope": r[1], "scope_id": r[2], "metric": r[3],
             "period": r[4], "window_key": r[5], "soft": r[6], "hard": r[7]}
            for r in conn.execute(
                "SELECT id, scope, scope_id, metric, period, window_key, "
                "soft, hard FROM limits WHERE "
                "(scope='platform' AND scope_id=:platform) OR "
                "(scope='project' AND scope_id=:project) OR "
                "(scope='team' AND scope_id=:team) OR "
                "(scope='seat' AND scope_id=:seat)",
                {"platform": chain["platform"],
                 "project": chain["project"], "team": chain["team"],
                 "seat": chain["seat"]})
        ]
        groups = {}
        for row in limit_rows:
            key = (row["metric"], row["period"], row["window_key"])
            groups.setdefault(key, []).append(row)
        if metered and budget is not None:
            groups.setdefault(("money_micro_eur", "month", None), [])
        concurrency_new = 0 if enforcement == "shadow" else 1
        for key in sorted(groups, key=lambda k: (
                _METRIC_RANK[k[0]], _PERIOD_RANK[k[1]], k[2] or "")):
            _admit_group(conn, key, limit_rows, chain, account_id, budget,
                         metered, input_cap, hold_sized, concurrency_new,
                         now_ms)

        # One live slot per (seat, turn): the lease_live partial index.
        # Checked explicitly so the refusal is a typed conflict, not a
        # raw constraint fault.
        live = conn.execute(
            "SELECT id FROM leases WHERE seat_id=? AND turn_id=? AND "
            "state IN ('active','uncertain')",
            (seat[0], request["turn_id"])).fetchone()
        if live is not None:
            raise QuotaError(errors.CODE_IDEMPOTENCY_CONFLICT,
                             errors.MSG_IDEMPOTENCY_CONFLICT)

        lease_id = str(_uuid.uuid4())
        period_start = _month_start_ms(now_ms)
        # The capped ttl cannot overflow, but the sum is still checked
        # against int64: a raw OverflowError is not a typed refusal.
        expires_at = now_ms + request["ttl_ms"]
        if expires_at > policy.INT64_MAX:
            _invalid()
        policy_snapshot = json.dumps({
            "policy_hash": model[10],
            "reservation_mode": model[9],
            "rates": {
                "tariff_input": model[5], "tariff_output": model[6],
                "reserve_input": model[7], "reserve_output": model[8],
            },
            "chain": dict(chain, account=account_id),
            "effort": request["effort"],
            "task_class": request["task_class"],
            "policy_revision": policy_revision,
            "caps": {"input_cap": input_cap, "hold_sized": hold_sized,
                     "output_cap": request["output_cap"],
                     "max_calls": request["max_calls"],
                     "per_call_output_cap": request["per_call_output_cap"]},
            "period_start": period_start,
            **({"input_cap_basis": "model_ceiling"} if unknown_context
               else {}),
        }, sort_keys=True)
        conn.execute(
            "INSERT INTO leases(id, machine_id, seat_id, account_id, "
            "model_id, gateway_id, parent_lease_id, turn_id, request_id, "
            "request_hash, token_hash, task_class, context_mode, "
            "enforcement, estimate, policy_snapshot, input_cap, output_cap, "
            "max_calls, per_call_output_cap, money_hold, granted_at, "
            "expires_at, period_start, state) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lease_id, machine_id, seat[0], account_id, model[0],
             request["gateway_id"], request["parent_lease_id"],
             request["turn_id"], request["request_id"],
             content_hash, None, request["task_class"],
             context["mode"], enforcement, json.dumps(est, sort_keys=True),
             policy_snapshot, input_cap, request["output_cap"],
             request["max_calls"], request["per_call_output_cap"],
             money_hold, now_ms, expires_at, period_start,
             "active"))
        # One holds row per applicable input/money limit (reserved = I or
        # H); only a sized metered lease reserves.
        for row in limit_rows:
            if row["metric"] == "input_tokens":
                reserved, limit_id = input_cap, row["id"]
            elif row["metric"] == "money_micro_eur":
                reserved, limit_id = hold_sized, row["id"]
            else:
                continue  # window_bps / concurrency carry no hold
            if not metered:
                continue
            conn.execute(
                "INSERT INTO holds(lease_id, limit_id, period_start, "
                "reserved) VALUES(?,?,?,?)",
                (lease_id, limit_id,
                 _period_start_for(row["period"], now_ms), reserved))
        if metered and budget is not None:
            conn.execute(
                "INSERT INTO holds(lease_id, limit_id, period_start, "
                "reserved) VALUES(?,?,?,?)",
                (lease_id, _account_limit_id(account_id),
                 _month_start_ms(now_ms), hold_sized))
        conn.execute(
            "INSERT INTO audit(actor_alias, action, object_id, at, "
            "redacted_details) VALUES(?,?,?,?,?)",
            ("coordinator", "lease_granted", lease_id, now_ms,
             json.dumps({"final": 0})))
        _record_http_identity(conn, wire_identity, now_ms)
        conn.execute("COMMIT")
        return _lease_dict(_lease_row(conn, lease_id))
    except BaseException:
        rollback_quietly(conn)
        raise


# --- check ------------------------------------------------------------------------

def check(path, lease_id, now_ms):
    """Re-evaluate one lease (no write, no caching, no TTL extension).

    Returns {state, expires_at, caps, enforcement, money_hold} only for
    an active/uncertain lease that is unexpired and unblocked. Settled
    is LEASE_CLOSED (410), expired is LEASE_EXPIRED (410), blocked is
    ACCOUNT_PARKED (429, with the block's retry_at), missing is
    NOT_FOUND (404).
    """
    policy._check_int(now_ms, 1)
    policy._check_uuid(lease_id)
    conn = _connect(path, must_exist=True)
    try:
        row = _lease_row(conn, lease_id)
        if row is None:
            _not_found()
        if row["state"] == "settled":
            raise QuotaError(errors.CODE_LEASE_CLOSED,
                             errors.MSG_LEASE_CLOSED)
        if now_ms >= row["expires_at"]:
            raise QuotaError(errors.CODE_LEASE_EXPIRED,
                             errors.MSG_LEASE_EXPIRED)
        _parked_block_error(conn, row["account_id"], row["model_id"])
        return _lease_dict(row)
    except QuotaError:
        raise
    except sqlite3.Error as exc:
        _store_error(exc)
    finally:
        conn.close()


# --- expire ---------------------------------------------------------------------

def expire(path, lease_id, now_ms):
    """The explicit expiry path (section 4: "Expiry -> uncertain,
    never a refund").

    One BEGIN IMMEDIATE transaction: an active lease whose TTL has
    elapsed (now_ms >= expires_at) is moved to uncertain. The outcome
    cannot be proved, so the holds stay reserved - the liability is
    retained and nothing is released; the lease keeps its live slot
    (the lease_live index counts uncertain) and a late final receipt
    can still settle it against the original grant period. Idempotent
    on an already-uncertain lease (replayed=True); a not-yet-expired
    lease is INVALID_REQUEST; a settled lease is terminal
    (RECEIPT_CONFLICT); a missing lease is NOT_FOUND (404).
    """
    policy._check_int(now_ms, 1)
    policy._check_uuid(lease_id)
    conn = _connect(path, must_exist=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = _lease_row(conn, lease_id)
            if row is None:
                _not_found()
            if row["state"] == "settled":
                raise QuotaError(errors.CODE_RECEIPT_CONFLICT,
                                 errors.MSG_RECEIPT_CONFLICT)
            if row["state"] == "uncertain":
                conn.execute("COMMIT")
                return _lease_dict(row, replayed=True)
            if now_ms < row["expires_at"]:
                _invalid()  # not expired: expiry is an effect of the TTL
            _mark_uncertain(conn, lease_id, now_ms)
            conn.execute("COMMIT")
            return _lease_dict(_lease_row(conn, lease_id))
        except BaseException:
            rollback_quietly(conn)
            raise
    finally:
        conn.close()


def _mark_uncertain(conn, lease_id, now_ms):
    # Expiry is never a refund: the state changes, the holds and the live
    # slot stay (section 4); one audit row records it.
    conn.execute("UPDATE leases SET state='uncertain' WHERE id=?",
                 (lease_id,))
    conn.execute(
        "INSERT INTO audit(actor_alias, action, object_id, at, "
        "redacted_details) VALUES(?,?,?,?,?)",
        ("coordinator", "lease_expired", lease_id, now_ms,
         json.dumps({"refunded": 0})))


def expire_sweep(path, now_ms):
    """Apply the expiry rule to every active lease whose TTL has elapsed
    (now_ms >= expires_at), in one BEGIN IMMEDIATE transaction. Returns the
    closed summary {"expired": count}. Idempotent (a second sweep finds
    nothing active and expired); never settles, refunds or deletes."""
    policy._check_int(now_ms, 1)
    conn = _connect(path, must_exist=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            due = conn.execute(
                "SELECT id FROM leases WHERE state='active' AND "
                "expires_at <= ? ORDER BY expires_at, id",
                (now_ms,)).fetchall()
            for (lease_id,) in due:
                _mark_uncertain(conn, lease_id, now_ms)
            conn.execute("COMMIT")
            return {"expired": len(due)}
        except BaseException:
            rollback_quietly(conn)
            raise
    except sqlite3.Error as exc:
        _store_error(exc)
    finally:
        conn.close()


# --- block-clear (operator-only; card L1a turn 3) ---------------------------------

CLEAR_REASONS = ("reset_observed", "false_positive", "operator_override")


def block_clear(path, account_alias, model_alias, reason, now_ms):
    """Clear the live (parked or probing) block of an account (or of one of
    its models), audited, with the N3 safety rule: in the same transaction
    EVERY unresolved adjustment of the account (not only the block's source
    event) refuses the clear with ACCOUNT_PARKED - the account stays parked
    until the reconciliation card can resolve the overrun. No live block:
    NOT_FOUND when none ever existed for the scope; a scope whose block is
    already cleared is a no-op that reports the cleared state (no second
    audit row). Returns {"state": "cleared", "replayed": bool}.
    """
    policy._check_alias(account_alias)
    if model_alias is not None:
        policy._check_alias(model_alias)
    if not isinstance(reason, str) or reason not in CLEAR_REASONS:
        _invalid()
    policy._check_int(now_ms, 1)
    conn = _connect(path, must_exist=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            account = conn.execute("SELECT id FROM accounts WHERE alias=?",
                                   (account_alias,)).fetchone()
            if account is None:
                _not_found()
            model_id = None
            if model_alias is not None:
                model = conn.execute(
                    "SELECT id FROM models WHERE account_id=? AND alias=?",
                    (account[0], model_alias)).fetchone()
                if model is None:
                    _not_found()
                model_id = model[0]
            live = conn.execute(
                "SELECT id, state FROM blocks WHERE account_id=? AND "
                "model_id IS ? AND state IN ('parked','probing')",
                (account[0], model_id)).fetchone()
            if live is None:
                cleared = conn.execute(
                    "SELECT 1 FROM blocks WHERE account_id=? AND "
                    "model_id IS ? AND state='cleared' LIMIT 1",
                    (account[0], model_id)).fetchone()
                if cleared is None:
                    _not_found()
                conn.execute("COMMIT")
                return {"state": "cleared", "replayed": True}
            # N3: scan EVERY unresolved adjustment of the account.
            if conn.execute(
                    "SELECT 1 FROM adjustments WHERE account_id=? AND "
                    "resolved_at IS NULL LIMIT 1",
                    (account[0],)).fetchone() is not None:
                raise QuotaError(errors.CODE_ACCOUNT_PARKED,
                                 errors.MSG_ACCOUNT_PARKED)
            block_id, previous = live
            conn.execute(
                "UPDATE blocks SET state='cleared', updated_at=? WHERE id=?",
                (now_ms, block_id))
            conn.execute(
                "INSERT INTO audit(actor_alias, action, object_id, at, "
                "redacted_details) VALUES(?,?,?,?,?)",
                ("operator", "block_cleared", block_id, now_ms,
                 json.dumps({"reason": reason, "previous_state": previous})))
            conn.execute("COMMIT")
            return {"state": "cleared", "replayed": False}
        except BaseException:
            rollback_quietly(conn)
            raise
    except sqlite3.Error as exc:
        _store_error(exc)
    finally:
        conn.close()


# --- settle ------------------------------------------------------------------------

_RECEIPT_FIELDS = ("lease_id", "gateway_id", "seq", "calls", "input_tokens",
                   "cached_input_tokens", "output_tokens", "actual_micro_eur",
                   "outcome", "final", "observed_at", "received_at")
# The replay-identity subset (fix round 2 finding N1): every field EXCEPT
# the two server-derived receipt timestamps, which an HTTP retry
# regenerates on every call and so can never agree with a stored row.
_RECEIPT_IDENTITY_FIELDS = tuple(
    f for f in _RECEIPT_FIELDS if f not in ("observed_at", "received_at"))


def _hold_metric(conn, limit_id):
    if limit_id.startswith("account:"):
        return "money_micro_eur"
    row = conn.execute("SELECT metric FROM limits WHERE id=?",
                       (limit_id,)).fetchone()
    if row is None:
        _invalid()  # a hold pointing at no limit: corrupt, fail closed
    return row[0]


def settle(path, receipt, now_ms):
    """Record a receipt for a lease; a final receipt settles it.

    One BEGIN IMMEDIATE transaction. A duplicate event_id with identical
    content replays as a no-op; different content is
    IDEMPOTENCY_CONFLICT. A final receipt (exactly one per lease - the
    receipt_final index) moves the lease to settled and releases every
    hold into settled_actual = min(actual, reserved) - never below zero,
    never above the hold; an observed overrun is recorded as liability
    (money: an adjustments row with resolved_at NULL plus an audit row;
    input: an audit row). Returns {state, released_micro_eur,
    retained_micro_eur, replayed}: released is the hold headroom freed
    by settlement, retained the money that stays as settled liability.
    """
    policy._check_int(now_ms, 1)
    _validate_settle_receipt(receipt)
    conn = _connect(path, must_exist=True)
    try:
        return _settle(conn, receipt, now_ms)
    except QuotaError:
        raise
    except sqlite3.Error as exc:
        _store_error(exc)
    finally:
        conn.close()


def _receipt_conflict_park(conn, lease, event_id, now_ms):
    # A conflicting FINAL receipt blocks further work on the account
    # (section 4, card row "receipt conflict blocks"): recorded like
    # the overrun park - one live NULL-model parked block keyed to
    # the conflicting event. OR IGNORE: the account may already be
    # parked (blocks_live allows one live block per (account,
    # model-or-null)). The park commits before the conflict is
    # raised: the raise rolls the settle transaction back, but the
    # block must survive it.
    conn.execute(
        "INSERT OR IGNORE INTO blocks(id, account_id, model_id, reason, "
        "retry_at, state, source_event_id, updated_at) "
        "VALUES(?,?,?,?,NULL,'parked',?,?)",
        (str(_uuid.uuid4()), lease["account_id"], None,
         "receipt_conflict", event_id, now_ms))
    conn.execute(
        "INSERT INTO audit(actor_alias, action, object_id, at, "
        "redacted_details) VALUES(?,?,?,?,?)",
        ("coordinator", "receipt_conflict_block", event_id, now_ms,
         json.dumps({"lease_id": lease["id"]})))
    conn.execute("COMMIT")


def _settle(conn, receipt, now_ms):
    conn.execute("BEGIN IMMEDIATE")
    try:
        lease = _lease_row(conn, receipt["lease_id"])
        if lease is None:
            _not_found()
        # The receipt must be attributed to the lease's own gateway
        # (a NULL names no other): a report naming a foreign gateway
        # is a conflicting account of this lease (static
        # RECEIPT_CONFLICT), never a settle - the overrun liability
        # would otherwise take a foreign home.
        if receipt["gateway_id"] is not None and \
                receipt["gateway_id"] != lease["gateway_id"]:
            # A conflicting FINAL receipt blocks further work on the
            # account (section 4, card row "receipt conflict blocks").
            if receipt["final"]:
                _receipt_conflict_park(conn, lease, receipt["event_id"],
                                       now_ms)
            raise QuotaError(errors.CODE_RECEIPT_CONFLICT,
                             errors.MSG_RECEIPT_CONFLICT)
        stored = conn.execute(
            "SELECT event_id," + ",".join(_RECEIPT_FIELDS) +
            " FROM receipts WHERE event_id=?",
            (receipt["event_id"],)).fetchone()
        if stored is not None:
            # A duplicate event_id: identical content is a no-op replay,
            # different content is an idempotency conflict. Identity
            # excludes observed_at/received_at (fix round 2,
            # tk-dae9aab24ef8 finding N1): the HTTP adapter derives both
            # from the receipt instant on EVERY call, so an identical
            # retry a moment later would otherwise always look like a
            # conflicting report and could never be recovered.
            stored_map = dict(zip(_RECEIPT_FIELDS, stored[1:]))
            if any(receipt[f] != stored_map[f]
                   for f in _RECEIPT_IDENTITY_FIELDS):
                raise QuotaError(errors.CODE_IDEMPOTENCY_CONFLICT,
                                 errors.MSG_IDEMPOTENCY_CONFLICT)
            # Replay the first receipt's own response unchanged (finding
            # N1): recomputed from the lease's fixed money_hold and the
            # stored (identity-checked) actual/final, the same formula
            # the original settle below uses - never the first call's
            # transient state, never a flat zero.
            released = retained = 0
            if stored_map["final"] and lease["money_hold"] is not None:
                retained = min(stored_map["actual_micro_eur"],
                               lease["money_hold"])
                released = lease["money_hold"] - retained
            if lease["money_hold"] is None:
                # No money was reserved: the money fields are null, not 0
                # (section 7: "money fields null for subscription").
                released = retained = None
            conn.execute("COMMIT")
            return {"state": lease["state"], "replayed": True,
                    "released_micro_eur": released,
                    "retained_micro_eur": retained}
        if lease["state"] == "settled":
            # Terminal rows cannot be reopened: a conflicting FINAL
            # receipt blocks further work on the account (section 4,
            # card row "receipt conflict blocks") and preserves the
            # conservative liability already recorded (reconciliation
            # is the reconciliation card).
            if receipt["final"]:
                _receipt_conflict_park(conn, lease, receipt["event_id"],
                                       now_ms)
            raise QuotaError(errors.CODE_RECEIPT_CONFLICT,
                             errors.MSG_RECEIPT_CONFLICT)
        if not receipt["final"] and (lease["state"] == "uncertain"
                                     or now_ms >= lease["expires_at"]):
            # Only the gateway's closed-child final receipt resolves an
            # unprovable outcome; a non-final report on a lease whose
            # TTL has elapsed (explicitly, or by the clock) is
            # refused. (A late FINAL receipt is accepted below and
            # settles the holds at their original, grant-time
            # period_start.)
            raise QuotaError(errors.CODE_LEASE_EXPIRED,
                             errors.MSG_LEASE_EXPIRED)
        # A final receipt on a METERED lease must carry the money:
        # unknown spend is not a zero final receipt. Leases that
        # reserved no money (advisory/subscription/shadow) settle with
        # money=None - their termination confirmation carries no figure.
        if (receipt["final"] and receipt["actual_micro_eur"] is None
                and lease["enforcement"] == "metered"):
            _invalid()
        conn.execute(
            "INSERT INTO receipts(event_id, lease_id, gateway_id, seq, "
            "calls, input_tokens, cached_input_tokens, output_tokens, "
            "actual_micro_eur, outcome, final, observed_at, received_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (receipt["event_id"], receipt["lease_id"],
             receipt["gateway_id"], receipt["seq"], receipt["calls"],
             receipt["input_tokens"], receipt["cached_input_tokens"],
             receipt["output_tokens"], receipt["actual_micro_eur"],
             receipt["outcome"], receipt["final"], receipt["observed_at"],
             receipt["received_at"]))
        released = retained = 0
        # Max money-hold overrun of this receipt: (period_start,
        # amount) - stamped with the lease's own grant-time period
        # (the original period), not a hold's bucket. One adjustments
        # row per receipt: source_event_id is UNIQUE.
        overrun = None
        if receipt["final"]:
            conn.execute(
                "UPDATE leases SET state='settled', outcome=? WHERE id=?",
                (receipt["outcome"], receipt["lease_id"]))
            hold_rows = conn.execute(
                "SELECT lease_id, limit_id, period_start, reserved, "
                "settled_actual FROM holds WHERE lease_id=?",
                (receipt["lease_id"],)).fetchall()
            for _lease_id, limit_id, period_start, reserved, _actual in \
                    hold_rows:
                metric = _hold_metric(conn, limit_id)
                actual = (receipt["actual_micro_eur"]
                          if metric == "money_micro_eur"
                          else receipt["input_tokens"])
                # Never below zero, never above the hold without
                # recording the overrun as liability.
                settled_actual = min(actual, reserved)
                conn.execute(
                    "UPDATE holds SET settled_actual=? WHERE lease_id=? "
                    "AND limit_id=?",
                    (settled_actual, receipt["lease_id"], limit_id))
                if actual > reserved:
                    conn.execute(
                        "INSERT INTO audit(actor_alias, action, object_id, "
                        "at, redacted_details) VALUES(?,?,?,?,?)",
                        ("coordinator", "overrun_recorded",
                         receipt["lease_id"], now_ms,
                         json.dumps({"metric": metric})))
                    if metric == "money_micro_eur":
                        amount = actual - reserved
                        if overrun is None or amount > overrun[1]:
                            overrun = (lease["period_start"], amount)
            # The response contract is per lease (section 7): the
            # lease's own reservation, not the sum over its holds rows
            # (every money cap reserves the same hold size; the
            # overrun above it is the adjustments row, not retained).
            money_hold = lease["money_hold"]
            if money_hold is not None:
                retained = min(receipt["actual_micro_eur"], money_hold)
                released = money_hold - retained
            if overrun is not None:
                gateway_id = (receipt["gateway_id"]
                              if receipt["gateway_id"] is not None
                              else lease["gateway_id"])
                if gateway_id is None:
                    _invalid()  # the liability needs a home gateway
                conn.execute(
                    "INSERT INTO adjustments(id, account_id, "
                    "gateway_id, source_event_id, period_start, "
                    "unattributed_micro_eur, reason, resolved_at) "
                    "VALUES(?,?,?,?,?,?,?,NULL)",
                    (str(_uuid.uuid4()), lease["account_id"],
                     gateway_id, receipt["event_id"], overrun[0],
                     overrun[1], "overrun"))
                # An observed overrun blocks further work on the account
                # until reconciled (section 4): parked, no timed retry.
                # The blocks_live index allows one live block per
                # (account, model-or-null): an existing parked block stays
                # parked, a probing one is parked again (a probe cannot
                # clear an unreconciled overrun), and every overrun's event
                # id is audited so concurrent overruns sharing the one
                # block stay traceable (card L1a turn 3, N3).
                live = conn.execute(
                    "SELECT id, state FROM blocks WHERE account_id=? AND "
                    "model_id IS NULL AND state IN ('parked','probing')",
                    (lease["account_id"],)).fetchone()
                if live is None:
                    block_id, previous = str(_uuid.uuid4()), None
                    conn.execute(
                        "INSERT INTO blocks(id, account_id, model_id, "
                        "reason, retry_at, state, source_event_id, "
                        "updated_at) VALUES(?,?,?,?,NULL,'parked',?,?)",
                        (block_id, lease["account_id"], None, "overrun",
                         receipt["event_id"], now_ms))
                else:
                    block_id, previous = live
                    if previous == "probing":
                        conn.execute(
                            "UPDATE blocks SET state='parked', "
                            "retry_at=NULL, updated_at=? WHERE id=?",
                            (now_ms, block_id))
                conn.execute(
                    "INSERT INTO audit(actor_alias, action, object_id, "
                    "at, redacted_details) VALUES(?,?,?,?,?)",
                    ("coordinator", "overrun_block", block_id, now_ms,
                     json.dumps({"event_id": receipt["event_id"],
                                 "previous_state": previous})))
            conn.execute(
                "INSERT INTO audit(actor_alias, action, object_id, at, "
                "redacted_details) VALUES(?,?,?,?,?)",
                ("coordinator", "lease_settled", receipt["lease_id"],
                 now_ms, json.dumps({"final": 1})))
        if lease["money_hold"] is None:
            released = retained = None   # nothing reserved (see the replay)
        conn.execute("COMMIT")
        return {"state": "settled" if receipt["final"] else lease["state"],
                "released_micro_eur": released,
                "retained_micro_eur": retained, "replayed": False}
    except BaseException:
        rollback_quietly(conn)
        raise


# --- limit-events (card 5 fix round: POST /v1/limit-events, section 7) -----------

_LIMIT_EVENT_KEYS = frozenset((
    "event_id", "account_id", "model_id", "turn_id", "observed_at",
    "evidence_kind", "resets_at", "exhausted_windows"))
_EVIDENCE_KINDS = frozenset((
    "codex_exhausted", "claude_exhausted", "provider_usage_429"))


def _validate_limit_event(event):
    if not isinstance(event, dict) or frozenset(event) != _LIMIT_EVENT_KEYS:
        _invalid()
    policy._check_uuid(event["event_id"])
    policy._check_uuid(event["account_id"])
    if event["model_id"] is not None:
        policy._check_uuid(event["model_id"])
    policy._check_uuid(event["turn_id"])
    policy._check_int(event["observed_at"], 1)
    # Type-checked BEFORE membership (fix round 2, tk-dae9aab24ef8
    # finding N6): `x in a_frozenset` hashes x first, so an unhashable
    # client value (a list, a dict) raised a raw TypeError - surfacing as
    # an internal fault (503) instead of the client's actual mistake (400).
    if not isinstance(event["evidence_kind"], str) \
            or event["evidence_kind"] not in _EVIDENCE_KINDS:
        _invalid()
    if event["resets_at"] is not None:
        policy._check_int(event["resets_at"], 1)
    windows = event["exhausted_windows"]
    if not isinstance(windows, list) or not all(isinstance(w, str) for w in windows):
        _invalid()


_LIMIT_EVENT_IDENTITY_FIELDS = (
    "account_id", "model_id", "turn_id", "observed_at", "evidence_kind",
    "resets_at", "exhausted_windows")


def _merge_retry_at(old, new):
    """Conservative evidence merge (fix round 2 finding N4): the latest
    KNOWN reset wins (never schedule an earlier retry than any report
    says), but an unknown (null) reset from EITHER side makes the merged
    reset unknown too - recovery then waits for positive evidence from
    every report, not just the first one."""
    if old is None or new is None:
        return None
    return max(old, new)


def record_limit_event(path, event, now_ms, authorized_for_account=None):
    """Publish a reporter-observed limit exhaustion as a parked block on
    the account (section 7: "POST /limit-events... 202
    {block_id,retry_at,state}").

    A block is account-wide (model_id NULL) or model-scoped, matching the
    blocks_live partial index (one live block per (account,
    model-or-null)). Idempotent by event_id - EVERY accepted event is
    retained in limit_events (fix round 2, tk-dae9aab24ef8 findings
    N3/N4; migration version 7), not only the one that first created its
    block: a replay compares the FULL content (not just account/model)
    and, if unchanged, returns the block's CURRENT state as a pure read -
    no write, no audit row, so a delayed replay can never re-park an
    account an operator has since cleared, nor drift from what the block
    actually now says. A DIFFERENT event_id for an account/model that
    already has a live block JOINS it (the card's own demo: "two machines
    share one block") rather than raising a uniqueness conflict, and
    MERGES its exhaustion evidence into the block's retry_at
    conservatively (_merge_retry_at) rather than discarding it.

    A LEGACY row (fix round 3, tk-f9018a7e573e finding F2; migration
    version 8) is one migration v8 backfilled from a pre-v7 store's own
    blocks.source_event_id - a creator identity from before limit_events
    existed at all, with no real turn_id/observed_at/exhausted_windows to
    compare (synthesized placeholders, not evidence) and no record of any
    OTHER event that may have joined its block. Its CONTENT is never
    compared and never merged: a replay of a legacy event_id always
    returns the block's CURRENT state unchanged, exactly like a
    content-matching replay, so a delayed pre-upgrade retry can neither
    conflict nor re-park (nor otherwise disturb) an account the operator
    has since recovered. Its account_id is NOT one of the unknown fields
    though (fix round 4, tk-1b3649a06f0e finding R2) - copied faithfully
    from the pre-v7 block, it is real and known, and ignoring content
    must never also disable authorization on it: `authorized_for_account`
    (an optional `(conn, account_id) -> bool` callback; None for every
    non-HTTP/CLI caller) is re-checked against the block's STORED account
    for a legacy row specifically, before any of its state is returned -
    not merely the submitted account_id the caller's own request named,
    which the original bug trusted blindly. A submitted account_id that
    disagrees with the stored one, from a caller who IS authorized for
    the real account, is still a genuine content mismatch
    (IDEMPOTENCY_CONFLICT) - the two checks are independent, not
    alternatives.
    """
    policy._check_int(now_ms, 1)
    _validate_limit_event(event)
    conn = _connect(path, must_exist=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT account_id, model_id, turn_id, observed_at, "
                "evidence_kind, resets_at, exhausted_windows, block_id, "
                "legacy FROM limit_events WHERE event_id=?",
                (event["event_id"],)).fetchone()
            if existing is not None:
                stored_account_id = existing[0]
                if existing[8]:
                    if authorized_for_account is not None \
                            and not authorized_for_account(
                                conn, stored_account_id):
                        raise QuotaError(errors.CODE_FORBIDDEN,
                                         errors.MSG_FORBIDDEN)
                    if event["account_id"] != stored_account_id:
                        raise QuotaError(errors.CODE_IDEMPOTENCY_CONFLICT,
                                         errors.MSG_IDEMPOTENCY_CONFLICT)
                else:
                    stored = dict(zip(
                        ("account_id", "model_id", "turn_id", "observed_at",
                         "evidence_kind", "resets_at", "exhausted_windows"),
                        existing[:7]))
                    stored["exhausted_windows"] = json.loads(stored["exhausted_windows"])
                    if any(stored[f] != event[f]
                           for f in _LIMIT_EVENT_IDENTITY_FIELDS):
                        raise QuotaError(errors.CODE_IDEMPOTENCY_CONFLICT,
                                         errors.MSG_IDEMPOTENCY_CONFLICT)
                block_id = existing[7]
                block = conn.execute(
                    "SELECT retry_at, state FROM blocks WHERE id=?",
                    (block_id,)).fetchone()
                conn.execute("COMMIT")
                return {"block_id": block_id, "retry_at": block[0],
                        "state": block[1]}
            account = conn.execute("SELECT id FROM accounts WHERE id=?",
                                   (event["account_id"],)).fetchone()
            if account is None:
                _not_found()
            if event["model_id"] is not None:
                model = conn.execute(
                    "SELECT id FROM models WHERE id=? AND account_id=?",
                    (event["model_id"], event["account_id"])).fetchone()
                if model is None:
                    _not_found()
            live = conn.execute(
                "SELECT id, retry_at, state FROM blocks WHERE account_id=? "
                "AND model_id IS ? AND state IN ('parked','probing')",
                (event["account_id"], event["model_id"])).fetchone()
            if live is None:
                block_id = str(_uuid.uuid4())
                retry_at, state = event["resets_at"], "parked"
                conn.execute(
                    "INSERT INTO blocks(id, account_id, model_id, "
                    "reason, retry_at, state, source_event_id, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (block_id, event["account_id"], event["model_id"],
                     event["evidence_kind"], retry_at, state,
                     event["event_id"], now_ms))
            else:
                block_id, old_retry_at, state = live
                retry_at = _merge_retry_at(old_retry_at, event["resets_at"])
                conn.execute(
                    "UPDATE blocks SET retry_at=?, updated_at=? WHERE id=?",
                    (retry_at, now_ms, block_id))
            conn.execute(
                "INSERT INTO limit_events(event_id, account_id, model_id, "
                "turn_id, observed_at, evidence_kind, resets_at, "
                "exhausted_windows, block_id, received_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (event["event_id"], event["account_id"], event["model_id"],
                 event["turn_id"], event["observed_at"],
                 event["evidence_kind"], event["resets_at"],
                 json.dumps(event["exhausted_windows"]), block_id, now_ms))
            conn.execute(
                "INSERT INTO audit(actor_alias, action, object_id, at, "
                "redacted_details) VALUES(?,?,?,?,?)",
                ("coordinator", "limit_event_recorded", block_id, now_ms,
                 json.dumps({"evidence_kind": event["evidence_kind"]})))
            conn.execute("COMMIT")
            return {"block_id": block_id, "retry_at": retry_at, "state": state}
        except BaseException:
            rollback_quietly(conn)
            raise
    except sqlite3.Error as exc:
        _store_error(exc)
    finally:
        conn.close()
