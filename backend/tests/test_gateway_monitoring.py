"""Gateway HTTP contracts using isolated production models; no startup/live IO."""
from datetime import timedelta
import importlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import test_message_business_events as fixture


class GatewayMonitoringTests(unittest.TestCase):
    setUpClass = classmethod(fixture.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.BusinessRoutesHTTPTests.tearDownClass.__func__)
    tearDown = fixture.BusinessRoutesHTTPTests.tearDown
    request = fixture.BusinessRoutesHTTPTests.request

    def setUp(self):
        fixture.BusinessRoutesHTTPTests.setUp(self)
        self.g = importlib.import_module(self.m.__package__ + '.gateway_monitoring')
        self.g.ensure_schema(self.m.engine)
        self.g.ensure_schema(self.m.engine)
        m = self.m
        with m.SessionLocal() as db:
            db.add(m.Product(id='history', enterprise_id='provider_tenant', name='Old API', product_type='api', status='security_unpublished'))
            db.flush()
            for rid, pid, version in [('a', 'product', 'v1'), ('b', 'product', 'v2'), ('h', 'history', 'v1')]:
                db.add(m.ApiGatewayRoute(id=rid, product_id=pid, route_key='route-' + rid, upstream_url='https://invalid', version=version, status='active', updated_at=m.now()))
            db.flush()
            for rid in ('a', 'b', 'h'):
                db.add(m.GatewayConfigRevision(id='rev-' + rid, route_id=rid, product_id='history' if rid == 'h' else 'product', revision=1))
            db.flush()
            for rid in ('a', 'b', 'h'):
                db.add(m.GatewayPublishRecord(id='fail-' + rid, route_id=rid, revision_id='rev-' + rid, status='failed', error_message='failure'))
            db.add(m.ApiCredential(id='cred', route_id='a', enterprise_id='buyer_tenant', name='test', key_hash='test'))
            db.flush()
            for uid, rid, code, age in [('u1', 'a', 200, 1), ('u2', 'a', 500, 2), ('u3', 'b', 200, 1), ('u4', 'a', 200, 25)]:
                db.add(m.ApiUsage(id=uid, route_id=rid, credential_id='cred', enterprise_id='buyer_tenant', method='GET', status_code=code, latency_ms=10, created_at=m.now() - timedelta(hours=age)))
            db.commit()

    def get(self, path, **kwargs):
        return self.request('GET', path, actor='platform', **kwargs)

    def action(self, action, version=0, key=None, note='', expected=200, route='a'):
        return self.request('POST', f'/api/gateway/routes/{route}/alerts/fail-a/{action}', actor='ops', expected=expected,
                            json={'request_id': key or action, 'expected_version': version, 'note': note})

    def test_inventory_legacy_published_default_history_filters_and_validation(self):
        self.assertEqual(len(self.get('/api/gateway/routes')['items']), 3)
        result = self.get('/api/gateway/routes', params={'page': 1})
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['page_size'], 20)
        self.assertTrue(all(x['product_status'] == 'published' for x in result['items']))
        for params, count in [({'product_status': 'all'}, 3), ({'product_status': 'security_unpublished'}, 1),
                              ({'route_name': ' route-a ', 'provider': 'provider', 'product_name': 'Test'}, 1),
                              ({'route_name': '%'}, 0), ({'health_status': 'unknown'}, 2)]:
            self.assertEqual(self.get('/api/gateway/routes', params=dict(page=1, **params))['total'], count)
        for params in ({'page': 0}, {'page': 10001}, {'page_size': 12}, {'product_status': 'bogus'}, {'route_status': 'bogus'}, {'health_status': 'bogus'}, {'route_name': 'x' * 201}):
            self.get('/api/gateway/routes', params=params, expected=422)

    def test_stable_pages_and_clamp(self):
        m = self.m
        instant = m.now()
        with m.SessionLocal() as db:
            for i in range(25):
                db.add(m.ApiGatewayRoute(id=f'z{i:02}', product_id='product', route_key=f'z{i:02}', version=f'z{i:02}', upstream_url='https://invalid', updated_at=instant))
            db.commit()
        seen = []
        for page in (1, 2, 3):
            result = self.get('/api/gateway/routes', params={'route_name': 'z', 'page': page, 'page_size': 10})
            self.assertEqual(result['total'], 25)
            seen += [r['id'] for r in result['items']]
        self.assertEqual(seen, [f'z{i:02}' for i in reversed(range(25))])
        self.assertEqual(self.get('/api/gateway/routes', params={'route_name': 'z', 'page': 99, 'page_size': 10})['page'], 3)

    def test_route_details_usage_window_and_gets_never_materialize_alerts(self):
        m = self.m
        stats = self.get('/api/gateway/routes/a/stats')
        self.assertEqual(stats['summary']['total'], 2)
        self.assertEqual(stats['summary']['error'], 1)
        self.assertEqual(stats['source'], 'api_usage')
        self.assertEqual(self.get('/api/gateway/routes/h/stats')['summary']['total'], None)
        for kind in ('revisions', 'alerts'):
            result = self.get('/api/gateway/routes/a/' + kind)
            self.assertEqual(result['total'], 1)
            self.assertEqual(result['items'][0]['route_id'], 'a')
            self.get('/api/gateway/routes/missing/' + kind, expected=404)
        self.get('/api/gateway/alerts')
        self.get('/api/gateway/routes/a/alerts/fail-a/audit')
        with m.SessionLocal() as db:
            self.assertEqual(db.scalar(self.select(self.func.count()).select_from(self.g.GatewayAlertState)), 0)
            self.assertEqual(db.scalar(self.select(self.func.count()).select_from(self.g.GatewayAlertAction)), 0)
        self.get('/api/gateway/routes/b/alerts/fail-a/audit', expected=404)
        self.action('acknowledge', route='b', expected=404)

    def test_detail_pagination_validation_and_response_fields(self):
        for suffix in ('stats', 'revisions', 'alerts', 'alerts/fail-a/audit'):
            path = '/api/gateway/routes/a/' + suffix
            for size in (10, 20, 50, 100):
                self.assertEqual(self.get(path, params={'page_size': size})['page_size'], size)
            for params in ({'page_size': 12}, {'page': 0}, {'page': 10001}):
                self.get(path, params=params, expected=422)
        item = self.get('/api/gateway/routes/a/revisions')['items'][0]
        self.assertEqual(item['revision'], 1)
        self.assertNotIn('config_json', item)
        for hours in (0, 2161):
            self.get('/api/gateway/routes/a/stats', params={'window_hours': hours}, expected=422)

    def test_lifecycle_idempotency_stale_conflict_and_durable_audit(self):
        self.action('process', expected=409)
        self.action('close', expected=422)
        self.action('acknowledge')
        self.assertTrue(self.action('acknowledge')['replayed'])
        self.action('acknowledge', note='changed', expected=409)
        self.action('process', version=0, expected=409)
        self.action('process', version=1)
        self.action('close', version=2, note='Investigated')
        self.assertTrue(self.action('acknowledge')['replayed'])
        self.action('close', version=3, key='new-close', note='again', expected=409)
        result = self.get('/api/gateway/routes/a/alerts', params={'status': 'closed'})
        self.assertEqual(result['items'][0]['version'], 3)
        audit = self.get('/api/gateway/routes/a/alerts/fail-a/audit')
        self.assertEqual(audit['total'], 3)
        self.assertEqual({x['actor'] for x in audit['items']}, {'ops'})
        self.assertEqual({(x['before_status'], x['after_status']) for x in audit['items']}, {('open', 'acknowledged'), ('acknowledged', 'processing'), ('processing', 'closed')})

    def test_direct_close_does_not_hide_next_failure(self):
        self.action('close', note='Known issue')
        m = self.m
        with m.SessionLocal() as db:
            db.add(m.GatewayPublishRecord(id='next-failure', route_id='a', revision_id='rev-a', status='failed'))
            db.commit()
        items = self.get('/api/gateway/routes/a/alerts')['items']
        self.assertEqual({x['id']: x['status'] for x in items}, {'fail-a': 'closed', 'next-failure': 'open'})

    def test_generic_audit_before_after_idempotency_and_atomic_failure(self):
        m = self.m
        with patch.object(m, 'audit', side_effect=RuntimeError('generic audit unavailable')):
            self.action('acknowledge', expected=500)
        self.assertEqual(self.get('/api/gateway/routes/a/alerts')['items'][0]['version'], 0)
        self.assertEqual(self.get('/api/gateway/routes/a/alerts/fail-a/audit')['total'], 0)
        self.action('acknowledge')
        self.action('acknowledge')
        with m.SessionLocal() as db:
            rows = db.scalars(self.select(m.AuditLog).where(m.AuditLog.target_type == 'gateway_alert')).all()
            self.assertEqual(len(rows), 1)
            event = rows[0]
            self.assertEqual((event.actor, event.target_id, event.request_id), ('ops', 'fail-a', 'acknowledge'))
            self.assertEqual(json.loads(event.before_json), {'route_id': 'a', 'status': 'open', 'version': 0})
            self.assertEqual(json.loads(event.after_json), {'route_id': 'a', 'status': 'acknowledged', 'version': 1})

    def test_health_uses_matching_fresh_probe_not_route_status(self):
        m = self.m
        initial = self.get('/api/gateway/routes', params={'route_name': 'route-a'})['items'][0]
        self.assertEqual((initial['health_status'], initial['health_source']), ('unknown', 'none'))
        Config, Observation = m.version_gateway['Config'], m.application_health['Observation']
        checked = m.now()
        with m.SessionLocal() as db:
            route = db.get(m.ApiGatewayRoute, 'a')
            fields = ('upstream_url', 'route_key', 'auth_mode', 'upstream_auth_mode', 'upstream_scope',
                      'timeout_ms', 'strip_prefix', 'health_method', 'health_path',
                      'rate_limit_per_minute', 'daily_quota', 'monthly_quota')
            db.add(Config(id='health-config', product_id='product', version_id='version', route_id='a',
                          version_code='v1', **{k: getattr(route, k) for k in fields}))
            db.add(Observation(id='probe', product_id='product', version_id='version', last_slot=1,
                               last_checked_at=checked, last_status='healthy', updated_at=checked))
            db.commit()
        result = self.get('/api/gateway/routes', params={'page': 1, 'health_status': 'healthy'})
        self.assertEqual([r['id'] for r in result['items']], ['a'])
        self.assertEqual(result['items'][0]['health_source'], 'application_health_observations')
        self.assertEqual(result['items'][0]['health_reason'], 'observed')
        with m.SessionLocal() as db:
            db.get(Observation, 'probe').last_status = 'failed'
            db.commit()
        self.assertEqual(self.get('/api/gateway/routes', params={'page': 1, 'health_status': 'unhealthy'})['total'], 1)
        with m.SessionLocal() as db:
            db.get(Observation, 'probe').last_checked_at = checked - timedelta(minutes=11)
            db.commit()
        stale = self.get('/api/gateway/routes', params={'route_name': 'route-a'})['items'][0]
        self.assertEqual(stale['health_status'], 'unknown')
        self.assertEqual(stale['health_reason'], 'stale_or_configuration_changed')
        with m.SessionLocal() as db:
            db.get(Observation, 'probe').last_checked_at = checked
            db.get(m.ApiGatewayRoute, 'a').upstream_url = 'https://changed.invalid'
            db.commit()
        self.assertEqual(self.get('/api/gateway/routes', params={'route_name': 'route-a'})['items'][0]['health_status'], 'unknown')

    def test_native_telemetry_observed_counts_scoped_without_merging_legacy(self):
        m = self.m
        with m.SessionLocal() as db:
            db.add(m.ProductReleaseVersion(id='version-b', product_id='product', version_code='v2'))
            db.flush()
            events = []
            for i, rid, code, age in [(1, 'route-a', 200, 1), (2, 'a', 503, 2),
                                     (3, 'route-b', 200, 1), (4, 'route-a', 500, 25),
                                     (5, 'missing', 200, 1)]:
                identity = 'gw_' + format(i, '032x')
                events.append(dict(event_id=identity, request_id=identity, product_id='product',
                                   route_id=rid, entry_id=rid, status_code=code,
                                   completed_at_ms=int((m.now() - timedelta(hours=age)).timestamp() * 1000)))
            m.sla_gateway_events['ingest'](db, events)
            db.commit()
        stats = self.get('/api/gateway/routes/a/stats')
        self.assertEqual(stats['source'], 'sla_gateway_terminal_events')
        self.assertEqual(stats['collection_status'], 'observed')
        self.assertFalse(stats['counts_complete'])
        self.assertEqual(stats['coverage'], 'unknown')
        self.assertEqual(stats['summary'], {'total': 2, 'success': 1, 'error': 1, 'avg_latency_ms': None})
        self.assertTrue(all(row['method'] is None and row['path'] is None for row in stats['items']))
        self.assertEqual(self.get('/api/gateway/routes/b/stats')['summary']['total'], 1)
        legacy = self.get('/api/gateway/routes/a/stats', params={'source': 'api_usage'})
        self.assertEqual(legacy['source'], 'api_usage')
        self.assertEqual(legacy['summary']['total'], 2)
        with patch.object(m, 'apisix_native_upstream', return_value=True):
            empty = self.get('/api/gateway/routes/h/stats')
            self.assertEqual(empty['source'], 'sla_gateway_terminal_events')
            self.assertEqual(empty['collection_status'], 'unknown')
            self.assertIsNone(empty['summary']['total'])
        self.get('/api/gateway/routes/a/stats', params={'source': 'invalid'}, expected=422)

    def test_roles_are_identical_for_inventory_details_actions_and_audit(self):
        paths = ['/api/gateway/routes', '/api/gateway/alerts', '/api/gateway/routes/a/stats',
                 '/api/gateway/routes/a/revisions', '/api/gateway/routes/a/alerts', '/api/gateway/routes/a/alerts/fail-a/audit']
        for actor in ('buyer', 'provider', 'reviewer', 'finance'):
            for path in paths:
                self.request('GET', path, actor=actor, expected=403)
            self.request('POST', '/api/gateway/routes/a/alerts/fail-a/close', actor=actor, expected=403,
                         json={'request_id': 'x', 'expected_version': 0, 'note': 'reason'})
        for path in paths:
            self.request('GET', path, actor='ops')

    def test_atomic_rollback_on_audit_failure(self):
        with patch.object(self.g, 'GatewayAlertAction', side_effect=RuntimeError('audit unavailable')):
            self.action('acknowledge', expected=500)
        self.assertEqual(self.get('/api/gateway/routes/a/alerts')['items'][0]['version'], 0)
        self.assertEqual(self.get('/api/gateway/routes/a/alerts/fail-a/audit')['total'], 0)

    def test_two_writers_cannot_overwrite_same_version(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        m = self.m
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'race.db')
            source = m.engine.raw_connection()
            try:
                with sqlite3.connect(path) as destination:
                    source.driver_connection.backup(destination)
            finally:
                source.close()
            race_engine = create_engine('sqlite:///' + path, connect_args={'check_same_thread': False})
            original_factory = m.SessionLocal
            m.SessionLocal = sessionmaker(bind=race_engine, autoflush=False)
            try:
                def write(key):
                    return self.client.post('/api/gateway/routes/a/alerts/fail-a/acknowledge',
                        headers={'Authorization': 'Bearer ' + self.tokens['ops']},
                        json={'request_id': key, 'expected_version': 0}).status_code
                with ThreadPoolExecutor(max_workers=2) as pool:
                    codes = list(pool.map(write, ['race-one', 'race-two']))
                self.assertEqual(sorted(codes), [200, 409])
                race_engine.dispose()
                self.assertEqual(self.get('/api/gateway/routes/a/alerts')['items'][0]['version'], 1)
                self.assertEqual(self.get('/api/gateway/routes/a/alerts/fail-a/audit')['total'], 1)
            finally:
                m.SessionLocal = original_factory
                race_engine.dispose()


if __name__ == '__main__':
    unittest.main()
