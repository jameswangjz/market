"""Validate/import the TRD plan, or monitor it without changing task states."""
import argparse
import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def validate(plan):
    rows = plan['tasks']
    codes = [row['code'] for row in rows]
    if not rows or len(codes) != len(set(codes)):
        raise ValueError('Empty plan or duplicated task code')
    index = {row['code']: row for row in rows}
    for row in rows:
        if not row['code'].startswith('TRD-') or len(row['code']) > 30:
            raise ValueError('Invalid task prefix/code')
        if len(row['title']) > 240 or not row['acceptance'].strip():
            raise ValueError('Invalid task title/acceptance')
        if row['status'] not in {'todo', 'in_progress', 'review', 'blocked', 'done'}:
            raise ValueError('Invalid status')
        if not 0 <= row['progress'] <= 100 or (row['status'] == 'done' and row['progress'] != 100):
            raise ValueError('Invalid progress')
        if any(code not in index for code in row['dependencies']):
            raise ValueError('Missing dependency')
    visited, active = set(), set()

    def visit(code):
        if code in active:
            raise ValueError('Dependency cycle')
        if code in visited:
            return
        active.add(code)
        for dependency in index[code]['dependencies']:
            visit(dependency)
        active.remove(code)
        visited.add(code)

    for code in codes:
        visit(code)
    return rows


def inspect(rows, expected, now=None, stale_minutes=60):
    now = now or datetime.now(timezone.utc)
    index = {row['code']: row for row in rows}
    alerts, ready = [], []
    for code in expected:
        row = index.get(code)
        if not row:
            alerts.append({'code': code, 'reason': 'missing_task'})
            continue
        if row['status'] == 'done' and row['progress'] != 100:
            alerts.append({'code': code, 'reason': 'done_progress_mismatch'})
        if row['status'] in {'todo', 'review', 'in_progress'} and row['progress'] == 100:
            alerts.append({'code': code, 'reason': 'unaccepted_progress_100'})
        deps = [x.strip() for x in row['dependencies'].split(',') if x.strip()]
        unfinished = [x for x in deps if x not in index or index[x]['status'] != 'done']
        if row['status'] == 'in_progress' and unfinished:
            alerts.append({'code': code, 'reason': 'unfinished_dependency', 'dependencies': unfinished})
        if row['status'] == 'todo' and not unfinished:
            ready.append(code)
        updated = row['updated_at']
        if isinstance(updated, str):
            updated = datetime.fromisoformat(updated.replace('Z', '+00:00'))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if row['status'] == 'in_progress' and (now - updated).total_seconds() > stale_minutes * 60:
            alerts.append({'code': code, 'reason': 'stale_progress', 'minutes': int((now-updated).total_seconds()/60)})
        if row['status'] == 'blocked':
            alerts.append({'code': code, 'reason': 'blocked', 'acceptance': row['acceptance']})
    selected = [index[code] for code in expected if code in index]
    counts = {status: sum(row['status'] == status for row in selected)
              for status in ('todo', 'in_progress', 'review', 'blocked', 'done')}
    return {'checked_at': now.isoformat(), 'total': len(selected), 'counts': counts,
            'ready': ready, 'alerts': alerts,
            'interpretation': 'Stale progress is a review signal, not proof that a process is stuck. No task state is changed automatically.'}


def render(plan, output):
    rows = validate(plan)
    output.mkdir(parents=True, exist_ok=True)
    with (output / '产品交易履约开发任务-20261010.csv').open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['任务编号', '负责人', '阶段', '标题', '依赖', '初始状态', '进度', '验收条件'])
        for row in rows:
            writer.writerow([row['code'], row['owner'], row['stage'], row['title'], ','.join(row['dependencies']),
                             row['status'], row['progress'], row['acceptance']])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['validate', 'render', 'import', 'monitor'])
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--stale-minutes', type=int, default=60)
    parser.add_argument('--notify', action='store_true')
    args = parser.parse_args()
    if not 5 <= args.stale_minutes <= 1440:
        parser.error('stale-minutes must be 5..1440')
    plan = json.loads(args.plan.read_text(encoding='utf-8'))
    spec = validate(plan)
    if args.mode == 'validate':
        print(json.dumps({'valid': True, 'tasks': len(spec), 'version': plan['version']}))
        return
    if args.mode == 'render':
        render(plan, args.output or args.plan.parent)
        return
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from sqlalchemy import select
    from app.main import DevelopmentTask, SessionLocal, User, audit, message_center
    with SessionLocal() as db:
        if args.mode == 'import':
            existing = {x.code: x for x in db.scalars(select(DevelopmentTask).where(DevelopmentTask.code.in_([x['code'] for x in spec])))}
            inserted = 0
            for row in spec:
                if row['code'] in existing:
                    continue
                db.add(DevelopmentTask(code=row['code'], owner=row['owner'], title=row['title'], area=row['area'],
                                      priority=row['priority'], status=row['status'], progress=row['progress'],
                                      dependencies=','.join(row['dependencies']), acceptance=row['acceptance']))
                inserted += 1
            if inserted:
                audit(db, 'task-planner', 'import_trading_task_plan', 'development_task_plan', plan['version'],
                      category='ops', business_domain='development', after={'inserted': inserted, 'total': len(spec)})
            db.commit()
            print(json.dumps({'total': len(spec), 'inserted': inserted, 'existing_states_preserved': True}))
            return
        rows = [{field: getattr(row, field) for field in ('code', 'status', 'progress', 'dependencies', 'acceptance', 'updated_at')}
                for row in db.scalars(select(DevelopmentTask))]
        result = inspect(rows, [row['code'] for row in spec], stale_minutes=args.stale_minutes)
        if args.notify and result['alerts']:
            recipients = list(db.scalars(select(User.id).where(User.platform_role.in_(['super_admin', 'platform_operator']),
                                                               User.is_active.is_(True), User.activation_status == 'active')))
            if recipients:
                stable = [{k: v for k, v in alert.items() if k != 'minutes'} for alert in result['alerts']]
                fingerprint = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:24]
                key = 'trd-monitor:' + datetime.now(timezone.utc).strftime('%Y%m%d%H') + ':' + fingerprint
                message_center['create'](db, recipients, '交易履约开发任务需要检查',
                                         json.dumps(result['alerts'], ensure_ascii=False),
                                         target_type='development_task', severity='important', category='ops', event_key=key)
                db.commit()
        print(json.dumps(result, ensure_ascii=False))
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output / '产品交易履约任务巡检-20261010.json').write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
