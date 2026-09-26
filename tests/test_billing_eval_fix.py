"""Blind-review counterexamples, through the public pure evaluator."""
import copy
import itertools
import unittest

from quota_balancer.billing_eval import evaluate_billing
from quota_balancer.errors import QuotaError
from test_card2b2 import _context, _request, _snapshot, _provider, _eid, SEP

E = 1_000_000
T = SEP + 2 * 86_400_000
MAX = 2**63 - 1
OUTPUT = {'state', 'action', 'reason', 'quality', 'admission_basis',
          'known_floor_micro_eur', 'estimate_micro_eur', 'headroom_micro_eur',
          'request_fits', 'proposal'}


def snap(amount=50*E, at=T+40_000, seq=1, **kw):
    return _snapshot(amount, at, _eid(f's-{at}-{seq}'), seq=seq, **kw)


def cert(p, upper=T, complete=T, floor=None, proof=True):
    return dict(event_id=p['event_id'], upper_at=upper, complete_at=complete,
                overlap_floor_micro_eur=floor, matched_provenance=proof)


def case(samples=None, providers=None, now=T+60_000, coverage=None):
    samples = [snap()] if samples is None else samples
    providers = [] if providers is None else providers
    ctx = _context(readings=samples + providers, boundary_exposure_micro_eur=0,
                   gateways=[dict(gateway='g1', share_micro_eur=60*E,
                                  current_chain_event_ids=[s['event_id'] for s in samples])])
    return ctx, _request(now_ms=now, coverage=coverage or [])


