"""Offline deployment-script checks: all cluster/build commands are stubs."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/deploy_mock_saas_oidc.sh'
TAG = '20261015104807-032d95d'


class DeployMockSaaSTests(unittest.TestCase):
    def run_deploy(self, placeholder):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'k8s').mkdir()
            manifest = (ROOT / 'k8s/mock-saas-oidc.yaml').read_text()
            (root / 'k8s/mock-saas-oidc.yaml').write_text(
                manifest.replace('REPLACE_WITH_DEPLOYED_TAG', placeholder))
            binary = root / 'bin'
            binary.mkdir()
            stub = '''#!/usr/bin/env python3
import base64, os, pathlib, sys
root = pathlib.Path(os.environ['TEST_OUTPUT'])
with (root / 'calls').open('a') as log:
    log.write(pathlib.Path(sys.argv[0]).name + ' ' + ' '.join(sys.argv[1:]) + '\\n')
if 'jsonpath={.data.secret}' in sys.argv:
    print(base64.b64encode(b'a' * 64).decode())
elif 'apply' in sys.argv:
    path = sys.argv[sys.argv.index('-f') + 1]
    data = sys.stdin.read() if path == '-' else pathlib.Path(path).read_text()
    with (root / 'applied').open('a') as output:
        output.write(data)
'''
            for name in ('kubectl', 'nerdctl'):
                executable = binary / name
                executable.write_text(stub)
                executable.chmod(0o755)
            result = subprocess.run(['bash', str(SCRIPT)], cwd=root,
                env={**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH'],
                     'RUNTIME_TAG': TAG, 'TEST_OUTPUT': str(root)}, capture_output=True, text=True)
            return result, ((root / 'calls').read_text() if (root / 'calls').exists() else ''), \
                ((root / 'applied').read_text() if (root / 'applied').exists() else '')

    def test_current_and_legacy_placeholders_render_target_image(self):
        for placeholder in ('REPLACE_WITH_DEPLOYED_TAG', 'oidc-development'):
            with self.subTest(placeholder=placeholder):
                result, calls, applied = self.run_deploy(placeholder)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('image: market-mock-saas:' + TAG, applied)
                self.assertNotIn(placeholder, applied)
                self.assertIn('nerdctl --namespace k8s.io build', calls)
                self.assertIn('rollout status', calls)

    def test_unknown_or_residual_placeholder_fails_before_external_calls(self):
        for placeholder in ('unknown-tag', 'REPLACE_WITH_UNKNOWN',
                            'REPLACE_WITH_DEPLOYED_TAG\n          unresolved: REPLACE_WITH_SECRET'):
            with self.subTest(placeholder=placeholder):
                result, calls, applied = self.run_deploy(placeholder)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('refusing deployment', result.stderr)
                self.assertEqual((calls, applied), ('', ''))


if __name__ == '__main__':
    unittest.main()
