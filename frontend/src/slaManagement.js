export const builtInLevels = ['standard', 'enterprise', 'strategic'];
export function activeMatrixLevels(matrices) {
  return sortedMatrices(matrices).filter(item => item.status === 'active' && item.metrics && Object.keys(metricNames).every(key => item.metrics[key])).map(item => ({ ...item, id: item.service_level_id }));
}
export function matrixMetadata(matrix, metric) {
  const metadata = matrix.display_metadata || matrix.readonly_metadata || {};
  return (metadata.rows || []).find(row => row.metric_key === metric) || {};
}
export function metadataFields(row) {
  return Object.entries(row).filter(([key]) => !['metric_key', '填写目标值', '填写预警阈值'].includes(key));
}
export const metricNames = {
  first_response: '普通首次响应', urgent_first_response: '紧急首次响应', resolution: '普通问题解决', urgent_resolution: '紧急问题解决', rectification: '整改', intervention_response: '平台介入响应', urgent_intervention_response: '紧急介入响应', intervention_review: '平台介入审查', notification: '通知', delivery: '交付', availability: '可用性', request_error_rate: '请求错误率', latency_p95: 'P95 延迟', delivery_timeliness: '交付及时率',
};
export const slaTerms = { minutes: '分钟', milliseconds: '毫秒', percent: '%', upper: '不高于目标', lower: '不低于目标', provider: '提供方', platform: '平台', delivery: '交付方', support: '支持时间', natural: '自然时间', waiting_buyer_material: '等待买方材料', waiting_buyer_confirmation: '等待买方确认' };
export function sortedMatrices(items) {
  const rank = code => builtInLevels.includes(code) ? builtInLevels.indexOf(code) : 3;
  return [...items].sort((a, b) => rank(a.code) - rank(b.code) || a.code.localeCompare(b.code));
}
export function matrixPayload(matrix, draft) {
  const metrics = {};
  for (const [key, rule] of Object.entries(matrix.metrics)) {
    const edit = draft[key];
    if (!edit || edit.target === '' || edit.warning === '') throw new Error('请填写完整目标和预警值');
    const target = Number(edit.target), warning = Number(edit.warning);
    if (![target, warning].every(value => Number.isFinite(value) && (rule.unit === 'percent' ? value >= 0 && value <= 100 : value > 0))) throw new Error('目标和预警值超出有效范围');
    if (rule.unit === 'minutes' && (!Number.isSafeInteger(target) || target < 1)) throw new Error('分钟指标目标须为不小于1的整数');
    if ((rule.direction === 'upper' && warning > target) || (rule.direction === 'lower' && warning < target)) throw new Error('预警值必须在违约目标之前');
    if (target !== rule.target || warning !== rule.warning) metrics[key] = { target, warning };
  }
  if (!Object.keys(metrics).length) throw new Error('尚未修改目标或预警值');
  return { expected_version: matrix.version, metrics };
}
export function customLevelPayload(form) {
  const code = form.code.trim(), name = form.name.trim();
  if (!/^[a-z0-9_]{2,30}$/.test(code) || builtInLevels.includes(code)) throw new Error('编码须为 2 至 30 位小写字母、数字或下划线，且不能使用内置编码');
  if (name.length < 2 || name.length > 80 || !builtInLevels.includes(form.base_code)) throw new Error('请填写名称并选择内置复制来源');
  return { code, name, base_code: form.base_code, description: form.description.trim() };
}
export function bindingPayload(form) {
  const reason = form.reason.trim();
  if (!form.enterprise_id || !form.service_level_id || !reason || reason.length > 2000) throw new Error('请选择企业、级别并填写调整原因（最多2000字符）');
  const expires_at = form.expires_at ? new Date(form.expires_at) : null;
  if (expires_at && (!Number.isFinite(expires_at.getTime()) || expires_at.getTime() <= Date.now())) throw new Error('到期时间须晚于当前时间');
  return { enterprise_id: form.enterprise_id, service_level_id: form.service_level_id, reason, expires_at: expires_at?.toISOString() || null, user_id: '' };
}
