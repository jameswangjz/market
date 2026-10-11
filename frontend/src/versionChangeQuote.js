import { serverTimeNow } from "./subscriptionTerms.js";

const monthlyMethods = ["api", "model_api", "tenant_access"];
const modes = ["compatible", "breaking"];
const text = value => typeof value === "string" && value.trim().length > 0;
const date = value => text(value) && Number.isFinite(Date.parse(value));
const amount = value => typeof value === "number" && Number.isFinite(value);
const price = value => amount(value) && value >= 0;
const closeAmount = (a, b) => Math.abs(a - b) <= 0.01000001;
const invalid = () => { throw new Error("版本变更响应不符合约定，请刷新重试"); };

export function canPreviewVersionChange(order, subscription, user, buyerEnterprise, at = Date.now()) {
  at = serverTimeNow(subscription, at);
  if (!order?.id || order.snapshot_version !== 1 || !monthlyMethods.includes(order.delivery_method || order.delivery_method_snapshot) ||
      order.payment_status !== "paid" || ["cancelled", "closed", "rejected", "refunded"].includes(order.main_status) ||
      !["accepted", "pending_acceptance"].includes(order.delivery_status) ||
      (order.paid_amount != null && order.amount != null && Number(order.paid_amount) !== Number(order.amount)) ||
      Number(order.refunded_amount || 0) !== 0 ||
      !subscription?.id || subscription.available !== true || subscription.status !== "active" ||
      !text(subscription.version_id) || !text(subscription.version_code) ||
      order.product_version_id !== subscription.version_id || order.product_version_code !== subscription.version_code ||
      !Number.isSafeInteger(subscription.generation) || subscription.generation < 1 ||
      !date(subscription.expires_at) || Date.parse(subscription.expires_at) <= at ||
      subscription.enterprise_id !== order.buyer_enterprise_id ||
      (order.product_id && subscription.product_id !== order.product_id) ||
      !subscription.terms?.some(term => term.order_id === order.id && term.status === "active" &&
        term.generation === subscription.generation && date(term.starts_at) && date(term.ends_at) &&
        Date.parse(term.starts_at) < Date.parse(term.ends_at) && Date.parse(term.ends_at) > at) ||
      !user || user.platform_role || user.verified_status !== "verified" || user.is_active === false ||
      (user.activation_status && user.activation_status !== "active") ||
      !buyerEnterprise?.id || buyerEnterprise.id !== order.buyer_enterprise_id || buyerEnterprise.verification_status !== "verified" ||
      buyerEnterprise.is_active === false || (buyerEnterprise.status && buyerEnterprise.status !== "active")) return false;
  const member = Array.isArray(user.enterprise_memberships) ? user.enterprise_memberships.find(item =>
    item.enterprise_id === order.buyer_enterprise_id && (item.status || item.membership_status) === "active") : null;
  return ["super_admin", "enterprise_admin", "admin"].includes(member?.role);
}

export function sanitizeVersionChangeOptions(data, order, subscription, at = Date.now()) {
  if (data?.server_time != null && !date(data.server_time)) invalid();
  const clock = { server_time: data?.server_time || subscription.server_time || null,
    client_received_at: data?.server_time ? at : subscription.client_received_at ?? at };
  const serverAt = serverTimeNow(clock, at);
  if (data?.order_id !== order.id || data.combination_id !== subscription.id ||
      data.delivery_method !== (order.delivery_method || order.delivery_method_snapshot) ||
      data.current_version_id !== subscription.version_id || data.current_version_code !== subscription.version_code ||
      !text(data.current_version_id) || !text(data.current_version_code) ||
      !date(data.expires_at) || Date.parse(data.expires_at) <= serverAt || !Array.isArray(data.versions)) invalid();
  const ids = new Set();
  const versions = data.versions.map(version => {
    if (!text(version?.id) || ids.has(version.id) || !text(version.version_code) || !price(version.monthly_price) ||
        !modes.includes(version.compatibility_mode) || typeof version.compatibility_notes !== "string") invalid();
    ids.add(version.id);
    return { id: version.id, version_code: version.version_code, monthly_price: version.monthly_price,
      compatibility_mode: version.compatibility_mode, compatibility_notes: version.compatibility_notes };
  });
  return { order_id: data.order_id, combination_id: data.combination_id, delivery_method: data.delivery_method,
    current_version_id: data.current_version_id, current_version_code: data.current_version_code, expires_at: data.expires_at, versions, ...clock };
}

export function versionChangeQuotePayload(options, targetId, confirmed, at = Date.now()) {
  at = serverTimeNow(options, at);
  const target = options?.versions.find(version => version.id === targetId && version.id !== options.current_version_id);
  if (!target || !date(options.expires_at) || Date.parse(options.expires_at) <= at) throw new Error("请选择可用的目标版本，或刷新版本信息");
  if (typeof confirmed !== "boolean") throw new Error("请核对兼容性确认");
  if (target.compatibility_mode === "breaking" && !confirmed) throw new Error("请先确认目标版本的不兼容变更");
  return { target_version_id: target.id, compatibility_confirmed: confirmed };
}

