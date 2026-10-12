"""Focused contract tests for the durable version-change orchestration."""
import importlib
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from sqlalchemy import Boolean, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, mapped_column, sessionmaker
from app.subscription_terms import UTCDateTime


worker = importlib.import_module('app.version_change_worker')


class Base(DeclarativeBase):
    pass


class WorkerBase(DeclarativeBase):
    pass


class WorkerRequest(WorkerBase):
    __tablename__ = 'worker_requests'
    id = mapped_column(String, primary_key=True)
    status = mapped_column(String)
    next_retry_at = mapped_column(DateTime(timezone=True))
    lease_expires_at = mapped_column(DateTime(timezone=True))
    lease_owner = mapped_column(String)
    created_at = mapped_column(DateTime(timezone=True))
    updated_at = mapped_column(DateTime(timezone=True))
    retries = mapped_column(Integer)
    last_error = mapped_column(String)
    resource_plan_json = mapped_column(Text)
    combination_id = mapped_column(String)
    old_version_id = mapped_column(String)
    old_version_code = mapped_column(String)
    kind = mapped_column(String)
    evidence_json = mapped_column(Text)
    resource_applied = mapped_column(Integer)
    compensation_started = mapped_column(Boolean)
    compensation_retries = mapped_column(Integer)


class WorkerCombination(WorkerBase):
    __tablename__ = 'worker_combinations'
    id = mapped_column(String, primary_key=True)
    status = mapped_column(String)
    version_id = mapped_column(String)
    version_code = mapped_column(String)
    anchor_at = mapped_column(UTCDateTime())
    total_months = mapped_column(Integer)


