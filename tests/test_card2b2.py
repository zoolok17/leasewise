"""Card 2b-2 acceptance tests: the evaluate_billing input/output contract
(turn 2 of 4 - input surface, output shape, errors and precedence).

Run from the repository root:
    python -m unittest discover -s tests -v

evaluate_billing(context, request) is a PURE function: no I/O, no store
writes, no clock reads (time is injected). This turn pins:

  billing_eval_input           closed context/request validation: bad
                               types, unknown fields, unsupported version,
                               account mismatch, coverage ordering
                               (complete w/o upper, complete_at>upper_at,
                               times after now), unknown/duplicate refs,
                               and an explicit overlap floor outside
                               min(P,L_now,B_e) are all INVALID_CONFIG;
                               inputs are not mutated.
  billing_overlap_precedence   explicit overlap_floor wins when present
                               (under its proofs); absent overlap derives
                               O_e=B_e only under proof and B_e<=P_e, else
                               O_e=0 - pinned through the spend estimate.
  billing_non_ai_alert         a retained current-period non-AI flag raises
                               action to alert/non_ai_present; coverage
                               alone stays none; the flag never downgrades
                               a hold nor replaces an earlier alert reason.
  billing_precedence           the decision table is ordered (a hold/excess
                               row precedes the unresolved-bounds catch-all)
                               and first-match-wins.

Turn 3 adds the reconciliation rows: gross excess (uncertified) with
reserves/allowance/boundary/tolerance, conflict-latest ordering (gross
first, conflict preserved), latest-seq ledger selection, gateway
usability, listed-transition precedence, the midnight boundary
exposure, the as-of bracket (skew/covered/excess), the read-only lag
timer and the tolerance. The advisory headroom is turn 4; until then
headroom stays null, admission_basis=unavailable and request_fits null
(false on a hold) so nothing is ever admitted on an un-evaluated
estimate. The floor diagnostic (known_floor) is the ledger's current
settled total, null when the ledger is unusable.
"""

import copy
import datetime
import json
import unittest
import uuid

from quota_balancer.billing_eval import (
    apply_non_ai_overlay,
    base_row_order,
    evaluate_billing,
)
from quota_balancer.errors import CODE_INVALID_CONFIG, QuotaError

_EID_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # NAMESPACE_URL


def _eid(label):
    return str(uuid.uuid5(_EID_NS, "quota-card2b2/eval/" + label))


def _ms(year, month, day=1):
    return int(datetime.datetime(year, month, day,
                                 tzinfo=datetime.timezone.utc).timestamp() * 1000)


SEP = _ms(2026, 9)
OCT = _ms(2026, 10)

_POLICY = {
    "account": "meter", "reporter_machine": "desktop", "scope_alias": "proj-ai",
    "scope_revision": 1, "dedicated_ai": True,
    "non_ai_allowance_micro_eur": 0, "cap_basis": "ex_vat",
    "provider_basis": "ex_vat", "ledger_basis": "ex_vat", "vat_bps": None,
    "poll_seconds": 900, "max_age_seconds": 7200, "lag_seconds": 14400,
    "abs_tolerance_micro_eur": 50000, "relative_tolerance_bps": 100,
    "mode": "advisory", "stale_action": "alert", "divergence_action": "hold",
}


def _provider(total, non_ai, observed, event=None, account="meter"):
    return {
        "event_id": event or _eid("prov"),
        "machine": "desktop", "account": account, "kind": "provider_billing",
        "observed_at": observed, "received_at": observed + 1000,
        "payload": {
            "scope_alias": "proj-ai", "scope_revision": 1,
            "period_start": SEP, "period_end": OCT, "currency": "EUR",
            "total_micro_eur": total, "non_ai_spend_present": non_ai,
        },
    }


def _snapshot(settled, observed, event, gateway="g1", seq=1, reserved=0,
              uncertain=0, unattributed=0, epoch=1):
    return {
        "event_id": event, "machine": "desktop", "account": "meter",
        "kind": "gateway_snapshot", "observed_at": observed,
        "received_at": observed + 1000,
        "payload": {
            "gateway": gateway, "ledger_epoch": epoch, "seq": seq,
            "period_start": SEP, "period_end": OCT, "policy_hashes": None,
            "sized_ready": False, "reconciliation_required": False,
            "settled_micro_eur": settled, "reserved_micro_eur": reserved,
            "uncertain_micro_eur": uncertain,
            "unattributed_micro_eur": unattributed,
        },
    }


def _context(**over):
    """A well-formed evaluation context for one metered account."""
    ctx = {
        "account": "meter",
        "period_start": SEP, "period_end": OCT,
        "cap_micro_eur": 100_000_000,
        "gateways": [
            {"gateway": "g1", "share_micro_eur": 60_000_000,
             "current_chain_event_ids": [], "reconciliation_required": False},
        ],
        "policy": copy.deepcopy(_POLICY),
        "readings": [],
        "unresolved_event_ids": [],
        "boundary_exposure_micro_eur": None,
        "boundary_event_ids": [],
    }
    ctx.update(over)
    return ctx


def _request(**over):
    req = {
        "version": 1, "account": "meter", "now_ms": OCT + 1000,
        "requested_micro_eur": 0, "coverage": [],
    }
    req.update(over)
    return req


def _eval(ctx, req):
    return evaluate_billing(ctx, req)


