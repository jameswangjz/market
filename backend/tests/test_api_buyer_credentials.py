"""Focused coverage for buyer credential issuance, download, and rollback."""
import hashlib
import importlib.util
import os
from pathlib import Path
import secrets
import sys
import unittest
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.params import Depends as DependsMarker
from sqlalchemy import DateTime, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


APP_DIR = Path(__file__).resolve().parents[1] / 'app'
PACKAGE_NAME = 'buyer_credential_test_' + secrets.token_hex(6)
package = ModuleType(PACKAGE_NAME)
package.__path__ = [str(APP_DIR)]
sys.modules[PACKAGE_NAME] = package


def load_app_module(name):
    spec = importlib.util.spec_from_file_location(PACKAGE_NAME + '.' + name,
        APP_DIR / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


install_auto_delivery = load_app_module('api_auto_delivery').install
install = load_app_module('api_buyer_credentials').install


class TestBase(DeclarativeBase):
    pass


class TestCredential(TestBase):
    __tablename__ = 'test_api_credentials'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    route_id: Mapped[str] = mapped_column(String)
    enterprise_id: Mapped[str] = mapped_column(String)
    order_id: Mapped[str] = mapped_column(String)
    product_version_id: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    key_prefix: Mapped[str] = mapped_column(String)
    key_hash: Mapped[str] = mapped_column(String)
    apisix_consumer_name: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_quota: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    secret_encrypted: Mapped[str | None] = mapped_column(String, nullable=True)


class BuyerCredentialTests(unittest.TestCase):
    def setUp(self):
        self.key = Fernet.generate_key().decode()
        self.app = Mock()
        self.rows = []
        self.db = Mock()
        self.db.scalars.return_value.all.side_effect = lambda: list(self.rows)
        self.user = SimpleNamespace(id='buyer-user', email='buyer@example.test', phone='',
                                    verified_status='verified', platform_role='')
        self.order = SimpleNamespace(id='order-1', product_id='product-1',
            buyer_enterprise_id='enterprise-1', product_name='Model', snapshot_version=1)
        self.route = SimpleNamespace(id='route-1', version='v1', route_key='model-v1',
            rate_limit_per_minute=60, daily_quota=100, monthly_quota=1000)
        self.track = Mock()
        self.cleanup = Mock()
        self.redis = Mock()
        self.redis_values = {}
        self.redis.hgetall.side_effect = lambda key: self.redis_values.get(key, {})
        self.redis.hset.side_effect = lambda key, mapping: self.redis_values.setdefault(key, {}).update(mapping)
        self.raw_key = ''
        self.metadata = {'mode': 'subscription', 'status': 'active'}
        self.sync = Mock(side_effect=self.sync_credential)
        self.admin = Mock(side_effect=self.admin_request)
        self.audit = Mock()
        self.ns = {
            'ApiCredential': TestCredential,
            'Order': object,
            'app': self.app,
            'current_user': lambda: self.user,
            'db_session': lambda: self.db,
            'trading_policy': {'require_buyer': Mock()},
            'subscription_terms': {'lock_combination': Mock(return_value=None),
                'current_entitlement': Mock(return_value={'version_code': 'v1',
                    'delivery_method': 'api'})},
            'subscription_gateway': {
                'policy': Mock(return_value={'rate_limit_per_minute': 60,
                    'daily_quota': 100, 'monthly_quota': 1000}),
                'metadata': Mock(return_value=self.metadata)},
            'sync_apisix_consumer': self.sync,
            'apisix_admin_request': self.admin,
            'apisix_policy_redis': lambda: self.redis,
            '_track_api_consumer_compensation': self.track,
            '_cleanup_api_consumer_compensation': self.cleanup,
            'audit': self.audit,
            'api_order_context': Mock(return_value=(self.order, SimpleNamespace(), self.route,
                                                       'enterprise-1')),
            'require_enterprise_admin': Mock(return_value=SimpleNamespace(role='enterprise_admin')),
            'now': Mock(return_value=datetime.now(timezone.utc)),
        }
        self.api = install(self.ns)
        env = patch.dict(os.environ, API_CREDENTIAL_ENCRYPTION_KEY=self.key)
        env.start()
        self.addCleanup(env.stop)

    def sync_credential(self, credential, raw_key, route, db):
        self.raw_key = raw_key
        self.redis.hset('market:apisix:credential:' + credential.apisix_consumer_name,
                        mapping=self.metadata)

    def admin_request(self, method, path, payload=None):
        if method == 'DELETE':
            return {}
        if method == 'GET' and path.startswith('/consumers/'):
            return {'node': {'value': {'username': path.rsplit('/', 1)[-1],
                'plugins': {'key-auth': {'key': self.raw_key},
                            'market-gateway-quota': {'route_key': 'model-v1'}}}}}
        raise AssertionError((method, path))

    def create_credential(self, **values):
        defaults = {'id': 'credential-1', 'route_id': 'route-1',
            'enterprise_id': 'enterprise-1', 'order_id': '', 'product_version_id': '',
            'name': 'Model key', 'key_prefix': 'mk_test', 'key_hash': '0' * 64,
            'apisix_consumer_name': 'market-consumer-legacy', 'status': 'active',
            'rate_limit_per_minute': 60, 'daily_quota': 100, 'monthly_quota': 1000,
            'total_quota': 0, 'expires_at': None, 'created_by': 'buyer',
            'secret_encrypted': None}
        defaults.update(values)
        return TestCredential(**defaults)

    def real_issuance_session(self):
        engine = create_engine('sqlite://')
        TestBase.metadata.create_all(engine)
        db = Session(engine)
        self.addCleanup(engine.dispose)
        self.addCleanup(db.close)
        redis = Mock()
        values = {}
        redis.hgetall.side_effect = lambda key: values.get(key, {})
        redis.hset.side_effect = lambda key, mapping: values.setdefault(key, {}).update(mapping)
        redis.delete.side_effect = lambda key: values.pop(key, None)
        self.redis = redis
        # Use fresh external-service mocks here; other tests in this class mutate
        # the setup mocks' side effects to exercise failed verification paths.
        admin = Mock(side_effect=self.admin_request)
        self.admin = admin
        adapter_ns = {'ApiCredential': TestCredential, 'ApiGatewayRoute': object,
            'apisix_policy_redis': lambda: redis, 'apisix_admin_request': admin}
        install_auto_delivery(adapter_ns)
        self.ns.update(adapter_ns)
        self.ns['_track_api_consumer_compensation'] = adapter_ns['_track_api_consumer_compensation']
        self.ns['_cleanup_api_consumer_compensation'] = adapter_ns['_cleanup_api_consumer_compensation']
        self.ns['sync_apisix_consumer'] = self.sync
        self.ns['audit'] = self.audit
        return db, redis, values

    def test_initial_credential_is_encrypted_and_verified(self):
        credential = self.api['ensure_initial'](self.db, self.order, self.route, self.user)
        self.assertEqual(credential.status, 'active')
        self.assertEqual(credential.id, credential.apisix_consumer_name.removeprefix('market-consumer-'))
        self.assertTrue(credential.id)
        self.assertNotEqual(credential.secret_encrypted, credential.key_hash)
        self.assertEqual(self.api['cipher']().decrypt(
            credential.secret_encrypted.encode()).decode()[:3], 'mk_')
        self.track.assert_called_once_with(self.db, credential.apisix_consumer_name)
        self.sync.assert_called_once()
        self.admin.assert_called_once_with('GET', '/consumers/' + credential.apisix_consumer_name)

        self.rows[:] = [credential]
        self.assertIs(self.api['ensure_initial'](self.db, self.order, self.route, self.user), credential)
        self.sync.assert_called_once()

    def test_failed_consumer_verification_triggers_eager_cleanup(self):
        self.admin.side_effect = lambda *args, **kwargs: {
            'node': {'value': {'username': 'wrong-consumer', 'plugins': {
                'key-auth': {'key': self.raw_key},
                'market-gateway-quota': {'route_key': 'model-v1'}}}}}
        with self.assertRaises(HTTPException) as raised:
            self.api['ensure_initial'](self.db, self.order, self.route, self.user)
        self.assertEqual(raised.exception.status_code, 409)
        self.cleanup.assert_called_once_with(self.db, 'market-consumer-' + self.db.add.call_args.args[0].id)

    def test_existing_history_is_never_rotated_or_reactivated(self):
        revoked = self.create_credential(status='revoked')
        self.rows[:] = [revoked]
        self.assertIsNone(self.api['ensure_initial'](self.db, self.order, self.route, self.user))
        self.assertEqual(revoked.status, 'revoked')
        self.sync.assert_not_called()
        legacy_active = self.create_credential(id='legacy', secret_encrypted=None)
        self.rows[:] = [revoked, legacy_active]
        self.assertIs(self.api['ensure_initial'](self.db, self.order, self.route, self.user), legacy_active)
        self.sync.assert_not_called()

    def test_dedicated_encryption_key_is_required(self):
        with patch.dict(os.environ, {'API_CREDENTIAL_ENCRYPTION_KEY': '',
                                     'JWT_SECRET': 'present',
                                     'OIDC_CREDENTIAL_ENCRYPTION_KEY': self.key}):
            with self.assertRaises(HTTPException) as raised:
                self.api['encrypt']('mk_secret')
        self.assertEqual(raised.exception.status_code, 503)

    def test_download_commits_audit_and_returns_usable_no_store_config(self):
        secret = 'mk_test-secret-value'
        credential = self.create_credential(key_hash=hashlib.sha256(secret.encode()).hexdigest(),
            secret_encrypted=self.api['cipher']().encrypt(secret.encode()).decode())
        self.db.scalar.return_value = credential
        self.db.get.return_value = self.order
        with patch.dict(os.environ, API_GATEWAY_PUBLIC_URL='https://gateway.example.test'):
            response = self.api['download']('order-1', 'credential-1', self.user, self.db)
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response.headers['cache-control'])
        self.assertIn('attachment;', response.headers['content-disposition'])
        self.assertEqual(self.db.commit.call_count, 1)
        self.assertEqual(self.audit.call_count, 1)
        self.assertNotIn(secret, repr(self.audit.call_args))
        self.ns['trading_policy']['require_buyer'].assert_called_once_with(
            self.db, self.user, 'enterprise-1')
        self.assertEqual(response.body and __import__('json').loads(response.body), {
            'credential_id': 'credential-1', 'key_id': 'credential-1',
            'name': 'Model key', 'api_key': secret,
            'route_key': 'model-v1', 'gateway_base_path': '/gateway/model-v1',
            'gateway_url': 'https://gateway.example.test/gateway/model-v1',
            'rate_limit_per_minute': 60, 'daily_quota': 100,
            'monthly_quota': 1000, 'total_quota': 0,
            'quota': {'rate_limit_per_minute': 60, 'daily_quota': 100,
                      'monthly_quota': 1000, 'total_quota': 0}})

    def test_download_rejects_platform_role_and_hash_mismatch(self):
        self.user.platform_role = 'super_admin'
        with self.assertRaises(HTTPException) as raised:
            self.api['download']('order-1', 'credential-1', self.user, self.db)
        self.assertEqual(raised.exception.status_code, 403)
        self.ns['trading_policy']['require_buyer'].assert_not_called()

        self.user.platform_role = ''
        self.db.scalar.return_value = self.create_credential(
            secret_encrypted=self.api['cipher']().encrypt(b'mk_wrong').decode())
        self.db.get.return_value = self.order
        with self.assertRaises(HTTPException) as raised:
            self.api['download']('order-1', 'credential-1', self.user, self.db)
        self.assertEqual(raised.exception.status_code, 503)

    def test_hash_only_legacy_secret_returns_conflict_without_audit(self):
        credential = self.create_credential(secret_encrypted=None)
        self.db.scalar.return_value = credential
        self.db.get.return_value = self.order
        with self.assertRaises(HTTPException) as raised:
            self.api['download']('order-1', credential.id, self.user, self.db)
        self.assertEqual(raised.exception.status_code, 409)
        self.audit.assert_not_called()
        self.db.commit.assert_not_called()

    def test_route_registers_authentication_and_database_dependencies(self):
        import inspect
        endpoint = self.app.add_api_route.call_args.args[1]
        parameters = inspect.signature(endpoint).parameters.values()
        self.assertEqual(sum(isinstance(value.default, DependsMarker)
                             for value in parameters), 2)

    def test_root_rollback_after_nested_commit_removes_consumer_and_policy(self):
        engine = create_engine('sqlite://')
        db = Session(engine)
        self.addCleanup(db.close)
        redis = Mock()
        admin = Mock()
        ns = {'ApiCredential': object, 'ApiGatewayRoute': object,
              'apisix_policy_redis': lambda: redis, 'apisix_admin_request': admin}
        install_auto_delivery(ns)
        db.begin()
        ns['_track_api_consumer_compensation'](db, 'market-consumer-rollback')
        nested = db.begin_nested()
        nested.commit()
        admin.assert_not_called()
        db.rollback()
        admin.assert_called_once_with('DELETE', '/consumers/market-consumer-rollback')
        redis.delete.assert_called_once_with('market:apisix:credential:market-consumer-rollback')

    def test_real_issuance_root_commit_keeps_consumer_and_policy(self):
        db, redis, values = self.real_issuance_session()
        with patch.dict(os.environ, API_CREDENTIAL_ENCRYPTION_KEY=self.key):
            credential = self.api['ensure_initial'](db, self.order, self.route, self.user)
            db.commit()
        self.assertEqual(db.query(TestCredential).count(), 1)
        self.assertIn('market:apisix:credential:' + credential.apisix_consumer_name, values)
        self.assertFalse(any(call.args[0] == 'DELETE' for call in self.admin.call_args_list))
        redis.delete.assert_not_called()

    def test_real_audit_failure_rolls_back_row_and_cleans_consumer(self):
        db, redis, values = self.real_issuance_session()
        self.audit.side_effect = RuntimeError('injected audit failure')
        with patch.dict(os.environ, API_CREDENTIAL_ENCRYPTION_KEY=self.key):
            with self.assertRaisesRegex(RuntimeError, 'injected audit failure'):
                self.api['ensure_initial'](db, self.order, self.route, self.user)
        db.rollback()
        self.assertEqual(db.query(TestCredential).count(), 0)
        self.assertEqual(self.admin.call_args_list[-1].args[0], 'DELETE')
        self.assertTrue(self.admin.call_args_list[-1].args[1].startswith('/consumers/market-consumer-'))
        self.assertEqual(values, {})
        redis.delete.assert_called_once()

    def test_real_commit_failure_rolls_back_row_and_cleans_consumer(self):
        from sqlalchemy import event
        db, redis, values = self.real_issuance_session()
        with patch.dict(os.environ, API_CREDENTIAL_ENCRYPTION_KEY=self.key):
            credential = self.api['ensure_initial'](db, self.order, self.route, self.user)

        def fail_commit(session):
            raise RuntimeError('injected commit failure')

        event.listen(db, 'before_commit', fail_commit)
        with self.assertRaisesRegex(RuntimeError, 'injected commit failure'):
            db.commit()
        event.remove(db, 'before_commit', fail_commit)
        db.rollback()
        self.assertEqual(db.query(TestCredential).count(), 0)
        self.assertEqual(self.admin.call_args_list[-1].args,
            ('DELETE', '/consumers/' + credential.apisix_consumer_name))
        self.assertEqual(values, {})
        redis.delete.assert_called_once()

    def test_root_commit_retries_failed_eager_cleanup(self):
        engine = create_engine('sqlite://')
        db = Session(engine)
        self.addCleanup(db.close)
        redis = Mock()
        redis.delete.side_effect = RuntimeError('redis unavailable')
        admin = Mock(side_effect=RuntimeError('apisix unavailable'))
        ns = {'ApiCredential': object, 'ApiGatewayRoute': object,
              'apisix_policy_redis': lambda: redis, 'apisix_admin_request': admin}
        install_auto_delivery(ns)
        db.begin()
        ns['_track_api_consumer_compensation'](db, 'market-consumer-retry')
        with self.assertRaises(RuntimeError):
            ns['_cleanup_api_consumer_compensation'](db, 'market-consumer-retry')
        with self.assertRaises(RuntimeError):
            db.commit()
        self.assertEqual(redis.delete.call_count, 2)
        self.assertEqual(admin.call_count, 2)

    def test_delete_consumer_treats_only_http_404_as_already_removed(self):
        engine = create_engine('sqlite://')
        db = Session(engine)
        self.addCleanup(db.close)
        redis = Mock()
        admin = Mock()
        ns = {'ApiCredential': object, 'ApiGatewayRoute': object,
              'apisix_policy_redis': lambda: redis, 'apisix_admin_request': admin}
        install_auto_delivery(ns)
        db.begin()
        ns['_track_api_consumer_compensation'](db, 'market-consumer-404')
        response = SimpleNamespace(status_code=404)
        cause = RuntimeError('not found')
        cause.response = response
        missing = RuntimeError('APISIX Admin API call failed')
        missing.__cause__ = cause
        admin.side_effect = missing
        ns['_cleanup_api_consumer_compensation'](db, 'market-consumer-404')
        db.commit()
        admin.assert_called_once_with('DELETE', '/consumers/market-consumer-404')

        db.begin()
        ns['_track_api_consumer_compensation'](db, 'market-consumer-10404')
        admin.side_effect = RuntimeError('timeout connecting to 10.0.0.1:10404')
        with self.assertRaises(RuntimeError):
            ns['_cleanup_api_consumer_compensation'](db, 'market-consumer-10404')
        admin.side_effect = None
        admin.return_value = {}
        db.rollback()
        self.assertEqual(admin.call_count, 3)


if __name__ == '__main__':
    unittest.main()


def tearDownModule():
    for module_name in list(sys.modules):
        if module_name == PACKAGE_NAME or module_name.startswith(PACKAGE_NAME + '.'):
            sys.modules.pop(module_name, None)
