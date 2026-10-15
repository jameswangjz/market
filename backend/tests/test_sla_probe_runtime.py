"""Isolated SQLite contracts and mocked transport; no production data/network."""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from types import SimpleNamespace
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from sqlalchemy import Boolean, DateTime, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, mapped_column, sessionmaker

from app import sla_probe_runtime as runtime
from app import enterprise_sla
from app.sla_order_assessment import RULE_KEYS, assess_order_metric
from app.sla_online_facts import ProbeFact, aggregate_probes, expected_probe_slots
from app.sla_probe_collector import IdentitySourceMissing


NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
SECRET = 'isolated-probe-secret-not-a-production-key'


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        class Base(DeclarativeBase):
            pass
        class Order(Base):
            __tablename__ = 'orders'
            id = mapped_column(String, primary_key=True)
            product_id = mapped_column(String)
            product_version_id = mapped_column(String)
            buyer_enterprise_id = mapped_column(String)
            payment_status = mapped_column(String)
            delivery_status = mapped_column(String)
            main_status = mapped_column(String)
            subscription_id = mapped_column(String)
        class Product(Base):
            __tablename__ = 'products'
            id = mapped_column(String, primary_key=True)
            status = mapped_column(String)
        class Contract(Base):
            __tablename__ = 'order_sla_contracts'
            id = mapped_column(String, primary_key=True)
            order_id = mapped_column(String, unique=True)
            paid_at = mapped_column(DateTime(timezone=True))
            verified_at = mapped_column(DateTime(timezone=True))
            source_delivery_task_id = mapped_column(String)
            delivery_eligible = mapped_column(Boolean)
            delivery_method = mapped_column(String)
            snapshot_json = mapped_column(Text)
        class Term(Base):
            __tablename__ = 'subscription_terms'
            id = mapped_column(String, primary_key=True)
            order_id = mapped_column(String)
            status = mapped_column(String)
            version_id = mapped_column(String)
            version_code = mapped_column(String)
            starts_at = mapped_column(DateTime(timezone=True))
            ends_at = mapped_column(DateTime(timezone=True))
        class Route(Base):
            __tablename__ = 'api_gateway_routes'
            id = mapped_column(String, primary_key=True)
            product_id = mapped_column(String)
            version = mapped_column(String)
            route_key = mapped_column(String)
            health_path = mapped_column(String)
            health_method = mapped_column(String)
            status = mapped_column(String)
        self.Order, self.Product, self.Contract, self.Term, self.Route = Order, Product, Contract, Term, Route
        self.request = Mock(return_value=SimpleNamespace(status_code=204, close=Mock()))
        self.adapter_patch = patch.object(runtime, 'validated_http_request', self.request)
        self.adapter_patch.start()
        self.entitlement = {'order_id': 'order', 'term_id': 'term', 'delivery_method': 'api',
            'version_id': 'version', 'version_code': 'v1', 'expires_at': NOW + timedelta(days=1)}
        self.ns = dict(Base=Base, Order=Order, Product=Product, ApiGatewayRoute=Route, now=lambda: NOW,
            order_sla_contracts={'Contract': Contract}, subscription_terms={'Term': Term,
                'current_entitlement': Mock(side_effect=lambda *_a, **_k: self.entitlement)},
            validated_http_request=self.request, application_address_policy={'validate_url': Mock()},
            sla_probe_gateway_origin='https://gateway.example.invalid', sla_probe_secret=lambda: SECRET,
            sla_probe_route_binding=self.binding)
        self.hooks = runtime.install(self.ns)
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        with self.Session.begin() as db:
            db.add_all([Product(id='product', status='published'),
                Order(id='order', product_id='product', product_version_id='version', buyer_enterprise_id='buyer', payment_status='paid',
                      delivery_status='accepted', main_status='completed', subscription_id='subscription'),
                Contract(id='contract', order_id='order', paid_at=NOW-timedelta(days=2),
                    verified_at=NOW-timedelta(days=1), source_delivery_task_id='verified-task',
                    delivery_eligible=True, delivery_method='api'),
                Term(id='term', order_id='order', status='active', version_id='version', version_code='v1',
                    starts_at=NOW-timedelta(days=1), ends_at=NOW+timedelta(days=1)),
                Route(id='route', product_id='product', version='v1', route_key='business-api',
                    health_path='/health', health_method='GET', status='active')])

    def tearDown(self):
        self.adapter_patch.stop()
        self.engine.dispose()

    def binding(self, _db, route, version_id):
        return dict(enabled=True, verified=True, quota_isolated=True, route_id=route.id,
            product_id=route.product_id, version_id=version_id, route_key=route.route_key,
            probe_path=f'/gateway/{route.route_key}{route.health_path}', method=route.health_method,
            source_cidrs=['10.244.0.0/16'], config_version='installed-config-1')

    def change(self, model, key, **values):
        with self.Session.begin() as db:
            row = db.get(model, key)
            for name, value in values.items():
                setattr(row, name, value)

    def collect(self):
        with self.Session.begin() as db:
            return self.hooks['collect_one'](db, 'contract', instant=NOW)

    def row(self):
        with self.Session() as db:
            return db.scalar(select(self.hooks['Slot']))

    def add_second_order(self):
        with self.Session.begin() as db:
            db.add_all([self.Order(id='order2', product_id='product', product_version_id='version',
                buyer_enterprise_id='buyer2', payment_status='paid', delivery_status='accepted',
                main_status='completed', subscription_id='subscription2'),
                self.Contract(id='contract2', order_id='order2', paid_at=NOW-timedelta(days=2),
                    verified_at=NOW-timedelta(hours=12), source_delivery_task_id='verified-task2',
                    delivery_eligible=True, delivery_method='api'),
                self.Term(id='term2', order_id='order2', status='active', version_id='version', version_code='v1',
                    starts_at=NOW-timedelta(hours=12), ends_at=NOW+timedelta(days=1))])
        second = {**self.entitlement, 'order_id': 'order2', 'term_id': 'term2'}
        self.ns['subscription_terms']['current_entitlement'].side_effect = lambda _db, buyer, _product, **kw: (
            self.entitlement if buyer == 'buyer' else second)
        return second

    def clone_slot(self, **changes):
        Slot = self.hooks['Slot']
        row = self.row()
        values = {column.name: getattr(row, column.name) for column in Slot.__table__.columns if column.name != 'id'}
        values.update(changes)
        with self.Session.begin() as db:
            db.add(Slot(**values))

    def test_two_paid_buyers_one_gateway_entry_per_slot_collect_exactly_once(self):
        self.add_second_order()
        self.assertEqual(self.collect()['created'], 1)
        with self.Session.begin() as db:
            second = self.hooks['collect_one'](db, 'contract2', instant=NOW)
        self.assertEqual(second, {'created': 0, 'duplicates': 1})
        self.request.assert_called_once()
        self.assertEqual(self.row().entry_id, 'gateway:business-api')
        with self.Session() as db:
            for order_id in ['order', 'order2']:
                rows = list(db.scalars(self.hooks['slot_query'](order_id=order_id,
                    period_start=NOW, period_end=NOW+timedelta(minutes=5))))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].contract_evidence_id, 'contract')

    def test_shared_query_is_readonly_scoped_and_deduplicates_legacy_rows(self):
        self.add_second_order(); self.collect()
        self.clone_slot(entry_id='order:order', status='failed', reason_code='legacy-failure')
        self.clone_slot(entry_id='gateway:other-route', status='failed')
        self.clone_slot(version_id='other-version', status='failed')
        self.clone_slot(product_id='other-product', status='failed')
        future = NOW+timedelta(minutes=5)
        self.clone_slot(entry_id='order:order', slot=int(future.timestamp())//300,
                        scheduled_at=future, observed_at=future, completed_at=future)
        self.ns['sla_probe_route_binding'] = Mock(side_effect=AssertionError('read query must not provision'))
        self.ns['subscription_terms']['current_entitlement'] = Mock(side_effect=AssertionError('read query must not resolve'))
        before = self.request.call_count
        with self.Session() as db:
            first = list(db.scalars(self.hooks['query'](db, db.get(self.Order, 'order'),
                period_start=NOW, period_end=future+timedelta(minutes=5))))
            second = list(db.scalars(self.hooks['slot_query'](order_id='order2',
                period_start=NOW, period_end=future+timedelta(minutes=5))))
            self.assertEqual([row.entry_id for row in first], ['gateway:business-api', 'order:order'])
            self.assertEqual(first[0].status, 'healthy')
            self.assertEqual(len(second), 1)
            self.assertEqual(second[0].id, first[0].id)
        self.assertEqual(self.request.call_count, before)

    def test_shared_query_clips_other_orders_service_start_and_rejects_ambiguous_routes(self):
        self.add_second_order(); self.collect()
        self.change(self.Contract, 'contract2', verified_at=NOW+timedelta(seconds=1))
        with self.Session() as db:
            self.assertEqual(list(db.scalars(self.hooks['slot_query'](order_id='order2',
                period_start=NOW-timedelta(hours=1), period_end=NOW+timedelta(minutes=5)))), [])
        with self.Session.begin() as db:
            db.add(self.Route(id='ambiguous', product_id='product', version='v1', route_key='other',
                health_path='/health', health_method='GET', status='active'))
        with self.Session() as db:
            self.assertEqual(list(db.scalars(self.hooks['slot_query'](order_id='order',
                period_start=NOW, period_end=NOW+timedelta(minutes=5)))), [])

    def test_saas_two_tenants_remain_separate_and_do_not_share_other_contract_rows(self):
        second = self.add_second_order()
        for cid in ['contract', 'contract2']:
            self.change(self.Contract, cid, delivery_method='tenant_access')
        self.entitlement['delivery_method'] = second['delivery_method'] = 'tenant_access'
        self.ns['sla_saas_business_probe_source'] = lambda db, order, ent: runtime.SaaSProbeSource(
            'product', 'version', order.subscription_id,
            f'https://saas.example.invalid/business/{order.subscription_id}/status',
            'authorized-source', 'r1', 'sla-probe:robot', lambda *_: {'Authorization': 'Bearer isolated'})
        self.assertEqual(self.collect()['healthy'], 1)
        with self.Session.begin() as db:
            self.assertEqual(self.hooks['collect_one'](db, 'contract2', instant=NOW)['healthy'], 1)
        self.assertEqual(self.request.call_count, 2)
        with self.Session() as db:
            for order_id, cid in [('order', 'contract'), ('order2', 'contract2')]:
                rows = list(db.scalars(self.hooks['slot_query'](order_id=order_id,
                    period_start=NOW, period_end=NOW+timedelta(minutes=5))))
                self.assertEqual([(row.entry_id, row.contract_evidence_id) for row in rows], [('order:'+order_id, cid)])

    def test_same_hundred_shared_slots_are_assessed_against_two_orders_frozen_thresholds(self):
        self.add_second_order()
        def digest(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with self.Session.begin() as db:
            for cid, buyer, tier in [('contract', 'buyer', 'standard'), ('contract2', 'buyer2', 'strategic')]:
                metrics, calendar = enterprise_sla._promise(tier)
                rules = dict(matrix_version=enterprise_sla.MATRIX_VERSION, service_level_code=tier,
                    calendar=calendar, metrics=metrics, legacy_level_config=None)
                snapshot = dict(schema_version=1, **rules, enterprise_id=buyer, frozen_at=NOW.isoformat(),
                    calendar_content_hash=digest(calendar), rule_version=digest(rules), contract_rule_version=digest(rules))
                db.get(self.Contract, cid).snapshot_json = json.dumps(snapshot)
        for index in range(100):
            at = NOW+timedelta(minutes=5*index)
            self.ns['now'] = lambda: at
            self.request.return_value.status_code = 503 if index < 3 else 204
            with self.Session.begin() as db:
                self.assertEqual(self.hooks['collect_one'](db, 'contract', instant=at)['created'], 1)
                self.assertEqual(self.hooks['collect_one'](db, 'contract2', instant=at)['duplicates'], 1)
        end = NOW+timedelta(minutes=500)
        assessed = []
        self.ns['sla_probe_route_binding'] = Mock(side_effect=AssertionError('assessment must not provision'))
        with self.Session() as db:
            identities = []
            for order_id, cid, buyer in [('order', 'contract', 'buyer'), ('order2', 'contract2', 'buyer2')]:
                rows = list(db.scalars(self.hooks['slot_query'](order_id=order_id, period_start=NOW, period_end=end)))
                identities.append([row.id for row in rows])
                observed = aggregate_probes([ProbeFact(row.slot, row.status) for row in rows],
                    expected_slots=expected_probe_slots(NOW, end), source_available=True)
                self.assertEqual((observed.value, observed.sample_count), (97, 100))
                assessed.append(assess_order_metric(json.loads(db.get(self.Contract, cid).snapshot_json),
                    'availability', observed, buyer_enterprise_id=buyer, source_available=True))
            self.assertEqual(identities[0], identities[1])
        self.assertEqual([(item.status, item.target) for item in assessed], [('met', 90), ('breached', 98)])
        self.assertEqual(self.request.call_count, 100)

    def test_verified_contract_resolves_actual_gateway_not_upstream_or_buyer_key(self):
        self.assertEqual(self.collect()['healthy'], 1)
        args, kwargs = self.request.call_args
        self.assertEqual(args, ('GET', 'https://gateway.example.invalid/gateway/business-api/health'))
        self.assertEqual(kwargs['timeout'], 5)
        self.assertFalse(kwargs['follow_redirects'])
        self.assertEqual(kwargs['headers'], {**runtime.probe_headers(SECRET, route_id='route', product_id='product',
            version_id='version', method='GET', path='/gateway/business-api/health', instant=NOW),
            'X-API-Key': runtime.probe_consumer_key(SECRET, 'route', 'product', 'version')})
        self.assertNotIn('Authorization', kwargs['headers'])
        row = self.row()
        self.assertEqual(row.entry_id, 'gateway:business-api')
        self.assertEqual(row.contract_evidence_id, 'contract')
        self.assertEqual(row.publication_evidence_id, 'delivery:verified-task')
        self.assertEqual(row.eligibility_evidence_id, 'term:term')
        self.assertNotIn(SECRET, row.config_summary_json)
        self.assertNotIn('Signature', row.config_summary_json)

    def test_missing_live_binding_is_persisted_source_missing_not_fake_health(self):
        self.ns.pop('sla_probe_route_binding')
        self.assertEqual(self.collect()['source_missing'], 1)
        self.assertEqual(self.row().status, 'source_missing')
        self.request.assert_not_called()

    def test_manual_withdrawal_still_probes_active_sold_contract(self):
        self.change(self.Product, 'product', status='draft')
        self.assertEqual(self.collect()['healthy'], 1)

    def test_security_withdrawal_is_failed_slot_not_excluded_or_management_health(self):
        self.change(self.Product, 'product', status='security_unpublished')
        self.ns.pop('sla_probe_route_binding')
        result = self.collect()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['skipped'], 0)
        self.assertEqual(result['source_missing'], 0)
        self.assertEqual(self.row().reason_code, 'security_withdrawal')
        self.assertEqual(self.row().responsibility, 'unknown')
        self.request.assert_not_called()

    def test_unpaid_unverified_or_inactive_term_not_enrolled(self):
        for model, changes in [(self.Order, {'payment_status': 'unpaid'}),
                (self.Order, {'delivery_status': 'failed'}), (self.Order, {'main_status': 'fulfilling'}),
                (self.Contract, {'verified_at': None}), (self.Contract, {'source_delivery_task_id': None}),
                (self.Contract, {'delivery_eligible': False}), (self.Term, {'status': 'revoked'})]:
            key = 'order' if model is self.Order else 'contract' if model is self.Contract else 'term'
            with self.Session() as db:
                before = {name: getattr(db.get(model, key), name) for name in changes}
            self.change(model, key, **changes)
            self.assertEqual(self.collect(), {'created': 0, 'skipped': 1})
            self.change(model, key, **before)
        self.request.assert_not_called()

    def test_entitlement_required_and_future_expired_intervals_excluded(self):
        for changes in [None, {**self.entitlement, 'order_id': 'other'},
                {**self.entitlement, 'expires_at': NOW}, {**self.entitlement, 'version_id': 'other'}]:
            old, self.entitlement = self.entitlement, changes
            self.assertEqual(self.collect(), {'created': 0, 'skipped': 1})
            self.entitlement = old
        self.change(self.Term, 'term', starts_at=NOW+timedelta(seconds=1))
        self.assertEqual(self.collect()['skipped'], 1)
        self.request.assert_not_called()

    def test_binding_route_scope_quota_identity_and_unsafe_url_fail_closed(self):
        good = self.binding
        for field, bad in [('enabled', False), ('verified', False), ('quota_isolated', False),
                ('route_id', 'other'), ('product_id', 'other'), ('version_id', 'other'),
                ('probe_path', '/admin/health'), ('method', 'POST'), ('source_cidrs', [])]:
            self.ns['sla_probe_route_binding'] = lambda *a, **k: {**good(*a, **k), field: bad}
            with self.Session() as db:
                self.assertIsNone(self.hooks['resolve'](db, 'contract', instant=NOW).url)
        self.ns['sla_probe_route_binding'] = good
        for path in ['/health?secret=bad', '//evil.invalid', '/health#bad']:
            self.change(self.Route, 'route', health_path=path)
            with self.Session() as db:
                self.assertIsNone(self.hooks['resolve'](db, 'contract', instant=NOW).url)
        self.request.assert_not_called()

    def test_missing_or_short_probe_secret_does_not_fall_back_to_buyer_credentials(self):
        self.ns['sla_probe_secret'] = lambda: ''
        self.assertEqual(self.collect()['source_missing'], 1)
        self.assertEqual(self.row().reason_code, 'business_identity_source_missing')
        self.request.assert_not_called()

    def test_saas_management_config_is_never_used_without_authorized_business_source(self):
        self.change(self.Contract, 'contract', delivery_method='tenant_access')
        self.entitlement['delivery_method'] = 'tenant_access'
        self.ns['saas_access_token'] = Mock(side_effect=AssertionError('management identity forbidden'))
        self.assertEqual(self.collect()['source_missing'], 1)
        self.request.assert_not_called()
        self.ns['saas_access_token'].assert_not_called()

    def test_saas_explicit_bound_source_uses_only_lazy_service_identity(self):
        self.change(self.Contract, 'contract', delivery_method='tenant_access')
        self.entitlement['delivery_method'] = 'tenant_access'
        source = runtime.SaaSProbeSource('product', 'version', 'subscription',
            'https://saas.example.invalid/business/status', 'authorized-source', 'r1', 'sla-probe:robot',
            lambda db, target: {'Authorization': 'Bearer isolated-robot-token'})
        self.ns['sla_saas_business_probe_source'] = Mock(return_value=source)
        self.assertEqual(self.collect()['healthy'], 1)
        self.assertNotIn('isolated-robot-token', self.row().config_summary_json)
        self.assertEqual(self.request.call_args.args[1], source.url)

    def test_duplicate_slot_and_half_open_order_query_keep_original_facts(self):
        self.collect()
        self.assertEqual(self.collect()['duplicates'], 1)
        self.request.assert_called_once()
        with self.Session() as db:
            rows = list(db.scalars(self.hooks['slot_query'](order_id='order', period_start=NOW,
                period_end=NOW+timedelta(minutes=5))))
            self.assertEqual(len(rows), 1)
            self.assertEqual(list(db.scalars(self.hooks['slot_query'](order_id='other', period_start=NOW,
                period_end=NOW+timedelta(minutes=5)))), [])
            self.assertEqual(self.hooks['page'](db, after_id='', limit=1), ['contract'])
            self.assertEqual(self.hooks['page'](db, after_id='contract'), [])

    def test_resolver_and_collector_do_not_commit_and_outer_rollback_removes_fact(self):
        # Start an explicit SQLite root write before the collector savepoint.
        with self.Session() as db:
            db.get(self.Product, 'product').status = 'draft'
            db.flush()
            self.hooks['collect_one'](db, 'contract', instant=NOW)
            db.rollback()
        self.assertIsNone(self.row())

    def test_installer_rejects_unbounded_adapter_and_is_idempotent(self):
        self.assertIs(runtime.install(self.ns), self.hooks)
        other = dict(self.ns); other.pop('sla_probe_runtime'); other['validated_http_request'] = lambda *_: None
        with self.assertRaises(ValueError):
            runtime.install(other)

    def gateway_control_plane(self):
        name = runtime.probe_consumer_name('route', 'product', 'version')
        labels = {'market_sla_probe_enabled': 'true', 'market_sla_probe_path': '/gateway/business-api/health',
            'market_sla_probe_method': 'GET', 'market_sla_probe_route_id': 'route', 'market_version_id': 'version',
            'market_sla_probe_consumer': name, 'market_sla_probe_source_cidrs': '10.244.0.0/16',
            'market_sla_probe_config_version': 'installed-v1'}
        payload = {'uri': '/gateway/business-api/*', 'methods': ['GET', 'HEAD'], 'labels': labels,
            'upstream': {'nodes': {'business.example.invalid:443': 1}, 'scheme': 'https'},
            'plugins': {'key-auth': {'header': 'X-API-Key'}, 'limit-count': {'key': 'consumer_name'},
                'market-gateway-quota': {'route_key': 'business-api'},
                'market-gateway-oauth': {'client_id': 'upstream-service', 'scope': 'resource.invoke'}}}
        actual = deepcopy(payload)
        consumer = {}
        def admin(method, path, body=None):
            if path == '/global_rules/market-sla-terminal' and method == 'GET':
                return {'node': {'value': {'plugins': {'market-sla-logger': {}}}}}
            if path == '/routes/business-api' and method == 'GET':
                return {'node': {'value': actual}}
            if path == '/consumers/'+name and method == 'PUT':
                consumer.update(deepcopy(body)); return {'value': body}
            if path == '/consumers/'+name and method == 'GET':
                return {'node': {'value': consumer}}
            raise AssertionError('unexpected control-plane scope')
        self.ns.update(ApiCredential=object, apisix_native_auth=lambda: True,
            apisix_route_payload=Mock(return_value=payload), apisix_admin_request=Mock(side_effect=admin),
            sla_probe_route_binding=self.hooks['verify_gateway_binding'])
        return actual, name

    def test_actual_default_binding_verifies_route_and_provisions_independent_standard_key_auth(self):
        actual, name = self.gateway_control_plane()
        with patch.dict('os.environ', {'SLA_PROBE_SOURCE_CIDRS': '10.244.0.0/16'}):
            self.assertEqual(self.collect()['healthy'], 1)
        puts = [call for call in self.ns['apisix_admin_request'].call_args_list if call.args[0] == 'PUT']
        self.assertTrue(puts)
        self.assertTrue(all(call.args[1] == '/consumers/'+name for call in puts))
        key = puts[0].args[2]['plugins']['key-auth']['key']
        self.assertEqual(key, runtime.probe_consumer_key(SECRET, 'route', 'product', 'version'))
        self.assertEqual(self.request.call_args.kwargs['headers']['X-API-Key'], key)
        self.assertNotEqual(key, SECRET)
        self.assertIn('market-gateway-oauth', actual['plugins'])
        self.assertNotIn(key, self.row().config_summary_json)
        before = self.ns['apisix_admin_request'].call_count
        self.assertEqual(self.collect()['duplicates'], 1)
        self.assertEqual(self.ns['apisix_admin_request'].call_count, before)

    def test_actual_default_binding_refuses_unbound_path_or_auth_oauth_drift_before_consumer_write(self):
        mutations = [lambda a: a['labels'].update(market_sla_probe_enabled='false'),
            lambda a: a['labels'].update(market_sla_probe_path='/admin/health'),
            lambda a: a['labels'].update(market_version_id='other'),
            lambda a: a['labels'].update(market_sla_probe_consumer='buyer-consumer'),
            lambda a: a['plugins']['key-auth'].update(anonymous=True),
            lambda a: a['plugins']['market-gateway-oauth'].update(scope='management'),
            lambda a: a['upstream'].update(nodes={'management.invalid:443': 1}),
            lambda a: a.update(status=0)]
        for mutate in mutations:
            actual, _ = self.gateway_control_plane(); mutate(actual)
            with patch.dict('os.environ', {'SLA_PROBE_SOURCE_CIDRS': '10.244.0.0/16'}), self.Session() as db:
                self.assertIsNone(self.hooks['verify_gateway_binding'](db, db.get(self.Route, 'route'), 'version'))
            self.assertFalse(any(call.args[0] == 'PUT' for call in self.ns['apisix_admin_request'].call_args_list))

    def test_actual_default_binding_missing_secret_or_source_allowlist_never_provisions(self):
        for env, secret in [({'SLA_PROBE_SOURCE_CIDRS': ''}, SECRET),
                ({'SLA_PROBE_SOURCE_CIDRS': '0.0.0.0/0'}, SECRET),
                ({'SLA_PROBE_SOURCE_CIDRS': '10.244.0.0/16'}, '')]:
            self.gateway_control_plane(); self.ns['sla_probe_secret'] = lambda: secret
            with patch.dict('os.environ', env), self.Session() as db:
                self.assertIsNone(self.hooks['verify_gateway_binding'](db, db.get(self.Route, 'route'), 'version'))
            self.ns['apisix_admin_request'].assert_not_called()


