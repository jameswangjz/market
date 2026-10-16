"""Explicit, bounded Redis RDB-to-AOF migration. Never invokes SSH."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = """
local result = {}
local cursor = '0'
repeat
    local batch = redis.call('SCAN', cursor, 'COUNT', 1000)
    cursor = batch[1]
    for _, key in ipairs(batch[2]) do
        local dump = redis.call('DUMP', key)
        if dump then
            result[redis.sha1hex(key)] = {digest=redis.sha1hex(dump),
                expires=redis.call('PEXPIRETIME', key), kind=redis.call('TYPE', key).ok}
        end
    end
until cursor == '0'
local now = redis.call('TIME')
return cjson.encode({keys=result, now_ms=tonumber(now[1])*1000+math.floor(tonumber(now[2])/1000)})
"""


def verify_snapshot(before, after):
    for key, value in before['keys'].items():
        if value['expires'] >= 0 and value['expires'] <= after['now_ms']:
            continue
        if after['keys'].get(key) != value:
            raise RuntimeError('restored dataset mismatch (hashed key ' + key + ')')
    if set(after['keys']) - set(before['keys']):
        raise RuntimeError('unexpected keys in restored dataset')


class Migration:
    def __init__(self, backup_dir, output):
        self.backup_dir, self.output = Path(backup_dir), Path(output)
        self.owner = uuid.uuid4().hex
        self.helper = 'market-redis-seed-' + self.owner
        self.report = {'phase': 'preflight', 'owner': self.owner, 'ok': False}
        self.deadline = time.monotonic() + 90
        self.old = None
        self.paused = False
        self.helper_created = False
        self.original_selector = None
        self.service_isolated = False
        self.report['helper_cleanup_verified'] = False

    def service_selector(self, isolate, bounded=True):
        isolated = {**self.original_selector, 'redis-maintenance': self.owner}
        expected, replacement = (self.original_selector, isolated) if isolate else (isolated, self.original_selector)
        if isolate:
            self.service_isolated = True  # A timed-out patch may still have reached the API.
        self.command('patch', 'service', 'market-redis', '--type=json', '-p', json.dumps([
            {'op': 'test', 'path': '/spec/selector', 'value': expected},
            {'op': 'replace', 'path': '/spec/selector', 'value': replacement}]), bounded=bounded)
        self.service_isolated = isolate
        self.report['service_isolated'] = isolate

    def wait_service_isolated(self):
        while True:
            slices = self.obj('get', 'endpointslices', '-l', 'kubernetes.io/service-name=market-redis')
            if not any(item.get('endpoints') for item in slices['items']):
                return
            if time.monotonic() >= self.deadline:
                raise RuntimeError('Service endpoint isolation deadline exceeded')
            time.sleep(0.25)

    def command(self, *args, data=None, destination=None, bounded=True):
        timeout = 30
        if bounded and self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
            if timeout <= 0:
                raise RuntimeError('maintenance deadline exceeded')
        result = subprocess.run(['kubectl', '-n', 'market', '--request-timeout=15s', *args],
                                input=json.dumps(data).encode() if data is not None else None,
                                stdout=destination if destination else subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
        if result.returncode:
            # kubectl errors may contain object content or connection credentials.
            raise RuntimeError('kubectl ' + args[0] + ' failed')
        return result.stdout.decode() if destination is None else ''

    def obj(self, *args):
        return json.loads(self.command(*args, '-o', 'json'))

    def redis(self, pod, port, *args, json_result=False, bounded=True):
        output = self.command('exec', pod, '-c', 'redis', '--', 'redis-cli',
                              '-p', str(port), '--json' if json_result else '--raw',
                              *args, bounded=bounded)
        return json.loads(output) if json_result else output.strip()

    def snapshot(self, pod, port):
        # EVAL may itself queue behind CLIENT PAUSE WRITE; EVAL_RO stays readable.
        return json.loads(self.redis(pod, port, 'EVAL_RO', SNAPSHOT, '0', json_result=True))

    def wait_ready(self, deployment=False):
        for _ in range(100):
            if deployment:
                dep = self.obj('get', 'deployment', 'market-redis')
                status = dep.get('status', {})
                pods = self.obj('get', 'pods', '-l', 'app=market-redis')['items']
                ready = (status.get('observedGeneration', 0) >= dep['metadata']['generation']
                         and status.get('updatedReplicas') == 1 and status.get('readyReplicas') == 1)
                candidates = [p for p in pods if not p['metadata'].get('deletionTimestamp')]
            else:
                candidates = [self.obj('get', 'pod', self.helper)]
                ready = True
            if ready and len(candidates) == 1:
                pod = candidates[0]
                if any(c['type'] == 'Ready' and c['status'] == 'True'
                       for c in pod.get('status', {}).get('conditions', [])):
                    return pod['metadata']['name']
            if self.deadline and time.monotonic() >= self.deadline:
                break
            time.sleep(0.5)
        raise RuntimeError('pod readiness deadline exceeded')

    def patch(self, port):
        probe = {'exec': {'command': ['redis-cli', '-p', str(port), 'ping']}}
        container = {'name': 'redis', 'args': ['redis-server', '/etc/redis/redis.conf'],
                     'volumeMounts': [{'name': 'data', 'mountPath': '/data'},
                                      {'name': 'config', 'mountPath': '/etc/redis', 'readOnly': True}],
                     'readinessProbe': {**probe, 'periodSeconds': 2},
                     'livenessProbe': {**probe, 'initialDelaySeconds': 30, 'periodSeconds': 10},
                     'startupProbe': {**probe, 'periodSeconds': 2, 'failureThreshold': 60},
                     'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'},
                                   'limits': {'cpu': '1', 'memory': '512Mi'}}}
        if port == 6380:
            container['args'] += ['--appendonly', 'no', '--port', '6380']
        patch = {'spec': {'replicas': 1, 'strategy': {'type': 'Recreate', 'rollingUpdate': None},
                          'template': {'spec': {'containers': [container], 'volumes': [
                              {'name': 'data', 'persistentVolumeClaim': {'claimName': 'market-redis-data'}},
                              {'name': 'config', 'configMap': {'name': 'market-redis-config'}}]}}}}
        self.command('patch', 'deployment', 'market-redis', '--type=strategic',
                     '-p', json.dumps(patch))

    def aof_ready(self, pod, port):
        info = dict(line.split(':', 1) for line in self.redis(pod, port, 'INFO', 'persistence').splitlines()
                    if ':' in line and not line.startswith('#'))
        return all(info.get(key) == value for key, value in {
            'aof_enabled': '1', 'aof_rewrite_in_progress': '0', 'aof_rewrite_scheduled': '0',
            'aof_last_bgrewrite_status': 'ok', 'aof_last_write_status': 'ok'}.items())

    def cleanup(self):
        if self.paused and self.old:
            try:
                pod = json.loads(self.command('get', 'pod', self.old['name'], '-o', 'json', bounded=False))
                if pod['metadata']['uid'] == self.old['uid'] and not pod['metadata'].get('deletionTimestamp'):
                    if self.redis(self.old['name'], 6379, 'CLIENT', 'UNPAUSE', bounded=False) != 'OK':
                        raise RuntimeError('unpause not confirmed')
                    if self.service_isolated:
                        self.service_selector(False, bounded=False)
            except Exception:
                self.report['old_unpause'] = 'not confirmed; pause expires automatically'
        if self.helper_created:
            try:
                pod = json.loads(self.command('get', 'pod', self.helper, '-o', 'json', bounded=False))
                if (pod['metadata']['name'] == self.helper and
                        pod['metadata'].get('labels', {}).get('market-redis-migration') == self.owner):
                    self.command('delete', 'pod', self.helper, '--wait=true', '--timeout=15s', bounded=False)
                    remaining = self.command('get', 'pod', self.helper, '--ignore-not-found', '-o', 'json', bounded=False)
                    self.report['helper_cleanup_verified'] = not remaining.strip()
            except Exception:
                self.report['helper_cleanup'] = 'not confirmed'
        self.report['service_isolated'] = self.service_isolated
        if self.service_isolated:
            self.report['manual_recovery'] = 'Service remains isolated; verify dataset and AOF before restoring original selector'

    def run(self):
        # Reserve the report before any Kubernetes action. Existing paths are never overwritten.
        fd = os.open(self.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            dep = self.obj('get', 'deployment', 'market-redis')
            spec = dep['spec']['template']['spec']
            if any(v.get('persistentVolumeClaim') for v in spec.get('volumes', [])):
                raise RuntimeError('PVC already attached; already migrated or partial migration; manual review required')
            containers = spec['containers']
            container = next(c for c in containers if c['name'] == 'redis')
            if container['image'] != 'redis:7-alpine' or dep['spec'].get('replicas', 1) != 1:
                raise RuntimeError('expected single redis:7-alpine deployment')
            pods = self.obj('get', 'pods', '-l', 'app=market-redis')['items']
            if len(pods) != 1 or pods[0]['metadata'].get('deletionTimestamp'):
                raise RuntimeError('expected exactly one live Redis pod')
            old = pods[0]['metadata']
            self.old = {'name': old['name'], 'uid': old['uid']}
            service = self.obj('get', 'service', 'market-redis')
            self.original_selector = service['spec'].get('selector')
            if not self.original_selector or 'redis-maintenance' in self.original_selector:
                raise RuntimeError('expected active Redis Service selector without maintenance marker')
            self.report['original_service_selector'] = self.original_selector
            self.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            backup = self.backup_dir / ('redis-' + self.owner)
            backup.mkdir(mode=0o700)
            self.report['backup_dir'] = str(backup)
            self.command('apply', '-f', str(ROOT / 'k8s/redis-persistence.yaml'))
            helper = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': self.helper,
                      'namespace': 'market', 'labels': {'market-redis-migration': self.owner}},
                      'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                               'terminationGracePeriodSeconds': 1,
                               # Keep scheduler participation for local-path WaitForFirstConsumer.
                               'affinity': {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {
                                   'nodeSelectorTerms': [{'matchFields': [{'key': 'metadata.name',
                                       'operator': 'In', 'values': [pods[0]['spec']['nodeName']]}]}]}}},
                               'securityContext': {'runAsUser': 0},
                               'containers': [{'name': 'seed', 'image': container['image'],
                                   'imagePullPolicy': 'Never', 'command': ['sleep', '600'],
                                   'resources': {'requests': {'cpu': '10m', 'memory': '16Mi'},
                                                 'limits': {'cpu': '100m', 'memory': '64Mi'}},
                                   'volumeMounts': [{'name': 'data', 'mountPath': '/data'}]}],
                               'volumes': [{'name': 'data', 'persistentVolumeClaim': {
                                   'claimName': 'market-redis-data'}}]}}
            self.helper_created = True
            self.command('create', '-f', '-', data=helper)
            self.wait_ready()
            if self.command('exec', self.helper, '--', 'find', '/data', '-mindepth', '1').strip():
                raise RuntimeError('PVC is not empty; refusing seed overwrite')
            self.report['phase'] = 'paused_snapshot'
            self.deadline = time.monotonic() + 110
            self.paused = True
            if self.redis(old['name'], 6379, 'CLIENT', 'PAUSE', '120000', 'WRITE') != 'OK':
                raise RuntimeError('write pause not confirmed')
            before = self.snapshot(old['name'], 6379)
            with open(backup / 'snapshot.json', 'x', opener=lambda p, f: os.open(p, f, 0o600)) as file:
                json.dump(before, file)
            if self.redis(old['name'], 6379, 'SAVE') != 'OK':
                raise RuntimeError('SAVE failed')
            dump = backup / 'dump.rdb'
            with open(dump, 'xb', opener=lambda p, f: os.open(p, f, 0o600)) as file:
                self.command('exec', old['name'], '-c', 'redis', '--', 'cat', '/data/dump.rdb', destination=file)
            digest = hashlib.sha256(dump.read_bytes()).hexdigest()
            self.report['dump_sha256'] = digest
            self.command('cp', str(dump), self.helper + ':/data/dump.rdb', '-c', 'seed')
            actual = self.command('exec', self.helper, '--', 'sha256sum', '/data/dump.rdb').split()[0]
            if actual != digest:
                raise RuntimeError('seed checksum mismatch')
            self.command('exec', self.helper, '--', 'chown', '999:1000', '/data/dump.rdb')
            self.report['phase'] = 'isolated_rdb_restore'
            self.service_selector(True)
            self.wait_service_isolated()
            self.patch(6380)
            pod = self.wait_ready(deployment=True)
            verify_snapshot(before, self.snapshot(pod, 6380))
            self.report['phase'] = 'aof_bootstrap'
            if self.redis(pod, 6380, 'CONFIG', 'SET', 'appendonly', 'yes') != 'OK':
                raise RuntimeError('AOF enable failed')
            while not self.aof_ready(pod, 6380):
                if time.monotonic() >= self.deadline:
                    raise RuntimeError('AOF rewrite deadline exceeded')
                time.sleep(0.25)
            self.command('exec', pod, '-c', 'redis', '--', 'test', '-s',
                         '/data/appendonlydir/appendonly.aof.manifest')
            self.report['phase'] = 'aof_restart'
            self.patch(6379)
            pod = self.wait_ready(deployment=True)
            verify_snapshot(before, self.snapshot(pod, 6379))
            if not self.aof_ready(pod, 6379):
                raise RuntimeError('AOF restart not healthy')
            self.service_selector(False)
            self.paused = False
            self.report.update(ok=True, phase='complete', key_count=len(before['keys']))
        except Exception as error:
            self.report['error'] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        finally:
            self.cleanup()
            with os.fdopen(fd, 'w') as file:
                json.dump(self.report, file, indent=2)
                file.write('\n')
        return self.report['ok']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--backup-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error('--execute is required; no Kubernetes actions performed')
    return 0 if Migration(args.backup_dir, args.output).run() else 1


if __name__ == '__main__':
    raise SystemExit(main())
