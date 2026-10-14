"""Align active task acceptance and status with current requirements/evidence."""
import argparse
import json
import os
from pathlib import Path


REVIEW = {'TRD-BE-017', 'TRD-BE-018', 'TRD-FE-005', 'TRD-FE-009', 'TRD-QA-005'}
SPEC = {
    'TRD-BE-011': ('企业+API产品共享凭据；同版本续费只延长期限不叠加额度；API/模型API月价成本乘月数，千次转换整数，0不限；生效日订阅月与北京时间自然日，分钟限流保留；升级降级保留用量，Redis原子计数及旧计数迁移不得放大授权。', '实际APISIX专项33个检查及隔离Redis测试通过；旧计数保守迁移已验证，真实历史业务迁移及完整迁移核对仍未执行。'),
    'TRD-BE-012': ('文件真实扫描通过且授权下载可用；API/模型API路由、授权及健康验证成功；SaaS租户、管理员及访问入口均验证成功后自动履约完成，无买方验收；文件/API仅买方super/admin访问；失败间隔10秒三次后转人工，凭据不入URL。', '真实SaaS交付及浏览器SSO通过；仍需归并文件、API、模型API真实扫描授权及初始交付失败转人工的完整证据，不将SaaS成功替代四类交付。'),
    'TRD-BE-017': ('API/模型API/SaaS当前剩余段和未来已付段共同升降级；升级到账且资源变更验证成功才生效；降级撤回高权限与净差退款关联；同组合并发锁、幂等、零差价、三次10秒重试及失败补偿/人工恢复，不重复退款。真实银行另列。', '真实Worker重启及故障补偿、实际APISIX/Redis/模拟SaaS资源切换通过；真实PG并发与远端写后SIGKILL、超时前后及Token拒绝专项通过。完整守护进程故障排列由FLT-OPS-002继续验证，真实银行由FLT-QA-002另列。'),
    'TRD-BE-018': ('原版本未用收入成本负向冲回、目标版本剩余收入成本正向计入，当前及未来段逐段计算；负向原规则、正向目标规则快照；用户仅净支付/退款，不重复按全额订单清算；同价成本调整、完成月份归属、五方金额守恒及幂等。', '实际交易月度快照、跨月私有PG清算、四条/八条经济调整、净资金排除、成本规则快照、报表导出、重复生成及确认后不可变均有通过证据。'),
    'TRD-FE-005': ('四级审核不变，API/模型API/SaaS进入待接入验证，健康成功后可售；提供方企业super/admin可查看接入状态和失败原因、下载规范；SaaS一个文件区分OAuth2/OIDC凭据；不混入买方调用凭据；HTTP由系统配置允许；持续3次异常不影响新购。', '接入状态前端测试、私有PG发布验证、实际健康任务及企业权限通过；真实Chrome提供方合并配置下载和买方/平台下载拒绝通过，桌面手机HTTP配置通过。'),
    'TRD-FE-009': ('展示组合期限、当前及未来段、配额和逐段净差；同版本续费只延长期限；API/模型API统一最新调用方式，无旧调用方兼容承诺；SaaS切换提示；支付退款及资源进度可查，买方所有响应和页面无成本。', '实际Chrome桌面API升级、手机降级与退款/月度调整通过；前端最新API调用方式规范化、SaaS提示、报价敏感字段过滤测试通过。SaaS/模型全部浏览器矩阵由QA任务继续验证。'),
    'TRD-OPS-002': ('market部署及YYYYMMDDHHMMSS-GitSha标签；Outbox租约、超时及Worker重启恢复，健康/失败/配额/告警可查；产物溯源门禁；新旧应用读取兼容及发布回退须实测，不停止共享服务做灾难演练。', '实际发布、Worker重启/租约恢复及备份双恢复通过；旧新应用读取兼容hook未执行，完整灰度回退尚缺证据。'),
    'TRD-QA-003': ('四类线上产品审核接入验证发布、模拟支付自动交付；文件真实病毒/Presidio扫描及订单下载次数，API/模型API企业共享凭据且同版本续费不叠加；SaaS自动开通；健康/授权失败不得完成、三次10秒重试转人工，买方角色限制。', 'SaaS实际链路和部分API交易通过；四类登记到交付及文件真实扫描报告/次数限制的统一端到端证据未齐，保持进行中。'),
    'TRD-QA-005': ('当前/未来段升级降级同价；实际PG并发幂等、远端写入后恢复；旧规则成本收入冲回、目标规则重新计入及跨月归属；模拟支付退款与资源故障补偿，无重复分账/双退；真实渠道及全部守护进程故障时序另列FLT任务。', '实际PG并发/SIGKILL及月度跨月清算，API/模型/SaaS资源认证/状态/超时66项专项及后续401修复实链验证通过；完整资金资源守护进程矩阵及真实银行明确不在已通过范围。'),
    'TRD-ARC-003': ('按最新需求逐任务归档代码、测试、部署、验收入口及限制，统计来自实时控制台；开发验证通过为review，用户确认才done；真实支付、真实供应商、多节点灾备另列；不得以模拟证据冒充。', '已复核15项并统一最新验收条件；尚需最终集成、未完成任务证据及用户验收入口完整归档，不宣称整批发布门禁全部通过。'),
    'TRD-R2-BE-008': ('当前企业及提供方/消费方隔离订单、履约、清算、审计、消息、附件和SSE；买方无成本/提供方Secret；企业提供方可看本企业清算、提案由对方确认，锁定后不可调整；不授企业全局付款权限。', '47项真实私有PG隔离测试及部署HTTP、浏览器读取通过；消息附件所有者预览修复尚未部署并完成部署回归，暂不能整体待验收。'),
    'TRD-R2-OPS-001': ('健康检查每300秒，market命名空间、Forbid、超时终止和持久记录；连续3次异常不暂停新购；源码产物溯源、时间Git标签、备份恢复及健康任务更新核对。', '实际健康任务每5分钟/Forbid/240秒及备份双恢复通过；当前健康任务仍用旧镜像，需更新与核对，读取回退兼容hook未执行，保持进行中。'),
    'TRD-R2-QA-002': ('桌面手机真实浏览器登记审核待接入发布商城购买支付；SaaS订单双树用户部门选中/全部同步与移除、成员权限；连续3次健康异常及恢复不影响新购；支付状态即时刷新。', '实际ChromeSSO、凭据下载、双树/目录/消息/清算展示及15项HTTP通过；真实批量同步已有后端证据，但浏览器点击批量动作和全交付类别完整流程仍未齐。'),
    'TRD-R2-ARC-002': ('按最新需求逐任务保存代码测试部署证据及前端验收步骤，明确部分完成与外部条件；未开发或未验证不得标待验收；用户验收后才done。', '已增加SSO与部署回归文档和15项状态复核；仍需最终部署修复及未完成任务验收证据汇总。'),
    'FLT-OPS-002': ('专属测试对象Worker选择、原子隔离登记及共享Worker排除、鉴权故障代理；覆盖API/模型/SaaS资源prepare/apply/proof/restore及资金阶段的守护进程故障时序；保护用户订单，不全局清空或停止共享服务。', '精确对象选择和受保护清单、循环地址鉴权故障代理16项通过；原子隔离登记/共享Worker自动排除和全守护进程故障矩阵仍未实现或验证。'),
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
            raise RuntimeError('Audit actor unavailable')
        changed = []
        for code, (acceptance, note) in SPEC.items():
            task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code).with_for_update())
            if task is None:
                raise RuntimeError('Missing task: ' + code)
            if task.status == 'done':
                continue
            before = {'status': task.status, 'progress': task.progress, 'acceptance': task.acceptance}
            if task.status == 'in_progress' and code in REVIEW:
                task.status, task.progress = 'review', 100
            task.acceptance = acceptance + '\n20261014验证边界：' + note
            after = {'status': task.status, 'progress': task.progress, 'acceptance': task.acceptance}
            if before != after:
                m.audit(db, actor.email, 'reconcile_latest_task_status', 'development_task',
                        target_id=task.id, detail=code + '；' + note,
                        before=before, after=after)
                changed.append({'code': code, 'before': before, 'after': after})
            m.audit(db, actor.email, 'update_development_task', 'development_task',
                    task.code, '20261014最新需求与状态复核；' + note)
        db.commit()
        snapshot = m.development_tasks(actor, db)
    args.output.write_text(json.dumps({'changes': changed, 'snapshot': snapshot,
        'evidence': {code: note for code, (_, note) in SPEC.items()}},
        ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'changed': len(changed), 'counts': snapshot['counts']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
