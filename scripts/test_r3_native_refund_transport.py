"""Transport validation never contacts SSH in these tests."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import r3_native_refund_transport as transport


class TransportTests(unittest.TestCase):
    def invoke(self, *extra, output=None):
        argv = ['probe', '--pod', 'market-api-a1-b2', '--manifest',
                '/tmp/qa-native-unit.json', *extra]
        if output:
            argv += ['--output', str(output)]
        stdout = io.StringIO()
        with patch('sys.argv', argv), patch.dict(os.environ, SSHPASS='not-a-real-secret'), \
                contextlib.redirect_stdout(stdout):
            code = transport.main()
        return code, json.loads(stdout.getvalue())

    def test_success_is_provenanced_and_archived(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'evidence.json'
            with patch.object(transport.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=0, stdout='{"status":"passed"}')) as run:
                code, result = self.invoke(output=output)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.read_text()), result)
            self.assertEqual(len(result['helper_sources_sha256']), 5)
            self.assertNotIn('not-a-real-secret', repr(run.call_args))
            self.assertEqual(run.call_args.kwargs['timeout'], 240)

    def test_failure_remains_failure(self):
        with patch.object(transport.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout='{"status":"failed","error":"owned_graph"}')):
            code, result = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result['error'], 'owned_graph')

    def test_malformed_transport_suppresses_diagnostics(self):
        with patch.object(transport.subprocess, 'run', return_value=SimpleNamespace(
                returncode=1, stdout='private-diagnostic')):
            code, result = self.invoke()
        self.assertEqual(code, 1)
        self.assertNotIn('private-diagnostic', str(result))

    def test_invalid_pod_never_starts_transport(self):
        with patch.object(transport.subprocess, 'run') as run, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke('--pod', 'foreign-pod')
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
