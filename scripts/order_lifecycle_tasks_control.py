"""Track approved order lifecycle work without importing live tasks or accepting review."""
import list_tasks_control as control

control.DOCUMENT_BASIS = '70 订单类型与订阅履约状态需求及设计-20261019165323.md'
control.TASKS = (
    ('ORD-LC-ARC-001', '订单类型与订阅履约状态契约', 'ARC', '',
     '最新五类new_purchase/renewal/upgrade/downgrade/refund；订单与订阅状态分离；服务起止及生效时间权威；不迁移旧任务数据'),
    ('ORD-LC-BE-001', '业务操作立即生成订单记录', 'BE', 'ORD-LC-ARC-001',
     '新购续费升降级退款操作立即生成对应类型订单；升级确认变更即建单而非等支付；幂等重放不重复建单；无支付降级订单pending_fulfillment'),
    ('ORD-LC-BE-002', '支付确认与服务重试权限', 'BE', 'ORD-LC-ARC-001',
     '购买方super_admin仅发起支付且任何购买方不能确认；仅平台super_admin/platform_operator确认，财务不能确认；POST /api/orders/{id}/service-retry仅super_admin/platform_operator/delivery_monitor；负金额及not_applicable无支付'),
    ('ORD-LC-BE-003', '订阅状态时钟与实际健康生效', 'BE', 'ORD-LC-BE-001,ORD-LC-BE-002',
     '主状态pending_fulfillment/fulfilling/completed；交付awaiting_effective/accepted/exception；未来服务到时结合实际健康激活，不以时间或计划伪造成功；重试幂等不重复延期；离线履约状态不变'),
    ('ORD-LC-BE-004', '退款负向订单与现金记录', 'BE', 'ORD-LC-BE-001,ORD-LC-BE-002',
     '退款订单负金额；refund_pending/refund_completed/refund_rejected；支付与交付not_applicable，无支付动作；真实负向现金记录可追溯且幂等；不重复生成正向清算'),
    ('ORD-LC-FE-001', '订单列表详情及支付权限展示', 'FE', 'ORD-LC-BE-001,ORD-LC-BE-002,ORD-LC-BE-003,ORD-LC-BE-004',
     '最新五类订单中文展示；订单版本及服务起止生效时间清晰；文件无限期，未计划开始待交付确定；退款显示负金额不取绝对值；退款及不适用状态中文；负金额及not_applicable无支付按钮；确认变更即刷新订单；离线标签不变'),
    ('ORD-LC-QA-001', '订单生命周期聚焦组合验收', 'QA', 'ORD-LC-BE-003,ORD-LC-BE-004,ORD-LC-FE-001',
     '覆盖最新五类立即建单、权限正反例、未来到时实际健康激活及异常重试；负向退款订单与负向现金记录无支付且无重复正向清算；列表负号和版本日期；不修改旧任务数据，不以单测替代实际链路验收'),
    ('ORD-LC-OPS-001', '最新订单生命周期部署与验收入口', 'OPS', 'ORD-LC-QA-001',
     '部署最新已验证产物并记录版本健康及验收入口；实际核验五类订单、未来健康激活、负向现金记录无支付和无重复正向清算；不迁移旧任务数据；提交待用户验收，不自动标记验收通过'),
)

if __name__ == '__main__':
    control.main()
