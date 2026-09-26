"""Pure opening planner (card 2b-2b).

plan_opening(context, request) -> Proposal. It reuses the 2b-2 context
validator, coverage validator, basis conversion, chain-usability and
freshness predicates (quota_balancer/billing_eval.py) and adds only the
opening-specific request shape and safety rules of docs/DESIGN.md,
section 11 ("Advisory headroom and opening plan", "Pure input surfaces").

Two outcomes for two kinds of bad input:
  * malformed or self-inconsistent input (bad shape or type, unknown or
    duplicated reference, wrong baseline reference, negative or overflowing
    amount, account or version mismatch) raises INVALID_CONFIG; and
  * well-shaped input whose evidence does not prove a safe opening returns
    the "unsafe" proposal: empty allocations, nothing allocatable.

Every validation and arithmetic check that can raise runs BEFORE any
safety verdict is reached, so an unsafe verdict never hides an invalid
input. No I/O, clock reads, randomness, input mutation or writes: the
proposal is a value, and nothing here applies it.
"""

import json
import uuid

from .billing_eval import (
    _checked,
    _conflict_latched,
    _fresh,
    _gateway_usable,
    _invalid,
    _latest_provider,
    _normalize_money,
    _overlaps,
    _scope_matches,
    _sum,
    _validate_context,
    _validate_coverage,
    _validate_known_overlap_bounds,
)
from .validate_readings import _is_alias, _is_bool, _is_nonneg_int, _is_uuid

# Closed opening request and its records (docs/DESIGN.md, section 11,
# "Pure input surfaces"). All fields required, no extra keys.
_OPEN_KEYS = frozenset({
    "version", "account", "now_ms", "drain_completed_at", "drain_verified",
    "billing_event_id", "coverage", "baselines", "allocations",
    "non_ai_allocation", "proven_external",
})
_BASELINE_KEYS = frozenset({"gateway", "snapshot_event_id", "baseline_micro_eur"})
_ALLOCATION_KEYS = frozenset({"gateway", "external_micro_eur", "evidence_ids"})
_NON_AI_KEYS = frozenset({"amount_micro_eur", "evidence_ids"})
_PROOF_KEYS = frozenset({"evidence_id", "gateway", "amount_micro_eur",
                         "disjoint_from_managed"})

# Fixed namespace of the deterministic proposal ID.
_ID_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # NAMESPACE_URL
_ID_PREFIX = "quota-card2b2b/opening/v1/"


def _closed(item, keys):
    if not isinstance(item, dict) or frozenset(item) != keys:
        _invalid()
    return item


def _list(value):
    if not isinstance(value, list):
        _invalid()
    return value


def _evidence_ids(value, proofs, consumed):
    """Validate one evidence_ids array; consume each proof exactly once."""
    ids = _list(value)
    for evidence_id in ids:
        if not _is_uuid(evidence_id) or evidence_id not in proofs:
            _invalid()                    # unknown reference
        if evidence_id in consumed:
            _invalid()                    # duplicate proof consumption
        consumed.add(evidence_id)
    return sorted(ids)


def _validate_baselines(items, ctx):
    """One baseline per configured gateway, equal to the tip of that
    gateway's current chain. Amounts are still in ledger units here."""
    by_id = {r["event_id"]: r for r in ctx["readings"]}
    baselines = {}
    for item in _list(items):
        _closed(item, _BASELINE_KEYS)
        gateway = item["gateway"]
        if (not _is_alias(gateway) or gateway not in ctx["chains_by_gateway"]
                or gateway in baselines):
            _invalid()
        ref = item["snapshot_event_id"]
        amount = item["baseline_micro_eur"]
        if not _is_uuid(ref) or not _is_nonneg_int(amount):
            _invalid()
        chain = ctx["chains_by_gateway"][gateway]
        row = next((r for r in chain if r[5] == ref), None)
        # A wrong reference: not a settled snapshot in this gateway's
        # current chain, not its latest sequence, or a different amount.
        if row is None or row[0] != max(r[0] for r in chain) or row[2] != amount:
            _invalid()
        baselines[gateway] = {"snapshot_event_id": ref, "baseline": amount,
                              "observed_at": by_id[ref]["observed_at"],
                              "payload": by_id[ref]["payload"]}
    if set(baselines) != set(ctx["chains_by_gateway"]):
        _invalid()
    return baselines


