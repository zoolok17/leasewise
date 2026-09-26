"""Card 2b-2b acceptance tests: the plan_opening(context, request) planner.

Run from the repository root:
    python -m unittest discover -s tests -v

plan_opening is a PURE function: no file reads or writes, no clock, no
network, no CLI verb. These tests pin the acceptance rows of
docs/DESIGN.md section 11 owned by 2b-2b:

  billing_opening_input             closed request, wrong references,
                                    duplicate proof consumption, account
                                    mismatch => INVALID_CONFIG; well-shaped
                                    insufficient proof => unsafe, no I/O
  billing_opening_uncertain_overlap reserved/uncertain/unresolved/non-
                                    disjoint evidence => unsafe
  billing_opening_catchup           18.43 / 19 / 0.57 and an early drain
  billing_opening_allocations       exact, owned, disjoint, in-share
  billing_proposal_only             negative => INVALID; valid plans keep
                                    baselines, stable IDs, one non-AI bucket
  billing_output_errors             closed output shape and exit mappings
  privacy_2b                        no sentinel leaks; zero side effects

Time is injected; all amounts are micro-EUR (EUR * 1_000_000).
"""

import ast
import builtins
import copy
import itertools
import json
import os
import pathlib
import random
import socket
import tempfile
import time
import unittest
from unittest import mock

from quota_balancer import billing_open
from quota_balancer.billing_open import plan_opening
from quota_balancer.errors import (
    CODE_INVALID_CONFIG, CODE_OPENING_UNSAFE, EXIT_CODES, QuotaError,
)
from test_card2b2 import SEP, _POLICY, _context, _eid, _provider, _snapshot

E = 1_000_000
INT64 = 2 ** 63 - 1
LAG_MS = _POLICY["lag_seconds"] * 1000
D0 = SEP + 2 * 86_400_000              # verified drain completion
PT = D0 + LAG_MS + 3_600_000           # provider lastUpdate (after drain+lag)
NOW = PT + 120_000                     # injected time
SNAP_AT = NOW - 30_000                 # ledger reports: fresh, after drain

PROPOSAL_KEYS = {"proposal_id", "state", "reason", "source_events",
                 "allocations", "non_ai_allocation_micro_eur"}
ALLOCATION_KEYS = {"gateway", "baseline_micro_eur", "external_micro_eur",
                   "opening_micro_eur"}
PROV = _eid("open-provider")
COVER = dict(event_id=PROV, upper_at=PT, complete_at=PT, overlap_floor_micro_eur=None,
             matched_provenance=True)


def snap_id(gateway):
    return _eid("open-snap-" + gateway)


def proof_id(label):
    return _eid("open-proof-" + label)


def proof(label, gateway, amount, disjoint=True):
    return dict(evidence_id=proof_id(label), gateway=gateway,
                amount_micro_eur=amount, disjoint_from_managed=disjoint)


def world(ledgers=None, total=19 * E, external=None, non_ai=None,
          allowance=0, shares=None, **snapshot_over):
    """(context, request) for a well-formed opening.

    ledgers   {gateway: settled}; external {gateway: [amounts]};
    non_ai    [amounts]. Every amount becomes its own proof, consumed once
    by its owner. Defaults are the design's 18.43 / 19 / 0.57 case.
    """
    ledgers = {"g1": 18_430_000} if ledgers is None else ledgers
    external = {"g1": [570_000]} if external is None else external
    non_ai = [] if non_ai is None else non_ai
    shares = shares or {g: 40 * E for g in ledgers}
    readings = [_provider(total, False, PT, event=PROV)]
    for gateway, settled in ledgers.items():
        readings.append(_snapshot(settled, SNAP_AT, snap_id(gateway),
                                  gateway=gateway, **snapshot_over))
    policy = dict(_POLICY, non_ai_allowance_micro_eur=allowance)
    ctx = _context(
        readings=readings, policy=policy, boundary_exposure_micro_eur=0,
        gateways=[dict(gateway=g, share_micro_eur=shares[g],
                       current_chain_event_ids=[snap_id(g)]) for g in ledgers])
    proven, allocations = [], []
    for gateway, amounts in external.items():
        ids = []
        for i, amount in enumerate(amounts):
            p = proof(f"{gateway}-{i}", gateway, amount)
            proven.append(p)
            ids.append(p["evidence_id"])
        allocations.append(dict(gateway=gateway, external_micro_eur=sum(amounts),
                                evidence_ids=ids))
    non_ai_ids = []
    for i, amount in enumerate(non_ai):
        p = proof(f"nonai-{i}", None, amount)
        proven.append(p)
        non_ai_ids.append(p["evidence_id"])
    req = dict(
        version=1, account="meter", now_ms=NOW, drain_completed_at=D0,
        drain_verified=True, billing_event_id=PROV, coverage=[dict(COVER)],
        baselines=[dict(gateway=g, snapshot_event_id=snap_id(g), baseline_micro_eur=s)
                   for g, s in ledgers.items()],
        allocations=allocations,
        non_ai_allocation=dict(amount_micro_eur=sum(non_ai), evidence_ids=non_ai_ids),
        proven_external=proven)
    return ctx, req


def reading(ctx, event_id):
    return next(r for r in ctx["readings"] if r["event_id"] == event_id)


class Base(unittest.TestCase):
    def invalid(self, ctx, req):
        with self.assertRaises(QuotaError) as caught:
            plan_opening(ctx, req)
        self.assertEqual(caught.exception.code, CODE_INVALID_CONFIG)
        return caught.exception

    def unsafe(self, ctx, req):
        out = plan_opening(ctx, req)
        self.assertEqual(set(out), PROPOSAL_KEYS)
        self.assertEqual((out["state"], out["reason"]), ("unsafe", "opening_unsafe"))
        self.assertEqual(out["allocations"], [])
        self.assertEqual(out["non_ai_allocation_micro_eur"], 0)
        return out

    def ready(self, ctx, req):
        out = plan_opening(ctx, req)
        self.assertEqual(set(out), PROPOSAL_KEYS)
        self.assertEqual((out["state"], out["reason"]), ("ready", "verified"))
        return out


