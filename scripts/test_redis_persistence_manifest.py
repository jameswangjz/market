"""Validate Redis persistence prerequisites and the consuming Deployment."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class RedisPersistenceManifestTests(unittest.TestCase):
    def test_persistence_contract(self):
        documents = list(yaml.safe_load_all((ROOT / 'k8s/redis-persistence.yaml').read_text()))
        config, pvc = documents
        self.assertEqual(config['metadata'], {'name': 'market-redis-config', 'namespace': 'market'})
        settings = dict(line.split(maxsplit=1) for line in config['data']['redis.conf'].splitlines())
        for key, value in {'dir': '/data', 'appendonly': 'yes', 'appendfsync': 'everysec',
                           'save': '3600 1', 'stop-writes-on-bgsave-error': 'yes',
                           'maxmemory-policy': 'noeviction'}.items():
            self.assertEqual(settings[key], value)
        self.assertEqual(pvc['metadata']['name'], 'market-redis-data')
        self.assertEqual(pvc['spec']['storageClassName'], 'local-path')
        self.assertEqual(pvc['spec']['resources']['requests']['storage'], '10Gi')
        self.assertEqual(pvc['spec']['accessModes'], ['ReadWriteOnce'])
        deployment = next(doc for doc in yaml.safe_load_all((ROOT / 'k8s/all-in-one.yaml').read_text())
                          if doc and doc['kind'] == 'Deployment' and doc['metadata']['name'] == 'market-redis')
        spec = deployment['spec']
        self.assertEqual(spec['replicas'], 1)
        self.assertEqual(spec['strategy']['type'], 'Recreate')
        pod = spec['template']['spec']
        volumes = {volume['name']: volume for volume in pod['volumes']}
        self.assertEqual(volumes['data']['persistentVolumeClaim']['claimName'], pvc['metadata']['name'])
        self.assertEqual(volumes['config']['configMap']['name'], config['metadata']['name'])
        container = pod['containers'][0]
        self.assertEqual(container['args'], ['redis-server', '/etc/redis/redis.conf'])
        self.assertIn({'name': 'data', 'mountPath': '/data'}, container['volumeMounts'])
        for probe in ('readinessProbe', 'livenessProbe', 'startupProbe'):
            self.assertEqual(container[probe]['exec']['command'], ['redis-cli', 'ping'])


if __name__ == '__main__':
    unittest.main()
