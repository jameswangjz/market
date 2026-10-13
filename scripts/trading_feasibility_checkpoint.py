"""Record bounded task audit without closing unverified parent tasks."""
import argparse
import json
import os
from pathlib import Path


NOTES = {
    'TRD-BE-011': '具备原生APISIX/Redis验证条件；订阅月、自然日及旧计数迁移须联合归档。未完成项不自动结项。',
    'TRD-BE-012': '真实文件Worker、MinIO下载、三次重试转人工及修复恢复通过；扫描元数据为预置夹具，不代表本轮真实扫描验收。仍须联合四种交付证据。',
    'TRD-BE-017': '模拟资金/资源Saga可继续验证；普通退款与共享授权恢复依赖BE-019，真实银行支付后续接入再验证。',
    'TRD-BE-018': '真实PG可验证经济调整与月度清算；完整退款与共享权益证据仍需补齐，不依据95%直接结项。',
    'TRD-FE-005': '已修复产品切换异步竞争、错误版本响应及重复验证；17项定向/181项全部前端测试通过，已部署20261013054512-e731e54；浏览器桌面/390px接入失败与成功、商城版本过滤、APISIX路由证据已通过。买方凭据下载和SaaS配置下载仍未覆盖。',
    'TRD-FE-009': '具备模拟环境验证条件；完整升降级进度和普通退款、未来时段及共享权益联合验证尚未全部完成。',
    'TRD-OPS-002': '真实Worker已验证文件重试/人工恢复；整体迁移回退及交易消息隔离依赖OPS-001/BE-020，不标记完成。',
    'TRD-QA-003': '本轮真实文件Worker/MinIO/下载额度验证通过，3次失败后转人工，实际重试间隔约12秒；其余交付及完整上架扫描闭环需联合验证。',
    'TRD-QA-005': '实际模拟环境可继续资金资源故障验证；测试专属队列/故障代理及普通退款前置依赖尚未齐备。',
    'TRD-QA-001': '真实私有PG权限验收通过：匿名、多企业订单绑定、付款权限、敏感字段、文件/API凭据隔离及同Token角色降级均通过；OIDC浏览器链路因模拟SaaS客户端回调配置返回400暂未完成，需修正模拟服务公开回调配置后补测。',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proof', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    proof = json.loads(Path(args.proof).read_text(encoding='utf-8-sig'))
    worker = proof.get('file_worker', {})
    if not (worker.get('status') == 'passed' and worker.get('cleanup_verified') is True
            and worker.get('attempts') == 3 and worker.get('manual_recovery') is True
            and worker.get('download_quota_enforced') is True
            and len(worker.get('retry_intervals_seconds', [])) == 2
            and all(x >= 10 for x in worker['retry_intervals_seconds'])):
        raise ValueError('Actual cleaned worker chain proof required')
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from sqlalchemy import select
    with m.SessionLocal() as db:
        actor = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if not actor:
            raise ValueError('Task audit actor unavailable')
        for code, note in NOTES.items():
            task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code))
            if not task:
                raise ValueError('Task missing: ' + code)
            if task.status in {'done', 'review'}:
                continue
            target_progress = {'TRD-FE-005': 95, 'TRD-QA-003': 50, 'TRD-BE-012': 70, 'TRD-QA-001': 85}.get(code)
            progress = max(task.progress, target_progress) if target_progress else (max(task.progress, 10) if code == 'TRD-QA-001' else task.progress)
            m.update_development_task(code, m.DevelopmentTaskUpdate(
                status='in_progress', progress=progress,
                note='20261013可开发性复核：' + note), actor, db)
        snapshot = m.development_tasks(actor, db)
    Path(args.output).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'counts': snapshot['counts'], 'updated_scope': list(NOTES)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
