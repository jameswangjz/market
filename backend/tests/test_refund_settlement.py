"""BE013 arithmetic and real execution adapter checks on disposable fixtures."""
from decimal import Decimal
from datetime import timedelta, timezone
import importlib
import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import select

import test_refund_execution as fixture
from app.refund_settlement import segment_values
from app.refund_execution import canonical, digest


class ArithmeticTests(unittest.TestCase):
    def plan(self, cumulative):
        return dict(calculation_version='ordinary-refund-plan-v1', segment_id='s', basis='calls', original_amount='0.03', refund_amount='0.01',
            cumulative_refund_amount=cumulative, economic_snapshot=dict(total_cost='0.01', currency='CNY',
                settlement_rule=dict(mode='global', version='frozen', rates=dict(platform_rate='10', provider_rate='60',
                    service_rate='20', expert_rate='5', channel_rate='0'))))

    def test_cumulative_cost_and_shares_reconcile(self):
        values = [segment_values(self.plan(v)) for v in ('0.01', '0.02', '0.03')]
        self.assertEqual(sum(v[1] for v in values), Decimal('0.01'))
        self.assertEqual(sum(v[2] for v in values), Decimal('-0.02'))
        self.assertEqual(sum(sum(v[3].values()) for v in values), Decimal('-0.02'))

    def test_inconsistent_revised_delta_rejected(self):
        plan = self.plan('0.02')
        plan['cost_reversal_delta'] = '0.00'
        with self.assertRaises(ValueError):
            segment_values(plan)

    def test_full_refund_half_cent_ties_match_source_rounding(self):
        plan = self.plan('0.05')
        plan.update(original_amount='0.05', refund_amount='0.05')
        plan['economic_snapshot']['total_cost'] = '0.00'
        original = segment_values(plan)
        self.assertEqual(original[3]['platform_fee'], Decimal('0.00'))
        plan.update(calculation_version='economic-segment-refund-v1', revenue='0.05', cost='0.00',
            prior_cost_reversal='0.00', cumulative_cost_reversal='0.00',
            cost_reversal_delta='0.00', revenue_reversal_delta='0.05')
        revised = segment_values(plan)
        self.assertEqual(revised[3]['platform_fee'], Decimal('-0.01'))
        self.assertEqual(sum(revised[3].values()), revised[2])

    def test_unknown_rule_mode_and_conflicting_original_cost_rejected(self):
        plan = self.plan('0.01')
        plan['cost'] = '0.02'
        with self.assertRaises(ValueError):
            segment_values(plan)
        del plan['cost']
        plan['economic_snapshot']['settlement_rule']['mode'] = 'live'
        with self.assertRaises(ValueError):
            segment_values(plan)

    def test_trusted_rule_without_mode_or_id_preserves_frozen_shares(self):
        plan = self.plan('0.02')
        expected = segment_values(plan)
        del plan['economic_snapshot']['settlement_rule']['mode']
        self.assertEqual(segment_values(plan), expected)