class BillingEvalInput(unittest.TestCase):
    """billing_eval_input: closed context/request, static INVALID_CONFIG,
    no raw input echoed, inputs not mutated."""

    def _valid(self):
        ctx = _context(readings=[_provider(80_000_000, False, SEP + 86_400_000)])
        req = _request()
        return ctx, req

    def test_valid_input_returns_closed_evaluation(self):
        ctx, req = self._valid()
        out = _eval(ctx, req)
        for key in ("state", "action", "reason", "quality",
                    "admission_basis", "known_floor_micro_eur",
                    "estimate_micro_eur", "headroom_micro_eur",
                    "request_fits", "proposal"):
            self.assertIn(key, out)
        self.assertIsNone(out["proposal"])  # evaluation never proposes

    def test_inputs_not_mutated(self):
        ctx, req = self._valid()
        before = (copy.deepcopy(ctx), copy.deepcopy(req))
        _eval(ctx, req)
        self.assertEqual(ctx, before[0])
        self.assertEqual(req, before[1])

    def _reject(self, ctx, req, **_):
        with self.assertRaises(QuotaError) as caught:
            _eval(ctx, req)
        self.assertEqual(caught.exception.code, CODE_INVALID_CONFIG)

    def test_context_closed_keys(self):
        ctx, req = self._valid()
        ctx2 = copy.deepcopy(ctx); ctx2["extra"] = 1
        self._reject(ctx2, req)
        ctx3 = copy.deepcopy(ctx); del ctx3["cap_micro_eur"]
        self._reject(ctx3, req)

    def test_context_types(self):
        ctx, req = self._valid()
        bad_cap = copy.deepcopy(ctx); bad_cap["cap_micro_eur"] = -1
        self._reject(bad_cap, req)
        bad_alias = copy.deepcopy(ctx); bad_alias["account"] = "METER"
        self._reject(bad_alias, req)
        # period must be exactly one UTC month
        bad_period = copy.deepcopy(ctx); bad_period["period_end"] = _ms(2026, 12)
        self._reject(bad_period, req)
        # bad policy record (a closed shape)
        bad_policy = copy.deepcopy(ctx)
        bad_policy["policy"] = dict(_POLICY); del bad_policy["policy"]["lag_seconds"]
        self._reject(bad_policy, req)
        # policy must be for this account
        mismatched = copy.deepcopy(ctx)
        mismatched["policy"] = dict(_POLICY, account="other")
        self._reject(mismatched, req)

    def test_readings_for_this_account_only(self):
        ctx, req = self._valid()
        foreign = _context(readings=[_provider(100, False, SEP + 1,
                                               account="other")])
        self._reject(foreign, req)

    def test_chain_membership_validated(self):
        ctx, req = self._valid()
        # a current_chain_event_id that references no reading
        ctx2 = _context(readings=[_provider(80_000_000, False, SEP + 1)],
                        gateways=[{"gateway": "g1", "share_micro_eur": 60,
                                   "current_chain_event_ids": [_eid("ghost")], "reconciliation_required": False}])
        self._reject(ctx2, req)

    def test_boundary_ids_are_known(self):
        ctx, req = self._valid()
        ctx2 = _context(readings=[_provider(80_000_000, False, SEP + 1)],
                        boundary_event_ids=[_eid("ghost")])
        self._reject(ctx2, req)

    def test_request_version_and_account(self):
        ctx, req = self._valid()
        self._reject(ctx, _request(version=2))
        self._reject(ctx, _request(account="other"))

    def test_request_types(self):
        ctx, req = self._valid()
        self._reject(ctx, _request(requested_micro_eur=-1))
        bad = _request(); bad["extra"] = 1
        self._reject(ctx, bad)

    def test_coverage_ordering(self):
        ctx, req = self._valid()
        pe = _provider(80_000_000, False, SEP + 1)
        ctx = _context(readings=[pe])
        # complete without upper
        self._reject(ctx, _request(
            coverage=[{"event_id": pe["event_id"], "upper_at": None,
                       "complete_at": OCT, "overlap_floor_micro_eur": None,
                       "matched_provenance": True}]))
        # complete_at after upper_at
        self._reject(ctx, _request(
            coverage=[{"event_id": pe["event_id"], "upper_at": SEP + 5,
                       "complete_at": SEP + 10, "overlap_floor_micro_eur": None,
                       "matched_provenance": True}]))
        # a time after now
        self._reject(ctx, _request(
            coverage=[{"event_id": pe["event_id"], "upper_at": OCT + 5000,
                       "complete_at": OCT + 4000,
                       "overlap_floor_micro_eur": None,
                       "matched_provenance": True}]))

    def test_coverage_refs_known_and_unique(self):
        ctx, req = self._valid()
        pe = _provider(80_000_000, False, SEP + 1)
        ctx = _context(readings=[pe])
        # unknown event reference
        self._reject(ctx, _request(
            coverage=[{"event_id": _eid("ghost"), "upper_at": OCT,
                       "complete_at": OCT, "overlap_floor_micro_eur": None,
                       "matched_provenance": True}]))
        # duplicate event reference
        self._reject(ctx, _request(coverage=[
            {"event_id": pe["event_id"], "upper_at": OCT, "complete_at": OCT,
             "overlap_floor_micro_eur": None, "matched_provenance": True},
            {"event_id": pe["event_id"], "upper_at": OCT, "complete_at": OCT,
             "overlap_floor_micro_eur": None, "matched_provenance": True},
        ]))

    def test_overlap_floor_out_of_range_rejected(self):
        # An explicit overlap floor above min(P, L_now, B_e) is
        # INVALID_CONFIG, never clipped.
        evA, evB = _eid("asof"), _eid("latest")
        b = _eid("boundary")
        ctx = _context(
            readings=[
                _provider(80_000_000, False, SEP + 86_400_000, _eid("prov")),
                _snapshot(50_000_000, SEP + 1 * 86_400_000, evA),
                _snapshot(60_000_000, SEP + 2 * 86_400_000, evB),
                _snapshot(0, SEP + 86_400_000, b),
            ],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [evA, evB], "reconciliation_required": False}],
            boundary_exposure_micro_eur=0,
            boundary_event_ids=[b],
        )
        # P=80, L_now=60, B_e=50 -> min=50. A floor of 51 is out of range.
        self._reject(ctx, _request(
            coverage=[{"event_id": _eid("prov"), "upper_at": SEP + 2 * 86_400_000,
                       "complete_at": SEP + 1 * 86_400_000,
                       "overlap_floor_micro_eur": 51_000_000,
                       "matched_provenance": True}]))
        # in-range floor is accepted
        _eval(ctx, _request(
            coverage=[{"event_id": _eid("prov"),
                       "upper_at": SEP + 2 * 86_400_000,
                       "complete_at": SEP + 1 * 86_400_000,
                       "overlap_floor_micro_eur": 50_000_000,
                       "matched_provenance": True}]))


