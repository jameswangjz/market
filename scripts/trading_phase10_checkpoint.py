"""Track the file-only slice without marking all online delivery complete."""
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
    parser.add_argument('--service-evidence')
    parser.add_argument('--browser-evidence')
    args = parser.parse_args()
    updates = {
        'TRD-BE-012': ('in_progress', 25, '开发文件自动交付：校验订单及文件快照、扫描报告、MinIO大小和可读性，成功即履约完成；API/SaaS真实开通尚未完成。'),
        'TRD-FE-006': ('in_progress', 25, '订单第二列增加文件访问入口和买方管理员权限校验；API/模型API/SaaS访问入口继续待开发。'),
        'TRD-OPS-002': ('in_progress', 15, '开发文件交付后台worker、10秒间隔三次尝试、异常转人工及人工重试；完整API/SaaS Saga和补偿尚未完成。'),
        'TRD-QA-003': ('in_progress', 15, '开始文件线上履约验证；全类型交付端到端验收仍未完成。'),
    }
    evidence = None
    if args.phase == 'finish':
        if not all((args.service_evidence, args.browser_evidence, args.runtime_tag)):
            raise ValueError('Finish requires deployment and service/browser evidence')
        evidence = json.loads(Path(args.service_evidence).read_text(encoding='utf-8-sig'))
        browser = json.loads(Path(args.browser_evidence).read_text(encoding='utf-8-sig'))
        assert evidence['status'] == 'passed' and evidence['cleanup'] == 'verified'
        assert browser['status'] == 'passed' and browser['deploymentTag'] == args.runtime_tag
        updates['TRD-BE-012'] = ('in_progress', 35, '文件自动履约已开发部署，真实PG/MinIO与模拟支付验证通过；订单快照/扫描状态/对象可读性校验、成功即完成、失败重试及权限回收已验证。扫描报告为验收fixture，不代表重做扫描。API/SaaS自动交付尚未完成。')
        updates['TRD-FE-006'] = ('in_progress', 35, '订单第二列文件下载入口、同风格弹框、次数刷新与权限校验已部署；桌面/移动浏览器通过。API/模型API/SaaS访问入口未完成。')
        updates['TRD-OPS-002'] = ('in_progress', 25, '单副本文件worker已部署，事务锁定、心跳探针、三次尝试及人工重试可用；多副本竞争和完整API/SaaS Saga仍待验收。')
        updates['TRD-QA-003'] = ('in_progress', 25, '文件交付隔离HTTP回归及开发环境PG/MinIO、模拟支付链路和浏览器验证通过；API/模型API/SaaS完整线上交付仍未验收，10GB实压不在本批范围。')
    with m.SessionLocal() as db:
        admin = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        headers = {'Authorization': 'Bearer '+m.issue_token(admin)}
    client = TestClient(m.app)
    try:
        for code, (status, progress, note) in updates.items():
            response = client.patch('/api/development/tasks/'+code, headers=headers,
                json={'status': status, 'progress': progress, 'note': note})
            if response.status_code != 200:
                raise RuntimeError('Task update failed: '+code)
        snapshot = client.get('/api/development/tasks', headers=headers).json()
    finally:
        client.close()
    snapshot['batch_verification'] = {'runtime_image_tag': args.runtime_tag, 'file_delivery': evidence,
        'limits': ['Only file fulfillment is covered; API/SaaS remain incomplete',
                   'Scan metadata is a fixture; this batch does not re-accept scanners',
                   'No 10GB load or multi-replica contention acceptance']}
    if args.output:
        Path(args.output).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'phase': args.phase, 'counts': snapshot['counts']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