# ---------------------------------------------------------------------------
# billing_opening_input
# ---------------------------------------------------------------------------

class BillingOpeningInput(Base):
    def test_design_case_ready(self):
        out = self.ready(*world())
        self.assertEqual(out["allocations"], [dict(
            gateway="g1", baseline_micro_eur=18_430_000,
            external_micro_eur=570_000, opening_micro_eur=19 * E)])
        self.assertEqual(out["non_ai_allocation_micro_eur"], 0)

    def test_extra_and_missing_request_keys(self):
        ctx, req = world()
        extra = dict(req, surprise=1)
        self.invalid(ctx, extra)
        for key in list(req):
            missing = {k: v for k, v in req.items() if k != key}
            self.invalid(ctx, missing)

    def test_extra_and_missing_nested_keys(self):
        for path in (("baselines", 0), ("allocations", 0),
                     ("proven_external", 0), ("coverage", 0)):
            ctx, req = world()
            item = req[path[0]][path[1]]
            for key in list(item):
                bad = copy.deepcopy(req)
                del bad[path[0]][path[1]][key]
                self.invalid(ctx, bad)
            bad = copy.deepcopy(req)
            bad[path[0]][path[1]]["surprise"] = 1
            self.invalid(ctx, bad)
        ctx, req = world()
        for key in list(req["non_ai_allocation"]):
            bad = copy.deepcopy(req)
            del bad["non_ai_allocation"][key]
            self.invalid(ctx, bad)
        bad = copy.deepcopy(req)
        bad["non_ai_allocation"]["surprise"] = 1
        self.invalid(ctx, bad)

    def test_non_dict_and_bad_container_types(self):
        ctx, req = world()
        self.invalid(ctx, [])
        self.invalid([], req)
        for key in ("baselines", "allocations", "proven_external", "coverage"):
            self.invalid(ctx, dict(req, **{key: None}))
            self.invalid(ctx, dict(req, **{key: {}}))
        self.invalid(ctx, dict(req, non_ai_allocation=[]))

    def test_account_mismatch(self):
        ctx, req = world()
        self.invalid(ctx, dict(req, account="other"))
        self.invalid(ctx, dict(req, account=None))

    def test_unsupported_version(self):
        ctx, req = world()
        for version in (0, 2, True, 1.0, "1", None):
            self.invalid(ctx, dict(req, version=version))

    def test_wrong_baseline_references(self):
        ctx, req = world(ledgers={"g1": 18_430_000, "g2": 5 * E},
                         external={"g1": [570_000]})
        cases = {}
        cases["unknown snapshot"] = lambda r: r["baselines"][0].update(
            snapshot_event_id=_eid("open-ghost"))
        cases["other gateway's snapshot"] = lambda r: r["baselines"][0].update(
            snapshot_event_id=snap_id("g2"))
        cases["provider event as snapshot"] = lambda r: r["baselines"][0].update(
            snapshot_event_id=PROV)
        cases["baseline differs from snapshot"] = lambda r: r["baselines"][0].update(
            baseline_micro_eur=18_430_001)
        cases["bad uuid"] = lambda r: r["baselines"][0].update(
            snapshot_event_id="not-a-uuid")
        cases["missing gateway baseline"] = lambda r: r["baselines"].pop()
        cases["duplicate gateway baseline"] = lambda r: r["baselines"].append(
            dict(r["baselines"][0]))
        cases["unconfigured gateway"] = lambda r: r["baselines"].append(
            dict(gateway="g9", snapshot_event_id=snap_id("g1"), baseline_micro_eur=0))
        cases["empty baselines"] = lambda r: r["baselines"].clear()
        for name, mutate in cases.items():
            with self.subTest(name):
                bad = copy.deepcopy(req)
                mutate(bad)
                self.invalid(ctx, bad)

    def test_baseline_must_be_the_chain_tip(self):
        ctx, req = world()
        older = _snapshot(18_000_000, SNAP_AT - 60_000, _eid("open-older"), seq=0)
        ctx["readings"].append(older)
        ctx["gateways"][0]["current_chain_event_ids"].append(older["event_id"])
        self.ready(ctx, req)                       # tip still referenced
        bad = copy.deepcopy(req)
        bad["baselines"][0].update(snapshot_event_id=older["event_id"],
                                   baseline_micro_eur=18_000_000)
        self.invalid(ctx, bad)                     # superseded snapshot

    def test_snapshot_outside_current_chain_is_wrong_reference(self):
        ctx, req = world()
        ctx["gateways"][0]["current_chain_event_ids"] = []
        self.invalid(ctx, req)

    def test_duplicate_proof_consumption(self):
        ctx, req = world(external={"g1": [300_000, 270_000]})
        ids = req["allocations"][0]["evidence_ids"]
        bad = copy.deepcopy(req)
        bad["allocations"][0]["evidence_ids"] = [ids[0], ids[0]]
        self.invalid(ctx, bad)                                  # twice in one list
        ctx, req = world(ledgers={"g1": 18_430_000, "g2": 0},
                         external={"g1": [570_000]})
        bad = copy.deepcopy(req)
        bad["allocations"].append(dict(
            gateway="g2", external_micro_eur=570_000,
            evidence_ids=list(req["allocations"][0]["evidence_ids"])))
        self.invalid(ctx, bad)                                  # two owners
        bad = copy.deepcopy(req)
        bad["non_ai_allocation"] = dict(
            amount_micro_eur=570_000,
            evidence_ids=list(req["allocations"][0]["evidence_ids"]))
        self.invalid(ctx, bad)                                  # gateway and non-AI

    def test_duplicate_proof_ids_and_gateway_allocations(self):
        ctx, req = world()
        bad = copy.deepcopy(req)
        bad["proven_external"].append(dict(bad["proven_external"][0]))
        self.invalid(ctx, bad)
        bad = copy.deepcopy(req)
        bad["allocations"].append(copy.deepcopy(bad["allocations"][0]))
        self.invalid(ctx, bad)

    def test_unknown_references(self):
        ctx, req = world()
        bad = copy.deepcopy(req)
        bad["allocations"][0]["evidence_ids"] = [_eid("open-ghost-proof")]
        self.invalid(ctx, bad)
        bad = copy.deepcopy(req)
        bad["allocations"][0]["gateway"] = "g9"
        self.invalid(ctx, bad)
        bad = copy.deepcopy(req)
        bad["proven_external"][0]["gateway"] = "g9"
        self.invalid(ctx, bad)
        self.invalid(ctx, dict(req, billing_event_id=_eid("open-ghost")))
        self.invalid(ctx, dict(req, billing_event_id=snap_id("g1")))
        bad = copy.deepcopy(req)
        bad["coverage"][0]["event_id"] = _eid("open-ghost")
        self.invalid(ctx, bad)

    def test_bad_ordering(self):
        ctx, req = world()
        self.invalid(ctx, dict(req, drain_completed_at=NOW + 1))
        bad = copy.deepcopy(req)
        bad["coverage"][0].update(upper_at=PT, complete_at=PT + 1)
        self.invalid(ctx, bad)                                  # complete after upper
        bad = copy.deepcopy(req)
        bad["coverage"][0].update(upper_at=NOW + 1, complete_at=NOW + 1)
        self.invalid(ctx, bad)                                  # after now
        bad = copy.deepcopy(req)
        bad["coverage"][0].update(upper_at=None)
        self.invalid(ctx, bad)                                  # complete without upper

    def test_invalid_explicit_overlap(self):
        ctx, req = world()
        bad = copy.deepcopy(req)
        bad["coverage"][0]["overlap_floor_micro_eur"] = 19 * E + 1   # > P
        self.invalid(ctx, bad)
        bad = copy.deepcopy(req)
        bad["coverage"][0].update(overlap_floor_micro_eur=1, matched_provenance=False)
        self.invalid(ctx, bad)

    def test_explicit_floor_needs_a_usable_bracket_bound(self):
        # matched_provenance and complete_at are present, but no snapshot
        # lies within 120 s at/before complete_at: no B_e, so the explicit
        # floor cannot be honoured (never clipped).
        ctx, req = world()
        bad = copy.deepcopy(req)
        bad["coverage"][0]["overlap_floor_micro_eur"] = 1
        self.invalid(ctx, bad)

    def test_explicit_floor_beyond_known_bounds_is_invalid_even_when_stale(self):
        ctx, req = world()
        snap = reading(ctx, snap_id("g1"))
        snap["observed_at"] = PT - 60_000              # continuous but not fresh
        snap["received_at"] = snap["observed_at"] + 1000
        ok = copy.deepcopy(req)
        ok["coverage"][0]["overlap_floor_micro_eur"] = 18_430_000
        self.unsafe(ctx, ok)                           # within bounds: stale => unsafe
        bad = copy.deepcopy(req)
        bad["coverage"][0]["overlap_floor_micro_eur"] = 18_430_001   # > L_now
        self.invalid(ctx, bad)

    def test_bool_is_not_int(self):
        ctx, req = world()
        for key in ("now_ms", "drain_completed_at"):
            self.invalid(ctx, dict(req, **{key: True}))
        for path, key in ((("baselines", 0), "baseline_micro_eur"),
                          (("allocations", 0), "external_micro_eur"),
                          (("proven_external", 0), "amount_micro_eur")):
            bad = copy.deepcopy(req)
            bad[path[0]][path[1]][key] = True
            self.invalid(ctx, bad)
        bad = copy.deepcopy(req)
        bad["non_ai_allocation"]["amount_micro_eur"] = False
        self.invalid(ctx, bad)

    def test_flags_must_be_exact_booleans(self):
        ctx, req = world()
        for value in (1, 0, "true", None, 1.0):
            self.invalid(ctx, dict(req, drain_verified=value))
            bad = copy.deepcopy(req)
            bad["proven_external"][0]["disjoint_from_managed"] = value
            self.invalid(ctx, bad)
            bad = copy.deepcopy(req)
            bad["coverage"][0]["matched_provenance"] = value
            self.invalid(ctx, bad)

    def test_floats_rejected(self):
        ctx, req = world()
        for key in ("now_ms", "drain_completed_at"):
            self.invalid(ctx, dict(req, **{key: float(req[key])}))
        for path, key in ((("baselines", 0), "baseline_micro_eur"),
                          (("allocations", 0), "external_micro_eur"),
                          (("proven_external", 0), "amount_micro_eur")):
            bad = copy.deepcopy(req)
            bad[path[0]][path[1]][key] = float(bad[path[0]][path[1]][key])
            self.invalid(ctx, bad)
        bad = copy.deepcopy(req)
        bad["non_ai_allocation"]["amount_micro_eur"] = 0.0
        self.invalid(ctx, bad)
        bad = copy.deepcopy(req)
        bad["coverage"][0]["complete_at"] = float(PT)
        self.invalid(ctx, bad)

    def test_int64_bounds(self):
        ctx, req = world()
        for key in ("now_ms", "drain_completed_at"):
            self.invalid(ctx, dict(req, **{key: INT64 + 1}))
            self.invalid(ctx, dict(req, **{key: -1}))
        for path, key in ((("baselines", 0), "baseline_micro_eur"),
                          (("allocations", 0), "external_micro_eur"),
                          (("proven_external", 0), "amount_micro_eur")):
            bad = copy.deepcopy(req)
            bad[path[0]][path[1]][key] = INT64 + 1
            self.invalid(ctx, bad)
        bad = copy.deepcopy(req)
        bad["non_ai_allocation"]["amount_micro_eur"] = INT64 + 1
        self.invalid(ctx, bad)

    def test_int64_sum_overflow_is_invalid_config(self):
        ctx, req = world(external={"g1": [INT64, INT64]})
        req["proven_external"][0]["amount_micro_eur"] = INT64
        req["allocations"][0]["external_micro_eur"] = INT64
        self.invalid(ctx, req)

    def test_bad_context_is_invalid_config(self):
        ctx, req = world()
        bad = copy.deepcopy(ctx)
        bad["surprise"] = 1
        self.invalid(bad, req)
        self.invalid(None, req)

    def test_well_shaped_insufficient_proof_is_unsafe_without_io(self):
        ctx, req = world(external={"g1": []})       # D = 0.57 with no proof
        req["allocations"] = []
        with mock.patch.object(builtins, "open", side_effect=AssertionError("io")), \
                mock.patch.object(time, "time", side_effect=AssertionError("clock")), \
                mock.patch.object(socket, "socket", side_effect=AssertionError("net")):
            out = self.unsafe(ctx, req)
        self.assertEqual(out["source_events"], [])