def _validate_open_request(request, ctx):
    """Validate the closed opening request; return a normalized copy in
    which every array is sorted, so no input order can matter."""
    _closed(request, _OPEN_KEYS)
    if type(request["version"]) is not int or request["version"] != 1:
        _invalid()
    if not _is_alias(request["account"]) or request["account"] != ctx["account"]:
        _invalid()
    now_ms = request["now_ms"]
    drain = request["drain_completed_at"]
    if not _is_nonneg_int(now_ms) or not _is_nonneg_int(drain) or drain > now_ms:
        _invalid()
    if not _is_bool(request["drain_verified"]):
        _invalid()
    billing_event_id = request["billing_event_id"]
    if (not _is_uuid(billing_event_id)
            or billing_event_id not in {e["event_id"] for e in ctx["provider_events"]}):
        _invalid()
    coverage = sorted(_validate_coverage(request["coverage"], ctx, now_ms),
                      key=lambda c: c["event_id"])
    baselines = _validate_baselines(request["baselines"], ctx)

    proofs = {}
    for item in _list(request["proven_external"]):
        _closed(item, _PROOF_KEYS)
        evidence_id, owner = item["evidence_id"], item["gateway"]
        if not _is_uuid(evidence_id) or evidence_id in proofs:
            _invalid()
        if owner is not None and (not _is_alias(owner)
                                  or owner not in ctx["chains_by_gateway"]):
            _invalid()
        if not _is_nonneg_int(item["amount_micro_eur"]) or not _is_bool(
                item["disjoint_from_managed"]):
            _invalid()
        proofs[evidence_id] = {"gateway": owner, "amount": item["amount_micro_eur"],
                               "disjoint": item["disjoint_from_managed"]}

    consumed = set()
    allocations = {}
    for item in _list(request["allocations"]):
        _closed(item, _ALLOCATION_KEYS)
        gateway = item["gateway"]
        if (not _is_alias(gateway) or gateway not in ctx["chains_by_gateway"]
                or gateway in allocations or not _is_nonneg_int(item["external_micro_eur"])):
            _invalid()
        allocations[gateway] = {
            "external": item["external_micro_eur"],
            "ids": _evidence_ids(item["evidence_ids"], proofs, consumed)}
    non_ai = _closed(request["non_ai_allocation"], _NON_AI_KEYS)
    if not _is_nonneg_int(non_ai["amount_micro_eur"]):
        _invalid()
    non_ai = {"amount": non_ai["amount_micro_eur"],
              "ids": _evidence_ids(non_ai["evidence_ids"], proofs, consumed)}
    # Missing gateway allocations mean zero.
    for gateway in ctx["chains_by_gateway"]:
        allocations.setdefault(gateway, {"external": 0, "ids": []})

    # Checked arithmetic (INVALID_CONFIG on int64 overflow) before any verdict.
    for entry in list(allocations.values()) + [non_ai]:
        entry["proof_sum"] = _sum(proofs[i]["amount"] for i in entry["ids"])
    _sum([a["external"] for a in allocations.values()] + [non_ai["amount"]])
    _sum(b["baseline"] for b in baselines.values())
    # Fix tk-dba39941b750 finding 3: the per-gateway opening sum is checked
    # here, unconditionally, so an overflowing baseline+external is
    # INVALID_CONFIG even when another safety predicate would already make
    # the plan unsafe (it must never reach the ready branch unmarked).
    for gateway in ctx["chains_by_gateway"]:
        _checked(baselines[gateway]["baseline"] + allocations[gateway]["external"])

    return {
        "now_ms": now_ms, "drain_completed_at": drain,
        "drain_verified": request["drain_verified"],
        "billing_event_id": billing_event_id, "coverage": coverage,
        "baselines": baselines, "allocations": allocations,
        "non_ai": non_ai, "proofs": proofs, "consumed": consumed,
    }


