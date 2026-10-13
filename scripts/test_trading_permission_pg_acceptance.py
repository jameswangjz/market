"""Independent guard tests for the bounded permission acceptance runner."""
import importlib.util
import ast
from pathlib import Path
import unittest


SCRIPT = Path(__file__).with_name('trading_permission_pg_acceptance.py')
SPEC = importlib.util.spec_from_file_location('trading_permission_pg_acceptance', SCRIPT)
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


class PermissionAcceptanceGuards(unittest.TestCase):
    def test_sensitive_keys_are_rejected_recursively(self):
        for response in ({'items': [{'nested': {'client_secret': 'x'}}]},
                         [{'metadata': {'unit_cost': 5}}],
                         {'payload': {'api_key': 'x'}},
                         {'settlement': {'cost_amount': 2}},
                         {'settlement': {'profit_amount': 3}},
                         {'order': {'unit_cost_snapshot': 4}},
                         {'order': {'total_cost_snapshot': 5}}):
            with self.subTest(response=response), self.assertRaises(AssertionError):
                acceptance.assert_no_forbidden_keys(response)

    def test_clean_nested_response_is_allowed(self):
        acceptance.assert_no_forbidden_keys({'items': [{'name': 'fixture', 'amount': 10}]})

    def test_safe_secret_download_boolean_is_allowed(self):
        acceptance.assert_no_forbidden_keys({'items': [{'secret_download_available': False}]})
        with self.assertRaises(AssertionError):
            acceptance.assert_no_forbidden_keys({'secret_download_available': 'false'})

    def test_case_inventory_keeps_boundaries_explicit(self):
        self.assertIn('selected_enterprise_isolation', acceptance.CASES)
        self.assertIn('file_and_api_credential_permissions', acceptance.CASES)
        self.assertIn('application_listing_is_member_scoped', acceptance.CASES)

    def test_selected_enterprise_covers_quote_checkout_and_rejections(self):
        source = SCRIPT.read_text(encoding='utf-8')
        for required in ('/api/orders/quote', "'/api/orders'", 'buyer_enterprise_id',
                         'foreign.status_code == 403', 'Ordinary member checkout',
                         'Unverified enterprise checkout', 'Anonymous checkout',
                         'order.buyer_enterprise_id == enterprise_id'):
            with self.subTest(required=required):
                self.assertIn(required, source)

    def test_private_schema_name_is_bounded(self):
        schema = 'qa_perm_' + 'a' * 16
        self.assertLessEqual(len(schema), 36)
        self.assertTrue(schema.startswith('qa_perm_'))

    def test_http_client_is_closed_from_runner_finally(self):
        tree = ast.parse(SCRIPT.read_text(encoding='utf-8'))
        runner = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'run')
        final_calls = [node for item in runner.body if isinstance(item, ast.Try)
                       for node in item.finalbody if isinstance(node, ast.If)]
        self.assertTrue(any(any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                                and call.func.attr == 'close' for call in ast.walk(node))
                            for node in final_calls))


if __name__ == '__main__':
    unittest.main()
