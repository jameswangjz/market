"""Production HTTP security and projection tests for provider reconciliation."""
import unittest
import importlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import event, select

import test_subscription_terms as fixture


class ProviderEntitlementsTests(unittest.TestCase):
    setUpClass = classmethod(fixture.SubscriptionTermsTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.SubscriptionTermsTests.tearDownClass.__func__)
    tearDown = fixture.SubscriptionTermsTests.tearDown
    request = fixture.SubscriptionTermsTests.request
    create = fixture.SubscriptionTermsTests.create
    deliver = fixture.SubscriptionTermsTests.deliver

    def setUp(self):
        fixture.SubscriptionTermsTests.setUp(self)
        self.m.now.return_value = datetime.now(timezone.utc)
        self.path = '/api/provider/products/product/entitlements'
        with self.m.SessionLocal() as db:
            db.add(self.m.OAuthClient(id='machine', product_id='product', client_id='provider-client',
                client_secret='test-secret', scope='resource.invoke saas.tenant', status='active'))
            db.commit()

    def mint(self, scope='resource.invoke'):
        response = self.client.post('/oauth/token', data={'grant_type': 'client_credentials',
            'client_id': 'provider-client', 'client_secret': 'test-secret', 'scope': scope})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['access_token']

    def machine(self, token, expected=200, path=None):
        response = self.client.get(path or self.path, headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    def test_user_security_and_active_owner_membership(self):
        self.assertEqual(self.client.get(self.path).status_code, 401)
        for actor in ('buyer', 'buyer_admin', 'outsider', 'finance', 'reviewer', 'quality'):
            self.request('GET', self.path, actor, 403)
        for actor in ('provider', 'platform', 'ops'):
            self.request('GET', self.path, actor)
        self.request('GET', self.path, 'disabled_ops', 401)
        with self.m.SessionLocal() as db:
            member = db.scalar(select(self.m.Membership).where(self.m.Membership.user_id == 'provider'))
            member.role = 'member'
            db.commit()
        self.request('GET', self.path, 'provider', 403)
        with self.m.SessionLocal() as db:
            member = db.scalar(select(self.m.Membership).where(self.m.Membership.user_id == 'provider'))
            member.role, member.business_roles = 'enterprise_admin', 'user'
            db.commit()
        self.request('GET', self.path, 'provider', 403)
        with self.m.SessionLocal() as db:
            member = db.scalar(select(self.m.Membership).where(self.m.Membership.user_id == 'provider'))
            member.business_roles, member.status = 'provider', 'disabled'
            db.commit()
        self.request('GET', self.path, 'provider', 403)

    def test_internal_machine_tokens_and_buyer_keys_are_never_provider_credentials(self):
        token = self.mint()
        self.machine(token, 401)
        self.machine(token, 401, '/api/provider/products/foreign/entitlements')
        self.machine('buyer-api-key', 401)
        self.machine(self.mint('saas.tenant'), 401)
        self.assertEqual(self.client.get(self.path, headers={'X-API-Key': 'buyer-api-key'}).status_code, 401)
        response = self.client.post('/oauth/revoke', data={'client_id': 'provider-client',
            'client_secret': 'test-secret', 'token': token})
        self.assertEqual(response.status_code, 200)
        self.machine(token, 401)
        token = self.mint()
        with self.m.SessionLocal() as db:
            db.get(self.m.OAuthClient, 'machine').status = 'revoked'
            db.commit()
        self.machine(token, 401)

    def test_history_projection_renewal_versions_and_no_writes(self):
        first = self.create()
        one = self.deliver(first)
        second = self.create()
        two = self.deliver(second, fixture.instant(2024, 2, 1))
        pending = self.create(delivered=False)
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, 'product').status = 'unpublished'
            term = db.scalar(select(self.T).where(self.T.order_id == first))
            term.version_id, term.version_code = 'v2', 'v2'
            order = db.get(self.m.Order, first)
            order.economic_snapshot_json = '{"secret":"PRIVATE"}'
            order.delivery_snapshot_json = '{"secret":"PRIVATE"}'
            db.commit()
        statements = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement.strip().split()[0].upper())
        event.listen(self.m.engine, 'before_cursor_execute', capture)
        try:
            payload = self.request('GET', self.path, 'provider')
        finally:
            event.remove(self.m.engine, 'before_cursor_execute', capture)
        self.assertFalse(set(statements) & {'INSERT', 'UPDATE', 'DELETE'})
        self.assertEqual(payload['total'], 3)
        rows = {row['order_id']: row for row in payload['items']}
        self.assertEqual(datetime.fromisoformat(rows[first]['starts_at']), one['starts_at'])
        self.assertEqual(datetime.fromisoformat(rows[first]['ends_at']), one['ends_at'])
        self.assertEqual(rows[first]['version_code'], 'v2')
        self.assertEqual(rows[first]['original_version_code'], 'v1')
        self.assertEqual(datetime.fromisoformat(rows[first]['combination_expires_at']), two['ends_at'])
        self.assertEqual(rows[first]['recorded_terms'][0]['version_code'], 'v2')
        self.assertIsNone(rows[pending]['starts_at'])
        self.assertEqual(rows[first]['reconciliation_context']['schema_version'], 1)
        text = str(payload)
        for forbidden in ('PRIVATE', 'cost', 'buyer_user', 'client_secret', 'password', 'email', 'phone'):
            self.assertNotIn(forbidden, text)

    def test_bounded_pagination_filters_and_strict_order_ownership(self):
        first = self.create()
        self.deliver(first)
        second = self.create()
        self.deliver(second, fixture.instant(2024, 2, 1))
        foreign = self.create()
        with self.m.SessionLocal() as db:
            db.get(self.m.Order, foreign).provider_enterprise_id = 'other_tenant'
            db.commit()
        page = self.request('GET', self.path + '?page_size=1', 'provider')
        self.assertEqual(page['total'], 2)
        self.assertTrue(page['has_more'])
        next_page = self.request('GET', self.path + '?page_size=1&page=2', 'provider')
        self.assertNotEqual(page['items'][0]['order_id'], next_page['items'][0]['order_id'])
        for query in ('page=0', 'page_size=101', 'page_size=0', 'page=100001',
                      'starts_before=2024-02-01T00:00:00'):
            self.request('GET', self.path + '?' + query, 'provider', 422)
        result = self.request('GET', self.path + '?ends_after=2024-02-29T04:00:00Z', 'provider')
        self.assertEqual([row['order_id'] for row in result['items']], [second])
        self.assertEqual(self.request('GET', self.path + '?enterprise_id=other_tenant', 'provider')['total'], 0)
        self.assertEqual(self.request('GET', self.path + '?order_id=' + foreign, 'provider')['total'], 0)

    def test_machine_saas_scope_and_expired_forged_tokens(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, 'product').product_type = 'saas'
            db.commit()
        self.machine(self.mint('resource.invoke'), 401)
        token = self.mint('saas.tenant')
        self.machine(token, 401)
        claims = self.m.jwt.decode(token, options={'verify_signature': False})
        for key, value in (('exp', 1), ('aud', 'other'), ('iss', 'other'), ('sub', 'buyer')):
            forged = self.m.jwt.encode(dict(claims, **{key: value}), self.m.JWT_SECRET, algorithm='HS256')
            self.machine(forged, 401)

    def test_refund_recovery_empty_segments_do_not_restore_original_term(self):
        order_id = self.create()
        self.deliver(order_id)
        with patch.dict(self.m.refund_execution_resources, {
                'entitlement': lambda db, order, term: {'periods': [], 'payment_compatible': True}}):
            response = self.request('GET', self.path, 'provider')
        row = response['items'][0]
        self.assertEqual(row['order_id'], order_id)
        self.assertEqual(row['quota_periods'], [])
        self.assertEqual(row['effective_rights']['periods'], [])
        self.assertIsNone(row['combination_expires_at'])
        self.assertEqual(row['segments'], [])
        self.assertEqual(row['segments_semantics'], 'effective_entitlement_intervals_not_financial_ledger')
        self.assertEqual(row['date_semantics'], 'recorded_term_bounds')
        self.assertIsNotNone(row['starts_at'])
        self.assertEqual(self.client.get(self.path, headers={
            'Authorization': 'Bearer ' + self.tokens['provider']}).headers['cache-control'], 'no-store')

    def test_provider_user_and_enterprise_must_be_verified(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.User, 'provider').verified_status = 'pending'
            db.commit()
        self.request('GET', self.path, 'provider', 403)
        with self.m.SessionLocal() as db:
            db.get(self.m.User, 'provider').verified_status = 'verified'
            db.get(self.m.Enterprise, 'provider_tenant').verification_status = 'pending'
            db.commit()
        self.request('GET', self.path, 'provider', 403)

    def persist_recovery(self, order_id, cutoff):
        """Persist hash-linked recovery evidence consumed by the real reader."""
        module = importlib.import_module(self.m.__package__ + '.refund_execution_resources')
        canonical, digest = module.canonical, module.digest
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, order_id)
            term = db.scalar(select(self.T).where(self.T.order_id == order_id))
            periods = self.hooks['quota_periods'](db, order.buyer_enterprise_id, order.product_id)
            periods = [row for row in periods if row['order_id'] == order_id and row['start_at'] < cutoff.timestamp()]
            for row in periods:
                row['end_at'] = min(row['end_at'], int(cutoff.timestamp()))
                row['starts_at'] = datetime.fromtimestamp(row['start_at'], timezone.utc).isoformat()
                row['ends_at'] = datetime.fromtimestamp(row['end_at'], timezone.utc).isoformat()
            db.add(self.m.Payment(id='payment', order_id=order_id, payment_no='PAY', amount=order.amount, status='paid'))
            db.flush()
            db.add(self.m.Refund(id='refund', refund_no='REF', order_id=order_id,
                payment_id='payment', amount=10, status='executing', reason='PRIVATE'))
            db.flush()
            envelope = {'economic_plan': {}}
            plan = dict(refund_id='refund', entitlement_periods=periods,
                frozen_envelope_hash=digest(envelope), expires_at=cutoff.isoformat())
            plan['plan_hash'] = digest(plan)
            proof = dict(refund_id='refund', resource_plan_hash=digest(plan))
            db.add(self.m.refund_workflow['Case'](refund_id='refund', requested_by_id='buyer',
                plan_json=canonical(envelope), plan_hash=digest(envelope)))
            db.add(self.m.refund_execution['Execution'](id='execution', refund_id='refund',
                status='resources_applied', plan_hash=digest(envelope), resource_plan_json=canonical(plan),
                resource_plan_hash=digest(plan), proof_json=canonical(proof), funding_json='[]', requested_by='ops'))
            db.flush()
            db.add(self.m.refund_execution_resources['Recovery'](id='recovery', refund_id='refund',
                execution_id='execution', order_id=order_id, term_id=term.id, combination_id=term.combination_id,
                generation=term.generation, recovery_number=1, plan_json=canonical(plan), proof_json=canonical(proof),
                payload_hash=digest(dict(plan=plan, proof=proof))))
            db.commit()

    def test_actual_refund_overlay_shortens_final_api_expiry_in_expired_history(self):
        order_id = self.create(months=2)
        term = self.deliver(order_id)
        cutoff = term['starts_at'] + timedelta(days=10)
        self.persist_recovery(order_id, cutoff)
        row = self.request('GET', self.path, 'provider')['items'][0]
        self.assertEqual(datetime.fromisoformat(row['combination_expires_at']), cutoff)
        self.assertEqual(datetime.fromisoformat(row['effective_rights']['ends_at']), cutoff)
        self.assertEqual(datetime.fromisoformat(row['recorded_combination_expires_at']), term['ends_at'])
        self.assertFalse(row['effective_rights']['active_now'])
        self.assertEqual(row['refunds'], [{'refund_id': 'refund', 'refund_no': 'REF', 'status': 'executing'}])
        self.assertIn('period_key', row['quota_periods'][0])
        self.assertNotIn('PRIVATE', str(row))

    def test_rejected_refund_evidence_never_falls_back_to_original_dates(self):
        order_id = self.create()
        term = self.deliver(order_id)
        self.persist_recovery(order_id, term['starts_at'] + timedelta(days=10))
        with self.m.SessionLocal() as db:
            db.get(self.m.refund_workflow['Case'], 'refund').plan_hash = 'invalid'
            db.commit()
        self.request('GET', self.path, 'provider', 409)

    def test_generation_restart_keeps_history_without_restoring_old_rights(self):
        first = self.create()
        old = self.deliver(first)
        second = self.create()
        current = self.deliver(second, old['ends_at'] + timedelta(days=1))
        self.m.now.return_value = current['starts_at'] + timedelta(days=1)
        rows = {row['order_id']: row for row in self.request('GET', self.path, 'provider')['items']}
        self.assertEqual(rows[first]['term_generation'], 1)
        self.assertEqual(rows[first]['combination_generation'], 2)
        self.assertFalse(rows[first]['effective_rights']['current_generation'])
        self.assertFalse(rows[first]['effective_rights']['active_now'])
        self.assertEqual(rows[first]['effective_rights']['periods'], [])
        self.assertTrue(rows[second]['effective_rights']['active_now'])
        self.assertEqual(datetime.fromisoformat(rows[first]['ends_at']), old['ends_at'])

    def test_saas_actual_subscription_expiry_and_tenant_are_returned(self):
        order_id = self.create(months=2)
        term = self.deliver(order_id)
        cutoff = term['starts_at'] + timedelta(days=10)
        self.persist_recovery(order_id, cutoff)
        with self.m.SessionLocal() as db:
            db.add(self.m.SaaSProductVersion(id='saas-version', product_id='product',
                version_code='v1', name='SaaS', monthly_price=100, cost=10, status='active'))
            db.flush()
            db.add(self.m.SaaSSubscription(id='subscription', enterprise_id='buyer_tenant', product_id='product',
                version_id='saas-version', external_tenant_id='external-tenant', status='active',
                starts_at=term['starts_at'], expires_at=cutoff))
            order = db.get(self.m.Order, order_id)
            order.subscription_id, order.delivery_method_snapshot = 'subscription', 'tenant_access'
            delivery = json.loads(order.delivery_snapshot_json)
            delivery['method'] = 'tenant_access'
            order.delivery_snapshot_json = json.dumps(delivery)
            combo = db.scalar(select(self.C))
            combo.delivery_method = 'tenant_access'
            db.commit()
        row = self.request('GET', self.path, 'provider')['items'][0]
        self.assertEqual(row['subscription_id'], 'subscription')
        self.assertEqual(row['tenant_id'], 'external-tenant')
        self.assertEqual(datetime.fromisoformat(row['combination_expires_at']), cutoff)
        self.assertEqual(datetime.fromisoformat(row['subscription_expires_at']), cutoff)
        self.assertEqual(datetime.fromisoformat(row['effective_rights']['ends_at']), cutoff)
        self.assertEqual(row['reconciliation_context']['tenant_id'], 'external-tenant')

    def test_refund_shortening_one_order_preserves_later_combination_rights(self):
        first = self.create()
        original = self.deliver(first)
        second = self.create()
        renewal = self.deliver(second, original['starts_at'] + timedelta(days=1))
        cutoff = original['starts_at'] + timedelta(days=10)
        self.persist_recovery(first, cutoff)
        rows = {row['order_id']: row for row in self.request('GET', self.path, 'provider')['items']}
        self.assertEqual(datetime.fromisoformat(rows[first]['effective_rights']['ends_at']), cutoff)
        for row in rows.values():
            self.assertEqual(datetime.fromisoformat(row['combination_expires_at']), renewal['ends_at'])

    def test_change_association_is_projected_without_private_snapshots(self):
        order_id = self.create()
        self.deliver(order_id)
        with self.m.SessionLocal() as db:
            term = db.scalar(select(self.T).where(self.T.order_id == order_id))
            Change = self.m.version_change_saga['VersionChangeRequest']
            db.add(Change(id='change', source_order_id=order_id, enterprise_id='buyer_tenant',
                combination_id=term.combination_id, target_version_id='v2', target_version_code='v2',
                old_version_id='version', old_version_code='v1', kind='upgrade', status='pending_payment',
                idempotency_key='change', request_hash='a'*64, quote_json='PRIVATE',
                prepared_json='PRIVATE', target_snapshots_json='PRIVATE', created_by='buyer'))
            db.commit()
        row = self.request('GET', self.path, 'provider')['items'][0]
        self.assertEqual(row['changes'], [dict(change_id='change', source_order_id=order_id,
            funding_order_id=None, kind='upgrade', status='pending_payment')])
        self.assertNotIn('PRIVATE', str(row))


if __name__ == '__main__':
    unittest.main()
