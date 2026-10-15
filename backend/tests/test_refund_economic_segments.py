import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, localcontext

from app.refund_economic_segments import EconomicSegment, plan_economic_segment
from app.refund_planning import OriginalSegment, PlanUnavailable, plan_segment


class EconomicSegmentTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 15, tzinfo=timezone.utc)
        self.segment = EconomicSegment('new', 'order', 'switch', Decimal('100'),
            Decimal('30'), 'api', 200, self.start, self.start + timedelta(days=10),
            {'settlement_rule': {'version': 2}}, {'quota': {'monthly_quota': 999}}, ('switch-proof',))

    def plan(self, amount='10', **kwargs):
        return plan_economic_segment(self.segment, amount, at=kwargs.pop('at', self.start),
                                     used_calls=kwargs.pop('used_calls', 50), **kwargs)

    def test_only_post_switch_calls_and_frozen_remaining_quota(self):
        plan = self.plan('75')
        self.assertEqual((plan['original_quota'], plan['charged_calls'], plan['recovery_delta']), (200, 50, 150))
        self.assertEqual(plan['cost_reversal_delta'], '22.50')
        self.assertEqual(plan['change_id'], 'switch')
        self.assertEqual(plan['evidence_ids'], ['switch-proof'])
        with self.assertRaises(PlanUnavailable):
            self.plan('75.01')
        # Old-version calls cannot be fed into the new segment's charge window.
        with self.assertRaises(PlanUnavailable):
            self.plan(used_calls=250)

    def test_future_segment(self):
        p = self.plan('100', at=self.start - timedelta(days=1), used_calls=0)
        self.assertEqual(p['recovery_delta'], 200)
        with self.assertRaises(PlanUnavailable):
            self.plan(at=self.start - timedelta(seconds=1), used_calls=1)

    def test_time_tail_and_none_vs_zero(self):
        for method in ('api', 'model_api', 'tenant_access'):
            with self.subTest(method=method):
                s = replace(self.segment, method=method, available_quota=None)
                p = plan_economic_segment(s, '50', at=self.start + timedelta(days=5))
                self.assertEqual(p['effective_ends_at'], (self.start + timedelta(days=5)).isoformat())
                self.assertEqual(p['original_duration_microseconds'], 10 * 86400 * 1000000)
                future = plan_economic_segment(s, '100', at=self.start - timedelta(days=1))
                self.assertEqual(future['effective_ends_at'], self.start.isoformat())
                with self.assertRaises(PlanUnavailable):
                    plan_economic_segment(s, '.01', at=s.ends_at)
        with self.assertRaises(PlanUnavailable):
            plan_economic_segment(replace(self.segment, available_quota=0), '1', at=self.start, used_calls=0)
        with self.assertRaises(PlanUnavailable):
            self.plan(used_calls=None)

    def test_cost_cumulative_rounding_even_when_cost_exceeds_revenue(self):
        s = replace(self.segment, revenue=Decimal('.03'), cost=Decimal('.05'), available_quota=3)
        deltas = []
        for prior in range(3):
            p = plan_economic_segment(s, '.01', at=self.start, used_calls=0,
                                     prior_amount=f'.0{prior}', prior_recovery=prior)
            deltas.append(p['cost_reversal_delta'])
        self.assertEqual(deltas, ['0.02', '0.01', '0.02'])
        self.assertEqual(sum(map(Decimal, deltas)), Decimal('.05'))

    def test_partial_holds_and_prior_reconciliation(self):
        p = self.plan('10', prior_amount='20', prior_recovery=40, other_pending='45')
        self.assertEqual((p['refundable_balance_before'], p['recovery_delta']), ('10.00', 20))
        with self.assertRaises(PlanUnavailable):
            self.plan('10.01', prior_amount='20', prior_recovery=40, other_pending='45')
        for recovered in (0, 39, 41, 151):
            with self.assertRaises(PlanUnavailable):
                self.plan(prior_amount='20', prior_recovery=recovered)

    def test_low_precision_large_amount_plus_cent_and_pending(self):
        with localcontext() as context:
            context.prec = 6
            context.rounding = ROUND_DOWN
            s = replace(self.segment, revenue=Decimal('999999999999.99'),
                        cost=Decimal('999999999999.98'), available_quota=99999999999999)
            p = plan_economic_segment(s, '.01', at=self.start, used_calls=0,
                                     prior_amount='999999999999.97',
                                     prior_recovery=99999999999997, other_pending='.01')
            self.assertEqual(p['cumulative_refund_amount'], '999999999999.98')
            self.assertEqual(p['refundable_balance_before'], '0.01')
            self.assertEqual(p['other_pending_amount'], '0.01')
            self.assertEqual(p['recovery_delta'], 1)
            self.assertEqual(p['cost_reversal_delta'], '0.01')
            self.assertEqual(p['cumulative_cost_reversal'], '999999999999.97')
            with self.assertRaises(PlanUnavailable):
                plan_economic_segment(s, '.02', at=self.start, used_calls=0,
                                      prior_amount='999999999999.97',
                                      prior_recovery=99999999999997, other_pending='.01')
            p = plan_economic_segment(s, '.01', at=self.start, used_calls=0,
                                     other_pending='999999999999.98')
            self.assertEqual(p['other_pending_amount'], '999999999999.98')
            self.assertEqual(p['refundable_balance_before'], '0.01')

    def test_low_precision_half_up_cost_is_stable(self):
        with localcontext() as context:
            context.prec = 6
            context.rounding = ROUND_DOWN
            s = replace(self.segment, revenue=Decimal('999999999999.98'),
                        cost=Decimal('999999999999.99'), available_quota=2)
            first = plan_economic_segment(s, '499999999999.99', at=self.start, used_calls=0)
            second = plan_economic_segment(s, '499999999999.99', at=self.start, used_calls=0,
                                          prior_amount='499999999999.99', prior_recovery=1)
            self.assertEqual(first['cost_reversal_delta'], '500000000000.00')
            self.assertEqual(second['cost_reversal_delta'], '499999999999.99')
            self.assertEqual(second['cumulative_cost_reversal'], '999999999999.99')

    def test_exact_money_bounds_and_fraction_validation_in_low_precision(self):
        with localcontext() as context:
            context.prec = 6
            context.rounding = ROUND_DOWN
            for field in ('revenue', 'cost'):
                s = replace(self.segment, **{field: Decimal('999999999999.9900')})
                self.assertEqual(getattr(s, field), Decimal('999999999999.99'))
                for value in ('1000000000000', '999999999999.991', '123456789.001',
                              'NaN', 'sNaN', 'Infinity', '-.01'):
                    with self.subTest(field=field, value=value), self.assertRaises(PlanUnavailable):
                        replace(self.segment, **{field: value})
            for kwargs in ({'prior_amount': '1000000000000'},
                           {'other_pending': '1000000000000'},
                           {'other_pending': '123456789.001'}):
                with self.subTest(kwargs=kwargs), self.assertRaises(PlanUnavailable):
                    self.plan(**kwargs)
            with self.assertRaises(PlanUnavailable):
                self.plan('1000000000000')

    def test_last_cent_and_existing_reconciliation_semantics(self):
        s = replace(self.segment, revenue=Decimal('3'), available_quota=100)
        p = plan_economic_segment(s, '.01', at=self.start, used_calls=1,
                                 prior_amount='2.96', prior_recovery=98)
        self.assertEqual((p['recovery_delta'], p['cumulative_recovery']), (1, 99))
        s = replace(s, revenue=Decimal('.03'), available_quota=10)
        p = plan_economic_segment(s, '.02', at=self.start, used_calls=2)
        self.assertEqual(p['cumulative_recovery'], 8)
        with self.assertRaisesRegex(PlanUnavailable, 'reconciliation'):
            plan_economic_segment(s, '.01', at=self.start, used_calls=0,
                                  prior_amount='.02', prior_recovery=8)

    def test_matches_original_planner_for_equivalent_basis(self):
        old = OriginalSegment('new', 'order', Decimal('100'), 'api', 200, 1,
                              self.start, self.segment.ends_at, {}, {})
        for amount, prior, recovered, pending in [('10', '0', 0, '5'), ('55', '20', 40, '0')]:
            a = self.plan(amount, prior_amount=prior, prior_recovery=recovered, other_pending=pending)
            b = plan_segment(old, amount, at=self.start, used_calls=50,
                             prior_amount=prior, prior_recovery=recovered, other_pending=pending)
            for key in ('recovery_delta', 'cumulative_recovery', 'refundable_balance_before'):
                self.assertEqual(a[key], b[key])

    def test_snapshots_are_frozen_and_outputs_detached(self):
        source = {'nested': [1, {'value': 2}]}
        s = replace(self.segment, economic_snapshot=source)
        source['nested'][1]['value'] = 3
        with self.assertRaises(FrozenInstanceError):
            s.cost = Decimal('0')
        with self.assertRaises(TypeError):
            s.economic_snapshot['nested'][1]['value'] = 4
        p = plan_economic_segment(s, '1', at=self.start, used_calls=0)
        p['economic_snapshot']['nested'][1]['value'] = 5
        self.assertEqual(s.economic_snapshot['nested'][1]['value'], 2)

    def test_invalid_segment_inputs_fail_closed(self):
        invalid = {'segment_id': ['', None, ' '], 'source_order_id': [''], 'change_id': [''],
            'revenue': ['0', '-1', 'NaN', '1.001'], 'cost': ['-1', 'Infinity', '.001'],
            'method': ['unknown', None], 'available_quota': [-1, True, '200', 1.5],
            'starts_at': [None, self.start.replace(tzinfo=None), self.segment.ends_at],
            'ends_at': [self.start], 'evidence_ids': [(), ('',), (' ',), 'proof'],
            'economic_snapshot': [None, [], {'x': float('nan')}, {'x': float('inf')},
                                  {1: 'bad'}, {'x': object()}, {'x': Decimal('1')}],
            'delivery_snapshot': [{'x': {1, 2}}]}
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(PlanUnavailable):
                    replace(self.segment, **{field: value})

    def test_invalid_plan_inputs_fail_closed(self):
        for amount in ('0', '-1', 'NaN', 'Infinity', '.001', None, True):
            with self.subTest(amount=amount), self.assertRaises(PlanUnavailable):
                self.plan(amount)
        for kwargs in ({'at': None}, {'at': self.start.replace(tzinfo=None)},
                       {'used_calls': True}, {'used_calls': -1}, {'used_calls': '1'},
                       {'prior_amount': '-1'}, {'prior_amount': '101'},
                       {'other_pending': '.001'}, {'other_pending': 'NaN'},
                       {'prior_recovery': True}, {'prior_recovery': -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(PlanUnavailable):
                self.plan(**kwargs)


if __name__ == '__main__':
    unittest.main()
