const actions = Object.freeze({
  create_product: '登记产品', update_product: '更新产品', submit_product_review: '提交产品审核', withdraw_product_review: '撤回产品审核',
  business_approve_product: '业务审核通过', business_reject_product: '业务审核拒绝', quality_approve_product: '质量审核通过', quality_reject_product: '质量审核拒绝', operation_approve_product: '运营审核通过', operation_reject_product: '运营审核拒绝',
  approve_product_security: '安全审核通过', reject_product_security: '安全审核拒绝', version_integration_pending: '版本接入待验证',
  unpublish_product: '下架产品', auto_unpublish_product: '安全策略自动下架产品', run_product_security_scan: '执行产品安全扫描', run_product_security_check: '执行产品安全检查', run_product_security_policy_scan: '执行安全策略扫描',
  create_order: '创建订单', approve_order: '审批订单', reject_order: '拒绝订单', start_payment: '发起支付', confirm_payment: '确认支付', cancel_order: '取消订单', start_delivery: '开始交付', confirm_delivery: '确认交付', reject_delivery: '拒绝交付', complete_order: '完成订单',
  create_delivery_task: '创建交付任务', delivery_succeeded: '交付成功', delivery_failed: '交付失败', upload_delivery_attachment: '上传交付附件', download_delivery_attachment: '下载交付附件', reject_infected_delivery_attachment: '拒绝风险交付附件',
  generate_settlement: '生成清算单', adjust_settlement: '调整清算单', lock_settlement: '锁定清算单', create_settlement_adjustment_proposal: '提出清算调整', decide_settlement_adjustment_proposal: '审核清算调整', create_refund_negative_settlement: '生成退款负向清算单',
  generate_settlement_batch: '生成批次清算单', create_settlement_batch: '创建清算批次', confirm_settlement_batch: '确认清算批次', rollback_settlement_batch: '回滚清算批次', pay_settlement_batch: '清算批次付款', reconcile_settlement_batch: '清算批次对账', settlement_batch_ready_for_confirmation: '清算批次待确认', supersede_settlement_batch: '替换清算批次', supersede_legacy_settlement_batch: '替换历史清算批次', skip_finalized_settlement_order: '跳过已完成清算订单', generate_refund_settlement_line: '生成退款清算明细',
  create_settlement_rule: '创建清算规则', approve_settlement_rule: '审批清算规则', reject_settlement_rule: '拒绝清算规则', activate_settlement_rule: '启用清算规则', simulate_settlement_rule: '试算清算规则', close_reconciliation_difference: '关闭对账差异', create_settlement_correction: '创建清算更正', approve_settlement_correction: '批准清算更正', reject_settlement_correction: '拒绝清算更正', export_settlement_report: '导出清算报表',
  create_service_level: '创建服务级别', update_service_level: '更新服务级别', assign_service_level: '绑定企业级别', expire_service_level_assignment: '企业级别到期恢复标准', update_sla_matrix: '更新 SLA 指标矩阵', create_sla_profile: '创建历史 SLA 规则', update_sla_profile: '更新历史 SLA 规则',
  activate_email: '激活邮箱', update_profile_contact: '更新联系方式', submit_personal_verification: '提交个人实名', resubmit_personal_verification: '重新提交个人实名', review_personal_verification: '审核个人实名', submit_enterprise_verification: '提交企业实名', review_enterprise_verification: '审核企业实名',
  invite_enterprise_member: '邀请企业成员', resend_enterprise_invitation: '重发企业邀请', accept_enterprise_invitation: '接受企业邀请', reject_enterprise_invitation: '拒绝企业邀请', update_enterprise_member_role: '修改成员角色', disable_enterprise_member: '禁用企业成员', enable_enterprise_member: '解禁企业成员', delete_enterprise_member: '删除企业成员', transfer_enterprise_super_admin: '转移企业超级管理员', create_enterprise_department: '创建企业部门', update_enterprise_department: '更新企业部门', delete_enterprise_department: '删除企业部门', update_enterprise_member_department: '更新成员部门', retire_business_review_account: '停用历史审核账号',
  update_notification_settings: '更新通知设置', grant_saas_access: '授权 SaaS 访问', create_saas_version: '创建 SaaS 版本', verify_saas_integration: '验证 SaaS 接入', revoke_oauth_token: '撤销 OAuth 令牌', ensure_platform_oauth_client: '配置平台 OAuth 客户端', download_oauth_credentials: '下载 OAuth 凭据',
  publish_apisix_route: '发布网关路由', publish_apisix_route_failed: '网关路由发布失败', unpublish_apisix_route_failed: '网关路由下线失败', auto_publish_gateway_route: '自动发布网关路由', auto_publish_gateway_route_failed: '网关路由自动发布失败', rollback_gateway_route: '回滚网关路由', create_api_credential: '创建 API 凭据', create_shared_api_credential: '创建企业共享 API 凭据', revoke_shared_api_credential: '撤销共享 API 凭据', regenerate_shared_api_credential: '重建共享 API 凭据',
  upload_file: '上传文件', download_file: '下载文件', download_file_denied: '文件下载被拒绝', reject_infected_file: '拒绝风险文件', generate_logo_thumbnail_failed: '标志缩略图生成失败', export_audit_logs: '导出审计日志', view_audit_timeline: '查看审计时间线', update_development_task: '更新开发任务',
  after_sales_create: '创建售后工单', after_sales_respond: '响应售后工单', after_sales_request_info: '要求补充售后材料', after_sales_supplement: '补充售后材料', after_sales_submit_resolution: '提交售后方案', after_sales_confirm: '确认售后解决', after_sales_reject_resolution: '拒绝售后方案', after_sales_withdraw: '撤回售后工单', after_sales_request_intervention: '申请平台介入', after_sales_respond_intervention: '响应平台介入', after_sales_review_intervention: '完成平台介入审查', after_sales_force_close: '强制关闭售后工单',
  ordinary_refund_request: '申请退款', ordinary_refund_approve: '批准退款', ordinary_refund_reject: '拒绝退款', ordinary_refund_manual_assessment: '人工评估退款', stage_version_change_adjustment: '暂存版本变更调整', post_version_change_adjustment: '入账版本变更调整', void_version_change_adjustment: '作废版本变更调整',
  login: '用户登录', logout: '用户退出', user_login: '用户登录', user_logout: '用户退出',
  order_created: '创建订单', order_paid: '订单支付', order_cancelled: '取消订单', order_confirmed: '确认订单', order_completed: '完成订单',
  product_created: '登记产品', product_updated: '更新产品', product_published: '发布产品', product_approved: '产品审核通过', product_rejected: '产品审核拒绝',
  delivery_uploaded: '上传交付', delivery_confirmed: '确认交付',
  refund_requested: '申请退款', refund_approved: '退款审核通过', refund_rejected: '拒绝退款', refund_completed: '退款完成',
  settlement_created: '创建清算单', settlement_confirmed: '确认清算单', settlement_locked: '锁定清算单',
  service_level_created: '创建服务级别', service_level_updated: '更新服务级别', service_level_assigned: '绑定企业级别',
  sla_evaluated: '执行 SLA 考核', sla_profile_created: '创建 SLA 规则', sla_profile_updated: '更新 SLA 规则',
  after_sales_created: '创建售后工单', after_sales_responded: '响应售后工单', after_sales_completed: '完成售后工单',
});
export function auditActionLabel(event) {
  const code = typeof event === 'string' ? event : event?.action;
  const label = typeof event === 'object' && (event?.action_label || event?.action_display || event?.action_name);
  if (typeof label === 'string' && /[\u3400-\u9fff]/.test(label)) return label;
  return (Object.hasOwn(actions, code) ? actions[code] : null) || (typeof code === 'string' && /[\u3400-\u9fff]/.test(code) ? code : code ? `其他操作（${code}）` : '未记录动作');
}
