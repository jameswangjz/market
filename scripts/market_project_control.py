#!/usr/bin/env python3
"""Stop/start only market, preserving replica counts, financial seals and PVCs."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

NAMESPACE = 'market'
STORAGE = {'market-postgres', 'market-redis', 'market-etcd', 'market-minio'}
CORE = STORAGE | {'market-api', 'redis-maintenance-worker', 'api-charge-ledger-worker'}
EDGES = {'market-web', 'market-apisix', 'market-gateway'}
KINDS = ('Deployment', 'StatefulSet')
POINTER = Path('/root/market/ops/project-state/latest.json')

MAINTENANCE_CLIENT = r'''
import httpx,json,sys,time,uuid
from sqlalchemy import select
from app import main as m
action,actor,reason=sys.argv[1:4]
with m.SessionLocal() as db:
    user=db.scalar(select(m.User).where(m.User.email==actor))
    assert user and user.is_active and user.activation_status=='active' and user.platform_role=='super_admin'
    token=m.issue_token(user)
with httpx.Client(base_url='http://127.0.0.1:8000/api',timeout=20,trust_env=False,
                  headers={'Authorization':'Bearer '+token}) as client:
    deadline=time.monotonic()+600
    while True:
        status=client.get('/admin/maintenance/redis'); status.raise_for_status()
        if not status.json()['active_task']: break
        if time.monotonic()>deadline: raise RuntimeError('Existing maintenance task did not finish')
        time.sleep(2)
    response=client.post('/admin/maintenance/redis/tasks',json={'action':action,'reason':reason,
        'accept_gap':action=='recover','idempotency_key':str(uuid.uuid4())})
    response.raise_for_status(); task=response.json()
    while True:
        response=client.get('/admin/maintenance/redis'); response.raise_for_status(); data=response.json()
        task=next((row for row in data['tasks'] if row['id']==task['id']),task)
        if task['status']=='succeeded':
            print(json.dumps({'task':task,'snapshot':data['snapshot']},ensure_ascii=False)); break
        if task['status'] in ('failed','manual_review'):
            raise RuntimeError('Maintenance task stopped; inspect its recorded steps')
        if time.monotonic()>deadline: raise RuntimeError('Maintenance task timed out; do not replay blindly')
        time.sleep(2)
'''


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


class Controller:
    def __init__(self, args, state=None):
        self.args, self.state = args, state

    def command(self, *args, **kwargs):
        result = subprocess.run(['kubectl', '-n', NAMESPACE, *args], capture_output=True,
                                timeout=kwargs.pop('timeout', 360), **kwargs)
        if result.returncode:
            if self.state:
                path = Path(self.state['directory']) / ('error-' + uuid.uuid4().hex + '.log')
                path.write_bytes(result.stderr)
            raise RuntimeError('Kubernetes operation failed; inspect the private state directory')
        return result.stdout

    def get(self, resources):
        return json.loads(self.command('get', resources, '-o', 'json'))

    def save(self, phase=None):
        if phase:
            self.state['phase'] = phase
        self.state['updated_at'] = datetime.now(timezone.utc).isoformat()
        write_json(Path(self.state['directory']) / 'state.json', self.state)
        write_json(POINTER, {'state': str(Path(self.state['directory']) / 'state.json')})
        if phase:
            print(json.dumps({'phase': phase, 'state': str(Path(self.state['directory']) / 'state.json')}), flush=True)

    def snapshot(self):
        nodes = json.loads(self.command('get', 'nodes', '-o', 'json'))
        if not any(a.get('address') == '192.168.10.10' for node in nodes['items']
                   for a in node.get('status', {}).get('addresses', [])):
            raise RuntimeError('This tool is restricted to the 192.168.10.10 cluster')
        resources = self.get('deployment,statefulset,daemonset,cronjob,job,pod,hpa,pvc')
        if any(item['kind'] in ('DaemonSet', 'HorizontalPodAutoscaler') for item in resources['items']):
            raise RuntimeError('DaemonSets/HPA require separate suspension planning; no change made')
        namespace = self.get('namespace/market')
        directory = Path('/root/market/backups') / ('market-stop-' + datetime.now().strftime('%Y%m%d%H%M%S') + '-' + uuid.uuid4().hex[:6])
        directory.mkdir(mode=0o700)
        write_json(directory / 'resources-before.json', resources)
        self.state = {'namespace_uid': namespace['metadata']['uid'], 'directory': str(directory),
                      'phase': 'snapshot', 'workloads': [item for item in resources['items'] if item['kind'] in KINDS],
                      'cronjobs': [item for item in resources['items'] if item['kind'] == 'CronJob'],
                      'jobs_suspended': [], 'maintenance': [],
                      'running_pods_before': sum(item['kind'] == 'Pod' and item.get('status', {}).get('phase') == 'Running'
                                                 for item in resources['items']),
                      'temporary_pods_before': sum(item['kind'] == 'Pod' and item.get('status', {}).get('phase') in ('Succeeded', 'Failed')
                                                   for item in resources['items'])}
        self.save()

    def validate(self):
        if self.get('namespace/market')['metadata']['uid'] != self.state['namespace_uid']:
            raise RuntimeError('Namespace was replaced; manual review required')
        for item in self.state['workloads'] + self.state['cronjobs']:
            current = self.get(item['kind'].lower() + '/' + item['metadata']['name'])
            if current['metadata']['uid'] != item['metadata']['uid']:
                raise RuntimeError('Workload was recreated; do not restore stale state automatically')

    def scale(self, names, restore=False, wait=False):
        selected = [item for item in self.state['workloads'] if item['metadata']['name'] in names]
        for item in selected:
            replicas = item['spec'].get('replicas', 1) if restore else 0
            target = item['kind'].lower() + '/' + item['metadata']['name']
            self.command('scale', target, '--replicas=' + str(replicas))
            if restore and wait and replicas:
                self.command('rollout', 'status', target, '--timeout=240s')

    def wait_stopped(self, names):
        for item in self.state['workloads']:
            if item['metadata']['name'] not in names:
                continue
            selector = item['spec']['selector']
            if selector.get('matchExpressions') or not selector.get('matchLabels'):
                raise RuntimeError('Complex selectors require explicit stop verification')
            labels = ','.join(k + '=' + v for k, v in selector['matchLabels'].items())
            deadline = time.monotonic() + 330
            while json.loads(self.command('get', 'pods', '-l', labels, '-o', 'json'))['items']:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Pod termination timed out for ' + item['metadata']['name'])
                time.sleep(2)

    def maintenance(self, action):
        report = json.loads(self.command('exec', '-i', 'deployment/market-api', '--', 'env', 'PYTHONPATH=/app',
                      'python', '-B', '-', action, self.args.actor, self.args.reason,
                      input=MAINTENANCE_CLIENT.encode(), timeout=1260))
        self.state['maintenance'].append(report)
        self.save()
        return report['snapshot']

    def suspend_jobs(self):
        for item in self.get('jobs')['items']:
            terminal = any(c.get('type') in ('Complete', 'Failed') and c.get('status') == 'True'
                           for c in item.get('status', {}).get('conditions', []))
            if not terminal and not item['spec'].get('suspend', False):
                self.state['jobs_suspended'].append({'name': item['metadata']['name'], 'uid': item['metadata']['uid']})
                self.save()
                self.command('patch', 'job', item['metadata']['name'], '--type=merge', '-p', '{"spec":{"suspend":true}}')

    def backup(self):
        path = Path(self.state['directory']) / 'database-before.dump'
        if path.exists():
            with path.open('rb') as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            if digest == self.state.get('database_backup_sha256'):
                return
        else:
            with path.open('xb') as output:
                result = subprocess.run(['kubectl', '-n', NAMESPACE, 'exec', 'deployment/market-postgres', '--',
                                         'pg_dump', '-U', 'market', '-d', 'market', '-Fc'], stdout=output,
                                        stderr=subprocess.PIPE, timeout=300)
            if result.returncode or not path.stat().st_size:
                raise RuntimeError('Host database backup failed; core storage has not been stopped')
        pods = json.loads(self.command('get', 'pods', '-l', 'app=market-postgres', '-o', 'json'))['items']
        pod = next(item for item in pods if item.get('status', {}).get('phase') == 'Running'
                   and not item['metadata'].get('deletionTimestamp'))['metadata']['name']
        remote = '/tmp/market-backup-check-' + uuid.uuid4().hex + '.dump'
        try:
            self.command('cp', str(path), pod + ':' + remote, '-c', 'postgres', timeout=180)
            # Parse every archive section without connecting to a database or executing SQL.
            self.command('exec', pod, '-c', 'postgres', '--', 'pg_restore',
                         '--file=/dev/null', '--no-owner', '--no-privileges', remote, timeout=180)
        finally:
            self.command('exec', pod, '-c', 'postgres', '--', 'rm', '-f', '--', remote)
        with path.open('rb') as source:
            self.state['database_backup_sha256'] = hashlib.file_digest(source, 'sha256').hexdigest()
        self.state['database_backup_validation'] = 'full archive parse; no database restore executed'
        self.save()

    def stop(self):
        if self.state and self.state['phase'] == 'stopped':
            self.validate()
            current = self.get('deployment,statefulset,cronjob,pod')['items']
            if any(item['kind'] == 'Pod' or
                   (item['kind'] in KINDS and item['spec'].get('replicas', 1) != 0) or
                   (item['kind'] == 'CronJob' and not item['spec'].get('suspend', False)) for item in current):
                raise RuntimeError('Workloads were changed outside this tool; review the saved stop state')
            return
        if self.state is None:
            self.snapshot()
        self.validate()
        names = {item['metadata']['name'] for item in self.state['workloads']}
        if not CORE.issubset(names):
            raise RuntimeError('Required storage/maintenance services missing; no safe seal possible')
        for item in self.state['cronjobs']:
            self.command('patch', 'cronjob', item['metadata']['name'], '--type=merge', '-p', '{"spec":{"suspend":true}}')
        self.suspend_jobs()
        self.save('closing_ingress_and_workers')
        early = names - CORE
        self.scale(early)
        self.wait_stopped(early & {'online-delivery-worker', 'version-change-worker', 'refund-execution-worker'})
        self.save('preparing_financial_seal')
        if not self.state.get('sealed_source_verified'):
            snapshot = self.maintenance('inspect')
            if snapshot['status'] in ('active_source_healthy', 'safe_sealed_resume_candidate'):
                self.backup()
            if snapshot['status'] == 'active_source_healthy':
                snapshot = self.maintenance('prepare')
            if snapshot['status'] != 'safe_sealed_resume_candidate':
                raise RuntimeError('Financial seal is not verified; Redis/PostgreSQL remain running for review')
            self.state['sealed_source_verified'] = True
        self.save('sealed_ready_to_stop')
        self.scale({'redis-maintenance-worker', 'api-charge-ledger-worker', 'market-api'})
        self.wait_stopped({'redis-maintenance-worker', 'api-charge-ledger-worker', 'market-api'})
        self.scale(names - {'market-postgres'})
        self.wait_stopped(names - {'market-postgres'})
        self.scale({'market-postgres'})
        self.wait_stopped({'market-postgres'})
        self.suspend_jobs()
        self.command('delete', 'pods', '--all', '--wait=true', '--timeout=330s')
        for _ in range(3):
            if self.get('pods')['items']:
                raise RuntimeError('Pods were recreated; inspect external controllers before proceeding')
            time.sleep(2)
        self.save('stopped')

    def start(self):
        if not self.state:
            raise RuntimeError('No saved stop state; refusing to guess original replicas')
        self.validate()
        # Once startup begins the old stop seal cannot authorize a later shutdown.
        self.state['sealed_source_verified'] = False
        self.save('starting_storage')
        self.scale({'market-postgres', 'market-etcd', 'market-minio'}, restore=True, wait=True)
        self.scale({'market-redis'}, restore=True, wait=True)
        self.scale({'market-api'}, restore=True, wait=True)
        self.scale({'redis-maintenance-worker'}, restore=True, wait=True)
        # A paused collector is expected to be unready until source verification succeeds.
        self.scale({'api-charge-ledger-worker'}, restore=True)
        self.save('verifying_financial_source')
        snapshot = self.maintenance('inspect')
        if snapshot['status'] == 'safe_sealed_resume_candidate':
            snapshot = self.maintenance('resume')
        elif snapshot['status'] == 'explicit_gap_required' and self.args.accept_gap:
            snapshot = self.maintenance('recover')
        if snapshot['status'] != 'active_source_healthy':
            raise RuntimeError('Continuity unproven; ingress remains stopped. Review before using --accept-gap')
        self.scale({'api-charge-ledger-worker'}, restore=True, wait=True)
        self.save('restoring_services')
        names = {item['metadata']['name'] for item in self.state['workloads']}
        self.scale(names - CORE - EDGES, restore=True, wait=True)
        self.scale(EDGES, restore=True, wait=True)
        if not self.args.keep_cronjobs_suspended:
            for item in self.state['cronjobs']:
                patch = json.dumps({'spec': {'suspend': item['spec'].get('suspend', False)}})
                self.command('patch', 'cronjob', item['metadata']['name'], '--type=merge', '-p', patch)
        self.save('running_cron_paused' if self.args.keep_cronjobs_suspended else 'running')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('stop', 'start'))
    parser.add_argument('--state', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--actor', default='admin@market.local')
    parser.add_argument('--reason', default='')
    parser.add_argument('--accept-gap', action='store_true')
    parser.add_argument('--keep-cronjobs-suspended', action='store_true')
    args = parser.parse_args(argv)
    if args.accept_gap and (args.action != 'start' or not args.reason.strip()):
        parser.error('--accept-gap is only valid for a reviewed start with a reason')
    if not args.reason.strip():
        args.reason = '管理员停用/启动market项目，保留数据和计费证据'
    os.umask(0o077)
    with open('/var/lock/market-project-control.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = args.state
        if path is None and POINTER.exists():
            path = Path(json.loads(POINTER.read_text())['state'])
        state = json.loads(path.read_text()) if path else None
        # A completed prior run must never replace its original replica snapshot with zeroes.
        if args.action == 'stop' and state and state['phase'] in ('running', 'running_cron_paused'):
            state = None
        controller = Controller(args, state)
        if args.dry_run:
            if state:
                controller.validate()
            print(json.dumps({'action': args.action, 'dry_run': True, 'state': str(path) if path else None,
                              'restore_original_replicas': True, 'namespace': NAMESPACE}, ensure_ascii=False))
            return 0
        try:
            getattr(controller, args.action)()
            print(json.dumps({'status': controller.state['phase'], 'state': str(Path(controller.state['directory']) / 'state.json'),
                              'deployments_recorded': len(controller.state['workloads']),
                              'cronjobs_recorded': len(controller.state['cronjobs']),
                              'running_pods_before': controller.state['running_pods_before'],
                              'temporary_pods_before': controller.state['temporary_pods_before'],
                              'pvc_deleted': False}, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            if controller.state:
                controller.state['failure_type'] = type(exc).__name__
                controller.state['failure'] = str(exc) if isinstance(exc, RuntimeError) else 'Operation interrupted; inspect private logs'
                controller.save('failed_' + args.action)
            print(json.dumps({'status': 'failed', 'action': args.action,
                              'reason': str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__,
                              'state': str(Path(controller.state['directory']) / 'state.json') if controller.state else None}), file=sys.stderr)
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
