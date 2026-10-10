"""QA-010: actual product review HTTP, isolated production models/database."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import qa_acceptance_harness as harness


class ProductReviewAcceptanceTests(harness.AcceptanceHTTPCase):
    def setUp(self):
        super().setUp()
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, 'product').status = 'draft'
            db.add(self.m.FileObject(id='sample', owner_id='provider', product_id='product',
                object_name='isolated-sample', original_name='sample.txt', content_type='text/plain',
                size=10, scan_status='clean', scan_report='{"clamav_status":"clean"}'))
            db.commit()
        self.network.enter_context(patch.object(self.m, 'read_product_sample', return_value='Safe test sample'))
        self.analyzer = self.network.enter_context(patch.object(self.m, 'presidio_analyze', return_value=([], 'success')))

    def submit(self):
        return self.request('POST', '/api/products/product/submit', actor='provider')

    def review(self, actor, decision='approve', comment='', security=False, expected=200):
        path = '/api/products/product/' + ('security-review' if security else 'review')
        return self.request('POST', path, actor=actor, expected=expected,
                            json={'decision': decision, 'comment': comment})

    def assert_state(self, status):
        self.assertEqual(self.rows(self.m.Product, id='product')[0].status, status)

    def test_four_stages_notifications_audit_and_scan_report_permissions(self):
        self.assertEqual(self.submit()['status'], 'pending_review')
        self.assertEqual(self.recipients(target_id='product', title='产品待业务审核'), {'platform', 'reviewer'})
        self.assertEqual(self.review('reviewer')['status'], 'quality_review')
        self.assertEqual(self.recipients(target_id='product', title='产品待质量审核'), {'platform', 'quality'})
        quality = self.review('quality')
        self.assertEqual(quality['status'], 'security_review')
        self.assertEqual(self.recipients(target_id='product', title='产品待安全审核'), {'platform', 'security'})
        self.assertTrue(self.analyzer.called)
        self.assertEqual(len(self.rows(self.m.ProductSecurityScan)), 1)
        before = self.counts()
        for actor in ('provider', 'outsider', 'buyer'):
            self.request('GET', '/api/products/product/security-report', actor=actor, expected=403)
            self.request('GET', '/api/files/sample/scan-report.pdf', actor=actor, expected=403 if actor != 'provider' else 200)
        for actor in ('reviewer', 'quality', 'security', 'ops', 'platform'):
            report = self.request('GET', '/api/products/product/security-report', actor=actor)
            self.assertEqual(report['security_report']['id'], quality['security_report']['id'])
            response = self.client.get('/api/files/sample/scan-report.pdf',
                headers={'Authorization': 'Bearer ' + self.tokens[actor]})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.content.startswith(b'%PDF'))
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.review('security', security=True)['status'], 'operation_review')
        self.assertEqual(self.recipients(target_id='product', title='产品待运营审核'), {'platform', 'ops'})
        self.assertEqual(self.review('ops')['status'], 'published')
        expected_actions = {'submit_product_review', 'business_approve_product', 'quality_approve_product',
                            'approve_product_security', 'operation_approve_product'}
        self.assertTrue(expected_actions <= self.audit_actions(target_id='product'))
        for row in self.rows(self.Message, target_id='product'):
            receipts = self.rows(self.Receipt, message_id=row.id)
            jobs = self.rows(self.Outbox, message_id=row.id)
            self.assertEqual(len({receipt.user_id for receipt in receipts}), len(receipts))
            self.assertEqual(len({(job.user_id, job.channel) for job in jobs}), len(jobs))
        self.request('GET', '/api/audit-logs', actor='outsider', expected=403)
        self.assertTrue(self.request('GET', '/api/audit-logs', actor='security', params={'q': 'product'})['items'])

    def test_wrong_stage_roles_cannot_advance_or_emit_events(self):
        self.submit()
        for expected_state, good_actor, bad_actor, security in (
            ('pending_review', 'reviewer', 'quality', False),
            ('quality_review', 'quality', 'ops', False),
            ('security_review', 'security', 'reviewer', True),
            ('operation_review', 'ops', 'quality', False)):
            with self.subTest(stage=expected_state):
                before = self.counts()
                audits = len(self.rows(self.m.AuditLog))
                self.review(bad_actor, security=security, expected=403)
                self.assert_state(expected_state)
                self.assertEqual(self.counts(), before)
                self.assertEqual(len(self.rows(self.m.AuditLog)), audits)
                self.review(good_actor, security=security)

    def test_rejection_reason_resubmit_and_new_notification_cycle(self):
        self.submit()
        before = self.counts()
        self.review('reviewer', decision='reject', expected=400)
        self.assertEqual(self.counts(), before)
        self.review('reviewer', decision='reject', comment='Missing evidence')
        self.assert_state('rejected')
        self.submit()
        messages = self.rows(self.Message, target_id='product', title='产品待业务审核')
        self.assertEqual(len(messages), 2)
        self.assertNotEqual(messages[0].event_key, messages[1].event_key)
        self.assertIn('business_reject_product', self.audit_actions(target_id='product'))

    def test_security_reject_then_resubmit_creates_new_scan(self):
        self.submit()
        self.review('reviewer')
        self.review('quality')
        self.review('security', decision='reject', security=True, expected=400)
        self.review('security', decision='reject', comment='Authorization incomplete', security=True)
        self.assert_state('rejected')
        self.submit()
        self.review('reviewer')
        self.review('quality')
        self.assertEqual(len(self.rows(self.m.ProductSecurityScan)), 2)

    def test_legacy_publish_cannot_bypass_four_stage_review(self):
        self.submit()
        before = self.counts()
        self.request('POST', '/api/products/product/publish', actor='provider', expected=409)
        self.assert_state('pending_review')
        self.assertEqual(self.counts(), before)

    def test_other_tenant_cannot_submit_or_publish_provider_product(self):
        before = self.counts()
        for actor in ('buyer', 'outsider'):
            self.request('POST', '/api/products/product/submit', actor=actor, expected=404)
        self.assert_state('draft')
        self.submit()
        submitted = self.counts()
        self.assertNotEqual(submitted, before)
        for actor in ('buyer', 'outsider'):
            self.request('POST', '/api/products/product/publish', actor=actor, expected=404)
        self.assert_state('pending_review')
        self.assertEqual(self.counts(), submitted)

    def test_notification_failure_rolls_back_review_scan_and_audit(self):
        self.submit()
        self.review('reviewer')
        before = self.counts()
        audits = len(self.rows(self.m.AuditLog))
        with self.fail_after_create('review'):
            self.review('quality', expected=500)
        self.assert_state('quality_review')
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.rows(self.m.ProductSecurityScan), [])
        self.assertEqual(len(self.rows(self.m.AuditLog)), audits)


if __name__ == '__main__':
    unittest.main()