class BillingOverlapPrecedence(unittest.TestCase):
    """billing_overlap_precedence: explicit floor wins; absent overlap
    derives O_e=B_e only under proof (and B_e<=P_e), else O_e=0. Pinned
    through the spend estimate E = max(L_now, P_e + L_now - O_e)."""

    def _ctx(self):
        evA, evB = _eid("asof"), _eid("latest")
        return _context(
            readings=[
                _provider(80_000_000, False, SEP + 86_400_000, _eid("prov")),
                _snapshot(50_000_000, SEP + 1 * 86_400_000, evA),
                _snapshot(60_000_000, SEP + 2 * 86_400_000, evB),
            ],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [evA, evB], "reconciliation_required": False}],
        )

    def test_explicit_valid_floor_wins(self):
        # O_e = 30 (explicit) -> E = max(60, 80+60-30) = 110.
        out = _eval(self._ctx(), _request(
            coverage=[{"event_id": _eid("prov"),
                       "upper_at": SEP + 2 * 86_400_000,
                       "complete_at": SEP + 1 * 86_400_000,
                       "overlap_floor_micro_eur": 30_000_000,
                       "matched_provenance": True}]))
        self.assertEqual(out["estimate_micro_eur"], 110_000_000)

    def test_absent_overlap_derives_with_proof(self):
        # No explicit floor, but complete + matched_provenance + usable
        # B_e=50 and B_e<=P_e (50<=80) -> O_e=B_e=50 -> E=max(60,80+60-50)=90.
        out = _eval(self._ctx(), _request(
            coverage=[{"event_id": _eid("prov"),
                       "upper_at": SEP + 2 * 86_400_000,
                       "complete_at": SEP + 1 * 86_400_000,
                       "overlap_floor_micro_eur": None,
                       "matched_provenance": True}]))
        self.assertEqual(out["estimate_micro_eur"], 90_000_000)

    def test_absent_overlap_no_proof_is_zero(self):
        # No explicit floor and no matched provenance -> O_e=0 ->
        # E=max(60,80+60-0)=140 (diagnostic over-count, still conservative).
        out = _eval(self._ctx(), _request(
            coverage=[{"event_id": _eid("prov"),
                       "upper_at": SEP + 2 * 86_400_000,
                       "complete_at": SEP + 1 * 86_400_000,
                       "overlap_floor_micro_eur": None,
                       "matched_provenance": False}]))
        self.assertEqual(out["estimate_micro_eur"], 140_000_000)


class BillingNonAiAlert(unittest.TestCase):
    """billing_non_ai_alert: coverage alone stays none; a retained
    current-period non-AI flag raises alert/non_ai_present and never
    downgrades a hold nor replaces an earlier alert reason."""

    def test_coverage_alone_is_none(self):
        # A fresh provider sample equal to the settled ledger (no
        # excess), no non-AI flag: the provisional catch-all, none.
        prov_obs = SEP + 86_400_000
        be = _eid("boundary")
        ctx = _context(
            readings=[
                _provider(45_000_000, False, prov_obs, _eid("prov")),
                _snapshot(45_000_000, prov_obs + 40_000, _eid("snap")),
                _snapshot(0, prov_obs, be),
            ],
            boundary_exposure_micro_eur=0, boundary_event_ids=[be])
        out = _eval(ctx, _request(now_ms=prov_obs + 50_000))
        self.assertEqual(out["action"], "none")
        self.assertEqual(out["quality"], "provisional")

    def test_non_ai_flag_raises_alert(self):
        prov_obs = SEP + 86_400_000
        be = _eid("boundary")
        ctx = _context(
            readings=[
                _provider(45_000_000, True, prov_obs, _eid("prov")),
                _snapshot(45_000_000, prov_obs + 40_000, _eid("snap")),
                _snapshot(0, prov_obs, be),
            ],
            boundary_exposure_micro_eur=0, boundary_event_ids=[be])
        out = _eval(ctx, _request(now_ms=prov_obs + 50_000))
        self.assertEqual(out["action"], "alert")
        self.assertEqual(out["reason"], "non_ai_present")

    def test_overlay_never_downgrades_hold(self):
        self.assertEqual(
            apply_non_ai_overlay("hold", "provider_excess", True),
            ("hold", "provider_excess"))

    def test_overlay_preserves_earlier_alert_reason(self):
        self.assertEqual(
            apply_non_ai_overlay("alert", "billing_stale", True),
            ("alert", "billing_stale"))

    def test_overlay_none_to_alert(self):
        self.assertEqual(
            apply_non_ai_overlay("none", "coverage_unknown", True),
            ("alert", "non_ai_present"))

    def test_overlay_no_flag_unchanged(self):
        self.assertEqual(
            apply_non_ai_overlay("none", "coverage_unknown", False),
            ("none", "coverage_unknown"))


class BillingPrecedence(unittest.TestCase):
    """billing_precedence: the decision table is ordered first-match-wins;
    a hold/excess row precedes the unresolved-bounds catch-all."""

    def test_excess_precedes_unresolved_bounds(self):
        order = base_row_order()
        self.assertIn("historical_excess", order)
        self.assertIn("unresolved_bounds", order)
        # A certified historical excess (a HOLD) is decided before the
        # unresolved-bounds catch-all (a none), so a hold is never shadowed
        # by the catch-all.
        self.assertLess(order.index("historical_excess"),
                        order.index("unresolved_bounds"))

    def test_hold_never_downgraded_by_overlay(self):
        # The overlay only ever raises none to alert; it never lowers a
        # hold, so precedence of a hold row is preserved end to end.
        action, reason = apply_non_ai_overlay("hold", "provider_excess", True)
        self.assertEqual((action, reason), ("hold", "provider_excess"))