def _proposal_id(ctx, req, provider_total, state):
    """Deterministic ID over the canonical numeric inputs and references.
    The injected clock (now_ms) is not part of the identity, and every
    array is sorted, so call order and key order cannot change it."""
    canonical = {
        "account": ctx["account"], "period_start": ctx["period_start"],
        "drain_completed_at": req["drain_completed_at"],
        "drain_verified": req["drain_verified"],
        "billing_event_id": req["billing_event_id"],
        "provider_total_micro_eur": provider_total,
        "coverage": req["coverage"],
        "baselines": [[g, b["snapshot_event_id"], b["baseline"]]
                      for g, b in sorted(req["baselines"].items())],
        "allocations": [[g, a["external"], a["ids"]]
                        for g, a in sorted(req["allocations"].items())],
        "non_ai": [req["non_ai"]["amount"], req["non_ai"]["ids"]],
        "proven_external": [[i, p["gateway"], p["amount"], p["disjoint"]]
                            for i, p in sorted(req["proofs"].items())],
        "state": state,
    }
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return str(uuid.uuid5(_ID_NAMESPACE, _ID_PREFIX + text))


def _catch_up_proved(ctx, req, event, now_ms):
    """lastUpdate >= drain_completed + lag_seconds PLUS a verified
    complete_at >= drain_completed. No expiry or elapsed wait proves it."""
    policy = ctx["policy"]
    if event["observed_at"] < req["drain_completed_at"] + policy["lag_seconds"] * 1000:
        return False
    entry = next((c for c in req["coverage"] if c["event_id"] == event["event_id"]), None)
    return bool(entry and entry["matched_provenance"] and entry["complete_at"] is not None
                and entry["complete_at"] >= req["drain_completed_at"])


def _ledger_quiet(ctx, req):
    """No reserved/uncertain attempt, no unresolved transition, no pending
    correction and no unattributed settled spend behind any baseline."""
    if ctx["unresolved_event_ids"]:
        return False
    return all(b["payload"]["reserved_micro_eur"] == 0
               and b["payload"]["uncertain_micro_eur"] == 0
               and b["payload"]["unattributed_micro_eur"] == 0
               and not b["payload"]["reconciliation_required"]
               for b in req["baselines"].values())


def _baselines_cover_drain(req):
    """Each selected baseline snapshot must be observed at or after drain
    completion (fix tk-dba39941b750 finding 1): a report from before the
    drain describes the pre-drain ledger and cannot attest its post-drain,
    quiet state, however fresh it still is relative to `now`. The design
    (section 11) requires "fresh matching reports" without stating this
    inequality; this is this build's fail-closed reading of it (recorded
    as a decision in docs/STEP-CARD-2B-2B.md, for the design owner)."""
    drain = req["drain_completed_at"]
    return all(b["observed_at"] >= drain for b in req["baselines"].values())


def _allocations_exact(ctx, req, provider_total):
    """The independently proven, disjoint, owned external charges cover
    exactly max(0, P-L); non-AI stays in the allowance bucket."""
    baselines, allocations = req["baselines"], req["allocations"]
    proofs = req["proofs"]
    ledger = _sum(b["baseline"] for b in baselines.values())
    gap = max(0, provider_total - ledger)          # P-L is not itself evidence
    shares = {g["gateway"]: g["share_micro_eur"] for g in ctx["gateways"]}
    if set(proofs) != req["consumed"]:
        return False                               # every proof consumed once
    if not all(p["disjoint"] for p in proofs.values()):
        return False
    for gateway, allocation in allocations.items():
        if allocation["external"] != allocation["proof_sum"]:
            return False
        if any(proofs[i]["gateway"] != gateway for i in allocation["ids"]):
            return False                           # charge only the owner
        if baselines[gateway]["baseline"] + allocation["external"] > shares[gateway]:
            return False                           # opening within its share
    non_ai = req["non_ai"]
    if (non_ai["amount"] != non_ai["proof_sum"]
            or any(proofs[i]["gateway"] is not None for i in non_ai["ids"])
            or non_ai["amount"] > ctx["policy"]["non_ai_allowance_micro_eur"]):
        return False
    total = _sum([a["external"] for a in allocations.values()] + [non_ai["amount"]])
    return total == gap


