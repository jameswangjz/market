"""Register and checkpoint approved Redis recovery work; never infer acceptance."""
import argparse
import json
import os
from pathlib import Path


TASKS = (
    ('REDIS-OPS-001', 'Redis持久卷与AOF部署及数据保全', 'OPS', '',
     '保留当前Redis数据；PVC+AOF everysec；重启后金融键及计费流不丢失；部署前备份；性能对比有边界'),
    ('REDIS-BE-001', '计费来源分代与缺口审计', 'BE', '',
     '旧来源与游标不覆盖；显式新代次；缺口审计；未知用量不当零；旧订阅额度不重置；连续恢复需证据'),
    ('REDIS-QA-001', '来源切换重启与退款安全回归', 'QA', 'REDIS-OPS-001,REDIS-BE-001',
     '真实Redis重启恢复；新来源网关扣额采集；幂等；历史缺证退款拒绝；原账本与受保护订单不变；模拟资金边界'),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', action='store_true')
    parser.add_argument('--progress', action='append', default=[], help='Approved task CODE:PERCENT, 1..99')
    parser.add_argument('--note', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if not args.note.strip():
        raise ValueError('Activity note required')
    updates = {}
    for item in args.progress:
        code, percent = item.rsplit(':', 1)
        percent = int(percent)
        if code not in {t[0] for t in TASKS} or not 1 <= percent <= 99:
            raise ValueError('Only approved Redis tasks at 1..99 may be checkpointed')
        updates[code] = percent
    fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from sqlalchemy import select
    with os.fdopen(fd, 'w') as out, m.SessionLocal() as db:
        actor = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if not actor or actor.platform_role != 'super_admin' or not actor.is_active:
            raise ValueError('Active platform administrator required')
        changes = []
        for code, title, area, dependencies, acceptance in TASKS:
            task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code).with_for_update())
            if task is not None:
                continue
            task = m.DevelopmentTask(code=code, title=title, area=area, priority='P1',
                                     owner='主Agent/开发与验证Agent', dependencies=dependencies,
                                     acceptance=acceptance + '\n用户确认：Redis持久化＋计费来源分代＋缺口审计。',
                                     status='in_progress' if args.start and area != 'QA' else 'todo',
                                     progress=5 if args.start and area != 'QA' else 0)
            db.add(task)
            m.audit(db, actor.email, 'register_redis_recovery_task', 'development_task', code,
                    args.note, before={}, after={'status': task.status, 'progress': task.progress})
            changes.append(code)
        for code, percent in updates.items():
            task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code).with_for_update())
            if task is None or task.status not in ('todo', 'in_progress'):
                raise ValueError('Progress requires a pending or active Redis task')
            before = {'status': task.status, 'progress': task.progress}
            task.status, task.progress, task.updated_at = 'in_progress', percent, m.now()
            task.acceptance = (task.acceptance or '') + '\n执行记录：' + args.note
            m.audit(db, actor.email, 'checkpoint_redis_recovery_task', 'development_task', code,
                    args.note, before=before, after={'status': task.status, 'progress': percent})
        parent = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == 'TRD-R3-OPS-003').with_for_update())
        if parent is None:
            raise ValueError('Original release task missing')
        before = {'dependencies': parent.dependencies, 'acceptance': parent.acceptance}
        dependencies = [v for v in (parent.dependencies or '').split(',') if v]
        if 'REDIS-QA-001' not in dependencies:
            dependencies.append('REDIS-QA-001')
            parent.dependencies = ','.join(dependencies)
            parent.acceptance = (parent.acceptance or '') + '\n当前执行记录：用户已批准修复方案；见REDIS三项任务，未重置旧游标。'
            parent.updated_at = m.now()
            m.audit(db, actor.email, 'link_redis_recovery_tasks', 'development_task', parent.code,
                    args.note, before=before,
                    after={'dependencies': parent.dependencies, 'acceptance': parent.acceptance})
        db.flush()
        result = {'status': 'registered', 'created': changes, 'captured_at': m.now().isoformat(),
                  'items': [{'code': t.code, 'status': t.status, 'progress': t.progress}
                            for t in db.scalars(select(m.DevelopmentTask).where(
                                m.DevelopmentTask.code.in_([v[0] for v in TASKS])))]}
        db.commit()
        json.dump(result, out, ensure_ascii=False, indent=2)
    print(json.dumps({'status': result['status'], 'created': len(changes)}))


if __name__ == '__main__':
    main()