class BillingGrossExcessUncertified(unittest.TestCase):
    """billing_reconcile_gross_excess_uncertified: a fresh provider total
    beyond L_now + R_now + B + allowance + tau (latest-value receipt
    guards, no certificate) is a comparable HOLD; the guards pin the
    time bounds (the demo: P=L+30, zero R/allowance/B -> hold)."""

    def _ctx(self, p, l=80_000_000, r=0):
        ev = _eid("snap")
        prov_obs = SEP + 2 * 86_400_000
        snap_obs = prov_obs + 40_000  # within 120s, covers the receipt+30s
        be = _eid("boundary")
        return _context(
            readings=[
                _provider(p, False, prov_obs, _eid("prov")),
                # A proved-empty boundary (B=0) lets the gross check run;
                # it precedes the ledger snapshot in the chain.
                _snapshot(0, prov_obs, be),
                _snapshot(l, snap_obs, ev, reserved=r),
            ],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [ev],
                       "reconciliation_required": False}],
            boundary_exposure_micro_eur=0,
            boundary_event_ids=[be],
        )

    def test_fresh_uncertified_excess_is_comparable_hold(self):
        # P = L + 30 EUR, R/B/allowance = 0, no coverage: the demo HOLD.
        out = _eval(self._ctx(80_000_000 + 30_000_000), _request(now_ms=SEP + 2 * 86_400_000 + 50_000))
        self.assertEqual(out["state"], "unexplained_excess")
        self.assertEqual(out["reason"], "provider_excess")
        self.assertEqual(out["action"], "hold")
        self.assertEqual(out["quality"], "comparable")

    def test_report_far_after_receipt_cannot_hold(self):
        # The report arrives 125 s after the provider receipt: beyond
        # the 120 s window it cannot cover the receipt, so no gross
        # hold even though it is fresh for now (B proven empty, so the
        # only thing standing between it and a hold is the window).
        ev = _eid("snap")
        be = _eid("boundary")
        prov_obs = SEP + 2 * 86_400_000
        ctx = _context(
            readings=[
                _provider(110_000_000, False, prov_obs, _eid("prov")),
                _snapshot(0, prov_obs, be),
                _snapshot(80_000_000, prov_obs + 125_000, ev),
            ],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [ev],
                       "reconciliation_required": False}],
            boundary_exposure_micro_eur=0,
            boundary_event_ids=[be],
        )
        out = _eval(ctx, _request(now_ms=prov_obs + 135_000))
        self.assertNotEqual(out["action"], "hold")
        self.assertEqual(out["quality"], "provisional")

    def test_old_snapshot_cannot_hold(self):
        # The report is older than 120 s from the provider receipt: the
        # time guard fails, so no gross hold (falls through to catch-all).
        ev = _eid("snap")
        prov_obs = SEP + 2 * 86_400_000
        ctx = _context(
            readings=[
                _provider(110_000_000, False, prov_obs, _eid("prov")),
                _snapshot(80_000_000, prov_obs + 121_000, ev),
            ],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [ev],
                       "reconciliation_required": False}],
        )
        out = _eval(ctx, _request())
        self.assertNotEqual(out["action"], "hold")
        self.assertEqual(out["quality"], "provisional")

    def test_stale_provider_sample_does_not_hold(self):
        # A provider sample older than max_age (stale row 2) alerts, and a
        # stale P never gross-holds: the hold test requires a fresh P.
        ev = _eid("snap")
        prov_obs = SEP + 86_400_000
        ctx = _context(
            readings=[
                _provider(150_000_000, False, prov_obs, _eid("prov")),
                _snapshot(80_000_000, prov_obs + 40_000, ev),
            ],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [ev],
                       "reconciliation_required": False}],
        )
        # now far after max_age has passed: the sample is stale.
        out = _eval(ctx, _request(now_ms=prov_obs + 7200_000 + 1000))
        self.assertEqual(out["state"], "unknown")
        self.assertEqual(out["reason"], "billing_stale")
        self.assertEqual(out["action"], "alert")
        self.assertEqual(out["quality"], "provisional")


class BillingGrossAllowance(unittest.TestCase):
    """billing_gross_allowance: the threshold is L_now + R_now + B +
    allowance + tau(L_now) - no hold at equality, hold at +1 micro-EUR;
    reserves/allowance prevent false excess; unknown B disables the check
    (never B=0)."""

    def _ctx(self, p, l=80_000_000, r=0, b=None, allowance=0, b_refs=()):
        ev = _eid("snap")
        prov_obs = SEP + 2 * 86_400_000
        snap_obs = prov_obs + 40_000
        readings = [
            _provider(p, False, prov_obs, _eid("prov")),
        ]
        # A known boundary is backed by a store boundary record (a
        # snapshot reading the reference points at); it precedes the
        # ledger snapshot so the chain's latest stays the ledger one.
        if b is not None and not b_refs:
            be = _eid("boundary")
            readings.append(_snapshot(0, prov_obs, be))
            b_refs = [be]
        readings.append(_snapshot(l, snap_obs, ev, reserved=r))
        return _context(
            readings=readings,
            policy=dict(_POLICY, non_ai_allowance_micro_eur=allowance),
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [ev],
                       "reconciliation_required": False}],
            boundary_exposure_micro_eur=b,
            boundary_event_ids=list(b_refs),
        )

    def test_no_hold_at_equality_hold_at_plus_one(self):
        # L=80M, R=0, B=0, allowance=0, tau(80M)=ceil(80M*100/10000)=800000:
        # threshold = 80_800_000. Equality: no hold; +1 micro: hold.
        out = _eval(self._ctx(80_800_000, b=0), _request(now_ms=SEP + 2 * 86_400_000 + 50_000))
        self.assertNotEqual(out["action"], "hold")
        self.assertEqual(out["quality"], "provisional")
        out = _eval(self._ctx(80_800_001, b=0), _request(now_ms=SEP + 2 * 86_400_000 + 50_000))
        self.assertEqual(out["action"], "hold")
        self.assertEqual(out["state"], "unexplained_excess")
        self.assertEqual(out["quality"], "comparable")

    def test_reserves_and_allowance_prevent_false_excess(self):
        # L=80M, R=10M, B=0, allowance=5M, tau(80M)=800000 ->
        # threshold 95_800_000. P one below: no hold (covered by
        # R/allowance); +1: hold.
        out = _eval(self._ctx(95_799_999, r=10_000_000, b=0,
                              allowance=5_000_000), _request(now_ms=SEP + 2 * 86_400_000 + 50_000))
        self.assertNotEqual(out["action"], "hold")
        out = _eval(self._ctx(95_800_001, r=10_000_000, b=0,
                              allowance=5_000_000), _request(now_ms=SEP + 2 * 86_400_000 + 50_000))
        self.assertEqual(out["action"], "hold")

    def test_unknown_boundary_disables_the_check(self):
        # B unknown (None, no proof of an empty boundary): the gross
        # excess test is disabled - no hold even far above L + R.
        out = _eval(self._ctx(200_000_000, b=None), _request(now_ms=SEP + 2 * 86_400_000 + 50_000))
        self.assertNotEqual(out["action"], "hold")
        self.assertEqual(out["state"], "temporally_unresolved")
        self.assertEqual(out["reason"], "snapshot_skew")

    def test_proved_empty_boundary_allows_the_check(self):
        # B=0 with a boundary proof (proved empty): the check runs.
        out = _eval(self._ctx(80_800_001, b=0), _request(now_ms=SEP + 2 * 86_400_000 + 50_000))
        self.assertEqual(out["action"], "hold")


