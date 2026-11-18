#!/usr/bin/env python3
"""Controlled Redis maintenance for market; never infer missing usage as zero."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

NAMESPACE = 'market'


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'planned-restart', 'resume', 'recover'))
    parser.add_argument('--reason', default='')
    parser.add_argument('--actor', default='admin@market.local')
    parser.add_argument('--confirm-restart', action='store_true')
    parser.add_argument('--accept-gap', action='store_true')
    parser.add_argument('--backup-root', type=Path, default=Path('/root/market/backups'))
    args = parser.parse_args(argv)
    if args.action != 'status' and not args.reason.strip():
        parser.error('Changing actions require --reason')
    if args.action == 'planned-restart' and not args.confirm_restart:
        parser.error('planned-restart requires --confirm-restart')
    if args.action == 'recover' and not args.accept_gap:
        parser.error('recover requires --accept-gap; unknown usage will remain unresolved')
    if args.confirm_restart and args.action != 'planned-restart':
        parser.error('--confirm-restart is only valid for planned-restart')
    if args.accept_gap and args.action != 'recover':
        parser.error('--accept-gap is only valid for recover')
    return args


class Runtime:
    def __init__(self, args):
        self.args = args
        self.tools = Path(__file__).resolve().parent
        for name in ('redis_source_admin.py', 'redis_source_continuity.py'):
            if not (self.tools / name).is_file():
                raise RuntimeError(f'Required companion script missing: {name}')
        identity = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S') + '-' + uuid.uuid4().hex[:8]
        self.output = args.backup_root / ('redis-maintenance-' + identity)
        self.output.mkdir(parents=True, mode=0o700)
        self.counter = 0

    def command(self, *args, timeout=240, **kwargs):
        result = subprocess.run(['kubectl', '-n', NAMESPACE, *args], text=True,
                                capture_output=True, timeout=timeout, **kwargs)
        if result.returncode:
            # kubectl stderr may contain connection details: archive privately, not stdout.
            self.save('kubectl-error-' + uuid.uuid4().hex + '.log', result.stderr)
            raise RuntimeError('kubectl operation failed; inspect the private evidence directory')
        return result.stdout

    def save(self, name, value):
        with (self.output / name).open('x') as stream:
            stream.write(value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False))

    def remote(self, script, args):
        self.counter += 1
        name = f'{self.counter:02d}-{Path(script).stem}-{uuid.uuid4().hex}.json'
        remote = '/tmp/' + name
        self.command('exec', '-i', 'deployment/market-api', '--', 'env', 'PYTHONPATH=/app',
                     'python', '-B', '-', *args, '--actor', self.args.actor, '--output', remote,
                     input=(self.tools / script).read_text(), timeout=300)
        report = json.loads(self.command('exec', 'deployment/market-api', '--', 'cat', remote))
        self.save(name, report)
        # Remove only the unique report just archived on the host.
        self.command('exec', 'deployment/market-api', '--', 'python', '-c',
                     'from pathlib import Path; import sys; Path(sys.argv[1]).unlink()', remote)
        return report

    def diagnose(self):
        return self.remote('redis_source_continuity.py', [])

    def backup(self):
        for name in ('market-redis', 'api-charge-ledger-worker'):
            self.save(name + '.json', json.loads(self.command('get', 'deployment', name, '-o', 'json')))
        path = self.output / 'database-before.sql'
        with path.open('x') as output:
            result = subprocess.run(['kubectl', '-n', NAMESPACE, 'exec', 'deployment/market-postgres',
                                     '--', 'pg_dump', '-U', 'market', '-d', 'market'], text=True,
                                    stdout=output, stderr=subprocess.PIPE, timeout=300)
        if result.returncode or not path.stat().st_size:
            raise RuntimeError('Database backup failed; no source transition or Redis restart performed')

    def source(self, action):
        report = self.remote('redis_source_admin.py', [action, '--reason', self.args.reason])
        source = report.get('source') or {}
        expected = 'paused' if action == 'seal' else 'active'
        if report.get('status') != 'passed' or source.get('status') != expected:
            raise RuntimeError(f'{action} did not reach the expected source state')
        if action == 'seal' and not source.get('sealed_hash'):
            raise RuntimeError('Seal evidence missing; do not restart Redis')

    def restart(self):
        self.command('rollout', 'restart', 'deployment/market-redis')
        self.command('rollout', 'status', 'deployment/market-redis', '--timeout=180s')

    def wait_worker(self):
        self.command('rollout', 'status', 'deployment/api-charge-ledger-worker', '--timeout=180s')
        self.save('worker-pods.json', json.loads(self.command('get', 'pods', '-l',
                  'app=api-charge-ledger-worker', '-o', 'json')))


def require_persistence(report):
    persistence = report.get('persistence', {})
    if not (persistence.get('loading') == 0 and persistence.get('aof_enabled') == 1
            and persistence.get('aof_last_write_status') == 'ok'
            and all(persistence.get(key) in (None, 'ok') for key in
                    ('aof_last_bgrewrite_status', 'rdb_last_bgsave_status'))):
        raise RuntimeError('Redis persistence is not healthy; fix storage/persistence before changing the source')


def perform(action, runtime):
    before = runtime.diagnose()
    status = before.get('status')
    if action == 'status':
        return {'result': 'inspection_only', 'source_status': status,
                'reasons': before.get('reasons', []), 'ledger': before.get('ledger')}
    if action == 'recover' and status == 'active_source_healthy':
        return {'result': 'already_healthy_no_change', 'source_status': status}
    if action == 'planned-restart' and status != 'active_source_healthy':
        raise RuntimeError('Source is not healthy; planned restart refused; inspect before recovering')
    if action == 'resume' and status != 'safe_sealed_resume_candidate':
        raise RuntimeError('No verified sealed source to resume; inspect evidence before accepting a gap')
    if action == 'recover' and status != 'explicit_gap_required':
        raise RuntimeError('Do not open a gap for a sealed or unknown state; inspect or use resume')
    require_persistence(before)
    runtime.backup()
    if action == 'planned-restart':
        runtime.source('seal')
        runtime.restart()
        restarted = runtime.diagnose()
        if restarted.get('status') != 'safe_sealed_resume_candidate':
            raise RuntimeError('Post-restart seal verification failed; source remains paused; no automatic gap opened')
        require_persistence(restarted)
        runtime.source('resume')
    else:
        runtime.source('resume' if action == 'resume' else 'open')
    after = runtime.diagnose()
    if after.get('status') != 'active_source_healthy':
        raise RuntimeError('Source transition completed but health verification failed; inspect evidence')
    runtime.wait_worker()
    return {'result': 'completed', 'source_status': after['status'],
            'gap_opened': action == 'recover', 'redis_restarted': action == 'planned-restart',
            'ledger': after.get('ledger'), 'quotas_reset': False}


def main(argv=None):
    args = arguments(argv)
    os.umask(0o077)
    runtime = None
    lock_path = Path('/var/lock/market-redis-maintenance.lock')
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another Redis maintenance command is running; no operation performed', file=sys.stderr)
            return 1
        try:
            runtime = Runtime(args)
            result = perform(args.action, runtime)
            result['evidence'] = str(runtime.output)
            runtime.save('result.json', result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            error = {'result': 'failed', 'reason': str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__,
                     'evidence': str(runtime.output) if runtime else None,
                     'automatic_gap_fallback': False}
            if runtime:
                runtime.save('failure.json', error)
            print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
