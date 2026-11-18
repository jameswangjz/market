"""Verified worker-only image/probe update; never touches billing or Redis data."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=300, **kwargs).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.root.resolve()
    run(sys.executable, str(root / 'scripts/trading_artifact_gate.py'), 'verify', '--repo', str(root),
        '--commit', args.commit, '--tag', args.tag, '--manifest-sha256', args.manifest_sha256)
    original = json.loads(run('kubectl', '-n', 'market', 'get', 'deployment', 'api-charge-ledger-worker', '-o', 'json'))
    spec = original['spec']['template']['spec']
    index = next(i for i, container in enumerate(spec['containers']) if container['name'] == 'worker')
    container = spec['containers'][index]
    if original['spec'].get('replicas', 1) != 1 or not container['image'].startswith('market-api:'):
        raise ValueError('Unexpected worker runtime; manual review required')
    backup = Path('/root/market/backups') / ('charge-worker-health-' + args.tag)
    backup.mkdir(mode=0o700)
    (backup / 'before.json').write_text(json.dumps(original))
    desired = json.loads(run('kubectl', 'create', '--dry-run=client', '-f', str(root / 'k8s/api-charge-ledger.yaml'), '-o', 'json'))
    probes = desired['spec']['template']['spec']['containers'][0]
    probes = {name: {**container[name], **probes[name]}
              for name in ('startupProbe', 'readinessProbe', 'livenessProbe')}
    image = 'market-api:' + args.tag
    build = run('nerdctl', '--namespace', 'k8s.io', 'build', '--build-arg', 'BASE_IMAGE=' + container['image'],
                '-t', image, '-f', str(root / 'backend/Dockerfile.runtime'), str(root / 'backend'))
    (backup / 'build.log').write_text(build)
    base = f'/spec/template/spec/containers/{index}/'
    latest = json.loads(run('kubectl', '-n', 'market', 'get', 'deployment', 'api-charge-ledger-worker', '-o', 'json'))
    if latest['metadata']['uid'] != original['metadata']['uid'] or latest['spec'] != original['spec']:
        raise ValueError('Worker runtime changed during build; no patch applied')
    changes = [dict(op='test', path='/metadata/resourceVersion', value=latest['metadata']['resourceVersion']),
               dict(op='replace', path=base + 'image', value=image)]
    for name in ('startupProbe', 'readinessProbe', 'livenessProbe'):
        changes.append(dict(op='replace', path=base + name, value=probes[name]))
    run('kubectl', '-n', 'market', 'patch', 'deployment', 'api-charge-ledger-worker', '--type=json', '-p', json.dumps(changes))
    try:
        run('kubectl', '-n', 'market', 'rollout', 'status', 'deployment/api-charge-ledger-worker', '--timeout=180s')
    except Exception:
        rollback = [dict(op='replace', path=base + 'image', value=container['image'])]
        for name in ('startupProbe', 'readinessProbe', 'livenessProbe'):
            rollback.append(dict(op='replace', path=base + name, value=container[name]))
        run('kubectl', '-n', 'market', 'patch', 'deployment', 'api-charge-ledger-worker', '--type=json', '-p', json.dumps(rollback))
        raise
    current = json.loads(run('kubectl', '-n', 'market', 'get', 'deployment', 'api-charge-ledger-worker', '-o', 'json'))
    expected = json.loads(json.dumps(original['spec']))
    expected_container = expected['template']['spec']['containers'][index]
    expected_container['image'] = image
    for name in ('startupProbe', 'readinessProbe', 'livenessProbe'):
        expected_container[name] = probes[name]
    if current['spec'] != expected:
        raise ValueError('Unexpected worker spec change; inspect private backup')
    print(json.dumps({'worker': 'api-charge-ledger-worker', 'tag': args.tag, 'ready': True,
                      'billing_data_changed_by_deployment': False, 'backup': str(backup)}))


if __name__ == '__main__':
    main()