class BillingConflictedLatestGross(unittest.TestCase):
    """billing_conflicted_latest_gross: the fresh latest value runs the
    gross check FIRST (its own receipt guards, not the retained
    high-water) and can hold comparable without a certificate; a
    non-excessive latest value falls through to the latched conflict
    ALERT, which the evaluation preserves (never cleared)."""

    def _ctx(self, p, l=50_000_000, r=0, allowance=0):
        # A store-accepted chain whose latest value carries the conflict
        # latch (as the 2b-1 reduce records it: a newer observation below
        # the high-water).
        prov_a = _provider(100_000_000, False, SEP + 1 * 86_400_000,
                           _eid("prov-a"))
        prov_b = dict(prov_a, event_id=_eid("prov-b"), observed_at=SEP + 2 * 86_400_000)
        prov_b["payload"] = dict(prov_a["payload"], total_micro_eur=p)
        # Its OWN receipt (a shallow copy would keep prov_a's day-old one).
        prov_b["received_at"] = SEP + 2 * 86_400_000 + 1000
        ev = _eid("snap")
        prov_obs = SEP + 2 * 86_400_000
        snap_obs = prov_obs + 40_000
        be = _eid("boundary")
        return _context(
            readings=[
                prov_a, prov_b,
                # A proved-empty boundary (B=0) lets the gross check run;
                # it precedes the ledger snapshot in the chain.
                _snapshot(0, prov_obs, be),
                _snapshot(l, snap_obs, ev, reserved=r),
            ],
            policy=dict(_POLICY, non_ai_allowance_micro_eur=allowance),
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [ev],
                       "reconciliation_required": False}],
            boundary_exposure_micro_eur=0,
            boundary_event_ids=[be],
        )

    def test_non_excessive_latest_yields_conflict_alert(self):
        # high-water 100, latest 50 = L: no excess -> the conflict row.
        out = _eval(self._ctx(50_000_000), _request(now_ms=SEP + 2 * 86_400_000 + 50_000))
        self.assertEqual(out["state"], "conflict")
        self.assertEqual(out["reason"], "source_conflict")
        self.assertEqual(out["action"], "alert")
        self.assertEqual(out["quality"], "provisional")

    def test_excessive_latest_holds_comparable_and_preserves_conflict(self):
        # latest 80, L=50, R/B/allowance=0: 80 > 50 + tau(50M)=50_050_000
        # -> a comparable HOLD without a certificate; the conflict is
        # preserved (diagnostics keep it, the action is the hold).
        out = _eval(self._ctx(80_000_000), _request(now_ms=SEP + 2 * 86_400_000 + 50_000))
        self.assertEqual(out["action"], "hold")
        self.assertEqual(out["state"], "unexplained_excess")
        self.assertEqual(out["reason"], "provider_excess")
        self.assertEqual(out["quality"], "comparable")
        # The latched conflict is still reported, not cleared.
        self.assertEqual(out["conflict_latched"], True)


class BillingLatestChainSeq(unittest.TestCase):
    """billing_latest_chain_seq: the latest ledger value is the highest
    seq of the STORE-SELECTED current chain (observed_at is only
    freshness, not ordering); another identity/chain (a moved alias that
    reuses the epoch number) cannot supply the current amounts."""

    def _ctx(self, chain_events, extra=(), chain=None):
        chain = chain or []
        return _context(
            readings=list(chain_events) + list(extra),
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": list(chain),
                       "reconciliation_required": False}],
        )

    def test_highest_seq_in_current_chain_wins(self):
        # Shuffled times: the highest-seq row in the chain (seq 30 at
        # t=1d) is the latest, not the newest-observed one (seq 20 at
        # t=2d). L_now uses seq 30's settled (100).
        t1 = SEP + 1 * 86_400_000
        t2 = SEP + 2 * 86_400_000
        ctx = self._ctx(
            [_snapshot(60_000_000, t2, _eid("s20"), seq=20),
             _snapshot(100_000_000, t1, _eid("s30"), seq=30)],
            chain=[_eid("s20"), _eid("s30")],
        )
        out = _eval(ctx, _request())
        # The floor diagnostic reflects the chain-latest settled (100).
        self.assertEqual(out["known_floor_micro_eur"], 100_000_000)

    def test_other_chain_cannot_supply_amounts(self):
        # A row outside the store-selected chain (a moved alias reusing
        # its epoch number) carries no current amount: with no
        # reconciliation the current identity has no chain, so L_now is
        # unknown (known_floor=null), not zero-filled.
        t1 = SEP + 1 * 86_400_000
        ctx = self._ctx(
            [_snapshot(90_000_000, t1, _eid("foreign"), seq=99, epoch=7)],
            chain=[],  # the chain is empty: no current identity
        )
        out = _eval(ctx, _request())
        self.assertIsNone(out["known_floor_micro_eur"])

    def test_lower_seq_replay_is_not_latest(self):
        # A replayed lower seq (same id, re-imported later) cannot move
        # the latest: the chain's highest seq still wins.
        t1 = SEP + 1 * 86_400_000
        t2 = SEP + 3 * 86_400_000
        ctx = self._ctx(
            [_snapshot(100_000_000, t1, _eid("s5"), seq=5),
             _snapshot(40_000_000, t2, _eid("s3"), seq=3)],
            chain=[_eid("s5"), _eid("s3")],
        )
        out = _eval(ctx, _request())
        self.assertEqual(out["known_floor_micro_eur"], 100_000_000)