class BillingReviewRegressions(unittest.TestCase):
    def reject(self, ctx, req):
        with self.assertRaises(QuotaError) as caught:
            evaluate_billing(ctx, req)
        self.assertEqual(caught.exception.code, 'INVALID_CONFIG')

    def test_c12_exact_spec_input_output(self):
        ctx, req = case()
        ctx['gateways'][0].pop('reconciliation_required', None)
        self.assertEqual(set(evaluate_billing(ctx, req)), OUTPUT)

    def test_c14_boolean_version(self):
        ctx, req = case(); req['version'] = True
        self.reject(ctx, req)

    def test_c14_unknown_unresolved_reference(self):
        ctx, req = case(); ctx['unresolved_event_ids'] = [_eid('ghost')]
        self.reject(ctx, req)

    def test_c14_null_payload_typed_error(self):
        ctx, req = case(); ctx['readings'][0]['payload'] = None
        self.reject(ctx, req)

    def test_s6_duplicate_gateway_rejected(self):
        ctx, req = case(); ctx['gateways'] *= 2
        self.reject(ctx, req)

    def test_chain_reference_wrong_gateway_or_kind(self):
        for kind in ('provider', 'other_gateway'):
            with self.subTest(kind=kind):
                ctx, req = case()
                r = _provider(E, False, T) if kind == 'provider' else snap(gateway='g2')
                ctx['readings'] = [r]
                ctx['gateways'][0]['current_chain_event_ids'] = [r['event_id']]
                self.reject(ctx, req)

    def test_duplicate_event_collision_rejected(self):
        ctx, req = case()
        ctx['readings'].append(copy.deepcopy(ctx['readings'][0]))
        ctx['readings'][1]['payload']['settled_micro_eur'] += 1
        self.reject(ctx, req)

    def test_c1_convert_ledger_before_fallback(self):
        ctx, req = case(); ctx['policy'].update(cap_basis='invoice_total', vat_bps=2000)
        req['requested_micro_eur'] = 45*E
        out = evaluate_billing(ctx, req)
        self.assertEqual((out['headroom_micro_eur'], out['request_fits']), (40*E, False))

    def test_s3_convert_provider_before_gross(self):
        p = _provider(59_500_000, False, T)
        ctx, req = case(providers=[p]); ctx['policy'].update(provider_basis='invoice_total', vat_bps=1900)
        out = evaluate_billing(ctx, req)
        self.assertEqual((out['action'], out['known_floor_micro_eur']), ('none', 50*E))

    def test_c1_basis_unknown_precedes_scope_and_gateway(self):
        for basis in ('cap_basis', 'provider_basis', 'ledger_basis'):
            with self.subTest(basis=basis):
                ctx, req = case(samples=[], providers=[_provider(80*E, False, T)])
                ctx['policy'][basis] = 'unknown'
                ctx['readings'][0]['payload']['scope_alias'] = 'other'
                out = evaluate_billing(ctx, req)
                self.assertEqual((out['reason'], out['quality'], out['headroom_micro_eur']),
                                 ('basis_unknown', 'unknown', None))

    def test_missing_vat_defers_comparison(self):
        ctx, req = case(); ctx['policy']['ledger_basis'] = 'invoice_total'
        out = evaluate_billing(ctx, req)
        self.assertEqual((out['reason'], out['estimate_micro_eur'], out['request_fits']),
                         ('basis_unknown', None, None))

    def test_c1_scope_owner_revision_gates(self):
        for field, value in [('scope_alias', 'other'), ('scope_revision', 7), ('machine', 'other')]:
            with self.subTest(field=field):
                p = _provider(80*E, False, T)
                (p if field == 'machine' else p['payload'])[field] = value
                ctx, req = case(providers=[p])
                out = evaluate_billing(ctx, req)
                self.assertEqual((out['reason'], out['headroom_micro_eur']), ('scope_mismatch', None))

    def test_c2_freshness_gates_both_headrooms(self):
        for certified, age in itertools.product((False, True), (120_000, 120_001)):
            with self.subTest(certified=certified, age=age):
                p = _provider(50*E, False, T)
                s = snap(at=T)
                ctx, req = case([s], [p], now=T+age, coverage=[cert(p)] if certified else [])
                out = evaluate_billing(ctx, req)
                if age == 120_001:
                    self.assertEqual((out['admission_basis'], out['headroom_micro_eur'], out['request_fits']),
                                     ('unavailable', None, None))
                else:
                    self.assertEqual(out['headroom_micro_eur'], 50*E)

    def test_c3_contradictory_same_seq_unusable_both_orders(self):
        rows = [snap(50*E, T, 1), snap(80*E, T+40_000, 1)]
        for ordered in (rows, rows[::-1]):
            ctx, req = case(ordered)
            self.assertEqual(evaluate_billing(ctx, req)['reason'], 'gateway_unusable')

    def test_c3_epoch_hash_machine_continuity(self):
        for field, value in [('ledger_epoch', 2), ('policy_hashes', {'price': 'a'*64}), ('machine', 'worker')]:
            with self.subTest(field=field):
                rows = [snap(40*E, T, 1), snap(50*E, T+40_000, 2)]
                # policy hashes use the accepted price/admission envelope.
                if field == 'policy_hashes':
                    value = {'price': 'a'*64, 'child_cap': 'b'*64}
                (rows[1] if field == 'machine' else rows[1]['payload'])[field] = value
                ctx, req = case(rows)
                self.assertEqual(evaluate_billing(ctx, req)['reason'], 'gateway_unusable')

    def test_c4_upper_bracket_not_lower_or_complete(self):
        p = _provider(13*E, False, T)
        rows = [snap(10*E, T-30_000, 1), snap(13*E, T+40_000, 2)]
        ctx, req = case(rows, [p], coverage=[cert(p, complete=T-30_000)])
        self.assertNotEqual(evaluate_billing(ctx, req)['action'], 'hold')

    def test_c4_exact_at_upper_time_counts(self):
        p = _provider(13*E, False, T)
        ctx, req = case([snap(10*E, T, 1), snap(14*E, T+40_000, 2)], [p],
                        coverage=[cert(p, complete=None)])
        self.assertEqual(evaluate_billing(ctx, req)['action'], 'hold')

    def test_s5_upper_only_certificate_holds(self):
        p = _provider(13*E, False, T)
        rows = [snap(10*E, T-10_000, 1), snap(10*E, T+10_000, 2), snap(14*E, T+40_000, 3)]
        ctx, req = case(rows, [p], coverage=[cert(p, complete=None)])
        self.assertEqual(evaluate_billing(ctx, req)['action'], 'hold')

    def test_c5_precedence_all_events_both_orders(self):
        p1 = _provider(15*E, False, T-500_000, _eid('p1'))
        p2 = _provider(20*E, False, T, _eid('p2'))
        rows = [snap(10*E, T-500_000, 1, reserved=10*E), snap(10*E, T, 2), snap(20*E, T+40_000, 3)]
        for ps, cs in itertools.product(([p1, p2], [p2, p1]), ([cert(p1, T-500_000, None), cert(p2, T, None)], [cert(p2, T, None), cert(p1, T-500_000, None)])):
            ctx, req = case(rows, ps, coverage=cs)
            self.assertEqual((evaluate_billing(ctx, req)['action'], evaluate_billing(ctx, req)['request_fits']), ('hold', False))

    def test_c6_no_maximum_receipt_gap(self):
        p = _provider(80*E, False, T)
        ctx, req = case([snap(at=T+600_000)], [p], now=T+601_000)
        self.assertEqual(evaluate_billing(ctx, req)['action'], 'hold')

    def test_mutant_receipt_margin_exact_boundary(self):
        p = _provider(80*E, False, T)
        for offset, action in ((29_999, 'none'), (30_000, 'hold')):
            with self.subTest(offset=offset):
                ctx, req = case([snap(at=p['received_at']+offset)], [p])
                self.assertEqual(evaluate_billing(ctx, req)['action'], action)

    def test_c7_unchanged_heartbeats_do_not_reset_idle(self):
        for elapsed, action in ((14_399_999, 'none'), (14_400_000, 'alert')):
            with self.subTest(elapsed=elapsed):
                now = T + elapsed
                p = _provider(45*E, False, now-60_000)
                ctx, req = case([snap(at=T), snap(at=now-1000, seq=2)], [p], now=now)
                out = evaluate_billing(ctx, req)
                self.assertEqual(out['action'], action)
                self.assertEqual(out, evaluate_billing(copy.deepcopy(ctx), copy.deepcopy(req)))
                if action == 'alert': self.assertEqual(out['reason'], 'lag_investigate')

    def test_c8_missing_old_spend_during_activity(self):
        p = _provider(45*E, False, T)
        rows = [snap(50*E, T-14_400_000, 1), snap(60*E, T, 2), snap(60*E, T+40_000, 3)]
        ctx, req = case(rows, [p], coverage=[cert(p, complete=None)])
        out = evaluate_billing(ctx, req)
        self.assertEqual((out['reason'], out['action']), ('missing_old_spend', 'alert'))

    def test_c8_matched_and_lag_expected(self):
        for amount, reason in ((50*E, 'matched'), (45*E, 'lag_expected')):
            with self.subTest(amount=amount):
                p = _provider(amount, False, T)
                ctx, req = case([snap(at=T), snap(at=T+40_000, seq=2)], [p], coverage=[cert(p)])
                out = evaluate_billing(ctx, req)
                self.assertEqual(out['reason'], reason)
                if reason == 'matched': self.assertEqual(out['quality'], 'comparable')

    def test_c9_each_overlap_uses_its_own_provider_amount(self):
        old = _provider(10*E, False, T-1000, _eid('old'))
        new = _provider(20*E, False, T, _eid('new'))
        ctx, req = case([snap(20*E, T-1000, 1), snap(20*E, T+40_000, 2)], [old, new],
                        coverage=[cert(old, T-1000, T-1000, 15*E)])
        self.reject(ctx, req)

    def test_c9_explicit_proof_beats_derived_overlap(self):
        p = _provider(45*E, False, T)
        ctx, req = case([snap(at=T), snap(at=T+40_000, seq=2)], [p], coverage=[cert(p, floor=40*E)])
        out = evaluate_billing(ctx, req)
        self.assertEqual((out['estimate_micro_eur'], out['headroom_micro_eur'], out['admission_basis']),
                         (55*E, 45*E, 'billing_estimate'))

    def test_c10_uuid_tie_not_receipt_order(self):
        older = _provider(80*E, False, T, '00000000-0000-4000-8000-000000000001')
        newer = _provider(50*E, False, T, '00000000-0000-4000-8000-000000000002')
        older['received_at'] += 1000
        for ps in ([older, newer], [newer, older]):
            ctx, req = case(providers=ps)
            out = evaluate_billing(ctx, req)
            self.assertEqual((out['reason'], out['action']), ('source_conflict', 'alert'))

    def test_c11_estimate_overflow_rejected(self):
        ctx, req = case([snap(MAX)], [_provider(MAX, False, T)])
        self.reject(ctx, req)

    def test_c11_component_sum_overflow_rejected(self):
        ctx, req = case([snap(0, reserved=MAX, uncertain=1)])
        self.reject(ctx, req)

    def test_mutant_tolerance_rounds_up(self):
        for amount, action in ((152, 'none'), (153, 'hold')):
            with self.subTest(amount=amount):
                ctx, req = case([snap(151)], [_provider(amount, False, T)])
                ctx['policy'].update(abs_tolerance_micro_eur=0, relative_tolerance_bps=1)
                self.assertEqual(evaluate_billing(ctx, req)['action'], action)

    def test_c11_tolerance_large_integer_exact(self):
        ledger = 2**53 + 1
        tau = (ledger * 101 + 9999) // 10000
        ctx, req = case([snap(ledger)], [_provider(ledger+tau, False, T)])
        ctx['policy'].update(abs_tolerance_micro_eur=0, relative_tolerance_bps=101)
        self.assertNotEqual(evaluate_billing(ctx, req)['action'], 'hold')

    def test_c13_known_provider_floor_survives_unusable_ledger(self):
        for rows in ([], [snap()]):
            ctx, req = case(rows, [_provider(80*E, False, T)])
            self.assertEqual(evaluate_billing(ctx, req)['known_floor_micro_eur'], 80*E)

    def test_missing_old_spend_needs_no_nearby_cutoff_snapshot(self):
        # A continuous older settled lower bound remains valid; the 120s
        # bracket constraint is for upper exposure, not this lower bound.
        p = _provider(45*E, False, T)
        rows = [snap(50*E, T-14_400_000-600_000, 1), snap(60*E, T+40_000, 2)]
        ctx, req = case(rows, [p], coverage=[cert(p, complete=None)])
        self.assertEqual(evaluate_billing(ctx, req)['reason'], 'missing_old_spend')

    def test_raw_same_seq_conflict_cannot_round_away(self):
        rows = [snap(101, T+40_000), snap(102, T+40_000)]
        rows[1]['event_id'] = _eid('round-away')
        ctx, req = case(rows)
        ctx['policy'].update(ledger_basis='invoice_total', vat_bps=2000)
        self.assertEqual(evaluate_billing(ctx, req)['reason'], 'gateway_unusable')

    def test_explicit_overlap_without_proof_rejected_on_unusable_ledger(self):
        p = _provider(50*E, False, T)
        ctx, req = case([], [p], coverage=[cert(p, floor=1, proof=False)])
        self.reject(ctx, req)

    def test_selected_legacy_report_does_not_disappear(self):
        rows = [snap(at=T), snap(at=T+40_000, seq=2)]
        for key in ('settled_micro_eur', 'reserved_micro_eur', 'uncertain_micro_eur', 'unattributed_micro_eur'):
            del rows[1]['payload'][key]
        ctx, req = case(rows)
        self.assertEqual(evaluate_billing(ctx, req)['admission_basis'], 'unavailable')

    def test_identical_replays_count_once(self):
        first = snap()
        replay = copy.deepcopy(first); replay['event_id'] = _eid('same-seq-replay')
        ctx, req = case([first, replay])
        self.assertEqual(evaluate_billing(ctx, req)['headroom_micro_eur'], 50*E)
        ctx['readings'].append(copy.deepcopy(first))
        self.assertEqual(evaluate_billing(ctx, req)['headroom_micro_eur'], 50*E)

    def test_reserve_change_resets_idle_but_repeated_reserve_does_not(self):
        now = T+14_400_000
        p = _provider(45*E, False, now-60_000)
        for changed, action in ((False, 'alert'), (True, 'none')):
            rows = [snap(at=T, reserved=1), snap(at=now-1000, seq=2, reserved=2 if changed else 1)]
            ctx, req = case(rows, [p], now=now)
            self.assertEqual(evaluate_billing(ctx, req)['action'], action)


if __name__ == '__main__':
    unittest.main()
