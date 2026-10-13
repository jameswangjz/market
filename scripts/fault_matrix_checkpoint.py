"""Record bounded completed checks separately from open recovery work."""
import argparse
import json
import os
from pathlib import Path


def validate(proof):
    groups = proof.get('groups', {})
    expected = {'postgresql_worker': 6, 'api_model_adapter': 22, 'native_gateway': 20,
                'saas_auth': 7, 'saas_status': 9, 'saas_network': 2}
    if (proof.get('status') != 'passed_with_findings' or
            proof.get('scope') != 'available bounded development-environment fault checks' or
            proof.get('executed_check_count') != sum(expected.values()) or
            set(groups) != set(expected) or
            any(groups[key].get('status') != 'passed' or groups[key].get('count') != count
                for key, count in expected.items()) or
            proof.get('cleanup_verified') is not True or
            groups['postgresql_worker'].get('isolated_schema_removed') is not True or
            proof.get('shared_services_stopped') is not False or
            proof.get('worker_batch_changed') is not False or
            not any(item.get('code') == 'OAUTH-CACHE-401' and item.get('status') == 'open'
                    for item in proof.get('findings', []))):
        raise ValueError('Reviewed bounded fault checks, exact cleanup and open findings required')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proof', required=True)
    args = parser.parse_args()
    proof = json.loads(Path(args.proof).read_text())
    validate(proof)
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from sqlalchemy import select
    tasks = [
        ('FLT-QA-001', '当前开发环境有界故障矩阵验证', '测试', 'done', 100,
         '66项检查执行完成，安全断言通过；生产组件与受控注入分别记录，现有业务保护及精确清理通过；缓存401恢复缺口另立任务。'),
        ('FLT-BE-001', 'OAuth缓存401失效处理与自动恢复', '后端', 'todo', 0,
         'SaaS及原生网关收到上游401后清除匹配的旧Token缓存；有效客户端下一次安全重试重新获取；停用客户端仍拒绝；不自动重放非幂等请求。'),
        ('FLT-QA-002', '真实在线支付和银行资金故障联调', '测试', 'blocked', 0,
         '待在线支付接入、真实渠道沙箱及银行/分账回执；当前模拟付款不得替代真实资金验证。'),
        ('FLT-QA-003', '真实第三方SaaS和API故障合约联调', '测试', 'blocked', 0,
         '待供应商测试环境、凭据、幂等与状态查询约定；再验证超时已执行、重复调用、撤销和补偿。'),
        ('FLT-OPS-001', '跨节点与共享基础设施故障演练', '部署', 'blocked', 0,
         '待隔离多节点、PG/Redis高可用与存储恢复环境；不通过停止当前共享服务来模拟生产灾难。'),
        ('FLT-OPS-002', '专属Worker队列及故障代理验证设施', '部署', 'todo', 0,
         '增加仅测试订单可命中的隔离Worker/故障代理，再完成全部资源prepare/apply/proof/restore与资金阶段的守护进程时序组合；当前脚本内注入不冒充此验证。'),
    ]
    with m.SessionLocal() as db:
        actor = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if not actor:
            raise ValueError('Audit actor unavailable')
        for code, title, area, status, progress, acceptance in tasks:
            row = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code))
            if row:
                if row.title != title or row.status != status or row.progress != progress:
                    raise ValueError('Existing task changed; refusing overwrite: ' + code)
                continue
            row = m.DevelopmentTask(code=code, owner='部署测试 Agent' if area != '后端' else '后端 Agent',
                title=title, area=area, status=status, progress=progress, priority='P0',
                dependencies='TRD-QA-005', acceptance=acceptance)
            db.add(row)
            m.audit(db, actor.email, 'create_development_task', 'development_task', code,
                proof['runtime_tag'] + '; ' + acceptance,
                before={}, after={'status': status, 'progress': progress})
        db.commit()
        for code in ('TRD-BE-017', 'TRD-OPS-002', 'TRD-QA-005'):
            task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code))
            if not task or task.status in {'done', 'review'}:
                raise ValueError('Unexpected broad task state: ' + code)
            m.update_development_task(code, m.DevelopmentTaskUpdate(status='in_progress',
                progress=max(task.progress, 95), note=proof['runtime_tag'] +
                '; 当前有界故障矩阵66项检查完成，详见FLT-QA-001；缓存401恢复缺口、真实渠道、供应商及完整守护进程矩阵另列，不将大任务标记全完成。'), actor, db)
        result = m.development_tasks(actor, db)
    print(json.dumps({'counts': result['counts'], 'total': result['total'],
                      'recorded_tasks': [item[0] for item in tasks]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
