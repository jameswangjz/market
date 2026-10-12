const pendingStatuses = new Set(["pending_payment", "resource_pending", "resource_applying", "refund_pending", "retrying", "compensating", "restoring"]);
const statuses = new Set([...pendingStatuses, "exception", "completed", "compensated", "cancelled"]);
const text = value => typeof value === "string" ? value : "";
const date = value => typeof value === "string" && Number.isFinite(Date.parse(value)) ? value : null;
const amount = value => typeof value === "number" && Number.isFinite(value) ? value : null;
const moneyStatuses = { unpaid: "未支付", paying: "支付处理中", paid: "已支付", refunding: "退款处理中", refunded: "已退款", completed: "已退款", pending: "处理中", not_required: "无需处理", failed: "失败" };
export const versionChangeRefundStatusLabel = value => moneyStatuses[value] || (value ? "处理中" : "无");
export function canReadVersionChangeHistory(order, user, enterprise = null) {
  if (!order || order.snapshot_version !== 1 || !["api", "model_api", "tenant_access", "saas"].includes(order.delivery_method || order.delivery_method_snapshot)) return false;
  const activeVerifiedPlatform = ["super_admin", "finance_settlement"].includes(user?.platform_role) &&
    user?.is_active === true && user?.verified_status === "verified" &&
    (!user.activation_status || user.activation_status === "active");
  return activeVerifiedPlatform || versionChangeExecutionPermissions(user, order, null, enterprise).request;
}
export function versionChangeReachedTerminal(previousItems, nextItems) {
  const previous = new Map((previousItems || []).map(item => [item.id, item.status]));
  return (nextItems || []).some(item => pendingStatuses.has(previous.get(item.id)) && ["completed", "compensated"].includes(item.status));
}
export function versionChangeExecutionPermissions(user, order, item = null, enterprise = null) {
  const activeUser = Boolean(user && user.is_active !== false && (!user.activation_status || user.activation_status === "active"));
  const activeEnterprise = Boolean(enterprise && enterprise.id === order?.buyer_enterprise_id &&
    enterprise.verification_status === "verified" && enterprise.is_active !== false && enterprise.eligible !== false &&
    (enterprise.is_active === true || enterprise.status === "active" || enterprise.eligible === true));
  const buyer = !user?.platform_role && activeUser && user.verified_status === "verified" && order?.buyer_enterprise_id && activeEnterprise &&
    (user?.enterprise_memberships?.some(member => member.enterprise_id === order.buyer_enterprise_id && (member.status || member.membership_status) === "active") ||
      (user?.membership_status === "active" && user?.enterprise_id === order.buyer_enterprise_id));
  const buyerRoles = user?.enterprise_memberships?.filter(member => member.enterprise_id === order?.buyer_enterprise_id &&
    (member.status || member.membership_status) === "active").map(member => member.role) ||
    (user?.enterprise_id === order?.buyer_enterprise_id && user?.membership_status === "active" ? [user.enterprise_role] : []);
  const request = Boolean(buyer && ["super_admin", "enterprise_admin", "admin"].some(role => buyerRoles.includes(role)));
  const superBuyer = Boolean(buyer && buyerRoles.includes("super_admin"));
  const platformSuper = user?.platform_role === "super_admin";
  const finance = user?.platform_role === "finance_settlement";
  const activePlatformUser = Boolean(user?.platform_role && activeUser);
  const status = item?.status;
  return {
    request,
    startPayment: Boolean(superBuyer && status === "pending_payment" && ["unpaid", "paying"].includes(item?.payment_status)),
    confirmPayment: Boolean((superBuyer || activePlatformUser && (platformSuper || finance)) && status === "pending_payment" && item?.payment_status === "paying"),
    retry: Boolean(activePlatformUser && (platformSuper || finance) && ["exception", "refund_pending"].includes(status)),
  };
}