export function sanitizeVersionChangeQuote(data, options, targetId, confirmed, at = Date.now()) {
  at = serverTimeNow(options, at);
  const target = options.versions.find(version => version.id === targetId);
  if (!target || data?.order_id !== options.order_id || data.combination_id !== options.combination_id ||
      !text(data.quote_id) || !text(data.quote_token) || data.current_version_code !== options.current_version_code ||
      data.target_version_code !== target.version_code || typeof data.currency !== "string" || !/^[A-Z]{3}$/.test(data.currency) || data.execution_ready !== false ||
      !["upgrade", "downgrade", "no_payment"].includes(data.action) ||
      !price(data.payment_amount) || !price(data.refund_amount) || !amount(data.net_difference) ||
      !date(data.created_at) || !date(data.expires_at) || Date.parse(data.expires_at) <= Date.parse(data.created_at) ||
      Date.parse(data.expires_at) > Date.parse(data.created_at) + 300000 ||
      Date.parse(data.expires_at) > Date.parse(options.expires_at) ||
      data.compatibility?.mode !== target.compatibility_mode || data.compatibility.notes !== target.compatibility_notes ||
      typeof data.compatibility.confirmation_required !== "boolean" ||
      (data.compatibility.confirmation_required && !confirmed) || !Array.isArray(data.segments) || !data.segments.length) invalid();
  if (Date.parse(data.expires_at) <= at) throw new Error("报价已过期，请重新获取");
  const ids = new Set();
  const segments = data.segments.map(segment => {
    if (!text(segment?.order_id) || ids.has(segment.order_id) || !text(segment.order_no) || !text(segment.version_code) ||
        !Number.isSafeInteger(segment.subscription_months) || segment.subscription_months < 1 ||
        !["starts_at", "ends_at", "remaining_starts_at", "remaining_ends_at"].every(key => date(segment[key])) ||
        Date.parse(segment.starts_at) >= Date.parse(segment.ends_at) ||
        Date.parse(segment.remaining_starts_at) < Date.parse(segment.starts_at) ||
        Date.parse(segment.remaining_ends_at) > Date.parse(segment.ends_at) ||
        Date.parse(segment.remaining_starts_at) >= Date.parse(segment.remaining_ends_at) ||
        typeof segment.remaining_ratio !== "string" || !/^\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(segment.remaining_ratio) ||
        !Number.isFinite(Number(segment.remaining_ratio)) || Number(segment.remaining_ratio) > 1 ||
        !["original_amount", "original_remaining_amount", "target_full_amount", "target_remaining_amount"].every(key => price(segment[key])) ||
        !amount(segment.net_difference) || !closeAmount(segment.target_remaining_amount - segment.original_remaining_amount, segment.net_difference) ||
        segment.original_remaining_amount > segment.original_amount + 0.01 ||
        segment.target_remaining_amount > segment.target_full_amount + 0.01) invalid();
    ids.add(segment.order_id);
    if (Date.parse(segment.remaining_starts_at) <= Date.parse(data.created_at) &&
        Date.parse(segment.remaining_ends_at) < Date.parse(data.expires_at)) invalid();
    return Object.fromEntries(["order_id", "order_no", "version_code", "subscription_months", "starts_at", "ends_at",
      "remaining_starts_at", "remaining_ends_at", "remaining_ratio", "original_amount", "original_remaining_amount",
      "target_full_amount", "target_remaining_amount", "net_difference"].map(key => [key, segment[key]]));
  });
  if (!closeAmount(segments.reduce((sum, segment) => sum + segment.net_difference, 0), data.net_difference) ||
      !closeAmount(data.payment_amount - data.refund_amount, data.net_difference) ||
      (data.action === "upgrade" && !(data.net_difference > 0 && data.payment_amount > 0 && data.refund_amount === 0)) ||
      (data.action === "downgrade" && !(data.net_difference < 0 && data.refund_amount > 0 && data.payment_amount === 0)) ||
      (data.action === "no_payment" && !(data.net_difference === 0 && data.payment_amount === 0 && data.refund_amount === 0))) invalid();
  // The quote token stays in the session closure, never in rendered state.
  return { order_id: data.order_id, combination_id: data.combination_id, quote_id: data.quote_id,
    created_at: data.created_at, expires_at: data.expires_at, current_version_code: data.current_version_code,
    target_version_code: data.target_version_code, action: data.action, payment_amount: data.payment_amount,
    refund_amount: data.refund_amount, net_difference: data.net_difference, currency: data.currency,
    compatibility: { mode: data.compatibility.mode, notes: data.compatibility.notes,
      confirmation_required: data.compatibility.confirmation_required }, segments, execution_ready: false };
}

