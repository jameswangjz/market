import unittest
from unittest.mock import patch
from decimal import Decimal

import test_message_business_events as business


class LedgerAcceptanceTests(unittest.TestCase):
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

    def seed(self, refund=False):
        oid = self.create_order()
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            number = order.order_no
            db.add(self.m.SettlementBatch(id='b', batch_no='BATCH-TEST', idempotency_key='b', status='generated'))
            db.add(self.m.Settlement(id='s', settlement_no='SET-TEST', order_id=oid,
                                      net_amount=100, platform_fee=18, provider_share=72))
            db.flush()
            for role in ('platform', 'provider'):
                db.add(self.m.SettlementLine(batch_id='b', settlement_id='s', participant_type=role))
            if refund:
                db.add(self.m.Settlement(id='refund', settlement_no='REF-TEST', order_id=oid,
                                          is_refund=True, net_amount=-20, platform_fee=-3.6, provider_share=-14.4))
                db.flush()
                db.add(self.m.SettlementLine(batch_id='b', settlement_id='refund', participant_type='provider'))
            db.commit()
        return number

    def load(self, entries, kind='payment', key='import-1'):
        return self.request('POST', '/api/settlement-batches/b/ledger-import', actor='finance',
                            json={'ledger_type': kind, 'event_key': key, 'source_name': 'Independent fixture',
                                  'entries': entries})

    def compare(self, imported):
        return self.request('POST', f"/api/settlement-batches/b/ledger-imports/{imported['id']}/compare", actor='finance')

    def test_four_ledger_types_distinguish_receipts_and_profit(self):
        number = self.seed()
        for kind in ('platform', 'payment', 'bank', 'distribution'):
            expected = '90.00' if kind == 'distribution' else '100.00'
            report = self.compare(self.load([{'entry_no': kind, 'order_no': number, 'amount': expected}], kind, kind))
            self.assertEqual(report['status'], 'matched')
            self.assertFalse(report['real_channel_verified'])
            self.assertEqual(report['items'][0]['expected_amount'], expected)

    def test_import_is_idempotent_and_conflicting_key_rejected(self):
        number = self.seed()
        body = [{'entry_no': 'P1', 'order_no': number, 'amount': '100.00'}]
        a, b = self.load(body), self.load(body)
        self.assertEqual(a['id'], b['id'])
        self.assertTrue(b['reused'])
        self.request('POST', '/api/settlement-batches/b/ledger-import', actor='finance', expected=409,
                     json={'ledger_type': 'payment', 'event_key': 'import-1', 'source_name': 'Different', 'entries': body})

    def test_duplicate_entry_does_not_double_count_but_is_difference(self):
        number = self.seed()
        entry = {'entry_no': 'P1', 'order_no': number, 'amount': '100.00'}
        report = self.compare(self.load([entry, entry]))
        self.assertEqual(report['status'], 'difference')
        self.assertEqual(report['duplicates'], ['P1'])
        self.assertEqual(report['items'][0]['actual_amount'], '100.00')

    def test_missing_unexpected_and_amount_difference(self):
        number = self.seed()
        report = self.compare(self.load([{'entry_no': 'P1', 'order_no': 'ORD-UNKNOWN', 'amount': '100.00'}]))
        self.assertEqual({x['status'] for x in report['items']}, {'missing', 'unexpected'})
        report = self.compare(self.load([{'entry_no': 'P2', 'order_no': number, 'amount': '99.99'}], key='amount'))
        self.assertEqual(report['items'][0]['difference_amount'], '-0.01')

    def test_refund_negative_entry_and_no_business_status_mutation(self):
        number = self.seed(refund=True)
        report = self.compare(self.load([{'entry_no': 'P1', 'order_no': number, 'amount': '100.00'},
                                        {'entry_no': 'R1', 'order_no': number, 'amount': '-20.00'}]))
        self.assertEqual(report['status'], 'matched')
        self.assertEqual(report['items'][0]['expected_amount'], '80.00')
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.SettlementBatch, 'b').status, 'generated')
            self.assertEqual(db.get(self.m.Settlement, 's').status, 'pending')

    def test_resolution_requires_evidence_and_is_not_matching(self):
        number = self.seed()
        report = self.compare(self.load([{'entry_no': 'P1', 'order_no': number, 'amount': '99.99'}]))
        path = f"/api/settlement-batches/b/ledger-reports/{report['id']}/resolve"
        self.request('POST', path, actor='finance', json={'reason': 'Correction'}, expected=422)
        data = self.request('POST', path, actor='finance', json={'reason': 'Confirmed rounding discrepancy', 'evidence_ref': 'TEST-DOCUMENT-001'})
        self.assertEqual(data['status'], 'resolved')
        self.assertFalse(data['real_channel_verified'])
        self.request('POST', path, actor='finance', json={'reason': 'Again', 'evidence_ref': 'TEST-002'}, expected=409)

    def test_ordinary_user_cannot_import_or_compare(self):
        number = self.seed()
        for actor in ('buyer', 'provider', 'outsider', 'quality'):
            self.request('POST', '/api/settlement-batches/b/ledger-import', actor=actor, expected=403,
                         json={'ledger_type': 'payment', 'source_name': 'test', 'event_key': actor,
                               'entries': [{'entry_no': 'P1', 'order_no': number, 'amount': '100'}]})
            self.request('GET', '/api/settlement-batches/b/ledger-reports', actor=actor, expected=403)

    def test_invalid_currency_precision_and_empty_entries(self):
        number = self.seed()
        for entries in ([], [{'entry_no': 'P1', 'order_no': number, 'amount': '1.001'}],
                        [{'entry_no': 'P1', 'order_no': number, 'amount': '1', 'currency': 'USD'}]):
            self.request('POST', '/api/settlement-batches/b/ledger-import', actor='finance', expected=422,
                         json={'ledger_type': 'bank', 'source_name': 'test', 'event_key': 'bad', 'entries': entries})

    def test_import_audit_failure_rolls_back_import_record(self):
        number = self.seed()
        with patch.object(self.m, 'audit', side_effect=RuntimeError('Injected audit failure')):
            self.request('POST', '/api/settlement-batches/b/ledger-import', actor='finance', expected=500,
                         json={'ledger_type': 'bank', 'source_name': 'test', 'event_key': 'rollback',
                               'entries': [{'entry_no': 'P1', 'order_no': number, 'amount': '100.00'}]})
        with self.m.SessionLocal() as db:
            self.assertIsNone(db.scalar(self.select(self.m.ledger_support['LedgerImport']).where(
                self.m.ledger_support['LedgerImport'].event_key == 'rollback')))

    def test_report_download_has_scope_and_audit(self):
        number = self.seed()
        report = self.compare(self.load([{'entry_no': 'P1', 'order_no': number, 'amount': '100.00'}]))
        path = f"/api/settlement-batches/b/ledger-reports/{report['id']}/download"
        self.request('GET', path, actor='buyer', expected=403)
        data = self.request('GET', path, actor='finance')
        self.assertEqual(data['id'], report['id'])
        self.assertFalse(data['real_channel_verified'])
        with self.m.SessionLocal() as db:
            self.assertIsNotNone(db.scalar(self.select(self.m.AuditLog.id).where(
                self.m.AuditLog.action == 'download_settlement_ledger_report',
                self.m.AuditLog.target_id == report['id'])))


if __name__ == '__main__':
    unittest.main()
