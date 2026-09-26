"""Pure billing evaluation (card 2b-2).

evaluate_billing(context, request) -> Evaluation is a PURE function:
no I/O, no store writes, no clock reads (now_ms is injected on the
request), and it never mutates its inputs. The evaluator never mutates
reconcile state; it reads the explicit, already-validated context.

Turn 2 (this module) pins the closed input surface, the complete output
shape, the ordered decision-table precedence and the non-AI overlay.
The reconciliation rows (gross excess, conflict, stale, matched, lag,
midnight, historical excess) and the advisory headroom math land in
turns 3/4. Every part that is not yet implemented is marked and FAILS
CLOSED: admission_basis=unavailable, headroom_micro_eur=null and
request_fits=null, so an un-evaluated estimate never admits. Nothing
here ever fakes a "pass" or a "matched" result.
"""

from .errors import (
    CODE_INVALID_CONFIG,
    MSG_INVALID_CONFIG,
    QuotaError,
)
from .validate_policies import validate_billing_policies
from .validate_readings import (
    KIND_CLI_WINDOW,
    KIND_GATEWAY_SNAPSHOT,
    KIND_PROVIDER_BILLING,
    _is_alias,
    _is_bool,
    _is_nonneg_int,
    _is_one_utc_month_period,
    _is_uuid,
    _validate_cli_window,
    _validate_gateway_snapshot,
    _validate_provider_billing,
)

INT64_MAX = 2 ** 63 - 1

# The ledger snapshot must be within 120 s of the certified time to count
# as the boundary exposure (section 11: "each within 120s").
_BOUNDARY_WINDOW_MS = 120_000

# Closed context / request / evidence shapes (docs/DESIGN.md, section 11,
# "Pure input surfaces"). All fields required, no extra keys.
_CONTEXT_KEYS = frozenset({
    "account", "period_start", "period_end", "cap_micro_eur", "gateways",
    "policy", "readings", "unresolved_event_ids", "boundary_exposure_micro_eur",
    "boundary_event_ids",
})
_GATEWAY_KEYS = frozenset({"gateway", "share_micro_eur",
                           "current_chain_event_ids"})
_READING_KEYS = frozenset({
    "event_id", "machine", "account", "kind", "observed_at", "payload",
    "received_at",
})
_REQUEST_KEYS = frozenset({"version", "account", "now_ms",
                           "requested_micro_eur", "coverage"})
_COVERAGE_KEYS = frozenset({
    "event_id", "upper_at", "complete_at", "overlap_floor_micro_eur",
    "matched_provenance",
})

# The ordered decision table (docs/DESIGN.md, section 11, the evaluation
# decision table). First matching base row wins. Only the rows this turn
# can decide honestly are implemented; the rest are turn-3/4 stubs that
# never match (so the evaluation falls through to the catch-all and
# headroom stays unavailable - fail closed, never a faked pass).
_BASE_ROW_ORDER = (
    "basis_scope",                 # 1: unknown / basis_unknown,scope_mismatch,gateway_unusable / alert / unknown
    "no_sample",                   # 2: unknown / billing_unknown (no provider sample) / alert / provisional
    "gross_excess",                # 3: unexplained_excess / provider_excess / hold / comparable
    "latched_conflict",            # 4: conflict / source_conflict / alert / provisional
    "midnight",                    # 5: temporally_unresolved / snapshot_skew / none / provisional
    "historical_excess",           # 6: unexplained_excess / provider_excess / hold / comparable
    "missing_old_spend",           # 7: provider_behind / missing_old_spend / alert / Q
    "idle_lag",                    # 8: provider_behind / lag_investigate / alert / provisional
    "no_certificate_time_guards",  # 9: temporally_unresolved / snapshot_skew / none / provisional
    "unresolved_bounds",           # 10: temporally_unresolved / coverage_unknown / none / provisional
    "reserve_covered",             # 11: reserve_or_scope_covered / reserve_or_scope_covered / none / Q
    "matched",                     # 12: matched / matched / none / Q
)


def _invalid():
    raise QuotaError(CODE_INVALID_CONFIG, MSG_INVALID_CONFIG)


