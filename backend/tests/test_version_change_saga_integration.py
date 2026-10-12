"""HTTP and SQLAlchemy integration coverage for durable version changes.

Run: python -m unittest discover -s backend/tests -p test_version_change_saga_integration.py -v
"""
import importlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import event, select

import test_subscription_terms as fixture


class VersionChangeSagaIntegrationTests(unittest.TestCase):
    setUpClass = classmethod(fixture.SubscriptionTermsTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.SubscriptionTermsTests.tearDownClass.__func__)
    tearDown = fixture.SubscriptionTermsTests.tearDown
    request = fixture.SubscriptionTermsTests.request
    create = fixture.SubscriptionTermsTests.create
    deliver = fixture.SubscriptionTermsTests.deliver

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
        self.catalog = patch.dict(self.m.storefront, {'versions_for': lambda db, product: [{'id': 'version'}, {'id': 'v2'}]})
        self.catalog.start()
        self.addCleanup(self.catalog.stop)
        self.clock = patch.object(self.m, 'now', wraps=self.m.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.quote_module = importlib.import_module(self.m.__package__ + '.version_change_quotes')
        self.saga_module = importlib.import_module(self.m.__package__ + '.version_change_saga')
        self.worker = importlib.import_module(self.m.__package__ + '.version_change_worker')

    def prepare(self, target_price=150):
        with self.m.SessionLocal() as db:
            target = db.get(self.m.ProductReleaseVersion, 'v2')
            target.price = target_price
            if target_price < target.cost:
                target.cost = 20
            db.commit()
        source = self.create(3, method='api')
        first = self.deliver(source)
        renewal = self.create(1, method='api')
        self.deliver(renewal, fixture.instant(2024, 2, 1))
        with self.m.SessionLocal() as db:
            for payment in db.scalars(select(self.m.Payment).where(
                    self.m.Payment.order_id.in_([source, renewal]))):
                payment.status = 'paid'
                payment.paid_at = self.m.now()
            db.commit()
        self.m.now.return_value = first['starts_at'] + (first['ends_at'] - first['starts_at']) / 2
        return source

    def quote(self, source, target='v2'):
        return self.request('POST', f'/api/orders/{source}/version-change-quote',
            json={'target_version_id': target})

    def request_change(self, source, quote, key='change-key'):
        return self.request('POST', f'/api/orders/{source}/version-changes', json={
            'quote_token': quote['quote_token'], 'idempotency_key': key})

    def proof(self, change_id, fingerprint='a'):
        return {'request_change_id': change_id, 'resource_change_id': change_id,
                'evidence_fingerprint': fingerprint * 64}

    def worker_once(self, at=None):
        with patch.object(self.worker.importlib, 'import_module', return_value=self.m):
            return self.worker.process_once(self.m.SessionLocal, worker_id='integration-worker',
                now=(lambda: at) if at else None)

    def worker_until(self, status, *, start=None, attempts=12):
        at = start or self.m.now()
        for _ in range(attempts):
            self.worker_once(at)
            with self.m.SessionLocal() as db:
                change = db.scalar(select(self.m.VersionChangeRequest))
                if change and change.status == status:
                    return change.id
            at += timedelta(seconds=11)
        self.fail(f'worker did not reach {status}')

    def test_request_replay_is_auth_bound_and_precedes_quote_expiry_validation(self):
        source = self.prepare()
        quote = self.quote(source)
        self.m.now.return_value += timedelta(seconds=1)
        other_quote = self.quote(source)
        body = {'quote_token': quote['quote_token'], 'idempotency_key': 'stable-request'}
        created = self.request('POST', f'/api/orders/{source}/version-changes', json=body)
        self.assertEqual(created['status'], 'pending_payment')
        self.m.now.return_value += timedelta(minutes=6)
        replay = self.request('POST', f'/api/orders/{source}/version-changes', json=body)
        self.assertEqual(replay['id'], created['id'])
        self.assertEqual(replay, created)
        self.request('POST', f'/api/orders/{source}/version-changes', expected=409,
            json={'quote_token': other_quote['quote_token'], 'idempotency_key': 'stable-request'})
        self.request('POST', f'/api/orders/{source}/version-changes', actor='member', expected=403,
            json=body)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.scalar(select(self.m.func.count()).select_from(self.m.VersionChangeRequest)), 1)

    def test_unpaid_upgrade_expires_to_cancelled_and_void_ledger(self):
        source = self.prepare()
        quote = self.quote(source)
        created = self.request_change(source, quote, 'expires-key')
        change_id = created['id']
        self.request('POST', f'/api/version-changes/{change_id}/start-payment', json={'reason':'fixture'})
        expired_at = self.m.now.return_value + timedelta(minutes=6)
        self.assertTrue(self.worker_once(expired_at))
        with self.m.SessionLocal() as db:
            change = db.get(self.m.VersionChangeRequest, change_id)
            funding = db.get(self.m.Order, change.funding_order_id)
            payment = db.scalar(select(self.m.Payment).where(self.m.Payment.order_id == funding.id))
            ledger = db.scalars(select(self.m.version_change_ledger['Model']).where(
                self.m.version_change_ledger['Model'].change_id == change_id)).all()
            self.assertEqual(change.status, 'cancelled')
            self.assertEqual(funding.payment_status, 'cancelled')
            self.assertEqual(payment.status, 'cancelled')
            self.assertTrue(ledger and all(row.status == 'void' for row in ledger))
            self.assertTrue(db.scalar(select(self.m.AuditLog.id).where(
                self.m.AuditLog.target_id == change_id,
                self.m.AuditLog.action == 'expire_version_change_payment')))

    def test_payment_confirmation_rejects_expired_quote_but_keeps_paid_replay_idempotent(self):
        source = self.prepare()
        quote = self.quote(source)
        change = self.request_change(source, quote, 'late-confirm-key')
        change_id = change['id']
        started = self.request('POST', f'/api/version-changes/{change_id}/start-payment',
            json={'reason':'fixture'})
        funding_id = started['funding_order_id']
        with self.m.SessionLocal() as db:
            request = db.get(self.m.VersionChangeRequest, change_id)
            request.next_retry_at = self.m.now() - timedelta(seconds=1)
            db.commit()

        self.request('POST', f'/api/version-changes/{change_id}/confirm-payment', expected=409,
            json={'reason':'late confirmation'})
        self.request('POST', f'/api/orders/{funding_id}/transition', expected=409,
            json={'action':'confirm_payment'})
        with self.m.SessionLocal() as db:
            request = db.get(self.m.VersionChangeRequest, change_id)
            funding = db.get(self.m.Order, funding_id)
            payment = db.scalar(select(self.m.Payment).where(self.m.Payment.order_id == funding_id))
            self.assertEqual(request.status, 'pending_payment')
            self.assertEqual(funding.payment_status, 'paying')
            self.assertEqual(payment.status, 'paying')
            funding.payment_status = 'paid'
            funding.paid_amount = funding.amount
            payment.status = 'paid'
            request.status = 'resource_pending'
            db.commit()

        replay = self.request('POST', f'/api/orders/{funding_id}/transition',
            json={'action':'confirm_payment'})
        self.assertEqual(replay['payment_status'], 'paid')
        with self.m.SessionLocal() as db:
            self.assertEqual(db.scalar(select(self.m.func.count()).select_from(self.m.Payment).where(
                self.m.Payment.order_id == funding_id)), 1)

    def test_ordinary_refund_guard_scopes_combination_lock_to_monthly_enterprise_orders(self):
        source = self.prepare()
        guard = self.m.version_change_saga['require_order_transition']
        lock = self.m.subscription_terms['lock_combination']
        calls = []

        def tracked_lock(*args, **kwargs):
            calls.append(args[1:])
            return lock(*args, **kwargs)

        with self.m.SessionLocal() as db, patch.dict(self.m.subscription_terms,
                {'lock_combination': tracked_lock}):
            nonmonthly = SimpleNamespace(id='offline-order', business_type='',
                buyer_enterprise_id='buyer_tenant', product_id='product', delivery_method_snapshot='file')
            guard(db, None, nonmonthly, 'approve_refund')
            no_enterprise = SimpleNamespace(id='orphan-order', business_type='',
                buyer_enterprise_id=None, product_id='product', delivery_method_snapshot='api')
            guard(db, None, no_enterprise, 'approve_refund')
            self.assertEqual(calls, [])

            monthly = db.get(self.m.Order, source)
            guard(db, None, monthly, 'approve_refund')
            self.assertTrue(calls)

    def test_transition_row_lock_order_for_monthly_and_version_funding(self):
        source = self.prepare()
        observed = []

        def capture_locks(call):
            def observe(state):
                statement = state.statement
                if not state.is_select or getattr(statement, '_for_update_arg', None) is None:
                    return
                entity = statement.column_descriptions[0].get('entity')
                if entity is not None:
                    observed.append(entity.__tablename__)

            event.listen(self.m.SessionLocal.class_, 'do_orm_execute', observe)
            try:
                return call()
            finally:
                event.remove(self.m.SessionLocal.class_, 'do_orm_execute', observe)

        headers = {'Authorization': 'Bearer ' + self.tokens['buyer']}
        response = capture_locks(lambda: self.client.post(
            f'/api/orders/{source}/transition', headers=headers, json={'action': 'cancel_order'}))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertLess(observed.index('products'), observed.index('orders'))
        self.assertLess(observed.index('orders'), observed.index('subscription_combinations'))

        observed.clear()
        quote = self.quote(source)
        change = self.request_change(source, quote, 'lock-order-key')
        started = self.request('POST', f"/api/version-changes/{change['id']}/start-payment",
            json={'reason':'lock-order test'})
        response = capture_locks(lambda: self.client.post(
            f"/api/orders/{started['funding_order_id']}/transition", headers=headers,
            json={'action':'confirm_payment'}))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertLess(observed.index('version_change_requests'), observed.index('orders'))
        self.assertLess(observed.index('orders'), observed.index('payments'))

    def test_paid_upgrade_reaches_verified_resource_terms_and_posted_ledger(self):
        source = self.prepare()
        quote = self.quote(source)
        created = self.request_change(source, quote)
        change_id = created['id']
        public_keys = {'id','change_no','source_order_id','combination_id','enterprise_id','kind','status',
            'current_version_code','target_version_code','net_difference','payment_amount','refund_amount',
            'currency','funding_order_id','funding_order_no','payment_status','refund_status','retry_count',
            'last_error','created_at','updated_at','completed_at'}
        self.assertEqual(set(created), public_keys)
        started = self.request('POST', f'/api/version-changes/{change_id}/start-payment', actor='buyer', json={'reason':'fixture'})
        self.assertEqual(started['payment_status'], 'paying')
        paid = self.request('POST', f'/api/version-changes/{change_id}/confirm-payment', actor='buyer', json={'reason':'fixture'})
        self.assertEqual(paid['payment_status'], 'paid')
        self.request('POST', f'/api/version-changes/{change_id}/confirm-payment', actor='buyer', json={'reason':'replay'})
        calls = []
        resources = self.m.version_change_resources
        with patch.dict(resources, {
            'prepare': lambda db, change: {'resource_change_id': change.id, 'operation': 'replace-version'},
            'apply': lambda db, change, direction='target': calls.append(direction) or self.proof(change.id),
        }):
            self.worker_until('completed')
        with self.m.SessionLocal() as db:
            change = db.get(self.m.VersionChangeRequest, change_id)
            fund = db.scalar(select(self.m.VersionChangeFund).where(
                self.m.VersionChangeFund.change_id == change_id, self.m.VersionChangeFund.kind == 'payment'))
            funding_payment = db.get(self.m.Payment, fund.payment_id)
            ledger = db.scalars(select(self.m.version_change_ledger['Model']).where(
                self.m.version_change_ledger['Model'].change_id == change_id)).all()
            combo = db.get(self.m.subscription_terms['Combination'], change.combination_id)
            self.assertEqual(change.status, 'completed')
            self.assertTrue(change.resource_applied)
            self.assertEqual(funding_payment.status, 'paid')
            self.assertEqual(funding_payment.amount, quote['payment_amount'])
            self.assertEqual(combo.version_code, 'v2')
            self.assertTrue(ledger and all(row.status == 'posted' and row.posted_at for row in ledger))
            self.assertEqual(db.scalar(select(self.m.Order).where(
                self.m.Order.id == change.funding_order_id)).business_type, 'version_change_payment')
            self.assertEqual(db.scalar(select(self.m.Order).where(
                self.m.Order.id == change.funding_order_id)).snapshot_version, 0)
            self.assertEqual(db.scalar(select(self.m.func.count()).select_from(self.m.OrderStateLog).where(
                self.m.OrderStateLog.order_id == change.funding_order_id,
                self.m.OrderStateLog.domain == 'delivery')), 0)
            self.assertTrue(db.scalar(select(self.m.AuditLog.id).where(
                self.m.AuditLog.target_id == change_id,
                self.m.AuditLog.action == 'apply_version_change_resource')))
        self.assertEqual(calls, ['target'])
        items = self.request('GET', f'/api/orders/{source}/version-changes')['items']
        self.assertEqual([row['id'] for row in items], [change_id])
        self.assertNotIn('prepared_json', json.dumps(items))

    def test_downgrade_refund_insert_failure_keeps_verified_low_revisions(self):
        source = self.prepare(target_price=50)
        quote = self.quote(source)
        self.assertEqual(quote['action'], 'downgrade')
        created = self.request_change(source, quote, 'downgrade-key')
        change_id = created['id']
        resources = self.m.version_change_resources
        with patch.dict(resources, {
            'prepare': lambda db, change: {'resource_change_id': change.id, 'operation': 'replace-version'},
            'apply': lambda db, change, direction='target': self.proof(change.id),
        }):
            self.worker_once()
            self.worker_once()
            with self.m.SessionLocal() as db:
                change = db.get(self.m.VersionChangeRequest, change_id)
                self.assertEqual(change.status, 'refund_pending')
                self.assertTrue(change.resource_applied)
                combo = db.get(self.m.subscription_terms['Combination'], change.combination_id)
                terms = db.scalars(select(self.m.subscription_terms['Term']).where(
                    self.m.subscription_terms['Term'].combination_id == combo.id)).all()
                self.assertEqual(combo.version_code, 'v2')
                self.assertTrue(terms and all(term.version_code == 'v2' for term in terms))
                source_row = db.get(self.m.Order, source)
                original_version = source_row.product_version_code
                snapshots_before = (source_row.economic_snapshot_json, source_row.delivery_snapshot_json)

            def fail_refund(_mapper, _connection, _target):
                raise RuntimeError('simulated local refund persistence fault')

            event.listen(self.m.Refund, 'before_insert', fail_refund)
            try:
                self.worker_once(self.m.now() + timedelta(seconds=1))
            finally:
                event.remove(self.m.Refund, 'before_insert', fail_refund)
            with self.m.SessionLocal() as db:
                change = db.get(self.m.VersionChangeRequest, change_id)
                self.assertEqual(change.status, 'refund_pending')
                self.assertEqual(change.retries, 1)
                self.assertEqual(change.last_error, 'RuntimeError')
                self.assertTrue(db.scalar(select(self.m.AuditLog.id).where(
                    self.m.AuditLog.target_id == change_id,
                    self.m.AuditLog.action == 'retry_version_change_phase')))
                combo = db.get(self.m.subscription_terms['Combination'], change.combination_id)
                self.assertEqual(combo.version_code, 'v2')
                source_row = db.get(self.m.Order, source)
                self.assertEqual(source_row.product_version_code, original_version)
                self.assertEqual((source_row.economic_snapshot_json, source_row.delivery_snapshot_json), snapshots_before)
                self.assertEqual(source_row.refunded_amount, 0)
                self.assertEqual(db.scalar(select(self.m.func.count()).select_from(self.m.Refund).where(
                    self.m.Refund.reason.like(f'%{change_id}%'))), 0)
                revisions = db.scalars(select(self.m.subscription_terms['Revision']).where(
                    self.m.subscription_terms['Revision'].change_id == change_id)).all()
                self.assertTrue(revisions)
                effective = self.m.subscription_terms['effective_snapshot'](
                    db, source_row, db.get(self.m.subscription_terms['Term'], revisions[0].term_id))
                self.assertEqual(effective['version_code'], 'v2')

    def test_failed_upgrade_resource_is_verified_restored_and_funding_refunded(self):
        source = self.prepare()
        quote = self.quote(source)
        created = self.request_change(source, quote, 'compensate-key')
        change_id = created['id']
        self.request('POST', f'/api/version-changes/{change_id}/start-payment', json={'reason':'fixture'})
        self.request('POST', f'/api/version-changes/{change_id}/confirm-payment', json={'reason':'fixture'})
        directions = []

        def apply(db, change, direction='target'):
            directions.append(direction)
            if direction == 'target':
                raise RuntimeError('private resource credential must not persist')
            return self.proof(change.id, 'b')

        resources = self.m.version_change_resources
        with patch.dict(resources, {
            'prepare': lambda db, change: {'resource_change_id': change.id, 'operation': 'replace-version'},
            'apply': apply,
        }):
            at = self.m.now()
            self.worker_once(at)  # paid request becomes resource_pending
            self.worker_once(at)  # persist replay-safe resource plan
            self.worker_once(at)  # attempt 1
            self.worker_once(at + timedelta(seconds=11))  # attempt 2
            self.worker_once(at + timedelta(seconds=22))  # attempt 3, durable compensation phase
            with self.m.SessionLocal() as db:
                change = db.get(self.m.VersionChangeRequest, change_id)
                self.assertEqual(change.status, 'compensating')
                self.assertEqual(change.retries, 3)
                self.assertTrue(change.compensation_started)
                self.assertEqual(change.compensation_retries, 0)
                self.assertEqual(change.last_error, 'RuntimeError')
                self.assertNotIn('credential', change.last_error)
            self.worker_once(at + timedelta(seconds=34))

        with self.m.SessionLocal() as db:
            change = db.get(self.m.VersionChangeRequest, change_id)
            self.assertEqual(change.status, 'compensated')
            self.assertEqual(directions, ['target', 'target', 'target', 'original'])
            combo = db.get(self.m.subscription_terms['Combination'], change.combination_id)
            self.assertEqual(combo.version_code, 'v1')
            fund_payment = db.scalar(select(self.m.VersionChangeFund).where(
                self.m.VersionChangeFund.change_id == change_id,
                self.m.VersionChangeFund.kind == 'payment'))
            refund_fund = db.scalar(select(self.m.VersionChangeFund).where(
                self.m.VersionChangeFund.change_id == change_id,
                self.m.VersionChangeFund.kind == 'refund'))
            payment = db.get(self.m.Payment, fund_payment.payment_id)
            refund = db.get(self.m.Refund, refund_fund.refund_id)
            funding_order = db.get(self.m.Order, payment.order_id)
            self.assertEqual(refund.payment_id, payment.id)
            self.assertEqual(refund.amount, quote['payment_amount'])
            self.assertEqual(payment.status, 'refunded')
            self.assertEqual(funding_order.refunded_amount, quote['payment_amount'])
            ledger = db.scalars(select(self.m.version_change_ledger['Model']).where(
                self.m.version_change_ledger['Model'].change_id == change_id)).all()
            self.assertTrue(ledger and all(row.status == 'void' for row in ledger))
            self.assertTrue(db.scalar(select(self.m.AuditLog.id).where(
                self.m.AuditLog.target_id == change_id,
                self.m.AuditLog.action == 'compensate_version_change')))


if __name__ == '__main__':
    unittest.main()