# ---------------------------------------------------------------------------
# billing_opening_uncertain_overlap
# ---------------------------------------------------------------------------

class BillingOpeningUncertainOverlap(Base):
    def test_reserved_attempt_refuses(self):
        self.unsafe(*world(reserved=1))

    def test_uncertain_attempt_refuses(self):
        self.unsafe(*world(uncertain=1))

    def test_uncertain_overlap_of_the_designs_057(self):
        # 0.57 is still an uncertain attempt in the ledger: P=19 would count it
        # twice if it also settled.
        self.unsafe(*world(uncertain=570_000))

    def test_unresolved_transition_refuses(self):
        ctx, req = world()
        ctx["unresolved_event_ids"] = [snap_id("g1")]
        self.unsafe(ctx, req)

    def test_any_unresolved_transition_refuses(self):
        ctx, req = world()
        ctx["unresolved_event_ids"] = [PROV]       # not a gateway snapshot
        self.unsafe(ctx, req)

    def test_reconciliation_required_refuses(self):
        ctx, req = world()
        reading(ctx, snap_id("g1"))["payload"]["reconciliation_required"] = True
        self.unsafe(ctx, req)

    def test_unattributed_settled_spend_has_unknown_provenance(self):
        self.unsafe(*world(unattributed=1))

    def test_proof_not_disjoint_from_managed_refuses(self):
        ctx, req = world()
        req["proven_external"][0]["disjoint_from_managed"] = False
        self.unsafe(ctx, req)

    def test_missing_matched_provenance_refuses(self):
        ctx, req = world()
        req["coverage"][0]["matched_provenance"] = False
        self.unsafe(ctx, req)

    def test_epoch_change_in_chain_refuses(self):
        ctx, req = world()
        older = _snapshot(18_000_000, SNAP_AT - 60_000, _eid("open-old-epoch"),
                          seq=0, epoch=7)
        ctx["readings"].append(older)
        ctx["gateways"][0]["current_chain_event_ids"].append(older["event_id"])
        self.unsafe(ctx, req)

    def test_contradictory_same_seq_chain_refuses(self):
        ctx, req = world()
        twin = _snapshot(18_430_001, SNAP_AT, _eid("open-twin"))
        ctx["readings"].append(twin)
        ctx["gateways"][0]["current_chain_event_ids"].append(twin["event_id"])
        self.unsafe(ctx, req)

    def test_conflicting_provider_totals_refuse(self):
        ctx, req = world()
        other = _provider(19 * E + 5, False, PT, event=_eid("open-provider-2"))
        ctx["readings"].append(other)
        self.unsafe(ctx, req)

    def test_stale_provider_refuses(self):
        ctx, req = world()
        self.unsafe(ctx, dict(req, now_ms=PT + 7_200_000 + 1,
                              coverage=[dict(COVER)]))

    def test_provider_freshness_boundary_with_a_fresh_ledger(self):
        max_age = _POLICY["max_age_seconds"] * 1000
        for age, expected in ((max_age, "ready"), (max_age + 1, "unsafe")):
            ctx, req = world()
            seen = NOW - age
            provider = ctx["readings"][0]
            provider["observed_at"], provider["received_at"] = seen, seen + 1000
            req = dict(req, drain_completed_at=seen - LAG_MS,
                       coverage=[dict(COVER, upper_at=seen, complete_at=seen)])
            self.assertEqual(plan_opening(ctx, req)["state"], expected)

    def test_stale_ledger_report_refuses(self):
        ctx, req = world()
        reading(ctx, snap_id("g1"))["observed_at"] = NOW - 120_001
        reading(ctx, snap_id("g1"))["received_at"] = NOW - 120_000
        self.unsafe(ctx, req)

    def test_superseded_provider_sample_refuses(self):
        ctx, req = world()
        newer = _provider(19 * E, False, PT + 60_000, event=_eid("open-provider-3"))
        ctx["readings"].append(newer)
        self.unsafe(ctx, req)

    def test_scope_mismatch_refuses(self):
        ctx, req = world()
        reading(ctx, PROV)["payload"]["scope_revision"] = 2
        self.unsafe(ctx, req)

    def test_unknown_or_mixed_basis_refuses(self):
        ctx, req = world()
        ctx["policy"]["provider_basis"] = "unknown"
        self.unsafe(ctx, req)
        ctx, req = world()
        ctx["policy"]["ledger_basis"] = "invoice_total"
        ctx["policy"]["vat_bps"] = 2000
        self.unsafe(ctx, req)

    def test_no_gateways_refuses(self):
        ctx, req = world()
        ctx["gateways"] = []
        req["baselines"] = []
        req["allocations"] = []
        req["proven_external"] = []
        req["non_ai_allocation"] = dict(amount_micro_eur=0, evidence_ids=[])
        self.unsafe(ctx, req)


