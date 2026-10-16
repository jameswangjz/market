"""Register/checkpoint the approved 20261016 list work without inferring acceptance."""
import argparse
import json
import os
from pathlib import Path

TASKS = (
    ('LIST-ARC-001', '列表消费者、分页字段和权限契约', 'ARC', '', '旧调用兼容；服务端权限内计数；中文和日期范围契约'),
    ('LIST-BE-001', '产品列表分页及五条件检索', 'BE', 'LIST-ARC-001', '名称目录提供方类型状态；企业隔离；稳定排序'),
    ('LIST-BE-002', '订单分页八条件及交易双方展示', 'BE', 'LIST-ARC-001', '交易双方；个人历史；状态组合；成本不泄露'),
    ('LIST-BE-003', '交付售后退款三个列表接口', 'BE', 'LIST-ARC-001', '独立条件状态分页；退款买卖双方及产品；订单授权'),
    ('LIST-BE-004', '网关路由检索历史及路由级详情', 'BE', 'LIST-ARC-001', '默认已发布；历史可查；路由统计发布记录告警不串数据'),
    ('LIST-BE-005', '网关告警持久化处置及审计', 'BE', 'LIST-BE-004', '确认处理关闭；权限幂等并发；GET不变更状态'),
    ('LIST-FE-001', '产品订单检索分页及交易双方列', 'FE', 'LIST-BE-001,LIST-BE-002', '中文方式；每页条数总数跳页；查询重置；交易双方'),
    ('LIST-FE-002', '交付售后三Tab独立检索分页', 'FE', 'LIST-BE-003', '三Tab；订单链接；退款产品提供方购买方'),
    ('LIST-FE-003', '只读网关台账及告警处置', 'FE', 'LIST-BE-004,LIST-BE-005', '无路由编辑发布入口；统计记录告警；历史查询'),
    ('LIST-QA-001', '产品订单与交付售后组合回归', 'QA', 'LIST-FE-001,LIST-FE-002', '角色数据隔离；分页与筛选；浏览器关键链路'),
    ('LIST-QA-002', '网关监控告警组合回归', 'QA', 'LIST-FE-003', '历史路由及告警处置；统计边界；审计'),
    ('LIST-OPS-001', '列表功能部署与待验收入口', 'OPS', 'LIST-QA-001,LIST-QA-002', '真实部署版本与健康；不修改财务数据；用户验收入口'),
)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--update', action='append', default=[], help='CODE:STATUS:PERCENT')
    p.add_argument('--note', required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    if not args.note.strip():
        raise ValueError('Activity note required')
    updates = {}
    for value in args.update:
        code, status, percent = value.split(':')
        percent = int(percent)
        if code not in {row[0] for row in TASKS} or status not in {'todo', 'in_progress', 'review'}:
            raise ValueError('Only approved list tasks and non-acceptance states allowed')
        if not (status == 'todo' and percent == 0 or status == 'in_progress' and 1 <= percent <= 99
                or status == 'review' and percent == 100):
            raise ValueError('Progress must match status')
        updates[code] = status, percent
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from sqlalchemy import select
    with os.fdopen(fd, 'w') as out, m.SessionLocal() as db:
        actor = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if actor is None or actor.platform_role != 'super_admin' or not actor.is_active:
            raise ValueError('Active platform administrator required')
        created = []
        for code, title, area, dependencies, acceptance in TASKS:
            task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code).with_for_update())
            if task is None:
                task = m.DevelopmentTask(code=code, title=title, area=area, dependencies=dependencies,
                    owner='主Agent/开发与验证Agent', priority='P1', status='todo', progress=0,
                    acceptance=acceptance + '\n依据：28 列表检索分页与网关监控需求及任务-20261016143205.md')
                db.add(task)
                created.append(code)
                m.audit(db, actor.email, 'register_list_task', 'development_task', code,
                        args.note, before={}, after={'status': 'todo', 'progress': 0})
            if code in updates:
                if task.status not in {'todo', 'in_progress', 'review'}:
                    raise ValueError('Cannot override accepted or blocked tasks')
                before = {'status': task.status, 'progress': task.progress}
                task.status, task.progress = updates[code]
                task.updated_at = m.now()
                task.acceptance = (task.acceptance or '') + '\n执行记录：' + args.note
                m.audit(db, actor.email, 'checkpoint_list_task', 'development_task', code,
                    args.note, before=before, after={'status': task.status, 'progress': task.progress})
        db.flush()
        items = [{'code': row.code, 'status': row.status, 'progress': row.progress}
                 for row in db.scalars(select(m.DevelopmentTask).where(
                     m.DevelopmentTask.code.in_([row[0] for row in TASKS])).order_by(m.DevelopmentTask.code))]
        result = {'captured_at': m.now().isoformat(), 'created': created, 'items': items, 'note': args.note}
        db.commit()
        json.dump(result, out, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
