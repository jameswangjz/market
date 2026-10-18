"""Inspect CLR2 tasks without changing them; notifications are explicitly opt-in."""
import argparse
import hashlib
import json
import os
from datetime import datetime, timezone

import clearing_v2_tasks_control as clearing
from trading_task_control import inspect

TASKS = tuple(getattr(clearing, 'TASKS', clearing.control.TASKS))


def expected_tasks():
    # The clearing controller configures the shared list controller on import.
    tasks = TASKS
    codes = tuple(task[0] for task in tasks)
    if len(codes) != 17 or len(set(codes)) != 17 or any(not code.startswith('CLR2-') for code in codes):
        raise ValueError('Expected exactly 17 unique CLR2 tasks')
    return tasks


def inspect_tasks(rows, now=None, stale_minutes=60):
    tasks = expected_tasks()
    expected = {task[0]: task for task in tasks}
    selected = [dict(row) for row in rows if row['code'] in expected]
    # Use the approved dependency graph, even if stored task metadata drifted.
    drift = []
    for row in selected:
        planned = expected[row['code']][3]
        actual = ','.join(x.strip() for x in row['dependencies'].split(',') if x.strip())
        if set(actual.split(',')) != set(planned.split(',')):
            drift.append(dict(code=row['code'], reason='dependency_drift', expected=planned, actual=actual))
        row['dependencies'] = planned
    result = inspect(selected, tuple(expected), now=now, stale_minutes=stale_minutes)
    result['alerts'].extend(drift)
    result.update(expected_total=17, task_family='CLR2', read_only_tasks=True)
    return result


def notification_key(alerts, now):
    stable = [{key: value for key, value in alert.items() if key != 'minutes'} for alert in alerts]
    stable.sort(key=lambda alert: json.dumps(alert, sort_keys=True))
    fingerprint = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:24]
    return 'clr2-monitor:' + now.astimezone(timezone.utc).strftime('%Y%m%d%H') + ':' + fingerprint


def notify(db, model, message_center, result, now):
    if not result['alerts']:
        return False
    from sqlalchemy import select
    recipients = list(db.scalars(select(model.id).where(model.platform_role == 'super_admin',
        model.is_active.is_(True), model.activation_status == 'active')))
    if not recipients:
        return False
    message_center['create'](db, recipients, 'CLR2清算开发任务需要检查',
        json.dumps(result['alerts'], ensure_ascii=False), target_type='development_task',
        severity='important', category='ops', event_key=notification_key(result['alerts'], now))
    db.commit()
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stale-minutes', type=int, default=60)
    parser.add_argument('--notify', action='store_true')
    args = parser.parse_args(argv)
    if not 5 <= args.stale_minutes <= 1440:
        parser.error('stale-minutes must be 5..1440')
    # Never silently inspect a local fallback database when deployment config is missing.
    if not os.environ.get('DATABASE_URL'):
        parser.error('DATABASE_URL must be supplied by the runtime environment')
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from sqlalchemy import select
    from app.main import DevelopmentTask, SessionLocal, User, message_center
    clock = datetime.now(timezone.utc)
    codes = [task[0] for task in expected_tasks()]
    with SessionLocal() as db:
        rows = [{field: getattr(row, field) for field in
                 ('code', 'status', 'progress', 'dependencies', 'acceptance', 'updated_at')}
                for row in db.scalars(select(DevelopmentTask).where(DevelopmentTask.code.in_(codes)))]
        result = inspect_tasks(rows, now=clock, stale_minutes=args.stale_minutes)
        result['notification_enabled'] = args.notify
        result['notification_requested'] = notify(db, User, message_center, result, clock) if args.notify else False
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
