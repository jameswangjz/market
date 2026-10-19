"""Committed provider operations integrated with the actual delivery worker."""
import copy
import importlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import test_api_auto_delivery as fixture
import test_refund_execution_resources as refund_fixture


class LifecycleDeliveryTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ApiAutoDeliveryTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.ApiAutoDeliveryTests.tearDownClass.__func__)
    tearDown = fixture.ApiAutoDeliveryTests.tearDown
    request = fixture.ApiAutoDeliveryTests.request
    pay = fixture.ApiAutoDeliveryTests.pay
    task_id = fixture.ApiAutoDeliveryTests.task_id
    create = fixture.ApiAutoDeliveryTests.create
    step = fixture.ApiAutoDeliveryTests.step
    redis_eval = fixture.ApiAutoDeliveryTests.redis_eval
    admin_request = fixture.ApiAutoDeliveryTests.admin_request

    def setUp(self):
        fixture.ApiAutoDeliveryTests.setUp(self)
        self.setup_provider()

    def setup_provider(self):
        m = self.m
        module = importlib.import_module(m.__package__ + '.api_provider_lifecycle')
        self.lifecycle = module.install(vars(m))
        m.Base.metadata.create_all(m.engine)
        self.temp = tempfile.TemporaryDirectory(prefix='api-lifecycle-callers-')
        self.addCleanup(self.temp.cleanup)
        filename = str(Path(self.temp.name) / 'journal.sqlite')
        # A file database gives execute's durability check a separate connection.
        source = m.engine.raw_connection()
        with sqlite3.connect(filename) as destination:
            source.driver_connection.backup(destination)
        source.close()
        m.engine.dispose()
        m.engine = create_engine('sqlite:///' + filename, connect_args={'check_same_thread': False})
        @event.listens_for(m.engine, 'connect')
        def configure(connection, _):
            connection.isolation_level = None
            connection.execute('PRAGMA foreign_keys=ON')
        @event.listens_for(m.engine, 'begin')
        def begin(connection):
            connection.exec_driver_sql('BEGIN')
        m.SessionLocal = sessionmaker(bind=m.engine, autoflush=False)
        self.endpoint = 'https://provider.example.invalid/manage'
        with m.SessionLocal() as db:
            db.get(m.Product, 'product').integration_api_url = self.endpoint
            db.add(m.OAuthClient(product_id='product', client_id='provider-client',
                client_secret='test-secret', scope='resource.invoke resource.manage', status='active'))
            db.commit()
        self.provider_calls = []
        self.remote = {}
        self.bad_provider = False
        self.network.enter_context(patch.dict(m.application_address_policy,
            {'validate_url': lambda *args, **kwargs: None}))
        self.network.enter_context(patch.object(m, 'validate_upstream_url', return_value=None))
        self.network.enter_context(patch.object(m, 'saas_access_token', return_value='test-token'))
        self.network.enter_context(patch.object(m, 'validated_http_request', side_effect=self.http))

    def http(self, method, url, **kwargs):
        if url != self.endpoint:
            return self.health
        body = json.loads(kwargs['content'])
        self.provider_calls.append(copy.deepcopy(body))
        if body['type'] == 'STATUS':
            response = copy.deepcopy(self.remote[body['event_id']])
        else:
            response = self.remote.setdefault(body['event_id'], dict(body, ret=0,
                tenant_id='provider-tenant', status='closed' if body['type'] == 'CLOSE' else 'active',
                effective_at=body['starts_at']))
            response = copy.deepcopy(response)
        if self.bad_provider:
            response['version'] = {'id': 'wrong', 'code': 'wrong'}
        return Mock(status_code=200, json=lambda: response)

    def preparation(self, oid):
        with self.m.SessionLocal() as db:
            return json.loads(db.get(self.api['Preparation'], oid).frozen_json)

    def test_prepare_has_no_term_or_success_and_second_tick_uses_reserved_ids(self):
        oid = self.create(months=2)
        self.step(oid)
        self.assertEqual(self.provider_calls, [])
        frozen = self.preparation(oid)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.SubscriptionTerm).count(), 0)
            self.assertEqual(db.query(self.m.SubscriptionCombination).count(), 0)
            task = db.query(self.m.DeliveryTask).filter_by(order_id=oid).one()
            self.assertEqual((task.status, task.retry_count), ('in_delivery', 0))
            self.assertIsNotNone(task.next_retry_at)
            self.assertEqual(db.query(self.lifecycle['Plan']).count(), 1)
            self.assertEqual(db.query(self.m.AuditLog).filter(
                self.m.AuditLog.order_id == oid,
                self.m.AuditLog.action.in_(['subscription_term_granted', 'delivery_succeeded'])).count(), 0)
        self.step(oid)
        with self.m.SessionLocal() as db:
            task = db.query(self.m.DeliveryTask).filter_by(order_id=oid).one()
            self.assertEqual(task.status, 'completed', task.last_error)
            term = db.query(self.m.SubscriptionTerm).filter_by(order_id=oid).one()
            self.assertEqual((term.id, term.combination_id), (frozen['term_id'], frozen['combination_id']))
            self.assertEqual(term.created_at.isoformat(), frozen['planned_at'])
        self.assertEqual(self.provider_calls[0], frozen['provider']['payload'])
        self.assertEqual([body['type'] for body in self.provider_calls], ['OPEN', 'STATUS'])

    def test_gateway_failure_keeps_committed_body_for_provider_replay(self):
        oid = self.create()
        self.step(oid)
        frozen = self.preparation(oid)
        with patch.dict(self.m.initial_api_credentials,
                {'ensure_initial': Mock(side_effect=RuntimeError('gateway failure'))}):
            self.step(oid)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.SubscriptionTerm).count(), 0)
            self.assertEqual(db.query(self.lifecycle['Receipt']).count(), 0)
            self.assertEqual(db.query(self.lifecycle['Plan']).count(), 1)
        with patch.object(self.m, 'now', return_value=self.m.now() + timedelta(days=1)):
            self.step(oid)
        self.assertEqual(self.preparation(oid), frozen)
        self.assertEqual(self.provider_calls[0], self.provider_calls[2])
        with self.m.SessionLocal() as db:
            term = db.query(self.m.SubscriptionTerm).filter_by(order_id=oid).one()
            self.assertEqual(term.created_at.isoformat(), frozen['planned_at'])

    def test_provider_failure_never_grants_term_or_credentials(self):
        oid = self.create()
        self.step(oid)
        self.bad_provider = True
        self.step(oid)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.SubscriptionTerm).count(), 0)
            self.assertEqual(db.query(self.m.ApiCredential).count(), 0)
            task = db.query(self.m.DeliveryTask).filter_by(order_id=oid).one()
            self.assertEqual((task.status, task.retry_count), ('retrying', 1))

    def test_legacy_snapshot_stays_unmanaged_after_live_configuration(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, 'product').integration_api_url = ''
            db.commit()
        oid = self.create()
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, 'product').integration_api_url = self.endpoint
            db.commit()
        self.step(oid)
        self.assertEqual(self.provider_calls, [])
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.SubscriptionTerm).filter_by(order_id=oid).count(), 1)
            self.assertEqual(db.query(self.lifecycle['Plan']).count(), 0)

    def test_pending_order_cannot_be_overtaken(self):
        first = self.create()
        second = self.create()
        self.step(first)
        self.step(second)
        with self.m.SessionLocal() as db:
            self.assertIsNotNone(db.get(self.api['Preparation'], first))
            self.assertIsNone(db.get(self.api['Preparation'], second))
            self.assertEqual(db.query(self.m.SubscriptionTerm).count(), 0)

    def test_renewal_rejects_changed_anchor_before_provider_call(self):
        oid = self.create()
        self.step(oid)
        self.step(oid)
        renewal = self.create()
        self.step(renewal)
        count = len(self.provider_calls)
        with self.m.SessionLocal() as db:
            combo = db.query(self.m.SubscriptionCombination).one()
            combo.anchor_at += timedelta(days=1)
            db.commit()
        self.step(renewal)
        self.assertEqual(len(self.provider_calls), count)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.SubscriptionTerm).filter_by(order_id=renewal).count(), 0)

    def test_microsecond_anchor_renewal_preserves_native_calendar_edges(self):
        at = datetime(2026, 10, 19, 12, 0, 0, 123456, timezone.utc)
        with patch.object(self.m, 'now', return_value=at):
            first = self.create()
            self.step(first)
            self.step(first)
            renewal = self.create()
            self.step(renewal)
            frozen = self.preparation(renewal)
            body = frozen['provider']['payload']
            self.assertEqual(body['starts_at'], at.isoformat())
            self.assertEqual(body['periods'][0]['starts_at'], at.isoformat())
            self.assertTrue(all(datetime.fromisoformat(p['expires_at']).microsecond == 123456
                for p in body['periods']))
            self.step(renewal)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.SubscriptionTerm).filter_by(order_id=renewal).count(), 1)

    def test_expired_combination_restores_existing_tenant_in_new_generation(self):
        first = self.create()
        self.step(first)
        self.step(first)
        with self.m.SessionLocal() as db:
            combo = db.query(self.m.SubscriptionCombination).one()
            combo_id, generation = combo.id, combo.generation
            expiry = db.query(self.m.SubscriptionTerm).one().ends_at
        with patch.object(self.m, 'now', return_value=expiry + timedelta(seconds=1)):
            self.assertTrue(self.m.online_delivery['process_once']())
            with self.m.SessionLocal() as db:
                self.assertEqual(db.get(self.m.Order, first).main_status, 'completed')
            renewal = self.create()
            self.step(renewal)
            body = self.preparation(renewal)['provider']['payload']
            self.assertEqual((body['type'], body['tenant_id'], body['combination_id'], body['generation']),
                ('RESTORE', 'provider-tenant', combo_id, generation + 1))
            self.step(renewal)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.SubscriptionTerm).filter_by(order_id=renewal).one().generation,
                generation + 1)

    def prepare_version_change(self):
        m = self.m
        oid = self.create()
        self.step(oid)
        self.step(oid)
        self.provider_calls.clear()
        self.health.extensions = {'validated_upstream_addresses': ['192.0.2.10']}
        payloads = {'route-v1': {'name': 'original'}, 'route-v2': {'name': 'target'}}
        def admin(method, path, payload=None):
            if method == 'GET' and path.startswith('/routes/'):
                return payloads[path.rsplit('/', 1)[-1]]
            return self.admin_request(method, path, payload)
        self.network.enter_context(patch.object(m, 'apisix_admin_request', side_effect=admin))
        self.network.enter_context(patch.object(m, 'apisix_route_payload',
            side_effect=lambda route, *args: payloads[route.route_key]))
        self.network.enter_context(patch.object(m, 'refund_segment_store', None))
        with m.SessionLocal() as db:
            old_route = db.get(m.ApiGatewayRoute, 'route')
            old_route.validated_ip = '192.0.2.10'
            db.add(m.ProductReleaseVersion(id='target-version', product_id='product', version_code='v2',
                price=200, cost=20, status='active', rate_limit_per_minute=60,
                daily_quota=24, monthly_quota=600, quota_amount=0))
            values = {c.name: getattr(old_route, c.name) for c in old_route.__table__.columns}
            values.update(id='target-route', version='v2', route_key='route-v2',
                rate_limit_per_minute=60, daily_quota=24, monthly_quota=600)
            db.add(m.ApiGatewayRoute(**values))
            db.flush()
            Config = m.version_gateway['Config']
            old_config = db.get(Config, 'config')
            values = {c.name: getattr(old_config, c.name) for c in old_config.__table__.columns}
            values.update(id='target-config', version_id='target-version', route_id='target-route',
                route_key='route-v2', version_code='v2', evidence_hash='target-fixture',
                rate_limit_per_minute=60, daily_quota=24, monthly_quota=600)
            config = Config(**values)
            db.add(config)
            db.flush()
            term = db.query(m.SubscriptionTerm).filter_by(order_id=oid).one()
            target = copy.deepcopy(m.order_economics['delivery_snapshot'](db.get(m.Order, oid)))
            target['quota'].update(rate_limit_per_minute=60, daily_quota=24, monthly_quota=600)
            snapshot = dict(delivery=target, integration=dict(config_id=config.id,
                revision_id=config.revision_id, evidence_hash=config.evidence_hash,
                route_id='target-route', route_key='route-v2', quote_fingerprint='q' * 64))
            change = m.VersionChangeRequest(id='lifecycle-change', source_order_id=oid,
                enterprise_id='buyer_tenant', combination_id=term.combination_id,
                target_version_id='target-version', target_version_code='v2',
                old_version_id='version', old_version_code='v1', kind='upgrade', status='resource_pending',
                idempotency_key='lifecycle-change', request_hash='h' * 64,
                quote_json=json.dumps({'quote_id': 'q' * 64}), prepared_json='[]',
                target_snapshots_json=json.dumps({term.id: snapshot}), created_by='buyer',
                resource_plan_json='{}', created_at=m.now())
            db.add(change)
            db.flush()
            plan = m.version_change_resources['prepare'](db, change)
            db.commit()
            return plan

    def test_version_change_freezes_and_replays_forward_and_compensation(self):
        at = self.m.now().replace(microsecond=123456)
        with patch.object(self.m, 'now', return_value=at):
            plan = self.prepare_version_change()
        self.assertEqual(datetime.fromisoformat(plan['provider']['target']['payload']['starts_at']).microsecond,
            123456)
        self.assertEqual(self.provider_calls, [])
        self.assertEqual(plan['provider']['target']['payload']['version']['code'], 'v2')
        self.assertEqual(plan['provider']['original']['payload']['direction'], 'restore')
        with self.m.SessionLocal() as db:
            change = db.get(self.m.VersionChangeRequest, 'lifecycle-change')
            first = self.m.version_change_resources['apply'](db, change, 'target')
            db.rollback()
        with self.m.SessionLocal() as db:
            change = db.get(self.m.VersionChangeRequest, 'lifecycle-change')
            self.assertEqual(self.m.version_change_resources['apply'](db, change, 'target'), first)
            db.commit()
        self.assertEqual(self.provider_calls[0], self.provider_calls[2])
        with self.m.SessionLocal() as db:
            change = db.get(self.m.VersionChangeRequest, 'lifecycle-change')
            self.m.version_change_resources['apply'](db, change, 'original')
            db.commit()
            self.assertEqual(db.query(self.m.ApiCredential).one().route_id, 'route')
        self.assertEqual(self.provider_calls[4], plan['provider']['original']['payload'])

    def test_version_provider_failure_precedes_gateway_and_success_proof(self):
        self.prepare_version_change()
        baseline = copy.deepcopy(self.redis_values)
        self.bad_provider = True
        with self.m.SessionLocal() as db:
            change = db.get(self.m.VersionChangeRequest, 'lifecycle-change')
            with self.assertRaises(Exception):
                self.m.version_change_resources['apply'](db, change, 'target')
            self.assertEqual(change.evidence_json, '{}')
            self.assertEqual(db.query(self.m.ApiCredential).one().route_id, 'route')
        self.assertEqual(self.redis_values, baseline)


