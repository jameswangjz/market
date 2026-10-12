"""Focused repeated version-change coverage over immutable subscription orders."""
import json
import unittest

from sqlalchemy import Boolean, Column, String, Text, select
from fastapi import HTTPException

import test_version_change_quotes as fixtures


class SubscriptionTermRevisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.VersionChangeQuoteTests.setUpClass.__func__(cls)
        Base = cls.m.Base

        class RevisionSagaRequest(Base):
            __tablename__ = 'test_revision_saga_requests'
            id = Column(String(36), primary_key=True)
            combination_id = Column(String(36), nullable=False)
            status = Column(String(30), nullable=False)
            resource_applied = Column(Boolean, nullable=False, default=False)
            evidence_json = Column(Text, nullable=False, default='{}')
            target_snapshots_json = Column(Text, nullable=False, default='{}')

        cls.SagaRequest = RevisionSagaRequest
    tearDownClass = classmethod(fixtures.VersionChangeQuoteTests.tearDownClass.__func__)
    tearDown = fixtures.VersionChangeQuoteTests.tearDown

    def setUp(self):
        fixtures.VersionChangeQuoteTests.setUp(self)
        self.m.Base.metadata.create_all(self.m.engine)
        def assert_no_pending(db, enterprise_id, product_id):
            combo = db.scalar(select(self.m.subscription_terms['Combination']).where(
                self.m.subscription_terms['Combination'].enterprise_id == enterprise_id,
                self.m.subscription_terms['Combination'].product_id == product_id))
            if combo and db.scalar(select(self.SagaRequest.id).where(
                    self.SagaRequest.combination_id == combo.id,
                    self.SagaRequest.status.in_({'pending_payment', 'resource_pending', 'retrying',
                        'compensating', 'refund_pending', 'exception'})).limit(1)):
                raise HTTPException(409, 'pending version change')
        self.m.version_change_saga = {'VersionChangeRequest': self.SagaRequest,
                                      'assert_no_pending': assert_no_pending}
        self.ledger = self.m.version_change_ledger
        self.oid, _ = self.prepare()
        with self.m.SessionLocal() as db:
            db.add(self.m.ProductReleaseVersion(id='v3', product_id='product', version_code='v3',
                price=180, cost=70, status='active', monthly_quota=789))
            db.commit()
        self.m.storefront['versions_for'] = lambda db, product: [
            {'id': 'version'}, {'id': 'v2'}, {'id': 'v3'}]

    prepare = fixtures.VersionChangeQuoteTests.prepare
    quote = fixtures.VersionChangeQuoteTests.quote
    validate = fixtures.VersionChangeQuoteTests.validate
    create = fixtures.VersionChangeQuoteTests.create
    deliver = fixtures.VersionChangeQuoteTests.deliver
    request = fixtures.VersionChangeQuoteTests.request

    def apply_quote(self, target, change_id):
        with self.m.SessionLocal() as db:
            prepared = self.m.version_change_quotes['prepare'](
                db, db.get(self.m.User, 'buyer'), self.oid, target, False, self.m.now())
            rows = self.ledger['stage'](db, change_id, prepared, 'revision-test')
            self.ledger['finalize'](db, change_id, {
                'request_change_id': change_id, 'resource_change_id': 'resource-' + change_id,
                'evidence_fingerprint': (change_id.encode().hex() * 32)[:64]}, 'revision-test')
            combo = prepared['combo']
            self.m.subscription_terms['apply_version_change'](db, combo,
                prepared['internal'][0]['target']['version_id'],
                prepared['public']['target_version_code'], prepared['snapshots_by_term'],
                self.m.now(), change_id)
            db.commit()
            return prepared, rows

    def test_repeated_changes_preserve_orders_calendar_and_quota_scope(self):
        with self.m.SessionLocal() as db:
            orders_before = {order.id: (order.product_version_id, order.product_version_code,
                order.amount, order.total_cost_snapshot, order.economic_snapshot_json,
                order.delivery_snapshot_json) for order in db.scalars(select(self.m.Order)).all()}
            terms_before = {term.id: (term.starts_at, term.ends_at, term.generation, term.start_month)
                for term in db.scalars(select(self.m.subscription_terms['Term'])).all()}
            periods_before = self.m.subscription_terms['quota_periods'](db, 'buyer_tenant', 'product')
        first, _ = self.apply_quote('v2', 'revision-one')
        self.assertEqual(len(first['snapshots_by_term']), 2)
        subscription = self.request('GET', f'/api/orders/{self.oid}/subscription')['item']
        self.assertTrue(subscription['can_version_change'])
        self.assertTrue(subscription['can_renew'])
        self.assertEqual(subscription['version_code'], 'v2')
        mid_quote = self.quote(self.oid, target='v3')
        self.assertEqual([segment['original_amount'] for segment in mid_quote['segments']], [450, 150])
        second, _ = self.apply_quote('v3', 'revision-two')
        self.assertEqual(len(second['snapshots_by_term']), 2)
        with self.m.SessionLocal() as db:
            orders_after = {order.id: (order.product_version_id, order.product_version_code,
                order.amount, order.total_cost_snapshot, order.economic_snapshot_json,
                order.delivery_snapshot_json) for order in db.scalars(select(self.m.Order)).all()}
            terms_after = {term.id: (term.starts_at, term.ends_at, term.generation, term.start_month)
                for term in db.scalars(select(self.m.subscription_terms['Term'])).all()}
            periods_after = self.m.subscription_terms['quota_periods'](db, 'buyer_tenant', 'product')
            revisions = db.scalars(select(self.m.subscription_terms['Revision']).order_by(
                self.m.subscription_terms['Revision'].revision_number)).all()
            self.assertEqual([revision.revision_number for revision in revisions], [1, 1, 2, 2])
            self.assertEqual({revision.version_code for revision in revisions}, {'v2', 'v3'})
        self.assertEqual(orders_after, orders_before)
        self.assertEqual(terms_after, terms_before)
        self.assertEqual([p['period_key'] for p in periods_after], [p['period_key'] for p in periods_before])
        self.assertEqual([p['monthly_quota'] for p in periods_after], [789, 789, 789, 789])

    def test_stale_quote_rejected_after_revision_is_applied(self):
        quote = self.quote(self.oid, target='v2')
        self.apply_quote('v2', 'stale-revision')
        with self.assertRaises(self.m.HTTPException) as caught:
            with self.m.SessionLocal() as db:
                self.m.version_change_quotes['validate'](db, db.get(self.m.User, 'buyer'), self.oid,
                    quote['quote_token'])
        self.assertEqual(caught.exception.status_code, 409)

    def test_api_gateway_revision_and_evidence_are_bound_to_quote_and_each_term(self):
        with self.m.SessionLocal() as db:
            route = self.m.ApiGatewayRoute(id='revision-target-route', product_id='product',
                version='v2', route_key='revision-target-key', upstream_url='https://target.example.invalid',
                status='active', validated_ip='203.0.113.20')
            db.add(route)
            db.flush()
            revision = self.m.GatewayConfigRevision(id='revision-target-config', route_id=route.id,
                product_id='product', revision=2, status='published')
            db.add(revision)
            db.add(self.m.version_gateway['Config'](id='target-gateway-config', product_id='product',
                version_id='v2', route_id=route.id, upstream_url=route.upstream_url,
                route_key=route.route_key, version_code='v2', auth_mode='api_key',
                upstream_auth_mode='oauth2', upstream_scope='resource.invoke', timeout_ms=30000,
                strip_prefix=True, health_method='GET', health_path='/health', rate_limit_per_minute=60,
                daily_quota=10, monthly_quota=456, status='verified', healthy=True,
                evidence_hash='evidence-v1', revision_id=revision.id))
            db.commit()
        quote = self.quote(self.oid, target='v2')
        with self.m.SessionLocal() as db:
            prepared = self.m.version_change_quotes['prepare'](
                db, db.get(self.m.User, 'buyer'), self.oid, 'v2', False, self.m.now())
            contexts = [item['integration'] for item in prepared['snapshots_by_term'].values()]
            self.assertEqual(contexts[0], contexts[1])
            self.assertEqual(contexts[0], {'kind': 'api', 'config_id': 'target-gateway-config',
                'revision_id': 'revision-target-config', 'evidence_hash': 'evidence-v1',
                'route_id': 'revision-target-route', 'route_key': 'revision-target-key',
                'upstream_url': 'https://target.example.invalid',
                'route_upstream_url': 'https://target.example.invalid', 'config_status': 'verified',
                'config_healthy': True, 'route_status': 'active', 'validated_ip': '203.0.113.20',
                'quote_fingerprint': prepared['digest']})
            db.get(self.m.version_gateway['Config'], 'target-gateway-config').evidence_hash = 'evidence-v2'
            db.commit()
        self.validate(self.oid, quote['quote_token'], expected=409)

    def test_apply_replay_is_exact_idempotent_and_rejects_mismatch_or_partial_history(self):
        prepared, _ = self.apply_quote('v2', 'revision-replay')
        terms = self.m.subscription_terms
        with self.m.SessionLocal() as db:
            combo = db.get(terms['Combination'], prepared['public']['combination_id'])
            rows = terms['apply_version_change'](db, combo, 'v2', 'v2',
                prepared['snapshots_by_term'], self.m.now(), 'revision-replay')
            self.assertEqual(len(rows), 2)
            bad = dict(prepared['snapshots_by_term'])
            first_id = next(iter(bad))
            bad[first_id] = dict(bad[first_id], economic={**bad[first_id]['economic'], 'unit_price': '999.00'})
            with self.assertRaises(self.m.HTTPException):
                terms['apply_version_change'](db, combo, 'v2', 'v2', bad,
                    self.m.now(), 'revision-replay')
            with self.assertRaises(self.m.HTTPException):
                terms['apply_version_change'](db, combo, 'v2', 'v2', {first_id: prepared['snapshots_by_term'][first_id]},
                    self.m.now(), 'revision-replay')
            db.rollback()

    def test_resource_verified_revision_keeps_quota_during_exceptional_refund(self):
        with self.m.SessionLocal() as db:
            prepared = self.m.version_change_quotes['prepare'](
                db, db.get(self.m.User, 'buyer'), self.oid, 'v2', False, self.m.now())
            change_id = 'revision-refund-exception'
            self.ledger['stage'](db, change_id, prepared, 'revision-test')
            proof = {'request_change_id': change_id, 'resource_change_id': change_id,
                     'evidence_fingerprint': 'a' * 64}
            db.add(self.SagaRequest(id=change_id, combination_id=prepared['combo'].id,
                status='exception', target_snapshots_json=json.dumps(prepared['snapshots_by_term']),
                evidence_json=json.dumps(proof), resource_applied=True))
            db.flush()
            self.m.subscription_terms['apply_version_change'](db, prepared['combo'], 'v2', 'v2',
                prepared['snapshots_by_term'], self.m.now(), change_id)
            db.commit()
        with self.m.SessionLocal() as db:
            entitlement = self.m.subscription_terms['current_entitlement'](
                db, 'buyer_tenant', 'product', at=self.m.now())
            periods = self.m.subscription_terms['quota_periods'](db, 'buyer_tenant', 'product')
        self.assertEqual(entitlement['limits']['monthly_quota'], 456)
        self.assertEqual([item['monthly_quota'] for item in periods], [456, 456, 456, 456])
        subscription = self.request('GET', f'/api/orders/{self.oid}/subscription')['item']
        self.assertFalse(subscription['can_renew'])
        self.assertFalse(subscription['can_version_change'])


class SaaSIntegrationQuoteTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.VersionChangeQuoteTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.VersionChangeQuoteTests.tearDownClass.__func__)
    tearDown = fixtures.VersionChangeQuoteTests.tearDown
    prepare = fixtures.VersionChangeQuoteTests.prepare
    quote = fixtures.VersionChangeQuoteTests.quote
    validate = fixtures.VersionChangeQuoteTests.validate
    create = fixtures.VersionChangeQuoteTests.create
    deliver = fixtures.VersionChangeQuoteTests.deliver
    request = fixtures.VersionChangeQuoteTests.request

    def setUp(self):
        fixtures.VersionChangeQuoteTests.setUp(self)
        self.oid, _ = self.prepare(method='tenant_access')

    def test_saas_safe_config_fingerprint_is_bound_without_secret_material(self):
        with self.m.SessionLocal() as db:
            config = self.m.SaaSIntegrationConfig(id='target-saas-config', product_id='product',
                base_url='https://saas.example.invalid', operation_path='/isv.php',
                token_url='https://saas.example.invalid/token', client_id='client-public-id',
                client_secret='never-include-this', scope='tenant.manage', auth_mode='oauth2',
                timeout_ms=5000, status='active')
            db.add(config)
            db.commit()
        quote = self.quote(self.oid, target='v2')
        with self.m.SessionLocal() as db:
            prepared = self.m.version_change_quotes['prepare'](
                db, db.get(self.m.User, 'buyer'), self.oid, 'v2', False, self.m.now())
            contexts = [item['integration'] for item in prepared['snapshots_by_term'].values()]
            self.assertEqual(contexts[0], contexts[1])
            self.assertEqual(contexts[0]['kind'], 'saas')
            self.assertEqual(contexts[0]['config_id'], 'target-saas-config')
            self.assertRegex(contexts[0]['config_fingerprint'], r'^[0-9a-f]{64}$')
            self.assertNotIn('never-include-this', json.dumps(prepared['snapshots_by_term']))
            db.get(self.m.SaaSIntegrationConfig, 'target-saas-config').base_url = 'https://changed.example.invalid'
            db.commit()
        self.validate(self.oid, quote['quote_token'], expected=409)


if __name__ == '__main__':
    unittest.main()
