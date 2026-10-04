"""Pure advisory billing evaluation (card 2b-2).

Validate the closed context, convert to the cap's monetary basis, then
evaluate the ordered predicates globally across the retained evidence.
Both headroom formulas require fresh, continuous gateway chains. Only
proved, fresh, conflict-free billing uses the provider spend estimate;
otherwise the ledger fallback retains the separate non-AI allowance.

No I/O, clock reads, input mutation, receipts or reconcile-state writes.
A HOLD is a recommendation and always makes request_fits false.
"""

from . import money
from .reduce import ROLE_DOWN, convert_basis, reduce_billing_total

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
    "stale_sample",                #    stale / billing_stale / alert / provisional
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
    "lag_expected",                # 13: provider_behind / lag_expected / none / Q
    "incomplete",                  # 14: temporally_unresolved / coverage_unknown / none / provisional
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
        # A payload that names a reader incarnation is a version-4 report and must have EXACTLY the version-4 keys (card R1-3d:
        # the evaluator's own reading check used to refuse every version-4 payload, so the real path could never reach it).
        v4 = "reader_incarnation" in payload
        billing = v4 or "settled_micro_eur" in payload
        norm = _validate_gateway_snapshot(payload, billing, v4)
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

def _validate_coverage(coverage, ctx, now_ms):
    """Validate the closed per-event coverage array; return a normalized
    copy. Shared by the evaluation and the opening request."""
    provider_ids = {e["event_id"] for e in ctx["provider_events"]}
    seen = set()
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
        if floor is not None and (complete_at is None or not item["matched_provenance"]):
            _invalid()
        normalized.append({
            "event_id": event_id,
            "upper_at": upper_at,
            "complete_at": complete_at,
            "overlap_floor_micro_eur": floor,
            "matched_provenance": item["matched_provenance"],
        })
    return normalized


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

    normalized = _validate_coverage(request["coverage"], ctx, now_ms)
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

def _checked(value):
    """`value` when it is an amount the store could keep. An UNKNOWN passes through: it is never turned into a number."""
    if value is money.UNKNOWN:
        return value
    if type(value) is not int or not 0 <= value <= INT64_MAX:
        _invalid()
    return value


def _sum(values):
    """The exact total of `values`, or UNKNOWN when any term is unknown (never the known part, never 0), refused as
    INVALID_CONFIG above the largest number the store keeps (card R1-M: the one place that adds is `money`)."""
    try:
        total = money.add(*values)
    except money.MoneyError:
        _invalid()
    return _checked(total)


def _normalize_money(ctx):
    """Convert detached amounts once, before any scope or spend comparison."""
    policy = ctx["policy"]
    target = policy["cap_basis"]
    sources = (policy["provider_basis"], policy["ledger_basis"])
    if target == "unknown" or "unknown" in sources:
        return False
    if any(source != target for source in sources) and policy["vat_bps"] is None:
        return False

    def convert(value, source, role=None):
        if role is None:
            return convert_basis(value, source, target, policy["vat_bps"])
        return convert_basis(value, source, target, policy["vat_bps"], role)

    for event in ctx["provider_events"]:
        event["payload"]["total_micro_eur"] = convert(
            event["payload"]["total_micro_eur"], policy["provider_basis"])
    for name, chain in ctx["chains_by_gateway"].items():
        ctx["chains_by_gateway"][name] = [
            (seq, obs, *(convert(v, policy["ledger_basis"]) for v in (l, r, u)), ev)
            for seq, obs, l, r, u, ev in chain]
    for gateway in ctx["gateways"]:
        # A share is used two ways. As an allocation (shares + allowance against the cap) it is an exposure and rounds UP, as
        # it always did. As the ceiling of its own gateway (`share_g`) it is a limit and rounds DOWN: never more than it is.
        gateway["share_limit_micro_eur"] = convert(gateway["share_micro_eur"], policy["ledger_basis"], ROLE_DOWN)
        gateway["share_micro_eur"] = convert(gateway["share_micro_eur"], policy["ledger_basis"])
    if ctx["boundary_exposure_micro_eur"] is not None:
        ctx["boundary_exposure_micro_eur"] = convert(
            ctx["boundary_exposure_micro_eur"], policy["ledger_basis"])
    return True


