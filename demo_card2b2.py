"""Card 2b-2 demo (docs/DESIGN.md, section 8, the 2b-2 row).

Runs the two demo outcomes of the card row over the pure evaluator:

  1. an uncertified 30 EUR excess -> a HOLD recommendation, and
  2. the normal P=45 / L=50 case -> the fallback headroom of 50.

Run from the repository root:
    python3 demo_card2b2.py

Fixtures only: no store, no network, no clock reads (now_ms is
injected). The money unit is micro-euro (EUR * 1_000_000).
"""

import uuid

from quota_balancer.billing_eval import evaluate_billing

_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # NAMESPACE_URL
SEP = 1_788_220_800_000  # 2026-09-01T00:00:00Z, ms
OCT = 1_790_812_800_000  # 2026-10-01T00:00:00Z, ms (September: 30 days)


def _eid(label):
    return str(uuid.uuid5(_NS, "quota-card2b2/demo/" + label))


def _provider(total, observed):
    return {
        "event_id": _eid("prov"), "machine": "desktop", "account": "meter",
        "kind": "provider_billing", "observed_at": observed,
        "received_at": observed + 1000,
        "payload": {"scope_alias": "proj-ai", "scope_revision": 1,
                    "period_start": SEP, "period_end": OCT,
                    "currency": "EUR", "total_micro_eur": total,
                    "non_ai_spend_present": False},
    }


def _snapshot(settled, observed, event):
    return {
        "event_id": event, "machine": "desktop", "account": "meter",
        "kind": "gateway_snapshot", "observed_at": observed,
        "received_at": observed + 1000,
        "payload": {"gateway": "g1", "ledger_epoch": 1, "seq": 1,
                    "period_start": SEP, "period_end": OCT,
                    "policy_hashes": None, "sized_ready": False,
                    "reconciliation_required": False,
                    "settled_micro_eur": settled,
                    "reserved_micro_eur": 0, "uncertain_micro_eur": 0,
                    "unattributed_micro_eur": 0},
    }


_POLICY = {
    "account": "meter", "reporter_machine": "desktop",
    "scope_alias": "proj-ai", "scope_revision": 1, "dedicated_ai": True,
    "non_ai_allowance_micro_eur": 0, "cap_basis": "ex_vat",
    "provider_basis": "ex_vat", "ledger_basis": "ex_vat", "vat_bps": None,
    "poll_seconds": 900, "max_age_seconds": 7200, "lag_seconds": 14400,
    "abs_tolerance_micro_eur": 50_000, "relative_tolerance_bps": 100,
    "mode": "advisory", "stale_action": "alert",
    "divergence_action": "hold",
}


def _context(readings):
    return {
        "account": "meter",
        "period_start": SEP, "period_end": OCT,
        "cap_micro_eur": 100_000_000,
        "gateways": [{"gateway": "g1", "share_micro_eur": 60_000_000,
                      "current_chain_event_ids": [
                          r["event_id"] for r in readings
                          if r["kind"] == "gateway_snapshot"]}],
        "policy": dict(_POLICY),
        "readings": readings,
        "unresolved_event_ids": [],
        "boundary_exposure_micro_eur": 0,
        "boundary_event_ids": [readings[1]["event_id"]],
    }


def _request(now_ms, requested=0):
    return {"version": 1, "account": "meter", "now_ms": now_ms,
            "requested_micro_eur": requested, "coverage": []}


def _print(title, out):
    print(title)
    for key in ("state", "action", "reason", "quality", "admission_basis",
                "known_floor_micro_eur", "estimate_micro_eur",
                "headroom_micro_eur", "request_fits"):
        print("  %-22s %s" % (key + ":", out[key]))
    print()


def main():
    # 1. The uncertified 30 EUR excess: P=80 EUR against the settled
    #    ledger L=50 (R=B=allowance=0, tau(50M)=500000) - 30 EUR beyond
    #    L, fresh and uncertified: a HOLD recommendation, comparable.
    #    No request is admitted on a hold (request_fits false).
    obs = SEP + 2 * 86_400_000
    out = evaluate_billing(
        _context([
            _provider(80_000_000, obs),
            _snapshot(20_000_000, obs - 30_000, _eid("a")),
            _snapshot(50_000_000, obs + 40_000, _eid("l")),
        ]),
        _request(obs + 50_000, requested=10_000_000))
    _print("1) uncertified 30 EUR excess (P=80, L=50) => HOLD:", out)
    assert out["action"] == "hold", "expected the HOLD recommendation"
    assert out["request_fits"] is False, "a hold never admits"

    # 2. The normal P=45 / L=50: P below the settled ledger, no
    #    overlap proof -> the provisional fallback headroom of 50
    #    (cap 100 - allowance 0 - L 50 - R 0); the diagnostic E=95 is
    #    not enforced in the fallback.
    out = evaluate_billing(
        _context([
            _provider(45_000_000, obs),
            _snapshot(20_000_000, obs - 30_000, _eid("a2")),
            _snapshot(50_000_000, obs + 40_000, _eid("l2")),
        ]),
        _request(obs + 50_000))
    _print("2) normal P=45 / L=50 => fallback headroom 50:", out)
    assert out["admission_basis"] == "ledger_fallback"
    assert out["headroom_micro_eur"] == 50_000_000
    assert out["action"] == "none"

    print("demo ok: both card outcomes hold.")


if __name__ == "__main__":
    main()
