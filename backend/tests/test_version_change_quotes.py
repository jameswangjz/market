"""Production HTTP quote routes, isolated SQLite/catalog fixture, no execution."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import json
import unittest
from unittest.mock import patch

import test_subscription_terms as fixture


class VersionChangeQuoteTests(unittest.TestCase):
    setUpClass = classmethod(fixture.SubscriptionTermsTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.SubscriptionTermsTests.tearDownClass.__func__)
    tearDown = fixture.SubscriptionTermsTests.tearDown
    request = fixture.SubscriptionTermsTests.request
    create = fixture.SubscriptionTermsTests.create
    deliver = fixture.SubscriptionTermsTests.deliver
    counts = fixture.SubscriptionTermsTests.counts

    def setUp(self):
        fixture.SubscriptionTermsTests.setUp(self)
        with self.m.SessionLocal() as db:
            db.get(self.m.ProductReleaseVersion, 'version').cost = 30
            target = db.get(self.m.ProductReleaseVersion, 'v2')
            target.price, target.cost = 150, 60
            db.add(self.m.User(id='member', name='Member', email='member@example.invalid',
                password_hash='unused', verified_status='verified', activation_status='active'))
            db.flush()
            db.add(self.m.Membership(user_id='member', enterprise_id='buyer_tenant', role='member'))
            db.commit()
            with patch.object(self.m, 'now', return_value=datetime.now(timezone.utc)):
                self.tokens['member'] = self.m.issue_token(db.get(self.m.User, 'member'))
        self.quote_module = importlib.import_module(self.m.__package__+'.version_change_quotes')

    def prepare(self, method='api', months=3, future=True):
        if method == 'tenant_access':
            with self.m.SessionLocal() as db:
                product = db.get(self.m.Product, 'product')
                product.product_type, product.delivery_method = 'saas', method
                db.add_all([self.m.SaaSProductVersion(id=vid, product_id='product', version_code=code,
                    name=code, monthly_price=price, cost=cost, status='active') for vid, code, price, cost in
                    [('version', 'v1', 100, 30), ('v2', 'v2', 150, 60)]])
                db.commit()
        oid = self.create(months, method=method)
        if method == 'tenant_access':
            with self.m.SessionLocal() as db:
                order = db.get(self.m.Order, oid)
                subscription = db.get(self.m.SaaSSubscription, order.subscription_id)
                subscription.status, subscription.external_tenant_id = 'active', 'fixture-tenant'
                db.commit()
        first = self.deliver(oid)
        if future:
            renewal = self.create(1, method=method)
            self.deliver(renewal, fixture.instant(2024, 2, 1))
        self.m.now.return_value = first['starts_at'] + (first['ends_at']-first['starts_at'])/2
        return oid, first

    def quote(self, oid, actor='buyer', expected=200, target='v2', confirmed=False):
        return self.request('POST', f'/api/orders/{oid}/version-change-quote', actor, expected,
            json={'target_version_id': target, 'compatibility_confirmed': confirmed})

    def validate(self, oid, token, actor='buyer', expected=200):
        return self.request('POST', f'/api/orders/{oid}/version-change-quote/validate', actor, expected,
            json={'quote_token': token})

    def test_api_current_and_future_segments_use_same_original_duration(self):
        oid, _ = self.prepare()
        before = self.counts()
        quoted = self.quote(oid)
        self.assertEqual((quoted['payment_amount'], quoted['refund_amount'], quoted['net_difference']), (125, 0, 125))
        self.assertEqual([(s['subscription_months'], s['original_remaining_amount'], s['target_remaining_amount'], s['net_difference'])
            for s in quoted['segments']], [(3, 150, 225, 75), (1, 100, 150, 50)])
        self.assertEqual([s['remaining_ratio'] for s in quoted['segments']], ['0.5', '1'])
        self.assertFalse(quoted['execution_ready'])
        self.assertEqual(self.validate(oid, quoted['quote_token']), quoted)
        self.assertEqual(self.counts(), before)

    def test_model_api_supports_same_calculation(self):
        oid, _ = self.prepare(method='model_api')
        self.assertEqual(self.quote(oid)['payment_amount'], 125)

    def test_live_source_price_and_cost_do_not_replace_frozen_order_values(self):
        oid, _ = self.prepare()
        with self.m.SessionLocal() as db:
            source = db.get(self.m.ProductReleaseVersion, 'version')
            source.price, source.cost = 999, 888
            db.commit()
        quoted = self.quote(oid)
        self.assertEqual(quoted['payment_amount'], 125)
        self.assertEqual([row['original_amount'] for row in quoted['segments']], [300, 100])

    def test_quote_routes_disallow_response_caching(self):
        oid, _ = self.prepare()
        headers = {'Authorization': 'Bearer ' + self.tokens['buyer']}
        options = self.client.get(f'/api/orders/{oid}/version-change-options', headers=headers)
        quoted = self.client.post(f'/api/orders/{oid}/version-change-quote', headers=headers,
            json={'target_version_id': 'v2'})
        validated = self.client.post(f'/api/orders/{oid}/version-change-quote/validate', headers=headers,
            json={'quote_token': quoted.json()['quote_token']})
        for response in (options, quoted, validated):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_saas_requires_binding_and_active_tenant_and_supports_quotes(self):
        oid, _ = self.prepare(method='tenant_access')
        self.assertEqual(self.quote(oid)['payment_amount'], 125)
        with self.m.SessionLocal() as db:
            subscription = db.get(self.m.SaaSSubscription, db.get(self.m.Order, oid).subscription_id)
            subscription.status = 'provisioning'
            db.commit()
        self.quote(oid, expected=409)

    def test_breaking_saas_change_still_requires_explicit_confirmation(self):
        oid, _ = self.prepare(method='tenant_access', future=False)
        with self.m.SessionLocal() as db:
            version = db.get(self.m.SaaSProductVersion, 'v2')
            version.compatibility_mode = 'breaking'
            version.compatibility_notes = 'Tenant contract changed'
            db.commit()
        self.quote(oid, expected=409)
        quote = self.quote(oid, confirmed=True)
        self.assertTrue(quote['compatibility']['confirmation_required'])

    def test_downgrade_and_equal_price_are_not_execution(self):
        oid, _ = self.prepare()
        for price, cost, expected, action in [(50, 20, -125, 'downgrade'), (100, 90, 0, 'no_payment')]:
            with self.m.SessionLocal() as db:
                target = db.get(self.m.ProductReleaseVersion, 'v2')
                target.price, target.cost = price, cost
                db.commit()
            quoted = self.quote(oid)
            self.assertEqual((quoted['net_difference'], quoted['action']), (expected, action))
            self.assertEqual(quoted['refund_amount'], -expected)
            self.assertFalse(quoted['execution_ready'])

    def test_internal_adjustments_reverse_old_cost_and_recount_target_cost(self):
        oid, _ = self.prepare()
        with self.m.SessionLocal() as db:
            result = self.m.version_change_quotes['prepare'](db, db.get(self.m.User, 'buyer'), oid, 'v2', False, self.m.now())
        self.assertEqual([(x['old']['revenue'], x['old']['cost'], x['target']['revenue'], x['target']['cost'])
            for x in result['internal']], [('-150.00', '-45.00', '225.00', '90.00'), ('-100.00', '-30.00', '150.00', '60.00')])
        self.assertEqual(sum((Decimal(x['target']['profit'])+Decimal(x['old']['profit']) for x in result['internal'])), 50)
        self.assertNotEqual(result['internal'][0]['old']['rule']['version'], '')

    def test_quote_fingerprint_is_frozen_into_each_integration_snapshot(self):
        oid, _ = self.prepare()
        context = {'kind': 'api', 'config_id': 'cfg', 'revision_id': 'rev-7',
            'evidence_hash': 'a' * 64, 'route_id': 'route', 'route_key': 'route-v2',
            'upstream_url': 'https://upstream.invalid/v2'}
        with self.m.SessionLocal() as db, patch.object(
                self.quote_module, 'integration_context', return_value=context):
            prepared = self.m.version_change_quotes['prepare'](
                db, db.get(self.m.User, 'buyer'), oid, 'v2', False, self.m.now())
        self.assertTrue(prepared['snapshots_by_term'])
        for snapshot in prepared['snapshots_by_term'].values():
            self.assertEqual(snapshot['integration'], {
                **context, 'quote_fingerprint': prepared['digest']})

    def test_public_json_and_decoded_token_hide_cost_rules_and_upstream(self):
        oid, _ = self.prepare()
        quoted = self.quote(oid)
        claims = self.m.jwt.decode(quoted['quote_token'], options={'verify_signature': False})
        for value in (quoted, claims, self.request('GET', f'/api/orders/{oid}/version-change-options')):
            text = json.dumps(value)
            for name in ('cost', 'profit', 'settlement_rule', 'upstream_url', 'client_secret'):
                self.assertNotIn(name, text)

    def test_five_minute_expiry_and_frozen_time(self):
        oid, _ = self.prepare()
        quoted = self.quote(oid)
        self.m.now.return_value += timedelta(seconds=60)
        self.assertEqual(self.validate(oid, quoted['quote_token']), quoted)
        self.m.now.return_value += timedelta(seconds=240)
        self.validate(oid, quoted['quote_token'], expected=409)

    def test_expiry_cannot_cross_current_term_boundary(self):
        oid, first = self.prepare()
        self.m.now.return_value = first['ends_at']-timedelta(seconds=10)
        quoted = self.quote(oid)
        self.assertEqual(quoted['expires_at'], first['ends_at'].isoformat())
        self.m.now.return_value = first['ends_at']
        self.validate(oid, quoted['quote_token'], expected=409)

    def test_tampering_actor_and_order_bindings(self):
        oid, _ = self.prepare()
        quoted = self.quote(oid)
        token = quoted['quote_token']
        bits = token.split('.')
        bits[1] = bits[1][:-2]+'aa'
        self.validate(oid, '.'.join(bits), expected=400)
        self.validate(oid, token, actor='buyer_admin', expected=403)
        self.validate('some-other-order', token, expected=403)
        self.validate(oid, self.tokens['buyer'], expected=400)

    def test_ordinary_platform_other_enterprise_and_unverified_are_denied(self):
        oid, _ = self.prepare()
        for actor in ('member', 'platform', 'finance', 'outsider'):
            self.quote(oid, actor=actor, expected=403)
            self.request('GET', f'/api/orders/{oid}/version-change-options', actor, 403)
        with self.m.SessionLocal() as db:
            db.get(self.m.User, 'buyer').verified_status = 'pending_review'
            db.commit()
        self.quote(oid, expected=403)

    def test_options_are_tenant_scoped_and_only_other_available_version(self):
        oid, _ = self.prepare()
        options = self.request('GET', f'/api/orders/{oid}/version-change-options', actor='buyer_admin')
        self.assertEqual(options['current_version_id'], 'version')
        self.assertEqual([(v['id'], v['monthly_price']) for v in options['versions']], [('v2', 150)])
        self.quote(oid, target='version', expected=409)
        self.quote(oid, target='missing-version', expected=400)

    def test_api_breaking_version_does_not_require_confirmation_and_changed_notes_invalidate(self):
        oid, _ = self.prepare()
        with self.m.SessionLocal() as db:
            version = db.get(self.m.ProductReleaseVersion, 'v2')
            version.compatibility_mode, version.compatibility_notes = 'legacy-unknown', 'Changed request schema'
            db.commit()
        listed = self.request('GET', f'/api/orders/{oid}/version-change-options')
        self.assertEqual((listed['versions'][0]['compatibility_mode'], listed['versions'][0]['compatibility_notes']), ('compatible', ''))
        quoted = self.quote(oid)
        self.assertEqual((quoted['compatibility']['mode'], quoted['compatibility']['notes']), ('compatible', ''))
        self.assertFalse(quoted['compatibility']['confirmation_required'])
        with self.m.SessionLocal() as db:
            db.get(self.m.ProductReleaseVersion, 'v2').compatibility_notes = 'Changed again'
            db.commit()
        self.validate(oid, quoted['quote_token'], expected=409)

    def test_target_price_cost_and_settlement_rule_changes_invalidate(self):
        oid, _ = self.prepare()
        for change in ('price', 'cost', 'rule'):
            quoted = self.quote(oid)
            with self.m.SessionLocal() as db:
                if change == 'rule':
                    db.get(self.m.SettlementRule, 'trading_rule').provider_rate = 60
                else:
                    version = db.get(self.m.ProductReleaseVersion, 'v2')
                    setattr(version, change, getattr(version, change)+1)
                db.commit()
            self.validate(oid, quoted['quote_token'], expected=409)

    def test_future_renewal_and_refund_invalidate_quotes(self):
        oid, _ = self.prepare()
        quoted = self.quote(oid)
        new = self.create(1)
        self.deliver(new, at=self.m.now())
        self.validate(oid, quoted['quote_token'], expected=409)
        quoted = self.quote(oid)
        with self.m.SessionLocal() as db:
            db.get(self.m.Order, new).refunded_amount = 1
            db.commit()
        self.validate(oid, quoted['quote_token'], expected=409)

    def test_future_gap_and_paid_undelivered_order_are_rejected(self):
        oid, _ = self.prepare()
        with self.m.SessionLocal() as db:
            combo = db.scalar(self.select(self.C))
            rows = db.scalars(self.select(self.T).order_by(self.T.starts_at)).all()
            rows[-1].status = 'revoked'
            db.commit()
        self.quote(oid, expected=409)

    def test_paid_order_without_term_prevents_omitting_future_payment(self):
        oid, _ = self.prepare()
        self.create(1)  # Paid snapshot, but trusted delivery hook has not assigned a term.
        self.quote(oid, expected=409)

    def test_quote_rounding_is_per_segment_and_calendar_uses_actual_seconds(self):
        oid, first = self.prepare(months=1, future=False)
        with self.m.SessionLocal() as db:
            target = db.get(self.m.ProductReleaseVersion, 'v2')
            target.price, target.cost = Decimal('100.01'), 30
            db.commit()
        quoted = self.quote(oid)
        self.assertEqual(quoted['segments'][0]['remaining_ratio'], '0.5')
        self.assertEqual(quoted['segments'][0]['target_remaining_amount'], 50.01)
        self.assertEqual(quoted['payment_amount'], 0.01)
        ratio = self.quote_module.remaining_ratio(first['starts_at'], first['ends_at'], first['starts_at']+timedelta(days=1))
        self.assertEqual(ratio, Decimal(28)/Decimal(29))

    def test_extra_fields_and_nonboolean_confirmation_are_rejected(self):
        oid, _ = self.prepare()
        for body in ({'target_version_id': 'v2', 'compatibility_confirmed': 'true'},
                     {'target_version_id': 'v2', 'cost': 0}):
            self.request('POST', f'/api/orders/{oid}/version-change-quote', expected=422, json=body)


if __name__ == '__main__':
    unittest.main()
