"""Second blind-read counterexamples, tested through the public evaluator."""
import copy
import unittest

from quota_balancer.billing_eval import evaluate_billing
from quota_balancer.errors import QuotaError
from test_billing_eval_fix import E, T, case, cert, snap
from test_card2b2 import _provider


class BillingFixRoundTwo(unittest.TestCase):
    def test_n1_timestamp_ties_use_later_sequence(self):
        p = _provider(12*E, False, T)
        rows = [snap(10*E, T, 1), snap(13*E, T, 2), snap(13*E, T+40_000, 3)]
        for ordered in (rows, rows[::-1]):
            with self.subTest(reversed=ordered == rows[::-1]):
                ctx, req = case(ordered, [p], coverage=[cert(p, complete=None)])
                out = evaluate_billing(ctx, req)
                self.assertEqual((out['reason'], out['action']), ('lag_expected', 'none'))
                self.assertEqual(out['known_floor_micro_eur'], 13*E)

    def test_n1_clock_rollback_never_inverts_historical_bracket(self):
        p = _provider(12*E, False, T)
        rows = [snap(10*E, T+20_000, 1), snap(13*E, T, 2), snap(13*E, T+40_000, 3)]
        for upper, reason in ((T, 'lag_expected'), (T+10_000, 'coverage_unknown')):
            for ordered in (rows, rows[::-1]):
                with self.subTest(upper=upper, reversed=ordered == rows[::-1]):
                    ctx, req = case(ordered, [p], coverage=[cert(p, upper=upper, complete=None)])
                    out = evaluate_billing(ctx, req)
                    self.assertEqual((out['reason'], out['action']), (reason, 'none'))
                    self.assertEqual(out['known_floor_micro_eur'], 13*E)

    def test_n2_raw_reserve_change_breaks_idle_despite_rounding(self):
        now = T+14_400_000
        p = _provider(45*E, False, now-60_000)
        for reserved, action in ((5, 'alert'), (6, 'none')):
            with self.subTest(reserved=reserved):
                rows = [snap(60*E, T, 1, reserved=5),
                        snap(60*E, now-1000, 2, reserved=reserved)]
                ctx, req = case(rows, [p], now=now)
                ctx['policy'].update(ledger_basis='invoice_total', vat_bps=2000)
                before = copy.deepcopy(ctx)
                out = evaluate_billing(ctx, req)
                self.assertEqual(out['action'], action)
                self.assertEqual(out['headroom_micro_eur'], 50*E-5)
                self.assertEqual(ctx, before)

    def test_n3_matched_requires_proved_overlap(self):
        p = _provider(49_900_000, False, T)
        for floor, reason in ((None, 'coverage_unknown'), (49_900_000, 'matched')):
            with self.subTest(floor=floor):
                ctx, req = case([snap(at=T), snap(at=T+40_000, seq=2)], [p],
                                coverage=[cert(p, floor=floor)])
                out = evaluate_billing(ctx, req)
                self.assertEqual(out['reason'], reason)
                self.assertEqual(out['state'], 'temporally_unresolved' if floor is None else 'matched')

    def test_n4_stale_ledger_cannot_hide_invalid_explicit_floor(self):
        for amount, ledger, floor in ((10*E, 20*E, 15*E), (20*E, 10*E, 15*E)):
            for age in (60_000, 200_000):
                with self.subTest(amount=amount, ledger=ledger, age=age):
                    p = _provider(amount, False, T)
                    ctx, req = case([snap(ledger, T)], [p], now=T+age,
                                    coverage=[cert(p, floor=floor)])
                    with self.assertRaises(QuotaError) as caught:
                        evaluate_billing(ctx, req)
                    self.assertEqual(caught.exception.code, 'INVALID_CONFIG')

    def test_n4_known_provider_and_historical_bounds_always_validate(self):
        for samples, amount, floor in (([], 10*E, 15*E),
                ([snap(10*E, T, 1), snap(20*E, T+40_000, 2)], 20*E, 15*E)):
            with self.subTest(samples=len(samples)):
                p = _provider(amount, False, T)
                ctx, req = case(samples, [p], now=T+200_000,
                                coverage=[cert(p, floor=floor)])
                with self.assertRaises(QuotaError) as caught:
                    evaluate_billing(ctx, req)
                self.assertEqual(caught.exception.code, 'INVALID_CONFIG')

    def test_n4_stale_ledger_valid_floor_stays_unavailable(self):
        p = _provider(10*E, False, T)
        ctx, req = case([snap(20*E, T)], [p], now=T+200_000,
                        coverage=[cert(p, floor=10*E)])
        out = evaluate_billing(ctx, req)
        self.assertEqual((out['reason'], out['admission_basis']), ('gateway_unusable', 'unavailable'))


if __name__ == '__main__':
    unittest.main()