def _opening_safe(ctx, req):
    """True only when every required proof is present. Raising checks run
    first (into `checks`), so an unsafe verdict never masks INVALID_CONFIG."""
    now_ms = req["now_ms"]
    policy = ctx["policy"]
    events = ctx["provider_events"]
    provider = next(e for e in events if e["event_id"] == req["billing_event_id"])
    ctx["provider_conflict"] = _conflict_latched(events)
    basis_ok = _normalize_money(ctx)
    checks = [req["drain_verified"], basis_ok]
    provider_total = provider["payload"]["total_micro_eur"]
    if basis_ok:
        _validate_known_overlap_bounds(ctx, req)
        scope_ok = _scope_matches(ctx)
        # One basis for baselines, external charges and openings: a mixed
        # basis is not silently converted (design is silent), it is unsafe.
        single = len({policy["cap_basis"], policy["provider_basis"],
                      policy["ledger_basis"]}) == 1
        # Continuity (a well-formed chain) is checked without the freshness
        # bracket, so an explicit overlap floor with no usable B_e is still
        # INVALID_CONFIG even when the ledger report is stale (fix
        # tk-dba39941b750 finding 2); freshness alone gates readiness,
        # via `usable` below.
        continuous = scope_ok and single and _gateway_usable(
            ctx, now_ms, require_fresh=False)
        usable = continuous and _gateway_usable(ctx, now_ms)
        checks += [scope_ok, single, usable]
        if continuous:
            chains = list(ctx["chains_by_gateway"].values())
            ledger = _sum(chain[-1][2] for chain in chains)
            _overlaps(events, req["coverage"], chains, ledger)   # invalid explicit floor
    checks += [
        _baselines_cover_drain(req),
        _ledger_quiet(ctx, req),
        _latest_provider(events)["event_id"] == provider["event_id"],
        not ctx["provider_conflict"],
        _fresh(provider, policy, now_ms),
        _catch_up_proved(ctx, req, provider, now_ms),
    ]
    if all(checks):
        checks.append(_allocations_exact(ctx, req, provider_total))
    return all(checks), provider_total


def plan_opening(context, request):
    """Pure opening proposal; never drains, applies or writes anything."""
    ctx = _validate_context(context)
    req = _validate_open_request(request, ctx)
    safe, provider_total = _opening_safe(ctx, req)
    state = "ready" if safe else "unsafe"
    proposal_id = _proposal_id(ctx, req, provider_total, state)
    if not safe:
        return {"proposal_id": proposal_id, "state": "unsafe",
                "reason": "opening_unsafe", "source_events": [],
                "allocations": [], "non_ai_allocation_micro_eur": 0}
    allocations = []
    for gateway in sorted(req["baselines"]):
        baseline = req["baselines"][gateway]["baseline"]
        external = req["allocations"][gateway]["external"]
        allocations.append({
            "gateway": gateway, "baseline_micro_eur": baseline,
            "external_micro_eur": external,
            "opening_micro_eur": _checked(baseline + external)})
    sources = {req["billing_event_id"]}
    sources |= {b["snapshot_event_id"] for b in req["baselines"].values()}
    sources |= {c["event_id"] for c in req["coverage"]}
    return {"proposal_id": proposal_id, "state": "ready", "reason": "verified",
            "source_events": sorted(sources), "allocations": allocations,
            "non_ai_allocation_micro_eur": req["non_ai"]["amount"]}
