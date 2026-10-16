import unittest

import sla_console_tasks_control as batch


class TaskDefinitionsTests(unittest.TestCase):
    def test_scope_and_dependencies_are_complete_and_acyclic(self):
        rows = batch.control.TASKS
        codes = {row[0] for row in rows}
        self.assertEqual(len(rows), 11)
        self.assertEqual(len(codes), len(rows))
        seen = set()
        for code, title, area, dependencies, acceptance in rows:
            self.assertTrue(code.startswith('SLC-'))
            self.assertTrue(title and acceptance)
            self.assertIn(area, {'ARC', 'BE', 'FE', 'QA', 'OPS'})
            for dependency in filter(None, dependencies.split(',')):
                self.assertIn(dependency, seen)
            seen.add(code)

    def test_document_basis_is_current_batch(self):
        self.assertIn('20261016155059', batch.control.DOCUMENT_BASIS)
        self.assertNotIn('列表检索', batch.control.DOCUMENT_BASIS)


if __name__ == '__main__':
    unittest.main()
