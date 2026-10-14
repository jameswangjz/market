"""Retired role migration and authentication against isolated production routes."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from qa_acceptance_harness import AcceptanceHTTPCase


class RetiredBusinessReviewRoleTests(AcceptanceHTTPCase):
    def setUp(self):
        super().setUp()
        self.password = 'Retirement-test-password'
        m = self.m
        with m.SessionLocal() as db:
            for uid, username, role in (
                ('old-fixed', 'ywsh', 'business_reviewer'),
                ('old-custom', 'custom-review', 'business_reviewer'),
                ('old-collision', 'sjcp', 'business_reviewer'),
                ('old-deleted', 'deleted-review', 'business_reviewer'),
            ):
                db.add(m.User(id=uid, username=username, email=uid + '@example.invalid', name=uid,
                    password_hash=m.hash_password(self.password), email_verified=True,
                    verified_status='verified', platform_role=role,
                    activation_status='deleted' if uid == 'old-deleted' else 'active'))
            db.flush()
            db.add(m.Membership(id='retired-membership', user_id='old-fixed', enterprise_id='buyer_tenant',
                                role='super_admin', status='pending_activation'))
            db.add(m.AuditLog(id='historical-review', actor='old-fixed@example.invalid',
                             action='business_approve_product', target_type='product', target_id='product',
                             before_json='{"status":"pending_review"}', after_json='{"status":"quality_review"}'))
            db.commit()
            self.tokens.update({uid: m.issue_token(db.get(m.User, uid)) for uid in
                                ('old-fixed', 'old-custom', 'old-collision', 'old-deleted')})

    def migrate(self):
        with self.m.SessionLocal() as db:
            self.m.ensure_platform_role_accounts(db)
            db.commit()

    def test_idempotent_migration_retains_identity_membership_and_audit(self):
        order_id = self.create_order()
        order = self.rows(self.m.Order, id=order_id)[0]
        original_order = {column.name: getattr(order, column.name) for column in order.__table__.columns}
        historical = {row.id: {column.name: getattr(row, column.name) for column in row.__table__.columns}
                      for row in self.rows(self.m.AuditLog)}
        before = self.rows(self.m.AuditLog, id='historical-review')[0]
        original = {column.name: getattr(before, column.name) for column in before.__table__.columns}
        self.migrate()
        self.migrate()
        for uid in ('old-fixed', 'old-custom', 'old-collision', 'old-deleted'):
            with self.subTest(user=uid):
                user = self.rows(self.m.User, id=uid)[0]
                self.assertFalse(user.is_active)
                self.assertEqual(user.activation_status, 'deleted' if uid == 'old-deleted' else 'disabled')
                self.assertEqual(user.platform_role, 'business_reviewer')
                self.assertTrue(self.m.verify_password(self.password, user.password_hash))
        self.assertEqual(self.rows(self.m.Membership, id='retired-membership')[0].status, 'pending_activation')
        after = self.rows(self.m.AuditLog, id='historical-review')[0]
        self.assertEqual({column.name: getattr(after, column.name) for column in after.__table__.columns}, original)
        for row in self.rows(self.m.AuditLog):
            if row.id in historical:
                self.assertEqual({column.name: getattr(row, column.name) for column in row.__table__.columns},
                                 historical[row.id])
        self.assertTrue(set(historical) <= {row.id for row in self.rows(self.m.AuditLog)})
        order = self.rows(self.m.Order, id=order_id)[0]
        self.assertEqual({column.name: getattr(order, column.name) for column in order.__table__.columns}, original_order)
        migrations = self.rows(self.m.AuditLog, action='retire_business_review_account')
        self.assertEqual(len(migrations), 4)
        self.assertEqual({row.target_id for row in migrations},
                         {'old-fixed', 'old-custom', 'old-collision', 'old-deleted'})
        for row in migrations:
            self.assertEqual(row.actor, 'system:retire_business_reviewer')
            self.assertEqual(row.target_type, 'user')
            self.assertEqual(json.loads(row.before_json), {'is_active': True,
                'activation_status': 'deleted' if row.target_id == 'old-deleted' else 'active',
                'platform_role': 'business_reviewer'})
            self.assertEqual(json.loads(row.after_json), {'is_active': False,
                'activation_status': 'deleted' if row.target_id == 'old-deleted' else 'disabled',
                'platform_role': 'business_reviewer'})
        self.assertEqual(len(self.rows(self.m.User, username='ywsh')), 1)
        self.assertNotIn('ywsh', self.m.PLATFORM_ROLE_ACCOUNTS)
        self.assertNotIn('business_reviewer', self.m.PLATFORM_ROLES)
        self.assertEqual(len(self.m.PLATFORM_ROLE_ACCOUNTS), 6)

    def test_password_and_existing_jwt_rejected_before_and_after_migration(self):
        for migrated in (False, True):
            if migrated:
                self.migrate()
            before_count = len(self.rows(self.m.AuditLog))
            for uid in ('old-fixed', 'old-custom', 'old-collision', 'old-deleted'):
                with self.subTest(migrated=migrated, user=uid):
                    self.request('POST', '/api/auth/login', actor='buyer', expected=401,
                                 json={'identifier': uid + '@example.invalid', 'password': self.password})
                    self.request('GET', '/api/auth/me', actor=uid, expected=401)
                    for decision in ('approve', 'reject'):
                        self.request('POST', '/api/products/product/review', actor=uid, expected=401,
                                     json={'decision': decision, 'comment': 'Retired user'})
            self.assertEqual(len(self.rows(self.m.AuditLog)), before_count)

    def test_retirement_and_migration_audit_rollback_together(self):
        with self.m.SessionLocal() as db:
            self.m.ensure_platform_role_accounts(db)
            db.flush()
            db.rollback()
        self.assertTrue(self.rows(self.m.User, id='old-fixed')[0].is_active)
        self.assertEqual(self.rows(self.m.User, id='old-fixed')[0].activation_status, 'active')
        self.assertEqual(self.rows(self.m.AuditLog, action='retire_business_review_account'), [])
        self.assertEqual(len(self.rows(self.m.AuditLog, id='historical-review')), 1)

    def test_initializer_does_not_create_ywsh_and_retires_username_even_without_role(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.User, 'old-fixed').username = 'archived-reviewer'
            db.commit()
        self.migrate()
        self.assertEqual(self.rows(self.m.User, username='ywsh'), [])
        with self.m.SessionLocal() as db:
            user = db.get(self.m.User, 'old-fixed')
            user.username = 'ywsh'
            user.platform_role = ''
            user.is_active = True
            user.activation_status = 'active'
            db.commit()
        self.request('GET', '/api/auth/me', actor='old-fixed', expected=401)
        self.migrate()
        user = self.rows(self.m.User, id='old-fixed')[0]
        self.assertFalse(user.is_active)
        self.assertEqual(user.platform_role, '')

    def test_disabled_users_cannot_login_or_use_jwt_and_active_manager_can(self):
        with self.m.SessionLocal() as db:
            disabled = db.get(self.m.User, 'disabled_ops')
            disabled.password_hash = self.m.hash_password(self.password)
            disabled.email_verified = True
            manager = db.get(self.m.User, 'reviewer')
            manager.password_hash = self.m.hash_password(self.password)
            manager.email_verified = True
            db.commit()
        self.request('POST', '/api/auth/login', actor='buyer', expected=401,
                     json={'identifier': 'disabled_ops@example.invalid', 'password': self.password})
        self.request('GET', '/api/auth/me', actor='disabled_ops', expected=401)
        result = self.request('POST', '/api/auth/login', actor='buyer',
                              json={'identifier': 'reviewer@example.invalid', 'password': self.password})
        self.assertEqual(result['user']['platform_role'], 'product_manager')
        self.assertTrue(result['token'])

    def test_fixed_catalog_and_submission_notifications_exclude_retired_role(self):
        self.migrate()
        catalog = self.request('GET', '/api/admin/platform-roles', actor='platform')
        self.assertNotIn('business_reviewer', catalog['roles'])
        self.assertFalse(any(item['username'] == 'ywsh' or item['role'] == 'business_reviewer'
                             for item in catalog['items']))
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, 'product').status = 'draft'
            db.commit()
        self.request('POST', '/api/products/product/submit', actor='provider')
        recipients = self.recipients(target_id='product', title='产品待业务审核')
        self.assertIn('platform', recipients)
        self.assertIn('reviewer', recipients)
        self.assertFalse(recipients & {'old-fixed', 'old-custom', 'old-collision', 'old-deleted'})
        self.assertEqual(json.loads(self.rows(self.m.AuditLog, id='historical-review')[0].after_json),
                         {'status': 'quality_review'})


if __name__ == '__main__':
    unittest.main()
