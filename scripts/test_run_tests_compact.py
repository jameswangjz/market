import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from run_tests_compact import run


class CompactRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / 'test.log'

    def test_success_retains_complete_output_but_bounds_summary(self):
        report = run([sys.executable, '-c',
                      "print('detail\\n'*2000); print('Ran 3 tests in 0.01s'); print('OK')"], self.log)
        self.assertEqual(report['exit_code'], 0)
        self.assertGreater(report['log_lines'], 2000)
        self.assertLess(len(json.dumps(report)), 1000)
        self.assertIn('OK', report['summary'])

    def test_failure_has_original_exit_and_diagnostics(self):
        report = run([sys.executable, '-c',
                      "print('FAIL: permission_guard'); raise SystemExit(7)"], self.log)
        self.assertEqual(report['exit_code'], 7)
        self.assertEqual(report['failures'], ['FAIL: permission_guard'])

    def test_unknown_failure_keeps_tail(self):
        report = run([sys.executable, '-c',
                      "print('unrecognized crash'); raise SystemExit(2)"], self.log)
        self.assertEqual(report['diagnostic_tail'], ['unrecognized crash'])

    def test_log_is_not_overwritten(self):
        self.log.write_text('previous evidence')
        with self.assertRaises(FileExistsError):
            run([sys.executable, '-c', 'pass'], self.log)
        self.assertEqual(self.log.read_text(), 'previous evidence')

    def test_cli_preserves_failure_exit(self):
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('run_tests_compact.py')),
                                 '--log', str(self.log), '--', sys.executable, '-c',
                                 'raise SystemExit(9)'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 9)
        self.assertEqual(json.loads(result.stdout)['exit_code'], 9)


if __name__ == '__main__':
    unittest.main()
