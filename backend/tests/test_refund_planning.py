"""Pure segment arithmetic and disposable SQLite manual/planning HTTP tests."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import hashlib
import json
import unittest
from unittest.mock import patch

from sqlalchemy import select

from app.refund_planning import OriginalSegment, PlanUnavailable, plan_segment
import test_refund_workflow as fixture


class SegmentPlanningTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 1, 31, tzinfo=timezone.utc)
        self.end = datetime(2026, 2, 28, tzinfo=timezone.utc)
        self.segment = OriginalSegment('segment', 'order', Decimal('100'), 'api', 100, 2,
            self.start, self.end, {'price': '100', 'cost': 'PRIVATE'}, {'quota': 100})

    def test_finite_api_uses_actual_charges_not_elapsed_time_or_success_rate(self):
        plan = plan_segment(self.segment, '40', at=self.end, used_calls=100)
        self.assertEqual(plan['basis'], 'calls')
        self.assertEqual(plan['original_quota'], 200)
        self.assertEqual(plan['recovery_delta'], 80)
        self.assertEqual(plan['refundable_balance_before'], '50.00')
        with self.assertRaises(PlanUnavailable):
            plan_segment(self.segment, '50.01', at=self.start, used_calls=100)

    def test_missing_charge_count_is_unknown_not_zero_and_bad_counts_reject(self):
        for value in (None, -1, True, 201):
            with self.assertRaises(PlanUnavailable):
                plan_segment(self.segment, '1', at=self.start, used_calls=value)

    def test_unlimited_api_and_saas_use_real_calendar_span_and_reject_future_terms(self):
        for method in ('api', 'tenant_access'):
            segment = replace(self.segment, method=method, monthly_quota=0)
            at = self.start + timedelta(days=14)
            plan = plan_segment(segment, '50', at=at)
            self.assertEqual((plan['basis'], plan['refundable_balance_before']), ('time', '50.00'))
            self.assertEqual(plan['effective_ends_at'], at.isoformat())
            with self.assertRaisesRegex(PlanUnavailable, 'future_renewal_segment_not_supported'):
                plan_segment(segment, '100', at=self.start - timedelta(days=1))

    def test_cumulative_partial_recovery_and_last_cent_residue(self):
        segment = replace(self.segment, original_amount=Decimal('3'), monthly_quota=5, months=1)
        first = plan_segment(segment, '1', at=self.start, used_calls=0)
        second = plan_segment(segment, '1', at=self.start, used_calls=0, prior_amount='1', prior_recovery=first['cumulative_recovery'])
        final = plan_segment(segment, '1', at=self.start, used_calls=0, prior_amount='2', prior_recovery=second['cumulative_recovery'])
        self.assertEqual([first['recovery_delta'], second['recovery_delta'], final['recovery_delta']], [1, 2, 2])
        self.assertEqual(final['remaining_total_quota'], 0)

    def test_pending_reservations_and_prior_recovery_never_double_count(self):
        with self.assertRaises(PlanUnavailable):
            plan_segment(self.segment, '21', at=self.start, used_calls=100, other_pending='30')
        with self.assertRaises(PlanUnavailable):
            plan_segment(self.segment, '1', at=self.start, used_calls=100, prior_amount='10', prior_recovery=150)
        with self.assertRaisesRegex(PlanUnavailable, 'prior_recovery_reconciliation_required'):
            plan_segment(self.segment, '1', at=self.start, used_calls=100, prior_amount='10', prior_recovery=0)

    def test_recovery_never_exceeds_unused_quota_or_time_after_prior_reservations(self):
        first = plan_segment(self.segment, '20', at=self.start, used_calls=100)
        final = plan_segment(self.segment, '30', at=self.start, used_calls=100,
            prior_amount='20', prior_recovery=first['cumulative_recovery'])
        self.assertEqual(first['recovery_delta'] + final['recovery_delta'], 100)
        segment = replace(self.segment, method='tenant_access', monthly_quota=0)
        at = self.start + timedelta(days=14)
        first = plan_segment(segment, '20', at=at)
        final = plan_segment(segment, '30', at=at, prior_amount='20', prior_recovery=first['cumulative_recovery'])
        self.assertEqual(first['recovery_delta'] + final['recovery_delta'], 14 * 86400 * 1000000)
        self.assertEqual(final['effective_ends_at'], at.isoformat())

    def test_money_half_up_is_separate_from_floor_quantity_recovery(self):
        segment = replace(self.segment, monthly_quota=3, months=1)
        plan = plan_segment(segment, '66.67', at=self.start, used_calls=1)
        self.assertEqual(plan['refundable_balance_before'], '66.67')
        self.assertEqual(plan['recovery_delta'], 2)

    def test_frozen_snapshot_is_detached_and_clock_inputs_are_validated(self):
        plan = plan_segment(self.segment, '1', at=self.start, used_calls=0)
        self.segment.economic_snapshot['price'] = '999'
        self.assertEqual(plan['economic_snapshot']['price'], '100')
        with self.assertRaises(PlanUnavailable):
            plan_segment(self.segment, '1', at=self.start.replace(tzinfo=None), used_calls=0)


class RefundPlanningHTTPTests(unittest.TestCase):
    setUpClass = classmethod(fixture.RefundWorkflowTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.RefundWorkflowTests.tearDownClass.__func__)
    tearDown = fixture.RefundWorkflowTests.tearDown
    request = fixture.RefundWorkflowTests.request
    create_order = fixture.RefundWorkflowTests.create_order
    create = fixture.RefundWorkflowTests.create
    review = fixture.RefundWorkflowTests.review

    def setUp(self):
        fixture.RefundWorkflowTests.setUp(self)
        self.module = importlib.import_module(self.m.__package__ + '.refund_planning')
        self.Manual = self.m.refund_planning['ManualAssessment']
        self.at = datetime(2026, 10, 15, tzinfo=timezone.utc)
        clock = patch.object(self.m, 'now', return_value=self.at)
        clock.start()
        self.addCleanup(clock.stop)

    def monthly(self, method='api', quota=100):
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, self.order_id)
            order.delivery_method_snapshot = method
            economic = json.loads(order.economic_snapshot_json)
            economic['billing_unit'] = 'month'
            order.economic_snapshot_json = json.dumps(economic)
            delivery = json.loads(order.delivery_snapshot_json)
            delivery.update(method=method, quota={'rate_limit_per_minute': 60, 'daily_quota': 0, 'monthly_quota': quota})
            order.delivery_snapshot_json = json.dumps(delivery)
            Combo, Term = (self.m.subscription_terms[key] for key in ('Combination', 'Term'))
            db.add(Combo(id='refund-combo', enterprise_id='buyer_tenant', product_id='product', delivery_method=method,
                version_id='version', version_code='v1', anchor_at=self.at - timedelta(days=14), generation=1, total_months=1))
            db.flush()
            db.add(Term(id='refund-segment', order_id=self.order_id, combination_id='refund-combo', generation=1,
                version_id='version', version_code='v1', start_month=0, months=1,
                starts_at=self.at - timedelta(days=14), ends_at=self.at + timedelta(days=14), status='active'))
            db.commit()

    def ledger(self, db, **window):
        return self.module.ChargeSummary(window['order_id'], window['segment_id'], window['start'], window['end'],
            window['as_of'], 25, True, 'native-charge-v1', ('charge-manifest-proof',))

    def quote(self, amount='20', actor='buyer', expected=200):
        return self.request('POST', f'/api/orders/{self.order_id}/refund-quote', actor=actor, expected=expected, json={'amount': amount})

    def manual(self, row, actor='ops', expected=200, **changes):
        return self.request('POST', f"/api/refunds/{row['id']}/manual-assessments", actor=actor, expected=expected,
            json={'amount': row['amount'], 'refundable_limit': '70.00', 'used_calls': 30,
                'reason': 'Manual statement and documented usage review', 'evidence_refs': ['external-statement-001'],
                'expected_version': row['version'], 'idempotency_key': 'manual-assessment-0001', **changes})

    def test_charge_adapter_is_explicit_scoped_complete_and_no_cost_is_public(self):
        self.monthly()
        self.assertIsNone(self.m.refund_charge_summary)
        missing = self.quote()
        self.assertEqual((missing['status'], missing['reason']), ('unavailable', 'api_charge_ledger_unavailable'))
        calls = []
        def ledger(db, **window):
            calls.append(window)
            return self.ledger(db, **window)
        with patch.dict(vars(self.m), refund_charge_summary=ledger):
            result = self.quote()
            self.assertEqual((result['status'], result['item']['charged_calls']), ('available', 25))
            self.assertEqual(calls[0]['order_id'], self.order_id)
            self.assertEqual(calls[0]['segment_id'], 'refund-segment')
            self.assertIn('including_upstream_failure', result['item']['charge_policy'])
            row = self.create()
            approved = self.review(row, decision='approve')
        self.assertEqual(approved['status'], 'approved')
        self.assertFalse(approved['execution_available'])
        self.assertNotIn('economic_snapshot', json.dumps(result))
        with self.m.SessionLocal() as db:
            plan = json.loads(db.get(self.Case, row['id']).plan_json)['economic_plan']
            self.assertEqual(plan['charge_ledger']['charged_calls'], 25)
            self.assertEqual(plan['charge_ledger']['policy_version'], 'native-charge-v1')
            self.assertEqual(db.get(self.m.Order, self.order_id).payment_status, 'paid')

    def test_incomplete_wrong_tenant_or_throwing_ledger_never_becomes_zero(self):
        self.monthly()
        for change in ({'complete': False}, {'order_id': 'foreign'}, {'charged_calls': None}, {'evidence_ids': ()}):
            with patch.dict(vars(self.m), refund_charge_summary=lambda db, **window: replace(self.ledger(db, **window), **change)):
                self.assertEqual(self.quote()['status'], 'unavailable')
        with patch.dict(vars(self.m), refund_charge_summary=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('PRIVATE'))):
            result = self.quote()
            self.assertEqual(result['reason'], 'charge_ledger_adapter_unavailable')
            self.assertNotIn('PRIVATE', json.dumps(result))

    def test_saas_and_unlimited_api_work_without_call_ledger(self):
        self.monthly('tenant_access', 0)
        result = self.quote('50')
        self.assertEqual((result['status'], result['item']['basis']), ('available', 'time'))
        self.assertEqual(self.quote('50.01')['status'], 'unavailable')
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, self.order_id)
            order.delivery_method_snapshot = 'api'
            delivery = json.loads(order.delivery_snapshot_json)
            delivery['method'] = 'api'
            order.delivery_snapshot_json = json.dumps(delivery)
            db.commit()
        self.assertEqual(self.quote('50')['item']['basis'], 'time')

    def test_revised_segment_is_unavailable_not_current_catalog_price(self):
        self.monthly('tenant_access', 0)
        with self.m.SessionLocal() as db:
            Revision = self.m.subscription_terms['Revision']
            db.add(Revision(change_id='historical-change', term_id='refund-segment', revision_number=1,
                economic_snapshot_json='{}', delivery_snapshot_json='{}', version_id='version', version_code='v1'))
            db.commit()
        self.assertEqual(self.quote()['reason'], 'revised_economic_segments_require_reconciliation')
        row = self.create()
        self.review(row, decision='approve', expected=409)

    def test_future_segment_quote_and_approval_are_controlled_unavailable(self):
        self.monthly('tenant_access', 0)
        with self.m.SessionLocal() as db:
            Term = self.m.subscription_terms['Term']
            term = db.get(Term, 'refund-segment')
            term.starts_at, term.ends_at = self.at + timedelta(days=1), self.at + timedelta(days=29)
            db.commit()
        self.assertEqual(self.quote()['reason'], 'future_renewal_segment_not_supported')
        row = self.create()
        self.review(row, decision='approve', expected=409)
        self.assertFalse(row['execution_available'])

    def test_cumulative_approved_plan_reserves_quantity_and_old_snapshots(self):
        self.monthly()
        with patch.dict(vars(self.m), refund_charge_summary=self.ledger):
            initial = self.create('20')
            first = self.review(initial, decision='approve')
            self.assertEqual(self.review(initial, decision='approve')['version'], first['version'])
            second = self.create('30', idempotency_key='second-refund-key')
            self.review(second, decision='approve', idempotency_key='second-approval-key')
        with self.m.SessionLocal() as db:
            plans = [json.loads(db.get(self.Case, row['id']).plan_json)['economic_plan'] for row in (first, second)]
        self.assertEqual([plan['recovery_delta'] for plan in plans], [20, 30])
        self.assertEqual(plans[1]['cumulative_recovery'], 50)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.Case).count(), 2)
            self.assertEqual(db.query(self.Event).filter_by(action='approve').count(), 2)

    def test_invalid_prior_plan_identity_shape_and_recovery_block_new_approval(self):
        self.monthly()
        with patch.dict(vars(self.m), refund_charge_summary=self.ledger):
            first = self.review(self.create('20'), decision='approve')
            second = self.create('10', idempotency_key='second-refund-key')
            with self.m.SessionLocal() as db:
                original = db.get(self.Case, first['id']).plan_json
            for corruption in ('identity', 'shape', 'recovery'):
                with self.subTest(corruption=corruption):
                    envelope = json.loads(original)
                    if corruption == 'identity':
                        envelope['payment_id'] = 'foreign-payment'
                    elif corruption == 'shape':
                        envelope['economic_plan'] = []
                    else:
                        envelope['economic_plan']['recovery_delta'] = 0
                    frozen = json.dumps(envelope, sort_keys=True)
                    with self.m.SessionLocal() as db:
                        case = db.get(self.Case, first['id'])
                        case.plan_json = frozen
                        case.plan_hash = hashlib.sha256(frozen.encode()).hexdigest()
                        db.commit()
                    self.assertEqual(self.quote('1')['status'], 'unavailable')
                    self.review(second, decision='approve', expected=409,
                                idempotency_key='second-approval-key')
                    with self.m.SessionLocal() as db:
                        self.assertEqual(db.get(self.m.Refund, second['id']).status, 'requested')
                        self.assertIsNone(db.get(self.Case, second['id']).plan_json)

    def test_manual_determination_is_permissioned_idempotent_and_not_automatic(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.Order, self.order_id).snapshot_version = 0
            db.commit()
        row = self.create()
        for actor in ('buyer', 'provider', 'finance', 'reviewer'):
            self.manual(row, actor=actor, expected=403)
        assessed = self.manual(row)
        self.assertEqual((assessed['version'], assessed['planning']['status']), (2, 'manual_ready'))
        self.assertEqual(self.manual(row)['version'], 2)
        self.manual(row, expected=409, used_calls=31)
        self.review(row, decision='approve', expected=409)
        approved = self.review(assessed, decision='approve')
        self.assertEqual(approved['status'], 'approved')
        self.assertFalse(approved['execution_available'])
        self.assertEqual(approved['planning']['source_trust'], 'manual_determination')
        self.assertFalse(approved['planning']['automatic_usage_trusted'])
        self.assertEqual(approved['planning']['resource_plan_status'], 'unavailable')
        self.request('POST', f"/api/refunds/{row['id']}/execute", actor='ops', expected=404, json={})
        with self.m.SessionLocal() as db:
            plan = json.loads(db.get(self.Case, row['id']).plan_json)['economic_plan']
            self.assertFalse(plan['automatic_usage_trusted'])
            self.assertEqual(plan['source_trust'], 'manual_determination')
            self.assertEqual(plan['used_calls'], 30)
            self.assertEqual(db.query(self.Manual).count(), 1)
            self.assertEqual(db.query(self.m.AuditLog).filter_by(action='manual_refund_determination').count(), 1)

    def test_manual_requires_basis_amount_usage_and_rechecks_limit_before_approval(self):
        row = self.create()
        self.manual(row, expected=422, evidence_refs=[])
        self.manual(row, expected=422, used_calls=-1)
        self.manual(row, expected=422, reason=' ')
        self.manual(row, expected=409, amount='21.00')
        assessed = self.manual(row, refundable_limit='20.00')
        self.create('1', idempotency_key='another-pending-refund')
        self.review(assessed, decision='approve', expected=409)

    def test_manual_audit_and_outbox_failure_roll_back_case_event_and_determination(self):
        row = self.create()
        for hook in ('audit', 'notify_business_event'):
            with patch.object(self.m, hook, side_effect=RuntimeError('isolated failure')):
                self.manual(row, expected=500)
            with self.m.SessionLocal() as db:
                self.assertEqual(db.query(self.Manual).count(), 0)
                self.assertEqual(db.get(self.Case, row['id']).version, 1)
                self.assertEqual(db.query(self.Event).count(), 1)


if __name__ == '__main__':
    unittest.main()