def base_row_order():
    """The ordered decision-table rows; first match wins. Exposed for the
    precedence acceptance rows (billing_precedence)."""
    return list(_BASE_ROW_ORDER)


def apply_non_ai_overlay(action, reason, non_ai_present):
    """The non-AI overlay (section 11): any retained non_ai_spend_present in
    the current period raises none to alert with reason=non_ai_present.
    It never downgrades a hold and never replaces an earlier alert reason.
    Returns the (action, reason) pair."""
    if not non_ai_present:
        return action, reason
    if action == "hold":
        # Never downgrade a hold recommendation.
        return action, reason
    if action == "alert":
        # Never replace an earlier alert reason.
        return action, reason
    # action == "none": raise to alert.
    return "alert", "non_ai_present"


# ---------------------------------------------------------------------------
# Context validation
# ---------------------------------------------------------------------------

def _validate_context(context):
    """Validate the closed evaluation context; return a normalized,
    detached copy (the caller's dict is never mutated)."""
    if not isinstance(context, dict) or frozenset(context) != _CONTEXT_KEYS:
        _invalid()

    account = context["account"]
    if not _is_alias(account):
        _invalid()
    period_start = context["period_start"]
    period_end = context["period_end"]
    if not _is_nonneg_int(period_start) or not _is_nonneg_int(period_end):
        _invalid()
    # The provider period is exactly one UTC month (the provider rule).
    if not _is_one_utc_month_period(period_start, period_end):
        _invalid()
    cap = context["cap_micro_eur"]
    if not _is_nonneg_int(cap):
        _invalid()

    # gateways: closed per-gateway records.
    if not isinstance(context["gateways"], list):
        _invalid()
    gateways = []
    for gw in context["gateways"]:
        if not isinstance(gw, dict) or frozenset(gw) != _GATEWAY_KEYS:
            _invalid()
        if not _is_alias(gw["gateway"]):
            _invalid()
        if not _is_nonneg_int(gw["share_micro_eur"]):
            _invalid()
        chain = gw["current_chain_event_ids"]
        if not isinstance(chain, list) or not all(_is_uuid(e) for e in chain):
            _invalid()
        gateways.append({
            "gateway": gw["gateway"],
            "share_micro_eur": gw["share_micro_eur"],
            "current_chain_event_ids": list(chain),
        })

    # policy: the full billing-policy-import record (closed shape), and it
    # must be the policy for THIS account.
    policy = context["policy"]
    if not isinstance(policy, dict):
        _invalid()
    normalized_policy = validate_billing_policies(
        {"version": 1, "policies": [policy]})[0]
    if policy.get("account") != account:
        _invalid()

    # readings: validated reading envelopes + received_at, all for this
    # account.
    if not isinstance(context["readings"], list):
        _invalid()
    readings = [_validate_reading(r, account) for r in context["readings"]]

    # unresolved_event_ids: local UUID references (closed type).
    unresolved = context["unresolved_event_ids"]
    if not isinstance(unresolved, list) or not all(_is_uuid(e) for e in unresolved):
        _invalid()

    boundary_exposure = context["boundary_exposure_micro_eur"]
    if boundary_exposure is not None and not _is_nonneg_int(boundary_exposure):
        _invalid()

    # boundary_event_ids: references to store boundary evidence (known
    # reading event ids).
    boundary_ids = context["boundary_event_ids"]
    if not isinstance(boundary_ids, list) or not all(_is_uuid(e) for e in boundary_ids):
        _invalid()
    reading_ids = {r["event_id"] for r in readings}
    for ref in boundary_ids:
        if ref not in reading_ids:
            _invalid()

    # current-chain membership: every chain id references a reading.
    for gw in gateways:
        for ref in gw["current_chain_event_ids"]:
            if ref not in reading_ids:
                _invalid()

    # current-period provider events and per-gateway ledger snapshots.
    provider_events = [r for r in readings if r["kind"] == KIND_PROVIDER_BILLING
                       and r["payload"]["period_start"] == period_start]
    snapshots_by_gateway = {}
    for gw in gateways:
        snaps = [
            (r["observed_at"], r["payload"]["settled_micro_eur"])
            for r in readings
            if r["kind"] == KIND_GATEWAY_SNAPSHOT
            and r["payload"]["gateway"] == gw["gateway"]
            and r["event_id"] in gw["current_chain_event_ids"]
            and r["payload"]["period_start"] == period_start
            and r["payload"]["settled_micro_eur"] is not None
        ]
        snapshots_by_gateway[gw["gateway"]] = snaps

    return {
        "account": account,
        "period_start": period_start,
        "period_end": period_end,
        "cap_micro_eur": cap,
        "gateways": gateways,
        "policy": normalized_policy,
        "readings": readings,
        "unresolved_event_ids": list(unresolved),
        "boundary_exposure_micro_eur": boundary_exposure,
        "boundary_event_ids": list(boundary_ids),
        "provider_events": provider_events,
        "snapshots_by_gateway": snapshots_by_gateway,
    }