class BillingGatewayUncertainty(unittest.TestCase):
    """billing_gateway_uncertainty: absent/null/stale (>120s)/decreased
    in-chain ledger or a reconciliation-required gateway make the ledger
    unusable (admission_basis=unavailable, headroom=null); a valid
    report counts unattributed once (inside settled, never again)."""

    def _out(self, **ctx_over):
        ev = _eid("snap")
        prov_obs = SEP + 2 * 86_400_000
        ctx = _context(
            readings=[
                _provider(50_000_000, False, prov_obs, _eid("prov")),
                _snapshot(40_000_000, prov_obs + 40_000, ev),
            ],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [ev],
                       "reconciliation_required": False}],
        )
        ctx.update(ctx_over)
        return _eval(ctx, _request())

    def _assert_unusable(self, out):
        self.assertEqual(out["admission_basis"], "unavailable")
        self.assertIsNone(out["headroom_micro_eur"])
        self.assertIsNone(out["request_fits"])

    def test_absent_snapshot_is_unusable(self):
        be = _eid("boundary")
        out = self._out(
            readings=[_provider(50_000_000, False, SEP + 1),
                      _snapshot(0, SEP + 1, be)],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [],
                       "reconciliation_required": False}],
            boundary_exposure_micro_eur=0,
            boundary_event_ids=[be])
        self._assert_unusable(out)

    def test_stale_snapshot_is_unusable(self):
        ev = _eid("snap")
        prov_obs = SEP + 2 * 86_400_000
        ctx = _context(
            readings=[
                _provider(50_000_000, False, prov_obs, _eid("prov")),
                _snapshot(40_000_000, prov_obs + 121_000, ev),
            ],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [ev],
                       "reconciliation_required": False}],
        )
        self._assert_unusable(_eval(ctx, _request()))

    def test_decreased_in_chain_total_is_unusable(self):
        # A decreased in-chain total (nondecreasing is a chain invariant)
        # makes the ledger unusable.
        t1 = SEP + 1 * 86_400_000
        t2 = SEP + 2 * 86_400_000
        ctx = _context(
            readings=[
                _provider(50_000_000, False, t2 + 1000, _eid("prov")),
                _snapshot(60_000_000, t1, _eid("s2"), seq=2),
                _snapshot(40_000_000, t2, _eid("s3"), seq=3),
            ],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [_eid("s2"), _eid("s3")],
                       "reconciliation_required": False}],
        )
        self._assert_unusable(_eval(ctx, _request()))

    def test_reconciliation_required_gateway_is_unusable(self):
        ev = _eid("snap")
        prov_obs = SEP + 2 * 86_400_000
        ctx = _context(
            readings=[
                _provider(50_000_000, False, prov_obs, _eid("prov")),
                _snapshot(40_000_000, prov_obs + 40_000, ev),
            ],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [ev],
                       "reconciliation_required": True}],
        )
        self._assert_unusable(_eval(ctx, _request()))

    def test_unattributed_counted_once(self):
        # A valid report with reserved/uncertain: R_now = reserved +
        # uncertain; unattributed (a subset of settled) is never added
        # again. The floor diagnostic uses settled only.
        ev = _eid("snap")
        prov_obs = SEP + 2 * 86_400_000
        ctx = _context(
            readings=[
                _provider(45_000_000, False, prov_obs, _eid("prov")),
                _snapshot(40_000_000, prov_obs + 40_000, ev, reserved=5_000_000,
                          uncertain=1_000_000, unattributed=2_000_000),
            ],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [ev],
                       "reconciliation_required": False}],
        )
        out = _eval(ctx, _request())
        # settled is 40M; the 2M unattributed inside it is not added.
        self.assertEqual(out["known_floor_micro_eur"], 40_000_000)


class BillingReconcileStatePrecedence(unittest.TestCase):
    """billing_reconcile_state_precedence: a transition listed in
    unresolved_event_ids leaves its gateway unusable (a repeated ok
    report does not clear it); once the store reconciles it away (id
    absent), it does not re-latch."""

    def _ctx(self, unresolved):
        ev = _eid("snap")
        prov_obs = SEP + 2 * 86_400_000
        return _context(
            readings=[
                _provider(50_000_000, False, prov_obs, _eid("prov")),
                _snapshot(40_000_000, prov_obs + 40_000, ev),
            ],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [ev],
                       "reconciliation_required": False}],
            unresolved_event_ids=unresolved,
        )

    def test_listed_transition_keeps_gateway_unusable(self):
        # While the transition id is listed, the gateway is unusable even
        # with an ordinary ok snapshot (no evaluator writes to clear it).
        out = _eval(self._ctx([_eid("transition")]), _request())
        self.assertEqual(out["admission_basis"], "unavailable")
        self.assertIsNone(out["headroom_micro_eur"])

    def test_cleared_transition_does_not_re_latch(self):
        # After the store reconciliation drops the id, the gateway is
        # usable again (the evaluator never re-infers from history).
        ev = _eid("snap")
        prov_obs = SEP + 2 * 86_400_000
        ctx = self._ctx([])
        out = _eval(ctx, _request())
        # The ledger is usable; the turn-3 rows decide the rest.
        self.assertEqual(out["known_floor_micro_eur"], 40_000_000)


