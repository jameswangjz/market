"""Record audited evidence without treating developer verification as user acceptance."""
import argparse
import json
import os
from pathlib import Path


CHECKPOINT = {
    'FLT-OPS-002': (95, '36个真实私有PG故障场景、210次子进程周期通过；真实10秒重试、SIGKILL回放、资金补偿及私有schema清理通过。资源边界为鉴权HTTP模拟，非真实供应商故障。隔离登记与共享Worker排除代码待最终部署回归。'),
    'TRD-OPS-002': (98, '实际Worker发布/重启已验证；备份两次隔离恢复、新旧镜像32项读取检查通过，原数据指纹不变且临时容器清理。用户批准的合成夹具仅在隔离恢复副本；不是生产全栈灰度/灾备演练。最终隔离代码发布待验证。'),
    'TRD-R2-OPS-001': (98, 'API/Web/Worker镜像20261014025638-9570a05已部署；健康CronJob已更新，5分钟、Forbid、240秒超时核对通过，计划任务实际处理19个目标。新旧读取回退与双恢复通过；待最终版本发布核对。'),
    'TRD-R2-QA-002': (90, '真实Chrome API/模型服务API/SaaS密码首购支付及实际Worker交付已通过并精确清理；SaaS双树17项及SSO已有浏览器证据。最新8db7d99的三类12次审核/各阶段消息/健康门禁/交付实际接口通过，ywsh移除与停用实链通过。按用户要求继续补齐浏览器登记表单和四级审核，不转手工补验；新夹具已开发，正补健康巡检提前发布防护、失败精确清理恢复、拒绝事件及消息/编辑保存证据，完整浏览器执行尚未通过。'),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from sqlalchemy import select
    with m.SessionLocal() as db:
        actor = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if actor is None:
            raise RuntimeError('Audit actor missing')
        changed = []
        for code, (progress, note) in CHECKPOINT.items():
            task = db.scalar(select(m.DevelopmentTask).where(
                m.DevelopmentTask.code == code).with_for_update())
            if task is None:
                raise RuntimeError('Task missing: ' + code)
            if task.status != 'in_progress':
                continue
            before = {'status': task.status, 'progress': task.progress,
                      'acceptance': task.acceptance}
            base = task.acceptance.split('\n20261014验证边界：')[0]
            base = base.replace('API/模型API', 'API应用（含模型服务）')
            task.acceptance = base + '\n20261014验证边界：' + note
            task.progress = max(task.progress, progress)
            after = {'status': task.status, 'progress': task.progress,
                     'acceptance': task.acceptance}
            if before != after:
                m.audit(db, actor.email, 'verified_active_task_checkpoint',
                        'development_task', target_id=task.id,
                        detail=code + '；' + note, before=before, after=after)
                changed.append({'code': code, 'before': before, 'after': after})
        db.commit()
        snapshot = m.development_tasks(actor, db)
    args.output.write_text(json.dumps({'changes': changed, 'counts': snapshot['counts']},
        ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'changed': len(changed), 'counts': snapshot['counts']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
