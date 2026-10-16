export const slaTabs = Object.freeze([
  { key: 'overview', label: '总览' },
  { key: 'levels', label: '服务级别支持权益' },
  { key: 'assignments', label: '企业级别绑定' },
  { key: 'evidence', label: '冻结承诺与指标证据' },
]);
export const identityTabs = Object.freeze([
  { key: 'users', label: '平台注册用户' },
  { key: 'enterprises', label: '平台注册企业' },
]);
export function deliveryTabs(refundsAllowed) {
  return [
    { key: 'tasks', label: '交付任务' },
    { key: 'aftersales', label: '售后工单' },
    ...(refundsAllowed ? [{ key: 'refunds', label: '退款申请' }] : []),
  ];
}
