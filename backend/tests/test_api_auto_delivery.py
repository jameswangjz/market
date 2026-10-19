"""SQLite HTTP coverage for production API automatic fulfillment wiring."""
import hashlib
import copy
import importlib
import json
import os
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch
from cryptography.fernet import Fernet
from sqlalchemy import event

import test_message_business_events as fixtures


class ApiAutoDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.BusinessRoutesHTTPTests.setUpClass.__func__(cls)

    tearDownClass = classmethod(fixtures.BusinessRoutesHTTPTests.tearDownClass.__func__)
    tearDown = fixtures.BusinessRoutesHTTPTests.tearDown
    request = fixtures.BusinessRoutesHTTPTests.request
    pay = fixtures.BusinessRoutesHTTPTests.pay
    task_id = fixtures.BusinessRoutesHTTPTests.task_id

    def setUp(self):
        fixtures.BusinessRoutesHTTPTests.setUp(self)
        m = self.m
        raw = m.engine.raw_connection()
        raw.driver_connection.rollback()
        raw.driver_connection.isolation_level = None
        raw.close()
        event.listen(m.engine, 'begin', lambda connection: connection.exec_driver_sql('BEGIN')
            if not connection.connection.driver_connection.in_transaction else None)
        api_module = importlib.import_module(m.__package__ + '.api_auto_delivery')
        self.api = api_module.install(vars(m))
        self.encryption_key = Fernet.generate_key().decode()
        encryption_env = patch.dict(os.environ, {'API_CREDENTIAL_ENCRYPTION_KEY': self.encryption_key})
        encryption_env.start()
        self.addCleanup(encryption_env.stop)
        with m.SessionLocal() as db:
            db.add(m.User(id='delivery-worker', name='Worker', email='jfsh@market.local',
                password_hash='unused', platform_role='delivery_monitor', verified_status='verified',
                activation_status='active'))
            product = db.get(m.Product, 'product')
            product.product_type, product.delivery_method = 'api', 'api'
            version = db.get(m.ProductReleaseVersion, 'version')
            version.rate_limit_per_minute, version.daily_quota, version.monthly_quota = 30, 12, 300
            route = m.ApiGatewayRoute(id='route', product_id='product', route_key='route-v1',
                upstream_url='https://upstream.example.invalid', version='v1', status='active',
                rate_limit_per_minute=30, daily_quota=12, monthly_quota=300,
                health_path='/health', health_method='GET')
            db.add(route)
            db.flush()
            config = m.version_gateway['Config'](id='config', product_id='product',
                version_id='version', route_id='route', upstream_url=route.upstream_url,
                route_key=route.route_key, version_code='v1', auth_mode='api_key',
                upstream_auth_mode=route.upstream_auth_mode, upstream_scope=route.upstream_scope,
                timeout_ms=route.timeout_ms, strip_prefix=route.strip_prefix,
                health_method=route.health_method, health_path=route.health_path,
                rate_limit_per_minute=30, daily_quota=12, monthly_quota=300,
                status='verified', healthy=True, verified_at=m.now(), health_status_code=200,
                evidence_hash='fixture')
            db.add(config)
            self.expected_route = m.apisix_route_payload(route, product, db)
            self.good_expected_route = self.expected_route
            db.commit()
        self.health = Mock(status_code=204, extensions={'validated_upstream_ip': None})
        gateway_url = patch.dict(os.environ, {'API_GATEWAY_PUBLIC_URL': 'https://gateway.example.invalid'})
        gateway_url.start()
        self.addCleanup(gateway_url.stop)
        self.redis = Mock()
        self.redis_values = {}
        self.redis.hgetall.side_effect = lambda key: self.redis_values.get(key, {})
        self.redis.hset.side_effect = lambda key, mapping: self.redis_values.setdefault(key, {}).update(
            {str(field): str(value) for field, value in mapping.items()})
        self.redis.delete.side_effect = lambda key: int(self.redis_values.pop(key, None) is not None)
        self.redis.info.side_effect = lambda section: (
            {'cluster_enabled': 0} if section == 'cluster' else {'role': 'master'})
        self.redis.dbsize.return_value = 0
        self.redis.scan.side_effect = lambda cursor, match, count: (0, [])
        self.redis.eval.side_effect = self.redis_eval
        self.apisix_consumers = {}
        self.route_payload = None
        self.patches = [
            patch.dict(m.version_gateway, {'version_ready': lambda *args: True}),
            patch.object(m, 'apisix_enabled', return_value=True),
            patch.object(m, 'validated_http_request', return_value=self.health),
            patch.object(m, 'apisix_policy_redis', return_value=self.redis),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.admin = patch.object(m, 'apisix_admin_request', side_effect=self.admin_request)
        self.admin.start()
        self.addCleanup(self.admin.stop)

    def admin_request(self, method, path, payload=None):
        if method == 'GET' and path == '/routes/route-v1':
            return self.expected_route
        if path.startswith('/consumers/'):
            name = path.rsplit('/', 1)[-1]
            if method == 'PUT':
                if not isinstance(payload, dict) or payload.get('username') != name:
                    raise AssertionError((method, path, payload))
                self.apisix_consumers[name] = copy.deepcopy(payload)
                return {'node': {'value': self.apisix_consumers[name]}}
            if method == 'GET':
                return {'node': {'value': self.apisix_consumers[name]}}
            if method == 'DELETE':
                self.apisix_consumers.pop(name, None)
                return {'node': {'value': {'username': name}}}
        raise AssertionError((method, path))

    def redis_eval(self, _script, numkeys, *args):
        if len(args) == numkeys:
            return 1  # Charge-ledger baseline initialization has no ARGV payload.
        data = json.loads(args[numkeys])
        self.redis_values.setdefault(args[0], {}).update(
            {str(field): str(value) for field, value in data.items()})
        return [1]

    def create(self, months=1):
        with patch.dict(self.m.storefront, {'versions_for': lambda db, product: [{'id': 'version'}]}):
            order = self.request('POST', '/api/orders', json={'product_id': 'product',
                'product_version_id': 'version', 'buyer_enterprise_id': 'buyer_tenant',
                'subscription_months': months})
        self.pay(order['id'])
        return order['id']

    def step(self, oid):
        with self.m.SessionLocal() as db:
            task = db.get(self.m.DeliveryTask, self.task_id(oid))
            if task.next_retry_at:
                task.next_retry_at = self.m.now() - timedelta(seconds=1)
                db.commit()
        return self.m.online_delivery['process_once']()

    def assert_fulfilled_with_initial_key(self, oid):
        self.step(oid)
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            task = db.get(self.m.DeliveryTask, self.task_id(oid))
            self.assertEqual((order.main_status, order.delivery_status, task.status),
                ('fulfilling', 'accepted', 'completed'), task.last_error)
            self.assertIn('API 路由、上游健康与订阅策略已核验', task.note)
            self.assertIn('企业共享凭据状态已保留', task.note)
            self.assertIn('买方企业管理员可在订单中查看和管理凭据', task.note)
            self.assertNotIn('共享 API 凭据需由买方按需生成', task.note)
            self.assertEqual(db.query(self.m.SubscriptionTerm).filter_by(order_id=oid).count(), 1)
            credential = db.query(self.m.ApiCredential).filter_by(enterprise_id='buyer_tenant').one()
            self.assertEqual(credential.status, 'active')
            self.assertTrue(credential.secret_encrypted)
            self.assertNotEqual(credential.secret_encrypted, credential.key_hash)
            credential_id = credential.id
        access = self.request('GET', f'/api/orders/{oid}/delivery-access')
        self.assertTrue(access['ready'], access['reason'])
        self.assertIn('credentials_path', access['api'])
        response = self.client.get(f'/api/orders/{oid}/api-credentials/{credential_id}/download',
            headers={'Authorization': 'Bearer ' + self.tokens['buyer']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['Cache-Control'], 'no-store, no-cache, must-revalidate')
        self.assertEqual(response.json()['credential_id'], credential_id)
        self.assertTrue(response.json()['api_key'].startswith('mk_'))

    def test_api_completes_with_encrypted_initial_key_and_buyer_download(self):
        self.assert_fulfilled_with_initial_key(self.create())

    def test_model_api_completes_with_encrypted_initial_key(self):
        with self.m.SessionLocal() as db:
            product = db.get(self.m.Product, 'product')
            product.product_type, product.delivery_method = 'model', 'model_api'
            db.commit()
        self.assert_fulfilled_with_initial_key(self.create())

    def test_frozen_method_survives_later_product_delivery_edit(self):
        oid = self.create()
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, 'product').delivery_method = 'file'
            db.commit()
        self.assert_fulfilled_with_initial_key(oid)

    def test_route_match_accepts_apisix_defaults_but_rejects_disable_or_extra_plugin(self):
        expected = self.expected_route
        live = copy.deepcopy(expected)
        live['status'] = 1
        live['id'] = 'admin-generated-id'
        live['methods'].reverse()
        live['upstream']['retries'] = 1
        live['labels']['admin_revision'] = 'generated'
        self.assertTrue(self.api['route_matches'](
            {'node': {'value': live}}, expected))
        disabled = copy.deepcopy(live)
        disabled['status'] = 0
        self.assertFalse(self.api['route_matches'](disabled, expected))
        weakened = copy.deepcopy(live)
        weakened['plugins']['limit-count']['allow_degradation'] = True
        self.assertFalse(self.api['route_matches'](weakened, expected))
        unknown = copy.deepcopy(live)
        unknown['plugins']['request-validation'] = {}
        self.assertFalse(self.api['route_matches'](unknown, expected))
        extra_node = copy.deepcopy(live)
        extra_node['upstream']['nodes']['attacker.example:443'] = 1
        self.assertFalse(self.api['route_matches'](extra_node, expected))
        proxy_override = copy.deepcopy(live)
        proxy_override['plugins']['proxy-rewrite']['uri'] = '/attacker'
        self.assertFalse(self.api['route_matches'](proxy_override, expected))
        disabled_plugin = copy.deepcopy(live)
        disabled_plugin['plugins']['limit-count']['_meta'] = {'disable': True}
        self.assertFalse(self.api['route_matches'](disabled_plugin, expected))

    def test_health_and_route_mismatch_do_not_grant_term(self):
        for bad_health, bad_route in ((True, False), (False, True)):
            with self.subTest(health=bad_health, route=bad_route):
                oid = self.create()
                if bad_health:
                    self.health.status_code = 503
                if bad_route:
                    self.expected_route = {'uri': '/wrong'}
                self.step(oid)
                with self.m.SessionLocal() as db:
                    self.assertEqual(db.query(self.m.SubscriptionTerm).filter_by(order_id=oid).count(), 0)
                    task = db.get(self.m.DeliveryTask, self.task_id(oid))
                    self.assertEqual(task.retry_count, 1)
                    self.assertNotIn('PRIVATE', task.last_error)
                self.health.status_code = 204
                self.expected_route = self.good_expected_route
                self.admin.side_effect = self.admin_request

    def test_apisix_disabled_fails_closed(self):
        oid = self.create()
        with patch.object(self.m, 'apisix_enabled', return_value=False):
            self.step(oid)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.SubscriptionTerm).filter_by(order_id=oid).count(), 0)

    def test_security_unpublished_product_cannot_receive_term(self):
        oid = self.create()
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, 'product').status = 'security_unpublished'
            db.commit()
        self.step(oid)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.SubscriptionTerm).filter_by(order_id=oid).count(), 0)

    def test_existing_consumer_mismatch_and_redis_sync_failure_do_not_grant(self):
        oid = self.create()
        with self.m.SessionLocal() as db:
            credential = self.m.ApiCredential(route_id='route', enterprise_id='buyer_tenant',
                order_id='', key_prefix='market-secret',
                key_hash=hashlib.sha256(b'market-secret-key').hexdigest(),
                apisix_consumer_name='consumer-existing', status='active', daily_quota=12,
                monthly_quota=300, rate_limit_per_minute=30)
            db.add(credential)
            db.commit()
            payload = {'username': 'consumer-existing', 'plugins': {
                'key-auth': {'key': 'wrong-consumer-key'},
                'market-gateway-quota': {'route_key': 'route-v1'}}}
            self.apisix_consumers['consumer-existing'] = payload
        self.step(oid)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.SubscriptionTerm).count(), 0)
            db.query(self.m.DeliveryTask).filter_by(order_id=oid).one().status = 'retrying'
            db.commit()

        self.apisix_consumers['consumer-existing']['plugins']['key-auth']['key'] = 'market-secret-key'
        self.redis.eval.side_effect = RuntimeError('private Redis endpoint detail')
        self.step(oid)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.SubscriptionTerm).count(), 0)
            task = db.query(self.m.DeliveryTask).filter_by(order_id=oid).one()
            self.assertNotIn('private Redis endpoint detail', task.last_error)
        self.assertEqual(self.redis.delete.call_count, 1)

    def test_active_shared_credential_without_consumer_marker_fails_closed(self):
        oid = self.create()
        with self.m.SessionLocal() as db:
            db.add(self.m.ApiCredential(route_id='route', enterprise_id='buyer_tenant',
                order_id='', key_prefix='market-secret',
                key_hash=hashlib.sha256(b'market-secret-key').hexdigest(),
                apisix_consumer_name='', status='active'))
            db.commit()
        self.step(oid)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.SubscriptionTerm).filter_by(order_id=oid).count(), 0)

    def test_existing_consumer_bound_to_another_route_fails_closed(self):
        oid = self.create()
        with self.m.SessionLocal() as db:
            db.add(self.m.ApiCredential(route_id='route', enterprise_id='buyer_tenant',
                order_id='', key_prefix='market-secret',
                key_hash=hashlib.sha256(b'market-secret-key').hexdigest(),
                apisix_consumer_name='consumer-existing', status='active'))
            db.commit()
        self.apisix_consumers['consumer-existing'] = {'username': 'consumer-existing', 'plugins': {
            'key-auth': {'key': 'market-secret-key'},
            'market-gateway-quota': {'route_key': 'different-route'}}}
        self.consumer_route_key = 'different-route'
        self.step(oid)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.SubscriptionTerm).filter_by(order_id=oid).count(), 0)

    def add_shared_credential(self):
        with self.m.SessionLocal() as db:
            db.add(self.m.ApiCredential(route_id='route', enterprise_id='buyer_tenant',
                order_id='', key_prefix='market-secret',
                key_hash=hashlib.sha256(b'market-secret-key').hexdigest(),
                apisix_consumer_name='consumer-existing', status='active', daily_quota=12,
                monthly_quota=300, rate_limit_per_minute=30))
            db.commit()
        self.apisix_consumers['consumer-existing'] = {'username': 'consumer-existing', 'plugins': {
            'key-auth': {'key': 'market-secret-key'},
            'market-gateway-quota': {'route_key': 'route-v1'}}}
        baseline = {'mode': 'subscription', 'quota_scope': 'prior-scope', 'status': 'active',
            'subscription_periods': '[]', 'daily_quota': '12', 'monthly_quota': '300', 'total_quota': '0'}
        self.redis_values['market:apisix:credential:consumer-existing'] = baseline.copy()
        return baseline

    def assert_rollback_restores_external_policy(self, oid, baseline, *, fail_audit=False):
        if fail_audit:
            original = self.m.audit

            def audit(*args, **kwargs):
                if len(args) > 2 and args[2] == 'delivery_succeeded':
                    raise RuntimeError('injected audit failure')
                return original(*args, **kwargs)

            audit_patch = patch.object(self.m, 'audit', side_effect=audit)
        else:
            Session = self.m.Session

            class CommitFailureSession(Session):
                def commit(self):
                    raise RuntimeError('injected commit failure')

            factory = self.m.sessionmaker(bind=self.m.engine, class_=CommitFailureSession,
                autoflush=False)
            audit_patch = patch.object(self.m, 'SessionLocal', factory)
        with audit_patch, self.assertRaisesRegex(RuntimeError, 'injected .* failure'):
            self.m.online_delivery['process_once']()
        self.assertEqual(self.redis_values['market:apisix:credential:consumer-existing'], baseline)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.SubscriptionTerm).filter_by(order_id=oid).count(), 0)
            self.assertEqual(db.get(self.m.DeliveryTask, self.task_id(oid)).status, 'in_delivery')

    def test_later_audit_failure_rolls_back_term_and_redis_policy(self):
        oid = self.create()
        baseline = self.add_shared_credential()
        self.assert_rollback_restores_external_policy(oid, baseline, fail_audit=True)

    def test_database_commit_failure_rolls_back_term_and_redis_policy(self):
        oid = self.create()
        baseline = self.add_shared_credential()
        self.assert_rollback_restores_external_policy(oid, baseline)

    def test_failure_retries_three_times_and_force_failure_never_grants(self):
        oid = self.create()
        self.health.status_code = 503
        for expected in (1, 2, 3):
            self.step(oid)
            with self.m.SessionLocal() as db:
                task = db.get(self.m.DeliveryTask, self.task_id(oid))
                self.assertEqual(task.retry_count, expected)
                self.assertEqual(task.status, 'exception' if expected == 3 else 'retrying')
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            task = db.get(self.m.DeliveryTask, self.task_id(oid))
            self.assertEqual(db.query(self.m.SubscriptionTerm).count(), 0)
            task.status, task.retry_count, task.max_retries = 'retrying', 0, 3
            order.delivery_status = 'in_delivery'
            db.commit()
        self.request('POST', f'/api/delivery-tasks/{self.task_id(oid)}/process', actor='platform',
            json={'success': False, 'error': 'untrusted external detail'})
        with self.m.SessionLocal() as db:
            task = db.get(self.m.DeliveryTask, self.task_id(oid))
            self.assertEqual(task.retry_count, 1)
            self.assertNotIn('untrusted external detail', task.last_error)

    def test_renewal_does_not_reset_existing_usage_or_revoked_key(self):
        first = self.create()
        self.step(first)
        with self.m.SessionLocal() as db:
            combo = db.query(self.m.SubscriptionCombination).one()
            credential = db.query(self.m.ApiCredential).one()
            consumer_name = credential.apisix_consumer_name
            self.redis_values['market:apisix:credential:' + consumer_name] = {'mode': 'subscription'}
            combo_id = combo.id
        renewal = self.create(2)
        self.step(renewal)
        live_policy = self.redis_values['market:apisix:credential:' + consumer_name]
        self.assertIn('subscription_periods', live_policy)
        self.assertNotEqual(live_policy, {'mode': 'subscription'})
        with self.m.SessionLocal() as db:
            credential = db.query(self.m.ApiCredential).one()
            self.assertEqual((credential.daily_quota, credential.monthly_quota), (12, 300))
            self.assertEqual(db.query(self.m.SubscriptionTerm).filter_by(order_id=renewal).count(), 1)
            self.assertEqual(db.query(self.m.SubscriptionCombination).one().id, combo_id)
        with self.m.SessionLocal() as db:
            db.query(self.m.ApiCredential).one().status = 'revoked'
            db.commit()
        next_renewal = self.create(1)
        self.step(next_renewal)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.ApiCredential).one().status, 'revoked')


if __name__ == '__main__':
    unittest.main()