# ---------------------------------------------------------------------------
# billing_opening_catchup
# ---------------------------------------------------------------------------

class BillingOpeningCatchup(Base):
    def test_18_43_19_0_57_unsafe_until_resolved(self):
        # Ledger 18.43 with 0.57 still uncertain; provider already shows 19.
        # Whatever the operator asserts, nothing is allocatable yet.
        ctx, req = world(uncertain=570_000)
        out = self.unsafe(ctx, req)
        self.assertEqual(out["allocations"], [])

    def test_p_minus_l_is_not_evidence_of_an_external_charge(self):
        ctx, req = world()
        req["allocations"] = []
        req["proven_external"] = []
        self.unsafe(ctx, req)

    def test_eventual_settled_19_produces_no_duplicate_057(self):
        # The uncertain 0.57 settles: L=19=P, so D=0 and the plan is empty.
        ctx, req = world(ledgers={"g1": 19 * E}, external={"g1": []})
        out = self.ready(ctx, req)
        self.assertEqual(out["allocations"], [dict(
            gateway="g1", baseline_micro_eur=19 * E, external_micro_eur=0,
            opening_micro_eur=19 * E)])
        # ... and a still-asserted 0.57 external charge would double count it.
        ctx, req = world(ledgers={"g1": 19 * E}, external={"g1": [570_000]})
        self.unsafe(ctx, req)

    def test_early_drain_refuses(self):
        ctx, req = world()
        early = PT - LAG_MS + 1                    # lastUpdate < drain + lag
        self.unsafe(ctx, dict(req, drain_completed_at=early))

    def test_lag_boundary_is_inclusive(self):
        ctx, req = world()
        self.ready(ctx, dict(req, drain_completed_at=PT - LAG_MS))
        self.unsafe(ctx, dict(req, drain_completed_at=PT - LAG_MS + 1))

    def test_complete_at_must_cover_the_drain(self):
        ctx, req = world()
        bad = copy.deepcopy(req)
        bad["coverage"][0].update(upper_at=D0 - 1, complete_at=D0 - 1)
        self.unsafe(ctx, bad)
        ok = copy.deepcopy(req)
        ok["coverage"][0].update(upper_at=D0, complete_at=D0)
        self.ready(ctx, ok)

    def test_missing_or_incomplete_coverage_refuses(self):
        ctx, req = world()
        self.unsafe(ctx, dict(req, coverage=[]))
        bad = copy.deepcopy(req)
        bad["coverage"][0].update(complete_at=None)
        self.unsafe(ctx, bad)
        bad = copy.deepcopy(req)
        bad["coverage"][0].update(upper_at=None, complete_at=None)
        self.unsafe(ctx, bad)

    def test_unverified_drain_refuses(self):
        ctx, req = world()
        self.unsafe(ctx, dict(req, drain_verified=False))

    def test_elapsed_wait_alone_never_proves_catch_up(self):
        # Long after the drain, but the provider has not reported past
        # drain+lag: no expiry proves catch-up.
        ctx, req = world()
        ctx["readings"][0]["observed_at"] = D0 + 1
        ctx["readings"][0]["received_at"] = D0 + 1001
        bad = copy.deepcopy(req)
        bad["coverage"][0].update(upper_at=D0 + 1, complete_at=D0 + 1)
        self.unsafe(ctx, dict(bad, now_ms=D0 + 1 + 7_200_000))


