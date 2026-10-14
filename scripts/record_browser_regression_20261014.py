"""Record bounded deployed browser evidence without completing tasks."""
import os

os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'

from sqlalchemy import select
from app import main as m


def main():
    with m.SessionLocal() as db:
        actor = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if actor is None:
            raise RuntimeError('Audit actor missing')
        notes = {
            'TRD-R2-QA-002': '20261014真实Chrome回归通过：订单OIDC授权至模拟SaaS会话、合并凭据下载、双树桌面/手机展示、目录设置、提供方清算空列表和消息中心；15项部署HTTP检查通过。未验证本轮密码登录、批量同步和清算提案，保持进行中。证据：docs/浏览器SSO与部署回归-20261014.md。',
            'TRD-FE-005': '20261014提供方真实浏览器确认一个OAuth2/OIDC合并下载入口；独立客户端及no-store通过，买方/平台管理员下载被拒绝；未代替其他验收项。',
            'TRD-BE-012': '20261014已支付SaaS-APP1交付回读及真实浏览器SSO通过：completed/accepted/active，重试0，5条接口操作成功；本轮未重新执行首购及故障重试，未自动结项。',
        }
        for code, note in notes.items():
            task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code))
            if task is None or task.status == 'done':
                continue
            m.update_development_task(code, m.DevelopmentTaskUpdate(
                status=task.status, progress=task.progress, note=note), actor, db)
        print('Browser evidence recorded; task statuses and progress preserved')


if __name__ == '__main__':
    main()
