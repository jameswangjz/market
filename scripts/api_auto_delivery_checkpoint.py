"""Record the verified API delivery increment, without closing full-flow QA."""
import argparse
import json
import os
from pathlib import Path


def validate(proof):
    expected = {'api: manual, negative, credential, renewal, revoke',
                'model_api: manual, negative, credential, renewal, revoke',
                'api: deployed worker', 'model_api: deployed worker'}
    if (proof.get('status') != 'passed' or proof.get('worker_exercised') is not True
            or set(proof.get('checks', [])) != expected
            or proof.get('payment') != 'simulated'
            or proof.get('cleanup') != 'exact owned fixtures removed; native DB2 rate keys expire'):
        raise ValueError('Complete isolated runtime and cleanup evidence required')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proof', required=True)
    parser.add_argument('--runtime-tag', required=True)
    args = parser.parse_args()
    proof = json.loads(Path(args.proof).read_text(encoding='utf-8-sig'))
    validate(proof)
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from sqlalchemy import select
    updates = {
        'TRD-BE-012': (90, 'API及模型API接入自动Worker，真实APISIX/Redis/模拟上游首购、续费、撤权与故障验证通过。首次调用凭据仍由买方在订单按需生成；尚未完成支付后自动安全下发凭据的闭环。文件与SaaS既有实现保留。'),
        'TRD-QA-003': (75, '隔离PG/APISIX/Redis模拟支付验证API及模型API自动交付、路由篡改拒绝、共享Key、续费不清用量、停用不恢复及实际Worker。完整审核到购买浏览器回归及四类交付整体验收仍未全部完成。'),
        'TRD-OPS-002': (40, 'API/模型API/文件/SaaS自动交付Worker已部署并验证。本批不包含版本升降级Saga、经济调整Worker及其故障恢复；该任务不关闭。'),
    }
    with m.SessionLocal() as db:
        admin = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if not admin:
            raise ValueError('Audit actor unavailable')
        for code, (progress, note) in updates.items():
            task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code))
            if not task or task.status in {'done', 'review'} or task.progress > progress:
                raise ValueError('Task changed; review before recording: ' + code)
        for code, (progress, note) in updates.items():
            m.update_development_task(code, m.DevelopmentTaskUpdate(status='in_progress',
                progress=progress, note=args.runtime_tag + '; ' + note), admin, db)
        snapshot = m.development_tasks(admin, db)
    print(json.dumps({'status': 'updated', 'counts': snapshot['counts'],
                      'tasks': list(updates)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