# ---------------------------------------------------------------------------
# billing_opening_allocations
# ---------------------------------------------------------------------------

class BillingOpeningAllocations(Base):
    def two_gateway(self, **over):
        kw = dict(ledgers={"g1": 10 * E, "g2": 5 * E}, total=17 * E,
                  external={"g1": [E], "g2": [500_000, 500_000]},
                  non_ai=[], shares={"g1": 40 * E, "g2": 30 * E})
        kw.update(over)
        return world(**kw)

    def test_split_across_gateways_charges_only_the_owner(self):
        out = self.ready(*self.two_gateway())
        self.assertEqual(out["allocations"], [
            dict(gateway="g1", baseline_micro_eur=10 * E,
                 external_micro_eur=E, opening_micro_eur=11 * E),
            dict(gateway="g2", baseline_micro_eur=5 * E,
                 external_micro_eur=E, opening_micro_eur=6 * E)])
        self.assertEqual(out["non_ai_allocation_micro_eur"], 0)

    def test_missing_gateway_allocation_means_zero(self):
        ctx, req = self.two_gateway(total=16 * E, external={"g1": [E]})
        out = self.ready(ctx, req)
        self.assertEqual([a["external_micro_eur"] for a in out["allocations"]], [E, 0])
        self.assertEqual(out["allocations"][1]["opening_micro_eur"], 5 * E)

    def test_non_ai_allocation_counted_once_in_its_own_bucket(self):
        ctx, req = self.two_gateway(total=17 * E, external={"g1": [E]},
                                    non_ai=[E], allowance=2 * E)
        out = self.ready(ctx, req)
        self.assertEqual(out["non_ai_allocation_micro_eur"], E)
        self.assertEqual(sum(a["external_micro_eur"] for a in out["allocations"]), E)
        # P - L = 2 EUR = 1 gateway + 1 non-AI: the non-AI EUR is in no ledger.
        self.assertEqual(sum(a["opening_micro_eur"] for a in out["allocations"]), 16 * E)

    def test_non_ai_allocation_never_lands_in_a_gateway_ledger(self):
        ctx, req = world(external={"g1": []}, non_ai=[570_000], allowance=E)
        out = self.ready(ctx, req)
        self.assertEqual(out["allocations"][0]["opening_micro_eur"], 18_430_000)
        self.assertEqual(out["non_ai_allocation_micro_eur"], 570_000)

    def test_wrong_owner_is_unsafe(self):
        ctx, req = self.two_gateway()
        req["proven_external"][0]["gateway"] = "g2"      # g1's proof owned by g2
        self.unsafe(ctx, req)

    def test_gateway_proof_consumed_by_non_ai_is_unsafe(self):
        ctx, req = world(external={"g1": []}, non_ai=[570_000], allowance=E)
        req["proven_external"][0]["gateway"] = "g1"
        self.unsafe(ctx, req)

    def test_non_ai_proof_consumed_by_gateway_is_unsafe(self):
        ctx, req = world()
        req["proven_external"][0]["gateway"] = None
        self.unsafe(ctx, req)

    def test_wrong_sum_is_unsafe(self):
        for delta in (-1, 1):
            ctx, req = world()
            req["allocations"][0]["external_micro_eur"] += delta   # != its proofs
            self.unsafe(ctx, req)
        ctx, req = world(total=19 * E + 1)                         # D larger
        self.unsafe(ctx, req)
        ctx, req = world(total=19 * E - 1)                         # D smaller
        self.unsafe(ctx, req)

    def test_non_ai_amount_must_equal_its_proofs(self):
        # Totals still match P-L (1 gateway + 569_999 non-AI); only the
        # non-AI amount disagrees with its own proof.
        ctx, req = world(external={"g1": [1]}, non_ai=[569_999], allowance=E)
        self.ready(ctx, req)
        req["non_ai_allocation"]["amount_micro_eur"] = 570_000 - 1 - 1
        req["allocations"][0]["external_micro_eur"] = 2
        req["proven_external"][0]["amount_micro_eur"] = 2
        self.unsafe(ctx, req)                          # 2 + 569_998 = 570_000
        ctx, req = world(external={"g1": [1]}, non_ai=[569_999], allowance=E)
        req["proven_external"][1]["amount_micro_eur"] = 569_998
        req["allocations"][0]["external_micro_eur"] = 2
        req["proven_external"][0]["amount_micro_eur"] = 2
        self.unsafe(ctx, req)                          # non-AI 569_999 vs proof 569_998

    def test_missing_ownership_is_unsafe(self):
        ctx, req = world()
        req["proven_external"] = []
        req["allocations"][0]["evidence_ids"] = []
        self.unsafe(ctx, req)

    def test_unconsumed_proof_is_unsafe(self):
        ctx, req = world()
        req["proven_external"].append(proof("spare", "g1", 0))
        self.unsafe(ctx, req)

    def test_allowance_exceeded_is_unsafe(self):
        ctx, req = world(external={"g1": []}, non_ai=[570_000], allowance=569_999)
        self.unsafe(ctx, req)
        ctx, req = world(external={"g1": []}, non_ai=[570_000], allowance=0)
        self.unsafe(ctx, req)

    def test_share_overflow_is_unsafe(self):
        ctx, req = world(shares={"g1": 19 * E - 1})
        self.unsafe(ctx, req)
        ctx, req = world(shares={"g1": 19 * E})
        self.ready(ctx, req)                                       # exactly at share

    def test_total_not_within_shares_plus_allowance_is_unsafe(self):
        ctx, req = world(shares={"g1": 90 * E}, allowance=20 * E)  # 110 > cap 100
        self.unsafe(ctx, req)

    def test_provider_below_ledger_preserves_every_baseline(self):
        ctx, req = self.two_gateway(total=12 * E, external={})
        out = self.ready(ctx, req)
        self.assertEqual([a["opening_micro_eur"] for a in out["allocations"]],
                         [10 * E, 5 * E])
        self.assertEqual([a["external_micro_eur"] for a in out["allocations"]], [0, 0])

    def test_provider_below_ledger_with_asserted_external_is_unsafe(self):
        ctx, req = self.two_gateway(total=12 * E, external={"g1": [1]})
        self.unsafe(ctx, req)

    def test_zero_gap_with_no_proofs_is_ready(self):
        ctx, req = world(ledgers={"g1": 19 * E}, external={})
        self.ready(ctx, req)

    def test_source_events_are_the_sorted_referenced_events(self):
        out = self.ready(*self.two_gateway())
        expected = sorted({PROV, snap_id("g1"), snap_id("g2")})
        self.assertEqual(out["source_events"], expected)
        names = [f"g{i}" for i in range(1, 8)]
        ctx, req = world(ledgers={n: E for n in names}, total=7 * E,
                         external={}, shares={n: 10 * E for n in names})
        out = self.ready(ctx, req)
        self.assertEqual(out["source_events"],
                         sorted({PROV, *(snap_id(n) for n in names)}))