class VersionChangeSagaTests(unittest.TestCase):
    def test_resource_proof_is_change_bound_and_sha256(self):
        change = SimpleNamespace(id='change-1')
        good = {'request_change_id': 'change-1', 'resource_change_id': 'change-1',
                'evidence_fingerprint': 'a' * 64}
        worker._verify_proof(change, good)
        for proof in (
            {**good, 'request_change_id': 'other'},
            {**good, 'resource_change_id': 'other'},
            {**good, 'evidence_fingerprint': 'g' * 64},
            {k: v for k, v in good.items() if k != 'evidence_fingerprint'},
        ):
            with self.subTest(proof=proof), self.assertRaises(ValueError):
                worker._verify_proof(change, proof)

    def test_request_input_rejects_unknown_fields_and_whitespace_key(self):
        from app.version_change_saga import CreateBody

        with self.assertRaises(Exception):
            CreateBody(quote_token='signed', idempotency_key='key', internal_cost=7)
        with self.assertRaises(Exception):
            CreateBody(quote_token='signed', idempotency_key='two words')
        parsed = CreateBody(quote_token='signed', idempotency_key='key-1')
        self.assertEqual(parsed.idempotency_key, 'key-1')

    def test_idempotency_digest_and_quote_owner_binding(self):
        from app.version_change_saga import quote_claims_match_owner, quote_token_digest

        token = 'signed-quote-fixture'
        self.assertEqual(quote_token_digest(token), quote_token_digest(token))
        self.assertNotEqual(quote_token_digest(token), quote_token_digest(token + '-changed'))
        claims = {'purpose': 'version_change_quote', 'sub': 'buyer-1', 'order_id': 'order-1'}
        self.assertTrue(quote_claims_match_owner(claims, 'buyer-1', 'order-1'))
        self.assertFalse(quote_claims_match_owner(claims, 'buyer-2', 'order-1'))
        self.assertFalse(quote_claims_match_owner(claims, 'buyer-1', 'order-2'))

    def test_retry_worker_configuration_is_bounded(self):
        self.assertEqual(worker.read_config({}), (2, 10))
        self.assertEqual(worker.read_config({'VERSION_CHANGE_WORKER_INTERVAL': '0.5',
                                             'VERSION_CHANGE_WORKER_BATCH': '3'}), (0.5, 3))
        for values in (
            {'VERSION_CHANGE_WORKER_INTERVAL': '0'},
            {'VERSION_CHANGE_WORKER_INTERVAL': '61'},
            {'VERSION_CHANGE_WORKER_BATCH': '0'},
            {'VERSION_CHANGE_WORKER_BATCH': '101'},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                worker.read_config(values)

    def test_resource_failure_persists_redacted_retry_then_compensation_phase(self):
        engine = create_engine('sqlite://')
        WorkerBase.metadata.create_all(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        instant = datetime(2026, 10, 12, tzinfo=timezone.utc)
        with sessions() as db:
            db.add(WorkerRequest(id='change-1', status='resource_pending', next_retry_at=None,
                lease_expires_at=None, lease_owner='', created_at=instant, updated_at=instant,
                retries=0, last_error='', resource_plan_json='{"kind":"fixture"}'))
            db.add(WorkerCombination(id='combo', status='active', version_id='v1', version_code='1',
                anchor_at=datetime(2026, 1, 1, tzinfo=timezone.utc), total_months=12))
            db.commit()
        main = SimpleNamespace(SessionLocal=sessions, version_change_saga={'VersionChangeRequest': WorkerRequest},
            now=lambda: instant, json=importlib.import_module('json'),
            audit=Mock(),
            version_change_resources={'apply': Mock(side_effect=RuntimeError('private token value'))},
            subscription_terms={'Combination': WorkerCombination}, version_change_ledger={})
        with sessions() as db:
            row = db.get(WorkerRequest, 'change-1')
            row.combination_id = 'combo'; row.old_version_id = 'v1'; row.old_version_code = '1'
            db.commit()
        with patch.object(worker.importlib, 'import_module', return_value=main):
            for seconds in (0, 10, 20):
                when = instant + timedelta(seconds=seconds)
                self.assertTrue(worker.process_once(worker_id='test-worker', now=lambda when=when: when))
        with sessions() as db:
            changed = db.get(WorkerRequest, 'change-1')
            self.assertEqual(changed.status, 'compensating')
            self.assertEqual(changed.retries, 3)
            self.assertTrue(changed.compensation_started)
            self.assertEqual(changed.compensation_retries, 0)
            self.assertEqual(changed.last_error, 'RuntimeError')
            self.assertNotIn('private', changed.last_error)
            self.assertIsNotNone(changed.next_retry_at)
        self.assertEqual(main.version_change_resources['apply'].call_count, 3)
        engine.dispose()

    def test_verified_restore_voids_ledger_and_refunds_upgrade_funding_once(self):
        engine = create_engine('sqlite://')
        WorkerBase.metadata.create_all(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        instant = datetime(2026, 10, 12, tzinfo=timezone.utc)
        with sessions() as db:
            db.add(WorkerRequest(id='change-restore', status='compensating', next_retry_at=None,
                lease_expires_at=None, lease_owner='', created_at=instant, updated_at=instant,
                retries=1, last_error='', resource_plan_json='{"kind":"fixture"}', kind='upgrade',
                evidence_json='{}', resource_applied=False))
            db.commit()
        proof = {'request_change_id': 'change-restore', 'resource_change_id': 'change-restore',
                 'evidence_fingerprint': 'b' * 64}
        resources = {'apply': Mock(return_value=proof)}
        void = Mock()
        refund = Mock()
        main = SimpleNamespace(SessionLocal=sessions,
            version_change_saga={'VersionChangeRequest': WorkerRequest, 'refund_upgrade_funding': refund},
            now=lambda: instant, json=importlib.import_module('json'), version_change_resources=resources,
            version_change_ledger={'void': void}, subscription_terms={}, audit=Mock())
        with patch.object(worker.importlib, 'import_module', return_value=main):
            self.assertTrue(worker.process_once(worker_id='test-worker', now=lambda: instant))
        with sessions() as db:
            self.assertEqual(db.get(WorkerRequest, 'change-restore').status, 'compensated')
        resources['apply'].assert_called_once_with(unittest.mock.ANY, unittest.mock.ANY, direction='original')
        void.assert_called_once()
        refund.assert_called_once()
        engine.dispose()

    def test_worker_heartbeats_only_after_committed_process_result(self):
        stopped = Mock()
        stopped.is_set.side_effect = [False, False, False, True]
        stopped.wait.return_value = None
        process = Mock(side_effect=[True, False])
        heartbeat = Mock()
        worker.run_worker(process, stopped, heartbeat=heartbeat, monotonic=lambda: 0,
                          interval=0.001)
        self.assertEqual(process.call_count, 2)
        self.assertEqual(heartbeat.touch.call_count, 2)

    def test_public_response_is_fixed_whitelist(self):
        from app.version_change_saga import install

        app = FastAPI()
        order_model = type('Order', (), {})
        ns = {
            'Base': Base, 'Order': order_model, 'app': app,
            'now': lambda: None, 'current_user': lambda: None, 'db_session': lambda: None,
            'Payment': type('Payment', (), {}), 'Membership': type('Membership', (), {}),
            'OrderStateLog': type('OrderStateLog', (), {}), 'Product': type('Product', (), {}),
            'SaaSProductVersion': type('SaaSProductVersion', (), {}),
            'ProductReleaseVersion': type('ProductReleaseVersion', (), {}),
        }
        install(ns)
        change = SimpleNamespace(id='0123456789abcdef', source_order_id='source', combination_id='combo',
            enterprise_id='enterprise', kind='upgrade', status='pending_payment',
            old_version_code='v1', target_version_code='v2', quote_json='{"net_difference":5,"payment_amount":5,"refund_amount":0,"currency":"USD","cost":99}',
            funding_order_id=None, retries=0, last_error='', created_at=None, updated_at=None, completed_at=None)
        result = ns['version_change_saga']['public'](change)
        self.assertEqual(set(result), {'id','change_no','source_order_id','combination_id','enterprise_id','kind',
            'status','current_version_code','target_version_code','net_difference','payment_amount','refund_amount',
            'currency','funding_order_id','funding_order_no','payment_status','refund_status','retry_count',
            'last_error','created_at','updated_at','completed_at'})
        self.assertNotIn('cost', result)
        self.assertEqual(result['last_error'], '')


if __name__ == '__main__':
    unittest.main()
