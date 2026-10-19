"""Documentation contracts, using only JSON examples and a private mock Store."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = ROOT / 'docs/saas-integration-standard.md'
FENCES = re.compile(r'^```json\s*\n(.*?)^```\s*$', re.MULTILINE | re.DOTALL)
spec = importlib.util.spec_from_file_location(
    'saas_documentation_store', ROOT / 'mock-saas/app/store.py')
store_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store_module)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 19, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class SaasIntegrationSpecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = DOCUMENT.read_text(encoding='utf-8')

    def section(self, number):
        match = re.search(
            r'^### ' + re.escape(number) + r'\s[^\n]*\n(.*?)(?=^#{1,3} \d|\Z)',
            self.document, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, 'Missing section ' + number)
        return match.group(1)

    def examples(self, number):
        return [json.loads(match.group(1)) for match in FENCES.finditer(self.section(number))]

    def mentions(self, number, *tokens):
        section = self.section(number)
        for token in tokens:
            with self.subTest(section=number, token=token):
                self.assertIn(token, section)

    def test_canonical_copies_are_byte_identical(self):
        for path in (ROOT / 'frontend/public/saas-integration-standard.md',
                     ROOT / 'docs/SaaS应用第三方接口对接规范.md'):
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual(DOCUMENT.read_bytes(), path.read_bytes())

    def test_every_json_fence_parses(self):
        fences = list(FENCES.finditer(self.document))
        self.assertTrue(fences)
        self.assertEqual(len(fences), len(re.findall(r'^```json\s*$', self.document, re.MULTILINE)))
        for match in fences:
            with self.subTest(line=self.document[:match.start()].count('\n') + 1):
                json.loads(match.group(1))

    def test_open_core_and_root_response(self):
        request, response = self.examples('3.1')
        required = {'product_id', 'enterprise_id', 'subscription_id', 'version'}
        self.assertEqual(request['type'], 'OPEN')
        self.assertTrue(required | {'admin_user_id', 'oidc_client_id', 'idempotency_key'} <= request.keys())
        self.assertTrue(required | {'ret', 'tenant_id', 'status'} <= response.keys())
        self.assertIs(type(response['ret']), int)
        self.assertEqual(response['ret'], 0)
        self.assertTrue(response['tenant_id'])
        for field in required:
            self.assertEqual(response[field], request[field])
        self.assertNotIn('expires_at', request)
        self.assertNotIn('subscription_months', request)
        self.mentions('3.1', 'reconciliation_context', 'provisional', 'null', 'RENEW', 'STATUS')

    def test_renew_full_request_and_matching_order_context(self):
        request, response = self.examples('3.3')
        required = {'type', 'tenant_id', 'product_id', 'enterprise_id', 'subscription_id',
                    'version', 'subscription_months', 'starts_at', 'expires_at',
                    'platform_delivery_at', 'admin_user_id', 'oidc_client_id',
                    'idempotency_key', 'reconciliation_context'}
        self.assertTrue(required <= request.keys())
        self.assertEqual(request['type'], 'RENEW')
        self.assertIs(type(request['subscription_months']), int)
        self.assertEqual(request['subscription_months'], 3)
        self.assertNotIn('expected_expires_at', request)
        self.assertNotIn('billing_cycle', request)
        context = request['reconciliation_context']
        for field in ('tenant_id', 'product_id', 'enterprise_id', 'subscription_id'):
            self.assertEqual(context[field], request[field])
        for field, root in (('event_id', 'idempotency_key'), ('event_type', 'type'),
                            ('order_starts_at', 'starts_at'), ('order_ends_at', 'expires_at'),
                            ('subscription_expires_at', 'expires_at'),
                            ('effective_at', 'platform_delivery_at')):
            self.assertEqual(context[field], request[root])
        self.assertEqual(context['period_status'], 'confirmed')
        self.assertEqual(context['effective_at_semantics'], 'platform_delivery')
        for field in ('order_id', 'order_no'):
            self.assertTrue(context[field])
            self.assertNotIn(field, request)
        start = datetime.fromisoformat(request['starts_at'])
        end = datetime.fromisoformat(request['expires_at'])
        self.assertEqual((end.year - start.year) * 12 + end.month - start.month, 3)
        self.assertEqual(end.day, start.day)
        self.assertLess(datetime.fromisoformat(request['platform_delivery_at']), start)
        self.assertIs(type(response['ret']), int)
        self.assertEqual(response['ret'], 0)
        self.assertEqual(response['status'], 'active')
        for field in ('tenant_id', 'product_id', 'enterprise_id', 'subscription_id',
                      'starts_at', 'expires_at', 'platform_delivery_at'):
            self.assertEqual(response[field], request[field])

    def test_user_assign_response_preserves_both_identities(self):
        request, response = self.examples('4.1')
        self.assertTrue({'ret', 'tenant_id', 'product_id', 'enterprise_id', 'subscription_id',
                         'platform_user_id', 'user_id', 'external_user_id', 'status'} <= response.keys())
        self.assertEqual(response['platform_user_id'], request['user_id'])
        self.assertEqual(response['tenant_id'], request['tenant_id'])
        self.assertTrue(response['external_user_id'])
        self.assertEqual(response['user_id'], response['external_user_id'])
        self.assertNotEqual(response['user_id'], response['platform_user_id'])
        self.assertIs(type(response['ret']), int)
        self.assertEqual(response['ret'], 0)
        self.assertEqual(response['status'], 'active')

    def test_scope_table_matches_real_store(self):
        documented = {}
        for line in self.section('2.1').splitlines():
            cells = [cell.strip() for cell in line.strip('|').split('|')]
            if len(cells) == 2 and re.fullmatch(r'`saas\.\w+`', cells[1]):
                for operation in re.findall(r'\b[A-Z][A-Z_]+\b', cells[0]):
                    documented[operation] = cells[1].strip('`')
        self.assertEqual(documented, store_module.OPERATION_SCOPES)

    def test_auth_callback_precision_errors_and_current_limits(self):
        # Check protocol vocabulary in its owning section, not exact sentences.
        self.assertIn('client_credentials', self.document)
        self.assertIn('/oauth/token', self.document)
        self.assertIn('introspection', self.document)
        self.mentions('8.1', 'HTTPS', 'HTTP', 'issuer')
        self.mentions('8.2', 'state', 'nonce', 'Cookie', 'S256', 'code_verifier',
                      'access_denied', 'authorization_code', 'opaque', 'RS256',
                      'invalid_client', 'invalid_grant', 'redirect_uri')
        self.mentions('8.3', 'userinfo', '401', '403', 'at_hash')
        self.mentions('3.3', 'expected_expires_at', 'UTC', '精度', '409')
        self.mentions('3.6', 'expected_expires_at', 'CAS', '409', '微秒', '六位')
        self.mentions('10.3', 'expected_expires_at', 'Webhook', 'HTTP', '内省', '多副本')
        common = self.document.split('## 2.', 1)[1].split('### 2.1', 1)[0]
        for token in ('根节点', '401', '403', '409', '429', '502', 'Retry-After', 'timeout_ms'):
            with self.subTest(token=token):
                self.assertIn(token, common)

    def test_documented_open_and_renew_execute_and_replay_atomically(self):
        open_request, open_response = self.examples('3.1')
        renew_request, renew_response = self.examples('3.3')
        token = dict(active=True, product_id=open_request['product_id'],
                     scope='saas.tenant saas.user saas.department')
        with tempfile.TemporaryDirectory() as directory, patch.object(
                store_module, 'datetime', FixedDatetime):
            store = store_module.Store(Path(directory) / 'state.sqlite3')
            original_open, original_renew = deepcopy(open_request), deepcopy(renew_request)
            opened = store.machine_operation(open_request, token)
            for field in ('ret', 'product_id', 'enterprise_id', 'subscription_id', 'version', 'status'):
                self.assertEqual(opened[field], open_response[field])
            tenant_id = opened['tenant_id']
            self.assertIsNone(store.tenant(tenant_id).get('expires_at'))
            # OPEN generates an opaque tenant ID: bind the documented RENEW to it.
            renew_request['tenant_id'] = tenant_id
            renew_request['reconciliation_context']['tenant_id'] = tenant_id
            first = store.machine_operation(renew_request, token)
            for field, value in renew_response.items():
                self.assertEqual(first[field], tenant_id if field == 'tenant_id' else value)
            before = store.tenant(tenant_id)
            store = store_module.Store(store.path)
            self.assertEqual(store.machine_operation(open_request, token), opened)
            self.assertEqual(store.machine_operation(renew_request, token), first)
            self.assertEqual(store.tenant(tenant_id), before)
            for request in (open_request, renew_request):
                changed = dict(request, version='different-version')
                with self.subTest(operation=request['type']):
                    with self.assertRaises(store_module.MachineOperationError) as caught:
                        store.machine_operation(changed, token)
                    self.assertEqual(caught.exception.status, 409)
                    self.assertEqual(store.tenant(tenant_id), before)
            later = deepcopy(renew_request)
            later['idempotency_key'] += ':later'
            later['starts_at'] = renew_request['expires_at']
            later['expires_at'] = '2027-05-19T04:00:00+00:00'
            later_context = later['reconciliation_context']
            later_context.update(event_id=later['idempotency_key'], order_id='order-later',
                                 order_no='ORD-later', order_starts_at=later['starts_at'],
                                 order_ends_at=later['expires_at'],
                                 subscription_expires_at=later['expires_at'])
            store.machine_operation(later, token)
            after_later = store.tenant(tenant_id)
            self.assertEqual(store.machine_operation(renew_request, token), first)
            self.assertEqual(store.tenant(tenant_id), after_later)
            self.assertEqual(after_later['expires_at'], later['expires_at'])
            self.assertEqual(open_request, original_open)
            original_renew['tenant_id'] = tenant_id
            original_renew['reconciliation_context']['tenant_id'] = tenant_id
            self.assertEqual(renew_request, original_renew)


if __name__ == '__main__':
    unittest.main()
