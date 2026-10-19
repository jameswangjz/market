#!/usr/bin/env python3
"""Verify by default; deploy a trusted order lifecycle export only with --execute."""
import argparse
import copy
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sys
import time


def load_helpers():
    path = Path(__file__).resolve().with_name('deploy_api_lifecycle_20261019082523.py')
    spec = importlib.util.spec_from_file_location('order_lifecycle_deployment_helpers', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DEPLOYMENTS = {
    'market-api': ('api', 'market-api'),
    'market-web': ('web', 'market-web'),
    'online-delivery-worker': ('worker', 'market-api'),
    'version-change-worker': ('worker', 'market-api'),
    'refund-execution-worker': ('worker', 'market-api'),
}
CRONJOBS = ('application-health', 'api-provider-expiry')
ROLLOUT_ORDER = ('market-api', 'online-delivery-worker', 'version-change-worker',
                 'refund-execution-worker', 'market-web')


def private_write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(value)


def assert_unchanged(current, original):
    if (current['metadata']['uid'] != original['metadata']['uid'] or
            current['spec'] != original['spec']):
        raise ValueError('Runtime drift detected; operator reconciliation required')


def replace_image(helpers, kind, name, original, cname, image):
    current = helpers.get(kind, name)
    assert_unchanged(current, original)
    body = helpers.image_patch(cname, image)
    if kind == 'cronjob':
        body = {'spec': {'jobTemplate': body}}
    # Optimistic concurrency closes the read/patch window without replacing env.
    body['metadata'] = {'resourceVersion': current['metadata']['resourceVersion']}
    helpers.patch(kind, name, body)
    expected = copy.deepcopy(original)
    helpers.container(expected, cname)['image'] = image
    installed = helpers.get(kind, name)
    assert_unchanged(installed, expected)
    return installed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--release-root', required=True, type=Path)
    parser.add_argument('--backup-root', type=Path, default=Path('/root/market/backups'))
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    helpers = load_helpers()
    root = args.release_root.resolve()
    gate = Path(__file__).resolve().with_name('trading_artifact_gate.py')
    gate_args = [sys.executable, str(gate), 'verify', '--repo', str(root),
                 '--commit', args.commit, '--tag', args.tag,
                 '--manifest-sha256', args.manifest_sha256]
    # Verify independently supplied provenance before any cluster access or build.
    helpers.command(gate_args, timeout=120)
    backup_root = args.backup_root.resolve()
    if backup_root == root or root in backup_root.parents or backup_root in root.parents:
        raise ValueError('Backup root must be separate from the trusted export')
    deployments = {name: helpers.get('deployment', name) for name in DEPLOYMENTS}
    crons = {name: helpers.get('cronjob', name) for name in CRONJOBS}
    postgres = helpers.get('deployment', 'market-postgres')
    postgres_image = helpers.container(postgres, 'postgres')['image']
    for name, (cname, prefix) in DEPLOYMENTS.items():
        original = deployments[name]
        if not helpers.container(original, cname)['image'].startswith(prefix + ':'):
            raise ValueError('Unexpected runtime base image; review compatibility')
        if original['spec'].get('paused'):
            raise ValueError('Paused deployment requires operator review')
        if name in {'market-api', 'market-web'} and (
                original['spec'].get('replicas', 1) < 1 or
                helpers.container(original, cname)['image'] == prefix + ':' + args.tag):
            raise ValueError('API/web require running replicas and a new release image tag')
        if name == 'market-api' and not helpers.container(original, cname).get('readinessProbe'):
            raise ValueError('API readiness probe required before rolling workers')
        if original.get('status', {}).get('observedGeneration', 0) < original['metadata']['generation']:
            raise ValueError('Deployment reconciliation is in progress')
        helpers.rollout(name)
    for cron in crons.values():
        containers = helpers.podspec(cron)['containers']
        if len(containers) != 1:
            raise ValueError('Unexpected CronJob container layout')
        helpers.container(cron, containers[0]['name'])
    expected_marker = json.loads((root / 'frontend/dist/market-build.json').read_text())
    if not args.execute:
        print('Trusted export and read-only preflight verified; no builds or cluster changes.')
        return 0

    os.umask(0o077)
    backup_root.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(backup_root / '.api-lifecycle.lock',
                      os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'a') as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        backup = backup_root / ('order-lifecycle-' + args.tag + '-' + str(time.time_ns()))
        backup.mkdir(mode=0o700)
        private_write(backup / 'commands.stderr.log', '')
        helpers.command.error_log = backup / 'commands.stderr.log'
        try:
            private_write(backup / 'before.json', json.dumps({
                'deployments': deployments, 'cronjobs': crons, 'postgres': postgres}))
            private_write(backup / 'release.json', json.dumps({
                'commit': args.commit, 'tag': args.tag, 'manifest_sha256': args.manifest_sha256,
                'release_root': str(root), 'marker': expected_marker,
                'rollout_order': ROLLOUT_ORDER, 'cronjobs_image_only': CRONJOBS}))
            # No stdin forwarding: dump bytes go straight into a private host file.
            with (backup / 'postgres.dump.partial').open('xb') as stream:
                helpers.command(['kubectl', '-n', 'market', 'exec', 'deployment/market-postgres',
                                 '--', 'pg_dump', '-U', 'market', '-d', 'market', '-Fc'],
                                output=stream, timeout=300)
            dump = backup / 'postgres.dump.partial'
            with dump.open('rb') as stream:
                signature = stream.read(5)
            if dump.stat().st_size < 32 or signature != b'PGDMP':
                raise ValueError('Invalid custom-format Postgres backup')
            helpers.validate_dump(dump, postgres_image, args.tag)
            dump.rename(backup / 'postgres.dump')
            with (backup / 'build.log').open('xb') as log:
                for name, context in (('market-api', 'backend'), ('market-web', 'frontend')):
                    cname, prefix = DEPLOYMENTS[name]
                    helpers.command(['nerdctl', '--namespace', 'k8s.io', 'build', '--build-arg',
                                     'BASE_IMAGE=' + helpers.container(deployments[name], cname)['image'],
                                     '-t', prefix + ':' + args.tag, '-f',
                                     str(root / context / 'Dockerfile.runtime'), str(root / context)],
                                    output=log, timeout=900)
            # Reverify inputs after building and refuse runtime drift before rollout.
            helpers.command(gate_args, timeout=120)
            for name, original in deployments.items():
                assert_unchanged(helpers.get('deployment', name), original)
            for name, original in crons.items():
                assert_unchanged(helpers.get('cronjob', name), original)
            assert_unchanged(helpers.get('deployment', 'market-postgres'), postgres)
            with (backup / 'operations.log').open('x') as log:
                # API readiness includes startup schema initialization before workers roll.
                for name in ROLLOUT_ORDER:
                    log.write('Starting rollout: ' + name + '\n')
                    log.flush()
                    cname, prefix = DEPLOYMENTS[name]
                    deployments[name] = replace_image(helpers, 'deployment', name,
                                                     deployments[name], cname, prefix + ':' + args.tag)
                    helpers.rollout(name)
                for name in CRONJOBS:
                    log.write('Updating CronJob image only: ' + name + '\n')
                    log.flush()
                    cname = helpers.podspec(crons[name])['containers'][0]['name']
                    crons[name] = replace_image(helpers, 'cronjob', name, crons[name],
                                               cname, 'market-api:' + args.tag)
            for kind, items in (('deployment', deployments), ('cronjob', crons)):
                for name, expected in items.items():
                    assert_unchanged(helpers.get(kind, name), expected)
            helpers.verify_http('http://127.0.0.1:30080/api/health')
            helpers.verify_http('http://127.0.0.1:30080/market-build.json', expected_marker)
            private_write(backup / 'SUCCESS', args.tag + '\n')
        except BaseException:
            private_write(backup / 'FAILED', 'Stopped; operator reconciliation required. '
                          'No automatic database restore or image rollback performed.\n')
            raise
    print('Order lifecycle deployment verified; private recovery set: ' + str(backup))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('Order lifecycle verification/deployment stopped; operator reconciliation required. '
              'Inspect the private recovery set if execution began.', file=sys.stderr)
        raise SystemExit(1)
