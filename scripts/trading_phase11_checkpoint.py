"""Record read-only quote progress separately from version-change execution."""
import argparse
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from app import main as m


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('start', 'finish'))
    parser.add_argument('--output')
    parser.add_argument('--runtime-tag', default='')
    parser.add_argument('--service-evidence')
    parser.add_argument('--browser-evidence')
    args = parser.parse_args()
    updates = {
        'TRD-BE-016': ('in_progress', 65, '正在验证API/模型API/SaaS版本变更报价：当前剩余段及未来已付段逐段计算、冻结价格成本、分币舍入、兼容性确认和五分钟签名报价；实际支付退款及资源变更不在本任务范围。'),
        'TRD-FE-009': ('in_progress', 60, '续费页面已实现；正在增加买方订单版本变更报价预览、刷新与校验。实际升降级执行及支付退款页面依赖后续Saga，尚未完成。'),
    }
    evidence = browser = None
    if args.phase == 'finish':
        if not all((args.service_evidence, args.browser_evidence, args.runtime_tag)):
            raise ValueError('Finish requires deployed service and browser evidence')
        evidence = json.loads(Path(args.service_evidence).read_text(encoding='utf-8-sig'))
        browser = json.loads(Path(args.browser_evidence).read_text(encoding='utf-8-sig'))
        assert re.fullmatch(r'\d{14}-[0-9a-f]{7,40}', args.runtime_tag)
        assert evidence['database'] == 'real PostgreSQL'
        assert evidence['status'] == 'passed' and evidence['cleanup'].startswith('verified')
        assert browser['status'] == 'passed' and browser['deploymentTag'] == args.runtime_tag
        updates['TRD-BE-016'] = ('done', 100, '只读版本变更报价已部署验收：API/模型API/SaaS当前及未来已付时段计算、旧收入成本冲回和目标重新计入的内部预览、签名失效/漂移及权限隔离通过真实PG验证；无资金、资源及清算落账。')
        updates['TRD-FE-009'] = ('in_progress', 70, '订单页版本变更报价预览、兼容性确认、刷新及校验已部署，桌面/移动浏览器验证通过。续费可用；实际升降级执行、支付/退款与资源切换等待BE-017/018，不能视为完整完成。')
    with m.SessionLocal() as db:
        admin = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        headers = {'Authorization': 'Bearer ' + m.issue_token(admin)}
    with_client = TestClient(m.app)
    try:
        for code, (status, progress, note) in updates.items():
            response = with_client.patch('/api/development/tasks/' + code, headers=headers,
                json={'status': status, 'progress': progress, 'note': note})
            if response.status_code != 200:
                raise RuntimeError('Task update failed: ' + code)
        snapshot = with_client.get('/api/development/tasks', headers=headers).json()
    finally:
        with_client.close()
    snapshot['batch_verification'] = {'runtime_image_tag': args.runtime_tag,
        'quote_service': evidence, 'browser': browser,
        'limits': ['Read-only quotes, no actual version switch/payment/refund/ledger posting',
                   'Synthetic active resource records do not verify APISIX or third-party SaaS provisioning']}
    if args.output:
        Path(args.output).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'phase': args.phase, 'counts': snapshot['counts']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
