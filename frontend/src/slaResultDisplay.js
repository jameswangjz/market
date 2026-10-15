const states = Object.freeze({
  met: { label: "达标", className: "status-done", title: "SLA 达标详情",
    reason: "已记录达标结论，请结合样本与评估口径核对。", advice: "请结合评估周期、样本及指标口径核对结果。" },
  warning: { label: "预警", className: "status-review", title: "SLA 预警详情",
    reason: "存在预警，请核对指标及评估依据。", advice: "建议持续观察指标，核对容量、延迟及交付风险。" },
  breached: { label: "违约", className: "status-blocked", title: "SLA 违约详情",
    reason: "已记录违约结论，请核对异常依据。", advice: "请定位异常原因，并记录处置及恢复过程。" },
  insufficient_data: { label: "数据不足", className: "status-todo", title: "SLA 数据不足详情",
    reason: "证据不足，当前无法判定是否达标。", advice: "请补齐数据源、规则或有效样本后重新评估，当前不作达标或违约判断。" },
  unknown: { label: "状态未知", className: "status-todo", title: "SLA 未知状态详情",
    reason: "评估状态未知，当前无法判定是否达标。", advice: "请核对评估状态及数据来源，确认前不作达标或违约判断。" },
});

const reasons = Object.freeze({ source_missing: "数据源缺失，无法完成评估。", missing_snapshot: "缺少订单冻结规则，无法完成评估。",
  invalid_snapshot: "订单冻结规则无效，无法完成评估。", sample_count: "有效样本不足，无法完成评估。",
  probe_coverage: "探针覆盖率不足，无法完成评估。", duration_completeness: "耗时数据完整度不足，无法完成评估。",
  upstream_insufficient: "上游评估证据不足，无法完成评估。", invalid_measurement: "观测数据无效，无法完成评估。" });

export function slaResultState(status) {
  return states[Object.hasOwn(states, status) ? status : "unknown"];
}

export function slaMetric(value, unit = "") {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? `${value}${unit}` : "—";
}

export function slaResultReason(result) {
  if (typeof result?.breach_reason === "string" && result.breach_reason.trim()) return result.breach_reason.trim();
  if (Object.hasOwn(reasons, result?.reason)) return reasons[result.reason];
  return slaResultState(result?.status).reason;
}