export function sanitizeVersionChangeExecution(value, order, combinationId = "", allowSameCombinationSource = false) {
  if (!value || typeof value !== "object" || typeof value.id !== "string" || !statuses.has(value.status)) {
    throw new Error("版本变更请求响应异常，请刷新重试");
  }
  const sameOrder = value.source_order_id === order?.id;
  const sameCombination = Boolean(combinationId && value.combination_id === combinationId);
  if (value.enterprise_id !== order?.buyer_enterprise_id || !value.combination_id ||
      (combinationId && value.combination_id !== combinationId) ||
      (!sameOrder && !(allowSameCombinationSource && sameCombination))) {
    throw new Error("版本变更请求响应与当前订单不匹配，请刷新重试");
  }
  return {
    id: value.id,
    status: value.status,
    current_version_code: text(value.current_version_code),
    target_version_code: text(value.target_version_code),
    payment_status: text(value.payment_status),
    refund_status: text(value.refund_status),
    payment_amount: amount(value.payment_amount),
    refund_amount: amount(value.refund_amount),
    net_difference: amount(value.net_difference),
    currency: /^[A-Z]{3}$/.test(text(value.currency)) ? value.currency : "",
    funding_order_no: text(value.funding_order_no),
    error: text(value.error || value.last_error) ? "处理异常，请联系平台管理员" : "",
    attempts: Number.isSafeInteger(value.attempts ?? value.retry_count) && (value.attempts ?? value.retry_count) >= 0 ? (value.attempts ?? value.retry_count) : 0,
    created_at: date(value.created_at),
    updated_at: date(value.updated_at),
    started_at: date(value.started_at),
    completed_at: date(value.completed_at),
  };
}

export function versionChangeExecutionError(error) {
  const status = error?.response?.status;
  if (status === 403) return "当前账号无权执行此操作";
  if (status === 409) return "请求状态已变化，请刷新后重试";
  if (status === 400) return "请求信息无效，请刷新后重试";
  return "版本变更请求暂时无法处理，请重试";
}