def _validate_reading(reading, account):
    """Validate one reading envelope (validated shape + received_at); all
    readings in the context are for `account`."""
    if not isinstance(reading, dict) or frozenset(reading) != _READING_KEYS:
        _invalid()
    event_id = reading["event_id"]
    machine = reading["machine"]
    r_account = reading["account"]
    kind = reading["kind"]
    observed_at = reading["observed_at"]
    received_at = reading["received_at"]
    if not _is_uuid(event_id):
        _invalid()
    if not _is_alias(machine):
        _invalid()
    # All readings are for this account (never ambiguous).
    if not _is_alias(r_account) or r_account != account:
        _invalid()
    if not _is_nonneg_int(observed_at) or not _is_nonneg_int(received_at):
        _invalid()
    payload = reading["payload"]
    if kind == KIND_PROVIDER_BILLING:
        norm = _validate_provider_billing(payload, observed_at)
    elif kind == KIND_GATEWAY_SNAPSHOT:
        billing = "settled_micro_eur" in payload
        norm = _validate_gateway_snapshot(payload, billing)
        if not billing:
            # A legacy (money-less) snapshot carries no settled figure for
            # L_now; mark it unusable for the ledger floor.
            norm = dict(norm)
            norm["settled_micro_eur"] = None
    elif kind == KIND_CLI_WINDOW:
        norm = _validate_cli_window(payload)
    else:
        _invalid()
    return {
        "event_id": event_id,
        "machine": machine,
        "account": r_account,
        "kind": kind,
        "observed_at": observed_at,
        "received_at": received_at,
        "payload": norm,
    }


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

def _validate_request(request, ctx):
    """Validate the closed evaluation request; return a normalized copy."""
    if not isinstance(request, dict) or frozenset(request) != _REQUEST_KEYS:
        _invalid()
    if request["version"] != 1:
        _invalid()
    if not _is_alias(request["account"]) or request["account"] != ctx["account"]:
        _invalid()
    now_ms = request["now_ms"]
    if not _is_nonneg_int(now_ms):
        _invalid()
    if not _is_nonneg_int(request["requested_micro_eur"]):
        _invalid()

    provider_ids = {e["event_id"] for e in ctx["provider_events"]}
    seen = set()
    coverage = request["coverage"]
    if not isinstance(coverage, list):
        _invalid()
    normalized = []
    for item in coverage:
        if not isinstance(item, dict) or frozenset(item) != _COVERAGE_KEYS:
            _invalid()
        event_id = item["event_id"]
        # A certificate attests a provider sample for this account.
        if not _is_uuid(event_id) or event_id not in provider_ids:
            _invalid()
        if event_id in seen:
            _invalid()  # duplicate event reference
        seen.add(event_id)
        upper_at = item["upper_at"]
        complete_at = item["complete_at"]
        for t in (upper_at, complete_at):
            if t is not None and (not _is_nonneg_int(t) or t > now_ms):
                _invalid()  # bad or after-now time
        if complete_at is not None and upper_at is None:
            _invalid()  # complete without upper
        if complete_at is not None and complete_at > upper_at:
            _invalid()  # complete_at after upper_at
        floor = item["overlap_floor_micro_eur"]
        if floor is not None and not _is_nonneg_int(floor):
            _invalid()
        if not _is_bool(item["matched_provenance"]):
            _invalid()
        normalized.append({
            "event_id": event_id,
            "upper_at": upper_at,
            "complete_at": complete_at,
            "overlap_floor_micro_eur": floor,
            "matched_provenance": item["matched_provenance"],
        })
    return {
        "version": 1,
        "account": request["account"],
        "now_ms": now_ms,
        "requested_micro_eur": request["requested_micro_eur"],
        "coverage": normalized,
    }