export function versionChangeError(error) {
  if (error?.response?.status === 400) return "报价无效或兼容性确认未满足，请刷新后重试";
  if (error?.response?.status === 403) return "无权访问此买方企业的版本变更报价";
  if (error?.response?.status === 409) return "报价已过期或订阅已变更，请刷新后重试";
  if (error instanceof Error && ["版本变更响应不符合约定，请刷新重试", "请选择可用的目标版本，或刷新版本信息",
    "请核对兼容性确认", "请先确认目标版本的不兼容变更", "报价已过期，请重新获取"].includes(error.message)) return error.message;
  return "版本变更信息暂时无法获取，请重试";
}

export function createVersionChangeSession({ api, getContext, onChange = () => {}, now = Date.now }) {
  const empty = () => ({ open: false, options: null, targetId: "", confirmed: false, quote: null,
    loading: false, quoting: false, validating: false, validated: false, error: "" });
  let state = empty(), quoteToken = "", epoch = 0, nonce = 0, disposed = false;
  const emit = () => { if (!disposed) onChange({ ...state }); };
  const contextKey = () => JSON.stringify(getContext());
  const eligible = () => {
    const { order, subscription, user, buyerEnterprise } = getContext();
    return canPreviewVersionChange(order, subscription, user, buyerEnterprise, now());
  };
  const current = (generation, request, context) => !disposed && state.open && generation === epoch &&
    request === nonce && context === contextKey() && eligible();
  function clearQuote() { quoteToken = ""; state.quote = null; state.validated = false; }
  function reset() { epoch++; nonce++; quoteToken = ""; state = empty(); emit(); }
  async function refresh() {
    if (disposed || !state.open || !eligible()) { reset(); return; }
    const generation = epoch, request = ++nonce, context = contextKey(), { order, subscription } = getContext();
    state = { ...empty(), open: true, loading: true };
    quoteToken = "";
    emit();
    try {
      const { data } = await api.get("/orders/" + encodeURIComponent(order.id) + "/version-change-options");
      if (current(generation, request, context)) state.options = sanitizeVersionChangeOptions(data, order, subscription, now());
    } catch (error) {
      if (current(generation, request, context)) state.error = versionChangeError(error);
    } finally {
      if (current(generation, request, context)) { state.loading = false; emit(); }
    }
  }
  async function open() {
    if (disposed || state.open || !eligible()) return;
    reset();
    state.open = true;
    await refresh();
  }
  function setTarget(value) {
    if (!state.open || state.loading || state.targetId === value) return;
    nonce++;
    clearQuote();
    state.targetId = value;
    state.confirmed = false;
    state.quoting = state.validating = false;
    state.error = "";
    emit();
  }
  function setConfirmed(value) {
    if (!state.open || typeof value !== "boolean" || state.confirmed === value) return;
    nonce++;
    clearQuote();
    state.confirmed = value;
    state.quoting = state.validating = false;
    state.error = "";
    emit();
  }
  function expire() {
    if (state.quote && Date.parse(state.quote.expires_at) <= serverTimeNow(state.options, now())) {
      nonce++;
      clearQuote();
      state.quoting = state.validating = false;
      state.error = "报价已过期，请重新获取";
      emit();
    }
  }
  async function requestQuote(validate = false) {
    if (disposed || !state.open || !eligible() || state.loading || state.quoting || state.validating) return;
    expire();
    if (validate && (!state.quote || !quoteToken)) return;
    let body;
    try { body = validate ? { quote_token: quoteToken } : versionChangeQuotePayload(state.options, state.targetId, state.confirmed, now()); }
    catch (error) { clearQuote(); state.error = versionChangeError(error); emit(); return; }
    const previous = state.quote, previousToken = quoteToken;
    const generation = epoch, request = ++nonce, context = contextKey(), { order } = getContext();
    if (!validate) clearQuote();
    state.validated = false;
    state.error = "";
    state[validate ? "validating" : "quoting"] = true;
    emit();
    try {
      const path = "/orders/" + encodeURIComponent(order.id) + "/version-change-quote" + (validate ? "/validate" : "");
      const { data } = await api.post(path, body);
      if (!current(generation, request, context)) return;
      const quote = sanitizeVersionChangeQuote(data, state.options, state.targetId, state.confirmed, now());
      if (validate && (data.quote_token !== previousToken || JSON.stringify(quote) !== JSON.stringify(previous))) invalid();
      quoteToken = data.quote_token;
      state.quote = quote;
      state.validated = validate;
    } catch (error) {
      if (current(generation, request, context)) { clearQuote(); state.error = versionChangeError(error); }
    } finally {
      if (current(generation, request, context)) { state.quoting = state.validating = false; emit(); }
    }
  }
  return { open, refresh, reset, close: reset, setTarget, setConfirmed, expire,
    quote: () => requestQuote(), validate: () => requestQuote(true), dispose() { reset(); disposed = true; } };
}
