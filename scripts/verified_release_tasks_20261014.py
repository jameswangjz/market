"""Submit bounded, deployed verification results for frontend acceptance."""
import argparse
import json
import os
from pathlib import Path


RESULTS = {
    'TRD-QA-003': '最新8db7d99部署后复验通过：业务审核仅product_manager/平台管理员；移除独立ywsh角色、现存账号停用及密码/令牌拒绝、六角色目录实际HTTP通过，历史关联保留及迁移审计幂等/回滚专项6项通过。全后端875项861通过14跳过、前端234通过、部署15项HTTP通过。API及模型服务API、SaaS12个真实审核动作/审计/各阶段消息通过，运营后待接入、失败健康不发布、成功后购买付款交付通过，证据online-product-review-latest-role-20261014.json。既有文件ClamAV/Presidio/四审核/限次及三次10秒故障证据保留。身份根合成、登记为提供方PUT；SaaS凭据预置，不冒充新建。完整浏览器登记四审核仍由TRD-R2-QA-002进行中跟踪，不以接口证据替代。真实银行另列，精确清理、受保护业务不变。',
    'TRD-BE-012': '实链交付通过：文件原始Logo ClamAV、压缩包病毒/Presidio、四级审核、PDF报告、200/200/429下载限次与角色拒绝；API应用（含模型服务）真实APISIX路由/凭据/续费/回收；SaaS OPEN/STATUS身份回读。独立测试路由故障三次真实10秒重试后exception及人工消息，第四次自动处理409。交付处理器是生产代码，统一套件非守护进程调度证明；守护进程交付由独立浏览器任务补证。测试对象精确清理、受保护业务不变。最终0dbb045部署后15项HTTP及真实Chrome SSO通过。',
    'FLT-OPS-002': '隔离登记启用窗口竞态修复已部署20261014055932-669b980；安装镜像代码10项真实私有PG登记测试与43项守护进程/鉴权HTTP/租约SIGKILL/过期隔离测试通过，schema已删除，证据deployed-fixture-activation-final-20261014.json；另36场景210周期真实PG资源资金故障矩阵通过。资源边界为HTTP模拟，不冒充真实供应商/APISIX全故障矩阵或银行支付。原环境订单、共享服务和队列未清空或停止。',
    'TRD-R2-OPS-001': 'market最终镜像20261014054149-0dbb045部署并核对就绪；Web构建标记与Git/资产哈希一致，健康及监控CronJob更新。健康5分钟/Forbid/240秒配置、计划任务与手动任务成功验证；同周期已处理目标时手动任务0属于幂等结果。新备份两次隔离恢复、新旧镜像32项读取兼容及精确容器清理通过。合成夹具仅隔离副本，不是生产多节点灾备演练。',
    'TRD-OPS-002': '最终API/Web/消息/交付/变更Worker镜像20261014054149-0dbb045均已就绪；真实Worker重启/租约恢复既有证据、最终隔离登记43项通过。发布产物门禁、实际备份双恢复、新旧镜像32项读取兼容通过，原恢复业务指纹不变。回退验证仅隔离恢复副本应用读取，不宣称生产流量灰度、整库覆盖恢复或多节点HA；这些未纳入本次开发环境演练。',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--codes', nargs='+', choices=tuple(RESULTS))
    args = parser.parse_args()
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from sqlalchemy import select
    with m.SessionLocal() as db:
        actor = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if actor is None:
            raise RuntimeError('Audit actor missing')
        changed = []
        for code in args.codes or RESULTS:
            note = RESULTS[code]
            task = db.scalar(select(m.DevelopmentTask).where(
                m.DevelopmentTask.code == code).with_for_update())
            if task is None:
                raise RuntimeError('Missing task: ' + code)
            if task.status == 'done':
                continue
            before = {'status': task.status, 'progress': task.progress,
                      'acceptance': task.acceptance}
            acceptance = task.acceptance.split('\n20261014验证边界：')[0]
            acceptance = acceptance.replace('API/模型API', 'API应用（含模型服务）')
            acceptance += '\n20261014验证边界：' + note
            after = {'status': 'review', 'progress': 100, 'acceptance': acceptance}
            if before == after:
                continue
            m.audit(db, actor.email, 'verified_release_task_submission',
                    'development_task', target_id=task.id, detail=code + '；' + note,
                    before=before, after=after)
            m.update_development_task(code, m.DevelopmentTaskUpdate(
                status='review', progress=100, acceptance=acceptance, note=note), actor, db)
            changed.append({'code': code, 'before': before, 'after': after})
        snapshot = m.development_tasks(actor, db)
    args.output.write_text(json.dumps({'changes': changed, 'snapshot': snapshot},
        ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'changed': len(changed), 'counts': snapshot['counts']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
