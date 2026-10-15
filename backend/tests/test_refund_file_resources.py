"""FILE receipt transactions and fail-closed order entitlement checks."""
import importlib
import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from sqlalchemy import Column, DateTime, Numeric, String, Text, create_engine, select
from sqlalchemy.orm import declarative_base, sessionmaker

from app.refund_execution import canonical, digest
from app.refund_file_resources import install
from app.refund_settlement import segment_values
import test_refund_planning as workflow_fixture
import test_refund_execution as execution_fixture


class FileResourcesTests(unittest.TestCase):
    def setUp(self):
        Base = declarative_base()

        class Order(Base):
            __tablename__ = 'orders'
            id = Column(String, primary_key=True)
            delivery_method_snapshot = Column(String, default='file')
            snapshot_version = Column(Numeric, default=1)
            product_id = Column(String, default='product')
            product_version_id = Column(String, default='version')
            refunded_amount = Column(Numeric(14, 2), default=0)

        class Refund(Base):
            __tablename__ = 'refunds'
            id = Column(String, primary_key=True)
            order_id = Column(String)
            payment_id = Column(String, default='payment')
            status = Column(String, default='executing')
            amount = Column(Numeric(14, 2), default=25)

        class Case(Base):
            __tablename__ = 'cases'
            refund_id = Column(String, primary_key=True)
            source = Column(String, default='ordinary')
            plan_json = Column(Text)
            plan_hash = Column(String)

        class Assessment(Base):
            __tablename__ = 'assessments'
            id = Column(String, primary_key=True)
            refund_id = Column(String)
            amount = Column(Numeric(14, 2), default=25)
            refundable_limit = Column(Numeric(14, 2), default=100)
            assessed_by = Column(String, default='operator')
            reason = Column(String, default='reviewed')
            evidence_refs_json = Column(Text, default='["ticket:1"]')
            case_version = Column(Numeric, default=2)
            used_calls = Column(Numeric, default=0)
            assessed_at = Column(DateTime, default=lambda: datetime(2026, 10, 15))

        class Execution(Base):
            __tablename__ = 'ordinary_refund_executions'
            id = Column(String, primary_key=True)
            refund_id = Column(String)
            status = Column(String, default='resources_pending')
            resume_status = Column(String, default='resources_pending')
            resource_plan_json = Column(Text)
            resource_plan_hash = Column(String)
            plan_hash = Column(String)
            proof_json = Column(Text)

        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        sessions = sessionmaker(engine, expire_on_commit=False)
        self.Order, self.Refund, self.Case, self.Execution = Order, Refund, Case, Execution
        self.Assessment = Assessment
        self.economic = dict(amount='100.00', total_cost='33.33', currency='CNY',
            settlement_rule=dict(version='frozen-v1', rates=dict(platform_rate='10',
                provider_rate='80', service_rate='5', expert_rate='3', channel_rate='2')))
        self.delivery = dict(method='file')
        self.ns = dict(Base=Base, Order=Order, Refund=Refund, SessionLocal=sessions,
            now=lambda: datetime.now(timezone.utc),
            refund_workflow=dict(Case=Case, controlled_source=lambda *_: 'ordinary',
                lock_order=lambda db, oid: db.scalar(select(Order).where(Order.id == oid)
                    .with_for_update().execution_options(populate_existing=True))),
            refund_execution=dict(Execution=Execution), refund_planning=dict(ManualAssessment=Assessment),
            order_economics=dict(economic_snapshot=lambda _: self.economic,
                                delivery_snapshot=lambda _: self.delivery))
        self.hooks = install(self.ns)
        Base.metadata.create_all(engine)
        self.sessions = sessions
        self.plan = dict(calculation_version='ordinary-file-refund-plan-v1', basis='manual',
            source_order_id='one', segment_id='one', original_amount='100.00',
            refund_amount='25.00', cumulative_refund_amount='25.00',
            economic_snapshot=self.economic, delivery_snapshot=self.delivery,
            manual_assessment_id='assessment', evidence_refs=['ticket:1'],
            manual_assessment_case_version=2, used_calls=0,
            assessed_at='2026-10-15T00:00:00',
            prior_completed_refund_ids=[],
            assessed_by='operator', reason='reviewed', refundable_limit='100.00')
        self.envelope = dict(refund_id='refund', order_id='one', payment_id='payment',
                             calculation_version='ordinary-file-refund-plan-v1',
                             basis='manual', amount='25.00', evidence_ids=['assessment'], economic_plan=self.plan)
        with sessions.begin() as db:
            db.add_all([Order(id='one'), Order(id='two'), Refund(id='refund', order_id='one'),
                        Assessment(id='assessment', refund_id='refund'),
                        Case(refund_id='refund', plan_json=canonical(self.envelope), plan_hash=digest(self.envelope))])
        with sessions.begin() as db:
            resource = self.hooks['prepare'](db, db.get(Order, 'one'), db.get(Refund, 'refund'), self.envelope)
            self.execution = Execution(id='execution', refund_id='refund',
                resource_plan_json=canonical(resource), resource_plan_hash=digest(resource),
                plan_hash=digest(self.envelope))
            db.add(self.execution)

    def test_replay_and_funds_failure_keep_only_target_revoked(self):
        proof = self.hooks['apply'](None, self.execution)
        with self.sessions.begin() as db:
            execution = db.get(self.Execution, 'execution')
            execution.status = 'failed'
            execution.resume_status = 'funds_pending'
            execution.proof_json = canonical(proof)
        self.assertEqual(proof, self.hooks['apply'](None, self.execution))
        with self.sessions() as db:
            self.assertEqual(len(db.scalars(select(self.hooks['Recovery'])).all()), 1)
            with self.assertRaises(HTTPException) as denied:
                self.hooks['require_download'](db, db.get(self.Order, 'one'))
            self.assertEqual(denied.exception.status_code, 409)
            self.hooks['require_download'](db, db.get(self.Order, 'two'))

    def test_transaction_failure_rolls_back_revocation_and_retry_works(self):
        from sqlalchemy import event
        def reject(*args):
            raise RuntimeError('injected commit failure')
        event.listen(self.sessions, 'before_commit', reject)
        try:
            with self.assertRaises(RuntimeError):
                self.hooks['apply'](None, self.execution)
        finally:
            event.remove(self.sessions, 'before_commit', reject)
        with self.sessions() as db:
            self.assertIsNone(db.scalar(select(self.hooks['Recovery'])))
            self.assertTrue(self.hooks['blocked'](db, db.get(self.Order, 'one')))
        self.assertTrue(self.hooks['apply'](None, self.execution)['resource_applied'])

    def test_pending_phase_blocks_before_receipt_and_failed_retry_stays_blocked(self):
        with self.sessions() as db:
            self.assertTrue(self.hooks['blocked'](db, db.get(self.Order, 'one')))
            self.assertFalse(self.hooks['blocked'](db, db.get(self.Order, 'two')))
        with self.sessions.begin() as db:
            db.get(self.Execution, 'execution').status = 'failed'
        with self.sessions() as db:
            self.assertTrue(self.hooks['blocked'](db, db.get(self.Order, 'one')))

    def test_changed_assessment_or_frozen_snapshot_is_rejected(self):
        with self.sessions.begin() as db:
            db.get(self.Assessment, 'assessment').amount = Decimal('30.00')
        with self.assertRaises(HTTPException):
            self.hooks['apply'](None, self.execution)
        with self.sessions.begin() as db:
            db.get(self.Assessment, 'assessment').amount = Decimal('25.00')
        self.economic = dict(self.economic, total_cost='40.00')
        with self.assertRaises(HTTPException):
            self.hooks['apply'](None, self.execution)

    def test_economic_original_revenue_and_manual_limit_cannot_be_forged(self):
        for changes in ({'original_amount': '200.00'},
                        {'cumulative_refund_amount': '26.00'},
                        {'manual_assessment_case_version': 1}):
            envelope = dict(self.envelope, economic_plan=dict(self.plan, **changes))
            with self.sessions.begin() as db:
                case = db.get(self.Case, 'refund')
                case.plan_json, case.plan_hash = canonical(envelope), digest(envelope)
            with self.sessions() as db, self.assertRaises(HTTPException):
                self.hooks['prepare'](db, db.get(self.Order, 'one'),
                                      db.get(self.Refund, 'refund'), envelope)

    def test_unpersisted_execution_and_conflicting_proof_are_rejected(self):
        forged = SimpleNamespace(**{k: getattr(self.execution, k) for k in
            ('id', 'refund_id', 'resource_plan_json')})
        forged.id = 'missing'
        with self.assertRaises(HTTPException):
            self.hooks['apply'](None, forged)
        self.hooks['apply'](None, self.execution)
        with self.sessions.begin() as db:
            db.get(self.Execution, 'execution').proof_json = '{}'
        with self.assertRaises(HTTPException):
            self.hooks['apply'](None, self.execution)

    def test_completed_legacy_partial_refund_denies_without_receipt(self):
        with self.sessions.begin() as db:
            db.get(self.Refund, 'refund').status = 'completed'
        with self.sessions() as db:
            with self.assertRaises(HTTPException):
                self.hooks['require_download'](db, db.get(self.Order, 'one'))

    def test_receipt_is_immutable(self):
        self.hooks['apply'](None, self.execution)
        with self.sessions() as db:
            row = db.scalar(select(self.hooks['Recovery']))
            row.order_id = 'two'
            with self.assertRaises(ValueError):
                db.flush()

    def test_cost_reversal_uses_cumulative_existing_rounding(self):
        first = segment_values(self.plan)
        second_plan = dict(self.plan, refund_amount='75.00', cumulative_refund_amount='100.00')
        second = segment_values(second_plan)
        self.assertEqual(first[1], Decimal('8.33'))
        self.assertEqual(first[1] + second[1], Decimal('33.33'))
        self.assertEqual(sum(first[3].values()), first[2])
        self.assertEqual(sum(second[3].values()), second[2])