def _scope_matches(ctx):
    policy = ctx["policy"]
    if _sum([g["share_micro_eur"] for g in ctx["gateways"]]
            + [policy["non_ai_allowance_micro_eur"]]) > ctx["cap_micro_eur"]:
        return False
    return all(e["machine"] == policy["reporter_machine"]
               and e["payload"]["scope_alias"] == policy["scope_alias"]
               and e["payload"]["scope_revision"] == policy["scope_revision"]
               for e in ctx["provider_events"])


def _gateway_usable(ctx, now_ms, *, require_fresh=True):
    """Validate the store-selected chain before selecting highest seq.

    Reconcile state is authoritative; history validates continuity but does
    not re-latch a cleared transition. Other chains never supply money.

    A second line of defence behind the store's own decisions (card R1-3c): the chain it is given starts at the adoption report
    itself (a chain of that one report is a whole chain) and must be ONE reader's, in one epoch, generation and month.
    """
    by_id = {r["event_id"]: r for r in ctx["readings"]}
    unresolved = set(ctx["unresolved_event_ids"])
    if not ctx["gateways"]:
        return False
    for gw in ctx["gateways"]:
        chain = ctx["chains_by_gateway"][gw["gateway"]]
        if not chain or len(chain) != len(gw["current_chain_event_ids"]):
            return False
        # A transition may reference a previous chain of this gateway.
        if any(by_id[e]["kind"] == KIND_GATEWAY_SNAPSHOT
               and by_id[e]["payload"]["gateway"] == gw["gateway"] for e in unresolved):
            return False
        identity = None
        seen = {}
        previous_settled = None
        for row in chain:
            event = by_id[row[5]]
            payload = event["payload"]
            key = (event["machine"], payload["ledger_epoch"], payload["policy_hashes"])
            if "reader_incarnation" in payload:                  # a version-4 report: one reader, one generation, one month
                key += (payload["reader_incarnation"], payload["generation"], payload["period_start"])
            if identity is not None and key != identity:
                return False
            identity = key
            money = tuple(payload[key] for key in (
                "settled_micro_eur", "reserved_micro_eur", "uncertain_micro_eur",
                "unattributed_micro_eur"))
            report = (event["observed_at"], money)
            if row[0] in seen and seen[row[0]] != report:
                return False
            seen[row[0]] = report
            if previous_settled is not None and money[0] < previous_settled:
                return False
            previous_settled = money[0]
        if require_fresh and not 0 <= now_ms - chain[-1][1] <= _BRACKET_WINDOW_MS:
            return False
    return True


def _latest_provider(events):
    return max(events, key=lambda e: (e["observed_at"], e["event_id"])) if events else None


def _conflict_latched(events):
    reduced = None
    for event in sorted(events, key=lambda e: (e["observed_at"], e["event_id"])):
        result = reduce_billing_total(reduced, event["payload"]["total_micro_eur"],
                                      event["observed_at"], event["event_id"])
        if result is not None:
            reduced = result
    return bool(reduced and reduced[-1])


def _fresh(event, policy, now_ms):
    return 0 <= now_ms - event["observed_at"] <= policy["max_age_seconds"] * 1000


def _tolerance(ledger, policy):
    relative = (ledger * policy["relative_tolerance_bps"] + 9999) // 10000
    return _checked(max(policy["abs_tolerance_micro_eur"], relative))


def _boundary_e(chains, at, max_age_ms=_BRACKET_WINDOW_MS):
    """Lower settled bound; old-spend diagnostics need no proximity limit."""
    if at is None or not chains:
        return None
    rows = [max((r for r in chain if r[1] <= at
                 and (max_age_ms is None or at-r[1] <= max_age_ms)),
                key=lambda r: (r[1], r[0]), default=None) for chain in chains]
    return _sum(r[2] for r in rows) if all(r is not None for r in rows) else None


def _bracket(chains, at):
    """Inclusive brackets around upper_at; settled AND open come from UPPER."""
    if at is None or not chains:
        return None
    upper = []
    for chain in chains:
        lower = max((r for r in chain if 0 <= at-r[1] <= _BRACKET_WINDOW_MS),
                    key=lambda r: (r[1], r[0]), default=None)
        # At equal observation times, the later sequence carries the
        # conservative upper exposure. Clock rollback must not invert
        # the bracket, even when current-chain ordering remains valid.
        following = min((r for r in chain if 0 <= r[1]-at <= _BRACKET_WINDOW_MS),
                        key=lambda r: (r[1], -r[0]), default=None)
        if lower is None or following is None or following[0] < lower[0]:
            return None
        upper.append(following)
    return _sum(r[2] for r in upper), _sum(v for r in upper for v in r[2:5])


