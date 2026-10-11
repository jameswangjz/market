"""Update only tasks verified in the SaaS combination/configuration batch."""
import argparse
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from app import main as m


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('start', 'finish'))
    parser.add_argument('--output')
    parser.add_argument('--runtime-tag', default='')
    parser.add_argument('--saas-evidence')
    args = parser.parse_args()
    updates = {
        'TRD-FE-004': ('in_progress', 90, '补齐版本兼容性和SaaS OIDC回调白名单登记；已售配置冻结继续保护，等待前后端测试及部署。'),
        'TRD-BE-013': ('in_progress', 35, '开始企业+产品唯一SaaS租户绑定：新版订单仅预留未开通租户、重复购买复用；历史多个租户必须明确选择，旧接口禁止新增独立租户。'),
        'TRD-BE-011': ('in_progress', 94, '补齐历史计数迁移的扫描边界、预检、回退及Cluster风险验证；尚未以本地测试代替真实历史业务迁移。'),
    }
    evidence = None
    if args.phase == 'finish':
        if not args.saas_evidence or not args.runtime_tag:
            raise ValueError('Finish requires deployment tag and real PostgreSQL evidence')
        evidence = json.loads(Path(args.saas_evidence).read_text(encoding='utf-8'))
        assert evidence['status'] == 'passed' and evidence['database'] == 'postgresql'
        assert evidence['two_blocked_sessions_observed'] and evidence['cleanup'] == 'verified'
        updates['TRD-FE-004'] = ('done', 100, '线上线下分组、版本文件/API配置、Logo必填、千次配额和0不限、已售配置冻结、版本兼容性和SaaS回调白名单已开发测试并部署；仅登记OIDC配置，OIDC协议/SSO属于后续任务。')
        updates['TRD-BE-013'] = ('done', 100, '企业+产品唯一租户绑定已实现；首购预留未开通租户，后续订单复用，历史多租户明确选择且保留；真实PG双首购连接竞争通过、订单未自动支付或开通、关联数据清理验证通过。自动开通/续费调用由TRD-BE-012负责。')
    with m.SessionLocal() as db:
        admin = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        headers = {'Authorization': 'Bearer '+m.issue_token(admin)}
    with TestClient(m.app) as client:
        for code, (status, progress, note) in updates.items():
            response = client.patch('/api/development/tasks/'+code, headers=headers,
                json={'status': status, 'progress': progress, 'note': note})
            assert response.status_code == 200, code
        snapshot = client.get('/api/development/tasks', headers=headers).json()
    snapshot['batch_verification'] = {'runtime_image_tag': args.runtime_tag, 'saas': evidence,
        'limits': ['OIDC protocol/SSO not implemented by registration configuration',
                   'No real third-party SaaS or payment channel acceptance',
                   'Historical quota migration still not fully accepted']}
    if args.output:
        Path(args.output).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'phase': args.phase, 'counts': snapshot['counts']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
