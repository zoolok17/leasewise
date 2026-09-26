"""Pure billing evaluation (card 2b-2).

evaluate_billing(context, request) -> Evaluation is a PURE function:
no I/O, no store writes, no clock reads (now_ms is injected on the
request), and it never mutates its inputs. The evaluator never mutates
reconcile state; it reads the explicit, already-validated context.

Turn 2 pinned the closed input surface, the complete output shape, the
ordered decision-table precedence and the non-AI overlay. Turn 3 adds
the reconciliation rows: the gross-excess hold (uncertified, with
reserves/allowance/boundary/tolerance), the conflict-latest ordering
(gross first, conflict preserved), the latest-seq ledger selection, the
gateway usability rules, the listed-transition precedence, the boundary
(midnight) exposure, the as-of bracket (skew/covered/excess) and the
read-only lag timer. Turn 4 fills in the advisory headroom and its
admission basis, and the closed output/privacy contract: comparable
headroom = cap - E - R_now (admission_basis=billing_estimate) only
with complete overlap proof for every retained provider event, a
fresh sample and no latched conflict; otherwise headroom =
cap - allowance - L_now - R_now (admission_basis=ledger_fallback)
with usable local ledger inputs - the separate non-AI allowance stays
out of the requested H, and the diagnostic E is not enforced in
fallback. A hold recommendation denies H regardless of headroom; a
missing P keeps the fallback (estimate null, never P=0); a rejected
(cap-exceeding) estimate or a missing/unusable ledger fails closed
(headroom null, admission_basis unavailable) - nothing is ever
admitted on a rejected estimate.
"""

import math

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

