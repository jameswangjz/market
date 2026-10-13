"""Record the scoped R2 batch; preserve unfinished tasks and evidence boundaries."""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--verified', action='store_true')
    args = parser.parse_args()
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from sqlalchemy import select
    updates = {
        'TRD-R2-ARC-001': ('review', 100, '本轮需求、历史冲突矩阵、详细设计和20项任务完成；新增API统一最新调用规范，无旧调用方兼容期。'),
        'TRD-R2-FE-001': ('in_progress', 60, 'gpt-6-luna medium完成租户开关删除、真实角色标签和配额合并；当前模型审查，浏览器验证待完成。'),
        'TRD-R2-FE-002': ('in_progress', 60, 'gpt-6-luna medium完成支付响应即时应用、详情支付单更新及旧列表请求隔离；浏览器真实确认支付验证待完成。'),
        'TRD-R2-FE-003': ('in_progress', 20, '已移除登记兼容性控件，并同步提供方/购买方规范和前端下载源；类型联动、自动路由及其它登记规则待后端配套。'),
    }
    if args.verified:
        updates['TRD-R2-FE-001'] = ('in_progress', 80, '当前模型验证185项前端测试及生产构建通过；角色、租户按钮、配额标签代码已验证，待实际浏览器验收。')
        updates['TRD-R2-FE-002'] = ('in_progress', 80, '当前模型验证185项前端测试及生产构建通过；支付单及订单即时更新、异步隔离通过，待浏览器模拟支付复测。')
    with m.SessionLocal() as db:
        actor = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if not actor:
            raise ValueError('Task actor unavailable')
        for code, (status, progress, note) in updates.items():
            task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code))
            if not task:
                raise ValueError('Task missing: ' + code)
            if task.status == 'done':
                continue
            m.update_development_task(code, m.DevelopmentTaskUpdate(status=status, progress=progress, note=note), actor, db)
        result = m.development_tasks(actor, db)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'counts': result['counts'], 'updated': list(updates)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
