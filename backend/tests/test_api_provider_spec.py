"""Executable contracts for the standalone API provider guide's JSON examples."""
import ast
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = ROOT / 'docs/API应用提供方接入统一网关接口与规范.md'
COPIES = (ROOT / 'docs/api-gateway-integration.md', ROOT / 'frontend/public/api-gateway-integration.md')
FENCES = re.compile(r'^```json[^\S\n]*\n(.*?)^```[^\S\n]*$', re.MULTILINE | re.DOTALL)
sys.path.insert(0, str(ROOT / 'backend'))
from app import api_provider_lifecycle as backend

spec = importlib.util.spec_from_file_location('api_spec_mock_provider', ROOT / 'mock-api/app/main.py')
provider = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider)


class APIProviderSpecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = DOCUMENT.read_text(encoding='utf-8')

    def section(self, number):
        match = re.search(r'^#{2,3} ' + re.escape(number) + r'\s[^\n]*\n(.*?)(?=^#{2,3} \d|\Z)',
                          self.document, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, 'Missing section ' + number)
        return match.group(1)

    def examples(self, number):
        return [json.loads(match.group(1)) for match in FENCES.finditer(self.section(number))]

    def open_example(self):
        examples = self.examples('5.3')
        self.assertEqual(len(examples), 1)
        return examples[0]

    def test_canonical_copies_are_byte_identical(self):
        for path in COPIES:
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual(DOCUMENT.read_bytes(), path.read_bytes())
        self.assertIn('v2.0', self.document)

    def test_all_json_examples_parse_in_canonical_and_published_copies(self):
        for path in (DOCUMENT, *COPIES):
            text = path.read_text(encoding='utf-8')
            fences = list(FENCES.finditer(text))
            self.assertTrue(fences)
            self.assertEqual(len(fences), len(re.findall(r'^```json[^\S\n]*$', text, re.MULTILINE)))
            for match in fences:
                with self.subTest(path=str(path.relative_to(ROOT)), line=text[:match.start()].count('\n') + 1):
                    json.loads(match.group(1))

    def test_full_open_matches_backend_payload_contract(self):
        request = self.open_example()
        normalized = backend.validate_payload(request)
        self.assertEqual(normalized, request)
        self.assertEqual(set(request), backend.ROOT)
        self.assertEqual(request['type'], 'OPEN')
        self.assertEqual(set(request['version']), {'id', 'code'})
        self.assertEqual(set(request['periods'][0]), backend.PERIOD)
        self.assertEqual(set(request['periods'][0]['quotas']), set(backend.QUOTAS))

    def test_documented_open_status_and_cloned_renew_apply_and_replay(self):
        request = backend.validate_payload(self.open_example())
        original = deepcopy(request)
        claims = dict(active=True, product_id=request['product_id'], scope='resource.manage')
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ',
                {'MOCK_API_DB_PATH': str(Path(directory) / 'guide.sqlite3')}):
            opened = provider.lifecycle(request, claims)
            self.assertEqual(request, original)
            self.assertIs(type(opened['ret']), int)
            self.assertEqual(opened['ret'], 0)
            self.assertNotIn('data', opened)
            self.assertEqual(set(opened), set(request) | {'ret', 'status', 'effective_at'})
            self.assertEqual(opened['status'], 'active')
            self.assertTrue(opened['tenant_id'])
            for key, value in request.items():
                if key != 'tenant_id':
                    self.assertEqual(opened[key], value, key)
            self.assertIsNotNone(backend.iso(opened['effective_at']))
            status_request = dict(request, type='STATUS', target_type='OPEN')
            self.assertEqual(provider.lifecycle(status_request, claims), opened)

            # Section 6.2: keep the aggregate anchor and old period; add the new order's period.
            renewal = deepcopy(request)
            renewal.update(type='RENEW', event_id='event-renew-2', idempotency_key='delivery:order-2',
                tenant_id=opened['tenant_id'], order_id='order-2', order_no='ORD-example-2',
                expires_at='2026-12-19T08:00:00+00:00')
            renewal['periods'].append(dict(deepcopy(request['periods'][0]), order_id=renewal['order_id'],
                order_no=renewal['order_no'], term_id='term-2', starts_at=request['expires_at'],
                expires_at=renewal['expires_at']))
            renewal = backend.validate_payload(renewal)
            renewed = provider.lifecycle(renewal, claims)
            self.assertEqual(renewed['starts_at'], opened['starts_at'])
            self.assertEqual(renewed['periods'][0], opened['periods'][0])
            self.assertEqual(renewed['periods'][1]['starts_at'], opened['expires_at'])
            self.assertEqual(renewed['periods'], renewal['periods'])
            self.assertEqual(renewed['tenant_id'], opened['tenant_id'])
            self.assertEqual(provider.lifecycle(renewal, claims), renewed)
            self.assertEqual(provider.lifecycle(request, claims), opened)
            self.assertEqual(provider.lifecycle(status_request, claims), renewed)
            self.assertEqual(provider.lifecycle(dict(renewal, type='STATUS', target_type='RENEW'), claims), renewed)

    def test_scope_table_and_introspection_example_match_mock_auth(self):
        rows = {}
        for line in self.section('3.1').splitlines():
            cells = [cell.strip() for cell in line.strip('|').split('|')]
            if len(cells) == 2:
                scopes = re.findall(r'\bresource\.[a-z]+\b', cells[1])
                if scopes:
                    rows[cells[0]] = scopes
        self.assertEqual(len(rows), 2)
        self.assertEqual([scope for key, scopes in rows.items() if 'APISIX' in key for scope in scopes],
                         ['resource.invoke'])
        self.assertEqual([scope for key, scopes in rows.items() if 'APISIX' not in key for scope in scopes],
                         ['resource.manage'])
        claims, = self.examples('3.2')
        self.assertIs(claims['active'], True)
        self.assertEqual(claims['product_id'], self.open_example()['product_id'])
        self.assertEqual(claims['scope'], 'resource.manage')
        response = httpx.Response(200, json=claims, request=httpx.Request('POST', 'https://platform.example/oauth/introspect'))
        with patch.object(provider.httpx, 'post', return_value=response), patch.dict('os.environ', {'MOCK_API_PRODUCT_ID': ''}):
            self.assertEqual(provider.require_platform_token('Bearer example', required_scope='resource.manage'), claims)
            with self.assertRaises(HTTPException) as denied:
                provider.require_platform_token('Bearer example')
            self.assertEqual(denied.exception.status_code, 403)

    def test_metadata_names_and_protocol_match_actual_download_function(self):
        tree = ast.parse((ROOT / 'backend/app/main.py').read_text(encoding='utf-8'))
        download = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == 'download_oauth_credentials')
        strings = [node.value for node in ast.walk(download) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        metadata = dict(value.split(':', 1) for value in strings if re.match(r'^api_(?:management|business)_\w+:', value))
        self.assertEqual(set(metadata), {'api_management_url', 'api_management_protocol',
                                        'api_management_scope', 'api_business_scope'})
        for key, value in metadata.items():
            self.assertIn(key, self.section('3.3'))
            if value.strip():
                self.assertIn(value.strip(), self.document)
        self.assertEqual(metadata['api_management_protocol'].strip(), 'market-api-management-v1')
        self.assertEqual(metadata['api_management_scope'].strip(), 'resource.manage')
        self.assertEqual(metadata['api_business_scope'].strip(), 'resource.invoke')

    def test_root_receipt_status_and_provider_only_interface_boundaries(self):
        for number, tokens in {
            '5.1': ('ret=0', 'data', 'event_id', 'idempotency_key'),
            '5.4': ('ret', 'status', 'effective_at', 'tenant_id'),
            '6.7': ('target_type', 'event_id', 'idempotency_key', 'effective_at', 'STATUS'),
            '3.3': ('/api/products/{product_id}/oauth-credentials-download',),
            '9.': ('/api/provider/products/{product_id}/entitlements',),
        }.items():
            section = self.section(number)
            for token in tokens:
                with self.subTest(section=number, token=token):
                    self.assertIn(token, section)
        for buyer_interface in ('/api/orders/', '/gateway-credentials', '/api/enterprise/api-credentials'):
            self.assertNotIn(buyer_interface, self.document)


if __name__ == '__main__':
    unittest.main()
