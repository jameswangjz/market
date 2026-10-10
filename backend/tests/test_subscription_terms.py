"""Persisted calendar and HTTP renewal regressions using production models.

Run: python -m unittest discover -s backend/tests -p test_subscription_terms.py -v
SQLite verifies transactions/constraints; PostgreSQL lock SQL is compiled below.
"""
import importlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

import test_message_business_events as fixtures


def instant(year=2024, month=1, day=31):
    # 12:00 Shanghai, including Jan 31 clamping in the business timezone.
    return datetime(year, month, day, 4, tzinfo=timezone.utc)


class SubscriptionTermsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.BusinessRoutesHTTPTests.setUpClass.__func__(cls)
        cls.module = importlib.import_module(cls.m.__package__ + '.subscription_terms')
        cls.ns = vars(cls.m)
        cls.hooks = cls.module.install(cls.ns)
        cls.C = cls.hooks['Combination']
        cls.T = cls.hooks['Term']
        cls.R = cls.hooks['SubscriptionRenewal']

    tearDownClass = classmethod(fixtures.BusinessRoutesHTTPTests.tearDownClass.__func__)
    tearDown = fixtures.BusinessRoutesHTTPTests.tearDown
    request = fixtures.BusinessRoutesHTTPTests.request

    def setUp(self):
        fixtures.BusinessRoutesHTTPTests.setUp(self)
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, 'product').delivery_method = 'api'
            db.get(self.m.Enterprise, 'buyer_tenant').verification_status = 'verified'
            version = db.get(self.m.ProductReleaseVersion, 'version')
            version.monthly_quota = 123
            version.quota_amount = 999
            db.add(self.m.ProductReleaseVersion(id='v2', product_id='product', version_code='v2',
                                               price=200, cost=20, status='active', monthly_quota=456))
            db.commit()
        self.catalog = patch.dict(self.m.storefront, {'versions_for': lambda db, product: [{'id': 'version'}, {'id': 'v2'}]})
        self.catalog.start()
        self.addCleanup(self.catalog.stop)
        self.clock = patch.object(self.m, 'now', return_value=instant())
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def create(self, months=1, version='version', delivered=True, method='api'):
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, 'product').delivery_method = method
            body = self.m.OrderBody(product_id='product', product_version_id=version,
                buyer_enterprise_id='buyer_tenant', subscription_months=months)
            quoted = self.m.order_economics['quote'](db, db.get(self.m.User, 'buyer'), body)
            order = self.m.create_quoted_order(db, db.get(self.m.User, 'buyer'), quoted)
            if delivered:
                order.payment_status = 'paid'
                order.paid_amount = order.amount
                order.main_status = 'pending_acceptance'
                order.delivery_status = 'pending_acceptance'
            db.commit()
            return order.id

    def deliver(self, oid, at=None, commit=True):
        with self.m.SessionLocal() as db:
            order = db.scalar(select(self.m.Order).where(self.m.Order.id == oid).with_for_update()
                              .execution_options(populate_existing=True))
            with patch.object(db, 'commit', side_effect=AssertionError('hook must not commit')):
                term = self.hooks['on_delivery_success'](db, order, db.get(self.m.User, 'ops'), at=at or instant())
            result = None if term is None else {key: getattr(term, key) for key in (
                'id', 'generation', 'start_month', 'months', 'starts_at', 'ends_at', 'created_at')}
            if commit:
                db.commit()
            else:
                db.rollback()
            return result

    def entitlement(self, at):
        with self.m.SessionLocal() as db:
            return self.hooks['current_entitlement'](db, 'buyer_tenant', 'product', at=at)

    def periods(self):
        with self.m.SessionLocal() as db:
            return self.hooks['quota_periods'](db, 'buyer_tenant', 'product')

    def counts(self):
        with self.m.SessionLocal() as db:
            return tuple(db.scalar(select(self.func.count()).select_from(model)) for model in (
                self.C, self.T, self.R, self.m.Order, self.m.Payment, self.m.OrderStateLog, self.m.AuditLog,
                self.Message, self.Receipt, self.Outbox, self.m.SettlementMeasurement))

    def renewal(self, oid, months=1, key='request-1', actor='buyer', expected=200):
        return self.request('POST', f'/api/orders/{oid}/renewal', actor, expected,
                            json={'subscription_months': months, 'idempotency_key': key})

    def test_install_before_create_all_and_idempotent_registration(self):
        self.assertIs(self.module.install(self.ns), self.hooks)
        self.assertIs(self.ns['SubscriptionCombination'], self.C)
        self.assertIs(self.ns['Term'], self.T)
        from sqlalchemy import inspect
        tables = inspect(self.m.engine).get_table_names()
        self.assertIn('subscription_combinations', tables)
        self.assertIn('subscription_terms', tables)
        self.assertIn('subscription_renewals', tables)

    def test_january_31_renewal_preserves_anchor_and_february_leap_clamp(self):
        first = self.create()
        one = self.deliver(first)
        self.assertEqual(one['starts_at'], instant())
        self.assertEqual(one['ends_at'], instant(2024, 2, 29))
        second = self.create()
        two = self.deliver(second, instant(2024, 2, 1))
        self.assertEqual(two['start_month'], 1)
        self.assertEqual(two['starts_at'], instant(2024, 2, 29))
        self.assertEqual(two['ends_at'], instant(2024, 3, 31))
        before = self.entitlement(instant(2024, 2, 28))
        boundary = self.entitlement(instant(2024, 2, 29))
        self.assertEqual(before['period_index'], 0)
        self.assertEqual(boundary['period_index'], 1)
        self.assertEqual(before['anchor_at'], instant())
        self.assertIsNone(self.entitlement(instant(2024, 3, 31)))

    def test_february_29_anniversary_and_36_month_purchase(self):
        term = self.deliver(self.create(36), instant(2024, 2, 29))
        self.assertEqual(term['ends_at'], instant(2027, 2, 28))
        entitlement = self.entitlement(instant(2025, 2, 28))
        self.assertEqual(entitlement['period_index'], 12)
        self.assertEqual(len(self.periods()), 36)

    def test_expiry_exact_boundary_restarts_generation_and_keeps_history(self):
        oid = self.create()
        old = self.deliver(oid)
        new = self.deliver(self.create(), old['ends_at'])
        self.assertEqual(new['generation'], 2)
        self.assertEqual(new['start_month'], 0)
        self.assertEqual(new['starts_at'], old['ends_at'])
        self.assertEqual(new['ends_at'], instant(2024, 3, 29))
        self.assertEqual(self.deliver(oid, instant(2024, 4, 1)), old)
        self.assertEqual(len(self.periods()), 1)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.scalar(select(self.func.count()).select_from(self.T)), 2)

    def test_future_term_changes_neither_current_snapshot_nor_bucket(self):
        first = self.create()
        self.deliver(first)
        before = self.entitlement(instant(2024, 2, 1))
        with self.m.SessionLocal() as db:
            db.get(self.m.ProductReleaseVersion, 'version').monthly_quota = 789
            db.commit()
        self.deliver(self.create(2), instant(2024, 2, 1))
        after = self.entitlement(instant(2024, 2, 1))
        self.assertEqual(before['limits'], after['limits'])
        self.assertEqual(before['period_key'], after['period_key'])
        self.assertEqual(before['period_starts_at'], after['period_starts_at'])
        self.assertEqual(after['limits']['monthly_quota'], 123)
        self.assertEqual(self.entitlement(instant(2024, 2, 29))['limits']['monthly_quota'], 789)
        periods = self.periods()
        self.assertEqual([p['monthly_quota'] for p in periods], [123, 789, 789])
        self.assertEqual(periods[0]['start_at'], int(instant().timestamp()))
        self.assertEqual(periods[1]['period_key'].rsplit(':', 1)[1], '1')

    def test_zero_monthly_quota_is_unlimited_no_quota_amount_conversion(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.ProductReleaseVersion, 'version').monthly_quota = 0
            db.commit()
        self.deliver(self.create())
        self.assertEqual(self.periods()[0]['monthly_quota'], 0)
        self.assertEqual(self.entitlement(instant())['limits']['quota_amount'], 999)

    def test_order_delivery_idempotency_never_extends_again(self):
        oid = self.create(36)
        first = self.deliver(oid)
        counts = self.counts()
        self.assertEqual(self.deliver(oid, instant(2026, 2, 1)), first)
        self.assertEqual(self.counts(), counts)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.scalar(select(self.C)).total_months, 36)

    def test_active_version_mismatch_rejected_purchase_and_delivery(self):
        self.deliver(self.create())
        oid = self.create(version='v2')
        before = self.counts()
        with self.assertRaises(self.m.HTTPException) as raised:
            self.deliver(oid)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn('升级', raised.exception.detail)
        with self.m.SessionLocal() as db:
            quote = self.m.order_economics['quote'](db, db.get(self.m.User, 'buyer'),
                self.m.OrderBody(product_id='product', product_version_id='v2', buyer_enterprise_id='buyer_tenant'))
            with self.assertRaises(self.m.HTTPException):
                self.hooks['validate_purchase'](db, db.get(self.m.User, 'buyer'), quote)
            db.rollback()
        self.assertEqual(self.counts(), before)

    def test_expired_combination_allows_new_version_with_new_generation(self):
        self.deliver(self.create())
        with patch.object(self.m, 'now', return_value=instant(2024, 3, 1)):
            with self.m.SessionLocal() as db:
                quote = self.m.order_economics['quote'](db, db.get(self.m.User, 'buyer'),
                    self.m.OrderBody(product_id='product', product_version_id='v2', buyer_enterprise_id='buyer_tenant'))
                self.hooks['validate_purchase'](db, db.get(self.m.User, 'buyer'), quote)
        self.deliver(self.create(version='v2'), instant(2024, 3, 1))
        self.assertEqual(self.entitlement(instant(2024, 3, 1))['version_code'], 'v2')

    def test_bad_financial_delivery_and_version_snapshots_grant_nothing(self):
        for field, value in (('payment_status', 'unpaid'), ('paid_amount', 99), ('refunded_amount', 1),
                ('delivery_status', 'in_delivery'), ('main_status', 'closed'),
                ('product_version_code', 'wrong'), ('economic_snapshot_json', '{}'),
                ('delivery_snapshot_json', '{}'), ('subscription_months', 37)):
            with self.subTest(field=field):
                oid = self.create()
                with self.m.SessionLocal() as db:
                    setattr(db.get(self.m.Order, oid), field, value)
                    db.commit()
                before = self.counts()
                with self.assertRaises(self.m.HTTPException):
                    self.deliver(oid)
                self.assertEqual(self.counts(), before)

    def test_revocation_refund_and_lifecycle_disable_entitlement_and_periods(self):
        for model, field, value in (('order', 'refunded_amount', 1), ('order', 'payment_status', 'unpaid'),
                ('order', 'paid_amount', 0), ('order', 'main_status', 'cancelled'), ('order', 'main_status', 'closed'),
                ('order', 'delivery_status', 'exception'), ('term', 'status', 'revoked'),
                ('combination', 'status', 'revoked')):
            with self.subTest(field=field, value=value):
                if not hasattr(self, 'oid'):
                    self.oid = self.create()
                    self.deliver(self.oid)
                with self.m.SessionLocal() as db:
                    row = db.get(self.m.Order, self.oid) if model == 'order' else db.scalar(select(self.T if model == 'term' else self.C))
                    original = getattr(row, field)
                    setattr(row, field, value)
                    db.commit()
                self.assertIsNone(self.entitlement(instant()))
                self.assertEqual(self.periods(), [])
                with self.m.SessionLocal() as db:
                    row = db.get(self.m.Order, self.oid) if model == 'order' else db.scalar(select(self.T if model == 'term' else self.C))
                    setattr(row, field, original)
                    db.commit()

    def test_rollback_removes_initial_and_extended_terms(self):
        oid = self.create()
        before = self.counts()
        self.deliver(oid, commit=False)
        self.assertEqual(self.counts(), before)
        self.deliver(oid)
        expiry = self.entitlement(instant())['expires_at']
        second = self.create()
        before = self.counts()
        self.deliver(second, instant(2024, 2, 1), commit=False)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.entitlement(instant())['expires_at'], expiry)

    def test_legacy_and_nonmonthly_do_not_import_or_mutate(self):
        for method, legacy in (('api', True), ('tenant_access', True), ('file', False), ('consulting', False)):
            oid = self.create(method=method)
            with self.m.SessionLocal() as db:
                order = db.get(self.m.Order, oid)
                if legacy:
                    order.snapshot_version = 0
                    order.subscription_id = 'historical-subscription'
                db.commit()
            before = self.counts()
            self.assertIsNone(self.deliver(oid))
            self.assertEqual(self.counts(), before)
            self.assertFalse(self.request('GET', f'/api/orders/{oid}/subscription')['item']['available'])

    def test_sql_lock_contract_first_insert_and_existing_combination(self):
        statements = []
        with self.m.SessionLocal() as db:
            execute = db.scalar
            def capture(statement, *args, **kwargs):
                statements.append(str(statement.compile(dialect=postgresql.dialect())))
                return execute(statement, *args, **kwargs)
            with patch.object(db, 'scalar', side_effect=capture):
                self.hooks['lock_combination'](db, 'buyer_tenant', 'product')
        self.assertIn('FROM enterprises', statements[0])
        self.assertIn('FOR UPDATE', statements[0])
        self.assertIn('FROM subscription_combinations', statements[1])
        self.assertIn('FOR UPDATE', statements[1])

    def test_unique_combination_and_order_term_backstops(self):
        oid = self.create()
        self.deliver(oid)
        for model in (self.C, self.T):
            with self.m.SessionLocal() as db:
                row = db.scalar(select(model))
                values = {c.name: getattr(row, c.name) for c in model.__table__.columns if c.name != 'id'}
                db.add(model(**values))
                with self.assertRaises(IntegrityError):
                    db.flush()
                db.rollback()

    def test_http_get_exact_dto_and_no_cost_or_secret(self):
        oid = self.create()
        self.deliver(oid)
        result = self.request('GET', f'/api/orders/{oid}/subscription')['item']
        self.assertEqual(set(result), {'available', 'id', 'product_id', 'enterprise_id', 'version_id',
            'version_code', 'anchor_at', 'generation', 'expires_at', 'status', 'terms', 'current_period', 'limits', 'can_renew'})
        self.assertTrue(result['available'])
        self.assertTrue(result['can_renew'])
        self.assertEqual(result['terms'][0]['order_id'], oid)
        self.assertEqual(result['current_period']['index'], 0)
        for word in ('unit_cost', 'total_cost', 'upstream', 'secret', 'settlement_rule'):
            self.assertNotIn(word, json.dumps(result))
        before = self.counts()
        for actor in ('buyer', 'buyer_admin', 'finance', 'platform'):
            item = self.request('GET', f'/api/orders/{oid}/subscription', actor)['item']
            self.assertEqual(item['can_renew'], actor in ('buyer', 'buyer_admin'))
        for actor in ('provider', 'outsider', 'inactive', 'ops', 'reviewer'):
            self.request('GET', f'/api/orders/{oid}/subscription', actor, 403)
        self.request('GET', f'/api/orders/{oid}/subscription', 'disabled_ops', 401)
        self.assertEqual(self.counts(), before)

    def test_http_renewal_unpaid_1_and_36_months_and_idempotency(self):
        oid = self.create()
        self.deliver(oid)
        for months in (1, 36):
            result = self.renewal(oid, months, f'renew-{months}')
            self.assertEqual(result['payment_status'], 'unpaid')
            self.assertEqual(result['delivery_status'], 'not_started')
            self.assertEqual(result['subscription_months'], months)
            before = self.counts()
            self.assertEqual(self.renewal(oid, months, f'renew-{months}', 'buyer_admin')['id'], result['id'])
            self.assertEqual(self.counts(), before)
            self.renewal(oid, 2, f'renew-{months}', expected=409)
        self.assertEqual(len(self.periods()), 1)

    def test_renewal_retry_survives_catalog_unpublication(self):
        oid = self.create()
        self.deliver(oid)
        result = self.renewal(oid)
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, 'product').status = 'unpublished'
            db.get(self.m.ProductReleaseVersion, 'version').status = 'disabled'
            db.commit()
        self.assertEqual(self.renewal(oid)['id'], result['id'])

    def test_http_expired_renewal_does_not_activate_or_reset_before_delivery(self):
        oid = self.create()
        self.deliver(oid)
        with patch.object(self.m, 'now', return_value=instant(2024, 4, 1)):
            result = self.renewal(oid)
            item = self.request('GET', f'/api/orders/{oid}/subscription')['item']
            self.assertFalse(item['available'])
            self.assertTrue(item['can_renew'])
            self.assertIsNotNone(item['id'])
        self.assertIsNone(self.entitlement(instant(2024, 4, 1)))
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, result['id'])
            order.payment_status = 'paid'
            order.paid_amount = order.amount
            order.delivery_status = order.main_status = 'pending_acceptance'
            db.commit()
        term = self.deliver(result['id'], instant(2024, 4, 1))
        self.assertEqual(term['generation'], 2)

    def test_http_renewal_permissions_bounds_and_extra_fields(self):
        oid = self.create()
        self.deliver(oid)
        before = self.counts()
        for actor in ('provider', 'outsider', 'inactive', 'ops', 'finance', 'platform'):
            self.renewal(oid, actor=actor, expected=403)
        for months in (0, 37, True, 1.5, '1'):
            self.renewal(oid, months=months, expected=422)
        for key in ('', ' ', 'a b'):
            self.renewal(oid, key=key, expected=422)
        self.request('POST', f'/api/orders/{oid}/renewal', expected=422,
            json={'subscription_months': 1, 'idempotency_key': 'x', 'product_version_id': 'v2'})
        self.assertEqual(self.counts(), before)

    def test_permission_changes_rechecked_for_read_and_idempotent_retry(self):
        oid = self.create()
        self.deliver(oid)
        self.renewal(oid)
        for model, field, value in (('user', 'verified_status', 'pending'), ('user', 'is_active', False),
                ('user', 'activation_status', 'disabled'), ('member', 'status', 'disabled'),
                ('member', 'role', 'member'), ('enterprise', 'verification_status', 'pending')):
            with self.m.SessionLocal() as db:
                row = db.get(self.m.User, 'buyer') if model == 'user' else (
                    db.get(self.m.Enterprise, 'buyer_tenant') if model == 'enterprise' else
                    db.scalar(select(self.m.Membership).where(self.m.Membership.user_id == 'buyer')))
                original = getattr(row, field)
                setattr(row, field, value)
                db.commit()
            expected = 401 if field == 'is_active' else 403
            self.request('GET', f'/api/orders/{oid}/subscription', expected=expected)
            self.renewal(oid, expected=expected)
            with self.m.SessionLocal() as db:
                row = db.get(self.m.User, 'buyer') if model == 'user' else (
                    db.get(self.m.Enterprise, 'buyer_tenant') if model == 'enterprise' else
                    db.scalar(select(self.m.Membership).where(self.m.Membership.user_id == 'buyer')))
                setattr(row, field, original)
                db.commit()

    def test_factory_failure_rolls_back_order_payment_audit_and_request(self):
        oid = self.create()
        self.deliver(oid)
        before = self.counts()
        original = self.m.create_quoted_order
        def fail(db, user, quoted):
            original(db, user, quoted)
            db.flush()
            raise RuntimeError('after factory flush')
        with patch.object(self.m, 'create_quoted_order', side_effect=fail):
            self.renewal(oid, expected=500)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.renewal(oid)['payment_status'], 'unpaid')

    def test_request_flush_failure_rolls_back_factory_before_commit(self):
        from sqlalchemy.orm import Session
        oid = self.create()
        self.deliver(oid)
        before = self.counts()
        original = Session.commit
        def fail(db):
            if any(isinstance(row, self.R) for row in db.new):
                db.flush()
                raise RuntimeError('after idempotency flush')
            return original(db)
        with patch.object(Session, 'commit', fail):
            self.renewal(oid, expected=500)
        self.assertEqual(self.counts(), before)

    def test_unaware_success_timestamp_rejected_before_mutation(self):
        oid = self.create()
        before = self.counts()
        with self.assertRaises(ValueError):
            self.deliver(oid, datetime(2024, 1, 31))
        self.assertEqual(self.counts(), before)

    def test_all_monthly_methods_persist_only_after_verified_success_hook(self):
        for method in ('api', 'model_api', 'tenant_access'):
            with self.subTest(method=method):
                oid = self.create(method=method)
                before = self.counts()
                self.assertEqual(before[1], 0)
                self.assertIsNone(self.entitlement(instant()))
                term = self.deliver(oid)
                self.assertEqual(self.entitlement(instant())['delivery_method'], method)
                with self.m.SessionLocal() as db:
                    db.delete(db.get(self.T, term['id']))
                    db.flush()
                    db.delete(db.scalar(select(self.C)))
                    db.commit()

    def test_renewal_product_lock_precedes_combination_and_single_commit(self):
        from sqlalchemy.orm import Session
        oid = self.create()
        self.deliver(oid)
        statements, commits = [], []
        scalar, commit = Session.scalar, Session.commit
        def capture(db, statement, *args, **kwargs):
            if getattr(statement, '_for_update_arg', None) is not None:
                statements.append(str(statement.compile(dialect=postgresql.dialect())))
            return scalar(db, statement, *args, **kwargs)
        def record_commit(db):
            commits.append(db)
            return commit(db)
        with patch.object(Session, 'scalar', capture), patch.object(Session, 'commit', record_commit):
            self.renewal(oid)
        self.assertEqual(len(commits), 1)
        product = next(i for i, sql in enumerate(statements) if 'FROM products' in sql)
        enterprise = next(i for i, sql in enumerate(statements) if 'FROM enterprises' in sql)
        combination = next(i for i, sql in enumerate(statements) if 'FROM subscription_combinations' in sql)
        self.assertLess(product, enterprise)
        self.assertLess(enterprise, combination)
        settlement = next(i for i, sql in enumerate(statements) if 'FROM settlement_rules' in sql)
        self.assertLess(settlement, enterprise)

    def test_grant_audit_measurement_notification_once_with_calendar_evidence(self):
        oid = self.create()
        self.deliver(oid)
        second = self.create()
        term = self.deliver(second, instant(2024, 2, 1))
        before = self.counts()
        self.deliver(second, instant(2024, 2, 2))
        self.assertEqual(self.counts(), before)
        with self.m.SessionLocal() as db:
            audit = db.scalar(select(self.m.AuditLog).where(self.m.AuditLog.order_id == second,
                              self.m.AuditLog.action == 'subscription_term_granted'))
            old, new = json.loads(audit.before_json), json.loads(audit.after_json)
            self.assertEqual(old['total_months'], 1)
            self.assertEqual(new['total_months'], 2)
            self.assertEqual(old['anchor_at'], new['anchor_at'])
            self.assertEqual(old['generation'], new['generation'])
            self.assertEqual(new['event_version'], term['id'])
            self.assertEqual(new['starts_at'], term['starts_at'].isoformat())
            measurement = db.scalar(select(self.m.SettlementMeasurement).where(
                self.m.SettlementMeasurement.order_id == second,
                self.m.SettlementMeasurement.measurement_type == 'subscription_term'))
            self.assertEqual(measurement.event_key, term['id'])
            evidence = json.loads(measurement.evidence_json)
            self.assertEqual(evidence['billing_effect'], 'none')
            message = db.scalar(select(self.Message).where(self.Message.event_key ==
                                 f"subscription_term_granted:{term['id']}"))
            self.assertIsNotNone(message)
            self.assertNotIn('cost', message.body)

    def test_each_grant_event_failure_rolls_back_term_and_all_side_effects(self):
        oid = self.create()
        for target in ('audit', 'record', 'notify_business_event'):
            with self.subTest(target=target):
                before = self.counts()
                with self.m.SessionLocal() as db:
                    order, user = db.get(self.m.Order, oid), db.get(self.m.User, 'ops')
                    mapping = self.m.settlement_metering if target == 'record' else self.ns
                    original = mapping[target]
                    def fail(*args, **kwargs):
                        original(*args, **kwargs)
                        db.flush()
                        raise RuntimeError('event failure')
                    with patch.dict(mapping, {target: fail}):
                        with self.assertRaisesRegex(RuntimeError, 'event failure'):
                            self.hooks['on_delivery_success'](db, order, user, at=instant())
                    db.rollback()
                self.assertEqual(self.counts(), before)

    def test_renewal_message_failure_restores_existing_dates_and_generation(self):
        self.deliver(self.create())
        oid = self.create(36)
        before = self.counts()
        with self.m.SessionLocal() as db:
            combination = db.scalar(select(self.C))
            previous = {key: getattr(combination, key) for key in (
                'anchor_at', 'generation', 'total_months', 'version_id', 'version_code', 'status', 'updated_at')}
        entitlement = self.entitlement(instant(2024, 2, 1))
        with self.m.SessionLocal() as db:
            order = db.scalar(select(self.m.Order).where(self.m.Order.id == oid).with_for_update())
            notify = self.ns['notify_business_event']
            def fail(*args, **kwargs):
                notify(*args, **kwargs)
                db.flush()
                raise RuntimeError('notification failed after message flush')
            with patch.dict(self.ns, {'notify_business_event': fail}):
                with self.assertRaisesRegex(RuntimeError, 'notification failed'):
                    self.hooks['on_delivery_success'](db, order, db.get(self.m.User, 'ops'), at=instant(2024, 2, 1))
            db.rollback()
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.entitlement(instant(2024, 2, 1)), entitlement)
        with self.m.SessionLocal() as db:
            combination = db.scalar(select(self.C))
            self.assertEqual({key: getattr(combination, key) for key in previous}, previous)

    def test_same_combination_key_different_source_returns_409(self):
        first = self.create()
        self.deliver(first)
        second = self.create()
        self.deliver(second, instant(2024, 2, 1))
        self.renewal(first)
        before = self.counts()
        self.renewal(second, expected=409)
        self.assertEqual(self.counts(), before)

    def test_database_idempotency_collision_is_409_and_rolls_back_new_order(self):
        from sqlalchemy.orm import Session
        oid = self.create()
        self.deliver(oid)
        self.renewal(oid)
        before = self.counts()
        original = Session.scalar
        def miss_request(db, statement, *args, **kwargs):
            descriptions = getattr(statement, 'column_descriptions', [])
            if descriptions and descriptions[0].get('entity') is self.R:
                return None
            return original(db, statement, *args, **kwargs)
        # Exercise the real unique constraint even if the precheck misses a row.
        with patch.object(Session, 'scalar', miss_request):
            self.renewal(oid, expected=409)
        self.assertEqual(self.counts(), before)

    def test_refunded_source_and_old_generation_not_renewable(self):
        oid = self.create()
        self.deliver(oid)
        with self.m.SessionLocal() as db:
            db.get(self.m.Order, oid).refunded_amount = 1
            db.commit()
        self.assertFalse(self.request('GET', f'/api/orders/{oid}/subscription')['item']['can_renew'])
        self.renewal(oid, expected=409)
        with self.m.SessionLocal() as db:
            db.get(self.m.Order, oid).refunded_amount = 0
            db.commit()
        self.deliver(self.create(), instant(2024, 3, 1))
        self.assertFalse(self.request('GET', f'/api/orders/{oid}/subscription')['item']['can_renew'])
        self.renewal(oid, expected=409)


if __name__ == '__main__':
    unittest.main()
