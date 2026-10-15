import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('sla_deploy', Path(__file__).with_name('deploy_sla_runtime.py'))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeploymentGuardTests(unittest.TestCase):
    def test_runtime_deployment_updates_both_sla_writers(self):
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'deploy_trading_runtime.sh'
        content = script.read_text()
        for template in ('ticket-sla-monitor.yaml', 'sla-online-monitor.yaml'):
            self.assertIn('if [[ -f k8s/' + template + ' ]]; then', content)
            self.assertIn('k8s/' + template + ' | kubectl apply -f -', content)
        self.assertIn('market-api:${TAG}', content)

    def test_bad_provenance_never_accesses_cluster(self):
        args = ['deploy', '--commit', 'a' * 40, '--tag', '20261015070000-aaaaaaa',
                '--manifest-sha256', 'b' * 64, '--probe-source-cidrs', '10.244.0.0/16']
        with patch.object(sys, 'argv', args), patch.object(deploy.subprocess, 'run',
                side_effect=subprocess.CalledProcessError(1, ['verify'])), patch.object(deploy, 'get') as get:
            with self.assertRaises(subprocess.CalledProcessError):
                deploy.main()
            get.assert_not_called()

    def test_secret_reference_preserves_unrelated_environment(self):
        spec = {'template': {'spec': {'containers': [{'name': 'api', 'image': 'verified:tag',
            'env': [{'name': 'EXISTING', 'value': 'keep'}, {'name': 'SLA_PROBE_SECRET', 'value': 'obsolete'}]},
            {'name': 'sidecar', 'image': 'do-not-change'}]}}}
        deploy.container_env(spec, 'api', ['SLA_PROBE_SECRET'])
        api, sidecar = spec['template']['spec']['containers']
        self.assertEqual(api['image'], 'verified:tag')
        self.assertEqual(api['env'][0], {'name': 'EXISTING', 'value': 'keep'})
        self.assertEqual(api['env'][1]['valueFrom']['secretKeyRef']['name'], 'market-sla-telemetry')
        self.assertNotIn('value', api['env'][1])
        self.assertEqual(sidecar, {'name': 'sidecar', 'image': 'do-not-change'})
        deploy.container_env(spec, 'api', ['SLA_PROBE_SECRET'])
        self.assertEqual(len(api['env']), 2)

    def test_kubectl_body_not_in_command_arguments(self):
        body = {'stringData': {'secret': 'must-not-appear-in-argv'}}
        with patch.object(deploy.subprocess, 'check_output', return_value=b'') as run:
            deploy.kubectl('apply', '-f', '-', body=body)
        self.assertNotIn('must-not-appear-in-argv', str(run.call_args.args))
        self.assertEqual(json.loads(run.call_args.kwargs['input']), body)

    def test_patch_uses_structured_json_and_existing_namespace(self):
        with patch.object(deploy.subprocess, 'check_output', return_value=b'') as run:
            deploy.patch('configmap', 'market-apisix-config', {'data': {'config.yaml': 'preserve'}})
        self.assertEqual(run.call_args.args[0][:3], ['kubectl', '-n', 'market'])
        self.assertIn('/dev/stdin', run.call_args.args[0])
        self.assertEqual(json.loads(run.call_args.kwargs['input'])['data']['config.yaml'], 'preserve')


if __name__ == '__main__':
    unittest.main()