class BillingMidnightExposure(unittest.TestCase):
    """billing_midnight_exposure: B (the previous-period boundary
    exposure) is added while it is known, before/after the lag window;
    unknown B cannot become 0; B never shrinks when old attempts settle.
    P > R + L + B + allowance + tau still holds."""

    def _ctx(self, p, b, now, b_refs=()):
        ev = _eid("snap")
        prov_obs = SEP + 2 * 86_400_000
        readings = [
            _provider(p, False, prov_obs, _eid("prov")),
        ]
        # A known boundary is backed by a store boundary record (a
        # snapshot reading the reference points at); it precedes the
        # ledger snapshot so the chain's latest stays the ledger one.
        if b is not None and not b_refs:
            be = _eid("boundary")
            readings.append(_snapshot(0, prov_obs, be))
            b_refs = [be]
        readings.append(_snapshot(50_000_000, prov_obs + 40_000, ev))
        return _context(
            readings=readings,
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [ev],
                       "reconciliation_required": False}],
            boundary_exposure_micro_eur=b,
            boundary_event_ids=list(b_refs),
        ), _request(now_ms=now)

    def test_known_boundary_prevents_false_excess(self):
        # B=0.20 EUR, P=0.20 EUR, L=R=0: P (200000) is NOT above
        # L+R+B+allowance+tau = 0+0+200000+0+50000 = 250000 -> no hold.
        now = SEP + 1 * 86_400_000
        out, req = self._ctx(200_000, 200_000, now)
        self.assertNotEqual(_eval(out, req)["action"], "hold")

    def test_excess_still_holds_with_boundary(self):
        # P = L + R + B + allowance + tau + 1 -> hold even with B known.
        # L=50M, B=200000, tau(50M)=500000 -> threshold 50_700_000;
        # P at +1.
        now = SEP + 1 * 86_400_000
        ctx, req = self._ctx(50_700_001, 200_000, now)
        self.assertEqual(_eval(ctx, req)["action"], "hold")

    def test_unknown_boundary_never_becomes_zero(self):
        # B unknown: the gross check is disabled (never B=0), so even a
        # large P does not hold - it is a provisional unresolved result.
        now = SEP + 1 * 86_400_000
        ctx, req = self._ctx(200_000_000, None, now)
        out = _eval(ctx, req)
        self.assertNotEqual(out["action"], "hold")
        self.assertEqual(out["reason"], "snapshot_skew")


class BillingReconcileSnapshotSkew(unittest.TestCase):
    """billing_reconcile_snapshot_skew; billing_reconcile_busy: an old
    current report makes the as-of bracket unusable (skew, unresolved);
    with valid brackets a true 3 EUR excess still holds despite continued
    spend (busy)."""

    def test_old_report_is_skew_not_excess(self):
        # L_asof bracket fails (the upper snapshot is >120s from the
        # certified time): temporally_unresolved/snapshot_skew, no hold.
        ev = _eid("snap")
        prov_obs = SEP + 2 * 86_400_000
        ctx = _context(
            readings=[
                _provider(13_000_000, False, prov_obs, _eid("prov")),
                _snapshot(10_000_000, prov_obs - 130_000, ev),
            ],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [ev],
                       "reconciliation_required": False}],
        )
        out = _eval(ctx, _request(now_ms=prov_obs + 50_000))
        self.assertEqual(out["state"], "temporally_unresolved")
        self.assertEqual(out["reason"], "snapshot_skew")
        self.assertEqual(out["action"], "none")

    def test_valid_brackets_catch_true_excess(self):
        # A certified gap of 3 EUR (P - L_asof = 300000 > tau(10M)=50000)
        # is a HOLD even though the ledger kept spending (busy).
        t_upper = SEP + 2 * 86_400_000
        t_lower = t_upper - 30_000
        ev_u, ev_l = _eid("upper"), _eid("lower")
        be = _eid("boundary")
        ctx = _context(
            readings=[
                _provider(13_000_000, False, t_upper + 20_000, _eid("prov")),
                # A proved-empty boundary (B=0) for the certified check.
                _snapshot(0, t_upper, be),
                _snapshot(13_000_000, t_upper + 40_000, ev_u, seq=2),
                _snapshot(10_000_000, t_lower, ev_l, seq=1),
            ],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [ev_u, ev_l],
                       "reconciliation_required": False}],
            boundary_exposure_micro_eur=0,
            boundary_event_ids=[be],
        )
        out = _eval(ctx, _request(
            now_ms=t_upper + 50_000,
            coverage=[{"event_id": _eid("prov"), "upper_at": t_upper,
                       "complete_at": t_lower,
                       "overlap_floor_micro_eur": None,
                       "matched_provenance": True}]))
        self.assertEqual(out["action"], "hold")
        self.assertEqual(out["state"], "unexplained_excess")


class BillingReconcileUncertainOverlap(unittest.TestCase):
    """billing_reconcile_uncertain_overlap; billing_coverage: an
    uncertified (no complete_at) lastUpdate is unresolved - covered or
    unresolved, never an excess HOLD; a verified P above
    U_asof + allowance + tau yields the (advisory) HOLD."""

    def test_uncertified_lastupdate_is_unresolved(self):
        # No complete_at: the bracket is unprovable -> unresolved, no
        # excess hold (a missing certificate alone is not anomalous).
        ev = _eid("snap")
        prov_obs = SEP + 2 * 86_400_000
        ctx = _context(
            readings=[
                _provider(19_000_000, False, prov_obs, _eid("prov")),
                _snapshot(18_430_000, prov_obs + 40_000, ev),
            ],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [ev],
                       "reconciliation_required": False}],
        )
        out = _eval(ctx, _request(now_ms=prov_obs + 60_000,
                                  coverage=[{
                                      "event_id": _eid("prov"),
                                      "upper_at": prov_obs,
                                      "complete_at": None,
                                      "overlap_floor_micro_eur": None,
                                      "matched_provenance": False}]))
        self.assertNotEqual(out["action"], "hold")
        self.assertIn(out["state"], ("temporally_unresolved",))

    def test_verified_excess_beyond_upper_is_hold(self):
        # Certified U_asof (reserved) + settled: P above
        # U_asof + allowance + tau is the advisory HOLD.
        t_upper = SEP + 2 * 86_400_000
        t_lower = t_upper - 20_000
        ev_u, ev_l = _eid("upper"), _eid("lower")
        be = _eid("boundary")
        ctx = _context(
            readings=[
                _provider(25_000_000, False, t_upper + 10_000, _eid("prov")),
                # A proved-empty boundary (B=0) for the certified check.
                _snapshot(0, t_upper, be),
                _snapshot(18_000_000, t_upper, ev_u, seq=2,
                          reserved=570_000),
                _snapshot(17_900_000, t_lower, ev_l, seq=1),
            ],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [ev_u, ev_l],
                       "reconciliation_required": False}],
            boundary_exposure_micro_eur=0,
            boundary_event_ids=[be],
        )
        out = _eval(ctx, _request(
            now_ms=t_upper + 40_000,
            coverage=[{"event_id": _eid("prov"), "upper_at": t_upper,
                       "complete_at": t_lower,
                       "overlap_floor_micro_eur": None,
                       "matched_provenance": True}]))
        self.assertEqual(out["action"], "hold")
        self.assertEqual(out["state"], "unexplained_excess")


