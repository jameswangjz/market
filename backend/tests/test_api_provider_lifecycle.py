"""Isolated file-backed DB tests: no main imports or external service calls."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import Column, Integer, String, Text, create_engine, select
from sqlalchemy.orm import Session, declarative_base

from app.api_provider_lifecycle import install, validate_payload


URL = 'https://provider.example/manage/full-endpoint'
START, END = '2026-10-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00'


class ProviderLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = create_engine('sqlite:///' + self.temp.name + '/provider.db')
        self.addCleanup(self.engine.dispose)
        Base = declarative_base()

        class Enterprise(Base):
            __tablename__ = 'enterprises'
            id = Column(String, primary_key=True)

        class Product(Base):
            __tablename__ = 'products'
            id = Column(String, primary_key=True)
            delivery_method = Column(String)
            integration_api_url = Column(String)

        class OAuthClient(Base):
            __tablename__ = 'oauth_clients'
            id = Column(String, primary_key=True)
            product_id = Column(String)
            status = Column(String)
            scope = Column(String)
            client_id = Column(String)
            client_secret = Column(String)

        class AuditRecord(Base):
            __tablename__ = 'audit_records'
            id = Column(Integer, primary_key=True)
            actor = Column(String)
            after_json = Column(Text)

        self.Product, self.Client = Product, OAuthClient
        self.Audit = AuditRecord
        self.calls, self.policies, self.configs = [], [], []
        self.corrupt = None
        self.http_status = 200
        self.evictions = []
        def token(config):
            self.configs.append(config)
            return 'machine-token'
        self.ns = dict(Base=Base, Product=Product, OAuthClient=OAuthClient,
            validate_upstream_url=lambda url: None,
            application_address_policy={'validate_url': lambda url, db: self.policies.append((url, db))},
            saas_access_token=token, validated_http_request=self.remote)
        self.ns['_evict_saas_token'] = lambda key, value: self.evictions.append((key, value))
        self.h = install(self.ns)
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as db:
            db.add_all([Enterprise(id='buyer'), Product(id='product', delivery_method='api', integration_api_url=URL),
                OAuthClient(id='machine', product_id='product', status='active', scope='resource.invoke resource.manage',
                    client_id='client', client_secret='secret')])
            db.commit()
        self.body = dict(schema_version=1, type='OPEN', event_id='event-1', idempotency_key='delivery:order',
            product_id='product', enterprise_id='buyer', combination_id='planned-combo', generation=1,
            tenant_id=None, order_id='order', order_no='ORDER-1', version={'id': 'version', 'code': 'v1'},
            subscription_months=1, starts_at=START, expires_at=END,
            periods=[dict(order_id='order', order_no='ORDER-1', term_id='planned-term', starts_at=START,
                expires_at=END, quotas=dict(rate_limit_per_minute=10, daily_quota=100,
                    monthly_quota=1000, quota_amount=1000))])

    def remote(self, method, url, **kwargs):
        body = json.loads(kwargs['content'])
        self.calls.append((method, url, copy.deepcopy(body), kwargs))
        result = dict(body, ret=0, tenant_id='remote-tenant', status='active', effective_at=START)
        if body['type'] == 'STATUS':
            result['type'] = result.pop('target_type', 'STATUS')
        if result['type'] == 'CLOSE':
            result['status'] = 'closed'
        if self.corrupt:
            self.corrupt(body, result)
        return SimpleNamespace(status_code=self.http_status, json=lambda: result)

    def stage(self, body=None):
        with Session(self.engine) as db:
            plan, created = self.h['prepare'](db, body or self.body, URL)
            self.assertTrue(created)
            plan_id = plan.id
            self.assertEqual(self.calls, [])
            db.commit()
            return plan_id

    def test_staging_and_actual_receipt_with_machine_token(self):
        pid = self.stage()
        with Session(self.engine) as db:
            plan = self.h['get_operation'](db, 'event-1')
            self.assertEqual(plan.id, pid)
            self.assertEqual(plan.status, 'prepared')
            result = self.h['execute'](db, plan)
            self.assertEqual(result['tenant_id'], 'remote-tenant')
            self.assertEqual(plan.status, 'verified')
            self.assertEqual(self.h['pending_operations'](db, 'buyer', 'product'), [])
            db.commit()
        self.assertEqual([r[2]['type'] for r in self.calls], ['OPEN', 'STATUS'])
        self.assertEqual(self.calls[0][1], URL)
        self.assertFalse(self.calls[0][3]['follow_redirects'])
        self.assertEqual(self.configs[0].scope, 'resource.manage')
        self.assertNotIn('secret', self.calls[0][3]['content'].decode())

    def install_audit(self):
        self.audit_calls = []
        def audit(db, actor, action, target_type, target_id, **kwargs):
            self.audit_calls.append((actor, action, target_type, target_id, kwargs))
            db.add(self.Audit(actor=actor, after_json=json.dumps(kwargs['after'])))
        self.ns['audit'] = audit

    def test_verified_receipt_audited_once_with_allowlisted_evidence(self):
        self.install_audit()
        pid = self.stage()
        with Session(self.engine) as db:
            self.h['execute'](db, db.get(self.h['Plan'], pid))
            db.commit()
        with Session(self.engine) as db:
            self.h['execute'](db, db.get(self.h['Plan'], pid))
            db.commit()
            self.assertEqual(len(db.scalars(select(self.Audit)).all()), 1)
        self.assertEqual(len(self.audit_calls), 1)
        actor, action, target_type, target_id, kwargs = self.audit_calls[0]
        self.assertEqual((actor, action, target_type, target_id),
            ('api-provider-worker', 'api_provider_verified', 'api_provider_receipt', pid))
        self.assertEqual(kwargs['category'], 'delivery')
        self.assertEqual(kwargs['business_domain'], 'trading')
        self.assertEqual(kwargs['order_id'], 'order')
        self.assertEqual(kwargs['tenant_id'], 'buyer')
        self.assertEqual(set(kwargs['after']), {'type', 'event_id', 'product_id', 'enterprise_id',
            'combination_id', 'generation', 'tenant_id', 'order_id', 'order_no', 'version',
            'starts_at', 'expires_at', 'periods', 'status', 'effective_at'})
        serialized = json.dumps(kwargs['after'])
        for private in ('machine-token', 'secret', 'cost', 'idempotency_key', URL):
            self.assertNotIn(private, serialized)

    def test_receipt_audit_rolls_back_with_parent_and_retries(self):
        self.install_audit()
        pid = self.stage()
        with Session(self.engine) as db:
            self.h['execute'](db, db.get(self.h['Plan'], pid))
            db.rollback()
        with Session(self.engine) as db:
            self.assertEqual(db.scalars(select(self.Audit)).all(), [])
            self.assertIsNone(db.get(self.h['Receipt'], pid))
            self.h['execute'](db, db.get(self.h['Plan'], pid))
            db.commit()
            self.assertEqual(len(db.scalars(select(self.Audit)).all()), 1)

    def test_failed_verification_emits_no_audit(self):
        self.install_audit()
        pid = self.stage()
        self.corrupt = lambda req, res: res.update(status='closed')
        with Session(self.engine) as db:
            with self.assertRaises(HTTPException):
                self.h['execute'](db, db.get(self.h['Plan'], pid))
            self.assertEqual(self.audit_calls, [])
            db.rollback()

    def test_audit_failure_propagates_and_caller_rolls_back_receipt(self):
        pid = self.stage()
        def broken_audit(*args, **kwargs):
            raise RuntimeError('audit unavailable')
        self.ns['audit'] = broken_audit
        with Session(self.engine) as db:
            with self.assertRaisesRegex(RuntimeError, 'audit unavailable'):
                self.h['execute'](db, db.get(self.h['Plan'], pid))
            db.rollback()
        with Session(self.engine) as db:
            self.assertIsNone(db.get(self.h['Receipt'], pid))
            self.assertIsNone(db.scalar(select(self.h['Tenant'])))

    def test_parent_rollback_replays_identical_open_with_null_tenant(self):
        pid = self.stage()
        with Session(self.engine) as db:
            self.h['execute'](db, db.get(self.h['Plan'], pid))
            db.rollback()
        with Session(self.engine) as db:
            self.assertIsNone(db.get(self.h['Receipt'], pid))
            self.assertIsNone(db.scalar(select(self.h['Tenant'])))
            self.h['execute'](db, db.get(self.h['Plan'], pid))
            db.commit()
        self.assertEqual(self.calls[0][3]['content'], self.calls[2][3]['content'])
        self.assertEqual(self.calls[0][3]['headers']['Idempotency-Key'], self.calls[2][3]['headers']['Idempotency-Key'])
        self.assertIsNone(self.calls[2][2]['tenant_id'])
        # Also replay after a successful local binding without enriching the body.
        with Session(self.engine) as db:
            self.h['execute'](db, self.body, URL)
            db.rollback()
        self.assertEqual(self.calls[0][3]['content'], self.calls[4][3]['content'])

    def test_uncommitted_plan_never_calls_provider(self):
        with Session(self.engine) as db:
            plan, _ = self.h['prepare'](db, self.body, URL)
            with self.assertRaises(HTTPException):
                self.h['execute'](db, plan)
            self.assertEqual(self.calls, [])
            db.rollback()

    def test_conflicting_key_cannot_replace_frozen_plan(self):
        self.stage()
        changed = copy.deepcopy(self.body)
        changed['periods'][0]['quotas']['daily_quota'] = 200
        with Session(self.engine) as db:
            with self.assertRaises(HTTPException):
                self.h['prepare'](db, changed, URL)
            db.rollback()

    def test_plan_orm_update_and_delete_rejected(self):
        pid = self.stage()
        for action in ('update', 'delete'):
            with self.subTest(action=action), Session(self.engine) as db:
                plan = db.get(self.h['Plan'], pid)
                if action == 'update':
                    plan.endpoint = 'https://evil.example/manage'
                else:
                    db.delete(plan)
                with self.assertRaises(ValueError):
                    db.flush()
                db.rollback()

    def test_provider_evidence_fail_closed(self):
        pid = self.stage()
        corruptions = [lambda req, res: res.update(ret=False),
            lambda req, res: res.update(ret='0'), lambda req, res: res.pop('ret'),
            lambda req, res: res.update(product_id='different'),
            lambda req, res: res.update(enterprise_id='other'),
            lambda req, res: res.update(combination_id='other'),
            lambda req, res: res.update(generation=2),
            lambda req, res: res.update(event_id='other'),
            lambda req, res: res.update(expires_at='2026-12-01T00:00:00Z'),
            lambda req, res: res.update(periods=[]),
            lambda req, res: res.pop('effective_at'),
            lambda req, res: res.update(effective_at=END) if req['type'] == 'STATUS' else None,
            lambda req, res: res.update(status='closed') if req['type'] == 'STATUS' else None]
        for corrupt in corruptions:
            with self.subTest(corrupt=corrupt), Session(self.engine) as db:
                self.corrupt = corrupt
                with self.assertRaises(HTTPException) as ctx:
                    self.h['execute'](db, db.get(self.h['Plan'], pid))
                self.assertEqual(ctx.exception.status_code, 502)
                self.assertIsNone(db.get(self.h['Receipt'], pid))
                db.rollback()

    def test_unmanaged_is_explicit_without_sync(self):
        with Session(self.engine) as db:
            db.get(self.Product, 'product').integration_api_url = ''
            db.commit()
            plan, created = self.h['prepare'](db, self.body, '')
            self.assertFalse(created)
            self.assertEqual(self.h['execute'](db, plan), {'status': 'skipped_unmanaged', 'provider_synced': False})
            self.assertEqual(self.calls, [])

    def test_frozen_endpoint_survives_live_config_change(self):
        pid = self.stage()
        with Session(self.engine) as db:
            db.get(self.Product, 'product').integration_api_url = 'https://other.example/manage'
            self.h['execute'](db, db.get(self.h['Plan'], pid))
            self.assertEqual(self.calls[0][1], URL)
            db.rollback()

    def test_disabled_machine_client_fails_closed(self):
        pid = self.stage()
        with Session(self.engine) as db:
            db.get(self.Client, 'machine').scope = 'resource.invoke'
            with self.assertRaises(HTTPException):
                self.h['execute'](db, db.get(self.h['Plan'], pid))
            self.assertEqual(self.calls, [])

    def test_legacy_empty_frozen_endpoint_never_uses_live_configuration(self):
        with Session(self.engine) as db:
            self.assertEqual(db.get(self.Product, 'product').integration_api_url, URL)
            result, created = self.h['prepare'](db, self.body, '')
            self.assertEqual(result['status'], 'skipped_unmanaged')
            self.assertFalse(created)
            self.assertEqual(self.calls, [])

    def test_url_policy_rechecked_before_dispatch(self):
        pid = self.stage()
        def denied(url, db):
            raise ValueError('denied')
        self.ns['application_address_policy']['validate_url'] = denied
        with Session(self.engine) as db:
            with self.assertRaises(HTTPException):
                self.h['execute'](db, db.get(self.h['Plan'], pid))
            self.assertEqual(self.calls, [])

    def test_binding_is_scoped_to_enterprise_product_combination(self):
        pid = self.stage()
        with Session(self.engine) as db:
            db.add(self.h['Tenant'](enterprise_id='buyer', product_id='product',
                combination_id='unrelated-combo', tenant_id='other-tenant'))
            db.commit()
            with self.assertRaises(HTTPException):
                self.h['execute'](db, db.get(self.h['Plan'], pid))
            self.assertEqual(self.calls, [])

    def test_receipt_first_effective_at_cannot_drift_on_replay(self):
        pid = self.stage()
        with Session(self.engine) as db:
            self.h['execute'](db, db.get(self.h['Plan'], pid))
            db.commit()
        self.corrupt = lambda req, res: res.update(effective_at=END)
        with Session(self.engine) as db:
            with self.assertRaises(HTTPException):
                self.h['execute'](db, db.get(self.h['Plan'], pid))
            db.rollback()

    def test_401_evicts_only_the_request_token_without_changing_plan(self):
        pid = self.stage()
        self.http_status = 401
        with Session(self.engine) as db:
            with self.assertRaises(HTTPException) as ctx:
                self.h['execute'](db, db.get(self.h['Plan'], pid))
            self.assertNotIn('machine-token', ctx.exception.detail)
            self.assertEqual(self.evictions, [('api-provider:machine', 'machine-token')])
            db.rollback()
        self.http_status = 200
        with Session(self.engine) as db:
            self.h['execute'](db, db.get(self.h['Plan'], pid))
            db.rollback()
        self.assertEqual(self.calls[0][3]['content'], self.calls[1][3]['content'])

    def test_all_provider_events_use_verified_protocol(self):
        for kind in ('OPEN', 'RENEW', 'CHANGE', 'CLOSE', 'RESTORE', 'ADJUST', 'STATUS'):
            with self.subTest(kind=kind):
                body = dict(self.body, type=kind, event_id='event:' + kind, idempotency_key='key:' + kind)
                if kind in {'CLOSE', 'ADJUST'}:
                    body['periods'] = []
                with Session(self.engine) as db:
                    plan, _ = self.h['prepare'](db, body, URL)
                    pid = plan.id
                    db.commit()
                with Session(self.engine) as db:
                    result = self.h['execute'](db, db.get(self.h['Plan'], pid))
                    self.assertEqual(result['type'], kind)
                    self.assertEqual(result['status'], 'closed' if kind == 'CLOSE' else 'active')
                    db.rollback()

    def test_expiry_stages_then_reserves_then_closes_only_combination(self):
        pid = self.stage()
        with Session(self.engine) as db:
            self.h['execute'](db, db.get(self.h['Plan'], pid))
            db.commit()
        at = datetime(2026, 11, 2, tzinfo=timezone.utc)
        for expected in ('prepared', 'reserved', 'verified'):
            with Session(self.engine) as db:
                result = self.h['expire_once'](db, blocked=lambda db, body: False, at=at)
                self.assertEqual(result[0]['status'], expected)
                db.commit()
            at += timedelta(seconds=1)
        close = self.calls[-2][2]
        self.assertEqual(close['type'], 'CLOSE')
        self.assertEqual(close['combination_id'], 'planned-combo')
        self.assertEqual(close['tenant_id'], 'remote-tenant')
        self.assertEqual(close['periods'], [])
        self.assertEqual(close['expires_at'], END)
        with Session(self.engine) as db:
            self.assertEqual(self.h['expire_once'](db, blocked=lambda db, body: False, at=at), [])

    def test_expiry_three_failures_require_manual_intervention(self):
        pid = self.stage()
        with Session(self.engine) as db:
            self.h['execute'](db, db.get(self.h['Plan'], pid))
            db.commit()
        self.http_status = 503
        at = datetime(2026, 11, 2, tzinfo=timezone.utc)
        with Session(self.engine) as db:
            self.h['expire_once'](db, blocked=lambda db, body: False, at=at)
            db.commit()
        for number in range(1, 4):
            with Session(self.engine) as db:
                self.assertEqual(self.h['expire_once'](db, blocked=lambda db, body: False, at=at)[0]['status'], 'reserved')
                db.commit()
            with Session(self.engine) as db:
                result = self.h['expire_once'](db, blocked=lambda db, body: False, at=at + timedelta(seconds=1))
                self.assertEqual(result[0]['attempts'], number)
                self.assertEqual(result[0]['status'], 'needs_manual' if number == 3 else 'prepared')
                attempt = db.scalar(select(self.h['ExpiryAttempt']))
                self.assertEqual(attempt.next_retry_at, at + timedelta(seconds=11))
                db.commit()
            # A worker may poll, but it must not reserve before the backoff ends.
            if number < 3:
                with Session(self.engine) as db:
                    self.assertEqual(self.h['expire_once'](db, blocked=lambda db, body: False,
                        at=at + timedelta(seconds=10)), [])
                    self.assertEqual(db.scalar(select(self.h['ExpiryAttempt'])).attempts, number)
            at += timedelta(seconds=11)
        count = len(self.calls)
        with Session(self.engine) as db:
            result = self.h['expire_once'](db, blocked=lambda db, body: False, at=at)
            self.assertEqual(result[0]['status'], 'needs_manual')
        self.assertEqual(len(self.calls), count)

    def test_unused_compensation_plan_does_not_block_expiry_after_saga_complete(self):
        pid = self.stage()
        with Session(self.engine) as db:
            self.h['execute'](db, db.get(self.h['Plan'], pid))
            db.commit()
        compensation = dict(self.body, type='CHANGE', event_id='unused-original',
            idempotency_key='compensation:original', tenant_id='remote-tenant',
            change_id='completed-change', direction='restore')
        with Session(self.engine) as db:
            plan, _ = self.h['prepare'](db, compensation, URL)
            unused_id = plan.id
            db.commit()
        at = datetime(2026, 11, 2, tzinfo=timezone.utc)
        for expected in ('prepared', 'reserved', 'verified'):
            with Session(self.engine) as db:
                result = self.h['expire_once'](db, blocked=lambda db, body: False, at=at)
                self.assertEqual(result[0]['status'], expected)
                db.commit()
            at += timedelta(seconds=1)
        with Session(self.engine) as db:
            self.assertIsNone(db.get(self.h['Receipt'], unused_id))
            self.assertEqual(db.get(self.h['Plan'], unused_id).status, 'prepared')
        self.assertEqual(self.calls[-2][2]['type'], 'CLOSE')

    def test_expiry_crashed_reservation_keeps_five_minute_lease(self):
        pid = self.stage()
        with Session(self.engine) as db:
            self.h['execute'](db, db.get(self.h['Plan'], pid))
            db.commit()
        at = datetime(2026, 11, 2, tzinfo=timezone.utc)
        for expected in ('prepared', 'reserved'):
            with Session(self.engine) as db:
                self.assertEqual(self.h['expire_once'](db, blocked=lambda db, body: False,
                    at=at)[0]['status'], expected)
                db.commit()
        count = len(self.calls)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(self.h['ExpiryAttempt'])).next_retry_at,
                at + timedelta(minutes=5))
            result = self.h['expire_once'](db, blocked=lambda db, body: False,
                at=at + timedelta(minutes=5))
            self.assertEqual(result[0]['status'], 'prepared')
            self.assertEqual(db.scalar(select(self.h['ExpiryAttempt'])).attempts, 1)
            db.commit()
        self.assertEqual(len(self.calls), count)

    def test_expiry_requires_explicit_blocker_clearance(self):
        pid = self.stage()
        with Session(self.engine) as db:
            self.h['execute'](db, db.get(self.h['Plan'], pid))
            db.commit()
        count = len(self.calls)
        with Session(self.engine) as db:
            result = self.h['expire_once'](db, blocked=lambda db, body: None,
                at=datetime(2026, 11, 2, tzinfo=timezone.utc))
            self.assertEqual(result[0]['status'], 'blocked')
            self.assertEqual(len(self.calls), count)
            self.assertEqual(db.scalars(select(self.h['Plan'])).all()[0].id, pid)

    def test_secret_fields_and_boolean_quotas_rejected(self):
        for extra in ({'buyer_secret': 'x'}, {'cost': 10}):
            with self.assertRaises(HTTPException):
                validate_payload(dict(self.body, **extra))
        bad = copy.deepcopy(self.body)
        bad['periods'][0]['quotas']['daily_quota'] = True
        with self.assertRaises(HTTPException):
            validate_payload(bad)


if __name__ == '__main__':
    unittest.main()
