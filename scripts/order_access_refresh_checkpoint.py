"""Record access-column verification without closing SaaS fulfillment."""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-proof', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--base-url', default='http://192.168.10.10:30080')
    args = parser.parse_args()
    proof = json.loads(Path(args.browser_proof).read_text(encoding='utf-8-sig'))
    if proof.get('status') != 'passed':
        raise RuntimeError('Browser access-column proof must pass')
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from httpx import Client
    with m.SessionLocal() as db:
        admin = db.scalar(m.select(m.User).where(m.User.email == 'admin@market.local',
            m.User.platform_role == 'super_admin', m.User.is_active.is_(True)))
        if not admin:
            raise RuntimeError('Active platform administrator unavailable')
        headers = {'Authorization': 'Bearer ' + m.issue_token(admin)}
    updates = {
        'TRD-FE-006': {'status': 'review', 'progress': 100,
            'note': '修复线上交付状态自动刷新及SaaS真实地址入口；桌面/移动真实前端+模拟HTTP验证通过。SaaS-APP1真实订单仍未开通，不把模拟UI验证当成真实SSO或开通验收。'},
        'TRD-BE-012': {'status': 'in_progress', 'progress': 60,
            'note': '文件自动交付及统一访问入口已实现。SaaS-APP1排查：支付创建tenant_access任务，但worker仅消费file；retry_count=0、订阅provisioning、无外部租户ID。仍需接入SaaS开通/续费、10秒三次重试、成功验证与异常转人工；产品OIDC也尚未配置。'},
        'TRD-QA-003': {'status': 'in_progress', 'progress': 40,
            'note': '访问列刷新和安全启动入口回归通过；607项后端测试563通过44跳过。实际SaaS租户开通、用户SSO及四类交付完整链路未完成，不计入已通过。'},
    }
    with Client(base_url=args.base_url, trust_env=False, timeout=30) as client:
        for code, body in updates.items():
            response = client.patch('/api/development/tasks/' + code, headers=headers, json=body)
            response.raise_for_status()
        response = client.get('/api/development/tasks', headers=headers)
        response.raise_for_status()
        snapshot = response.json()
    Path(args.output).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'tasks_updated': list(updates), 'counts': snapshot['counts']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