# ---------------------------------------------------------------------------
# billing_proposal_only
# ---------------------------------------------------------------------------

class BillingProposalOnly(Base):
    def test_negative_amounts_are_invalid_config(self):
        ctx, req = world()
        for path, key in ((("baselines", 0), "baseline_micro_eur"),
                          (("allocations", 0), "external_micro_eur"),
                          (("proven_external", 0), "amount_micro_eur")):
            bad = copy.deepcopy(req)
            bad[path[0]][path[1]][key] = -1
            self.invalid(ctx, bad)
        bad = copy.deepcopy(req)
        bad["non_ai_allocation"]["amount_micro_eur"] = -1
        self.invalid(ctx, bad)

    def test_proposal_id_stable_across_repeated_calls(self):
        ctx, req = world()
        ids = {plan_opening(ctx, req)["proposal_id"] for _ in range(5)}
        self.assertEqual(len(ids), 1)
        outs = [json.dumps(plan_opening(ctx, req), sort_keys=True) for _ in range(3)]
        self.assertEqual(len(set(outs)), 1)

    def test_proposal_id_stable_across_key_orders(self):
        ctx, req = world(ledgers={"g1": 10 * E, "g2": 5 * E}, total=17 * E,
                         external={"g1": [E], "g2": [E]})
        base = plan_opening(ctx, req)

        def reordered(value, rng):
            if isinstance(value, dict):
                items = list(value.items())
                rng.shuffle(items)
                return {k: reordered(v, rng) for k, v in items}
            if isinstance(value, list):
                return [reordered(v, rng) for v in value]
            return value

        for seed in range(10):
            rng = random.Random(seed)
            self.assertEqual(plan_opening(reordered(ctx, rng), reordered(req, rng)), base)

    def test_input_order_independence(self):
        ctx, req = world(ledgers={"g1": 10 * E, "g2": 5 * E}, total=17 * E,
                         external={"g1": [400_000, 600_000], "g2": [E]},
                         non_ai=[], allowance=0)
        req["coverage"].append(dict(COVER, event_id=_eid("open-provider-b")))
        ctx["readings"].append(_provider(17 * E - 5, False, PT - 900_000,
                                         event=_eid("open-provider-b")))
        base = plan_opening(ctx, req)
        self.assertEqual(base["state"], "ready")
        for perm in itertools.permutations(range(len(ctx["readings"]))):
            c = copy.deepcopy(ctx)
            c["readings"] = [ctx["readings"][i] for i in perm]
            self.assertEqual(plan_opening(c, req), base)
        for name in ("baselines", "allocations", "proven_external", "coverage"):
            for perm in itertools.permutations(range(len(req[name]))):
                r = copy.deepcopy(req)
                r[name] = [req[name][i] for i in perm]
                self.assertEqual(plan_opening(ctx, r), base, name)
        for perm in itertools.permutations(range(2)):
            c = copy.deepcopy(ctx)
            c["gateways"] = [ctx["gateways"][i] for i in perm]
            self.assertEqual(plan_opening(c, req), base)
        r = copy.deepcopy(req)
        r["allocations"][0]["evidence_ids"].reverse()
        self.assertEqual(plan_opening(ctx, r), base)

    def test_proposal_id_tracks_the_material_inputs(self):
        ctx, req = world()
        base = plan_opening(ctx, req)["proposal_id"]
        other_ctx, other_req = world(ledgers={"g1": 18_430_000}, total=19 * E + 1,
                                     external={"g1": [570_001]})
        self.assertNotEqual(base, plan_opening(other_ctx, other_req)["proposal_id"])
        again = dict(req, now_ms=NOW + 1)      # only the injected time moved
        self.assertEqual(base, plan_opening(ctx, again)["proposal_id"])
        unsafe = plan_opening(ctx, dict(req, drain_verified=False))
        self.assertNotEqual(base, unsafe["proposal_id"])
        self.assertEqual(unsafe["proposal_id"],
                         plan_opening(ctx, dict(req, drain_verified=False))["proposal_id"])

    def test_valid_plan_preserves_baselines(self):
        ctx, req = world(ledgers={"g1": 10 * E, "g2": 5 * E}, total=17 * E,
                         external={"g1": [E], "g2": [E]})
        out = self.ready(ctx, req)
        by_gateway = {a["gateway"]: a for a in out["allocations"]}
        for baseline in req["baselines"]:
            got = by_gateway[baseline["gateway"]]
            self.assertEqual(got["baseline_micro_eur"], baseline["baseline_micro_eur"])
            self.assertEqual(got["opening_micro_eur"],
                             got["baseline_micro_eur"] + got["external_micro_eur"])
        self.assertEqual([a["gateway"] for a in out["allocations"]], ["g1", "g2"])

    def test_no_financial_writes_inputs_untouched(self):
        for build in (world, lambda: world(uncertain=1)):
            ctx, req = build()
            before = copy.deepcopy((ctx, req))
            plan_opening(ctx, req)
            self.assertEqual((ctx, req), before)

    def test_error_path_leaves_inputs_untouched(self):
        ctx, req = world()
        req["version"] = 2
        before = copy.deepcopy((ctx, req))
        self.invalid(ctx, req)
        self.assertEqual((ctx, req), before)

    def test_result_is_detached_from_inputs(self):
        ctx, req = world()
        out = plan_opening(ctx, req)
        out["allocations"][0]["external_micro_eur"] = 0
        out["source_events"].append("x")
        self.assertEqual(plan_opening(ctx, req)["allocations"][0]["external_micro_eur"],
                         570_000)

    def test_evaluation_side_is_unaffected(self):
        from quota_balancer.billing_eval import evaluate_billing
        ctx, req = world()
        ev = evaluate_billing(ctx, dict(version=1, account="meter", now_ms=NOW,
                                        requested_micro_eur=0, coverage=[]))
        self.assertIsNone(ev["proposal"])


