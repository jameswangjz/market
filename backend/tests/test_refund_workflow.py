"""BE010 isolated HTTP request/review proofs; no executor or real bank I/O."""
from datetime import timedelta
from decimal import Decimal
import importlib
import json
import unittest
from unittest.mock import patch

from sqlalchemy import event, select
from sqlalchemy.orm import Session

import test_after_sales_workflow as fixture


class RefundWorkflowTests(unittest.TestCase):
    setUpClass = classmethod(fixture.AfterSalesWorkflowTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.AfterSalesWorkflowTests.tearDownClass.__func__)
    tearDown = fixture.AfterSalesWorkflowTests.tearDown
    request = fixture.AfterSalesWorkflowTests.request
    create_order = fixture.AfterSalesWorkflowTests.create_order

    def setUp(self):
        fixture.AfterSalesWorkflowTests.setUp(self)
        # Production installs a Redis-backed adapter; isolated tests opt in to
        # local evidence stubs only, including subclasses reusing this fixture.
        adapter = patch.dict(vars(self.m), refund_charge_summary=None)
        adapter.start()
        self.addCleanup(adapter.stop)
        self.Case, self.Event = (self.m.refund_workflow[key] for key in ('Case', 'Event'))
        self.Plan = importlib.import_module(self.m.__package__ + '.refund_schema').PreparedRefundPlan
        with self.m.SessionLocal() as db:
            db.add(self.m.Payment(id='paid-source', order_id=self.order_id, payment_no='PAY-ISOLATED',
                status='paid', amount=100, paid_at=self.m.now() - timedelta(hours=1)))
            db.commit()

    def create(self, amount='20.00', actor='buyer', expected=200, **changes):
        return self.request('POST', f'/api/orders/{self.order_id}/refund-requests', actor=actor, expected=expected,
            json={'amount': amount, 'reason': 'Ordinary refund request', 'idempotency_key': 'request-refund-0001', **changes})

    def review(self, row, decision='reject', actor='ops', expected=200, **changes):
        return self.request('POST', f"/api/refunds/{row['id']}/review", actor=actor, expected=expected,
            json={'decision': decision, 'reason': 'Recorded review reason', 'expected_version': row['version'],
                  'idempotency_key': 'review-refund-0001', **changes})

    def trusted_plan(self, db, order, refund):
        return self.Plan(refund_id=refund.id, order_id=order.id, payment_id=refund.payment_id,
            amount=Decimal(str(refund.amount)), calculation_version='isolated-plan-v1', as_of=self.m.now(),
            evidence_ids=('isolated-proof',), basis='time' if order.snapshot_version == 1
                and order.delivery_method_snapshot == 'tenant_access' else 'manual',
            economic_plan={'source_order_id': order.id, 'basis': 'isolated fixture', 'cost': 'PRIVATE_COST'})

    def test_buyer_request_reuses_refund_without_changing_order_payment_or_after_sales(self):
        row = self.create()
        self.assertEqual((row['status'], row['source'], row['version'], row['amount']), ('requested', 'ordinary', 1, '20.00'))
        self.assertFalse(row['execution_available'])
        with self.m.SessionLocal() as db:
            order, payment = db.get(self.m.Order, self.order_id), db.get(self.m.Payment, 'paid-source')
            self.assertEqual((order.payment_status, order.after_sales_status, payment.status), ('paid', 'none', 'paid'))
            self.assertEqual(Decimal(order.refunded_amount), 0)
            self.assertEqual(db.query(self.m.Settlement).count(), 0)
            self.assertEqual(db.query(self.Event).count(), 1)

    def test_only_buyer_admin_may_request_not_provider_platform_finance_or_member(self):
        for actor in ('provider', 'platform', 'ops', 'finance', 'outsider'):
            self.create(actor=actor, expected=403 if actor in ('platform', 'ops', 'finance') else 404)
        with self.m.SessionLocal() as db:
            db.scalar(select(self.m.Membership).where(self.m.Membership.user_id == 'buyer_admin')).role = 'member'
            db.commit()
        self.create(actor='buyer_admin', expected=403)
        row = self.create()
        with self.m.SessionLocal() as db:
            db.scalar(select(self.m.Membership).where(self.m.Membership.user_id == 'buyer')).status = 'disabled'
            db.commit()
        self.create(expected=403)
        self.request('GET', f"/api/refunds/{row['id']}", expected=403)

    def test_request_replay_is_idempotent_and_changed_payload_conflicts(self):
        first = self.create()
        self.assertEqual(self.create()['id'], first['id'])
        self.assertEqual(self.create('20')['id'], first['id'])
        self.create(amount='21.00', expected=409)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.Refund).count(), 1)
            self.assertEqual(db.query(self.Event).count(), 1)
            self.assertEqual(db.query(self.m.AuditLog).filter_by(action='ordinary_refund_request').count(), 1)
            Message = self.m.message_center['Message']
            messages = db.query(Message).filter(Message.event_key.like('ordinary-refund:%')).all()
            self.assertEqual(len(messages), 1)
            jobs = db.query(self.m.message_center['Outbox']).filter_by(message_id=messages[0].id).all()
            self.assertTrue(any(job.channel == 'email' for job in jobs))
            self.assertEqual(len(jobs), len({(job.user_id, job.channel) for job in jobs}))

    def test_pending_reservations_limit_order_and_payment_and_rejection_releases(self):
        row = self.create('80.00')
        self.create('21.00', expected=409, idempotency_key='second-too-much')
        self.review(row)
        self.assertEqual(self.create('100.00', idempotency_key='after-rejection')['amount'], '100.00')
        self.create('0.01', expected=409, idempotency_key='final-overflow-key')

    def test_completed_legacy_amount_and_pending_unknown_are_conservatively_reserved(self):
        with self.m.SessionLocal() as db:
            db.add(self.m.Refund(id='old', refund_no='REF-OLD', order_id=self.order_id,
                payment_id='paid-source', amount=30, status='pending'))
            db.get(self.m.Order, self.order_id).refunded_amount = 20
            db.commit()
        self.create('50.01', expected=409)
        self.assertEqual(self.create('50.00')['status'], 'requested')
        old = self.request('GET', '/api/refunds/old', actor='ops')
        self.assertEqual((old['source'], old['status'], old['version']), ('legacy', 'pending', None))
        self.assertEqual(old['allowed_actions'], [])
        self.review({'id': 'old', 'version': 1}, expected=409)

    def test_admin_operator_reject_replay_conflict_and_finance_denied(self):
        row = self.create()
        for actor in ('buyer', 'provider', 'finance', 'reviewer'):
            self.review(row, actor=actor, expected=403)
        self.review(row, expected=422, reason=' ')
        rejected = self.review(row, actor='platform')
        self.assertEqual((rejected['status'], rejected['version']), ('rejected', 2))
        self.assertEqual(self.review(row, actor='platform')['version'], 2)
        self.review(row, actor='platform', expected=409, reason='Different reason')
        self.review(row, expected=409, idempotency_key='stale-review-key')

    def test_approval_without_trusted_plan_is_unavailable_not_fake_success(self):
        row = self.create()
        self.assertEqual(row['planning']['status'], 'unavailable')
        self.review(row, decision='approve', expected=409)
        stored = self.request('GET', f"/api/refunds/{row['id']}", actor='ops')
        self.assertEqual((stored['status'], stored['version']), ('requested', 1))
        with self.m.SessionLocal() as db:
            self.assertIsNone(db.get(self.Case, row['id']).plan_json)

    def test_trusted_plan_freezes_only_on_valid_approval_never_executes(self):
        row = self.create()
        with patch.dict(vars(self.m), prepare_refund_plan=self.trusted_plan):
            approved = self.review(row, decision='approve')
            replay = self.review(row, decision='approve')
        self.assertEqual((approved['status'], approved['version']), ('approved', 2))
        self.assertEqual(replay['version'], 2)
        self.assertFalse(approved['execution_available'])
        self.assertNotIn('PRIVATE_COST', json.dumps(approved))
        with self.m.SessionLocal() as db:
            case = db.get(self.Case, row['id'])
            self.assertTrue(case.plan_hash)
            self.assertIn('PRIVATE_COST', case.plan_json)
            self.assertEqual(db.get(self.m.Order, self.order_id).payment_status, 'paid')
            self.assertEqual(db.get(self.m.Payment, 'paid-source').status, 'paid')
            self.assertEqual(db.query(self.m.Settlement).count(), 0)
            self.assertEqual(db.query(self.Event).count(), 2)

    def test_untrusted_plan_dict_or_wrong_identity_cannot_approve(self):
        row = self.create()
        with patch.dict(vars(self.m), prepare_refund_plan=lambda *args: {'amount': '20.00'}):
            self.review(row, decision='approve', expected=409)
        def wrong(db, order, refund):
            plan = self.trusted_plan(db, order, refund)
            from dataclasses import replace
            return replace(plan, order_id='foreign-order')
        with patch.dict(vars(self.m), prepare_refund_plan=wrong):
            self.review(row, decision='approve', expected=409)

    def test_api_and_historical_unknown_are_explicitly_unavailable_not_zero(self):
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, self.order_id)
            order.snapshot_version, order.delivery_method_snapshot = 1, 'api'
            db.commit()
        row = self.create()
        # The fixture still carries a file delivery snapshot: once the strict
        # planner is installed this is reconciliation, not a valid API segment.
        self.assertEqual(row['planning']['reason'], 'source_reconciliation_required')
        self.assertFalse(row['planning']['automatic_quote_available'])
        with self.m.SessionLocal() as db:
            db.get(self.m.Order, self.order_id).snapshot_version = 0
            db.commit()
        row = self.request('GET', f"/api/refunds/{row['id']}")
        self.assertEqual(row['planning']['reason'], 'manual_assessment_required')
        self.assertTrue(row['planning']['manual_assessment_allowed'])

    def test_source_and_economic_plan_cannot_be_supplied_by_client(self):
        for changes in ({'source': 'version_change'}, {'economic_plan': {'amount': 1}}, {'status': 'approved'}):
            self.create(expected=422, **changes)
        with self.m.SessionLocal() as db:
            db.get(self.m.Order, self.order_id).business_type = 'version_change_payment'
            db.commit()
        self.create(expected=409)

    def test_old_public_approve_complete_cannot_bypass_or_allow_finance(self):
        for action in ('approve_refund', 'complete_refund'):
            path = f'/api/orders/{self.order_id}/transition'
            for actor, status in [('finance', 403), ('buyer', 403), ('ops', 409), ('platform', 409)]:
                self.request('POST', path, actor=actor, expected=status, json={'action': action, 'reason': 'No back door'})
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.Refund).count(), 0)

    def test_list_detail_scope_and_pagination_are_safe_and_no_cost_fields(self):
        row = self.create()
        for actor in ('buyer', 'buyer_admin', 'provider', 'ops', 'platform'):
            detail = self.request('GET', f"/api/refunds/{row['id']}", actor=actor)
            self.assertNotIn('economic_plan', detail)
            self.assertNotIn('cost', json.dumps(detail))
        self.request('GET', f"/api/refunds/{row['id']}", actor='outsider', expected=404)
        self.request('GET', '/api/refunds', actor='finance', expected=403)
        self.request('GET', f"/api/refunds/{row['id']}?enterprise_id=other_tenant", expected=403)
        self.assertEqual(self.request('GET', '/api/refunds', actor='outsider')['total'], 0)
        for size in (10, 12, 20, 50, 100):
            result = self.request('GET', f'/api/refunds?page_size={size}')
            self.assertEqual((result['total'], len(result['items'])), (1, 1))
        self.assertEqual(self.request('GET', '/api/refunds?page=99')['items'], [])
        self.request('GET', '/api/refunds?page_size=101', expected=422)

    def test_bad_amounts_reason_missing_payment_and_foreign_ticket_reject(self):
        for amount in ('0', '-1', '1.001', 'NaN', 'Infinity'):
            self.create(amount, expected=422)
        self.create(expected=422, reason=' ')
        self.create(expected=404, ticket_id='foreign-ticket')
        self.create(expected=409, payment_id='foreign-payment')
        with self.m.SessionLocal() as db:
            db.delete(db.get(self.m.Payment, 'paid-source'))
            db.commit()
        self.create(expected=409)

    def test_request_and_approval_audit_outbox_failures_roll_back(self):
        for hook in ('audit', 'notify_business_event'):
            with patch.object(self.m, hook, side_effect=RuntimeError('isolated failure')):
                self.create(expected=500)
            with self.m.SessionLocal() as db:
                self.assertEqual(db.query(self.m.Refund).count(), 0)
                self.assertEqual(db.query(self.Case).count(), 0)
                self.assertEqual(db.query(self.Event).count(), 0)
        row = self.create()
        for hook in ('audit', 'notify_business_event'):
            with patch.dict(vars(self.m), prepare_refund_plan=self.trusted_plan), \
                    patch.object(self.m, hook, side_effect=RuntimeError('isolated failure')):
                self.review(row, decision='approve', expected=500)
            with self.m.SessionLocal() as db:
                self.assertEqual(db.get(self.m.Refund, row['id']).status, 'requested')
                self.assertEqual(db.get(self.Case, row['id']).version, 1)
                self.assertIsNone(db.get(self.Case, row['id']).plan_hash)
                self.assertEqual(db.query(self.Event).count(), 1)

    def test_review_lock_order_and_stale_version(self):
        row = self.create()
        locks = []
        def observe(state):
            if getattr(state.statement, '_for_update_arg', None) is not None:
                locks.append([table.name for table in state.statement.get_final_froms()])
        event.listen(Session, 'do_orm_execute', observe)
        try:
            self.review(row, expected=409, expected_version=2)
        finally:
            event.remove(Session, 'do_orm_execute', observe)
        self.assertEqual(locks[:2], [['orders'], ['refunds']])
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.Case, row['id']).version, 1)


if __name__ == '__main__':
    unittest.main()
