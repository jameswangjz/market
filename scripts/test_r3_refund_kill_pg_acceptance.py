"""Offline guards; subprocess tests do not connect to PostgreSQL or remote hosts."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import r3_refund_kill_pg_acceptance as probe


@unittest.skipUnless(os.name == 'posix', 'SIGKILL requires POSIX')
class KillGuards(unittest.TestCase):
    def child(self, program, check, timeout=2):
        return probe.kill_child([sys.executable, '-B', '-c', program],
            dict(stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True), check, timeout)

    def test_real_handshake_and_sigkill(self):
        checked = []
        result = self.child('import signal\nprint(' + repr(probe.MARKER)
                            + ', flush=True)\nsignal.pause()', lambda: checked.append(True))
        self.assertEqual(checked, [True])
        self.assertEqual(result.returncode, 0)

    def test_failed_durable_check_reaps_child(self):
        def fail():
            raise AssertionError('not durable')
        with self.assertRaises(AssertionError):
            self.child('import signal\nprint(' + repr(probe.MARKER)
                       + ', flush=True)\nsignal.pause()', fail)

    def test_timeout_reaps_with_sigkill(self):
        created = []
        original = subprocess.Popen

        def spawn(*args, **kwargs):
            child = original(*args, **kwargs)
            created.append(child)
            return child

        with patch.object(probe.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaises(probe.base.AcceptanceFailure):
                self.child('import signal; signal.pause()', lambda: None, timeout=0.1)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].returncode, -probe.signal.SIGKILL)
        with self.assertRaises(ProcessLookupError):
            os.kill(created[0].pid, 0)

    def test_timeout_and_missing_handshake(self):
        for program in ('import signal; signal.pause()', 'print("wrong", flush=True)',
                        'import os, signal; os.write(1, b"partial"); signal.pause()'):
            with self.assertRaises(probe.base.AcceptanceFailure):
                self.child(program, lambda: self.fail('Must not inspect DB'), timeout=0.1)

    def test_reuses_owned_harness_and_labels_evidence(self):
        def remote(encoded, manifest):
            self.assertIs(probe.execution.cross.seed, probe.execution.seed)
            self.assertIs(probe.execution.cross.exercise, probe.exercise)
            self.assertIn(probe.CASE, probe.execution.cross.CASE_NAMES)
            self.assertEqual(probe.execution.cross.SOURCE_FILES, probe.execution.SOURCES)
            return dict(status='passed', protected_order_content_read=False)
        with patch.object(probe.execution.cross, 'remote_run', side_effect=remote):
            result = probe.remote_run('offline', {})
        self.assertEqual(result['coverage'], [])
        self.assertIn('Synthetic', result['external_adapters']['resources'])
        self.assertIn('Simulated', result['external_adapters']['funds'])
        self.assertFalse(result['protected_order_content_read'])

    def test_cli_cannot_execute_remote_transport(self):
        result = subprocess.run([sys.executable, '-B', str(Path(probe.__file__))],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('no remote transport entry point', result.stderr)


if __name__ == '__main__':
    unittest.main()