class LifecycleRefundTests(unittest.TestCase):
    setUpClass = classmethod(refund_fixture.ExecutionResourcesTests.setUpClass.__func__)
    tearDownClass = classmethod(refund_fixture.ExecutionResourcesTests.tearDownClass.__func__)
    tearDown = refund_fixture.ExecutionResourcesTests.tearDown
    create = refund_fixture.ExecutionResourcesTests.create
    deliver = refund_fixture.ExecutionResourcesTests.deliver
    periods = refund_fixture.ExecutionResourcesTests.periods
    entitlement = refund_fixture.ExecutionResourcesTests.entitlement
    fixture = refund_fixture.ExecutionResourcesTests.fixture
    apply = refund_fixture.ExecutionResourcesTests.apply
    setup_provider = LifecycleDeliveryTests.setup_provider
    http = LifecycleDeliveryTests.http

    def setUp(self):
        refund_fixture.ExecutionResourcesTests.setUp(self)
        self.setup_provider()

    def test_prepare_commits_provider_plan_with_resource_plan(self):
        oid, other, plan = self.fixture()
        operation = plan['provider']
        self.assertEqual(self.provider_calls, [])
        self.assertEqual(operation['payload']['type'], 'ADJUST')
        self.assertEqual(operation['payload']['refund_id'], 'execution-refund')
        self.assertEqual({p['order_id'] for p in operation['payload']['periods']}, {oid, other})
        with self.m.SessionLocal() as db:
            row = db.get(self.lifecycle['Plan'], operation['plan_id'])
            self.assertEqual(row.payload, operation['payload'])
        self.apply()
        self.assertEqual(self.provider_calls[0], operation['payload'])

    def test_provider_failure_preserves_gateway_and_blocks_resource_success(self):
        _, _, plan = self.fixture()
        baseline = self.redis.hgetall(plan['key'])
        self.bad_provider = True
        with self.assertRaises(Exception):
            self.apply()
        self.assertEqual(self.redis.hgetall(plan['key']), baseline)
        with self.m.SessionLocal() as db:
            execution = db.get(self.m.refund_execution['Execution'], 'owned-execution')
            self.assertEqual(execution.status, 'resources_pending')
            self.assertEqual(db.query(self.resources['Recovery']).count(), 0)
            self.assertEqual(db.query(self.lifecycle['Receipt']).count(), 0)

    def test_rollback_replays_frozen_provider_and_gateway_operation(self):
        self.fixture()
        first = self.apply(rollback=True)
        second = self.apply()
        self.assertEqual(first, second)
        self.assertEqual(self.provider_calls[0], self.provider_calls[2])

    def test_microsecond_refund_bounds_follow_exact_target_rights(self):
        self.at = self.at.replace(microsecond=123456)
        self.m.now.return_value = self.at
        oid, _, plan = self.fixture(unlimited=True)
        body = plan['provider']['payload']
        selected = [p for p in body['periods'] if p['order_id'] == oid]
        self.assertEqual(selected[0]['starts_at'], self.at.isoformat())
        self.assertEqual(max(p['expires_at'] for p in selected), plan['effective_ends_at'])
        self.assertEqual(body['expires_at'], max(p['expires_at'] for p in body['periods']))
        module = importlib.import_module(self.m.__package__ + '.api_auto_delivery')
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            term = db.query(self.m.SubscriptionTerm).filter_by(order_id=oid).one()
            combo = db.get(self.m.SubscriptionCombination, term.combination_id)
            projected = module.provider_operation(vars(self.m), db, order, term, combo,
                endpoint=self.endpoint, periods=selected, event_type='ADJUST',
                operation_key='refund:shortened-projection', refund_id='execution-refund')
            self.assertEqual(projected['payload']['expires_at'], plan['effective_ends_at'])
            self.assertLess(datetime.fromisoformat(projected['payload']['expires_at']),
                db.query(self.m.SubscriptionTerm).order_by(self.m.SubscriptionTerm.ends_at.desc()).first().ends_at)
            closed = module.provider_operation(vars(self.m), db, order, term, combo,
                endpoint=self.endpoint, periods=[], event_type='CLOSE',
                operation_key='refund:closed-projection', refund_id='execution-refund')
            self.assertEqual(closed['payload']['expires_at'],
                db.query(self.m.SubscriptionTerm).order_by(self.m.SubscriptionTerm.ends_at.desc()).first().ends_at.isoformat())
        self.apply()


if __name__ == '__main__':
    unittest.main()