# ---------------------------------------------------------------------------
# billing_output_errors
# ---------------------------------------------------------------------------

class BillingOutputErrors(Base):
    def branches(self):
        yield "ready", world()
        yield "ready-zero", world(ledgers={"g1": 19 * E}, external={})
        yield "unsafe-uncertain", world(uncertain=1)
        yield "unsafe-no-proof", (lambda c, r: (c, dict(
            r, allocations=[], proven_external=[])))(*world())
        yield "unsafe-unverified", (lambda c, r: (c, dict(r, drain_verified=False)))(*world())

    def test_every_branch_is_closed_and_json_serialisable(self):
        enums = {"state": {"ready", "unsafe"}, "reason": {"verified", "opening_unsafe"}}
        for name, (ctx, req) in self.branches():
            with self.subTest(name):
                out = plan_opening(ctx, req)
                self.assertEqual(set(out), PROPOSAL_KEYS)
                json.dumps(out)
                self.assertIn(out["state"], enums["state"])
                self.assertIn(out["reason"], enums["reason"])
                self.assertEqual((out["state"], out["reason"]) in
                                 {("ready", "verified"), ("unsafe", "opening_unsafe")}, True)
                self.assertEqual(type(out["proposal_id"]), str)
                self.assertEqual(type(out["non_ai_allocation_micro_eur"]), int)
                for a in out["allocations"]:
                    self.assertEqual(set(a), ALLOCATION_KEYS)
                    for key in ("baseline_micro_eur", "external_micro_eur",
                                "opening_micro_eur"):
                        self.assertIs(type(a[key]), int)
                if out["state"] == "unsafe":
                    self.assertEqual(out["allocations"], [])

    def test_invalid_config_maps_to_exit_2_with_static_message(self):
        ctx, req = world()
        messages = set()
        for bad in (dict(req, version=2), dict(req, account="other"),
                    dict(req, surprise=1), dict(req, now_ms=True)):
            err = self.invalid(ctx, bad)
            messages.add(err.message)
            self.assertEqual(EXIT_CODES[err.code], 2)
        self.assertEqual(len(messages), 1)             # one static message

    def test_opening_unsafe_code_exits_2_for_an_explicit_usable_plan_request(self):
        self.assertEqual(EXIT_CODES[CODE_OPENING_UNSAFE], 2)
        self.assertEqual(EXIT_CODES[CODE_INVALID_CONFIG], 2)

    def test_numeric_overflow_uses_invalid_config(self):
        ctx, req = world(ledgers={"g1": INT64}, total=INT64, external={"g1": []},
                         shares={"g1": INT64})
        ctx["cap_micro_eur"] = INT64
        self.ready(ctx, req)                           # exactly int64: fine
        ctx, req = world(ledgers={"g1": INT64, "g2": 1}, total=INT64,
                         external={}, shares={"g1": INT64, "g2": 1})
        ctx["cap_micro_eur"] = INT64
        self.invalid(ctx, req)                         # L sums past int64

    def test_only_local_typed_error_escapes(self):
        ctx, req = world()
        for junk in (object(), 3, "x", b"x", float("nan")):
            for bad_ctx, bad_req in ((junk, req), (ctx, junk)):
                with self.assertRaises(QuotaError):
                    plan_opening(bad_ctx, bad_req)


