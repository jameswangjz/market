"""Safety guards for a script intended to run against a development database."""
import ast
from pathlib import Path
import unittest


class RuntimeProofGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = Path(__file__).with_name('sla_runtime_acceptance.py')
        cls.text = cls.path.read_text()
        cls.tree = ast.parse(cls.text)

    def test_no_testclient_lifespan_context(self):
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    self.assertNotEqual(getattr(getattr(item.context_expr, 'func', None), 'id', ''), 'TestClient')

    def test_no_ddl_or_global_seed_calls(self):
        forbidden = {'create_all', 'drop_all', 'ensure_schema', 'seed', 'startup', 'init_db'}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                self.assertNotIn(getattr(node.func, 'attr', ''), forbidden)

    def test_fact_cleanup_requires_exact_owned_products(self):
        remove = next(node for node in ast.walk(self.tree)
                      if isinstance(node, ast.FunctionDef) and node.name == 'remove_owned_facts')
        source = ast.get_source_segment(self.text, remove)
        self.assertIn("model.product_id.in_(saved['products'])", source)
        self.assertIn('delete(model).where(', source)

    def test_existing_guarded_preflight_and_cleanup_are_used(self):
        self.assertIn('fixture.preflight(m, saved)', self.text)
        self.assertIn('fixture.cleanup(m, saved, path)', self.text)
        self.assertIn("saved['authorized'] = True", self.text)
        self.assertIn('fixture.write_manifest', self.text)


if __name__ == '__main__':
    unittest.main()
