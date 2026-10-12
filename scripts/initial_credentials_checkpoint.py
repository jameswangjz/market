"""Track this increment without declaring the unfinished version-change Saga complete."""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('start', 'verified'), required=True)
    parser.add_argument('--proof')
    parser.add_argument('--runtime-tag', default='development')
    args = parser.parse_args()
    if args.phase == 'verified':
        proof = json.loads(Path(args.proof).read_text()) if args.proof else {}
        if (proof.get('status') != 'passed' or proof.get('worker_exercised') is not True
                or proof.get('cleanup') != 'exact owned fixtures removed; native DB2 rate keys expire'):
            raise ValueError('Complete deployed runtime and exact cleanup evidence required')
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from sqlalchemy import select
    updates = {
        'TRD-BE-012': (90, '继续首次买方共享凭据自动签发、密文持久化及授权下载；不自动轮换停用凭据。'),
        'TRD-BE-018': (10, '开始升降级正负经济调整台账；实际资金和资源Saga、月度清算批次接入尚未实现。'),
    }
    if args.phase == 'verified':
        updates = {
            'TRD-BE-012': (95, '首次API/模型API共享凭据自动签发及授权下载完成部署，实际APISIX调用、续费、停用和回滚验证通过。综合四类线上交付审核到购买验收仍不关闭。'),
            'TRD-BE-018': (25, '正负经济调整台账及原/目标规则快照、防重复和事务回滚验证通过；尚未接入版本变更Saga和月度清算批次。'),
        }
    with m.SessionLocal() as db:
        admin = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if not admin:
            raise ValueError('Platform audit actor unavailable')
        for code in updates:
            task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code))
            if not task or task.status in {'done', 'review'}:
                raise ValueError('Task requires manual review: ' + code)
        for code, (progress, note) in updates.items():
            task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code))
            m.update_development_task(code, m.DevelopmentTaskUpdate(
                status='in_progress', progress=max(task.progress, progress),
                note=args.runtime_tag + '; ' + note), admin, db)
        snapshot = m.development_tasks(admin, db)
    print(json.dumps({'counts': snapshot['counts'], 'tasks': list(updates)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
