"""PostgreSQL-backed, administrator-only maintenance control plane."""
import json
import uuid
from datetime import timezone

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, String, Text, select, text

QUEUE_LOCK = 723401830049
RUNNER_LOCK = 723401830050
ACTIONS = {'inspect', 'prepare', 'resume', 'recover'}


class MaintenanceRequest(BaseModel):
    action: str
    reason: str = Field(default='', max_length=1000)
    accept_gap: bool = False
    idempotency_key: str = Field(min_length=16, max_length=100)


def install(ns):
    Base, app, now = ns['Base'], ns['app'], ns['now']

    class Task(Base):
        __tablename__ = 'redis_maintenance_tasks'
        id = Column(String(36), primary_key=True, default=lambda: uuid.uuid4().hex)
        task_no = Column(String(60), unique=True, nullable=False)
        action = Column(String(20), nullable=False)
        status = Column(String(24), nullable=False, index=True, default='queued')
        stage = Column(String(40), nullable=False, default='queued')
        actor_id = Column(String(36), nullable=False)
        actor = Column(String(255), nullable=False)
        reason = Column(Text, nullable=False, default='')
        request_key = Column(String(100), nullable=False, unique=True)
        request_json = Column(Text, nullable=False)
        steps_json = Column(Text, nullable=False, default='[]')
        error = Column(String(200), nullable=False, default='')
        evidence_path = Column(Text, nullable=False, default='')
        created_at = Column(DateTime(timezone=True), nullable=False, default=now)
        started_at = Column(DateTime(timezone=True), nullable=True)
        finished_at = Column(DateTime(timezone=True), nullable=True)

    class State(Base):
        __tablename__ = 'redis_maintenance_state'
        id = Column(String(20), primary_key=True)
        snapshot_json = Column(Text, nullable=False, default='null')
        seen_at = Column(DateTime(timezone=True), nullable=True)

    class Collector(Base):
        __tablename__ = 'redis_collector_health'
        id = Column(String(20), primary_key=True)
        status = Column(String(20), nullable=False)
        error = Column(String(100), nullable=False, default='')
        checked_at = Column(DateTime(timezone=True), nullable=False)

    def require_admin(user):
        if user.platform_role != 'super_admin':
            raise HTTPException(403, '仅平台系统管理员可以执行系统维护')

    def task_out(task):
        return {**{key: getattr(task, key) for key in ('id', 'task_no', 'action', 'status', 'stage',
                 'actor', 'reason', 'error', 'created_at', 'started_at', 'finished_at')},
                'steps': json.loads(task.steps_json)}

    def collector_out(db):
        row = db.get(Collector, 'collector')
        if not row:
            return {'ready': False, 'status': 'unknown', 'error': 'collector_heartbeat_unavailable'}
        at = row.checked_at.replace(tzinfo=timezone.utc) if row.checked_at.tzinfo is None else row.checked_at
        fresh = 0 <= (now() - at).total_seconds() < 30
        return {'ready': fresh and row.status == 'ready', 'status': row.status if fresh else 'stale',
                'checked_at': row.checked_at, 'error': row.error if fresh else 'collector_heartbeat_stale'}

    def heartbeat(status, error=''):
        with ns['SessionLocal']() as db:
            row = db.get(Collector, 'collector')
            if row is None:
                row = Collector(id='collector')
                db.add(row)
            row.status, row.error, row.checked_at = status, error or '', now()
            db.commit()

    @app.get('/api/admin/maintenance/redis')
    def overview(user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        require_admin(user)
        state = db.get(State, 'redis')
        snapshot = json.loads(state.snapshot_json) if state else None
        if snapshot is not None:
            snapshot['worker'] = collector_out(db)
            snapshot['executor_seen_at'] = state.seen_at
        tasks = db.scalars(select(Task).order_by(Task.created_at.desc()).limit(20)).all()
        active = db.scalar(select(Task).where(Task.status.in_(['queued', 'running'])).order_by(Task.created_at))
        return {'snapshot': snapshot, 'active_task': task_out(active) if active else None,
                'tasks': [task_out(task) for task in tasks]}

    @app.post('/api/admin/maintenance/redis/tasks')
    def submit(body: MaintenanceRequest, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        require_admin(user)
        if body.action not in ACTIONS:
            raise HTTPException(400, '不支持的维护操作')
        if body.action != 'inspect' and not body.reason.strip():
            raise HTTPException(400, '请填写维护原因')
        if body.action == 'recover' and not body.accept_gap:
            raise HTTPException(400, '异常恢复必须明确接受保留未知计费缺口')
        if body.accept_gap and body.action != 'recover':
            raise HTTPException(400, '该操作不需要接受缺口')
        encoded = json.dumps(body.model_dump(exclude={'idempotency_key'}), sort_keys=True)
        if db.bind.dialect.name == 'postgresql':
            db.execute(text(f'SELECT pg_advisory_xact_lock({QUEUE_LOCK})'))
        previous = db.scalar(select(Task).where(Task.request_key == body.idempotency_key))
        if previous:
            if previous.actor_id != user.id or previous.request_json != encoded:
                raise HTTPException(409, '幂等请求标识已用于其他操作')
            return task_out(previous)
        if db.scalar(select(Task.id).where(Task.status.in_(['queued', 'running'])).limit(1)):
            raise HTTPException(409, '已有维护任务执行中，请等待完成')
        task = Task(task_no='RM-' + now().strftime('%y%m%d%H%M%S') + '-' + uuid.uuid4().hex[:6].upper(),
                    action=body.action, actor_id=user.id, actor=user.email or user.username or user.id,
                    reason=body.reason.strip(), request_key=body.idempotency_key, request_json=encoded)
        db.add(task)
        db.flush()
        ns['audit'](db, task.actor, 'redis_maintenance_request', 'redis_maintenance', task.id,
                    task.reason, category='ops', after={'action': task.action, 'status': 'queued'})
        db.commit()
        return task_out(task)

    hooks = dict(Task=Task, State=State, Collector=Collector, task_out=task_out,
                 collector_out=collector_out, collector_heartbeat=heartbeat)
    ns['redis_maintenance'] = hooks
    return hooks
