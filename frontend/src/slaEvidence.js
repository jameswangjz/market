export const slaMetricNames = {
  availability: "探测可用性", request_error_rate: "请求错误率", latency_p95: "请求延迟 P95",
  delivery_timeliness: "线上交付及时率", first_response: "首次响应", resolution: "解决",
  rectification: "整改", intervention_response: "介入响应", intervention_review: "介入审查",
  notification: "异常通知", urgent_first_response: "紧急首次响应", urgent_resolution: "紧急解决",
  urgent_intervention_response: "紧急介入响应",
};

export function metricRows(result) {
  const metrics = result?.metrics;
  if (!metrics || typeof metrics !== "object" || Array.isArray(metrics)) return [];
  return Object.entries(metrics).filter(([, value]) => value && typeof value === "object").map(([key, value]) => ({
    ...value, key, label: Object.hasOwn(slaMetricNames, key) ? slaMetricNames[key] : key,
    unit: key === "latency_p95" ? " ms" : ["availability", "request_error_rate", "delivery_timeliness"].includes(key) ? "%" : "",
    sourceMissing: value.reason === "source_missing" || result.source_summary?.source_available?.[key] === false,
  }));
}

export function reviewSlaPayload(kind, decision, levelId, reason) {
  if (kind !== "enterprise" || decision !== "approve") return {};
  return { service_level_id: levelId || "", sla_reason: reason.trim() };
}