# A ledger snapshot counts for the as-of bracket only within 120 s of
# the certified time (section 11: "require both within 120s of T"), and
# the existing 30 s clock-skew margin lets a report cover a provider
# receipt at observed_at >= received_at + 30000.
_BRACKET_WINDOW_MS = 120_000
_RECEIPT_MARGIN_MS = 30_000

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
# decision table). First matching base row wins.
_BASE_ROW_ORDER = (
    "basis_scope",                 # 1: unknown / basis_unknown,scope_mismatch,gateway_unusable / alert / unknown
    "no_sample",                   # 2: unknown / billing_unknown (no sample) / alert / provisional
    "stale_sample",                #    unknown / billing_stale / alert / provisional
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
    gateway_names = set()
    for gw in context["gateways"]:
        if not isinstance(gw, dict) or frozenset(gw) != _GATEWAY_KEYS:
            _invalid()
        if not _is_alias(gw["gateway"]):
            _invalid()
        if gw["gateway"] in gateway_names:
            _invalid()
        gateway_names.add(gw["gateway"])
        if not _is_nonneg_int(gw["share_micro_eur"]):
            _invalid()
        chain = gw["current_chain_event_ids"]
        if not isinstance(chain, list) or not all(_is_uuid(e) for e in chain):
            _invalid()
        if len(chain) != len(set(chain)):
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
    by_id = {}
    for reading in readings:
        event_id = reading["event_id"]
        if event_id in by_id and by_id[event_id] != reading:
            _invalid()
        by_id[event_id] = reading
    readings = list(by_id.values())

    # unresolved_event_ids: local UUID references (closed type).
    unresolved = context["unresolved_event_ids"]
    if not isinstance(unresolved, list) or not all(_is_uuid(e) for e in unresolved):
        _invalid()
    if len(unresolved) != len(set(unresolved)) or any(e not in by_id for e in unresolved):
        _invalid()

    boundary_exposure = context["boundary_exposure_micro_eur"]
    if boundary_exposure is not None and not _is_nonneg_int(boundary_exposure):
        _invalid()

    # boundary_event_ids: references to store boundary evidence (known
    # reading event ids).
    boundary_ids = context["boundary_event_ids"]
    if not isinstance(boundary_ids, list) or not all(_is_uuid(e) for e in boundary_ids):
        _invalid()
    if len(boundary_ids) != len(set(boundary_ids)):
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
            row = by_id[ref]
            if (row["kind"] != KIND_GATEWAY_SNAPSHOT
                    or row["payload"]["gateway"] != gw["gateway"]):
                _invalid()

    # current-period provider events and per-gateway ledger chains
    # (seq, observed_at, money fields); the chain order is by seq.
    provider_events = [r for r in readings if r["kind"] == KIND_PROVIDER_BILLING
                       and r["payload"]["period_start"] == period_start]
    chains_by_gateway = {}
    for gw in gateways:
        chain = [
            (r["payload"]["seq"], r["observed_at"],
             r["payload"]["settled_micro_eur"],
             r["payload"]["reserved_micro_eur"],
             r["payload"]["uncertain_micro_eur"], r["event_id"])
            for r in readings
            if r["kind"] == KIND_GATEWAY_SNAPSHOT
            and r["payload"]["gateway"] == gw["gateway"]
            and r["event_id"] in gw["current_chain_event_ids"]
            and r["payload"]["period_start"] == period_start
            and r["payload"]["settled_micro_eur"] is not None
        ]
        chain.sort(key=lambda row: row[0])
        chains_by_gateway[gw["gateway"]] = chain

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
        "chains_by_gateway": chains_by_gateway,
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
    if not isinstance(payload, dict):
        _invalid()
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
    if type(request["version"]) is not int or request["version"] != 1:
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

def _latest_provider(provider_events):
    """The latest provider event of the current period by (source time,
    receipt). None when there is no provider sample. The gross check uses
    this value, never the retained high-water."""
    if not provider_events:
        return None
    return max(provider_events, key=lambda e: (e["observed_at"],
                                               e["received_at"]))


def _provider_total(provider_events):
    latest = _latest_provider(provider_events)
    return latest["payload"]["total_micro_eur"] if latest else None


def _chain_latest(chain):
    """The highest-seq row of a store-selected chain (seq ordering, never
    observed_at); None when the chain has no usable report."""
    return chain[-1] if chain else None


def _chain_is_decreasing(chain):
    """A decreased in-chain total (seq order) breaks the nondecreasing
    ledger invariant; the chain is unusable then."""
    return any(chain[i + 1][2] < chain[i][2] for i in range(len(chain) - 1))


def _gateway_usable(ctx):
    """(usable, unusable_reason) for the current ledger: every configured
    gateway must have a usable current chain - a nondecreasing chain and
    no reconciliation requirement (an empty chain is an identity with no
    current amount and contributes 0 to L_now). A transition listed in
    unresolved_event_ids (copied from the store's reconcile state) keeps
    its gateway unusable; a repeated ok report cannot clear it, and a
    transition no longer listed does not re-latch."""
    for gw in ctx["gateways"]:
        chain = ctx["chains_by_gateway"][gw["gateway"]]
        if _chain_is_decreasing(chain):
            return False, "gateway_unusable"
        listed = set(ctx["unresolved_event_ids"])
        if listed & {row[5] for row in chain}:
            return False, "gateway_unusable"
    return True, None


def _ledger_now(ctx):
    """L_now / R_now: per gateway, the latest (highest-seq) snapshot's
    settled, and reserved + uncertain. R_now never adds unattributed
    again (it is a subset of settled). None when the ledger is unusable
    (missing/ambiguous chains, a decreased total, a listed transition or
    a reconciliation-required gateway)."""
    if not _gateway_usable(ctx)[0]:
        return None, None
    l_now = 0
    r_now = 0
    for gw in ctx["gateways"]:
        chain = ctx["chains_by_gateway"][gw["gateway"]]
        if not chain:
            # A configured identity with no current amount: L_now is
            # unknown (null), never zero-filled.
            return None, None
        latest = _chain_latest(chain)
        l_now += latest[2]
        r_now += latest[3] + latest[4]
    return l_now, r_now


def _boundary_known(ctx):
    """The midnight boundary exposure B: known only with store boundary
    evidence (a boundary event id) or an explicit 0 with none - a null
    exposure is UNKNOWN, never zero. Returns (B, known)."""
    exposure = ctx["boundary_exposure_micro_eur"]
    if exposure is None:
        return None, False
    return exposure, True


def _provider_fresh(latest, policy, now_ms):
    """The sample is fresh when now - observed_at <= max_age_seconds
    (source age, the billing freshness rule)."""
    return (now_ms - latest["observed_at"]
            <= policy["max_age_seconds"] * 1000)


def _conflict_latched(provider_events):
    """Replay the store's source-time reduce over the current-period
    provider totals: a newer total below the retained high-water (or an
    equal-time unequal total) latches the sticky conflict. The evaluator
    preserves it, never clears it."""
    rows = sorted(provider_events, key=lambda e: e["observed_at"])
    conflict = False
    high_water = None
    latest_obs = None
    latest_total = None
    for e in rows:
        total = e["payload"]["total_micro_eur"]
        if high_water is None:
            high_water = total
        if e["observed_at"] > (latest_obs or -1):
            if total < high_water:
                conflict = True
            if total > high_water:
                high_water = total
            latest_obs = e["observed_at"]
            latest_total = total
        elif e["observed_at"] == latest_obs and total != latest_total:
            conflict = True
        elif total > high_water:
            high_water = total
    return conflict


def _last_financial_change(ctx):
    """Read-only idle timer base: the latest stored ledger amount/epoch
    change - the max observed_at across the selected gateway chains. The
    provider sample is the EXTERNAL observation, not local activity, so it
    does not reset the local idle timer (a fresh provider reading against
    an idle ledger is exactly what the diagnostic catches). None when
    there is no ledger history: missing history cannot establish elapsed
    idle time, and reserve activity (a newer gateway report) resets it."""
    changes = []
    for chain in ctx["chains_by_gateway"].values():
        changes.extend(row[1] for row in chain)
    return max(changes) if changes else None


def _bracket(chains, complete_at):
    """As-of bracket at the certified time T=complete_at: per gateway,
    the last chain snapshot at/before T (its settled is the L_asof base)
    and the first at/after T (its reserved + uncertain are the still-open
    amounts). Both must be within 120 s of T; otherwise the bracket is
    unusable (None). L_asof = sum of settled at/before T. U_asof =
    L_asof + sum of the at/after-T reserved + uncertain (the upper bound
    of in-scope money at T)."""
    l_asof = 0
    open_after = 0
    for chain in chains:
        lower = None
        upper = None
        for _seq, obs, settled, reserved, uncertain, _ev in chain:
            if obs <= complete_at:
                if lower is None or obs > lower[0]:
                    lower = (obs, settled)
            else:
                if upper is None or obs < upper[0]:
                    upper = (obs, reserved, uncertain)
        if lower is None or complete_at - lower[0] > _BRACKET_WINDOW_MS:
            return None
        if upper is None or upper[0] - complete_at > _BRACKET_WINDOW_MS:
            return None
        l_asof += lower[1]
        open_after += upper[1] + upper[2]
    return l_asof, l_asof + open_after


def _bracket_usable(chains, complete_at, now_ms):
    """The bracket side is usable only when every configured gateway
    contributes (a gateway with no chain cannot be bracketed)."""
    return len(chains) > 0 and all(c for c in chains) and _bracket(
        chains, complete_at) is not None


def _tolerance(l, policy):
    """tau(L) = max(abs_tolerance, ceil(L * relative_bps / 10000)). The
    absolute floor dominates at low totals; tolerance is a comparison
    width, never a budget credit."""
    rel = math.ceil(l * policy["relative_tolerance_bps"] / 10_000) \
        if policy["relative_tolerance_bps"] else 0
    return max(policy["abs_tolerance_micro_eur"], rel)


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
        if not (complete and proof and B_e is not None and P is not None
                and L_now is not None):
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
    best = L_now if L_now is not None else 0
    for e in provider_events:
        O_e = overlap_by_event.get(e["event_id"], 0)
        cand = e["payload"]["total_micro_eur"] + (L_now or 0) - O_e
        if cand > best:
            best = cand
    return best


def _any_non_ai_current(provider_events):
    """A retained current-period non_ai_spend_present (exact boolean)."""
    return any(e["payload"]["non_ai_spend_present"] is True
               for e in provider_events)


# ---------------------------------------------------------------------------
# The ordered decision table (first matching base row wins).
# ---------------------------------------------------------------------------

def _gross_excess_holds(latest, l_now, r_now, b_known, b, allowance, policy,
                        chains, now_ms):
    """Row 3: a FRESH latest P_e beyond L_now + R_now + B + allowance +
    tau(L_now) is unexplained_excess / hold / comparable, without any
    certificate. The time/ledger guards: every gateway's latest report
    must cover the provider receipt (observed_at >= received_at + 30 s)
    and be <= 120 s old; an unknown boundary disables the check (never
    B=0); a nonmonotonic or incomplete ledger (unusable) invalidates the
    sufficient test."""
    if l_now is None or not b_known:
        return False
    if not _provider_fresh(latest, policy, now_ms):
        return False
    receipt = latest["received_at"]
    for chain in chains:
        latest_report = _chain_latest(chain)
        if latest_report is None:
            return False
        if latest_report[1] < receipt + _RECEIPT_MARGIN_MS:
            return False  # does not cover the provider receipt
        if latest_report[1] - receipt > _BRACKET_WINDOW_MS:
            return False  # older than 120 s after the provider receipt
        if now_ms - latest_report[1] > _BRACKET_WINDOW_MS:
            return False  # stale report
    tau = _tolerance(l_now, policy)
    return latest["payload"]["total_micro_eur"] > \
        l_now + r_now + b + allowance + tau


def _base_row(ctx, req, P, l_now):
    """Walk the ordered decision table; return the first matching row as
    (state, reason, action, quality). The headroom math stays fail-closed
    until turn 4; no row here admits."""
    now_ms = req["now_ms"]
    policy = ctx["policy"]
    provider_events = ctx["provider_events"]
    allowance = policy["non_ai_allowance_micro_eur"]
    chains = list(ctx["chains_by_gateway"].values())
    usable, unusable_reason = _gateway_usable(ctx)
    latest = _latest_provider(provider_events)
    b, b_known = _boundary_known(ctx)

    # Row 1: unusable ledger -> unknown / gateway_unusable / alert /
    # unknown (basis/scope variants are turn-4 basis work).
    if not usable:
        return ("unknown", unusable_reason, "alert", "unknown")

    # Row 2: no provider sample -> alert (never a new hold).
    if latest is None:
        return ("unknown", "billing_unknown", "alert", "provisional")

    # Row 2b: a stale sample alerts; a stale P never gross-holds.
    if not _provider_fresh(latest, policy, now_ms):
        return ("unknown", "billing_stale", "alert", "provisional")

    # Row 3: the fresh latest-value gross check runs BEFORE the conflict
    # row, with the latest receipt's own guards, without clearing the
    # conflict.
    if _gross_excess_holds(latest, l_now, _ledger_now(ctx)[1], b_known, b,
                           allowance, policy, chains, now_ms):
        return ("unexplained_excess", "provider_excess",
                "hold", "comparable")

    # Row 4: the remaining latched conflict alerts (preserved, never
    # cleared by the evaluator).
    if _conflict_latched(provider_events):
        return ("conflict", "source_conflict", "alert", "provisional")

    # Row 5: unknown midnight boundary exposure - the excess checks are
    # disabled as snapshot_skew, not B=0.
    if not b_known:
        return ("temporally_unresolved", "snapshot_skew",
                "none", "provisional")

    # Rows 6/7/11: the certified as-of brackets (per event, complete
    # coverage required); the chain must be continuous (nondecreasing,
    # usable) for the historical amounts to count.
    for entry in req["coverage"]:
        complete_at = entry["complete_at"]
        if complete_at is None:
            continue
        if not usable or _bracket(chains, complete_at) is None:
            continue  # unusable/ambiguous chain: no historical amounts
        l_asof, u_asof = _bracket(chains, complete_at)
        P_e = next(e["payload"]["total_micro_eur"]
                   for e in provider_events
                   if e["event_id"] == entry["event_id"])
        tau_asof = _tolerance(l_asof, policy)
        if P_e > u_asof + b + allowance + tau_asof:
            # Row 6: certified excess beyond the upper + B + allowance.
            return ("unexplained_excess", "provider_excess",
                    "hold", "comparable")
        if P_e > l_asof + tau_asof:
            # Row 11: the difference is covered by the upper's
            # reserved/uncertain plus allowance.
            return ("reserve_or_scope_covered",
                    "reserve_or_scope_covered", "none", "comparable")

    # Row 8: the read-only idle diagnostic. A material negative gap
    # (P below L_now - tau) alerts once now - last stored amount/epoch
    # change reaches lag_seconds; equality never starts the timer, and
    # missing history cannot establish elapsed idle time.
    last_change = _last_financial_change(ctx)
    if last_change is not None:
        tau_now = _tolerance(l_now, policy)
        idle_ms = now_ms - last_change
        if (idle_ms >= policy["lag_seconds"] * 1000
                and P < l_now - tau_now):
            return ("provider_behind", "lag_investigate",
                    "alert", "provisional")

    # Row 9: a fresh sample with no usable certificate brackets but no
    # time/ledger guards either -> provisional snapshot_skew. (The
    # no-bracket case: the bracket side cannot be proved.)
    if latest is not None and not any(
            e["complete_at"] is not None for e in req["coverage"]):
        return ("temporally_unresolved", "snapshot_skew",
                "none", "provisional")

    # Row 10: catch-all - an incomplete certificate/bracket is a
    # provisional observation with no action.
    return ("temporally_unresolved", "coverage_unknown",
            "none", "provisional")


def evaluate_billing(context, request):
    """Evaluate one metered account's billing evidence (pure).

    Returns the closed Evaluation output (all fields required, nullable
    arithmetic when unknown). Turn 4 fills in the advisory headroom;
    until then headroom_micro_eur=null, admission_basis=unavailable and
    request_fits=null (false on a hold) - nothing is admitted on an
    un-evaluated estimate. proposal is always null for the evaluator (the
    opening planner is a separate, later function).
    """
    ctx = _validate_context(context)
    req = _validate_request(request, ctx)

    P = _provider_total(ctx["provider_events"])
    l_now, r_now = _ledger_now(ctx)

    # Resolve each certificate's overlap (raises INVALID_CONFIG on an
    # out-of-range explicit floor), then the spend estimate.
    overlap_by_event = {}
    for entry in req["coverage"]:
        B_e = None
        if entry["complete_at"] is not None:
            B_e = _boundary_e(ctx["chains_by_gateway"].values(),
                              entry["complete_at"])
        overlap_by_event[entry["event_id"]] = _resolve_overlap(
            entry, P, l_now, B_e)
    estimate = _estimate(ctx["provider_events"], overlap_by_event, l_now)
    # The floor diagnostic is the ledger's current settled total (L_now);
    # unattributed is a subset of settled and never added again, and an
    # unusable ledger has no known floor (null, never zero-filled).
    known_floor = l_now

    state, reason, action, quality = _base_row(
        ctx, req, P, l_now)
    action, reason = apply_non_ai_overlay(
        action, reason, _any_non_ai_current(ctx["provider_events"]))

    conflict = _conflict_latched(ctx["provider_events"])
    headroom, admission_basis = _advisory_headroom(
        ctx, req, l_now, r_now, estimate, conflict, P)
    # A hold recommendation denies the requested H regardless of
    # headroom (always false-fit). Otherwise H fits when the headroom
    # is available and the requested amount does not exceed it; with no
    # known headroom the fit is UNKNOWN (None) - never a false-fit, and
    # never an admission either.
    if action == "hold":
        request_fits = False
    elif headroom is None:
        request_fits = None
    else:
        request_fits = req["requested_micro_eur"] <= headroom

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


def _advisory_headroom(ctx, req, l_now, r_now, estimate, conflict, P):
    """(headroom, admission_basis) for the advisory managed-work
    headroom (section 11, "Advisory headroom and opening plan").
    Comparable (admission_basis=billing_estimate) only when EVERY
    retained provider event is fully proven (a coverage entry with a
    certified time, matched provenance, a usable B_e and the derived
    O_e=B_e under B_e<=P_e - never an explicit floor), the latest
    sample is fresh and no conflict is latched: headroom = cap - E -
    R_now. Otherwise, with usable local ledger inputs (R_now known),
    the fallback: headroom = cap - allowance - L_now - R_now
    (admission_basis=ledger_fallback) - the separate non-AI allowance
    stays out of the requested H, and the diagnostic E is kept for
    diagnostics, not enforced in fallback. A cap-exceeding headroom
    is rejected: the estimate fails closed (null, unavailable), never
    reported negative, and nothing is admitted on it. A
    missing/unusable ledger leaves both fields null/unavailable."""
    if r_now is None:
        return None, "unavailable"
    cap = ctx["cap_micro_eur"]
    if P is not None and estimate is not None:
        comparable = (not conflict
                      and _provider_fresh(
                          _latest_provider(ctx["provider_events"]),
                          ctx["policy"], req["now_ms"])
                      and _fully_proven(ctx["provider_events"],
                                        req["coverage"],
                                        ctx["chains_by_gateway"].values()))
        if comparable:
            headroom = cap - estimate - r_now
            if 0 <= headroom <= INT64_MAX:
                return headroom, "billing_estimate"
            # Rejected (the estimate exceeds the cap): fail closed.
            return None, "unavailable"
    # The fallback keeps the separate non-AI allowance out of H.
    headroom = (cap - ctx["policy"]["non_ai_allowance_micro_eur"]
                - l_now - r_now)
    if 0 <= headroom <= INT64_MAX:
        return headroom, "ledger_fallback"
    return None, "unavailable"


def _fully_proven(provider_events, coverage, chains):
    """Whether every retained provider event carries the proofs for a
    comparable estimate: a coverage entry with a certified (complete)
    time, matched provenance, a usable B_e (the last ledger snapshot
    at/before the certified time, within 120 s, on every gateway) and
    B_e<=P_e (the derived O_e=B_e) - a hand-set explicit floor never
    counts as proof."""
    by_event = {e["event_id"]: e for e in coverage}
    for e in provider_events:
        entry = by_event.get(e["event_id"])
        if (entry is None or entry["complete_at"] is None
                or entry["matched_provenance"] is not True):
            return False
        b_e = _boundary_e(chains, entry["complete_at"])
        if b_e is None or b_e > e["payload"]["total_micro_eur"]:
            return False
    return True


def _boundary_e(chains, complete_at):
    """B_e: the sum of the last ledger snapshot at/before complete_at
    (each within 120 s) across the configured gateways. None (unusable)
    when complete_at is unknown or any gateway lacks a qualifying
    snapshot - a proved empty boundary is the only path to B_e=0."""
    total = 0
    for chain in chains:
        best = None
        for _seq, obs, settled, _r, _u, _ev in chain:
            if obs <= complete_at and (complete_at - obs) <= _BRACKET_WINDOW_MS:
                if best is None or obs > best[0]:
                    best = (obs, settled)
        if best is None:
            return None
        total += best[1]
    return total