def _validate_known_overlap_bounds(ctx, req):
    """Staleness cannot excuse evidence disproved by known numeric bounds."""
    explicit_entries = [e for e in req["coverage"]
                        if e["overlap_floor_micro_eur"] is not None]
    if not explicit_entries:
        return
    amounts = {e["event_id"]: e["payload"]["total_micro_eur"]
               for e in ctx["provider_events"]}
    continuous = _gateway_usable(ctx, req["now_ms"], require_fresh=False)
    chains = list(ctx["chains_by_gateway"].values())
    ledger = _sum(chain[-1][2] for chain in chains) if continuous else None
    for entry in explicit_entries:
        explicit = entry["overlap_floor_micro_eur"]
        bounds = [amounts[entry["event_id"]]]
        if continuous:
            bounds.append(ledger)
            bound = _boundary_e(chains, entry["complete_at"])
            if bound is not None:
                bounds.append(bound)
        if explicit > min(bounds):
            _invalid()


def _overlaps(events, coverage, chains, ledger):
    by_id = {e["event_id"]: e for e in coverage}
    amounts = {}
    proved = bool(events)
    for event in events:
        entry = by_id.get(event["event_id"])
        amount = event["payload"]["total_micro_eur"]
        bound = _boundary_e(chains, entry["complete_at"]) if entry else None
        proof = bool(entry and entry["matched_provenance"] and bound is not None)
        explicit = entry["overlap_floor_micro_eur"] if entry else None
        if explicit is not None:
            if not proof or not 0 <= explicit <= min(amount, ledger, bound):
                _invalid()
            overlap = explicit
        elif proof and bound <= amount:
            overlap = bound
        else:
            overlap, proof = 0, False
        amounts[event["event_id"]] = overlap
        proved = proved and proof
    return amounts, proved


def _last_financial_change(chains):
    changes = []
    for chain in chains:
        previous = None
        for row in chain:
            money = row[2:5]
            if money != previous:
                changes.append(row[1])
            previous = money
    return max(changes) if changes else None


def _receipt_covered(event, chains):
    return all(chain[-1][1] - event["received_at"] >= _RECEIPT_MARGIN_MS
               for chain in chains)


def _decision(ctx, req, ledger, reserves, comparable):
    """Evaluate each predicate across ALL events before moving to the next row."""
    events, policy = ctx["provider_events"], ctx["policy"]
    now = req["now_ms"]
    latest = _latest_provider(events)
    if latest is None:
        return "unknown", "billing_unknown", "alert", "provisional"
    if not _fresh(latest, policy, now):
        return "stale", "billing_stale", "alert", "provisional"
    conflict = ctx["provider_conflict"]
    chains = list(ctx["chains_by_gateway"].values())
    boundary = ctx["boundary_exposure_micro_eur"]
    allowance = policy["non_ai_allowance_micro_eur"]
    if boundary is not None:
        threshold = _sum([ledger, reserves, boundary, allowance, _tolerance(ledger, policy)])
        candidates = [latest] if conflict else events
        if any(_fresh(e, policy, now) and _receipt_covered(e, chains)
               and e["payload"]["total_micro_eur"] > threshold for e in candidates):
            return "unexplained_excess", "provider_excess", "hold", "comparable"
    if conflict:
        return "conflict", "source_conflict", "alert", "provisional"
    if boundary is None:
        return "temporally_unresolved", "snapshot_skew", "none", "provisional"
    by_id = {e["event_id"]: e for e in events}
    certified = []
    for entry in req["coverage"]:
        bracket = _bracket(chains, entry["upper_at"])
        if bracket is not None:
            certified.append((entry, by_id[entry["event_id"]]["payload"]["total_micro_eur"], *bracket))
    if any(p > _sum([u, boundary, allowance, _tolerance(l, policy)])
           for _, p, l, u in certified):
        return "unexplained_excess", "provider_excess", "hold", "comparable"
    quality = "comparable" if comparable else "provisional"
    for entry in req["coverage"]:
        if entry["upper_at"] is None:
            continue
        amount = by_id[entry["event_id"]]["payload"]["total_micro_eur"]
        old = _boundary_e(chains, entry["upper_at"] - policy["lag_seconds"] * 1000,
                          max_age_ms=None)
        if old is not None and amount < old - _tolerance(old, policy):
            return "provider_behind", "missing_old_spend", "alert", quality
    last_change = ctx["last_financial_change"]
    if (last_change is not None and now-last_change >= policy["lag_seconds"] * 1000
            and latest["payload"]["total_micro_eur"] < ledger - _tolerance(ledger, policy)):
        return "provider_behind", "lag_investigate", "alert", "provisional"
    if not certified:
        reason = "coverage_unknown" if req["coverage"] or _receipt_covered(latest, chains) else "snapshot_skew"
        return "temporally_unresolved", reason, "none", "provisional"
    if any(p > l + _tolerance(l, policy) for _, p, l, _ in certified):
        return "reserve_or_scope_covered", "reserve_or_scope_covered", "none", quality
    if comparable and all(entry["complete_at"] is not None and entry["matched_provenance"]
           and l == u and abs(p-l) <= _tolerance(l, policy)
           for entry, p, l, u in certified) and len(certified) == len(events):
        return "matched", "matched", "none", quality
    if any(p < l - _tolerance(l, policy) for _, p, l, _ in certified):
        return "provider_behind", "lag_expected", "none", quality
    return "temporally_unresolved", "coverage_unknown", "none", "provisional"


