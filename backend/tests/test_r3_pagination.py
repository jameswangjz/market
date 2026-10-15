"""Existing paginated HTTP surfaces against isolated production models."""
from datetime import timedelta
import unittest

import test_message_business_events as fixture


class R3PaginationTests(unittest.TestCase):
    setUpClass = classmethod(fixture.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.BusinessRoutesHTTPTests.tearDownClass.__func__)
    setUp = fixture.BusinessRoutesHTTPTests.setUp
    tearDown = fixture.BusinessRoutesHTTPTests.tearDown
    request = fixture.BusinessRoutesHTTPTests.request

    def assert_pages(self, path, expected_ids, *, params=None, actor='buyer'):
        for size in (10, 12, 20, 50, 100):
            with self.subTest(path=path, size=size):
                seen = []
                pages = (len(expected_ids) + size - 1) // size
                for page in range(1, pages + 2):
                    query = dict(params or {}, page=page, page_size=size)
                    result = self.request('GET', path, actor=actor, params=query)
                    self.assertEqual(result['total'], len(expected_ids))
                    ids = [item['id'] for item in result['items']]
                    self.assertEqual(ids, expected_ids[(page - 1) * size:page * size])
                    seen.extend(ids)
                self.assertEqual(seen, expected_ids)
                repeat = self.request('GET', path, actor=actor,
                    params=dict(params or {}, page=1, page_size=size))
                self.assertEqual([item['id'] for item in repeat['items']], expected_ids[:size])

    def test_storefront_pages_count_only_matching_ready_published_products(self):
        m = self.m
        instant = m.now()
        ids = [f'r3-product-{i:03}' for i in range(105)]
        with m.SessionLocal() as db:
            for pid in reversed(ids + ['r3-hidden', 'r3-unready', 'r3-filtered']):
                db.add(m.Product(id=pid, enterprise_id='provider_tenant', name='R3 Pagination ' + pid,
                    product_type='dataset' if pid != 'r3-filtered' else 'training',
                    delivery_method='file', status='draft' if pid == 'r3-hidden' else 'published',
                    updated_at=instant))
            db.flush()
            db.add_all([m.ProductReleaseVersion(id=pid + '-v1', product_id=pid, version_code='v1',
                price=100, cost=10, status='active') for pid in ids + ['r3-hidden', 'r3-filtered']])
            db.commit()
        self.assert_pages('/api/storefront/products', ids,
            params={'q': 'R3 Pagination', 'product_type': 'dataset'})

    def seed_messages(self):
        m, Message, Receipt = self.m, self.Message, self.Receipt
        instant = m.now()
        with m.SessionLocal() as db:
            for index in reversed(range(108)):
                mid = f'r3-message-{index:03}'
                db.add(Message(id=mid, event_key=mid, title='R3 Pagination', category='r3-page',
                    sender_id='provider' if index == 106 else 'buyer',
                    tenant_id='other_tenant' if index == 105 else 'buyer_tenant',
                    status='draft' if index == 107 else 'sent', sent_at=instant,
                    expires_at=instant + timedelta(days=1)))
            db.flush()
            for index in reversed(range(108)):
                db.add(Receipt(id=f'r3-receipt-{index:03}', message_id=f'r3-message-{index:03}',
                    user_id='provider' if index == 106 else 'buyer', created_at=instant))
            db.commit()

    def test_inbox_and_sent_pages_are_stable_and_tenant_recipient_scoped(self):
        self.seed_messages()
        params = {'enterprise_id': 'buyer_tenant', 'category': 'r3-page'}
        self.assert_pages('/api/notifications', [f'r3-receipt-{i:03}' for i in reversed(range(105))],
            params=params)
        self.assert_pages('/api/notifications/sent', [f'r3-message-{i:03}' for i in reversed(range(105))],
            params=params)
        for path in ('/api/notifications', '/api/notifications/sent'):
            self.request('GET', path, params={'enterprise_id': 'other_tenant'}, expected=403)

    def seed_audits(self):
        m = self.m
        instant = m.now()
        ids = [f'r3-audit-{i:03}' for i in range(105)]
        with m.SessionLocal() as db:
            for aid in reversed(ids + ['r3-foreign']):
                db.add(m.AuditLog(id=aid, actor='test', action='pagination', category='r3-page',
                    target_type='pagination_test', target_id='shared-object', created_at=instant,
                    tenant_id='other_tenant' if aid == 'r3-foreign' else 'buyer_tenant',
                    detail='PRIVATE COST', before_json='{"secret":"PRIVATE"}',
                    after_json='{"upstream":"PRIVATE"}'))
            db.commit()
        return ids

    def test_enterprise_audit_list_and_timeline_count_only_visible_events(self):
        ids = self.seed_audits()
        params = {'enterprise_id': 'buyer_tenant', 'category': 'r3-page'}
        self.assert_pages('/api/audit-logs', list(reversed(ids)), params=params, actor='buyer_admin')
        path = '/api/audit-logs/' + ids[0] + '/timeline'
        self.assert_pages(path, ids, params={'enterprise_id': 'buyer_tenant'}, actor='buyer_admin')
        result = self.request('GET', path, actor='buyer_admin', params={'enterprise_id': 'buyer_tenant'})
        allowed = {'id', 'action', 'target_type', 'target_id', 'result', 'category',
                   'order_id', 'order_no', 'created_at'}
        for row in [result['event'], *result['items']]:
            self.assertEqual(set(row), allowed)
        self.assertNotIn('PRIVATE', str(result))
        self.request('GET', '/api/audit-logs/r3-foreign/timeline', actor='buyer_admin', expected=404)
        self.request('GET', path, actor='outsider', expected=404)
        self.request('GET', path, actor='buyer_admin', params={'enterprise_id': 'other_tenant'}, expected=403)
        with self.m.SessionLocal() as db:
            member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == 'buyer_admin'))
            member.role = 'member'
            db.commit()
        for endpoint in ('/api/audit-logs', path):
            self.request('GET', endpoint, actor='buyer_admin', expected=403)

    def test_platform_audit_list_and_timeline_keep_full_authorized_scope(self):
        ids = self.seed_audits() + ['r3-foreign']
        self.assert_pages('/api/audit-logs', sorted(ids, reverse=True),
            params={'category': 'r3-page'}, actor='finance')
        self.assert_pages('/api/audit-logs/r3-audit-000/timeline', sorted(ids), actor='finance')

    def test_order_timeline_uses_company_business_side_and_active_membership(self):
        oid = fixture.BusinessRoutesHTTPTests.create_order(self)
        with self.m.SessionLocal() as db:
            db.add(self.m.AuditLog(id='r3-order-event', actor='test', action='pagination',
                target_type='order', target_id=oid, order_id=oid, tenant_id='other_tenant'))
            db.commit()
        path = '/api/audit-logs/r3-order-event/timeline'
        for actor, enterprise_id, allowed, forbidden in (
                ('buyer_admin', 'buyer_tenant', 'buyer', 'provider'),
                ('provider', 'provider_tenant', 'provider', 'buyer')):
            result = self.request('GET', path, actor=actor,
                params={'enterprise_id': enterprise_id, 'business_view': allowed})
            self.assertEqual(result['total'], 1)
            self.request('GET', path, actor=actor, expected=404,
                params={'enterprise_id': enterprise_id, 'business_view': forbidden})
        with self.m.SessionLocal() as db:
            member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == 'buyer_admin'))
            member.status = 'disabled'
            db.commit()
        self.request('GET', path, actor='buyer_admin', expected=403,
            params={'enterprise_id': 'buyer_tenant'})

    def test_all_surfaces_validate_range_and_keep_legacy_sizes(self):
        self.seed_audits()
        paths = ('/api/storefront/products', '/api/notifications', '/api/notifications/sent',
                 '/api/audit-logs', '/api/audit-logs/r3-audit-000/timeline')
        for path in paths:
            for params in ({'page': 0}, {'page': -1}, {'page': 'bad'}, {'page_size': 0},
                           {'page_size': -1}, {'page_size': 101}, {'page_size': 'bad'}):
                with self.subTest(path=path, params=params):
                    self.request('GET', path, actor='platform', params=params, expected=422)
            for size in (1, 12, 100):
                self.request('GET', path, actor='platform', params={'page_size': size})


if __name__ == '__main__':
    unittest.main()
