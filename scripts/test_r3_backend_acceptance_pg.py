"""Offline ownership and evidence sanitization guards; never opens a database."""
import unittest

import r3_backend_acceptance_pg as acceptance


class AcceptanceGuardTests(unittest.TestCase):
    def test_only_random_owned_schema_is_eligible_for_cleanup(self):
        self.assertTrue(acceptance.owned_schema('qa_r3_be_0123456789abcdef'))
        for name in ('public', 'qa_r3_be_', 'qa_r3_be_0123456789abcdef.public',
                     'qa_r3_be_0123456789abcdef;DROP SCHEMA public', 'qa_perm_0123456789abcdef'):
            self.assertFalse(acceptance.owned_schema(name))

    def test_errors_never_include_exception_text(self):
        try:
            raise ValueError('PRIVATE_SENTINEL password token DATABASE_URL')
        except ValueError as exc:
            result = acceptance.safe_error(exc)
        self.assertEqual(set(result), {'status', 'error_type', 'line'})
        self.assertNotIn('PRIVATE_SENTINEL', str(result))

    def test_retired_and_all_forged_mapping_cases_are_explicit(self):
        self.assertEqual(len(acceptance.INVALID_TYPES), 10)
        self.assertIn(('report', 'file'), acceptance.INVALID_TYPES)
        self.assertIn(('application', 'file'), acceptance.INVALID_TYPES)
        self.assertIn(('model', 'model_api'), acceptance.INVALID_TYPES)

    def test_platform_role_matrix_contains_only_one_system_admin(self):
        self.assertEqual(acceptance.PLATFORM_ROLES.count('super_admin'), 1)
        self.assertIn('platform_operator', acceptance.PLATFORM_ROLES)
        self.assertIn('delivery_monitor', acceptance.PLATFORM_ROLES)


if __name__ == '__main__':
    unittest.main()