class BillingReconcileLagTimer(unittest.TestCase):
    """billing_reconcile_equal_idle; billing_reconcile_lag_timer: a
    material negative gap (P < L_now - tau) alerts at >= lag_seconds of
    read-only idle (no stored amount/epoch change since); equality never
    starts the timer; fresh activity keeps it matched/none. The timer
    is derived from stored history - no writes, no report-arrival."""

    def _ctx(self, p, l, last_change, now):
        # The ledger idles since last_change; the provider sample stays
        # fresh (source time now - 1h) so the STALE row cannot shadow the
        # lag row: the lag is about the ledger, not the feed.
        ev = _eid("snap")
        be = _eid("boundary")
        return _context(
            readings=[
                _provider(p, False, now - 3600_000, _eid("prov")),
                # A proved-empty boundary (B=0); it precedes the ledger
                # snapshot so the chain's latest stays the ledger one.
                _snapshot(0, last_change - 10_000, be),
                _snapshot(l, last_change, ev),
            ],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [ev],
                       "reconciliation_required": False}],
            boundary_exposure_micro_eur=0,
            boundary_event_ids=[be],
        ), _request(now_ms=now)

    def test_material_gap_alerts_at_lag(self):
        # P = L - 1 EUR (material: 1M > tau(10M)=50000) and idle since
        # the last stored change for >= lag_seconds (14400s) -> alert.
        last = SEP + 1 * 86_400_000
        # Past lag_seconds and past max_age: the sample stays fresh, so
        # the lag row (not the stale row) decides.
        now = last + 14405_000
        ctx, req = self._ctx(9_000_000, 10_000_000, last, now)
        out = _eval(ctx, req)
        self.assertEqual(out["action"], "alert")
        self.assertEqual(out["reason"], "lag_investigate")
        self.assertEqual(out["state"], "provider_behind")

    def test_alerts_at_exactly_lag(self):
        # The boundary itself: idle time EXACTLY lag_seconds alerts
        # (>=, not >).
        last = SEP + 1 * 86_400_000
        now = last + 14400_000
        ctx, req = self._ctx(9_000_000, 10_000_000, last, now)
        out = _eval(ctx, req)
        self.assertEqual(out["action"], "alert")
        self.assertEqual(out["reason"], "lag_investigate")

    def test_gap_before_lag_is_not_yet_alert(self):
        # The same gap but the idle time has not reached lag_seconds:
        # no alert (falls through to the provisional catch-all).
        last = SEP + 1 * 86_400_000
        now = last + 14400_000 - 1000
        ctx, req = self._ctx(9_000_000, 10_000_000, last, now)
        out = _eval(ctx, req)
        self.assertEqual(out["action"], "none")

    def test_equality_never_starts_the_timer(self):
        # P == L: no material gap, no alert at any idle duration.
        last = SEP + 1 * 86_400_000
        now = last + 2 * 14400_000
        ctx, req = self._ctx(10_000_000, 10_000_000, last, now)
        out = _eval(ctx, req)
        self.assertNotEqual(out["reason"], "lag_investigate")

    def test_recent_activity_resets_idle(self):
        # A stored amount/epoch change inside the lag window resets the
        # timer: the material gap does not alert yet.
        last = SEP + 1 * 86_400_000
        now = last + 1000_000  # < lag_seconds since the last change
        ctx, req = self._ctx(9_000_000, 10_000_000, last, now)
        out = _eval(ctx, req)
        self.assertEqual(out["action"], "none")


class BillingTolerance(unittest.TestCase):
    """billing_tolerance: tau(L)=max(abs, ceil(L*rel/10000)); the
    absolute floor dominates at low totals; tolerance is never a budget
    credit (it only widens the comparison, not the cap)."""

    def _ctx(self, p, l):
        ev = _eid("snap")
        prov_obs = SEP + 2 * 86_400_000
        be = _eid("boundary")
        return _context(
            readings=[
                _provider(p, False, prov_obs, _eid("prov")),
                # A proved-empty boundary (B=0) lets the gross check run;
                # it precedes the ledger snapshot in the chain.
                _snapshot(0, prov_obs, be),
                _snapshot(l, prov_obs + 40_000, ev),
            ],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [ev],
                       "reconciliation_required": False}],
            boundary_exposure_micro_eur=0,
            boundary_event_ids=[be],
        )

    def test_absolute_floor_at_low_totals(self):
        # L = 20 EUR (20_000_000): rel component = ceil(20M * 100/10000)
        # = 200_000; abs = 50_000 -> tau = 200_000 (0.20 EUR). P at
        # L + 199_999: within tolerance (no hold); P at L + 200_001: hold.
        out = _eval(self._ctx(20_199_999, 20_000_000),
                    _request(now_ms=SEP + 2 * 86_400_000 + 50_000))
        self.assertNotEqual(out["action"], "hold")
        out = _eval(self._ctx(20_200_001, 20_000_000),
                    _request(now_ms=SEP + 2 * 86_400_000 + 50_000))
        self.assertEqual(out["action"], "hold")

    def test_tolerance_is_not_a_budget_credit(self):
        # Tolerance widens the excess comparison only: the cap (100 EUR)
        # is untouched - a request fitting the cap but exceeding the
        # tolerance still holds.
        out = _eval(self._ctx(20_200_001, 20_000_000),
                    _request(requested_micro_eur=10, now_ms=SEP + 2 * 86_400_000 + 50_000))
        self.assertEqual(out["action"], "hold")


if __name__ == "__main__":
    unittest.main()
