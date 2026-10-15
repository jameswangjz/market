"""Real production HTTP routes, isolated SQLite and explicitly mocked MinIO."""
import json
import unittest
from unittest.mock import patch

import test_message_business_events as fixture


class OnlineDeliveryTests(unittest.TestCase):
    setUpClass = classmethod(fixture.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.BusinessRoutesHTTPTests.tearDownClass.__func__)
    tearDown = fixture.BusinessRoutesHTTPTests.tearDown
    request = fixture.BusinessRoutesHTTPTests.request
    create_order = fixture.BusinessRoutesHTTPTests.create_order
    pay = fixture.BusinessRoutesHTTPTests.pay
    task_id = fixture.BusinessRoutesHTTPTests.task_id
    counts = fixture.BusinessRoutesHTTPTests.counts
    fail_after_create = fixture.BusinessRoutesHTTPTests.fail_after_create

    def setUp(self):
        fixture.BusinessRoutesHTTPTests.setUp(self)
        with self.m.SessionLocal() as db:
            db.get(self.m.Enterprise, 'buyer_tenant').verification_status = 'verified'
            db.get(self.m.Product, 'product').download_limit = 1
            db.add(self.m.User(id='member', name='Member', email='member@example.invalid',
                password_hash='unused', verified_status='verified', activation_status='active'))
            db.flush()
            db.add(self.m.Membership(user_id='member', enterprise_id='buyer_tenant', role='member'))
            db.commit()
            self.tokens['member'] = self.m.issue_token(db.get(self.m.User, 'member'))
        self.network.enter_context(patch.object(self.m, 'MINIO_ENDPOINT', 'fixture'))
        self.storage = self.network.enter_context(patch.object(self.m, 'Minio')).return_value
        self.storage.stat_object.return_value.size = 1
        self.storage.get_object.return_value.read.return_value = b'x'

    def paid_order(self):
        oid = self.create_order()
        self.pay(oid)
        return oid, self.task_id(oid)

    def process(self, task):
        return self.request('POST', f'/api/delivery-tasks/{task}/process', actor='ops', json={'success': True})

    def test_real_checks_complete_without_buyer_accept_and_idempotent(self):
        oid, task = self.paid_order()
        result = self.process(task)
        self.assertEqual((result['task']['status'], result['order']['main_status'], result['order']['delivery_status']),
            ('completed', 'completed', 'accepted'))
        self.storage.stat_object.assert_called_once()
        self.storage.get_object.assert_called_once_with(self.m.MINIO_BUCKET, 'dataset', offset=0, length=1)
        self.storage.get_object.return_value.close.assert_called_once()
        self.storage.get_object.return_value.release_conn.assert_called_once()
        before = self.counts()
        self.process(task)
        self.assertEqual(self.counts(), before)
        self.request('POST', f'/api/orders/{oid}/transition', expected=409, json={'action': 'accept_delivery'})

    def test_three_failures_then_manual_new_round_keeps_attempt_counter(self):
        oid, task = self.paid_order()
        self.storage.stat_object.side_effect = RuntimeError('PRIVATE SECRET')
        for attempt in range(1, 4):
            result = self.process(task)['task']
            self.assertEqual(result['retry_count'], attempt)
            self.assertNotIn('PRIVATE', result['last_error'])
            self.assertEqual(result['status'], 'retrying' if attempt < 3 else 'exception')
            self.assertEqual(result['next_retry_at'] is None, attempt == 3)
        self.request('POST', f'/api/delivery-tasks/{task}/process', actor='ops', expected=409, json={'success': True})
        self.request('POST', f'/api/orders/{oid}/transition', actor='ops', json={'action': 'retry_delivery', 'reason': 'Storage restored'})
        result = self.process(task)['task']
        self.assertEqual((result['retry_count'], result['max_retries'], result['status']), (4, 6, 'retrying'))
        self.storage.stat_object.side_effect = None
        self.assertEqual(self.process(task)['task']['status'], 'completed')

    def test_access_download_and_quota_are_buyer_admin_only(self):
        oid, task = self.paid_order()
        path = f'/api/orders/{oid}/delivery-access'
        self.assertFalse(self.request('GET', path)['ready'])
        self.process(task)
        for actor in ('buyer', 'buyer_admin'):
            result = self.request('GET', path, actor=actor)
            self.assertTrue(result['ready'])
            self.assertEqual(result['files'][0]['remaining_downloads'], 1)
            self.assertNotIn('object_name', result['files'][0])
        download = f'/api/files/product_data/download?order_id={oid}'
        for actor in ('member', 'outsider', 'platform'):
            self.request('GET', path, actor=actor, expected=403)
        for actor in ('member', 'outsider'):
            self.request('GET', download, actor=actor, expected=403)
        response = self.client.get(download, headers={'Authorization': 'Bearer '+self.tokens['buyer_admin']})
        self.assertEqual((response.status_code, response.content), (200, b'x'))
        self.request('GET', download, expected=429)
        result = self.request('GET', path)
        self.assertFalse(result['ready'])
        self.assertEqual(result['files'][0]['url'], '')
        self.assertEqual(result['files'][0]['used_downloads'], 1)

    def test_missing_scan_bad_size_or_unreadable_file_cannot_complete(self):
        for failure in ('scan', 'size', 'empty'):
            with self.subTest(failure=failure):
                oid, task = self.paid_order()
                with self.m.SessionLocal() as db:
                    db.get(self.m.FileObject, 'product_data').scan_report = json.dumps({'presidio_status': 'error' if failure == 'scan' else 'available'})
                    db.commit()
                self.storage.stat_object.return_value.size = 2 if failure == 'size' else 1
                self.storage.get_object.return_value.read.return_value = b'' if failure == 'empty' else b'x'
                result = self.process(task)
                self.assertEqual(result['task']['status'], 'retrying')
                self.assertNotEqual(result['order']['main_status'], 'completed')

    def test_corrupt_file_snapshot_fails_closed_instead_of_server_error(self):
        oid, task = self.paid_order()
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            snapshot = json.loads(order.delivery_snapshot_json)
            snapshot['files'] = [{}]
            order.delivery_snapshot_json = json.dumps(snapshot)
            db.commit()
        self.assertEqual(self.process(task)['task']['status'], 'retrying')
        self.storage.stat_object.assert_not_called()

    def test_notification_failure_rolls_back_completion_and_task(self):
        oid, task = self.paid_order()
        before = self.counts()
        with self.fail_after_create('order'):
            self.request('POST', f'/api/delivery-tasks/{task}/process', actor='ops', expected=500, json={'success': True})
        self.assertEqual(before, self.counts())
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.DeliveryTask, task).status, 'in_delivery')
            self.assertEqual(db.get(self.m.Order, oid).main_status, 'fulfilling')

    def test_security_unpublish_revokes_download(self):
        oid, task = self.paid_order()
        self.process(task)
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, 'product').status = 'security_unpublished'
            db.commit()
        self.assertFalse(self.request('GET', f'/api/orders/{oid}/delivery-access')['ready'])
        self.request('GET', f'/api/files/product_data/download?order_id={oid}', expected=409)

    def test_legacy_refund_transitions_reject_finance_and_admin_without_mutation(self):
        oid, task = self.paid_order()
        self.process(task)
        def snapshot():
            with self.m.SessionLocal() as db:
                order = db.get(self.m.Order, oid)
                payment = db.scalar(self.m.select(self.m.Payment).where(self.m.Payment.order_id == oid))
                return ((order.main_status, order.payment_status, order.delivery_status,
                         order.paid_amount, order.refunded_amount, payment.status, payment.amount),
                        tuple(db.query(model).count() for model in (
                            self.m.Refund, self.m.refund_workflow['Case'], self.m.AuditLog,
                            self.m.refund_execution['Execution'], self.m.refund_execution['PaymentLeg'])),
                        self.counts())
        before = snapshot()
        for action in ('approve_refund', 'complete_refund'):
            for actor, status in (('finance', 403), ('ops', 409), ('platform', 409)):
                self.request('POST', f'/api/orders/{oid}/transition', actor=actor, expected=status,
                             json={'action': action, 'refund_amount': 20})
                self.assertEqual(snapshot(), before)

    def test_partial_file_refund_manual_assessment_and_admin_approval_not_funds(self):
        oid, task = self.paid_order()
        self.process(task)
        refund = self.request('POST', f'/api/orders/{oid}/refund-requests', json={
            'amount': '20.00', 'reason': 'Partial file delivery refund',
            'idempotency_key': 'file-partial-refund-request'})
        rid = refund['id']
        review = {'decision': 'approve', 'reason': 'Reviewed partial file refund',
                  'expected_version': refund['version'], 'idempotency_key': 'file-partial-refund-approval'}
        self.request('POST', f'/api/refunds/{rid}/review', actor='finance', expected=403, json=review)
        refund = self.request('POST', f'/api/refunds/{rid}/manual-assessments', actor='ops', json={
            'amount': '20.00', 'refundable_limit': '100.00', 'used_calls': 0,
            'reason': 'Documented assessment of partial file refund',
            'evidence_refs': ['file-refund-assessment'], 'expected_version': refund['version'],
            'idempotency_key': 'file-partial-refund-assessment'})
        review['expected_version'] = refund['version']
        approved = self.request('POST', f'/api/refunds/{rid}/review', actor='ops', json=review)
        self.assertEqual((approved['status'], approved['amount']), ('approved', '20.00'))
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            refund = db.get(self.m.Refund, rid)
            self.assertEqual(refund.status, 'approved')
            self.assertEqual(order.refunded_amount, 0)
        self.assertTrue(approved['execution_available'])

    def test_completed_partial_refund_fixture_revokes_file_access_not_funds(self):
        oid, task = self.paid_order()
        self.process(task)
        self.assertTrue(self.request('GET', f'/api/orders/{oid}/delivery-access')['ready'])
        row = self.request('POST', f'/api/orders/{oid}/refund-requests', json={
            'amount': '20.00', 'reason': 'Completed file refund access-guard fixture',
            'idempotency_key': 'completed-file-refund-fixture'})
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            refund = db.get(self.m.Refund, row['id'])
            # This legacy completed state only exercises access guards;
            # real FILE execution and receipts are tested separately.
            refund.status, refund.completed_at = 'completed', self.m.now()
            refund.completed_by = 'test:manual-file-refund'
            order.refunded_amount = refund.amount
            db.commit()
        self.assertFalse(self.request('GET', f'/api/orders/{oid}/delivery-access')['ready'])
        self.request('GET', f'/api/files/product_data/download?order_id={oid}', expected=409)


if __name__ == '__main__':
    unittest.main()
