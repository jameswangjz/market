"""Mock API wire contract, durable receipts, and OAuth boundary regressions."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from fastapi import HTTPException
from sqlalchemy import Column, String, create_engine
from sqlalchemy.orm import Session, declarative_base


SOURCE = Path(__file__).resolve().parents[2] / 'mock-api/app/main.py'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import api_provider_lifecycle as backend


def load_provider():
    spec = importlib.util.spec_from_file_location('mock_api_lifecycle', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MockAPILifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'mock-api.sqlite3')
        self.environment = patch.dict('os.environ', {'MOCK_API_DB_PATH': self.path, 'MOCK_API_PRODUCT_ID': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.module = load_provider()
        self.claims = dict(active=True, product_id='product', scope='resource.manage')
        self.introspect = patch.object(self.module.httpx, 'post', side_effect=self.token_response)
        self.introspect.start()
        self.addCleanup(self.introspect.stop)
        self.client = TestClient(self.module.app)
        self.body = dict(schema_version=1, type='OPEN', event_id='open', idempotency_key='open-key',
            product_id='product', enterprise_id='enterprise', combination_id='combination',
            generation=1, tenant_id=None, order_id='order', order_no='ORDER-1', version={'id': 'version-1', 'code': 'v1'},
            subscription_months=1, starts_at='2090-01-01T00:00:00Z',
            expires_at='2090-02-01T00:00:00Z', periods=[dict(order_id='order', order_no='ORDER-1',
                term_id='term', starts_at='2090-01-01T00:00:00Z', expires_at='2090-02-01T00:00:00Z',
                quotas=dict(rate_limit_per_minute=10, daily_quota=100, monthly_quota=1000, quota_amount=1000))])

    def token_response(self, *args, **kwargs):
        return httpx.Response(200, json=self.claims, request=httpx.Request('POST', 'http://platform/oauth/introspect'))

    def call(self, body, expected=200, headers=None):
        response = self.client.post('/management', json=body,
            headers={'Authorization': 'Bearer machine-token', **(headers or {})})
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    def next_body(self, receipt, operation, key):
        return dict(deepcopy(self.body), type=operation, event_id=key, idempotency_key=key + '-key',
                    tenant_id=receipt['tenant_id'])

    def status(self, receipt, **overrides):
        payload = {key: receipt[key] for key in backend.ROOT | backend.OPTIONAL if key in receipt}
        return self.call(dict(payload, type='STATUS', target_type=receipt['type'], **overrides))

    def backend_harness(self):
        """Run the real backend journal and its private receipt verifier over HTTP."""
        engine = create_engine('sqlite:///' + str(Path(self.directory.name) / 'backend.sqlite3'))
        self.addCleanup(engine.dispose)
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

        self.wire_calls = []

        def token(config):
            self.assertEqual(config.scope, 'resource.manage')
            self.claims['scope'] = config.scope
            return 'machine-token'

        def transport(method, url, **kwargs):
            response = self.client.request(method, '/management',
                content=kwargs['content'], headers=kwargs['headers'])
            self.wire_calls.append((json.loads(kwargs['content']), response.json()))
            return response

        hooks = backend.install(dict(Base=Base, Product=Product, OAuthClient=OAuthClient,
            validate_upstream_url=lambda url: None,
            application_address_policy={'validate_url': lambda url, db: None},
            saas_access_token=token, validated_http_request=transport))
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            db.add_all([Enterprise(id='enterprise'),
                Product(id='product', delivery_method='api', integration_api_url='https://provider.example/management'),
                OAuthClient(id='machine', product_id='product', status='active', scope='resource.invoke resource.manage',
                    client_id='client', client_secret='not-sent-to-provider')])
            db.commit()
        return engine, hooks

    def stage_backend(self, engine, hooks, payload):
        body = backend.validate_payload(payload)
        with Session(engine) as db:
            plan, _ = hooks['prepare'](db, body, 'https://provider.example/management')
            plan_id = plan.id
            db.commit()
        return plan_id

    def execute_backend(self, engine, hooks, plan_id, commit=True):
        with Session(engine) as db:
            result = hooks['execute'](db, db.get(hooks['Plan'], plan_id))
            if commit:
                db.commit()
            else:
                db.rollback()
            return result

    def test_backend_validate_http_and_receipt_verification_all_mutations(self):
        engine, hooks = self.backend_harness()
        state = None
        for operation in ('OPEN', 'RENEW', 'CHANGE', 'ADJUST', 'RESTORE', 'CLOSE', 'RESTORE'):
            body = deepcopy(self.body) if state is None else self.next_body(state, operation, str(len(self.wire_calls)))
            if operation == 'CHANGE':
                body.update(change_id='change-id', direction='upgrade', version={'id': 'v2-id', 'code': 'v2'})
            if operation == 'ADJUST':
                body.update(refund_id='refund-id', periods=[])
            if operation == 'CLOSE':
                body.update(refund_id='close-id', periods=[])
            if operation == 'RESTORE':
                body.update(direction='restore')
            normalized = backend.validate_payload(body)
            state = self.execute_backend(engine, hooks, self.stage_backend(engine, hooks, normalized))
            applied, status = self.wire_calls[-2:]
            self.assertEqual(applied[1], status[1])
            self.assertEqual(status[0]['type'], 'STATUS')
            self.assertEqual(status[0]['target_type'], operation)
            self.assertEqual(status[1]['type'], operation)
            self.assertEqual(state, applied[1])
            for key, value in normalized.items():
                if key != 'tenant_id':
                    self.assertEqual(state[key], value, key)
            self.assertNotIn('data', state)
            self.assertEqual(type(state['ret']), int)

    def test_backend_parent_rollback_and_provider_restart_stable_open_receipt(self):
        engine, hooks = self.backend_harness()
        plan_id = self.stage_backend(engine, hooks, self.body)
        first = self.execute_backend(engine, hooks, plan_id, commit=False)
        self.module = load_provider()
        self.client = TestClient(self.module.app)
        replay = self.execute_backend(engine, hooks, plan_id)
        self.assertEqual(first, replay)
        self.assertIsNone(self.wire_calls[0][0]['tenant_id'])
        self.assertIsNone(self.wire_calls[1][0]['tenant_id'])
        self.assertEqual(self.wire_calls[0][0], self.wire_calls[2][0])
        self.assertEqual(self.execute_backend(engine, hooks, plan_id), first)

    def test_backend_restore_advances_generation_persists_and_rejects_rollback(self):
        engine, hooks = self.backend_harness()
        opened = self.execute_backend(engine, hooks, self.stage_backend(engine, hooks, self.body))
        close = dict(self.next_body(opened, 'CLOSE', 'close'), periods=[])
        closed = self.execute_backend(engine, hooks, self.stage_backend(engine, hooks, close))
        for operation in ('OPEN', 'RENEW', 'CHANGE', 'ADJUST', 'CLOSE', 'STATUS'):
            with self.subTest(operation=operation):
                body = dict(self.next_body(closed, operation, 'wrong-generation-' + operation), generation=2)
                self.call(body, 409)
        restore = dict(self.next_body(closed, 'RESTORE', 'restore-generation-2'),
                       generation=2, direction='restore', refund_id='refund-restore')
        restore_id = self.stage_backend(engine, hooks, restore)
        restored = self.execute_backend(engine, hooks, restore_id)
        self.assertEqual(restored['generation'], 2)
        self.assertEqual(restored['tenant_id'], opened['tenant_id'])
        self.assertEqual(restored['status'], 'active')
        self.module = load_provider()
        self.client = TestClient(self.module.app)
        self.assertEqual(self.execute_backend(engine, hooks, restore_id), restored)
        self.assertEqual(self.status(restored), restored)
        rollback = dict(restore, event_id='rollback', idempotency_key='rollback', generation=1)
        self.call(rollback, 409)
        self.call(dict(rollback, type='STATUS'), 409)
        wrong_tenant = dict(restore, event_id='wrong-tenant', idempotency_key='wrong-tenant',
                            generation=3, tenant_id='another-tenant')
        self.call(wrong_tenant, 409)
        self.assertEqual(self.status(restored), restored)

    def test_backend_rejects_old_plan_when_status_has_newer_actual_state(self):
        engine, hooks = self.backend_harness()
        open_id = self.stage_backend(engine, hooks, self.body)
        opened = self.execute_backend(engine, hooks, open_id)
        changed = self.next_body(opened, 'CHANGE', 'change')
        changed.update(version={'id': 'changed-id', 'code': 'changed'}, direction='downgrade')
        latest = self.execute_backend(engine, hooks, self.stage_backend(engine, hooks, changed))
        self.assertEqual(self.status(opened), latest)
        with self.assertRaises(HTTPException) as caught:
            self.execute_backend(engine, hooks, open_id)
        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(self.wire_calls[-2][1], opened)
        self.assertEqual(self.wire_calls[-1][1], latest)

    def test_backend_normalized_dates_and_period_shapes(self):
        body = deepcopy(self.body)
        body['starts_at'] = body['periods'][0]['starts_at'] = '2090-01-01T01:00:00+01:00'
        body['expires_at'] = body['periods'][0]['expires_at'] = '2090-02-01T01:00:00+01:00'
        normalized = backend.validate_payload(body)
        receipt = self.call(body)
        for key, value in normalized.items():
            if key != 'tenant_id':
                self.assertEqual(receipt[key], value)
        self.assertEqual(self.call(normalized), receipt)
        for field in ('quotas', 'expires_at'):
            invalid = deepcopy(self.body)
            invalid['periods'][0].pop(field)
            self.call(invalid, 400)

    def test_restart_duplicate_canonical_body_and_first_receipt(self):
        first = self.call(self.body)
        self.assertTrue(first['tenant_id'])
        self.assertEqual(first['periods'], backend.validate_payload(self.body)['periods'])
        self.assertNotIn('data', first)
        self.assertEqual(type(first['ret']), int)
        self.module = load_provider()
        self.client = TestClient(self.module.app)
        with patch.object(self.module, 'datetime') as clock:
            # Validation parses dates, but duplicate replay never creates a new receipt time.
            clock.fromisoformat.side_effect = __import__('datetime').datetime.fromisoformat
            self.assertEqual(self.call(dict(reversed(list(self.body.items())))), first)
            clock.now.assert_not_called()
        self.assertEqual(self.status(first), first)
        with sqlite3.connect(self.path) as db:
            rows = db.execute('SELECT bodyhash,canonical_body,receipt FROM lifecycle_receipts').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], rows[1])

    def test_key_body_conflict_and_replay_old_receipt_after_change(self):
        opened = self.call(self.body)
        changed = self.next_body(opened, 'CHANGE', 'change')
        changed['version'] = {'id': 'version-2', 'code': 'v2'}
        changed['periods'][0]['quotas']['monthly_quota'] = 2000
        latest = self.call(changed)
        self.assertEqual(self.call(self.body), opened)
        self.assertEqual(self.status(latest, version={'id': 'caller', 'code': 'caller'}), latest)
        self.call(dict(changed, version={'id': 'version-3', 'code': 'v3'}), 409)
        self.call(dict(changed, event_id='different-event'), 409)

    def test_all_transitions_absolute_targets_and_closed_restore(self):
        state = self.call(self.body)
        for operation in ('RENEW', 'ADJUST', 'CHANGE'):
            body = self.next_body(state, operation, operation.lower())
            body['expires_at'] = '2090-03-01T00:00:00Z'
            body['periods'][0]['expires_at'] = body['expires_at']
            body['periods'][0]['quotas']['monthly_quota'] = 7
            state = self.call(body)
            self.assertEqual(state['periods'][0]['quotas']['monthly_quota'], 7)
            self.assertEqual(state['expires_at'], '2090-03-01T00:00:00+00:00')
        close = dict(self.next_body(state, 'CLOSE', 'close'), periods=[])
        closed = self.call(close)
        self.assertEqual(closed['status'], 'closed')
        self.assertEqual(closed['periods'], [])
        self.call(self.next_body(closed, 'RENEW', 'closed-renew'), 409)
        restored = self.call(self.next_body(closed, 'RESTORE', 'restore'))
        self.assertEqual(restored['status'], 'active')
        self.assertEqual(self.call(self.next_body(restored, 'RESTORE', 'again'))['status'], 'active')

    def test_malformed_or_unconfirmed_dates_and_targets(self):
        variants = [dict(starts_at=value) for value in (
            None, '2090-01-01', '2090-02-30T00:00:00Z', '2090-01-01T00:00:00',
            '2090-01-01T00:00:00+99:00', '2091-01-01T00:00:00Z')]
        variants += [dict(expires_at=None), dict(periods=[]), dict(stage='provisional'),
                     dict(schema_version=True), dict(generation=True), dict(event_id=[]),
                     dict(idempotency_key={})]
        for fields in variants:
            with self.subTest(fields=fields):
                self.call(dict(self.body, **fields), 400)
        for field, value in [('daily_quota', True), ('monthly_quota', -1),
                             ('starts_at', '2091-01-01T00:00:00Z'), ('term_id', None)]:
            body = deepcopy(self.body)
            target = body['periods'][0]['quotas'] if field.endswith('_quota') else body['periods'][0]
            target[field] = value
            self.call(body, 400)
        self.assertFalse(Path(self.path).exists())

    def test_cross_product_token_headers_and_claim_identity(self):
        self.call(dict(self.body, product_id='other-product'), 403)
        self.call(self.body, 403, {'X-Product-Id': 'other-product'})
        self.call(self.body, 403, {'X-Enterprise-Id': 'other-enterprise'})
        self.claims['enterprise_id'] = 'other-enterprise'
        self.call(self.body, 403)
        self.claims.pop('enterprise_id')
        self.claims['scope'] = 'unrelated'
        self.call(self.body, 403)
        self.claims['scope'] = 'resource.manage'
        self.claims['active'] = False
        self.call(self.body, 401)

    def test_separate_enterprises_and_products_same_event_and_combination(self):
        first = self.call(dict(self.body, tenant_id='tenant'))
        second_body = dict(self.body, enterprise_id='second-enterprise', tenant_id='tenant')
        second = self.call(second_body)
        self.assertNotEqual(first['enterprise_id'], second['enterprise_id'])
        self.assertEqual(self.status(first), first)
        self.assertEqual(self.status(second), second)
        self.claims['product_id'] = 'second-product'
        third = self.call(dict(self.body, product_id='second-product', tenant_id='tenant'))
        self.assertEqual(self.status(third), third)
        self.call(dict(second_body, type='STATUS'), 403)

    def test_tenant_generation_and_missing_identity_fail_closed(self):
        opened = self.call(self.body)
        for fields in (dict(tenant_id='wrong'), dict(generation=2)):
            self.call(dict(self.next_body(opened, 'RENEW', 'renew'), **fields), 409)
        self.call(dict(self.body, event_id='other', idempotency_key='other'), 409)
        missing = self.next_body(opened, 'STATUS', 'status')
        missing['combination_id'] = 'unknown'
        self.call(missing, 404)

    def test_concurrent_duplicate_open_only_one_activation(self):
        def activate(_):
            return self.module.lifecycle(deepcopy(self.body), self.claims)
        with ThreadPoolExecutor(max_workers=4) as executor:
            receipts = list(executor.map(activate, range(8)))
        self.assertTrue(all(row == receipts[0] for row in receipts))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM lifecycle_state').fetchone()[0], 1)

    def test_business_endpoints_keep_behaviour_and_check_scope_product(self):
        self.claims['scope'] = 'resource.invoke'
        headers = {'Authorization': 'Bearer machine-token'}
        self.assertEqual(self.client.get('/v1/echo', headers=headers).json()['code'], 0)
        self.assertEqual(self.client.get('/v1/file', headers=headers).text, 'mock api file content\n')
        stream = self.client.get('/v1/stream', headers=headers)
        self.assertEqual(stream.text.count('data: '), 3)
        self.assertEqual(self.client.get('/v1/echo').status_code, 401)
        self.assertEqual(self.client.get('/v1/echo', headers={'Authorization': 'Bearer '}).status_code, 401)
        self.assertEqual(self.client.get('/v1/file', headers={**headers, 'X-Product-Id': 'wrong'}).status_code, 403)
        self.claims['scope'] = ''
        self.assertEqual(self.client.get('/v1/stream', headers=headers).status_code, 403)

    def test_malformed_json_introspection_and_unavailable_platform(self):
        self.assertEqual(self.client.post('/management', content='{',
            headers={'Authorization': 'Bearer machine-token'}).status_code, 400)
        self.call([], 400)
        with patch.object(self.module.httpx, 'post', side_effect=httpx.ConnectError('private transport details')):
            response = self.client.get('/v1/echo', headers={'Authorization': 'Bearer machine-token'})
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('private', response.text)
        with patch.object(self.module.httpx, 'post', return_value=httpx.Response(200, text='bad',
                request=httpx.Request('POST', 'http://platform/oauth/introspect'))):
            self.assertEqual(self.client.get('/v1/echo', headers={'Authorization': 'Bearer machine-token'}).status_code, 503)

    def test_management_and_business_scopes_are_separate_health_is_public(self):
        headers = {'Authorization': 'Bearer machine-token'}
        self.claims['scope'] = 'resource.invoke'
        self.call(self.body, 403)
        self.assertEqual(self.client.get('/v1/echo', headers=headers).status_code, 200)
        self.claims['scope'] = 'resource.manage'
        self.call(self.body)
        for endpoint in ('echo', 'file', 'stream', 'slow', 'error'):
            self.assertEqual(self.client.get('/v1/' + endpoint, headers=headers).status_code, 403)
        self.claims['active'] = False
        self.assertEqual(self.client.get('/health').status_code, 200)


if __name__ == '__main__':
    unittest.main()
