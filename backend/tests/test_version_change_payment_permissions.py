"""Approved immediate funding creation and platform-only confirmation contracts."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import select

import test_version_change_saga_integration as fixtures


class VersionChangePaymentPermissionsTests(unittest.TestCase):
    Fixture = fixtures.VersionChangeSagaIntegrationTests
    setUpClass = classmethod(Fixture.setUpClass.__func__)
    tearDownClass = classmethod(Fixture.tearDownClass.__func__)
    setUp = Fixture.setUp
    tearDown = Fixture.tearDown
    request = Fixture.request
    create = Fixture.create
    deliver = Fixture.deliver
    prepare = Fixture.prepare
    quote = Fixture.quote
    request_change = Fixture.request_change

    def test_creation_freezes_funding_and_all_future_term_end(self):
        source = self.prepare()
        quote = self.quote(source)
        created = self.request_change(source, quote)
        self.assertIsNotNone(created['funding_order_id'])
        self.assertEqual(created['payment_status'], 'unpaid')
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, created['funding_order_id'])
            payments = db.scalars(select(self.m.Payment).where(self.m.Payment.order_id == order.id)).all()
            self.assertEqual(len(payments), 1)
            self.assertEqual(payments[0].status, 'unpaid')
            funds = db.scalars(select(self.m.VersionChangeFund).where(
                self.m.VersionChangeFund.change_id == created['id'], self.m.VersionChangeFund.kind == 'payment')).all()
            self.assertEqual(len(funds), 1)
            self.assertEqual(funds[0].payment_id, payments[0].id)
            if hasattr(self.m.Order, 'order_type'):
                self.assertEqual(order.order_type, 'upgrade')
                self.assertEqual(order.service_starts_at, order.created_at)
                self.assertEqual(order.service_ends_at.replace(tzinfo=timezone.utc), max(datetime.fromisoformat(s['ends_at']) for s in quote['segments']))
                self.assertIsNone(order.service_effective_at)
        replay = self.request_change(source, quote)
        self.assertEqual(replay['funding_order_id'], created['funding_order_id'])
        for _ in range(2):
            started = self.request('POST', f"/api/version-changes/{created['id']}/start-payment", json={})
            self.assertEqual(started['payment_status'], 'paying')
            self.assertEqual(started['funding_order_id'], created['funding_order_id'])

    def test_only_platform_admin_and_operator_confirm_on_both_routes(self):
        source = self.prepare()
        created = self.request_change(source, self.quote(source))
        routes = [(f"/api/version-changes/{created['id']}/confirm-payment", {}),
                  (f"/api/orders/{created['funding_order_id']}/transition", {'action': 'confirm_payment'})]
        for path, body in routes:
            for actor in ('buyer', 'member', 'finance', 'provider', 'outsider'):
                self.request('POST', path, actor=actor, expected=403, json=body)
        for actor in ('ops', 'platform'):
            for path, body in routes:
                self.request('POST', path, actor=actor, json=body)
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, created['funding_order_id'])
            self.assertEqual(order.business_type, 'version_change_payment')
            self.assertEqual(order.payment_status, 'paid')
            logs = db.scalars(select(self.m.OrderStateLog).where(
                self.m.OrderStateLog.order_id == order.id, self.m.OrderStateLog.action == 'confirm_payment')).all()
            self.assertEqual(len(logs), 1)

    def test_start_payment_requires_enterprise_superadmin(self):
        source = self.prepare()
        created = self.request_change(source, self.quote(source))
        for actor in ('member', 'buyer_admin', 'finance', 'ops', 'platform', 'outsider'):
            self.request('POST', f"/api/version-changes/{created['id']}/start-payment",
                         actor=actor, expected=403, json={})
        self.request('POST', f"/api/orders/{created['funding_order_id']}/transition",
                     actor='buyer', json={'action': 'start_payment'})
        self.request('POST', f"/api/orders/{created['funding_order_id']}/transition",
                     actor='buyer', json={'action': 'start_payment'})

    def test_downgrade_creates_negative_operation_without_payment(self):
        source = self.prepare(target_price=50)
        quote = self.quote(source)
        created = self.request_change(source, quote)
        self.assertEqual(created['kind'], 'downgrade')
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, created['funding_order_id'])
            self.assertEqual(float(order.amount), -quote['refund_amount'])
            self.assertEqual(db.scalars(select(self.m.Payment).where(self.m.Payment.order_id == order.id)).all(), [])
            if hasattr(self.m.Order, 'order_type'):
                self.assertEqual(order.order_type, 'downgrade')
            change = db.get(self.m.VersionChangeRequest, created['id'])
            self.m.version_change_saga['process_downgrade_refunds'](db, change)
            db.flush()
            before = len(db.scalars(select(self.m.Refund)).all())
            self.m.version_change_saga['process_downgrade_refunds'](db, change)
            db.flush()
            self.assertEqual(len(db.scalars(select(self.m.Refund)).all()), before)

    def test_zero_net_operation_type_uses_monthly_price_direction(self):
        for price, expected in ((150, 'upgrade'), (50, 'downgrade'), (100, 'downgrade')):
            with self.subTest(price=price):
                source = self.prepare(target_price=price)
                quote = self.quote(source)
                original = self.m.version_change_quotes['prepare']

                def zero_net(*args, **kwargs):
                    result = original(*args, **kwargs)
                    result['public'].update(action='no_payment', net_difference=0, payment_amount=0, refund_amount=0)
                    return result

                with patch.dict(self.m.version_change_quotes, {'prepare': zero_net}):
                    created = self.request_change(source, quote, key=f'zero-{price}')
                with self.m.SessionLocal() as db:
                    order = db.get(self.m.Order, created['funding_order_id'])
                    self.assertEqual(order.order_type, expected)
                    self.assertEqual(order.amount, 0)
                    self.assertIsNone(db.scalar(select(self.m.Payment).where(self.m.Payment.order_id == order.id)))
                    change = db.get(self.m.VersionChangeRequest, created['id'])
                    change.status = 'cancelled'
                    db.commit()

    def test_ordinary_payment_hooks_restrict_confirmation(self):
        source = self.create(1, delivered=False)
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, source)
            authorize = self.m.order_workflow['authorize_transition']
            for actor in ('buyer', 'buyer_admin', 'finance', 'provider', 'outsider'):
                with self.subTest(actor=actor), self.assertRaises(self.m.HTTPException) as caught:
                    authorize(db, db.get(self.m.User, actor), order, 'confirm_payment')
                self.assertEqual(caught.exception.status_code, 403)
            for actor in ('ops', 'platform'):
                self.assertTrue(authorize(db, db.get(self.m.User, actor), order, 'confirm_payment'))


if __name__ == '__main__':
    unittest.main()