class FileWorkflowTests(unittest.TestCase):
    setUpClass = classmethod(workflow_fixture.RefundPlanningHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(workflow_fixture.RefundPlanningHTTPTests.tearDownClass.__func__)
    tearDown = workflow_fixture.RefundPlanningHTTPTests.tearDown
    request = workflow_fixture.RefundPlanningHTTPTests.request
    create_order = workflow_fixture.RefundPlanningHTTPTests.create_order
    create = workflow_fixture.RefundPlanningHTTPTests.create
    review = workflow_fixture.RefundPlanningHTTPTests.review
    manual = workflow_fixture.RefundPlanningHTTPTests.manual
    execute = execution_fixture.RefundExecutionTests.execute

    def setUp(self):
        workflow_fixture.RefundPlanningHTTPTests.setUp(self)
        self.worker = importlib.import_module(self.m.__package__ + '.refund_execution_worker')
        self.other = self.create_order()
        with self.m.SessionLocal() as db:
            for oid in (self.order_id, self.other):
                order = db.get(self.m.Order, oid)
                order.payment_status, order.paid_amount = 'paid', order.amount
                order.main_status, order.delivery_status = 'completed', 'accepted'
            db.commit()

    def step(self):
        self.assertTrue(self.worker.process_once(self.m.SessionLocal, ns=vars(self.m)))

    def approved(self, amount='20.00'):
        row = self.create(amount)
        assessed = self.manual(row, refundable_limit='100.00')
        return self.review(assessed, decision='approve')

    def check_access(self, target_blocked):
        for oid, denied in ((self.order_id, target_blocked), (self.other, False)):
            with self.m.SessionLocal() as db:
                order = self.m.refund_workflow['lock_order'](db, oid)
                self.assertEqual(self.m.refund_file_resources['blocked'](db, order), denied)
                self.assertNotEqual(db.get(self.m.FileObject, 'product_data').status, 'deleted')
            self.request('GET', f'/api/orders/{oid}/product-files', expected=409 if denied else 200)
            self.request('GET', f'/api/orders/{oid}/product-files', actor='ops', expected=200)
            storage = MagicMock()
            storage.get_object.return_value.read.return_value = b'x'
            with patch.dict(vars(self.m), MINIO_ENDPOINT='local-test:9000', Minio=lambda *_a, **_kw: storage):
                self.request('GET', f'/api/files/product_data/download?order_id={oid}',
                             expected=409 if denied else 200)
            storage.remove_object.assert_not_called()
            self.assertEqual(storage.get_object.call_count, 0 if denied else 1)

    def test_real_manual_workflow_resource_before_funds_and_negative_cost(self):
        approved = self.approved()
        self.assertTrue(approved['execution_available'])
        self.check_access(False)
        self.execute(approved)
        self.step()
        self.check_access(True)
        self.step()
        with self.m.SessionLocal() as db:
            execution = db.scalar(select(self.m.refund_execution['Execution']))
            self.assertEqual(execution.status, 'resources_applied')
            self.assertTrue(execution.proof_json)
            self.assertEqual(db.scalar(select(self.m.refund_execution['PaymentLeg'])).status, 'prepared')
            self.assertEqual(db.get(self.m.Order, self.order_id).refunded_amount, 0)
            self.assertIsNotNone(db.scalar(select(self.m.refund_file_resources['Recovery'])))
            self.assertEqual(db.query(self.m.subscription_terms['Term']).count(), 0)
        for _ in range(3):
            self.step()
        self.check_access(True)
        with self.m.SessionLocal() as db:
            refund = db.get(self.m.Refund, approved['id'])
            self.assertEqual(refund.status, 'completed')
            settlement = self.m.refund_settlement['negative_settlement'](db, refund)
            self.assertEqual(settlement.net_amount, Decimal('-20.00'))
            self.assertEqual(settlement.cost_amount, Decimal('-2.00'))
            self.assertEqual(settlement.profit_amount, Decimal('-18.00'))
            db.commit()

    def test_api_manual_assessment_cannot_enable_execution(self):
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, self.order_id)
            order.delivery_method_snapshot = 'api'
            delivery = json.loads(order.delivery_snapshot_json)
            delivery['method'] = 'api'
            order.delivery_snapshot_json = canonical(delivery)
            db.commit()
        approved = self.approved()
        self.assertFalse(approved['execution_available'])
        self.execute(approved, expected=409)

    def test_missing_frozen_cost_blocks_file_approval(self):
        row = self.create()
        assessed = self.manual(row)
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, self.order_id)
            economic = json.loads(order.economic_snapshot_json)
            del economic['total_cost']
            order.economic_snapshot_json = canonical(economic)
            db.commit()
        self.review(assessed, decision='approve', expected=409)

    def test_funds_failure_retains_receipt_and_retry_completes(self):
        approved = self.approved()
        self.execute(approved)
        for _ in range(3):
            self.step()
        with self.m.SessionLocal() as db:
            receipt = db.scalar(select(self.m.refund_file_resources['Recovery'])).proof_json
            payment = db.get(self.m.Payment, 'paid-source')
            payment.status = 'failed'
            db.commit()
        self.step()
        failed = self.request('GET', f"/api/refunds/{approved['id']}", actor='ops')
        self.assertEqual(failed['status'], 'failed')
        self.check_access(True)
        with self.m.SessionLocal() as db:
            db.get(self.m.Payment, 'paid-source').status = 'paid'
            db.commit()
        self.execute(failed, operation='retry')
        self.step()
        self.step()
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Refund, approved['id']).status, 'completed')
            rows = db.scalars(select(self.m.refund_file_resources['Recovery'])).all()
            self.assertEqual([row.proof_json for row in rows], [receipt])

    def test_two_partial_refunds_reverse_full_frozen_cost_with_delayed_settlement(self):
        first = self.approved('33.33')
        self.execute(first)
        for _ in range(5):
            self.step()
        second = self.create('66.67', idempotency_key='second-refund-request')
        assessed = self.manual(second, refundable_limit='100.00', idempotency_key='second-manual-assessment')
        approved = self.review(assessed, decision='approve', idempotency_key='second-refund-review')
        self.assertTrue(approved['execution_available'])
        self.execute(approved, key='second-refund-execute')
        for _ in range(5):
            self.step()
        self.check_access(True)
        with self.m.SessionLocal() as db:
            settlements = [self.m.refund_settlement['negative_settlement'](db,
                db.get(self.m.Refund, row['id'])) for row in (first, approved)]
            self.assertEqual(sum(row.net_amount for row in settlements), Decimal('-100.00'))
            self.assertEqual(sum(row.cost_amount for row in settlements), Decimal('-10.00'))
            self.assertEqual(db.get(self.m.Order, self.order_id).refunded_amount, Decimal('100.00'))
            execution = db.scalar(select(self.m.refund_execution['Execution']).where(
                self.m.refund_execution['Execution'].refund_id == first['id']))
            original_proof = json.loads(execution.proof_json)
            db.expunge(execution)
            db.commit()
        self.assertEqual(self.m.refund_file_resources['apply'](None, execution), original_proof)
        self.create('0.01', expected=409, idempotency_key='third-over-refund')
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.refund_file_resources['Recovery']).count(), 2)

    def test_failed_proof_phase_commit_replays_durable_recovery(self):
        self.execute(self.approved())
        self.step()
        publish = self.m.refund_execution['publish']
        def fail_publish(db, execution, refund, case, action, before):
            if action == 'resources_applied':
                raise RuntimeError('injected phase commit failure')
            return publish(db, execution, refund, case, action, before)
        with patch.dict(self.m.refund_execution, publish=fail_publish):
            self.step()
        with self.m.SessionLocal() as db:
            execution = db.scalar(select(self.m.refund_execution['Execution']))
            self.assertEqual(execution.status, 'failed')
            self.assertIsNone(execution.proof_json)
            receipt = db.scalar(select(self.m.refund_file_resources['Recovery'])).proof_json
        self.check_access(True)
        failed = self.request('GET', '/api/refunds', actor='ops')['items'][0]
        self.execute(failed, operation='retry')
        self.step()
        with self.m.SessionLocal() as db:
            execution = db.scalar(select(self.m.refund_execution['Execution']))
            self.assertEqual(execution.status, 'resources_applied')
            self.assertEqual(execution.proof_json, receipt)


if __name__ == '__main__':
    unittest.main()
