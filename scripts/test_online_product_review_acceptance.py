"""Companion guards and isolated real review routes; no live acceptance evidence."""
from contextlib import ExitStack
import importlib.util
import os
from io import BytesIO
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import online_product_review_acceptance as c


class Guards(unittest.TestCase):
    def test_review_identity_contract_and_canonical_semantics(self):
        self.assertEqual(c.ROLES['business'], 'product_manager')
        ids = c.roots('qa-review-abcdef012345')
        users = [uid for group in ids['reviewers'].values() for uid in group.values()]
        self.assertEqual(len(set(users)), 12)
        self.assertTrue(all(len(uid) <= 36 for uid in users + list(ids['canonical_versions'].values())))
        self.assertEqual(c.STEPS[-1][3], 'pending_integration')
        targets = c.targets(ids, c.a.fixture_ids(), c.s.fixture_ids())
        self.assertEqual([targets[k]['kind'] for k in ('a0', 'a1', 's')], ['api', 'api', 'saas'])

    def test_parent_gate_precedes_import_and_mutation(self):
        with patch.object(c.a, 'load_main') as load:
            with self.assertRaisesRegex(c.a.AcceptanceFailure, 'Parent review required'):
                c.run(SimpleNamespace(parent_reviewed=False))
            load.assert_not_called()

    def test_object_keys_bind_exact_owner_product_role_and_filename(self):
        root = c.roots('qa-review-abcdef012345')
        targets = c.targets(root, c.a.fixture_ids('qa-api-auto-abcdef012345'), c.s.fixture_ids('qa-vcs-saas-abcdef012345'))
        target = targets['a1']
        root['keys'] = [{'key': target['provider'] + '/20261014/abcdef012345-' + root['run_id'] + '-a1.logo.png',
                         'owner': target['provider'], 'product': target['pid'], 'role': 'product_logo'}]
        c.validate_asset_records(root, targets)
        for field, value in [('owner', 'outside'), ('product', c.f.PROTECTED_PRODUCT), ('key', '../escape')]:
            bad = dict(root, keys=[dict(root['keys'][0], **{field: value})])
            with self.subTest(field=field), self.assertRaises(c.a.AcceptanceFailure):
                c.validate_asset_records(bad, targets)

    def test_private_journal_exclusive_create(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'review.json'
            c.write(path, c.roots(), create=True)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                c.write(path, {}, create=True)


@unittest.skipUnless(importlib.util.find_spec('sqlalchemy') and importlib.util.find_spec('fastapi'),
                     'Backend dependency environment required')
class ReviewRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parents[1] / 'backend/tests/test_message_business_events.py'
        spec = importlib.util.spec_from_file_location('online_review_unit_fixture', path)
        cls.fixture_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.fixture_module)
        cls.fixture_module.BusinessRoutesHTTPTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls.fixture_module.BusinessRoutesHTTPTests.tearDownClass()

    def chain(self, kind):
        from sqlalchemy import select
        fixture = self.fixture_module.BusinessRoutesHTTPTests(methodName='runTest')
        fixture.setUp()
        m = fixture.m
        label = 's' if kind == 'saas' else 'a0'
        root = c.roots()
        storage = {}
        class Response(BytesIO):
            def release_conn(self):
                pass
        class Storage:
            def __init__(self, *args, **kwargs):
                pass
            def bucket_exists(self, bucket):
                return True
            def put_object(self, bucket, key, data, length, **kwargs):
                storage[key] = data.read(length)
            def get_object(self, bucket, key, **kwargs):
                return Response(storage[key])
        try:
            with m.SessionLocal() as db:
                db.get(m.Enterprise, 'provider_tenant').verification_status = 'verified'
                db.add(m.ProductCategory(id='unit-parent', name='Unit'))
                db.flush()
                db.add(m.ProductCategory(id='unit-child', name='Services', parent_id='unit-parent'))
                product = db.get(m.Product, 'product')
                product.product_type = kind
                product.delivery_method = 'api' if kind == 'api' else 'tenant_access'
                product.logo_file_id, product.logo_thumbnail_file_id = '', ''
                product.upstream_url = c.a.MOCK_URL if kind == 'api' else ''
                if kind == 'saas':
                    product.application_url = 'https://unit.example.invalid/app'
                    product.integration_api_url = c.s.MOCK_SAAS + '/isv.php'
                    product.oidc_redirect_uris_json = '["https://unit.example.invalid/callback"]'
                for role, uid in root['reviewers'][label].items():
                    db.add(m.User(id=uid, name=uid, email=uid + '@example.invalid', password_hash='unit',
                                  platform_role=c.ROLES[role], verified_status='verified', activation_status='active'))
                if kind == 'api':
                    route = m.ApiGatewayRoute(product_id='product', route_key='unit-owned-route', version='v1', upstream_url=c.a.MOCK_URL)
                    db.add(route)
                    db.flush()
                    db.add(m.version_gateway['Config'](product_id='product', version_id='version', route_id=route.id,
                        upstream_url=c.a.MOCK_URL, route_key='unit-owned-route', version_code='v1', auth_mode='api_key',
                        upstream_auth_mode='oauth2', upstream_scope='resource.invoke', timeout_ms=10000, strip_prefix=True,
                        health_method='GET', health_path='/health', rate_limit_per_minute=60, daily_quota=100, monthly_quota=1000))
                db.commit()
                tokens = {role: m.issue_token(db.get(m.User, uid)) for role, uid in root['reviewers'][label].items()}
            target = {'pid': 'product', 'kind': kind, 'enterprise': 'provider_tenant', 'provider': 'provider'}
            with ExitStack() as mocks:
                from cryptography.fernet import Fernet
                mocks.enter_context(patch.dict(os.environ, {'OIDC_CREDENTIAL_ENCRYPTION_KEY': Fernet.generate_key().decode()}))
                mocks.enter_context(patch.object(m, 'MINIO_ENDPOINT', 'unit-storage'))
                mocks.enter_context(patch.object(m, 'Minio', Storage))
                mocks.enter_context(patch.object(m, 'clamav_scan_stream', return_value=('clean', 'unit stream OK')))
                mocks.enter_context(patch.object(m, 'clamav_version', return_value='unit'))
                mocks.enter_context(patch.object(m, 'presidio_version', return_value='unit'))
                mocks.enter_context(patch.object(m, 'presidio_analyze', return_value=([], 'available')))
                mocks.enter_context(patch.dict(m.application_address_policy,
                    {'validate_url': lambda value, **kwargs: value, 'allow_http': lambda *args, **kwargs: True}))
                evidence = c.review_chain(m, fixture.client, root, label, target, fixture.tokens['provider'], tokens, 'unit-child')
            self.assertEqual([row['after'] for row in evidence['reviews']],
                             ['quality_review', 'security_review', 'operation_review', 'pending_integration'])
            self.assertEqual(evidence['reviewer_inbox_stages'], 4)
            with m.SessionLocal() as db:
                self.assertEqual(db.get(m.Product, 'product').status, 'pending_integration')
                self.assertIsNone(db.scalar(select(m.Order.id).where(m.Order.product_id == 'product')))
                if kind == 'saas':
                    self.assertEqual(db.get(m.Product, 'product').oidc_redirect_uris_json,
                                     '["https://unit.example.invalid/callback"]')
        finally:
            fixture.tearDown()

    def test_actual_api_four_reviews_stop_at_pending_integration(self):
        self.chain('api')

    def test_actual_saas_four_reviews_retain_callbacks_and_pending_gate(self):
        self.chain('saas')


if __name__ == '__main__':
    unittest.main()
