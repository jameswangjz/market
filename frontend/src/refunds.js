import { activeEnterpriseMembership } from "./consoleBusinessView.js";
import { newRenewalRequestKey } from "./subscriptionTerms.js";

export const newRefundRequestKey = newRenewalRequestKey;
const statuses = Object.freeze({ requested: "待审核", pending_review: "待审核", approved: "已批准", rejected: "已拒绝",
  executing: "执行中", completed: "已退款", failed: "执行失败", pending: "历史待处理" });
export function refundStatus(value) { return Object.hasOwn(statuses, value) ? statuses[value] : "状态未知"; }
export function refundSource(value) { return value === "version_change" ? "版本变更" : ["ordinary", "after_sales"].includes(value) ? "普通退款" : "来源未明确"; }
function active(user) {
  return Boolean(user?.id && user.is_active !== false && user.verified_status === "verified"
    && (!user.activation_status || user.activation_status === "active"));
}
export function isRefundApprover(user) { return active(user) && ["super_admin", "platform_operator"].includes(user.platform_role); }
export function canViewRefunds(user, enterpriseId) {
  return isRefundApprover(user) || Boolean(active(user) && !user.platform_role
    && ["super_admin", "enterprise_admin"].includes(activeEnterpriseMembership(user, enterpriseId)?.role));
}
export function canRequestRefund(order, user, enterpriseId, mode = "buyer") {
  return Boolean(active(user) && !user.platform_role && enterpriseId && order?.buyer_enterprise_id === enterpriseId
    && mode !== "provider" && !["upgrade", "downgrade", "version_change_payment"].includes(order.business_type)
    && ["super_admin", "enterprise_admin"].includes(activeEnterpriseMembership(user, enterpriseId)?.role)
    && ["paid", "refunding", "refunded"].includes(order.payment_status));
}
export function refundAmount(value) {
  const text = String(value ?? "").trim();
  if (!/^(?:0|[1-9]\d{0,11})(?:\.\d{1,2})?$/.test(text)) throw new Error("金额须大于0，最多两位小数");
  const [whole, fraction = ""] = text.split(".");
  const cents = BigInt(whole) * 100n + BigInt(fraction.padEnd(2, "0"));
  if (cents <= 0n) throw new Error("退款金额必须大于0");
  return `${whole}.${fraction.padEnd(2, "0")}`;
}
export function refundReason(value) {
  const reason = String(value ?? "").trim();
  if (!reason || reason.length > 10000) throw new Error("请填写理由（最多10000字符）");
  return reason;
}
export function refundMoney(value) {
  if (value == null || value === "") return "—";
  try {
    if (String(value).trim() === "0" || String(value).trim() === "0.00") return "¥0.00";
    return `¥${refundAmount(value)}`;
  } catch { return "—"; }
}
export function refundPreviewState(preview) {
  if (preview?.available !== true) return { available: false, label: "暂不可用",
    reason: preview?.reason === "usage_basis_pending" ? "API实际扣减额度账本尚未就绪" : preview?.reason === "historical_evidence_missing" ? "历史证据缺失，不能按零用量计算；可由平台人工核定" : "尚无可信试算结果，不推断可退金额或已用量" };
  return { available: true, label: "服务端试算", reason: "以服务端冻结方案和最终校验为准" };
}
export function refundPlanning(planning) {
  const reasons = { historical_ledger_policy_pending: "历史用量或经济证据未知，自动试算禁用；待平台人工核定",
    api_usage_policy_pending: "实际扣减额度账本尚未就绪，不以SLA日志或零用量替代",
    manual_assessment_required: "历史用量或经济证据未知，自动试算禁用；需平台人工核定金额、用量及依据",
    api_charge_ledger_unavailable: "实际扣减额度账本尚未就绪，不以SLA日志或零用量替代",
    economic_plan_not_available: "可信经济方案尚未就绪" };
  if (planning?.status === "frozen") return { label: "经济方案已冻结", reason: "按已冻结版本处理，不代表资金已执行" };
  if (planning?.status === "manual_ready") return { label: "人工核定就绪 / manual", reason: "人工金额与用量依据，不是自动账本证据；资金与资源尚未执行" };
  if (planning?.status === "available") return { label: "服务端自动试算", reason: "非约束性预览，以审批时冻结校验为准；尚未执行退款" };
  return { label: "试算不可用 / unknown", reason: reasons[planning?.reason] || "缺少可信用量或经济证据，不推断可退金额" };
}
export function canAssessRefund(item, user) {
  return isRefundApprover(user) && item?.source === "ordinary" && item.status === "requested"
    && Number.isSafeInteger(item.version) && item.version > 0 && item.planning?.manual_assessment_allowed === true;
}
export function refundManualValues(values) {
  const used = String(values.usedCalls ?? "").trim();
  if (!/^\d+$/.test(used) || !Number.isSafeInteger(Number(used))) throw new Error("请明确填写安全整数范围的已扣减用量；未知不可填零");
  const refs = String(values.evidenceRefs ?? "").split(/\r?\n/).map(value => value.trim()).filter(Boolean);
  if (!refs.length || refs.length > 20 || refs.some(value => value.length > 500)) throw new Error("请填写1至20条证据引用，每行一条，最多500字符");
  return { amount: refundAmount(values.amount), refundable_limit: refundAmount(values.refundableLimit),
    used_calls: Number(used), reason: refundReason(values.basis), evidence_refs: refs };
}
export function refundAllowedActions(item, user) {
  if (!isRefundApprover(user) || item?.source !== "ordinary" || item.status !== "requested"
    || !Number.isSafeInteger(item.version) || item.version < 1 || !Array.isArray(item.allowed_actions)) return [];
  return item.allowed_actions.filter(value => ["approve", "reject"].includes(value));
}
export function refundExecutionActions(item, user) {
  if (!isRefundApprover(user) || item?.source !== "ordinary" || item.execution_available !== true
    || !Number.isSafeInteger(item.version) || item.version < 1 || !Array.isArray(item.allowed_actions)) return [];
  const action = item.status === "approved" ? "execute" : item.status === "failed" ? "retry" : null;
  return action && item.allowed_actions.includes(action) ? [action] : [];
}
const executionStates = Object.freeze({ pending: "等待处理", requested: "已提交", executing: "处理中",
  prepared: "执行已准备", resources_pending: "等待资源回收", resources_applied: "资源已回收", funds_pending: "等待资金处理",
  pending_be013: "待清算接入", pending_monthly: "待纳入月度清算", settlement_generated: "负向清算单已生成",
  in_batch: "已纳入清算批次", paid: "清算已付款", applied: "已回收",
  processing: "处理中", succeeded: "已完成", completed: "已完成", failed: "失败", unavailable: "不可用", posted: "已入账" });
