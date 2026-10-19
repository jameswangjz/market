#!/usr/bin/env python3
"""Verify/inspect by default; --execute deploys an independently trusted export."""
import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.request


DEPLOYMENTS = {
    'market-api': ('api', 'market-api', 'backend'),
    'market-web': ('web', 'market-web', 'frontend'),
    'market-mock-api': ('mock-api', 'market-mock-api', 'mock-api'),
    'online-delivery-worker': ('worker', 'market-api', None),
    'version-change-worker': ('worker', 'market-api', None),
    'refund-execution-worker': ('worker', 'market-api', None),
}


def command(argv, *, body=None, output=None, timeout=300):
    # Cluster/build output can contain env literals or provider errors. Keep it
    # private in execute mode; callers see only a fixed failure message.
    data = json.dumps(body).encode() if body is not None else None
    timed_out = False
    with subprocess.Popen(argv, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                          stdout=output or subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(data, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Bound only the process group launched by this invocation. Never
            # signal a previously running deployment or an unrelated kubectl.
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=5)
        returncode = process.returncode
    private_log = getattr(command, 'error_log', None)
    if private_log is not None and (stderr or timed_out):
        with private_log.open('ab') as stream:
            stream.write((Path(argv[0]).name + ' stderr:\n').encode())
            stream.write(stderr or b'')
            if timed_out:
                stream.write(('\nCommand exceeded ' + str(timeout) + ' seconds.').encode())
            stream.write(b'\n')
    if timed_out:
        raise RuntimeError(Path(argv[0]).name + ' exceeded ' + str(timeout) + ' seconds'
                           + ('; stderr saved privately' if private_log is not None else ''))
    if returncode:
        raise RuntimeError(Path(argv[0]).name + ' failed with exit code ' + str(returncode)
                           + ('; stderr saved privately' if private_log is not None else ''))
    return stdout


def kube(*args, body=None):
    return command(['kubectl', '-n', 'market', *args], body=body)


def get(kind, name, optional=False):
    args = ['get', kind, name, '-o', 'json']
    if optional:
        args.append('--ignore-not-found')
    raw = kube(*args)
    return json.loads(raw) if raw.strip() else None


def patch(kind, name, body, strategy='strategic'):
    kube('patch', kind, name, '--type=' + strategy, '--patch-file=/dev/stdin', body=body)


def podspec(obj):
    spec = obj['spec']
    if obj['kind'] == 'CronJob':
        spec = spec['jobTemplate']['spec']
    return spec['template']['spec']


def container(obj, name):
    matches = [c for c in podspec(obj)['containers'] if c['name'] == name]
    if len(matches) != 1 or len(podspec(obj)['containers']) != 1:
        raise ValueError('Unexpected runtime container layout')
    return matches[0]


def image_patch(name, image):
    return {'spec': {'template': {'spec': {'containers': [{'name': name, 'image': image}]}}}}


def rollout(name):
    kube('rollout', 'status', 'deployment/' + name, '--timeout=240s')


def validate_dump(dump, postgres_image, tag):
    """Read the host file directly: no kubectl stdin/EOF forwarding involved."""
    name = 'api-lifecycle-backup-check-' + tag + '-' + str(os.getpid())
    runtime = ['nerdctl', '--namespace', 'k8s.io']
    try:
        command([*runtime, 'run', '--name', name, '--pull=never',
                 '--network=none', '--read-only', '--cap-drop=ALL',
                 '--security-opt=no-new-privileges', '-v', str(dump.resolve()) + ':/backup.dump:ro',
                 '--entrypoint', 'pg_restore', postgres_image, '--list', '/backup.dump'], timeout=120)
    finally:
        # A timed-out nerdctl client may leave its disposable validation
        # container behind. This name cannot identify a business container.
        command([*runtime, 'rm', '--force', name], timeout=30)


def verify_http(url, expected=None):
    for attempt in range(8):
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                if response.status != 200:
                    raise ValueError('Unexpected HTTP status')
                if expected is not None and json.load(response) != expected:
                    raise ValueError('Served build marker mismatch')
            return
        except (OSError, ValueError):
            if attempt == 7:
                raise ValueError('Final HTTP verification failed after eight attempts') from None
            time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--release-root', type=Path, required=True)
    parser.add_argument('--backup-root', type=Path, default=Path('/root/market/backups'))
    parser.add_argument('--attempt', help='Optional unique recovery-directory suffix for a reviewed retry')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--enable-expiry', action='store_true')
    args = parser.parse_args()
    if args.attempt is not None and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.attempt):
        raise ValueError('Invalid attempt suffix')
    root = args.release_root.resolve()
    os.chdir(root)
    # Gate comes before cluster reads, backups, builds, locks, or mutations.
    command([sys.executable, 'scripts/trading_artifact_gate.py', 'verify', '--repo', str(root),
             '--commit', args.commit, '--tag', args.tag, '--manifest-sha256', args.manifest_sha256], timeout=120)
    import yaml

    backup_root = args.backup_root.resolve()
    if backup_root == root or root in backup_root.parents or backup_root in root.parents:
        raise ValueError('Backup root must be separate from the verified release tree')
    if args.enable_expiry and not args.execute:
        raise ValueError('--enable-expiry requires --execute')
    originals = {name: get('deployment', name) for name in DEPLOYMENTS}
    health = get('cronjob', 'application-health')
    old_expiry = get('cronjob', 'api-provider-expiry', optional=True)
    pvc = get('pvc', 'market-mock-api-data', optional=True)
    postgres = get('deployment', 'market-postgres')
    postgres_image = container(postgres, 'postgres')['image']
    for name, (cname, prefix, _) in DEPLOYMENTS.items():
        image = container(originals[name], cname)['image']
        if not image.startswith(prefix + ':'):
            raise ValueError('Unexpected base image; review runtime build compatibility')
        if originals[name]['status'].get('observedGeneration', 0) < originals[name]['metadata']['generation']:
            raise ValueError('Deployment reconciliation in progress')
        rollout(name)
    api = container(originals['market-api'], 'api')
    host_env = [copy.deepcopy(e) for e in api.get('env', []) if e['name'] == 'UPSTREAM_ALLOWED_HOSTS']
    if len(host_env) != 1:
        raise ValueError('Expected explicit deployed API host policy; review inherited policy first')
    mock = container(originals['market-mock-api'], 'mock-api')
    volumes = podspec(originals['market-mock-api']).get('volumes', [])
    mounts = mock.get('volumeMounts', [])
    if mounts or volumes:
        if (not any(v.get('persistentVolumeClaim', {}).get('claimName') == 'market-mock-api-data'
                    and v['name'] == 'provider-state' for v in volumes)
                or not any(m['name'] == 'provider-state' and m['mountPath'] == '/data' for m in mounts)):
            raise ValueError('Existing mock storage requires a reviewed migration')
    else:
        # Never hide an existing ephemeral DB beneath a new PVC or lose it in a rollout.
        kube('exec', 'deployment/market-mock-api', '--', 'python', '-c',
             'import os,pathlib; p=pathlib.Path(os.getenv("MOCK_API_DB_PATH","/data/mock-api.sqlite3")); '
             'assert not p.exists(), "Existing mock state requires reviewed migration"')
    if originals['market-mock-api']['spec'].get('replicas', 1) != 1:
        raise ValueError('Mock SQLite requires exactly one replica')
    docs = list(yaml.safe_load_all((root / 'k8s/mock-api-lifecycle-storage.yaml').read_text()))
    if (len(docs) != 2 or docs[0]['kind'] != 'PersistentVolumeClaim'
            or docs[0]['metadata']['name'] != 'market-mock-api-data'
            or docs[1]['kind'] != 'Deployment' or docs[1]['metadata']['name'] != 'market-mock-api'):
        raise ValueError('Unexpected storage manifest contract')
    if pvc and pvc['spec'].get('storageClassName') != docs[0]['spec']['storageClassName']:
        raise ValueError('Existing PVC storage class differs')
    expiry = yaml.safe_load((root / 'k8s/api-provider-expiry-cronjob.yaml').read_text())
    expiry['spec']['suspend'] = True
    ec = container(expiry, 'api-provider-expiry')
    ec['image'] = 'market-api:' + args.tag
    ec['env'] = [e for e in ec.get('env', []) if e['name'] != 'UPSTREAM_ALLOWED_HOSTS'] + host_env
    required = {'API_CREDENTIAL_ENCRYPTION_KEY', 'JWT_SECRET', 'APISIX_ADMIN_KEY',
                'OIDC_SIGNING_KEY_PEM', 'OIDC_CREDENTIAL_ENCRYPTION_KEY'}
    if not required <= {e['name'] for e in ec['env'] if 'secretKeyRef' in e.get('valueFrom', {})}:
        raise ValueError('Expiry secret references missing')
    if expiry['spec']['schedule'] != '*/5 * * * *' or expiry['spec']['concurrencyPolicy'] != 'Forbid':
        raise ValueError('Unexpected expiry schedule')
    # Server validation is dry-run only, including the partial Deployment PATCH.
    storage_patch = copy.deepcopy(docs[1]['spec'])
    storage_patch['strategy']['rollingUpdate'] = None
    storage_patch['template']['spec']['containers'][0]['image'] = 'market-mock-api:' + args.tag
    kube('apply', '--dry-run=server', '-f', '-', body=docs[0])
    kube('patch', 'deployment', 'market-mock-api', '--type=strategic', '--dry-run=server',
         '--patch-file=/dev/stdin', body={'spec': storage_patch})
    kube('apply', '--dry-run=server', '-f', '-', body=expiry)
    if not args.execute:
        print('Artifact and read-only preflight passed. No backups, builds or deployment performed.')
        return 0

    os.umask(0o077)
    backup_root.mkdir(parents=True, exist_ok=True)
    with (backup_root / '.api-lifecycle.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        backup = backup_root / ('api-lifecycle-' + args.tag + ('-' + args.attempt if args.attempt else ''))
        backup.mkdir(mode=0o700)  # Never overwrite an earlier recovery set.
        command.error_log = backup / 'commands.stderr.log'
        snapshot = dict(deployments=originals, application_health=health, expiry=old_expiry, pvc=pvc)
        (backup / 'before.json').write_text(json.dumps(snapshot))
        (backup / 'expiry.rendered.json').write_text(json.dumps(expiry))
        # Custom-format dump has no credentials or data in console output.
        with (backup / 'postgres.dump.partial').open('xb') as stream:
            command(['kubectl', '-n', 'market', 'exec', 'deployment/market-postgres', '--',
                     'pg_dump', '-U', 'market', '-d', 'market', '-Fc'], output=stream)
        dump = backup / 'postgres.dump.partial'
        with dump.open('rb') as stream:
            signature = stream.read(5)
        if dump.stat().st_size < 32 or signature != b'PGDMP':
            raise ValueError('Postgres backup invalid')
        dump.rename(backup / 'postgres.dump')
        validate_dump(backup / 'postgres.dump', postgres_image, args.tag)
        # Existing mounted mock state must be backed up consistently before rollout.
        if mounts:
            code = ('import os,sqlite3,sys; p=os.getenv("MOCK_API_DB_PATH","/data/mock-api.sqlite3"); '
                    's=sqlite3.connect("file:"+p+"?mode=ro",uri=True); '
                    'd=sqlite3.connect(":memory:"); s.backup(d); '
                    'sys.stdout.buffer.write(d.serialize()); d.close(); s.close()')
            with (backup / 'mock-api.sqlite3').open('xb') as stream:
                command(['kubectl', '-n', 'market', 'exec', 'deployment/market-mock-api', '--',
                         'python', '-c', code], output=stream)
        with (backup / 'build.log').open('xb') as log:
            for name, (cname, prefix, context) in DEPLOYMENTS.items():
                if context:
                    command(['nerdctl', '--namespace', 'k8s.io', 'build', '--build-arg',
                        'BASE_IMAGE=' + container(originals[name], cname)['image'], '-t',
                        prefix + ':' + args.tag, '-f', context + '/Dockerfile.runtime', context],
                        output=log, timeout=900)
        # Refuse changes since inspection/backup; serialize operators externally too.
        for name, original in originals.items():
            if get('deployment', name)['spec'] != original['spec']:
                raise ValueError('Deployment drift detected; stop and prepare a new release')
        for name, original in (('application-health', health), ('api-provider-expiry', old_expiry)):
            current = get('cronjob', name, optional=True)
            if (current or {}).get('spec') != (original or {}).get('spec'):
                raise ValueError('CronJob drift detected')
        try:
            # No manually created Job; suspend existing scheduling before any rollout.
            if old_expiry:
                patch('cronjob', 'api-provider-expiry', {'spec': {'suspend': True}}, 'merge')
                if old_expiry.get('status', {}).get('active'):
                    raise ValueError('Active expiry Job must finish before deployment')
            kube('apply', '-f', '-', body=docs[0])
            patch('deployment', 'market-mock-api', {'spec': storage_patch})
            rollout('market-mock-api')
            if get('pvc', 'market-mock-api-data').get('status', {}).get('phase') != 'Bound':
                raise ValueError('Mock PVC is not Bound')
            # API initializes additive lifecycle tables before workers use them.
            for name in ('market-api', 'online-delivery-worker', 'version-change-worker',
                         'refund-execution-worker', 'market-web'):
                cname, prefix, _ = DEPLOYMENTS[name]
                patch('deployment', name, image_patch(cname, prefix + ':' + args.tag))
                rollout(name)
            hc = podspec(health)['containers']
            if len(hc) != 1:
                raise ValueError('Unexpected health CronJob container layout')
            patch('cronjob', 'application-health', {'spec': {'jobTemplate': image_patch(
                hc[0]['name'], 'market-api:' + args.tag)}})
            kube('apply', '-f', '-', body=expiry)
            for name, (cname, prefix, _) in DEPLOYMENTS.items():
                current = get('deployment', name)
                if container(current, cname)['image'] != prefix + ':' + args.tag:
                    raise ValueError('Installed image mismatch')
            installed = get('cronjob', 'api-provider-expiry')
            if container(installed, 'api-provider-expiry') != ec:
                # Kubernetes defaults may add fields: compare intended keys only.
                actual = container(installed, 'api-provider-expiry')
                if any(actual.get(k) != v for k, v in ec.items()):
                    raise ValueError('Expiry runtime configuration mismatch')
            verify_http('http://127.0.0.1:30080/api/health')
            expected = json.loads((root / 'frontend/dist/market-build.json').read_text())
            verify_http('http://127.0.0.1:30080/market-build.json', expected)
            if args.enable_expiry:
                patch('cronjob', 'api-provider-expiry', {'spec': {'suspend': False}}, 'merge')
            (backup / 'SUCCESS').write_text(args.tag + '\n')
        except Exception:
            # Retain provider PVC/receipts and additive DB tables. Automatic data
            # restore or image rollback could invalidate durable provider work.
            if get('cronjob', 'api-provider-expiry', optional=True):
                patch('cronjob', 'api-provider-expiry', {'spec': {'suspend': True}}, 'merge')
            (backup / 'FAILED').write_text('Stopped; operator reconciliation required.\n')
            raise
    print('Deployment verified. Expiry ' + ('enabled.' if args.enable_expiry else 'remains suspended.'))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        print('API lifecycle preparation/deployment stopped; review the private recovery directory.', file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        print('API lifecycle preparation/deployment stopped; review the private recovery directory.', file=sys.stderr)
        raise SystemExit(1)