# ---------------------------------------------------------------------------
# Current-period state (pure extractions, no reconciliation math)
# ---------------------------------------------------------------------------

def _latest_provider_total(provider_events):
    """P_e: the latest provider total for the current period (by source
    time, then receipt). None when there is no provider sample."""
    if not provider_events:
        return None
    latest = max(provider_events, key=lambda e: (e["observed_at"],
                                                  e["received_at"]))
    return latest["payload"]["total_micro_eur"]


def _latest_ledger_settled(snapshots_by_gateway):
    """L_now: the sum of the latest current-period settled snapshot per
    gateway (0 where a gateway has no usable snapshot)."""
    total = 0
    for snaps in snapshots_by_gateway.values():
        if snaps:
            total += max(snaps, key=lambda s: s[0])[1]
    return total


def _boundary_e(snapshots_by_gateway, complete_at):
    """B_e: the sum of the last ledger snapshot at/before complete_at
    (each within 120 s) across the configured gateways. None (unusable)
    when complete_at is unknown or any gateway lacks a qualifying
    snapshot - a proved empty boundary is the only path to B_e=0."""
    if complete_at is None:
        return None
    total = 0
    for snaps in snapshots_by_gateway.values():
        best = None
        for obs, settled in snaps:
            if obs <= complete_at and (complete_at - obs) <= _BOUNDARY_WINDOW_MS:
                if best is None or obs > best[0]:
                    best = (obs, settled)
        if best is None:
            return None
        total += best[1]
    return total


def _resolve_overlap(entry, P, L_now, B_e):
    """O_e for one certified provider event. An explicit
    overlap_floor_micro_eur wins when present and requires complete
    coverage, matched provenance, a usable B_e and
    0<=O_e<=min(P,L_now,B_e) - a violation is INVALID_CONFIG, never
    clipped. Absent, O_e=B_e only under those proofs and B_e<=P_e; else
    O_e=0 (overlap unproved, a conservative diagnostic)."""
    floor = entry["overlap_floor_micro_eur"]
    complete = entry["complete_at"] is not None
    proof = entry["matched_provenance"] is True
    if floor is not None:
        if not (complete and proof and B_e is not None and P is not None):
            _invalid()
        if not (0 <= floor <= min(P, L_now, B_e)):
            _invalid()
        return floor
    if complete and proof and B_e is not None and P is not None and B_e <= P:
        return B_e
    return 0


def _estimate(provider_events, overlap_by_event, L_now):
    """Spend estimate E = max(L_now, max_e(P_e + L_now - O_e)). Missing P
    means estimate=null (not P=0). The diagnostic may over-count when
    overlap is unproved; it never admits (headroom fails closed)."""
    if not provider_events:
        return None
    best = L_now
    for e in provider_events:
        O_e = overlap_by_event.get(e["event_id"], 0)
        cand = e["payload"]["total_micro_eur"] + L_now - O_e
        if cand > best:
            best = cand
    return best


def _known_floor(P, L_now):
    """known_floor = max of the known operands (P, L_now); null if neither
    is known. Unknown operands are omitted, never zero-filled."""
    known = [v for v in (P, L_now) if v is not None]
    return max(known) if known else None


def _any_non_ai_current(provider_events):
    """A retained current-period non_ai_spend_present (exact boolean)."""
    return any(e["payload"]["non_ai_spend_present"] is True
               for e in provider_events)


