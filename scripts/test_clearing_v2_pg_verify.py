"""Runner allowlist checks with all database/process operations mocked."""
from contextlib import redirect_stderr
from io import StringIO
import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy.engine import make_url

import clearing_v2_pg_verify as runner


class ClearingPostgresRunnerTests(unittest.TestCase):
    def setUp(self):
        self.database = 'market_clr2_test_' + 'a' * 16
        self.connection = MagicMock()
        self.admin = MagicMock()
        self.admin.connect.return_value.__enter__.return_value = self.connection
        self.engine = self.enterContext(patch.object(runner, 'create_engine', return_value=self.admin))
        self.process = self.enterContext(patch.object(runner.subprocess, 'run', return_value=SimpleNamespace(returncode=0)))
        self.enterContext(patch.object(runner.secrets, 'token_hex', return_value='a' * 16))
        self.enterContext(patch.dict(os.environ, DATABASE_URL='postgresql://tester:fake@example.invalid/business_db'))

    def assert_isolated(self, module):
        admin_url = self.engine.call_args.args[0]
        self.assertEqual(admin_url.database, 'postgres')
        self.assertEqual(self.engine.call_args.kwargs['isolation_level'], 'AUTOCOMMIT')
        self.assertEqual(self.connection.exec_driver_sql.call_args_list[0].args,
                         ('CREATE DATABASE "' + self.database + '"',))
        self.assertEqual(self.connection.exec_driver_sql.call_args_list[-1].args,
                         ('DROP DATABASE "' + self.database + '" WITH (FORCE)',))
        command = self.process.call_args.args[0]
        self.assertEqual(command[-2:], [module, '-v'])
        test_url = make_url(self.process.call_args.kwargs['env']['CLEARING_TEST_DATABASE_URL'])
        self.assertEqual(test_url.database, self.database)
        self.assertNotEqual(test_url.database, make_url(os.environ['DATABASE_URL']).database)
        self.admin.dispose.assert_called_once_with()

    def test_funded_combination_pg_http_is_allowlisted_and_isolated(self):
        module = 'test_settlement_funded_combinations_pg_http'
        runner.main(['--module', module])
        self.assert_isolated(module)

    def test_existing_default_suite_remains_allowed(self):
        runner.main([])
        self.assert_isolated('test_settlement_finance_pg')

    def test_version_funding_class_selector_is_allowlisted_and_isolated(self):
        module = 'test_settlement_funded_combinations_pg_http.VersionFundingPostgresHTTPTests'
        runner.main(['--module', module])
        self.assert_isolated(module)

    def test_unknown_module_rejected_before_database_or_process(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
            runner.main(['--module', 'not_an_allowlisted_suite'])
        self.assertEqual(error.exception.code, 2)
        self.engine.assert_not_called()
        self.process.assert_not_called()

    def test_suite_failure_still_drops_only_its_disposable_database(self):
        module = 'test_settlement_funded_combinations_pg_http'
        self.process.return_value = SimpleNamespace(returncode=1)
        with self.assertRaisesRegex(RuntimeError, 'Isolated PostgreSQL suite failed'):
            runner.main(['--module', module])
        self.assert_isolated(module)


if __name__ == '__main__':
    unittest.main()