class SignatureTests(unittest.TestCase):
    def test_consumer_identity_is_route_product_version_bound_and_separate_from_master(self):
        name = runtime.probe_consumer_name('r', 'p', 'v')
        self.assertTrue(name.startswith('market-sla-probe-'))
        key = runtime.probe_consumer_key(SECRET, 'r', 'p', 'v')
        self.assertEqual(key, hmac.new(SECRET.encode(), b'sla-consumer-v1\nr\np\nv', hashlib.sha256).hexdigest())
        self.assertNotEqual(runtime.probe_consumer_key(SECRET, 'r2', 'p', 'v'), key)
        self.assertNotEqual(runtime.probe_consumer_name('r2', 'p', 'v'), name)

    def test_exact_signature_contract_and_binding_changes(self):
        values = dict(route_id='r', product_id='p', version_id='v', method='HEAD', path='/gateway/key/health', instant=NOW)
        result = runtime.probe_headers(SECRET, **values)
        message = f'sla-probe-v1\nr\np\nv\nHEAD\n/gateway/key/health\n{int(NOW.timestamp())}'
        expected = hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(result['X-Market-SLA-Signature'], expected)
        for key, value in [('route_id', 'other'), ('product_id', 'other'), ('version_id', 'other'),
                ('method', 'GET'), ('path', '/gateway/other/health'), ('instant', NOW+timedelta(seconds=1))]:
            self.assertNotEqual(runtime.probe_headers(SECRET, **{**values, key: value})['X-Market-SLA-Signature'], expected)
        for secret in ['', 'short', 'x'*32+'\n']:
            with self.assertRaises(IdentitySourceMissing):
                runtime.probe_headers(secret, **values)


if __name__ == '__main__':
    unittest.main()
