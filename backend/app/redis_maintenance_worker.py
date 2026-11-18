"""Unprivileged maintenance executor: PostgreSQL queue, no SSH/Kubernetes client."""
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from .api_charge_ledger import bounded_client
from .redis_maintenance import RUNNER_LOCK

logger = logging.getLogger(__name__)
LABELS = {'inspect': '检查Redis及计费来源', 'backup': '备份数据库', 'seal': '暂停并封存计费来源',
          'verify_seal': '校验封存证据', 'resume': '恢复原计费分代', 'open': '登记缺口并建立新分代',
          'verify': '核对来源及采集状态', 'completed': '完成'}


def healthy_persistence(snapshot):
    value = snapshot.get('persistence', {})
    return (value.get('loading') == 0 and value.get('aof_enabled') == 1
            and value.get('aof_last_write_status') == 'ok'
            and all(value.get(key) in (None, 'ok') for key in
                    ('aof_last_bgrewrite_status', 'rdb_last_bgsave_status')))


class MaintenanceError(Exception):
    """Safe operator-facing explanation, never a database or connection exception."""


class RedisRuntime:
    def __init__(self, ns):
        self.ns = ns
        path = Path(os.getenv('REDIS_DIAGNOSTIC_SCRIPT', '/app/ops/redis_source_continuity.py'))
        spec = importlib.util.spec_from_file_location('market_maintenance_diagnostics', path)
        self.diagnostics = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.diagnostics)
        self.root = Path(os.getenv('REDIS_MAINTENANCE_STORAGE', '/var/lib/market-maintenance'))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def inspect(self):
        client = bounded_client(self.ns)
        try:
            with self.ns['SessionLocal']() as db:
                result = self.diagnostics.build_report(db, client, self.ns['api_charge_ledger'])
                db.rollback()
                return result
        finally:
            client.close()

    def backup(self, task_id):
        url = make_url(self.ns['DATABASE_URL'])
        if url.get_backend_name() != 'postgresql':
            raise MaintenanceError('维护备份仅支持PostgreSQL')
        environment = {**os.environ, 'PGHOST': url.host or '', 'PGPORT': str(url.port or 5432),
                       'PGUSER': url.username or '', 'PGPASSWORD': url.password or '',
                       'PGDATABASE': url.database or '', 'PGCONNECT_TIMEOUT': '10',
                       'PGOPTIONS': '-c statement_timeout=180000 -c lock_timeout=10000'}
        temporary = self.root / (task_id + '.dump.partial')
        final = self.root / (task_id + '.dump')
        with temporary.open('xb') as output:
            subprocess.run(['pg_dump', '--format=custom'], env=environment, stdout=output,
                           stderr=subprocess.PIPE, timeout=240, check=True)
            output.flush()
            os.fsync(output.fileno())
        if not temporary.stat().st_size:
            raise MaintenanceError('数据库备份为空，已停止维护')
        subprocess.run(['pg_restore', '--list', str(temporary)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=30, check=True)
        temporary.replace(final)
        with final.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        return {'backup_completed': True, 'bytes': final.stat().st_size, 'sha256': digest}

    def source(self, action, actor_id, reason):
        client = bounded_client(self.ns)
        try:
            with self.ns['SessionLocal']() as db:
                actor = db.get(self.ns['User'], actor_id)
                if not actor or not actor.is_active or actor.activation_status != 'active' or actor.platform_role != 'super_admin':
                    raise MaintenanceError('操作人员不再具备维护权限，已停止执行')
                result = self.ns['api_charge_ledger'][action + '_epoch'](db, client, actor.email or actor.id, reason)
                self.ns['audit'](db, actor.email or actor.id, 'redis_source_' + action, 'api_charge_source',
                                 result['source_id'], reason, category='ops', after=result)
                db.commit()
                return result
        finally:
            client.close()

    def evidence(self, task_id, value):
        temporary = self.root / (task_id + '.json.tmp')
        final = self.root / (task_id + '.json')
        with temporary.open('w') as output:
            json.dump(value, output, ensure_ascii=False, indent=2, default=str)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(final)
        return str(final)


def save_snapshot(ns, snapshot):
    with ns['SessionLocal']() as db:
        State = ns['redis_maintenance']['State']
        row = db.get(State, 'redis')
        if row is None:
            row = State(id='redis')
            db.add(row)
        row.snapshot_json = json.dumps(snapshot, default=str)
        row.seen_at = ns['now']()
        db.commit()


def execute_task(ns, task_id, runtime, fence=lambda: None):
    Task = ns['redis_maintenance']['Task']
    def step(stage, operation):
        fence()
        with ns['SessionLocal']() as db:
            task = db.get(Task, task_id)
            if task.status != 'running':
                raise MaintenanceError('维护任务已被中断，禁止继续执行')
            task.stage = stage
            steps = json.loads(task.steps_json)
            steps.append({'stage': stage, 'label': LABELS[stage], 'status': 'running', 'at': ns['now']().isoformat()})
            task.steps_json = json.dumps(steps, ensure_ascii=False)
            db.commit()
        result = operation()
        with ns['SessionLocal']() as db:
            task = db.get(Task, task_id)
            steps = json.loads(task.steps_json)
            steps[-1].update(status='succeeded', finished_at=ns['now']().isoformat())
            if stage in {'backup', 'seal', 'resume', 'open'}:
                steps[-1]['result'] = result
            task.steps_json = json.dumps(steps, ensure_ascii=False)
            ns['audit'](db, task.actor, 'redis_maintenance_step', 'redis_maintenance', task.id,
                         LABELS[stage], category='ops', before={'stage': stage, 'status': 'running'},
                         after={'stage': stage, 'status': 'succeeded'})
            db.commit()
            evidence = ns['redis_maintenance']['task_out'](task)
        runtime.evidence(task_id, evidence)
        return result

    def inspect():
        snapshot = runtime.inspect()
        save_snapshot(ns, snapshot)
        return snapshot

    try:
        with ns['SessionLocal']() as db:
            task = db.get(Task, task_id)
            actor = db.get(ns['User'], task.actor_id)
            if not actor or not actor.is_active or actor.activation_status != 'active' or actor.platform_role != 'super_admin':
                raise MaintenanceError('操作人员已停用或维护权限已取消')
            created = task.created_at.replace(tzinfo=timezone.utc) if task.created_at.tzinfo is None else task.created_at
            if (ns['now']() - created).total_seconds() > 300:
                raise MaintenanceError('排队时间超过5分钟，请重新检查状态后提交')
            action, actor_id, reason = task.action, task.actor_id, task.reason
        before = step('inspect', inspect)
        status = before.get('status')
        if action != 'inspect':
            if not healthy_persistence(before):
                raise MaintenanceError('Redis持久化不健康，禁止维护，请先修复存储或连接')
            expected = {'prepare': 'active_source_healthy', 'resume': 'safe_sealed_resume_candidate',
                        'recover': 'explicit_gap_required'}[action]
            if action == 'recover' and status == 'active_source_healthy':
                raise MaintenanceError('当前来源健康，无需异常恢复，未建立新分代')
            if status != expected:
                raise MaintenanceError('来源状态已变化或不符合操作条件，请刷新状态后重试')
            step('backup', lambda: runtime.backup(task_id))
            if action == 'prepare':
                sealed = step('seal', lambda: runtime.source('seal', actor_id, reason))
                if sealed.get('status') != 'paused' or not sealed.get('sealed_hash'):
                    raise MaintenanceError('封存未完成，禁止停机')
                after = step('verify_seal', inspect)
                if after.get('status') != 'safe_sealed_resume_candidate':
                    raise MaintenanceError('封存证据校验失败，尚不可停机，请核查')
            else:
                operation = 'resume' if action == 'resume' else 'open'
                recovery_started = ns['now']()
                step(operation, lambda: runtime.source(operation, actor_id, reason))
                after = step('verify', inspect)
                if after.get('status') != 'active_source_healthy':
                    raise MaintenanceError('来源恢复后的校验未通过，请检查维护记录')
                deadline = time.monotonic() + 60
                while True:
                    fence()
                    with ns['SessionLocal']() as db:
                        health = ns['redis_maintenance']['collector_out'](db)
                    at = health.get('checked_at')
                    if at and at.tzinfo is None:
                        at = at.replace(tzinfo=timezone.utc)
                    if health['ready'] and at and at >= recovery_started:
                        break
                    if time.monotonic() >= deadline:
                        raise MaintenanceError('来源已恢复，但采集Worker尚未就绪，请检查后刷新状态')
                    time.sleep(2)
        with ns['SessionLocal']() as db:
            task = db.get(Task, task_id)
            task.status, task.stage, task.finished_at = 'succeeded', 'completed', ns['now']()
            ns['audit'](db, task.actor, 'redis_maintenance_succeeded', 'redis_maintenance', task.id,
                         task.reason, category='ops', before={'status': 'running'},
                         after={'status': 'succeeded', 'action': task.action})
            db.commit()
            evidence = ns['redis_maintenance']['task_out'](task)
        runtime.evidence(task_id, evidence)
    except Exception as exc:
        message = str(exc) if isinstance(exc, MaintenanceError) else '维护执行失败，已停止自动操作，请核查任务与执行器日志'
        logger.error('Maintenance stopped: task=%s type=%s', task_id, type(exc).__name__)
        with ns['SessionLocal']() as db:
            task = db.get(Task, task_id)
            task.status, task.error, task.finished_at = 'failed', message, ns['now']()
            steps = json.loads(task.steps_json)
            if steps and steps[-1]['status'] == 'running':
                steps[-1].update(status='failed', error=message, finished_at=ns['now']().isoformat())
                task.steps_json = json.dumps(steps, ensure_ascii=False)
            ns['audit'](db, task.actor, 'redis_maintenance_failed', 'redis_maintenance', task.id,
                         message, category='ops', result='failure', after={'status': 'failed', 'stage': task.stage})
            db.commit()
        try:
            inspect()
        except Exception:
            save_snapshot(ns, {'status': 'dependency_unavailable', 'reasons': ['inspection_unavailable'],
                               'captured_at': ns['now']().isoformat()})


def process_once(ns, runtime):
    Task = ns['redis_maintenance']['Task']
    with ns['engine'].connect() as guard:
        postgres = guard.dialect.name == 'postgresql'
        if postgres and not guard.scalar(text(f'SELECT pg_try_advisory_lock({RUNNER_LOCK})')):
            return
        pid = guard.scalar(text('SELECT pg_backend_pid()')) if postgres else None
        def fence():
            if postgres and guard.scalar(text('SELECT pg_backend_pid()')) != pid:
                raise MaintenanceError('执行器数据库会话已变化，必须人工核查')
        try:
            with ns['SessionLocal']() as db:
                # A previous running task cannot be replayed after executor interruption.
                for old in db.scalars(select(Task).where(Task.status == 'running')):
                    old.status, old.error, old.finished_at = 'manual_review', '执行器中断；未自动重放，请检查当前来源及已完成步骤', ns['now']()
                    ns['audit'](db, 'system', 'redis_maintenance_interrupted', 'redis_maintenance', old.id,
                                 old.error, category='ops', result='failure', after={'status': 'manual_review'})
                db.commit()
                task = db.scalar(select(Task).where(Task.status == 'queued').order_by(Task.created_at).with_for_update())
                if task is None:
                    return
                task.status, task.started_at = 'running', ns['now']()
                task_id = task.id
                ns['audit'](db, task.actor, 'redis_maintenance_started', 'redis_maintenance', task.id,
                             task.reason, category='ops', before={'status': 'queued'}, after={'status': 'running'})
                db.commit()
            execute_task(ns, task_id, runtime, fence)
        finally:
            if postgres:
                guard.execute(text(f'SELECT pg_advisory_unlock({RUNNER_LOCK})'))


def main():
    os.umask(0o077)
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    logging.basicConfig(level=logging.INFO)
    ns = vars(importlib.import_module('app.main'))
    from sqlalchemy import event
    if ns['engine'].dialect.name == 'postgresql':
        @event.listens_for(ns['engine'], 'connect')
        def connection_limits(connection, _):
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '60s'")
                cursor.execute("SET lock_timeout = '10s'")
            connection.commit()
    runtime = RedisRuntime(ns)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    last_inspection = -100
    while not stopped.is_set():
        try:
            process_once(ns, runtime)
            if time.monotonic() - last_inspection >= 30:
                try:
                    snapshot = runtime.inspect()
                except Exception:
                    snapshot = {'status': 'dependency_unavailable', 'reasons': ['inspection_unavailable'],
                                'captured_at': ns['now']().isoformat()}
                save_snapshot(ns, snapshot)
                last_inspection = time.monotonic()
            Path('/tmp/redis-maintenance-alive').touch()
        except Exception as exc:
            logger.error('Maintenance executor cycle failed: %s', type(exc).__name__)
        stopped.wait(2)


if __name__ == '__main__':
    main()
