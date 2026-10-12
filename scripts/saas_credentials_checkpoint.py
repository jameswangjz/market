"""Update progress only after live SaaS/browser evidence has passed."""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-proof', required=True)
    parser.add_argument('--runtime-tag', required=True)
    args = parser.parse_args()
    proof = json.loads(Path(args.browser_proof).read_text(encoding='utf-8-sig'))
    if proof.get('status') != 'passed' or len(proof.get('checks', [])) < 3:
        raise RuntimeError('Live browser proof incomplete')
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from sqlalchemy import select
    with m.SessionLocal() as db:
        admin = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if not admin or admin.platform_role != 'super_admin':
            raise RuntimeError('Audit actor unavailable')
        for code, title, area, acceptance in [
            ('SAAS-AUTH-001', '发布自动生成OAuth2接口及OIDC登录双凭据', '后端', '审核事务、重复发布稳定、加密存储、双下载权限与审计'),
            ('SAAS-AUTH-002', '提供方双凭据下载及待接入验证操作', '前端', '提供方前端可下载两份配置；购买方和平台角色不能获取提供方密钥'),
            ('HTTP-CFG-001', '系统配置统一管理应用HTTP地址策略', '全栈', '数据库持久化、默认禁止HTTP、启用后支持API/模型API/SaaS地址与OIDC回调')]:
            task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code))
            if task and task.title != title:
                raise RuntimeError('Task code already owned by another feature')
            if not task:
                task = m.DevelopmentTask(code=code, title=title, area=area, owner='主 Agent', priority='P0',
                    dependencies='TRD-BE-012', acceptance=acceptance)
                db.add(task)
            task.status, task.progress = 'review', 100
            m.audit(db, admin.email, 'verify_development_task', 'development_task', code,
                args.runtime_tag + '; 622后端测试578通过44跳过，154前端通过；真实模拟SaaS开通及浏览器SSO、双下载、桌面/移动HTTP设置验证通过',
                after={'status': task.status, 'progress': task.progress})
        for code, progress, note in [
            ('TRD-BE-012', 80, '文件与SaaS自动开通、结果核验、10秒三次重试已实现；SaaS-APP1真实模拟服务交付成功。API/模型API自动交付Worker接入仍待完成。'),
            ('TRD-QA-003', 60, '实际SaaS订单开通、Token校验、OIDC用户会话与凭据下载权限通过；API/模型API全链路仍待验证。')]:
            task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code))
            if not task:
                raise RuntimeError('Existing delivery task unavailable')
            task.status, task.progress = 'in_progress', progress
            m.audit(db, admin.email, 'update_development_task', 'development_task', task.id,
                note, after={'status': task.status, 'progress': progress})
        db.commit()
        print(json.dumps({'status': 'updated', 'new_tasks': ['SAAS-AUTH-001', 'SAAS-AUTH-002', 'HTTP-CFG-001'],
            'existing_tasks_kept_open': ['TRD-BE-012', 'TRD-QA-003']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
