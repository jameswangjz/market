"""Offline transport checks; never contact SSH or Kubernetes."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import r3_sso_runtime_transport as transport


def args(mode='prepare', **kwargs):
    return SimpleNamespace(mode=mode, manifest='/tmp/qa-r3-sso-offline.json',
                           runtime_tag='20261015123456-abcdef0' if mode == 'prepare' else None,
                           state=kwargs.pop('state', None), **kwargs)


class TransportTests(unittest.TestCase):
    def test_invalid_manifest_commands_never_start_process(self):
        for path in ('relative.json', '/tmp/qa-r3-sso-../escape.json',
                     '/tmp/qa-r3-sso-a.json;id', '/etc/qa-r3-sso-a.json',
                     '/tmp/qa-r3-sso-a/b.json', '/tmp/qa-r3-sso-.json'):
            value = args()
            value.manifest = path
            with self.subTest(path=path), patch.object(transport.subprocess, 'run') as run:
                with self.assertRaises(ValueError):
                    transport.execute(value)
                run.assert_not_called()
        value = args('state', state='disable-all')
        with self.assertRaises(ValueError):
            transport.build_program(value)

    def test_secret_only_enters_pod_stdin_on_prepare(self):
        secret = 'offline-secret-sentinel'
        for mode in ('prepare', 'restore'):
            calls = []

            def fake_run(argv, **kwargs):
                calls.append((argv, kwargs))
                self.assertNotIn(secret, repr(argv))
                self.assertNotIn('SSHPASS', repr(argv))
                if 'get' in argv:
                    return SimpleNamespace(returncode=0, stdout=base64.b64encode(secret.encode()))
                source = kwargs['input'].decode()
                namespace = {}
                exec(source.split("\ntry:\n", 1)[0], namespace)
                self.assertEqual(namespace['p'].get('secret'), secret if mode == 'prepare' else None)
                return SimpleNamespace(returncode=0, stdout=b'{"status":"passed"}')

            output = io.StringIO()
            with patch('subprocess.run', side_effect=fake_run), contextlib.redirect_stdout(output):
                exec(transport.build_program(args(mode)), {})
            self.assertEqual(len(calls), 2 if mode == 'prepare' else 1)
            self.assertNotIn(secret, output.getvalue())
            self.assertEqual(json.loads(output.getvalue())['status'], 'passed')

    def test_sources_compile_and_public_callback_is_fixed(self):
        program = transport.build_program(args())
        compile(program, '<host>', 'exec')
        compile('p = {}\n' + transport.POD, '<pod>', 'exec')
        namespace = {}
        exec(program.decode().split('\nprogram = ', 1)[0], namespace)
        command = namespace['payload']['args']
        self.assertEqual(command[command.index('--rp-public') + 1], 'http://192.168.10.10:30083')
        self.assertEqual(command[command.index('--base-url') + 1], 'http://192.168.10.10:30080')

    def test_private_copy_permissions_and_no_overwrite(self):
        response = SimpleNamespace(returncode=0, stdout=json.dumps([{}, {}, {'password':'private'}]).encode())
        with patch.dict(os.environ, {'SSHPASS':'ssh-password-sentinel'}), \
                patch.object(transport.subprocess, 'run', return_value=response) as run:
            first = transport.execute(args('copy-private'))
            second = transport.execute(args('copy-private'))
        try:
            self.assertNotEqual(first['private_directory'], second['private_directory'])
            for result in (first, second):
                directory = Path(result['private_directory'])
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                self.assertEqual(len(list(directory.iterdir())), 3)
                for path in directory.iterdir():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('ssh-password-sentinel', repr(run.call_args.args))
            self.assertNotIn('password', json.dumps(first))
        finally:
            for result in (first, second):
                shutil.rmtree(result['private_directory'])

    def test_failure_output_suppresses_remote_details(self):
        response = SimpleNamespace(returncode=1, stdout=b'secret', stderr=b'secret')
        with patch.dict(os.environ, {'SSHPASS':'secret'}), \
                patch.object(transport.subprocess, 'run', return_value=response):
            with self.assertRaisesRegex(RuntimeError, '^Transport failed; journals retained$'):
                transport.execute(args('restore'))

    def test_cleanup_rp_handoff_precedes_postgres_cleanup(self):
        control = {'run_id':'qa-r3-sso-123456abcdef', 'rp_cleanup_verified':False}
        artifacts = [{'run_id':control['run_id']}, control]
        calls = []

        def fake_run(argv, **kwargs):
            namespace = {}
            exec(kwargs['input'].decode().split('\ntry:\n', 1)[0], namespace)
            payload = namespace['p']
            calls.append((argv, payload))
            if len(calls) == 1:
                self.assertEqual(payload['op'], 'read')
                result = artifacts
            elif len(calls) == 2:
                self.assertIn('deployment/market-mock-saas', argv)
                self.assertEqual(payload['artifacts'], artifacts)
                self.assertEqual(payload['args'], ['--cleanup-rp','--rp-store','/data/mock-saas.sqlite3'])
                result = {'status':'passed', 'control':dict(control, rp_cleanup_verified=True)}
            else:
                self.assertIn('deployment/market-api', argv)
                self.assertEqual(payload['op'], 'write-control')
                self.assertEqual(payload['previous'], control)
                self.assertTrue(payload['control']['rp_cleanup_verified'])
                self.assertNotIn('artifacts', payload)
                result = {'status':'passed'}
            return SimpleNamespace(returncode=0, stdout=json.dumps(result).encode())

        with patch('subprocess.run', side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
            exec(transport.build_program(args('cleanup-rp')), {})
        self.assertEqual(len(calls), 3)
        self.assertTrue(all('--cleanup' not in p['args'] for _, p in calls))

    def test_sanitized_failure_fields_survive_transport(self):
        value = {'status':'failed', 'phase':'api_helper', 'error_type':'HelperFailure',
                 'helper_error_type':'AcceptanceFailure', 'stderr':'private-message'}
        with patch.dict(os.environ, {'SSHPASS':'secret'}), patch.object(
                transport.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=1, stdout=json.dumps(value).encode())):
            with self.assertRaises(transport.TransportFailure) as caught:
                transport.execute(args())
        self.assertEqual(caught.exception.details, {k:v for k,v in value.items() if k != 'stderr'})
        failure = transport.TransportFailure('private-phase', 'secret with spaces', 'bad.message')
        self.assertEqual(failure.details['phase'], 'ssh_transport')
        self.assertEqual(failure.details['error_type'], 'UnknownError')
        self.assertEqual(failure.details['helper_error_type'], 'UnknownError')


if __name__ == '__main__':
    unittest.main()
