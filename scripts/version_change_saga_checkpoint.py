"""Report evidence-based progress without treating real funds as simulated funds."""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('start', 'implemented', 'api-runtime', 'saas-runtime', 'financial-reviewed', 'runtime-regression', 'browser-regression', 'verified'), required=True)
    parser.add_argument('--proof')
    parser.add_argument('--runtime-tag', default='development')
    args = parser.parse_args()
    if args.phase == 'browser-regression':
        proof = json.loads(Path(args.proof).read_text()) if args.proof else {}
        if (proof.get('status') != 'passed' or
                proof.get('scope') != 'real Chrome API upgrade and downgrade to isolated monthly settlement' or
                proof.get('cleanup') != 'exact owned rows and external identities removed; private schema dropped; protected order unchanged' or
                any(proof.get(key) != 'completed' for key in ('desktop_upgrade', 'mobile_downgrade', 'downgrade_refund')) or
                proof.get('blocked_requests') != 0 or proof.get('unexpected_requests') != 0 or
                proof.get('monthly_posted_adjustments') != 4):
            raise ValueError('Reviewed browser transaction, monthly and precise cleanup proof required')
    if args.phase == 'runtime-regression':
        proof = json.loads(Path(args.proof).read_text()) if args.proof else {}
        if (proof.get('status') != 'passed' or
                proof.get('scope') != 'actual worker restart, owned route fault, and actual trade monthly snapshot' or
                proof.get('cleanup') != 'exact owned live fixtures removed; private schema dropped; protected order and product unchanged' or
                proof.get('restart', {}).get('status') != 'completed' or
                proof.get('fault', {}).get('status') != 'compensated' or
                proof.get('fault', {}).get('attempts') != 3 or
                proof.get('monthly', {}).get('isolated_schema_removed') is not True or
                proof.get('worker_batch_restored') != 10):
            raise ValueError('Reviewed restart, fault, monthly and cleanup evidence required')
    if args.phase in {'api-runtime', 'saas-runtime', 'financial-reviewed'}:
        proof = json.loads(Path(args.proof).read_text()) if args.proof else {}
        expected = {'api-runtime': 'API/model-API migration runtime only',
                    'saas-runtime': 'market-mock-saas upgrade/downgrade worker',
                    'financial-reviewed': 'isolated PostgreSQL financial HTTP'}[args.phase]
        if proof.get('status') != 'passed' or proof.get('scope') != expected:
            raise ValueError('Matching bounded acceptance proof required')
        cleanup = proof.get('cleanup', '')
        if (args.phase == 'api-runtime' and cleanup != 'exact owned fixtures and journaled external identities removed') or (
                args.phase == 'financial-reviewed' and cleanup != 'isolated schema removed; protected public order and product unchanged') or (
                args.phase == 'saas-runtime' and cleanup != 'fixture database rows removed; exact tenant closed; exact mock OIDC client removed'):
            raise ValueError('Matching precise cleanup proof required')
    if args.phase == 'verified':
        proof = json.loads(Path(args.proof).read_text()) if args.proof else {}
        if proof.get('status') != 'passed' or proof.get('cleanup') != 'exact owned fixtures removed':
            raise ValueError('Actual runtime acceptance and precise cleanup evidence required')
    api_downgrade = (args.phase == 'api-runtime' and
                    set(proof.get('migrations', {})) == {'api', 'model'} and
                    all(item.get('downgrade_status') == 'completed' and
                        item.get('downgrade_refund_status') == 'completed'
                        for item in proof['migrations'].values()))
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from sqlalchemy import select
    phases = {
        'start': {
            'TRD-BE-017': (10, '开始持久化差价资金、资源版本切换与失败补偿；沿用模拟资金。'),
            'TRD-BE-018': (30, '开始连续变更有效期限修订和已入账正负调整的月度清算接入。'),
            'TRD-FE-009': (72, '开始订单变更提交、差价支付和阶段进度界面。'),
            'TRD-OPS-002': (45, '开始持久化变更Worker、租约和重启恢复。'),
            'TRD-QA-005': (5, '开始三类资源当前/未来段变更及正负清算故障验证。'),
        },
        'implemented': {
            'TRD-BE-017': (65, '代码集成中；待实际资源、模拟资金和故障补偿验证。'),
            'TRD-BE-018': (65, '有效修订及月度调整代码集成中；待月度重复、重算与报表验证。'),
            'TRD-FE-009': (85, '提交及资金进度界面集成中；待浏览器端到端验证。'),
            'TRD-OPS-002': (65, '变更Worker集成中；待K8S和持久化恢复验证。'),
            'TRD-QA-005': (40, '自动测试执行中；实际外部资源及完整故障覆盖未关闭。'),
        },
        'api-runtime': {
            'TRD-BE-017': (85, 'API/模型API实际网关版本切换、共享凭据和计数连续性通过；' +
                           ('正常降级及退款通过；' if api_downgrade else '降级待验证；') +
                           'SaaS及其它故障矩阵继续验证。'),
            'TRD-OPS-002': (75, 'K8S持久化变更Worker实际处理通过；重启及完整故障恢复继续验证。'),
            'TRD-QA-005': (65, 'API/模型API真实资源联调通过；' +
                           ('正常降级及退款通过；' if api_downgrade else '降级待验证；') +
                           'SaaS、未来续费及浏览器矩阵未据此关闭。'),
        },
        'financial-reviewed': {
            'TRD-BE-018': (90, '独立PG财务HTTP验证：正负金额、差价退款排重、批次重算锁定、五方报表；完整交易链清算继续验证。'),
        },
        'saas-runtime': {
            'TRD-BE-017': (90, '模拟SaaS实际Worker升级、降级退款、当前及未来续费段变更通过；完整故障矩阵继续验证。'),
            'TRD-OPS-002': (80, '三类资源实际Worker处理通过；实际重启和故障恢复覆盖继续验证。'),
            'TRD-QA-005': (75, 'API/模型API及模拟SaaS真实组件联调通过；浏览器、故障恢复及交易到清算完整矩阵未关闭。'),
        },
        'runtime-regression': {
            'TRD-BE-017': (95, '实际Worker重启、目标路由故障三次重试、原资源恢复与模拟差价资金补偿通过；其它资源故障断点未据此关闭。'),
            'TRD-BE-018': (95, '本轮真实API/模型API交易及8条已入账调整在隔离PG中完成月度清算、五方守恒、排重、重建及锁定验证；跨月矩阵仍待验证。'),
            'TRD-OPS-002': (90, '实际Worker持久化计划后重启及故障恢复通过，临时batch配置已恢复；生产高可用及全部断点仍待验证。'),
            'TRD-QA-005': (85, '重启、真实路由故障补偿和交易到月度清算通过；浏览器与跨月完整矩阵继续验证。'),
        },
        'browser-regression': {
            'TRD-FE-009': (95, '真实Chrome桌面升级模拟支付、移动端降级及退款完成通过；修复报价弹框提交入口。API预置原订单及QA令牌不计密码登录/浏览器首购覆盖。'),
            'TRD-QA-005': (90, '同一浏览器变更订单的4条真实调整进入隔离月度清算通过，精确清理完成；跨月、SaaS/模型浏览器及全部故障矩阵继续验证。'),
        },
        'verified': {
            'TRD-BE-017': (95, '本批实际资源和模拟资金验收通过；真实资金渠道仍未接入。'),
            'TRD-BE-018': (95, '本批正负调整纳入月度清算验证通过；完整覆盖以验收矩阵为准。'),
            'TRD-OPS-002': (85, '本批Worker部署和恢复验证通过；生产高可用及全部资源恢复覆盖仍保留。'),
            'TRD-QA-005': (85, '本批实际资源和月度清算验收通过；其它故障矩阵未据此标完成。'),
        },
    }
    with m.SessionLocal() as db:
        actor = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if not actor:
            raise ValueError('Audit actor unavailable')
        for code, (progress, note) in phases[args.phase].items():
            task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code))
            if not task or task.status in {'done', 'review'}:
                raise ValueError('Unexpected task state: ' + code)
            m.update_development_task(code, m.DevelopmentTaskUpdate(
                status='in_progress', progress=max(progress, task.progress),
                note=args.runtime_tag + '; ' + note), actor, db)
        snapshot = m.development_tasks(actor, db)
    print(json.dumps({'counts': snapshot['counts'], 'tasks': list(phases[args.phase])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
