"""Card 2b-2b demo (docs/DESIGN.md, section 8, the 2b-2b row).

Runs the two demo outcomes of the card row over the pure planner:

  1. the design's 18.43 / 19 / 0.57 case with the 0.57 still uncertain in
     the ledger -> an unsafe proposal, no allocation; and
  2. the same case once drain, provenance and the external 0.57 charge are
     verified -> a ready proposal whose ID is stable across calls.

Run from the repository root:
    python3 demo_card2b2b.py

Fixtures only: no store, no network, no clock reads (now_ms is injected).
The money unit is micro-euro (EUR * 1_000_000).
"""

import copy

from demo_card2b2 import SEP, _POLICY, _eid, _provider, _snapshot
from quota_balancer.billing_open import plan_opening

DRAIN = SEP + 2 * 86_400_000                 # verified drain completion
LAG = _POLICY["lag_seconds"] * 1000
LAST_UPDATE = DRAIN + LAG + 3_600_000        # provider lastUpdate
NOW = LAST_UPDATE + 120_000


def _fixture(uncertain):
    prov = _provider(19_000_000, LAST_UPDATE)
    snap = _snapshot(18_430_000, NOW - 30_000, _eid("snap"))
    snap["payload"]["uncertain_micro_eur"] = uncertain
    context = {
        "account": "meter", "period_start": SEP,
        "period_end": prov["payload"]["period_end"],
        "cap_micro_eur": 100_000_000,
        "gateways": [{"gateway": "g1", "share_micro_eur": 60_000_000,
                      "current_chain_event_ids": [snap["event_id"]]}],
        "policy": dict(_POLICY), "readings": [prov, snap],
        "unresolved_event_ids": [], "boundary_exposure_micro_eur": 0,
        "boundary_event_ids": [],
    }
    proof = _eid("external-0.57")
    request = {
        "version": 1, "account": "meter", "now_ms": NOW,
        "drain_completed_at": DRAIN, "drain_verified": True,
        "billing_event_id": prov["event_id"],
        "coverage": [{"event_id": prov["event_id"], "upper_at": LAST_UPDATE,
                      "complete_at": LAST_UPDATE, "overlap_floor_micro_eur": None,
                      "matched_provenance": True}],
        "baselines": [{"gateway": "g1", "snapshot_event_id": snap["event_id"],
                       "baseline_micro_eur": 18_430_000}],
        "allocations": [{"gateway": "g1", "external_micro_eur": 570_000,
                         "evidence_ids": [proof]}],
        "non_ai_allocation": {"amount_micro_eur": 0, "evidence_ids": []},
        "proven_external": [{"evidence_id": proof, "gateway": "g1",
                             "amount_micro_eur": 570_000,
                             "disjoint_from_managed": True}],
    }
    return context, request


def _show(title, proposal):
    print(title)
    for key in ("state", "reason", "allocations", "non_ai_allocation_micro_eur"):
        print("  %-30s %s" % (key + ":", proposal[key]))
    print()


def main():
    context, request = _fixture(uncertain=570_000)
    out = plan_opening(context, request)
    _show("1) 0.57 still uncertain in the ledger => unsafe, no allocation:", out)
    assert out["state"] == "unsafe" and out["allocations"] == []

    context, request = _fixture(uncertain=0)
    first = plan_opening(copy.deepcopy(context), copy.deepcopy(request))
    second = plan_opening(context, request)
    _show("2) drain, provenance and the 0.57 external charge verified:", first)
    assert first["state"] == "ready"
    assert first["allocations"][0]["opening_micro_eur"] == 19_000_000
    assert first == second, "a repeated call must return the same proposal"
    print("stable proposal_id:", first["proposal_id"])
    print("demo ok")


if __name__ == "__main__":
    main()
