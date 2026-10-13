"""Isolated persistence and transition coverage for scheduled health checks."""
import importlib
from pathlib import Path
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_message_business_events import load_isolated_main


class ApplicationHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_isolated_main()

    @classmethod
    def tearDownClass(cls):
        cls.m.engine.dispose()
        package = cls.m.__package__
        for name in list(sys.modules):
            if name == package or name.startswith(package + '.'):
                del sys.modules[name]

    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        event.listen(self.engine, 'connect', lambda conn, _: conn.execute('PRAGMA foreign_keys=ON'))
        self.m.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.notify = Mock()
        self.m.notify_business_event = self.notify
        with self.Session() as db:
            db.add(self.m.Enterprise(id='provider', name='Provider', credit_code='test'))
            product = self.m.Product(id='p', enterprise_id='provider', name='API', product_type='api',
                delivery_method='api', status='published')
            db.add(product)
            db.flush()
            version = self.m.ProductReleaseVersion(id='v', product_id='p', version_code='v1',
                price=10, cost=1, status='active')
            db.add(version)
            db.flush()
            route = self.m.ApiGatewayRoute(id='r', product_id='p', route_key='p-v1', version='v1',
                upstream_url='https://provider.invalid', auth_mode='api_key', upstream_auth_mode='none',
                upstream_scope='', timeout_ms=5000, strip_prefix=False, health_method='GET',
                health_path='/health', rate_limit_per_minute=60, daily_quota=100, monthly_quota=1000,
                status='active')
            config = self.m.version_gateway['Config'](product_id='p', version_id='v', route_id='r',
                upstream_url='https://provider.invalid', route_key='p-v1', version_code='v1',
                auth_mode='api_key', upstream_auth_mode='none', upstream_scope='', timeout_ms=5000,
                strip_prefix=False, health_method='GET', health_path='/health',
                rate_limit_per_minute=60, daily_quota=100, monthly_quota=1000,
                status='verified', healthy=True, verified_by='provider',
                evidence_hash='published-evidence', error_code='')
            db.add(route)
            db.flush()
            db.add(config)
            db.commit()
        self.request = Mock(return_value=Mock(status_code=204))
        self.m.validated_http_request = self.request

    def tearDown(self):
        self.engine.dispose()

    def run_at(self, minute):
        instant = datetime(2026, 10, 13, 10, minute, tzinfo=timezone.utc)
        with self.Session() as db:
            return self.m.application_health['run_once'](db, instant=instant)

    def test_success_three_failures_single_incident_recovery_and_unchanged_publication(self):
        self.assertEqual(self.run_at(0), 1)
        self.request.return_value.status_code = 503
        for minute in (5, 10):
            self.assertEqual(self.run_at(minute), 1)
        self.notify.assert_not_called()
        self.assertEqual(self.run_at(15), 1)
        self.assertEqual(self.notify.call_count, 1)
        self.assertEqual(self.run_at(20), 1)
        self.assertEqual(self.notify.call_count, 1)
        self.request.return_value.status_code = 204
        self.assertEqual(self.run_at(25), 1)
        self.assertEqual(self.notify.call_count, 2)
        with self.Session() as db:
            obs = db.scalar(select(self.m.application_health['Observation']))
            config = db.get(self.m.version_gateway['Config'], db.scalar(select(self.m.version_gateway['Config'].id)))
            product = db.get(self.m.Product, 'p')
            self.assertEqual((obs.last_status, obs.failure_streak, obs.incident_active), ('healthy', 0, False))
            self.assertEqual(config.evidence_hash, 'published-evidence')
            self.assertEqual(config.status, 'verified')
            self.assertEqual(product.status, 'published')
            slots = db.scalars(select(self.m.application_health['Slot']).order_by(
                self.m.application_health['Slot'].slot)).all()
            self.assertEqual([entry.status for entry in slots],
                ['healthy', 'failed', 'failed', 'failed', 'failed', 'healthy'])
            self.assertTrue(all(entry.latency_ms is not None for entry in slots))
            self.assertTrue(all(entry.reason_code == 'upstream_unavailable' and entry.status_code == 503
                                for entry in slots[1:5]))
        # Same product/version and five-minute slot cannot be claimed twice.
        self.assertEqual(self.run_at(25), 0)

    def test_oauth_probe_uses_configured_scope_and_bearer_header(self):
        with self.Session() as db:
            route = db.get(self.m.ApiGatewayRoute, 'r')
            config = db.scalar(select(self.m.version_gateway['Config']))
            route.upstream_auth_mode = config.upstream_auth_mode = 'oauth2'
            route.upstream_scope = config.upstream_scope = 'data.read'
            route.upstream_client_id = 'probe-client'
            route.upstream_client_secret = 'probe-secret'
            db.commit()
        token_response = Mock()
        token_response.json.return_value = {'access_token': 'probe-token'}
        token_response.raise_for_status.return_value = None
        with unittest.mock.patch.object(self.m.httpx, 'post', return_value=token_response) as acquire:
            self.assertEqual(self.run_at(30), 1)
        self.assertEqual(acquire.call_args.kwargs['data']['scope'], 'data.read')
        self.assertEqual(acquire.call_args.kwargs['timeout'], 5.0)
        self.assertFalse(acquire.call_args.kwargs['follow_redirects'])
        self.assertEqual(self.request.call_args.kwargs['headers'], {'Authorization': 'Bearer probe-token'})

    def test_competing_sessions_cannot_claim_the_same_scheduled_slot(self):
        instant = datetime(2026, 10, 13, 11, 0, tzinfo=timezone.utc)
        first, second = self.Session(), self.Session()
        try:
            claim = self.m.application_health['claim']
            self.assertTrue(claim(first, 'p', 'v', 600000, instant))
            first.commit()
            self.assertFalse(claim(second, 'p', 'v', 600000, instant))
        finally:
            first.close()
            second.close()

    def pending_product(self):
        with self.Session() as db:
            product = db.get(self.m.Product, 'p')
            product.status = 'pending_integration'
            db.get(self.m.ApiGatewayRoute, 'r').status = 'draft'
            db.scalar(select(self.m.version_gateway['Config'])).status = 'pending_integration'
            db.commit()

    def add_pending_version(self):
        with self.Session() as db:
            version = self.m.ProductReleaseVersion(id='v2', product_id='p', version_code='v2',
                price=10, cost=1, status='active')
            db.add(version)
            db.flush()
            route = db.get(self.m.ApiGatewayRoute, 'r')
            fields = ('upstream_url', 'auth_mode', 'upstream_auth_mode', 'upstream_scope',
                'timeout_ms', 'strip_prefix', 'health_method', 'health_path',
                'rate_limit_per_minute', 'daily_quota', 'monthly_quota')
            values = {field: getattr(route, field) for field in fields}
            db.add(self.m.ApiGatewayRoute(id='r2', product_id='p', route_key='p-v2',
                version='v2', status='draft', **values))
            db.flush()
            db.add(self.m.version_gateway['Config'](product_id='p', version_id='v2',
                route_id='r2', route_key='p-v2', version_code='v2', status='pending_integration',
                **values))
            db.commit()

    def test_each_pending_version_uses_actual_verifier_and_own_publication_evidence(self):
        self.pending_product()
        self.add_pending_version()
        hook = self.m.version_gateway
        verifier = Mock(wraps=hook['verify_version'])
        with patch.dict(hook, verify_version=verifier), \
                patch.object(self.m, 'apisix_enabled', return_value=True), \
                patch.object(self.m, 'apisix_native_upstream', return_value=False), \
                patch.object(self.m, 'apisix_native_auth', return_value=False), \
                patch.object(self.m, 'apisix_admin_request', return_value={'key': 'route'}):
            self.assertEqual(self.run_at(35), 2)
            self.assertEqual([call.args[2] for call in verifier.call_args_list], ['v', 'v2'])
            with self.Session() as db:
                product = db.get(self.m.Product, 'p')
                self.assertEqual(product.status, 'published')
                configs = db.scalars(select(hook['Config'])).all()
                self.assertTrue(all(config.status == 'verified' for config in configs))
                self.assertEqual(len({config.publish_record_id for config in configs}), 2)
                for version in db.scalars(select(self.m.ProductReleaseVersion)):
                    self.assertTrue(hook['version_ready'](db, product, version))
            self.assertEqual(self.run_at(40), 2)
            self.assertEqual(verifier.call_count, 2)

    def test_invalid_pending_version_does_not_disable_verified_version(self):
        self.pending_product()
        self.add_pending_version()
        with self.Session() as db:
            db.get(self.m.ApiGatewayRoute, 'r2').route_key = 'mismatch'
            db.commit()
        hook = self.m.version_gateway
        verifier = Mock(wraps=hook['verify_version'])
        with patch.dict(hook, verify_version=verifier), \
                patch.object(self.m, 'apisix_enabled', return_value=True), \
                patch.object(self.m, 'apisix_native_upstream', return_value=False), \
                patch.object(self.m, 'apisix_native_auth', return_value=False), \
                patch.object(self.m, 'apisix_admin_request', return_value={'key': 'route'}):
            self.assertEqual(self.run_at(35), 2)
            self.assertEqual(verifier.call_count, 1)
            with self.Session() as db:
                product = db.get(self.m.Product, 'p')
                self.assertEqual(product.status, 'published')
                self.assertTrue(hook['version_ready'](db, product, db.get(self.m.ProductReleaseVersion, 'v')))
                self.assertFalse(hook['version_ready'](db, product, db.get(self.m.ProductReleaseVersion, 'v2')))

    def test_timeout_and_address_rejection_retain_completed_slot(self):
        for minute, error, reason in ((35, TimeoutError('private details'), 'probe_timeout'),
                (40, ValueError('private details'), 'address_policy_rejected')):
            self.request.side_effect = error
            self.assertEqual(self.run_at(minute), 1)
            with self.Session() as db:
                slot = db.scalar(select(self.m.application_health['Slot']).order_by(
                    self.m.application_health['Slot'].slot.desc()))
                self.assertEqual((slot.status, slot.reason_code), ('failed', reason))
                self.assertIsNotNone(slot.completed_at)
                self.assertIsNotNone(slot.latency_ms)
                self.assertEqual(db.scalar(select(self.m.version_gateway['Config'])).evidence_hash,
                    'published-evidence')

    def test_first_pending_api_verification_publishes_only_after_success(self):
        self.pending_product()

        def verify(db, product, version_id, actor):
            self.assertEqual(version_id, 'v')
            self.assertEqual(actor.platform_role, 'platform_operator')
            config = db.scalar(select(self.m.version_gateway['Config']))
            config.status, config.healthy = 'verified', True
            product.status = 'published'
            return config

        verifier = Mock(side_effect=verify)
        with patch.dict(self.m.version_gateway, verify_version=verifier):
            self.assertEqual(self.run_at(35), 1)
        verifier.assert_called_once()
        with self.Session() as db:
            self.assertEqual(db.get(self.m.Product, 'p').status, 'published')
            observation = db.scalar(select(self.m.application_health['Observation']))
            self.assertEqual(observation.last_status, 'healthy')

    def test_failed_first_pending_api_verification_stays_pending(self):
        self.pending_product()

        def verify(db, product, version_id, actor):
            config = db.scalar(select(self.m.version_gateway['Config']))
            config.status, config.healthy = 'failed', False
            return config

        verifier = Mock(side_effect=verify)
        with patch.dict(self.m.version_gateway, verify_version=verifier):
            self.assertEqual(self.run_at(35), 1)
        with self.Session() as db:
            self.assertEqual(db.get(self.m.Product, 'p').status, 'pending_integration')
            observation = db.scalar(select(self.m.application_health['Observation']))
            self.assertEqual(observation.last_status, 'failed')


if __name__ == '__main__':
    unittest.main()