export function refundExecutionStatus(value) { return Object.hasOwn(executionStates, value) ? executionStates[value] : "状态待确认"; }
export function refundExecutionFailure(value, status) {
  if (status === "completed" || status === "succeeded") return "";
  const reasons = { payment_failed: "资金处理失败", resource_failed: "资源回收失败", timeout: "处理超时，需核对结果",
    completion_failed: "退款完成确认失败，请由平台核对已完成资金腿后重试",
    reconciliation_required: "需要人工核对", source_restarted: "执行来源变化，需人工核对" };
  return Object.hasOwn(reasons, value) ? reasons[value] : value ? "执行未完成，请由平台核对后处理" : "";
}
export function refundExecutionProgress(item) {
  const nested = item?.execution;
  const value = nested && typeof nested === "object" ? nested : item?.execution_status ? {
    status: item.execution_status, error_code: item.execution_error_code,
    resource_status: item.resource_status, ledger_status: item.ledger_status,
    payment_legs: item.payment_legs, funds_mode: item.execution_mode,
  } : null;
  if (!value) return null;
  const legs = value.payment_legs ?? value.legs;
  return { status: refundExecutionStatus(value.status), resource: refundExecutionStatus(value.resource_status),
    ledger: refundExecutionStatus(value.ledger_status), failure: refundExecutionFailure(value.error_code ?? value.last_error, value.status),
    simulated: item.execution_mode === "simulated" || value.funds_mode === "simulated",
    legsAvailable: Array.isArray(legs),
    retryable: value.retryable === true, legs: Array.isArray(legs) ? legs.map((leg, index) => ({
      label: `资金腿 ${index + 1}`, amount: refundMoney(leg?.amount), status: refundExecutionStatus(leg?.status),
      failure: refundExecutionFailure(leg?.error_code ?? leg?.last_error, leg?.status),
    })) : [] };
}
export function createRefundSession(api, state, getContext, makeKey = newRefundRequestKey, timers = globalThis) {
  let epoch = 0, listSequence = 0, detailSequence = 0, disposed = false, operation;
  let pollTimer = null, pollGeneration = 0, pollIdentity = null, pollAttempts = 0;
  const contextKey = () => { const c = getContext(); return JSON.stringify([c.user?.id, c.user?.platform_role, c.enterpriseId, c.order?.id, c.params]); };
  const capture = () => ({ epoch, key: contextKey() });
  const current = snapshot => !disposed && snapshot.epoch === epoch && snapshot.key === contextKey();
  function failure(error, fallback) { return typeof error.response?.data?.detail === "string" ? error.response.data.detail : fallback; }
  async function list(page = 1, pageSize = 20) {
    const snapshot = capture(), request = ++listSequence, c = getContext();
    if (!canViewRefunds(c.user, c.enterpriseId)) return;
    state.listBusy = true; state.listError = "";
    try {
      const response = await api.get("/refunds", { params: { ...c.params, page, page_size: pageSize, ...(c.order?.id ? { order_id: c.order.id } : {}) } });
      if (current(snapshot) && request === listSequence) { state.items = response.data.items; state.total = response.data.total; }
    } catch (error) { if (current(snapshot) && request === listSequence) { state.items = []; state.total = 0; state.listError = failure(error, "退款列表读取失败"); } }
    finally { if (current(snapshot) && request === listSequence) state.listBusy = false; }
  }
  function stopExecutionRefresh() {
    pollGeneration++;
    if (pollTimer !== null) timers.clearTimeout(pollTimer);
    pollTimer = null; pollIdentity = null; pollAttempts = 0;
  }
  function observeExecution() {
    const c = getContext();
    if (disposed || state.item?.status !== "executing" || !canViewRefunds(c.user, c.enterpriseId)) {
      stopExecutionRefresh(); return;
    }
    const identity = JSON.stringify([contextKey(), state.item.id]);
    if (pollIdentity === identity) return;
    stopExecutionRefresh(); pollIdentity = identity;
    const snapshot = capture(), id = state.item.id, generation = pollGeneration;
    const valid = () => current(snapshot) && generation === pollGeneration && state.item?.id === id
      && state.item.status === "executing" && canViewRefunds(getContext().user, getContext().enterpriseId);
    function schedule() {
      if (!valid() || pollAttempts >= 40) return;
      pollTimer = timers.setTimeout(async () => {
        pollTimer = null;
        if (!valid()) return;
        pollAttempts++;
        if (!state.busy && !state.detailBusy) await detail(id, true);
        schedule();
      }, 3000);
    }
    schedule();
  }
  async function detail(id, quiet = false) {
    const snapshot = capture(), request = ++detailSequence, c = getContext(), generation = pollGeneration;
    if (!canViewRefunds(c.user, c.enterpriseId)) return;
    if (state.item?.id !== id) state.item = null;
    if (!quiet) { state.detailBusy = true; state.error = ""; }
    try {
      const response = await api.get(`/refunds/${encodeURIComponent(id)}`, { params: { ...c.params } });
      if (current(snapshot) && request === detailSequence
        && (!quiet || (generation === pollGeneration && !state.busy && state.item?.id === id))) state.item = response.data;
    } catch (error) { if (!quiet && current(snapshot) && request === detailSequence) { state.item = null; state.error = failure(error, "退款详情读取失败"); } }
    finally { if (!quiet && current(snapshot) && request === detailSequence) state.detailBusy = false; }
  }
  function closeDetail() { stopExecutionRefresh(); detailSequence++; state.item = null; state.detailBusy = false; }
  async function refreshDetail() {
    const id = state.item?.id;
    if (!id || state.busy || state.detailBusy) return;
    stopExecutionRefresh(); await detail(id); observeExecution();
  }
  async function send(kind, values) {
    if (disposed || state.busy || state.detailBusy || !["request", "review", "manual", "execute", "retry"].includes(kind)) return false;
    const snapshot = capture(), c = getContext(), selectedId = state.item?.id;
    let path, body;
    try {
      if (kind === "request") {
        if (!canRequestRefund(c.order, c.user, c.enterpriseId, c.params?.business_view)) return false;
        path = `/orders/${encodeURIComponent(c.order.id)}/refund-requests`;
        body = { amount: refundAmount(values.amount), reason: refundReason(values.reason),
          ...(values.ticketId ? { ticket_id: values.ticketId } : {}), ...(values.paymentId ? { payment_id: values.paymentId } : {}) };
      } else if (["execute", "retry"].includes(kind)) {
        if (!refundExecutionActions(state.item, c.user).includes(kind)) return false;
        path = `/refunds/${encodeURIComponent(state.item.id)}/${kind}`;
        body = { expected_version: state.item.version, reason: refundReason(values.reason) };
      } else if (kind === "manual") {
        if (!canAssessRefund(state.item, c.user)) return false;
        path = `/refunds/${encodeURIComponent(state.item.id)}/manual-assessments`;
        body = { ...refundManualValues(values), expected_version: state.item.version };
        if (body.amount !== refundAmount(state.item.amount)) throw new Error("核定金额须与本退款申请一致");
      } else {
        if (!refundAllowedActions(state.item, c.user).includes(values.decision)) return false;
        path = `/refunds/${encodeURIComponent(state.item.id)}/review`;
        body = { decision: values.decision, reason: refundReason(values.reason), expected_version: state.item.version };
      }
      const fingerprint = JSON.stringify([snapshot.key, path, body]);
      if (operation?.fingerprint !== fingerprint) operation = { fingerprint, key: makeKey() };
    } catch (error) { state.error = error.message || "请求参数无效"; return false; }
    state.busy = true; state.error = ""; state.message = "";
    try {
      const response = await api.post(path, { ...body, idempotency_key: operation.key }, { params: { ...c.params } });
      if (!current(snapshot)) return false;
      if (["execute", "retry"].includes(kind) && state.item?.id !== selectedId) return false;
      operation = null; state.item = response.data;
      if (["execute", "retry"].includes(kind)) {
        state.message = "执行请求已提交，以最新执行进展为准；不代表银行已到账。";
        const id = response.data.id;
        await list(values.page || 1, values.pageSize || 20);
        if (!current(snapshot) || state.item?.id !== id) return false;
        await detail(id);
        return current(snapshot);
      }
      state.message = kind === "manual" ? "人工核定已记录（manual），不是自动用量证据；尚未退款。" : kind === "request" ? "申请已提交，等待平台审核；尚未退款。" : `审核结果：${refundStatus(response.data.status)}；未执行资金或资源变更。`;
      return true;
    } catch (error) {
      if (!current(snapshot)) return false;
      if (error.response?.status === 409) {
        operation = null;
        if (kind !== "request" && state.item?.id) await detail(state.item.id);
        if (!current(snapshot)) return false;
        state.error = "退款记录或余额已变化，请刷新核对后重新确认；原输入已保留。";
      } else state.error = ["execute", "retry"].includes(kind)
        ? "执行请求结果待确认，请刷新核对；再次提交相同内容将复用请求标识。"
        : failure(error, "提交失败，输入已保留，可重试");
      return false;
    } finally { if (current(snapshot)) state.busy = false; }
  }
  let quoteSequence = 0;
  function clearQuote() { quoteSequence++; state.quote = null; state.quoteBusy = false; }
  async function quote(amount) {
    const c = getContext();
    if (disposed || state.busy || !canRequestRefund(c.order, c.user, c.enterpriseId, c.params?.business_view)) return;
    const snapshot = capture(), sequence = ++quoteSequence;
    state.quote = null; state.quoteBusy = true;
    try {
      const response = await api.post(`/orders/${encodeURIComponent(c.order.id)}/refund-quote`, { amount: refundAmount(amount) }, { params: { ...c.params } });
      if (current(snapshot) && sequence === quoteSequence) state.quote = response.data;
    } catch { if (current(snapshot) && sequence === quoteSequence) state.quote = { status: "unavailable", reason: "source_reconciliation_required" }; }
    finally { if (current(snapshot) && sequence === quoteSequence) state.quoteBusy = false; }
  }
  function reset() { stopExecutionRefresh(); epoch++; listSequence++; detailSequence++; operation = null; clearQuote(); Object.assign(state, { items: [], total: 0, item: null, busy: false, listBusy: false, detailBusy: false, error: "", listError: "", message: "" }); }
  return { list, detail, send, quote, clearQuote, reset, observeExecution, refreshDetail, closeDetail,
    dispose() { stopExecutionRefresh(); disposed = true; epoch++; } };
}
