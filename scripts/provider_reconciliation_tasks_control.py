"""Track additive provider entitlement reconciliation without accepting user review."""
import list_tasks_control as control

control.DOCUMENT_BASIS = '61 第三方订单期限对账需求设计与任务-20261019121516.md'
control.TASKS = (
    ('REC-ARC-001', '第三方订单期限与回执契约', 'ARC', '', 'UTC及左闭右开；订单与组合期限分离；未知日期不伪造；重试不改已冻结请求'),
    ('REC-BE-001', 'SaaS生命周期订单期限上下文', 'BE', 'REC-ARC-001', '开通续费升降级退款及成员部门操作；关联订单；保留旧请求幂等'),
    ('REC-BE-002', '提供方只读权益对账接口', 'BE', 'REC-ARC-001', '产品归属及角色隔离；有界分页；订单期限及变更段；不泄漏密钥成本'),
    ('REC-QA-001', '模拟SaaS回执与组合回归', 'QA', 'REC-BE-001,REC-BE-002', '冻结请求重放；真实模拟服务回执；异常上下文拒绝；跨企业隔离'),
    ('REC-DOC-001', '第三方规范与前端下载同步', 'ARC', 'REC-BE-001,REC-BE-002', 'SaaS与API提供方规范同步；查询示例；期限不等于Token有效期'),
    ('REC-OPS-001', '部署与运行只读核验', 'OPS', 'REC-QA-001,REC-DOC-001', '可信发布标签；运行健康与文档一致；不修改历史交易或凭据'),
)

if __name__ == '__main__':
    control.main()
