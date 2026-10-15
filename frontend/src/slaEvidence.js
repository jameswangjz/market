export const slaMetricNames = {
  availability: "探测可用性", request_error_rate: "请求错误率", latency_p95: "请求延迟 P95",
  delivery_timeliness: "线上交付及时率", delivery_duration: "单笔交付时长", first_response: "首次响应", resolution: "解决",
  rectification: "整改", intervention_response: "介入响应", intervention_review: "介入审查",
  notification: "异常通知", urgent_first_response: "紧急首次响应", urgent_resolution: "紧急解决",
  urgent_intervention_response: "紧急介入响应",
};

export function metricRows(result) {
  const metrics = result?.metrics;
  if (!metrics || typeof metrics !== "object" || Array.isArray(metrics)) return [];
  return Object.entries(metrics).filter(([, value]) => value && typeof value === "object").map(([key, value]) => ({
    ...value, key, label: Object.hasOwn(slaMetricNames, key) ? slaMetricNames[key] : key,
    unit: key === "latency_p95" ? " ms" : key === "delivery_duration" ? " 秒" : ["availability", "request_error_rate", "delivery_timeliness"].includes(key) ? "%" : "",
    sourceMissing: !["not_applicable", "not_started"].includes(value.status)
      && (value.reason === "source_missing" || result.source_summary?.source_available?.[key] === false),
  }));
}

export function reviewSlaPayload(kind, decision, levelId, reason) {
  if (kind !== "enterprise" || decision !== "approve") return {};
  return { service_level_id: levelId || "", sla_reason: reason.trim() };
}