# ---------------------------------------------------------------------------
# privacy_2b
# ---------------------------------------------------------------------------

SENTINELS = ("sk-SECRET-sentinel", "C:\\secret\\path.txt", "/etc/secret-sentinel",
             "provider-id-ovh-9f8e7d")


class Privacy2b(Base):
    def test_sentinels_never_appear_in_errors(self):
        ctx, req = world()
        for sentinel in SENTINELS:
            slots = []
            for key in ("account", "billing_event_id"):
                slots.append(dict(req, **{key: sentinel}))
            for path, key in ((("baselines", 0), "gateway"),
                              (("baselines", 0), "snapshot_event_id"),
                              (("allocations", 0), "gateway"),
                              (("proven_external", 0), "gateway"),
                              (("proven_external", 0), "evidence_id"),
                              (("coverage", 0), "event_id")):
                bad = copy.deepcopy(req)
                bad[path[0]][path[1]][key] = sentinel
                slots.append(bad)
            bad = copy.deepcopy(req)
            bad["allocations"][0]["evidence_ids"] = [sentinel]
            slots.append(bad)
            bad = copy.deepcopy(req)
            bad["surprise-" + sentinel] = 1
            slots.append(bad)
            bad_ctx = copy.deepcopy(ctx)
            bad_ctx["gateways"][0]["gateway"] = sentinel
            for request in slots:
                err = self.invalid(ctx, request)
                for text in (str(err), repr(err), err.message, repr(err.args)):
                    self.assertNotIn(sentinel, text)
            err = self.invalid(bad_ctx, req)
            self.assertNotIn(sentinel, str(err) + repr(err))

    def test_success_output_holds_only_approved_values(self):
        ctx, req = world(ledgers={"g1": 10 * E, "g2": 5 * E}, total=17 * E,
                         external={"g1": [E], "g2": [E]})
        for out in (plan_opening(ctx, req),
                    plan_opening(*world(uncertain=1))):
            approved = {"g1", "g2", "ready", "unsafe", "verified", "opening_unsafe"}
            approved |= set(out["source_events"]) | {out["proposal_id"]}
            approved |= {r["event_id"] for r in ctx["readings"]}

            def walk(value):
                if isinstance(value, dict):
                    self.assertLessEqual(set(value), PROPOSAL_KEYS | ALLOCATION_KEYS)
                    for v in value.values():
                        walk(v)
                elif isinstance(value, list):
                    for v in value:
                        walk(v)
                elif isinstance(value, str):
                    self.assertIn(value, approved)
                else:
                    self.assertIs(type(value), int)

            walk(out)

    def test_module_imports_nothing_that_can_do_io(self):
        source = pathlib.Path(billing_open.__file__).read_text(encoding="utf-8")
        forbidden = {"os", "sys", "io", "time", "datetime", "sqlite3", "socket",
                     "subprocess", "pathlib", "random", "shutil", "tempfile",
                     "urllib", "http", "ssl", "logging", "threading", "secrets",
                     "builtins", "importlib", "ctypes"}
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported & forbidden, set())
        called = {n.func.id for n in ast.walk(ast.parse(source))
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertEqual(called & {"open", "print", "input", "exec", "eval",
                                   "compile", "__import__"}, set())

    def test_no_io_no_clock_no_network_no_randomness_at_runtime(self):
        def boom(*a, **k):
            raise AssertionError("side effect")

        cases = [world(), world(uncertain=1), world(ledgers={"g1": 19 * E}, external={})]
        bad_ctx, bad_req = world()
        bad_req["version"] = 3
        with mock.patch.object(builtins, "open", boom), \
                mock.patch.object(builtins, "print", boom), \
                mock.patch.object(time, "time", boom), \
                mock.patch.object(time, "monotonic", boom), \
                mock.patch.object(time, "sleep", boom), \
                mock.patch.object(socket, "socket", boom), \
                mock.patch.object(socket, "create_connection", boom), \
                mock.patch.object(os, "stat", boom), \
                mock.patch.object(os, "listdir", boom), \
                mock.patch.object(os, "urandom", boom), \
                mock.patch.object(random, "random", boom):
            for ctx, req in cases:
                plan_opening(ctx, req)
            with self.assertRaises(QuotaError):
                plan_opening(bad_ctx, bad_req)

    def test_before_after_snapshots_show_zero_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                env = dict(os.environ)

                def listing():
                    return sorted(str(p) for p in pathlib.Path(tmp).rglob("*"))

                before = (listing(), env, os.getcwd())
                for build in (world, lambda: world(uncertain=1)):
                    ctx, req = build()
                    snapshot = copy.deepcopy((ctx, req))
                    plan_opening(ctx, req)
                    self.assertEqual((ctx, req), snapshot)
                bad_ctx, bad_req = world()
                bad_req["surprise"] = 1
                with self.assertRaises(QuotaError):
                    plan_opening(bad_ctx, bad_req)
                self.assertEqual((listing(), dict(os.environ), os.getcwd()), before)
            finally:
                os.chdir(cwd)

    def test_no_cli_verb_or_store_import(self):
        self.assertFalse(hasattr(billing_open, "main"))
        self.assertFalse(hasattr(billing_open, "sqlite3"))


if __name__ == "__main__":
    unittest.main()