# ---------------------------------------------------------------------------
# The ordered decision table (turn 2: no_sample + the catch-all are live;
# the rest are turn-3/4 stubs that never match, so the evaluation falls
# through to the catch-all and headroom stays unavailable - fail closed).
# ---------------------------------------------------------------------------

def _base_row(ctx, P):
    """Walk the ordered decision table; return the first matching row as
    (state, reason, action, quality). Rows whose reconciliation logic
    lands in turns 3/4 are marked and return None (no match)."""
    for row in _BASE_ROW_ORDER:
        # 1: basis/scope/unusable-ledger -> alert/unknown.
        #   TODO(turn 3): the unusable-ledger and scope checks.
        # 3: fresh gross excess -> hold.
        #   TODO(turn 3): the gross-excess predicate with time/ledger guards.
        # 4: latched conflict -> alert.
        #   TODO(turn 3): the conflict latch from the reduce.
        # 5: unknown midnight boundary exposure -> none/snapshot_skew.
        #   TODO(turn 3): the boundary-exposure timer.
        # 6: certified historical excess -> hold.
        #   TODO(turn 3): the certified historical bracket.
        # 7: certified P below old settled -> alert/missing_old_spend.
        #   TODO(turn 3): the certified historical negative gap.
        # 8: idle material negative gap -> alert/lag_investigate.
        #   TODO(turn 3): the read-only lag timer.
        # 9: no certificate, gross time guards absent -> none/snapshot_skew.
        #   TODO(turn 3): the as-of bracket guards.
        # 11: certified difference covered -> reserve_or_scope_covered.
        #   TODO(turn 3): the reserve/allowance bracket.
        # 12: matched within tolerance, complete evidence -> matched/Q.
        #   TODO(turn 3): the matched classification.
        if row == "no_sample" and P is None:
            return ("unknown", "billing_unknown", "alert", "provisional")
        if row == "unresolved_bounds":
            # Catch-all: an incomplete certificate/bracket is a provisional
            # observation with no action. This is the only base row that
            # matches in turn 2, so no turn-2 evaluation ever reaches a
            # "matched" or hold result.
            return ("temporally_unresolved", "coverage_unknown",
                    "none", "provisional")
    # Unreachable: "unresolved_bounds" always matches.
    _invalid()


def evaluate_billing(context, request):
    """Evaluate one metered account's billing evidence (pure).

    Returns the closed Evaluation output (all fields required, nullable
    arithmetic when unknown). Turn 2 fails closed on admission:
    admission_basis=unavailable, headroom_micro_eur=null and
    request_fits=null (except the hold rule), so nothing is admitted on an
    un-evaluated estimate. proposal is always null for the evaluator (the
    opening planner is a separate, later function).
    """
    ctx = _validate_context(context)
    req = _validate_request(request, ctx)

    P = _latest_provider_total(ctx["provider_events"])
    L_now = _latest_ledger_settled(ctx["snapshots_by_gateway"])

    # Resolve each certificate's overlap (raises INVALID_CONFIG on an
    # out-of-range explicit floor), then the spend estimate.
    overlap_by_event = {}
    for entry in req["coverage"]:
        B_e = _boundary_e(ctx["snapshots_by_gateway"], entry["complete_at"])
        overlap_by_event[entry["event_id"]] = _resolve_overlap(
            entry, P, L_now, B_e)
    estimate = _estimate(ctx["provider_events"], overlap_by_event, L_now)
    known_floor = _known_floor(P, L_now)

    state, reason, action, quality = _base_row(ctx, P)
    action, reason = apply_non_ai_overlay(
        action, reason, _any_non_ai_current(ctx["provider_events"]))

    # Turn 2: headroom is not yet implemented - fail closed.
    headroom = None
    admission_basis = "unavailable"
    request_fits = False if action == "hold" else None

    return {
        "state": state,
        "action": action,
        "reason": reason,
        "quality": quality,
        "admission_basis": admission_basis,
        "known_floor_micro_eur": known_floor,
        "estimate_micro_eur": estimate,
        "headroom_micro_eur": headroom,
        "request_fits": request_fits,
        "proposal": None,
    }