export const slaParties = { provider: "提供方", platform: "平台", buyer: "买方配合", caller: "调用方", unknown: "责任未明确" };
export function slaParty(value) { return Object.hasOwn(slaParties, value) ? slaParties[value] : slaParties.unknown; }
export function slaClockState(row) {
  if (row?.stale) return { label: "历史测量（待刷新）", className: "status-todo" };
  if (row?.status === "breached") return { label: row.lifecycle === "open" ? "进行中逾期"
    : row.lifecycle === "completed" ? "完成后违约" : row.lifecycle === "closed_incomplete" ? "关闭时逾期（未确认解决）" : "逾期（终态未明确）", className: "status-blocked" };
  if (row?.status === "warning") return { label: "预警", className: "status-review" };
  if (row?.status === "closed_without_confirmation" || row?.lifecycle === "closed_incomplete") return { label: "未确认关闭", className: "status-todo" };
  if (row?.status === "met" || row?.status === "within_target") return row.lifecycle === "completed"
    ? { label: "已完成且未超时", className: "status-done" } : row.lifecycle === "open" ? { label: "进行中未逾期", className: "status-in_progress" }
      : { label: "未证明正常完成", className: "status-todo" };
  if (row?.status === "running") return { label: "进行中", className: "status-in_progress" };
  if (row?.status === "insufficient_data") return { label: "数据不足", className: "status-todo" };
  if (row?.status === "not_applicable") return { label: "不适用", className: "status-todo" };
  if (row?.status === "not_started") return { label: "未开始", className: "status-todo" };
  if (row?.status === "historical_missing") return { label: "历史证据缺失", className: "status-todo" };
  return { label: "状态未知", className: "status-todo" };
}
export function slaSeconds(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? `${value} 秒` : "—";
}
export function slaMinutes(seconds) {
  return typeof seconds === "number" && Number.isFinite(seconds) && seconds >= 0 ? `${Number((seconds / 60).toFixed(6))} 分钟` : "—";
}
export function createSlaReadSession(api, state) {
  let sequence = 0, disposed = false;
  return {
    async read(path, params = {}, validate = () => true) {
      if (disposed) return;
      const current = ++sequence;
      state.busy = true; state.error = ""; state.data = null;
      try {
        const response = await api.get(path, { params: { ...params } });
        if (!disposed && current === sequence) {
          if (!validate(response.data)) { state.error = "返回证据与所选版本不一致，请重试"; return; }
          state.data = response.data;
        }
      } catch (failure) {
        if (!disposed && current === sequence) state.error = failure.response?.data?.detail || "证据读取失败，请重试";
      } finally { if (!disposed && current === sequence) state.busy = false; }
    },
    dispose() { disposed = true; sequence++; },
    reset() { sequence++; state.data = null; state.error = ""; state.busy = false; },
  };
}
export function sourcePageTotal(data) {
  const retained = Number.isSafeInteger(data?.retained_count) && data.retained_count >= 0 ? data.retained_count : 0;
  return Number.isSafeInteger(data?.total) && data.total >= 0 ? Math.min(data.total, retained) : retained;
}
export function sourceFactTotal(data) {
  for (const value of [data?.source_total, data?.observed_total]) if (Number.isSafeInteger(value) && value >= 0) return value;
  return null;
}
export function slaMetricUnit(metric, record) {
  const units = { '%': '%', percent: '%', ms: ' ms', milliseconds: ' ms', seconds: ' 秒', minutes: ' 分钟' };
  if (Object.hasOwn(units, record?.unit)) return units[record.unit];
  return metric === "latency_p95" ? " ms" : metric === "delivery_duration" ? " 秒" : "%";
}
export function slaTier(value) { return ({ standard: "标准级", enterprise: "企业级", strategic: "战略级" })[value] || value || "—"; }
const evidenceReasons = Object.freeze({
  valid_success: "有效成功请求", attributable_server_error: "可归责服务端错误", unresolved_eligibility: "统计资格未明确",
  unresolved_failure: "故障责任未明确", missing_terminal_gateway_fact: "网关终态记录缺失", caller_parameters: "调用参数问题",
  buyer_credentials: "买方凭据问题", undeclared_path: "未声明路径", contract_quota: "合同额度限制", client_cancel: "调用方取消",
  upstream_failure: "上游服务故障", timeout: "请求超时", connection_failure: "连接失败", forward_auth_failure: "转发认证失败",
  missing_published_route: "已发布路由缺失", incorrect_rate_limit: "限流配置异常", valid_input_failure: "有效输入请求失败",
  trusted_probe: "可信探测请求", trusted_isolated_test: "可信隔离测试", trusted_internal: "可信内部请求",
  contract_preapproved_maintenance: "合同预批准维护", valid_probe: "有效探测", probe_unknown: "探测状态未知",
  missing_frozen_contract: "冻结合同缺失", deadline_cohort: "按截止时间纳入周期", deadline_not_elapsed: "尚未到交付截止时间",
  first_verified_delivery: "首次确认交付", source_missing: "缺少数据来源", insufficient_samples: "有效样本不足",
  sample_count: "有效样本不足", incomplete_history: "事件历史不完整", historical_evidence_unknown: "历史证据未知",
  calendar_not_ready: "支持日历未就绪", missing_response_event: "缺少响应事件", missing_start_event: "缺少开始事件",
  missing_frozen_target: "缺少冻结目标", invalid_frozen_target: "冻结目标无效", no_persisted_clocks: "尚无已记录时钟",
});
export function slaEvidenceReason(value) { return Object.hasOwn(evidenceReasons, value) ? evidenceReasons[value] : value ? "未识别原因" : "—"; }
const sourceReasons = Object.freeze({ http_success: "HTTP响应成功", http_failure: "HTTP响应失败", probe_timeout: "探测超时",
  connection_failure: "连接失败", business_identity_source_missing: "业务探测身份缺失", business_probe_source_missing: "业务探测来源缺失",
  invalid_or_unsafe_source: "来源无效或不安全", probe_adapter_error: "探测适配器异常", security_unpublished: "安全下架" });
export function slaSourceReason(value) { return Object.hasOwn(sourceReasons, value) ? sourceReasons[value] : "实际原因未知"; }
