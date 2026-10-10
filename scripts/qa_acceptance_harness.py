"""Shared isolated production HTTP fixture for QA-006 and QA-010."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

TESTS = Path(__file__).resolve().parents[1] / 'backend' / 'tests'
sys.path.insert(0, str(TESTS))
import test_message_business_events as business


class AcceptanceHTTPCase(unittest.TestCase):
    setUpClass = business.BusinessRoutesHTTPTests.__dict__['setUpClass']
    tearDownClass = business.BusinessRoutesHTTPTests.__dict__['tearDownClass']
    request = business.BusinessRoutesHTTPTests.request
    counts = business.BusinessRoutesHTTPTests.counts
    recipients = business.BusinessRoutesHTTPTests.recipients
    fail_after_create = business.BusinessRoutesHTTPTests.fail_after_create
    create_order = business.BusinessRoutesHTTPTests.create_order
    pay = business.BusinessRoutesHTTPTests.pay

    def setUp(self):
        business.BusinessRoutesHTTPTests.setUp(self)
        self.addCleanup(business.BusinessRoutesHTTPTests.tearDown, self)
        self.network.enter_context(patch.object(self.m.smtplib, 'SMTP', side_effect=AssertionError('SMTP forbidden')))
        self.network.enter_context(patch.object(self.m.smtplib, 'SMTP_SSL', side_effect=AssertionError('SMTP forbidden')))
        self.network.enter_context(patch.object(self.m, 'clamav_version', return_value='isolated-version'))
        with self.m.SessionLocal() as db:
            user = self.m.User(id='security', name='Security QA', email='security@example.invalid',
                password_hash='unused', platform_role='security_compliance', verified_status='verified')
            db.add(user)
            db.commit()
            self.tokens['security'] = self.m.issue_token(user)

    def rows(self, model, **filters):
        with self.m.SessionLocal() as db:
            return db.scalars(self.select(model).filter_by(**filters)).all()

    def audit_actions(self, **filters):
        return {row.action for row in self.rows(self.m.AuditLog, **filters)}
