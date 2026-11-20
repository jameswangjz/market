#!/usr/bin/env python3
"""Install the K8S2 release promoter into an existing digest-pinned Flux site."""
import argparse
from pathlib import Path
import re
import subprocess

import yaml

from market_release_bundle import DIGEST, repository

ROOT = Path(__file__).resolve().parents[1]
NAME = 'market-release-promotion'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', required=True)
    parser.add_argument('--image', required=True, help='Pinned Market API runtime image in the target Harbor')
    parser.add_argument('--namespace', default='flux-system')
    parser.add_argument('--target-namespace', default='market')
    parser.add_argument('--db-type', choices=['Postgres', 'Dameng'], default='Postgres')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    target = repository(args.registry, 'market')
    repo, _, digest = args.image.partition('@')
    if (repo != target + '/market-api' or not DIGEST.fullmatch(digest)
            or any(not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', n) or len(n) > 63
                   for n in (args.namespace, args.target_namespace))):
        parser.error('Use the target market-api image digest and valid Kubernetes namespaces')

    commands = ['kubectl', '-n', args.namespace]
    for kind, name in [('secret', 'market-harbor-pull'), ('secret', 'market-harbor-ca'),
                       ('ocirepository', 'market-release'),
                       *[('kustomization', 'market-' + stage) for stage in ('api', 'web', 'workers', 'jobs')]]:
        subprocess.run([*commands, 'get', kind, name, '-o', 'name'], check=True)

    documents = list(yaml.safe_load_all((ROOT / 'k8s/market-release-promotion.yaml').read_text()))
    for obj in documents:
        obj['metadata']['namespace'] = args.namespace
        if obj['kind'] == 'RoleBinding':
            for subject in obj['subjects']:
                subject['namespace'] = args.namespace
        if obj['kind'] == 'CronJob':
            container = obj['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]
            container['image'] = args.image
            for entry in container['env']:
                entry['value'] = {'MARKET_REGISTRY': args.registry,
                                  'MARKET_TARGET_NAMESPACE': args.target_namespace,
                                  'MARKET_DB_TYPE': args.db_type}[entry['name']]

    def resource(kind, name, api='v1', **fields):
        return {'apiVersion': api, 'kind': kind,
                'metadata': {'name': name, 'namespace': args.namespace}, **fields}

    scripts = {name: (ROOT / 'scripts' / name).read_text() for name in (
        'promote_market_release.py', 'verify_market_release.py', 'market_release_bundle.py')}
    documents.append(resource('ConfigMap', NAME + '-scripts', data=scripts))
    durable = [
        resource('ConfigMap', NAME + '-state', data={'paused': 'false', 'state.json': '{}', 'skip-through': ''}),
        resource('Lease', NAME, api='coordination.k8s.io/v1', spec={}),
    ]

    for obj in durable:
        exists = subprocess.check_output([*commands, 'get', obj['kind'], obj['metadata']['name'],
                                          '--ignore-not-found', '-o', 'name'], text=True)
        if not exists:
            subprocess.run([*commands, 'create', '-f', '-', *(['--dry-run=server'] if args.dry_run else [])],
                           input=yaml.safe_dump(obj), text=True, check=True)
    subprocess.run([*commands, 'apply', '--server-side', '-f', '-', *(['--dry-run=server'] if args.dry_run else [])],
                   input=yaml.safe_dump_all(documents), text=True, check=True)


if __name__ == '__main__':
    main()
