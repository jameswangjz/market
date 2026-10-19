"""Track API provider management integration without accepting user review."""
import list_tasks_control as control

control.DOCUMENT_BASIS = '67 API提供方管理闭环需求设计与任务-20261019151632.md'
control.TASKS = (
    ('API-LC-ARC-001', 'API业务地址与管理协议边界', 'ARC', '', '移除产品级重复URL；独立管理地址；平台冻结期限；网关权威；OAuth scope隔离'),
    ('API-LC-FE-001', 'API登记管理地址及每版本业务地址', 'FE', 'API-LC-ARC-001', '无产品后端URL；管理地址独立；版本上游必填；已售版本保护'),
    ('API-LC-BE-001', '管理操作持久化OAuth及严格回执', 'BE', 'API-LC-ARC-001', '冻结请求先提交；管理scope；身份周期及状态回读；失败不假成功'),
    ('API-LC-BE-002', 'API首购续费分阶段交付', 'BE', 'API-LC-BE-001', '平台冻结订单及组合期限；重试不重复延期；回滚不丢冻结计划'),
    ('API-LC-BE-003', '版本退款与到期管理同步', 'BE', 'API-LC-BE-001', '变更及恢复独立幂等；权益调整不影响无关订单；到期仅关闭组合'),
    ('API-LC-QA-001', '模拟提供方及组合链路测试', 'QA', 'API-LC-BE-002,API-LC-BE-003', '真实协议一致；权限隔离；回执重启持久化；失败重试；不使用真实金融数据'),
    ('API-LC-DOC-001', 'API提供方单文档独立接入规范', 'ARC', 'API-LC-BE-001', '完整管理请求响应和OAuth时序；业务代理区别；对账字段；测试矩阵和限制'),
    ('API-LC-OPS-001', '部署持久化及实际链路验证', 'OPS', 'API-LC-QA-001,API-LC-DOC-001', '可信产物；模拟提供方PVC；新接口实际验证；不改旧业务端点或额度'),
)

if __name__ == '__main__':
    control.main()
