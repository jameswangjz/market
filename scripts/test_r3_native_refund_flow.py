"""Offline safety tests. Runtime API/Redis evidence requires the composer."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, mapped_column, sessionmaker

import r3_native_refund_flow as flow


class Models(DeclarativeBase):
    pass


class Execution(Models):
    __tablename__ = 'execution'
    id = mapped_column(String, primary_key=True)
    refund_id = mapped_column(String)
    status = mapped_column(String)
    lease_expires_at = mapped_column(DateTime(timezone=True))
    resume_status = mapped_column(String)
    error_code = mapped_column(String)
    proof_json = mapped_column(String)


class Order(Models):
    __tablename__ = 'orders'
    id = mapped_column(String, primary_key=True)
    product_id = mapped_column(String)
    buyer_enterprise_id = mapped_column(String)
    refunded_amount = mapped_column(Numeric, default=0)
    order_no = mapped_column(String)


class Refund(Models):
    __tablename__ = 'refunds'
    id = mapped_column(String, primary_key=True)
    order_id = mapped_column(String)
    status = mapped_column(String)
    refund_no = mapped_column(String)


class Case(Models):
    __tablename__ = 'cases'
    refund_id = mapped_column(String, primary_key=True)
    version = mapped_column(Integer)


class Leg(Models):
    __tablename__ = 'legs'
    id = mapped_column(String, primary_key=True)
    refund_id = mapped_column(String)
    status = mapped_column(String)
    receipt = mapped_column(String)
    completed_at = mapped_column(DateTime(timezone=True))


class Completion(Models):
    __tablename__ = 'completions'
    refund_id = mapped_column(String, primary_key=True)


class Recovery(Models):
    __tablename__ = 'recoveries'
    id = mapped_column(String, primary_key=True)
    refund_id = mapped_column(String)


class Settlement(Models):
    __tablename__ = 'settlements'
    id = mapped_column(String, primary_key=True)
    refund_id = mapped_column(String)
    order_id = mapped_column(String)
    is_refund = mapped_column(Integer)
    gross_amount = mapped_column(Numeric)
    cost_amount = mapped_column(Numeric)
    profit_amount = mapped_column(Numeric)
    platform_fee = mapped_column(Numeric)
    provider_share = mapped_column(Numeric)
    service_share = mapped_column(Numeric)
    expert_fee = mapped_column(Numeric)
    channel_fee = mapped_column(Numeric)


class SettlementLink(Models):
    __tablename__ = 'settlement_links'
    refund_id = mapped_column(String, primary_key=True)
    settlement_id = mapped_column(String)


class DeliveryTask(Models):
    __tablename__ = 'tasks'
    id = mapped_column(String, primary_key=True)
    order_id = mapped_column(String)
    status = mapped_column(String)
    next_retry_at = mapped_column(DateTime(timezone=True))


class Combination(Models):
    __tablename__ = 'combos'
    id = mapped_column(String, primary_key=True)
    product_id = mapped_column(String)
    enterprise_id = mapped_column(String)


class Term(Models):
    __tablename__ = 'terms'
    id = mapped_column(String, primary_key=True)
    order_id = mapped_column(String)
    combination_id = mapped_column(String)
    generation = mapped_column(Integer)
    start_month = mapped_column(Integer)
    months = mapped_column(Integer)


class ApiCredential(Models):
    __tablename__ = 'credentials'
    id = mapped_column(String, primary_key=True)
    enterprise_id = mapped_column(String)


class Product(Models):
    __tablename__ = 'products'
    id = mapped_column(String, primary_key=True)
    enterprise_id = mapped_column(String)
    product_type = mapped_column(String)
    delivery_method = mapped_column(String)
    status = mapped_column(String)
    settlement_rule_mode = mapped_column(String)
    settlement_rule_json = mapped_column(String)


class ProductReleaseVersion(Models):
    __tablename__ = 'versions'
    id = mapped_column(String, primary_key=True)
    product_id = mapped_column(String)
    status = mapped_column(String)
    price = mapped_column(Numeric)
    cost = mapped_column(Numeric)
    monthly_quota = mapped_column(Integer)
    daily_quota = mapped_column(Integer)
    rate_limit_per_minute = mapped_column(Integer)


class User(Models):
    __tablename__ = 'users'
    id = mapped_column(String, primary_key=True)
    platform_role = mapped_column(String)


class Membership(Models):
    __tablename__ = 'memberships'
    user_id = mapped_column(String, primary_key=True)


class Preference(Models):
    __tablename__ = 'notification_preferences'
    user_id = mapped_column(String, primary_key=True)
    email_enabled = mapped_column(Boolean, default=True)


class AuditLog(Models):
    __tablename__ = 'audit_logs'
    id = mapped_column(String, primary_key=True)
    action = mapped_column(String)
    order_id = mapped_column(String)
    target_type = mapped_column(String)
    target_id = mapped_column(String)


class OwnedRedis:
    def __init__(self, coverage, combo):
        self.coverage = coverage
        self.hash = {'baseline': '0', 'preserved': 'value'}
        self.combo = combo
        self.writes = []

    def hgetall(self, key):
        if key == self.coverage:
            return dict(self.hash)
        if key == 'market:apisix:credential:consumer':
            return dict(quota_scope=self.combo, subscription_periods=json.dumps([
                dict(period_key=self.combo + ':1:0', monthly_quota=10)]))
        raise AssertionError('Unrelated Redis read')

    def pttl(self, key):
        assert key == self.coverage
        return -1

    def eval(self, script, count, key, expected, baseline):
        assert script == flow.COVERAGE_SWAP and count == 1 and key == self.coverage
        assert self.hash == json.loads(expected)
        self.hash['baseline'] = baseline
        self.writes.append((key, baseline))
        return 1

    def get(self, key):
        assert key == 'market:apisix:quota:subscription:month:' + self.combo + ':' + self.combo + '%3A1%3A0'
        return '2'

    def exists(self, key):
        assert key.startswith(flow.guards.INTENT)
        return 0


class FlowTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine('sqlite://')
        Models.metadata.create_all(engine)
        self.sessions = sessionmaker(bind=engine)
        self.instant = datetime(2026, 10, 15, tzinfo=timezone.utc)
        self.m = SimpleNamespace(engine=engine, SessionLocal=self.sessions,
            refund_execution={'Execution': Execution, 'PaymentLeg': Leg, 'Completion': Completion},
            refund_execution_resources={'Recovery': Recovery}, refund_workflow={'Case': Case},
            Order=Order, Refund=Refund, Settlement=Settlement, User=User, Membership=Membership,
            ProductReleaseVersion=ProductReleaseVersion,
            message_center={'Preference': Preference},
            AuditLog=AuditLog,
            DeliveryTask=DeliveryTask, ApiCredential=ApiCredential, Product=Product,
            subscription_terms={'Term': Term, 'Combination': Combination},
            now=lambda: self.instant, notify_business_event=lambda *a, **kw: None)
        self.ids = flow.base.fixture_ids('qa-vcs-123456abcdef')
        self.manifest = flow.base.fixture_ids(self.ids['run_id'])
        self.state = dict(order_ids=[], refund_ids=[], extra_keys=[])
        self.manifest['database_ids']['native_refund'] = self.state

    def test_new_run_delivery_audit_requires_unique_exact_task_identity(self):
        with self.sessions.begin() as db:
            db.add(AuditLog(id='own', action='create_delivery_task', order_id='own-order',
                            target_type='delivery_task', target_id='own-task'))
            db.add(AuditLog(id='other', action='create_delivery_task', order_id='other-order',
                            target_type='delivery_task', target_id='other-task'))
        self.assertEqual(flow.verify_delivery_audit(self.m, 'own-order', 'own-task'), 1)
        with self.assertRaises(flow.base.AcceptanceFailure):
            flow.verify_delivery_audit(self.m, 'missing-order', 'own-task')
        with self.sessions.begin() as db:
            db.get(AuditLog, 'own').target_id = ''
        with self.assertRaises(flow.base.AcceptanceFailure):
            flow.verify_delivery_audit(self.m, 'own-order', 'own-task')
        with self.sessions.begin() as db:
            db.get(AuditLog, 'own').target_id = 'own-task'
            db.add(AuditLog(id='duplicate', action='create_delivery_task', order_id='own-order',
                            target_type='delivery_task', target_id='own-task'))
        with self.assertRaises(flow.base.AcceptanceFailure):
            flow.verify_delivery_audit(self.m, 'own-order', 'own-task')

    def test_bootstrap_only_activates_used_api_source_and_leaves_foreign_product(self):
        with self.sessions.begin() as db:
            for role, uid in self.ids['users'].items():
                db.add(User(id=uid, platform_role='delivery_monitor' if role == 'delivery' else ''))
            db.add(User(id='real-admin', platform_role='super_admin'))
            db.add(Preference(user_id='real-admin', email_enabled=True))
            db.add(Preference(user_id=self.ids['users']['buyer'], email_enabled=True))
            db.add(Preference(user_id=self.ids['users']['finance'], email_enabled=False))
            for kind in ('api', 'model'):
                db.add(Product(id=self.ids['products'][kind],
                    enterprise_id=self.ids['enterprises']['provider'], status='published',
                    product_type=kind, delivery_method='model_api' if kind == 'model' else 'api'))
                for side in ('source', 'target'):
                    db.add(ProductReleaseVersion(id=self.ids['versions'][kind][side],
                        product_id=self.ids['products'][kind], status='active'))
            db.add(Product(id='foreign', enterprise_id='foreign', status='published'))
        flow.bootstrap(self.m, self.ids)
        with self.sessions() as db:
            for kind in ('api', 'model'):
                product = db.get(Product, self.ids['products'][kind])
                self.assertEqual(product.status, 'published' if kind == 'api' else 'draft')
                self.assertEqual((product.product_type, product.delivery_method), ('api', 'api'))
                for side in ('source', 'target'):
                    version = db.get(ProductReleaseVersion, self.ids['versions'][kind][side])
                    self.assertEqual(version.status,
                        'active' if kind == 'api' and side == 'source' else 'inactive')
                    self.assertEqual((version.price, version.cost, version.monthly_quota,
                        version.daily_quota, version.rate_limit_per_minute), (20, 5, 10, 0, 100))
            self.assertEqual(db.get(Product, 'foreign').status, 'published')
            for uid in self.ids['users'].values():
                self.assertIs(db.get(Preference, uid).email_enabled, False)
            self.assertIs(db.get(Preference, 'real-admin').email_enabled, True)
            self.assertEqual(set(db.scalars(select(Preference.user_id))),
                             set(self.ids['users'].values()) | {'real-admin'})

    def test_bootstrap_rejects_foreign_email_preference_identity(self):
        self.ids['users']['buyer'] = 'real-admin'
        with self.assertRaises(flow.base.AcceptanceFailure):
            flow.bootstrap(self.m, self.ids)
        with self.sessions() as db:
            self.assertEqual(list(db.scalars(select(Preference.user_id))), [])

    def test_worker_claim_and_get_cannot_select_unrelated_execution(self):
        with self.sessions.begin() as db:
            db.add_all([Execution(id='first', refund_id='foreign', status='prepared'),
                        Execution(id='second', refund_id='owned', status='prepared')])
        sessions = flow.scoped_sessions(self.m, 'owned')
        with sessions() as db:
            row = db.scalar(select(Execution).order_by(Execution.id)
                            .with_for_update(skip_locked=True).limit(1))
            self.assertEqual(row.refund_id, 'owned')
            self.assertIsNone(db.get(Execution, 'first'))
            row.status = 'completed'
            db.commit()
        with self.sessions() as db:
            self.assertEqual(db.get(Execution, 'first').status, 'prepared')
            self.assertEqual(db.get(Execution, 'second').status, 'completed')

    def test_term_coverage_journal_precedes_policy_publication_and_hooks_restore(self):
        snapshots = []
        def write(path, manifest):
            snapshots.append(dict(manifest['database_ids']['native_refund']))
        original_notify = self.m.notify_business_event
        with patch.object(flow.base, 'write_manifest', side_effect=write):
            with flow.owned_hooks(self.m, self.ids, 'unused', self.manifest):
                with self.sessions.begin() as db:
                    order = Order(id='a' * 32, product_id=self.ids['products']['api'],
                                  buyer_enterprise_id=self.ids['enterprises']['buyer'])
                    db.add(order)
                    db.flush()
                    combo = Combination(id='b' * 32, product_id=order.product_id,
                                        enterprise_id=order.buyer_enterprise_id)
                    db.add(combo)
                    db.flush()
                    db.add(Term(id='c' * 32, order_id=order.id, combination_id=combo.id,
                                generation=1, start_month=0, months=1))
                    db.flush()
                    coverage = flow.guards.COVERAGE + combo.id + ':' + combo.id + '%3A1%3A0'
                    self.assertIn(coverage, snapshots[-1]['extra_keys'])
                    self.assertEqual(snapshots[-1]['term_id'], 'c' * 32)
                    self.m.notify_business_event(db, '', '', 'order', order.id)
                    product = Product(id=self.ids['products']['api'],
                                      enterprise_id=self.ids['enterprises']['provider'])
                    db.add(product)
                    db.flush()
                    self.m.notify_business_event(db, '', '', 'product', product.id)
                    product.enterprise_id = 'foreign'
                    with self.assertRaises(flow.base.AcceptanceFailure):
                        self.m.notify_business_event(db, '', '', 'product', product.id)
                    with self.assertRaises(flow.base.AcceptanceFailure):
                        self.m.notify_business_event(db, '', '', 'order', 'foreign')
                    task = DeliveryTask(id='task', order_id=order.id, status='in_delivery')
                    db.add(task)
                    db.flush()
                    self.assertEqual(task.next_retry_at, self.instant + flow.timedelta(hours=1))
        self.assertIs(self.m.notify_business_event, original_notify)
        with self.sessions.begin() as db:
            task = DeliveryTask(id='after', order_id='a' * 32, status='in_delivery')
            db.add(task)
            db.flush()
            self.assertIsNone(task.next_retry_at)

    def test_execution_hold_is_owned_and_applies_to_private_worker_sessions(self):
        self.state['refund_ids'].append('owned')
        with patch.object(flow.base, 'write_manifest'):
            with flow.owned_hooks(self.m, self.ids, 'unused', self.manifest):
                # The real base factory uses sqlalchemy.orm.Session, as does
                # this factory; its event listener must reach worker subclasses.
                with flow.scoped_sessions(self.m, 'owned').begin() as db:
                    owned = Execution(id='owned', refund_id='owned', status='prepared')
                    foreign = Execution(id='foreign', refund_id='foreign', status='prepared')
                    db.add_all([owned, foreign])
                    db.flush()
                    self.assertEqual(owned.lease_expires_at, self.instant + flow.timedelta(hours=1))
                    self.assertIsNone(foreign.lease_expires_at)

    def test_native_extension_roundtrips_without_changing_base_redis_keys(self):
        self.state.update(order_id='a' * 32, term_id='b' * 32,
                          extra_keys=[flow.guards.INTENT + 'c' * 32])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            flow.base.write_manifest(path, self.manifest, create=True)
            loaded = flow.base.read_manifest(path)
        self.assertEqual(loaded['database_ids']['native_refund'], self.state)
        self.assertNotIn(self.state['extra_keys'][0], loaded['external']['redis_keys'])

    def test_baseline_drives_two_calls_then_worker_and_enforces_native_429(self):
        from unittest.mock import Mock
        redis = Mock()
        redis.hgetall.return_value = dict(quota_scope='b' * 32, subscription_periods=json.dumps([
            dict(period_key='b' * 32 + ':1:0', monthly_quota=2)]))
        redis.get.return_value = '2'
        self.m.apisix_policy_redis = lambda: redis
        def purchase(*args):
            self.manifest['database_ids']['native_refund'].update(
                combination_id='b' * 32, credential_id='shared', delivery_audit_identity_verified=1)
            return 'mk_private_test_credential'
        with self.sessions.begin() as db:
            db.add(ApiCredential(id='shared', enterprise_id=self.ids['enterprises']['buyer']))
        # The narrow fake here is for final credential verification, while
        # transaction isolation and event hooks are exercised above with SQL.
        credential = SimpleNamespace(status='active', order_id='', route_id='route',
                                     enterprise_id=self.ids['enterprises']['buyer'])
        final_db = Mock()
        final_db.__enter__ = Mock(return_value=final_db)
        final_db.__exit__ = Mock(return_value=False)
        final_db.get.return_value = credential
        def charge(*args):
            invoke = args[-1]
            self.assertEqual(invoke().status_code, 200)
            self.assertEqual(invoke().status_code, 200)
            return 'metadata', 'month'
        def negative(*args):
            self.manifest['database_ids']['native_refund'].update(
                negative_settlement_source='flow_invoked_production_adapter',
                cross_month_coverage_claim=False, report_csv_verified=1)
            return {key: float(value) for key, value in flow.NEGATIVE_AMOUNTS.items()}
        self.manifest['database_ids'].pop('native_refund')
        with patch.object(flow.base, 'write_manifest'), \
                patch.object(flow, 'bootstrap'), \
                patch.object(flow, 'publish', return_value='route'), \
                patch.object(flow, 'purchase', side_effect=purchase), \
                patch.object(flow, 'disable_fixture_sla_logging'), \
                patch.object(flow, 'negative_settlement', side_effect=negative), \
                patch.object(flow, 'charge', side_effect=charge), \
                patch.object(flow, 'refund', return_value=1) as refund, \
                patch.object(self.m, 'SessionLocal', return_value=final_db), \
                patch.object(flow, 'owned_hooks') as hooks, \
                patch('httpx.get', side_effect=[SimpleNamespace(status_code=200),
                    SimpleNamespace(status_code=200), SimpleNamespace(status_code=429)]) as http:
            hooks.return_value.__enter__.return_value = None
            proof = flow.exercise(self.m, self.ids, 'unused', self.manifest, Mock(), {})
        self.assertEqual(proof, dict(charges=2, refunded_amount=16, monthly_quota=2,
            monthly_used=2, next_http_status=429, simulated_payment_legs=1,
            negative_settlement={key: float(value) for key, value in flow.NEGATIVE_AMOUNTS.items()},
            negative_settlement_source='flow_invoked_production_adapter',
            cross_month_coverage_claim=False, report_csv_verified=1,
            delivery_audit_identity_verified=1))
        self.assertEqual(set(proof['negative_settlement']), set(flow.NEGATIVE_AMOUNTS))
        self.assertTrue(all(type(value) is float for value in proof['negative_settlement'].values()))
        self.assertNotIn('shared', json.dumps(proof))
        self.assertNotIn(self.ids['run_id'], json.dumps(proof))
        self.assertEqual(http.call_count, 3)
        self.assertTrue(all(call.kwargs['trust_env'] is False and
                            call.kwargs['follow_redirects'] is False for call in http.call_args_list))
        refund.assert_called_once()
        state = self.manifest['database_ids']['native_refund']
        self.assertEqual(state['fixture_mode'], 'baseline')
        self.assertEqual(state['sla_immutable_facts_policy'], 'preserve_exact_owned_facts')
        self.assertFalse(state['sla_coverage_claim'])
        self.assertTrue(state['local_business_notifications_suppressed'])
        self.assertTrue(state['global_monitors_unmodified'])
        self.assertNotIn('notifications_suppressed', state)
        redis.close.assert_called_once()

    def seed_fault(self):
        combo, rid, oid, tid = 'b' * 32, 'd' * 32, 'a' * 32, 'c' * 32
        coverage = flow.guards.COVERAGE + combo + ':' + combo + '%3A1%3A0'
        self.state.update(combination_id=combo, refund_id=rid, order_id=oid, term_id=tid,
                          generation=1, period_index=0, extra_keys=[coverage], consumer='consumer')
        with self.sessions.begin() as db:
            db.add_all([Order(id=oid, product_id=self.ids['products']['api'],
                             buyer_enterprise_id=self.ids['enterprises']['buyer'], order_no='ORD-owned'),
                Combination(id=combo, product_id=self.ids['products']['api'],
                            enterprise_id=self.ids['enterprises']['buyer']),
                Term(id=tid, order_id=oid, combination_id=combo, generation=1, start_month=0, months=1),
                Refund(id=rid, order_id=oid, status='executing', refund_no='REF-owned'),
                Execution(id='execution', refund_id=rid, status='resources_pending'),
                Leg(id='leg', refund_id=rid, status='prepared'), Case(refund_id=rid, version=4)])
        return OwnedRedis(coverage, combo)

    def test_coverage_fault_restores_exact_owned_hash_even_on_failure(self):
        redis = self.seed_fault()
        snapshots = []
        def save(*args):
            snapshots.append(json.loads(json.dumps(self.state)))
        with patch.object(flow.base, 'write_manifest', side_effect=save):
            with self.assertRaisesRegex(RuntimeError, 'phase failed'):
                with flow.coverage_mismatch(self.m, self.ids, 'unused', self.manifest, redis):
                    self.assertEqual(snapshots[0]['coverage_fault']['original'],
                                     {'baseline': '0', 'preserved': 'value'})
                    self.assertEqual(redis.hash['baseline'], '1')
                    raise RuntimeError('phase failed')
        self.assertEqual(redis.hash, {'baseline': '0', 'preserved': 'value'})
        self.assertEqual(redis.writes, [(redis.coverage, '1'), (redis.coverage, '0')])
        self.assertEqual(self.state['coverage_fault']['status'], 'restored')

    def test_coverage_restores_if_post_injection_manifest_write_fails(self):
        redis = self.seed_fault()
        writes = 0
        def write(*args):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise RuntimeError('journal interrupted')
        with patch.object(flow.base, 'write_manifest', side_effect=write):
            with self.assertRaisesRegex(RuntimeError, 'journal interrupted'):
                with flow.coverage_mismatch(self.m, self.ids, 'unused', self.manifest, redis):
                    self.fail('Fault body entered after manifest failure')
        self.assertEqual(redis.hash, {'baseline': '0', 'preserved': 'value'})
        self.assertEqual(self.state['coverage_fault']['status'], 'restored')

    def test_environment_selects_fault_with_unchanged_six_argument_call(self):
        from unittest.mock import Mock
        self.manifest['database_ids'].pop('native_refund')
        with patch.dict(flow.os.environ, QA_NATIVE_REFUND_MODE='coverage_fault'), \
                patch.object(flow.base, 'write_manifest'), \
                patch.object(flow, 'bootstrap', side_effect=RuntimeError('stop before side effects')):
            with self.assertRaisesRegex(RuntimeError, 'stop before side effects'):
                flow.exercise(self.m, self.ids, 'unused', self.manifest, Mock(), {})
        self.assertEqual(self.manifest['database_ids']['native_refund']['fixture_mode'], 'coverage_fault')

    def test_unknown_environment_mode_fails_before_any_manifest_write(self):
        from unittest.mock import Mock
        self.manifest['database_ids'].pop('native_refund')
        with patch.dict(flow.os.environ, QA_NATIVE_REFUND_MODE='unknown'), \
                patch.object(flow.base, 'write_manifest') as write:
            with self.assertRaises(flow.base.AcceptanceFailure):
                flow.exercise(self.m, self.ids, 'unused', self.manifest, Mock(), {})
        write.assert_not_called()

    def test_local_sla_removal_preserves_native_plugins_and_global_logging(self):
        from copy import deepcopy
        route_key = self.ids['routes']['api']['source']
        self.state['route_key'] = route_key
        self.manifest['external']['route_keys'].append(route_key)
        route = dict(id=route_key, create_time=1, update_time=2,
            labels={'market_product_id': self.ids['products']['api']},
            upstream={'nodes': {'mock:8300': 1}}, plugins={
                'key-auth': {'header': 'X-API-Key'}, 'market-gateway-quota': {'monthly_quota': 10},
                'market-gateway-oauth': {'scope': 'resource.invoke'}, 'market-sla-logger': {}})
        expected = deepcopy(route)
        expected.pop('create_time')
        expected.pop('update_time')
        expected['plugins'].pop('market-sla-logger')
        with patch.object(flow.base, 'write_manifest'), patch.object(flow.base, 'apisix_admin',
                side_effect=[deepcopy(route), {}, deepcopy(expected)]) as admin:
            flow.disable_fixture_sla_logging(self.m, self.ids, 'unused', self.manifest)
        self.assertEqual(admin.call_args_list[1].args,
                         (self.m, 'PUT', '/routes/' + route_key, expected))
        self.assertTrue(self.state['route_local_sla_logger_removed'])
        self.assertTrue(self.state['global_sla_logging_unmodified'])
        self.assertFalse(self.state['sla_coverage_claim'])
        self.assertNotIn('sla_fixture_logging_disabled', self.state)

    def test_fault_ownership_rejected_before_any_redis_mutation(self):
        redis = self.seed_fault()
        with self.sessions.begin() as db:
            db.get(Combination, self.state['combination_id']).enterprise_id = 'foreign'
        with patch.object(flow.base, 'write_manifest'):
            with self.assertRaises(flow.base.AcceptanceFailure):
                with flow.coverage_mismatch(self.m, self.ids, 'unused', self.manifest, redis):
                    self.fail('Foreign scope entered')
        self.assertEqual(redis.writes, [])

    def test_resource_failure_proof_rejects_executed_funds(self):
        redis = self.seed_fault()
        rid = self.state['refund_id']
        with self.sessions.begin() as db:
            execution = db.get(Execution, 'execution')
            execution.status, execution.resume_status = 'failed', 'resources_pending'
            execution.error_code = 'execution_revalidation_failed'
            db.get(Refund, rid).status = 'failed'
        self.assertEqual(flow.verify_resource_failure(self.m, self.manifest, redis), 4)
        with self.sessions.begin() as db:
            db.get(Leg, 'leg').status = 'completed'
        with self.assertRaises(flow.base.AcceptanceFailure):
            flow.verify_resource_failure(self.m, self.manifest, redis)

    def test_fault_retry_restores_before_real_api_and_reuses_body(self):
        from unittest.mock import Mock
        redis = self.seed_fault()
        def process(**kwargs):
            if redis.hash['baseline'] == '1':
                with self.sessions.begin() as db:
                    execution = db.get(Execution, 'execution')
                    execution.status, execution.resume_status = 'failed', 'resources_pending'
                    execution.error_code = 'execution_revalidation_failed'
                    db.get(Refund, self.state['refund_id']).status = 'failed'
            return True
        def request(client, token, method, endpoint, *, body):
            self.assertEqual(redis.hash['baseline'], '0')
            self.assertEqual(endpoint, '/refunds/' + self.state['refund_id'] + '/retry')
            self.assertEqual(body['expected_version'], 4)
            return {'id': self.state['refund_id']}
        worker = Mock(process_once=Mock(side_effect=process))
        with patch.object(flow.base, 'write_manifest'), patch.object(flow.base, 'request', side_effect=request) as http:
            body = flow.fault_and_retry(self.m, self.ids, 'unused', self.manifest, Mock(),
                                       {'delivery': 'token'}, redis, worker, self.sessions)
        self.assertEqual(http.call_count, 2)
        self.assertEqual(http.call_args_list[0].kwargs['body'], body)
        self.assertEqual(http.call_args_list[1].kwargs['body'], body)
        self.assertTrue(self.state['coverage_fault']['failure_before_funds'])
        self.assertEqual(self.state['coverage_fault']['status'], 'restored')

    def test_negative_settlement_exact_db_link_report_and_export(self):
        from unittest.mock import Mock
        import csv
        import io
        self.seed_fault()
        rid, oid = self.state['refund_id'], self.state['order_id']
        with self.sessions.begin() as db:
            db.get(Refund, rid).status = 'completed'
            db.add(Settlement(id='negative', refund_id=rid, order_id=oid, is_refund=1,
                              **flow.NEGATIVE_AMOUNTS))
            db.add(SettlementLink(refund_id=rid, settlement_id='negative'))
        self.m.refund_settlement = dict(Model=SettlementLink,
            negative_settlement=lambda db, refund, actor: db.get(Settlement, 'negative'))
        row = dict(flow.NEGATIVE_AMOUNTS, id='negative', refund_id=rid, order_id=oid,
                   order_no='ORD-owned', refund_no='REF-owned', source_kind='ordinary_refund', is_refund=True,
                   participants=[dict(participant_type=kind, amount=flow.NEGATIVE_AMOUNTS[field])
                                 for kind, field in flow.PARTICIPANT_FIELDS.items()])
        fields = ['order_amount', 'cost_amount', 'profit_amount', '\u5e73\u53f0\u8fd0\u8425\u65b9',
                  '\u6570\u636e/\u670d\u52a1\u63d0\u4f9b\u65b9', '\u6570\u636e\u670d\u52a1\u65b9', '\u4e13\u5bb6', '\u6e20\u9053']
        exported = {column: flow.NEGATIVE_AMOUNTS[key] for column, key in zip(fields, flow.NEGATIVE_AMOUNTS)}
        exported.update({key: row[key] for key in ('refund_id', 'order_id', 'order_no', 'refund_no', 'source_kind')})
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=list(exported))
        writer.writeheader()
        writer.writerow(exported)
        client = Mock()
        client.get.return_value = SimpleNamespace(status_code=200, text=output.getvalue())
        with patch.object(flow.base, 'write_manifest'), patch.object(flow.base, 'request',
                return_value={'items': [{'refund_id': 'unrelated'}, row]}):
            proof = flow.negative_settlement(self.m, 'unused', self.manifest, client, {'delivery': 'token'})
        self.assertEqual(proof['gross_amount'], -16)
        self.assertEqual(proof['platform_fee'], -2.4)
        self.assertEqual(self.state['negative_settlement_adapter_mode'], 'existing/idempotent_adapter')
        self.assertEqual(self.state['negative_settlement_source'], 'preexisting_producer_unasserted')
        self.assertEqual(self.state['negative_settlement_adapter_actor'], 'system')
        self.assertFalse(self.state['cross_month_coverage_claim'])
        self.assertEqual(self.state['report_csv_verified'], 1)

    def test_interrupted_charge_keeps_owned_event_window_for_cleanup(self):
        from unittest.mock import Mock
        combo, oid, term = 'b' * 32, 'a' * 32, 'c' * 32
        self.state.update(combination_id=combo, order_id=oid, term_id=term,
                          credential_id='d' * 32)
        coverage = flow.guards.COVERAGE + combo + ':' + combo + '%3A1%3A0'
        self.state['extra_keys'].append(coverage)
        self.manifest['consumers'].append(dict(id='d' * 32, name='consumer'))
        redis = Mock()
        redis.hgetall.return_value = dict(quota_scope=combo, subscription_periods=json.dumps([
            dict(period_key=combo + ':1:0', monthly_quota=10)]))
        redis.info.return_value = {'run_id': 'f' * 40}
        redis.xrevrange.side_effect = [[('10-0', {})], [('12-0', {})]]
        redis.xrange.return_value = [('11-0', {'order_id': oid}), ('12-0', {'order_id': 'foreign'})]
        invoke = Mock(side_effect=RuntimeError('interrupted'))
        with patch.object(flow.base, 'journal_counter', return_value=['month']), \
                patch.object(flow.base, 'write_manifest'):
            with self.assertRaises(RuntimeError):
                flow.charge(self.m, self.ids, 'unused', self.manifest, redis, invoke)
        self.assertEqual(self.state['stream_start'], '10-0')
        self.assertEqual(self.state['stream_end'], '12-0')
        self.assertEqual(self.state['event_ids'], ['11-0'])
        self.assertEqual(redis.xrange.call_args.kwargs['count'], 200)


if __name__ == '__main__':
    unittest.main()
