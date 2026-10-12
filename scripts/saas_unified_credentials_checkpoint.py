"""Record verified unified downloads without closing unrelated delivery tasks."""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-proof', required=True)
    parser.add_argument('--runtime-proof', required=True)
    parser.add_argument('--runtime-tag', required=True)
    args = parser.parse_args()
    browser = json.loads(Path(args.browser_proof).read_text(encoding='utf-8-sig'))
    runtime = json.loads(Path(args.runtime_proof).read_text(encoding='utf-8-sig'))
    if (browser.get('status') != 'passed' or len(browser.get('checks', [])) < 3
            or runtime.get('status') != 'passed'
            or not runtime.get('provider_unified_download')
            or not runtime.get('existing_secrets_preserved')):
        raise RuntimeError('Unified configuration verification incomplete')
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from sqlalchemy import select
    with m.SessionLocal() as db:
        admin = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == 'SAAS-AUTH-002'))
        if not admin or admin.platform_role != 'super_admin' or not task:
            raise RuntimeError('Existing task or audit actor unavailable')
        before = {'title': task.title, 'status': task.status, 'progress': task.progress,
            'acceptance': task.acceptance}
        task.title = '提供方统一SaaS接入配置下载及待接入验证操作'
        task.acceptance = '提供方企业超级管理员、管理员可下载一份JSON；OAuth2/OIDC独立分区；重复下载不轮换；买方/普通成员/平台角色无权限；实际浏览器SSO回归通过'
        task.status, task.progress = 'review', 100
        m.audit(db, admin.email, 'verify_development_task', 'development_task', task.id,
            args.runtime_tag + '; 统一接入配置下载、既有凭据稳定、真实SaaS浏览器SSO回归已验证；关联交付任务不变',
            before=before, after={'title': task.title, 'status': task.status,
                'progress': task.progress, 'acceptance': task.acceptance})
        db.commit()
        print(json.dumps({'status': 'updated', 'task': task.code, 'progress': task.progress,
            'task_status': task.status}, ensure_ascii=False))


if __name__ == '__main__':
    main()