export function createVersionChangeExecution({ api, getContext, quoteSession, onChange = () => {}, pollMs = 4000, makeKey = () =>
  globalThis.crypto?.randomUUID?.() || `vc-${Date.now()}-${Math.random().toString(36).slice(2)}` }) {
  const empty = () => ({ open: false, items: [], loading: false, submitting: false, actionId: "", error: "", confirmItem: null });
  let state = empty(), epoch = 0, nonce = 0, disposed = false, timer = null, quoteKey = "", quoteId = "";
  const controllers = new Set();
  const requestConfig = () => { const controller = new AbortController(); controllers.add(controller); return { signal: controller.signal, done: () => controllers.delete(controller) }; };
  const emit = () => { if (!disposed) onChange({ ...state, items: [...state.items] }); };
  const contextKey = () => {
    const { order, user, enterprise, tenantId, token } = getContext();
    const { combinationId } = getContext();
    return JSON.stringify([order?.id, order?.buyer_enterprise_id, user?.id, enterprise?.id, tenantId, token, combinationId]);
  };
  const sameContext = (e, key) => !disposed && state.open && e === epoch && key === contextKey();
  const active = (e, n, key) => !disposed && state.open && e === epoch && n === nonce && key === contextKey();
  const stopPoll = () => { if (timer) clearInterval(timer); timer = null; };
  const schedulePoll = () => {
    stopPoll();
    if (state.open && state.items.some(item => pendingStatuses.has(item.status))) timer = setInterval(() => load(), pollMs);
  };
  function reset() { epoch++; nonce++; stopPoll(); for (const controller of controllers) controller.abort(); controllers.clear(); state = empty(); quoteKey = quoteId = ""; emit(); }
  async function load() {
    if (disposed || !state.open || state.loading || state.submitting || state.actionId) return;
    const { order, combinationId = "" } = getContext();
    if (!order?.id) { reset(); return; }
    const e = epoch, n = ++nonce, key = contextKey();
    state.loading = true; state.error = ""; emit();
    const request = requestConfig();
    try {
      const { data } = await api.get(`/orders/${encodeURIComponent(order.id)}/version-changes`, { signal: request.signal });
      if (!active(e, n, key)) return;
      if (!Array.isArray(data?.items)) throw new Error("invalid");
      state.items = data.items.map(item => sanitizeVersionChangeExecution(item, order, combinationId, true))
        .sort((a, b) => Date.parse(b.created_at || 0) - Date.parse(a.created_at || 0));
    } catch (error) {
      if (active(e, n, key) && !request.signal.aborted) state.error = error instanceof Error && error.message.startsWith("版本变更请求响应")
        ? error.message : versionChangeExecutionError(error);
    } finally {
      request.done();
      if (active(e, n, key)) { state.loading = false; schedulePoll(); emit(); }
    }
  }
  async function open() { if (disposed || state.open) return; reset(); state.open = true; emit(); await load(); }
  async function submit() {
    const { order, user, combinationId = "" } = getContext();
    const permission = versionChangeExecutionPermissions(user, order, null, getContext().enterprise);
    if (!permission.request || !state.open || state.submitting || state.loading) return false;
    state.submitting = true; state.error = ""; emit();
    const e = epoch, n = nonce, key = contextKey(), request = requestConfig();
    try {
      const quote = quoteSession?.state?.();
      const quoteIdNow = quote?.quote?.quote_id;
      if (!quoteIdNow || quote.quote.order_id !== order.id) throw new Error("quote");
      if (quoteId !== quoteIdNow) { quoteId = quoteIdNow; quoteKey = makeKey(); }
      const body = quoteSession.executionPayload(quoteKey);
      if (!body) throw new Error("quote");
      const { data } = await api.post(`/orders/${encodeURIComponent(order.id)}/version-changes`, body, { signal: request.signal });
      const result = sanitizeVersionChangeExecution(data?.item || data, order, combinationId || quote.quote.combination_id);
      if (!active(e, n, key)) return false;
      state.items = [result, ...state.items.filter(item => item.id !== result.id)];
      quoteKey = quoteId = "";
      state.confirmItem = null;
      return true;
    } catch (error) { if (!request.signal.aborted && active(e, n, key)) state.error = versionChangeExecutionError(error); return false; }
    finally { request.done(); if (active(e, n, key)) { state.submitting = false; emit(); schedulePoll(); } }
  }
  async function perform(item, action, reason = "") {
    if (state.actionId) return;
    const { order, user } = getContext();
    const permission = versionChangeExecutionPermissions(user, order, item, getContext().enterprise);
    if (!permission[action]) return;
    state.actionId = item.id; state.error = ""; emit();
    const e = epoch, n = nonce, key = contextKey(), request = requestConfig();
    try {
      const path = `/version-changes/${encodeURIComponent(item.id)}`;
      if (action === "startPayment") await api.post(`${path}/start-payment`, {}, { signal: request.signal });
      else if (action === "confirmPayment") await api.post(`${path}/confirm-payment`, { reason: reason.trim() }, { signal: request.signal });
      else if (action === "retry") await api.post(`${path}/retry`, {}, { signal: request.signal });
      if (!sameContext(e, key)) return;
      state.actionId = "";
      emit();
      await load();
    } catch (error) { if (!request.signal.aborted && sameContext(e, key)) state.error = versionChangeExecutionError(error); }
    finally { request.done(); if (sameContext(e, key)) { state.actionId = ""; emit(); } }
  }
  return {
    open, close: reset, load, submit,
    confirm(item) { state.confirmItem = item; emit(); },
    cancelConfirm() { state.confirmItem = null; emit(); },
    startPayment: item => perform(item, "startPayment"),
    confirmPayment: (item, reason) => perform(item, "confirmPayment", reason),
    retry: item => perform(item, "retry"),
    dispose() { reset(); disposed = true; },
  };
}
