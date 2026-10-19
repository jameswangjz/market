"""Entry path checks and authoritative launch context without external services."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qsl, urlsplit

from fastapi import HTTPException
from app.saas_entry import probe_saas_entry
import test_oidc_provider as fixture


class SaaSEntryProbeTests(unittest.TestCase):
    def setUp(self):
        self.product = SimpleNamespace(id='product',
            application_url='https://vendor.example/app?keep=1&enterprise_id=private')
        self.request = Mock(return_value=SimpleNamespace(status_code=204))
        self.policy = Mock()
        self.ns = {'validated_http_request': self.request,
            'application_address_policy': {'validate_url': self.policy}}

    def test_success_has_only_product_header_and_no_mutation(self):
        before = vars(self.product).copy()
        for status in (200, 204, 299, 301, 302, 399):
            self.request.return_value.status_code = status
            self.assertEqual(probe_saas_entry(self.ns, None, self.product), status)
        self.request.assert_called_with('HEAD', 'https://vendor.example/app', timeout=5,
            follow_redirects=False, headers={'X-Market-Product-Id': 'product'})
        self.assertEqual(vars(self.product), before)

    def test_head_unsupported_falls_back_to_bounded_safe_get(self):
        for status in (405, 501):
            self.request.reset_mock()
            self.request.side_effect = [SimpleNamespace(status_code=status), SimpleNamespace(status_code=302)]
            self.assertEqual(probe_saas_entry(self.ns, None, self.product), 302)
            self.assertEqual([call.args[0] for call in self.request.call_args_list], ['HEAD', 'GET'])
            for call in self.request.call_args_list:
                self.assertEqual(call.kwargs, {'timeout': 5, 'follow_redirects': False,
                    'headers': {'X-Market-Product-Id': 'product'}})

    def test_failures_are_sanitized(self):
        for status in (400, 404, 410, 500):
            self.request.return_value.status_code = status
            with self.assertRaises(HTTPException) as caught:
                probe_saas_entry(self.ns, None, self.product)
            self.assertEqual(caught.exception.status_code, 502)
            self.assertEqual(caught.exception.detail, f'SaaS应用访问入口检查未通过 (HTTP {status})')
        for error in (TimeoutError('private'), ValueError('unsafe destination with secret')):
            self.request.side_effect = error
            with self.assertRaises(HTTPException) as caught:
                probe_saas_entry(self.ns, None, self.product)
            self.assertEqual(caught.exception.detail, 'SaaS应用访问入口检查未通过')

    def test_unsafe_destination_is_delegated_to_safe_helper(self):
        self.product.application_url = 'https://127.0.0.1/app?secret=private'
        self.request.side_effect = ValueError('denied')
        with self.assertRaises(HTTPException):
            probe_saas_entry(self.ns, None, self.product)
        self.request.assert_called_once_with('HEAD', 'https://127.0.0.1/app', timeout=5,
            follow_redirects=False, headers={'X-Market-Product-Id': 'product'})

    def test_malformed_and_policy_denied_addresses_do_not_request(self):
        for url in ('https://user:secret@vendor.example/app', 'https://vendor.example:0/app',
                'https://vendor.example/app#fragment', 'https://vendor.example/ app',
                'https://vendor.example\\evil/app', 'not-a-url'):
            self.product.application_url = url
            with self.assertRaises(HTTPException) as caught:
                probe_saas_entry(self.ns, None, self.product)
            self.assertEqual(caught.exception.status_code, 409)
        self.product.application_url = 'http://vendor.example/app'
        self.policy.side_effect = ValueError('HTTP disabled')
        with self.assertRaises(HTTPException):
            probe_saas_entry(self.ns, None, self.product)
        self.request.assert_not_called()


class SaaSEntryLaunchTests(unittest.TestCase):
    setUpClass = classmethod(fixture.OIDCProviderTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.OIDCProviderTests.tearDownClass.__func__)
    setUp = fixture.OIDCProviderTests.setUp
    tearDown = fixture.OIDCProviderTests.tearDown
    request = fixture.OIDCProviderTests.request
    prepare = fixture.OIDCProviderTests.prepare
    create = fixture.OIDCProviderTests.create
    deliver = fixture.OIDCProviderTests.deliver

    def test_launch_replaces_all_context_preserves_other_params_without_probe(self):
        raw = ('https://saas.example.invalid/sso/apps?product_id=wrong&keep=1&'
            'enterprise_id=wrong&product_id=duplicate&enterprise_id=duplicate&keep=2&blank=')
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, 'product').application_url = raw
            db.commit()
        with patch.object(self.m, 'validated_http_request') as network:
            self.request('GET', '/api/applications', 'member')
            result = self.request('POST', f'/api/applications/{self.sid}/launch', 'member')
            network.assert_not_called()
        self.assertEqual(parse_qsl(urlsplit(result['launch_url']).query, keep_blank_values=True),
            [('keep', '1'), ('keep', '2'), ('blank', ''),
             ('product_id', 'product'), ('enterprise_id', 'buyer_tenant')])
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Product, 'product').application_url, raw)