class SettlementTests(unittest.TestCase):
    setUpClass = classmethod(fixture.RefundExecutionTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.RefundExecutionTests.tearDownClass.__func__)
    setUp = fixture.RefundExecutionTests.setUp
    tearDown = fixture.RefundExecutionTests.tearDown
    request = fixture.RefundExecutionTests.request
    create_order = fixture.RefundExecutionTests.create_order
    monthly = fixture.RefundExecutionTests.monthly
    create = fixture.RefundExecutionTests.create
    review = fixture.RefundExecutionTests.review
    ledger = fixture.RefundExecutionTests.ledger
    approve = fixture.RefundExecutionTests.approve
    execute = fixture.RefundExecutionTests.execute
    step = fixture.RefundExecutionTests.step

    def adapter(self):
        module = importlib.import_module(self.m.__package__ + '.refund_settlement')
        hook = module.install(vars(self.m))
        hook['Model'].__table__.create(self.m.engine, checkfirst=True)
        return hook

    def test_completed_execution_idempotent_mapping_and_safe_description(self):
        hook = self.adapter()
        approved = self.approve()
        self.execute(approved)
        for _ in range(5):
            self.step()
        with self.m.SessionLocal() as db:
            refund = db.get(self.m.Refund, approved['id'])
            completion = db.get(self.Completion, refund.id)
            frozen = completion.economic_plan_json
            self.assertEqual(hook['ledger_status'](db, refund.id), 'pending_monthly')
            with patch.object(self.m, 'audit', wraps=self.m.audit) as audit:
                first = hook['negative_settlement'](db, refund)
                after = audit.call_args.kwargs['after']
                self.assertEqual(after['net_amount'], '-20.00')
                self.assertEqual(after['refund_id'], refund.id)
                self.assertNotIn('economic_plan', after)
                self.assertNotIn('segments', after)
            db.commit()
            self.assertEqual(first.id, hook['negative_settlement'](db, refund).id)
            self.assertEqual(first.net_amount, Decimal('-20.00'))
            self.assertEqual(sum(getattr(first, k) for k in
                ('platform_fee', 'provider_share', 'service_share', 'expert_fee', 'channel_fee')), first.profit_amount)
            self.assertEqual(hook['ledger_status'](db, refund.id), 'settlement_generated')
            description = hook['describe'](db, first.id)
            self.assertNotIn('economic_plan', description)
            self.assertEqual(description['refund_id'], refund.id)
            self.assertEqual(completion.economic_plan_json, frozen)
            self.assertEqual(completion.settlement_status, 'pending_be013')
            self.assertEqual(len(db.scalars(select(hook['Model'])).all()), 1)
            batch = self.m.SettlementBatch(batch_no='BE013-BATCH', status='generated', idempotency_key='be013')
            db.add(batch)
            db.flush()
            line = self.m.SettlementLine(batch_id=batch.id, settlement_id=first.id,
                                         participant_type='provider', amount=first.provider_share)
            db.add(line)
            db.flush()
            self.assertEqual(hook['ledger_status'](db, refund.id), 'in_batch')
            batch.status = 'paid'
            db.flush()
            # A batch flag without final-baseline recovery/offset evidence is
            # still only batch participation, never proof of money recovered.
            self.assertEqual(hook['ledger_status'](db, refund.id), 'in_batch')
            line.status = 'superseded'
            db.flush()
            self.assertEqual(hook['ledger_status'](db, refund.id), 'settlement_generated')

    def test_incomplete_execution_fails_closed(self):
        hook = self.adapter()
        approved = self.approve()
        with self.m.SessionLocal() as db:
            with self.assertRaises(HTTPException) as error:
                hook['negative_settlement'](db, db.get(self.m.Refund, approved['id']))
            self.assertEqual(error.exception.status_code, 409)
            self.assertEqual(db.scalars(select(self.m.Settlement)).all(), [])

    def test_malformed_completed_evidence_and_audit_failure_leave_no_mapping(self):
        hook = self.adapter()
        approved = self.approve()
        self.execute(approved)
        for _ in range(5):
            self.step()
        with self.m.SessionLocal() as db:
            refund = db.get(self.m.Refund, approved['id'])
            completion = db.get(self.Completion, refund.id)
            # Override only the read value, without mutating persisted evidence.
            with patch.object(completion, 'economic_plan_json', '{broken'):
                with db.no_autoflush, self.assertRaises(HTTPException):
                    hook['negative_settlement'](db, refund)
            db.expire(completion)
            with patch.object(self.m, 'audit', side_effect=RuntimeError('audit unavailable')):
                with self.assertRaises(RuntimeError):
                    hook['negative_settlement'](db, refund)
            self.assertEqual(db.scalars(select(self.m.Settlement)).all(), [])
            self.assertEqual(db.scalars(select(hook['Model'])).all(), [])
            hook['negative_settlement'](db, refund)
            db.commit()

    def test_completion_date_payment_and_resource_order_must_match(self):
        hook = self.adapter()
        approved = self.approve()
        self.execute(approved)
        for _ in range(5):
            self.step()
        with self.m.SessionLocal() as db:
            refund = db.get(self.m.Refund, approved['id'])
            completion = db.get(self.Completion, refund.id)
            execution = db.get(self.Execution, completion.execution_id)
            completed_at = refund.completed_at
            execution_at = execution.completed_at
            db.add(self.m.Payment(id='different-payment', order_id=refund.order_id,
                                  payment_no='BE013-DIFFERENT', amount=20, status='paid'))
            db.flush()
            for target, field, value in (
                (refund, 'completed_at', completed_at + timedelta(days=32)),
                (refund, 'completed_at', None),
                (execution, 'completed_at', execution_at + timedelta(seconds=1)),
                (refund, 'payment_id', 'different-payment'),
            ):
                with self.subTest(field=field, value=value):
                    original = getattr(target, field)
                    setattr(target, field, value)
                    db.flush()
                    with self.assertRaises(HTTPException) as error:
                        hook['negative_settlement'](db, refund)
                    self.assertEqual(error.exception.status_code, 409)
                    setattr(target, field, original)
                    db.flush()
            resource = json.loads(execution.resource_plan_json)
            resource['order_id'] = 'different-order'
            resource['plan_hash'] = digest({k: v for k, v in resource.items() if k != 'plan_hash'})
            with patch.object(execution, 'resource_plan_json', canonical(resource)), \
                    patch.object(execution, 'resource_plan_hash', digest(resource)):
                with db.no_autoflush, self.assertRaises(HTTPException):
                    hook['negative_settlement'](db, refund)
            db.expire(execution)
            self.assertEqual(db.scalars(select(self.m.Settlement)).all(), [])
            self.assertEqual(db.scalars(select(hook['Model'])).all(), [])
            # Equivalent timezone representations, including SQLite's naive
            # refund timestamp, must still permit the valid evidence.
            execution.completed_at = execution_at.astimezone(timezone(timedelta(hours=8)))
            db.flush()
            hook['negative_settlement'](db, refund)
