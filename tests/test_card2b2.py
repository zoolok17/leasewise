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

The reconciliation rows (gross excess, conflict, stale, matched, lag,
midnight, historical excess) and the headroom math land in turns 3/4;
this turn's output shape is complete and the money fields fail closed
(admission_basis=unavailable, headroom=null, request_fits=null) so nothing
is ever admitted on an un-evaluated estimate.
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


def _snapshot(settled, observed, event, gateway="g1"):
    return {
        "event_id": event, "machine": "desktop", "account": "meter",
        "kind": "gateway_snapshot", "observed_at": observed,
        "received_at": observed + 1000,
        "payload": {
            "gateway": gateway, "ledger_epoch": 1, "seq": 1,
            "period_start": SEP, "period_end": OCT, "policy_hashes": None,
            "sized_ready": False, "reconciliation_required": False,
            "settled_micro_eur": settled, "reserved_micro_eur": 0,
            "uncertain_micro_eur": 0, "unattributed_micro_eur": 0,
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
             "current_chain_event_ids": []},
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
                                   "current_chain_event_ids": [_eid("ghost")]}])
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
        ctx = _context(
            readings=[
                _provider(80_000_000, False, SEP + 86_400_000, _eid("prov")),
                _snapshot(50_000_000, SEP + 1 * 86_400_000, evA),
                _snapshot(60_000_000, SEP + 2 * 86_400_000, evB),
            ],
            gateways=[{"gateway": "g1", "share_micro_eur": 60_000_000,
                       "current_chain_event_ids": [evA, evB]}],
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
                       "current_chain_event_ids": [evA, evB]}],
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
        # A provider sample with no non-AI flag and (turn 2) no other
        # anomaly: the provisional catch-all, action none.
        ctx = _context(readings=[_provider(45_000_000, False, SEP + 1)])
        out = _eval(ctx, _request())
        self.assertEqual(out["action"], "none")
        self.assertEqual(out["quality"], "provisional")

    def test_non_ai_flag_raises_alert(self):
        ctx = _context(readings=[_provider(45_000_000, True, SEP + 1)])
        out = _eval(ctx, _request())
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


if __name__ == "__main__":
    unittest.main()
