"""Isolated platform DB and real mock SQLite operations; no external services."""
from datetime import timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
import test_message_business_events as fixture


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class SaaSAutoDeliveryTests(unittest.TestCase):
    setUpClass = classmethod(fixture.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.BusinessRoutesHTTPTests.tearDownClass.__func__)
    tearDown = fixture.BusinessRoutesHTTPTests.tearDown
    request = fixture.BusinessRoutesHTTPTests.request
    pay = fixture.BusinessRoutesHTTPTests.pay
    task_id = fixture.BusinessRoutesHTTPTests.task_id
    fail_after_create = fixture.BusinessRoutesHTTPTests.fail_after_create

    def setUp(self):
        fixture.BusinessRoutesHTTPTests.setUp(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(__file__).parents[2] / 'mock-saas/app/store.py'
        spec = importlib.util.spec_from_file_location('saas_delivery_fixture_store', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.store = module.Store(Path(self.temp.name) / 'mock.sqlite3')
        pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode()
        self.env = patch.dict(os.environ, OIDC_ISSUER='https://platform.example.invalid',
            OIDC_SIGNING_KEY_PEM=pem, OIDC_ALLOW_HTTP_DEVELOPMENT='false',
            OIDC_CREDENTIAL_ENCRYPTION_KEY=Fernet.generate_key().decode())
        self.env.start()
        self.addCleanup(self.env.stop)
        with self.m.SessionLocal() as db:
            db.get(self.m.Enterprise, 'buyer_tenant').verification_status = 'verified'
            product = db.get(self.m.Product, 'product')
            product.product_type, product.delivery_method = 'saas', 'tenant_access'
            product.application_url = 'https://saas.example.invalid/app'
            product.integration_api_url = 'https://saas.example.invalid/isv.php'
            product.oidc_redirect_uris_json = '["https://saas.example.invalid/callback"]'
            db.add(self.m.SaaSProductVersion(id='version', product_id='product',
                version_code='v1', name='v1', monthly_price=100, cost=10, status='active'))
            db.add(self.m.SaaSIntegrationConfig(product_id='product', base_url='https://saas.example.invalid',
                operation_path='/isv.php', client_id='machine-client', client_secret='private-fixture-secret',
                token_url='https://platform.example.invalid/oauth/token',
                scope='saas.tenant saas.user saas.department', auth_mode='oauth2', status='active'))
            db.add(self.m.User(id='delivery-worker', name='Worker', email='jfsh@market.local',
                password_hash='unused', platform_role='delivery_monitor', verified_status='verified',
                activation_status='active'))
            db.commit()
        oidc = self.request('POST', '/api/products/product/oidc-client', actor='platform')
        self.cid = oidc['client_id']
        with self.store.connect() as db:
            db.execute('INSERT INTO clients VALUES (?, ?)', (self.cid,
                json.dumps({'client_id': self.cid, 'product_id': 'product'})))
        self.calls, self.failures, self.bad_status = [], {}, None
        self.call_patch = patch.object(self.m, 'saas_call', side_effect=self.remote)
        self.call_patch.start()
        self.addCleanup(self.call_patch.stop)

    def remote(self, config, operation, payload):
        self.calls.append((operation, json.loads(json.dumps(payload))))
        if self.failures.get(operation, 0):
            self.failures[operation] -= 1
            raise self.m.HTTPException(502, 'PRIVATE ACCESS TOKEN MUST NOT LEAK')
        response = self.store.machine_operation({'type': operation, **payload},
            {'active': True, 'product_id': 'product', 'scope': 'saas.tenant saas.user saas.department'})
        if operation == 'STATUS' and self.bad_status:
            response.update(self.bad_status)
        return response

    def create(self, months=1):
        return self.request('POST', '/api/orders', json={'product_id': 'product',
            'product_version_id': 'version', 'buyer_enterprise_id': 'buyer_tenant',
            'subscription_months': months})['id']

    def step(self, oid):
        with self.m.SessionLocal() as db:
            task = db.get(self.m.DeliveryTask, self.task_id(oid))
            if task.status in {'in_delivery', 'retrying'}:
                task.next_retry_at = self.m.now() - timedelta(seconds=1)
                db.commit()
        return self.m.online_delivery['process_once']()

    def finish(self, oid):
        for _ in range(30):
            with self.m.SessionLocal() as db:
                status = db.get(self.m.DeliveryTask, self.task_id(oid)).status
            if status in {'completed', 'exception'}:
                return status
            self.step(oid)
        self.fail('Staged SaaS delivery did not converge')

    def test_unpaid_does_not_provision_and_first_purchase_is_verified(self):
        oid = self.create(3)
        self.assertFalse(self.m.online_delivery['process_once']())
        self.assertEqual(self.calls, [])
        self.pay(oid)
        self.assertEqual(self.finish(oid), 'completed')
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            sub = db.get(self.m.SaaSSubscription, order.subscription_id)
            self.assertEqual((order.main_status, order.delivery_status, sub.status),
                ('completed', 'accepted', 'active'))
            term = db.query(self.m.SubscriptionTerm).filter_by(order_id=oid).one()
            self.assertEqual(term.months, 3)
            self.assertEqual(utc(sub.expires_at), utc(term.ends_at))
            self.assertEqual(db.query(self.m.Order).count(), 1)
            self.assertEqual(db.query(self.m.Payment).count(), 1)
            self.assertEqual(db.query(self.m.SaaSUserMapping).count(), 1)
        data = self.request('GET', f'/api/orders/{oid}/delivery-access')
        self.assertTrue(data['ready'], data['reason'])
        self.assertEqual(data['saas']['application_url'], 'https://saas.example.invalid/app')
        self.assertEqual([op for op, _ in self.calls].count('OPEN'), 1)

    def test_renewal_reuses_tenant_and_extends_existing_anchor(self):
        oid = self.create()
        self.pay(oid)
        self.assertEqual(self.finish(oid), 'completed')
        with self.m.SessionLocal() as db:
            old = db.get(self.m.SaaSSubscription, db.get(self.m.Order, oid).subscription_id)
            sid, tenant_id, starts_at, expiry = old.id, old.external_tenant_id, old.starts_at, old.expires_at
        renewal = self.create(2)
        self.pay(renewal)
        self.assertEqual(self.finish(renewal), 'completed')
        with self.m.SessionLocal() as db:
            sub = db.get(self.m.SaaSSubscription, sid)
            term = db.query(self.m.SubscriptionTerm).filter_by(order_id=renewal).one()
            self.assertEqual((sub.id, sub.external_tenant_id, sub.starts_at), (sid, tenant_id, starts_at))
            self.assertEqual(utc(term.starts_at), utc(expiry))
            self.assertEqual(utc(sub.expires_at), utc(term.ends_at))
            self.assertEqual(db.query(self.m.SaaSSubscription).count(), 1)
        self.assertEqual([op for op, _ in self.calls].count('OPEN'), 1)

    def test_missing_oidc_fails_three_times_at_ten_second_intervals(self):
        oid = self.create()
        self.pay(oid)
        with self.m.SessionLocal() as db:
            db.query(self.m.OIDCClient).delete()
            db.commit()
        for count in range(1, 4):
            self.step(oid)
            with self.m.SessionLocal() as db:
                task = db.get(self.m.DeliveryTask, self.task_id(oid))
                self.assertEqual(task.retry_count, count)
                if count < 3:
                    self.assertAlmostEqual((utc(task.next_retry_at) - utc(self.m.now())).total_seconds(), 10, delta=2)
                else:
                    self.assertEqual(task.status, 'exception')
                    self.assertIsNone(task.next_retry_at)
                self.assertNotEqual(db.get(self.m.Order, oid).main_status, 'completed')
        self.assertEqual(self.calls, [])

    def test_transient_open_failure_retries_same_request_without_secret_leak(self):
        oid = self.create()
        self.pay(oid)
        self.failures['OPEN'] = 1
        self.assertEqual(self.finish(oid), 'completed')
        opens = [payload for op, payload in self.calls if op == 'OPEN']
        self.assertEqual(len(opens), 2)
        self.assertEqual(opens[0], opens[1])
        with self.m.SessionLocal() as db:
            task = db.get(self.m.DeliveryTask, self.task_id(oid))
            self.assertEqual(task.retry_count, 1)
            for row in db.query(self.m.AuditLog).all():
                self.assertNotIn('PRIVATE ACCESS TOKEN', str(row.detail))

    def test_wrong_tenant_or_admin_result_cannot_grant_entitlement(self):
        for bad in ({'ret': None}, {'ret': False}, {'enterprise_id': 'other'}, {'tenant_id': 'other'}, {'admin_active': False},
                    {'application_ready': False}, {'oidc_client_id': 'other'}):
            with self.subTest(result=bad):
                oid = self.create()
                self.pay(oid)
                self.bad_status = bad
                self.assertEqual(self.finish(oid), 'exception')
                with self.m.SessionLocal() as db:
                    self.assertEqual(db.query(self.m.SubscriptionTerm).filter_by(order_id=oid).count(), 0)
                    self.assertNotEqual(db.get(self.m.Order, oid).main_status, 'completed')
                self.bad_status = None

    def test_mutation_intent_is_persisted_before_any_remote_call(self):
        oid = self.create()
        self.pay(oid)
        self.step(oid)
        self.assertEqual(self.calls, [])
        with self.m.SessionLocal() as db:
            record = db.query(self.m.SaaSOperation).filter_by(operation='OPEN').one()
            self.assertEqual(record.status, 'prepared')
            key, payload = record.idempotency_key, json.loads(record.request_payload)
        self.step(oid)
        self.assertEqual(self.calls[0][1]['idempotency_key'], key)
        self.assertEqual(self.calls[0][1], payload)


if __name__ == '__main__':
    unittest.main()
