import csv
import io
import unittest
from datetime import timedelta

import test_message_business_events as business
from sqlalchemy import insert


class AuditAcceptanceTests(unittest.TestCase):
    setUp = business.BusinessRoutesHTTPTests.setUp
    tearDown = business.BusinessRoutesHTTPTests.tearDown
    request = business.BusinessRoutesHTTPTests.request
    create_order = business.BusinessRoutesHTTPTests.create_order

    @classmethod
    def setUpClass(cls):
        cls.m = business.load_isolated_main()

    @classmethod
    def tearDownClass(cls):
        business.BusinessRoutesHTTPTests.tearDownClass.__func__(cls)

    def seed(self):
        first, second = self.create_order(), self.create_order()
        with self.m.SessionLocal() as db:
            number = db.get(self.m.Order, first).order_no
            db.add_all([
                self.m.AuditLog(id='a1', actor='finance', action='confirmed', target_type='settlement',
                                target_id='SET-A', order_id=first, batch_no='BATCH-A', category='settlement',
                                business_domain='settlement', detail='=HYPERLINK("x")',
                                before_json='{"status":"draft"}', after_json='{"status":"locked"}'),
                self.m.AuditLog(id='a2', actor='finance', action='paid', target_type='settlement',
                                target_id='SET-A', order_id=first, batch_no='BATCH-A', category='settlement_payment'),
                self.m.AuditLog(id='other', actor='finance', action='paid', target_type='settlement',
                                target_id='SET-B', order_id=second, batch_no='BATCH-A', category='settlement_payment'),
            ])
            db.commit()
        return first, number

    def test_order_number_and_internal_id_filters_agree(self):
        oid, number = self.seed()
        a = self.request('GET', '/api/audit-logs', actor='finance', params={'order_id': oid})
        b = self.request('GET', '/api/audit-logs', actor='finance', params={'order_id': number})
        self.assertEqual([x['id'] for x in a['items']], [x['id'] for x in b['items']])
        self.assertTrue(all(x['order_no'] == number for x in b['items']))

    def test_timeline_never_includes_other_order_in_same_batch(self):
        self.seed()
        data = self.request('GET', '/api/audit-logs/a1/timeline', actor='finance')
        self.assertEqual([x['id'] for x in data['items']], ['a1', 'a2'])
        with self.m.SessionLocal() as db:
            self.assertEqual(db.scalar(self.select(self.m.AuditLog.action).where(
                self.m.AuditLog.target_id == 'a1')), 'view_audit_timeline')

    def test_export_csv_quoting_formula_safety_and_audit(self):
        oid, _ = self.seed()
        response = self.client.get('/api/audit-logs/export', params={'order_id': oid},
                                   headers={'Authorization': 'Bearer ' + self.tokens['finance']})
        self.assertEqual(response.status_code, 200, response.text)
        rows = list(csv.DictReader(io.StringIO(response.text.lstrip('\ufeff'))))
        record = next(x for x in rows if x['action'] == 'confirmed')
        self.assertTrue(record['detail'].startswith("'="))
        self.assertEqual(record['before'], '{"status": "draft"}')
        with self.m.SessionLocal() as db:
            exported = db.scalar(self.select(self.m.AuditLog).where(self.m.AuditLog.action == 'export_audit_logs'))
            self.assertTrue(exported.request_id)

    def test_view_and_export_reject_ordinary_enterprise_and_review_roles(self):
        self.seed()
        for actor in ('buyer', 'provider', 'outsider', 'quality', 'reviewer'):
            for path in ('/api/audit-logs', '/api/audit-logs/export', '/api/audit-logs/a1/timeline'):
                self.request('GET', path, actor=actor, expected=403)

    def test_empty_and_invalid_date_filters(self):
        self.request('GET', '/api/audit-logs', actor='finance', params={'start': '', 'end': ''})
        for params in ({'start': 'invalid'}, {'start': '2026-11-01', 'end': '2026-10-01'}):
            self.request('GET', '/api/audit-logs', actor='finance', params=params, expected=422)
            self.request('GET', '/api/audit-logs/export', actor='finance', params=params, expected=422)

    def test_keyword_percent_is_literal_not_match_all(self):
        self.seed()
        data = self.request('GET', '/api/audit-logs', actor='finance', params={'q': '%'})
        self.assertEqual(data['total'], 0)

    def test_combined_filters_pagination_and_time_range(self):
        oid, _ = self.seed()
        data = self.request('GET', '/api/audit-logs', actor='finance', params={
            'category': 'settlement_payment', 'order_id': oid, 'actor': 'finance',
            'batch_no': 'BATCH-A', 'page_size': 1, 'start': '2000-01-01', 'end': '2099-01-01'})
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['items'][0]['id'], 'a2')
        self.request('GET', '/api/audit-logs', actor='finance', params={'page_size': 201}, expected=422)

    def test_request_id_preserved_and_untrusted_header_replaced(self):
        for header in ('TEST-AUDIT-001', 'spaces are forbidden'):
            r = self.client.get('/api/audit-logs', headers={
                'Authorization': 'Bearer ' + self.tokens['finance'], 'X-Request-Id': header})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers['X-Request-Id'] == header, header == 'TEST-AUDIT-001')

    def test_legacy_invalid_json_does_not_break_query(self):
        self.seed()
        with self.m.SessionLocal() as db:
            db.get(self.m.AuditLog, 'a1').before_json = 'legacy text'
            db.commit()
        data = self.request('GET', '/api/audit-logs', actor='finance', params={'q': 'HYPERLINK'})
        self.assertEqual(data['items'][0]['before'], {'legacy_summary': 'legacy text'})

    def test_export_limit_rejects_instead_of_silent_truncation(self):
        with self.m.SessionLocal() as db:
            db.execute(insert(self.m.AuditLog), [dict(id=f'limit-{i}', actor='test',
                action='export_limit', target_type='test', category='limit') for i in range(10001)])
            db.commit()
        self.request('GET', '/api/audit-logs/export', actor='finance', params={'category': 'limit'}, expected=413)


if __name__ == '__main__':
    unittest.main()
