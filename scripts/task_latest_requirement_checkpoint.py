"""Reconcile unfinished task acceptance without claiming new test completion."""
import argparse
import json
import os
from pathlib import Path


REPLACEMENTS = {
    'TRD-FE-009': ('升降级前兼容性确认',
                   'API及模型API统一最新调用方式、不要求旧调用方兼容；SaaS版本切换提示'),
    'TRD-ARC-003': ('仅实际通过标记done',
                    '开发验证通过标记review，用户前端验收确认后才标记done'),
}

ACTIVE_NOTES = {
    'TRD-R2-BE-008': '正在补齐消息企业/视角隔离及提供方清算详情、对方确认；完成后须通过隔离权限及前端回归。',
    'TRD-R2-OPS-001': '正在补齐构建产物溯源门禁及隔离备份恢复/版本兼容验证；不停止共享业务服务。',
    'TRD-BE-011': '已发现并修正策略刷新可能移动计数范围的问题，正在联合Redis/Lua、迁移及实际部署证据；不因旧百分比直接结项。',
    'FLT-OPS-002': '正在增加精确测试对象Worker选择及独立故障端点；原子隔离注册和全部故障时序尚未验收。',
    'TRD-R2-QA-002': '等待本轮集成冻结后进行真实浏览器回归；保留已有通过证据，不将只读展示当成交易及同步验证。',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from sqlalchemy import select
    with m.SessionLocal() as db:
        actor = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if not actor:
            raise ValueError('Audit actor unavailable')
        changed = []
        for code, (old, new) in REPLACEMENTS.items():
            task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code))
            if task is None:
                raise ValueError('Task missing: ' + code)
            if task.status == 'done' or old not in task.acceptance:
                continue
            before = task.acceptance
            task.acceptance = before.replace(old, new)
            m.audit(db, actor.email, 'reconcile_task_acceptance', 'development_task',
                    target_id=task.id, detail='最新需求覆盖旧验收条件；状态及进度不变',
                    before={'acceptance': before}, after={'acceptance': task.acceptance})
            db.commit()
            changed.append(code)
        for code, note in ACTIVE_NOTES.items():
            task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code))
            if task is None or task.status != 'in_progress':
                continue
            m.update_development_task(code, m.DevelopmentTaskUpdate(
                status=task.status, progress=task.progress,
                note='最新需求复核：' + note), actor, db)
        snapshot = m.development_tasks(actor, db)
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str),
                           encoding='utf-8')
    print(json.dumps({'changed': changed, 'counts': snapshot['counts']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
