"""Focused list contracts on isolated SQLite; no external payments or live data."""
from datetime import date, datetime, timezone
import unittest

from fastapi import HTTPException
from sqlalchemy import select

import test_refund_workflow as fixture
from app.listing_common import contains, dates


class AfterSalesRefundListTests(unittest.TestCase):
    setUpClass = classmethod(fixture.RefundWorkflowTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.RefundWorkflowTests.tearDownClass.__func__)
    setUp = fixture.RefundWorkflowTests.setUp
    tearDown = fixture.RefundWorkflowTests.tearDown
    request = fixture.RefundWorkflowTests.request
    create_order = fixture.RefundWorkflowTests.create_order
    create_refund = fixture.RefundWorkflowTests.create

    def test_refund_filters_and_party_labels_do_not_leak_cost(self):
        refund = self.create_refund()
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, self.order_id)
            provider = db.get(self.m.Enterprise, order.provider_enterprise_id)
            provider.name = 'Provider Unique Name'
            order.product_name, order.buyer_name = 'Dataset Unique Name', 'Buyer Unique Name'
            db.commit()
        result = self.request('GET', '/api/refunds?page=1&page_size=10&product=Unique&provider=Provider&buyer=Buyer')
        self.assertEqual(result['total'], 1)
        row = result['items'][0]
        self.assertEqual((row['provider_name'], row['buyer_name'], row['product_name']),
                         ('Provider Unique Name', 'Buyer Unique Name', 'Dataset Unique Name'))
        self.assertNotIn('cost', str(row))
        self.assertEqual(self.request('GET', '/api/refunds?provider=NoSuchParty')['total'], 0)
        self.assertEqual(self.request('GET', '/api/refunds?status=completed')['total'], 0)
        self.assertEqual(self.request('GET', '/api/refunds?refund_id=' + refund['refund_no'])['total'], 1)
        self.request('GET', '/api/refunds?status=not-a-state', expected=422)
        self.request('GET', '/api/refunds?page_size=101', expected=422)
        self.request('GET', '/api/refunds?start=2026-10-16&end=2026-10-15', expected=422)

    def test_after_sales_filter_and_paging_preserve_old_shape(self):
        ticket = self.request('POST', '/api/after-sales', actor='buyer', json={
            'order_id': self.order_id, 'description': 'List test issue', 'idempotency_key': 'list-ticket-00001'})
        path = '/api/after-sales'
        legacy = self.request('GET', path)
        self.assertNotIn('total', legacy)
        result = self.request('GET', path + '?page=1&page_size=10&status=awaiting_response')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['order_id'], self.order_id)
        self.assertTrue(result['items'][0]['order_no'])
        self.assertEqual(self.request('GET', path + '?page=999&page_size=10')['page'], 1)
        self.assertEqual(self.request('GET', path + '?page=1&ticket_id=' + ticket['ticket_no'])['total'], 1)
        self.assertEqual(self.request('GET', path + '?page=1&assignee=NoSuchOwner')['total'], 0)
        self.request('GET', path + '?page=1&status=invalid', expected=422)
        self.request('GET', path + '?page=1&enterprise_id=other_tenant', expected=403)

    def test_literal_wildcards_and_beijing_date_edges(self):
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, self.order_id)
            order.product_name = 'contains_underscore'
            db.commit()
            self.assertEqual(len(db.scalars(select(self.m.Order).where(contains(self.m.Order.product_name, '%'))).all()), 0)
            self.assertEqual(len(db.scalars(select(self.m.Order).where(contains(self.m.Order.product_name, '_'))).all()), 1)
        statement = dates(select(self.m.Order), self.m.Order.created_at, date(2026, 10, 16), date(2026, 10, 16))
        values = list(statement.compile().params.values())
        self.assertEqual(values, [datetime(2026, 10, 15, 16, tzinfo=timezone.utc), datetime(2026, 10, 16, 16, tzinfo=timezone.utc)])
        with self.assertRaises(HTTPException):
            dates(select(self.m.Order), self.m.Order.created_at, None, date.max)


if __name__ == '__main__':
    unittest.main()