def evaluate_billing(context, request):
    """Pure advisory evaluation; no receipt, reconcile state or ledger writes."""
    ctx = _validate_context(context)
    req = _validate_request(request, ctx)
    # The store latches conflicts on source amounts. Basis conversion may
    # round distinct amounts alike, but it must never clear that latch.
    ctx["provider_conflict"] = _conflict_latched(ctx["provider_events"])
    # Preserve raw activity before conversion can round distinct amounts
    # alike. Epoch changes are already gated by current-chain continuity.
    ctx["last_financial_change"] = _last_financial_change(
        ctx["chains_by_gateway"].values())
    basis_ok = _normalize_money(ctx)
    if basis_ok:
        _validate_known_overlap_bounds(ctx, req)
    scope_ok = basis_ok and _scope_matches(ctx)
    usable = scope_ok and _gateway_usable(ctx, req["now_ms"])
    ledger = reserves = estimate = headroom = None
    admission = "unavailable"
    known = []
    if scope_ok:
        known.extend(e["payload"]["total_micro_eur"] for e in ctx["provider_events"])
    if usable:
        chains = list(ctx["chains_by_gateway"].values())
        ledger = _sum(chain[-1][2] for chain in chains)
        reserves = _sum(v for chain in chains for v in chain[-1][3:5])
        known.append(ledger)
        overlaps, proved = _overlaps(ctx["provider_events"], req["coverage"], chains, ledger)
        if ctx["provider_events"]:
            estimate = max([ledger] + [_checked(e["payload"]["total_micro_eur"] + ledger
                           - overlaps[e["event_id"]]) for e in ctx["provider_events"]])
        comparable = (proved and not ctx["provider_conflict"]
                      and all(_fresh(e, ctx["policy"], req["now_ms"]) for e in ctx["provider_events"]))
        state, reason, action, quality = _decision(ctx, req, ledger, reserves, comparable)
        if comparable:
            used = _sum([money.UNKNOWN if estimate is None else estimate, reserves])
            admission = "billing_estimate"
        else:
            used = _sum([ctx["policy"]["non_ai_allowance_micro_eur"], ledger, reserves])
            admission = "ledger_fallback"
        available = money.headroom(ctx["cap_micro_eur"], [used])      # UNKNOWN when any term was: never a manufactured room
        if available is not money.UNKNOWN and available >= 0:
            headroom = available
        else:
            admission = "unavailable"
    else:
        state, action, quality = "unknown", "alert", "unknown"
        reason = "basis_unknown" if not basis_ok else "scope_mismatch" if not scope_ok else "gateway_unusable"
    action, reason = apply_non_ai_overlay(action, reason, any(
        e["payload"]["non_ai_spend_present"] for e in ctx["provider_events"]))
    fits = False if action == "hold" else None if headroom is None else req["requested_micro_eur"] <= headroom
    return {"state": state, "action": action, "reason": reason, "quality": quality,
            "admission_basis": admission, "known_floor_micro_eur": max(known) if known else None,
            "estimate_micro_eur": estimate, "headroom_micro_eur": headroom,
            "request_fits": fits, "proposal": None}
