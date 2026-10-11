const text = value => typeof value === "string" ? value : "";
const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
const date = value => typeof value === "string" && Number.isFinite(Date.parse(value)) ? value : null;

export function monthlyQuotaThousands(calls) {
  if (!Number.isSafeInteger(calls) || calls < 0) return "";
  const whole = BigInt(calls) / 1000n;
  const remainder = String(BigInt(calls) % 1000n).padStart(3, "0").replace(/0+$/, "");
  return remainder ? `${whole}.${remainder}` : String(whole);
}

export function monthlyQuotaCalls(thousands) {
  const value = String(thousands ?? "").trim();
  if (!/^\d+(?:\.\d{0,3})?$/.test(value)) throw new Error("请输入非负配额，最多三位小数，0 表示不限");
  const [whole, fraction = ""] = value.split(".");
  const calls = BigInt(whole) * 1000n + BigInt(fraction.padEnd(3, "0"));
  if (calls > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("配额超出有效整数范围");
  return Number(calls);
}

export function serverTimeNow(source, clientNow = Date.now()) {
  const serverTime = date(source?.server_time);
  if (!serverTime || !Number.isFinite(source?.client_received_at)) return clientNow;
  return Date.parse(serverTime) + Math.max(0, clientNow - source.client_received_at);
}

export function sanitizeSubscription(data, receivedAt = Date.now()) {
  const item = data?.item;
  if (item?.available === false && !item.id) return { available: false };
  if (typeof item?.available !== "boolean" || !text(item.id) || !text(item.enterprise_id) ||
      !text(item.product_id) || !text(item.version_id) || !Array.isArray(item.terms)) {
    throw new Error("订阅信息暂时不可用");
  }
  const period = item.current_period;
  if (item.server_time != null && !date(item.server_time)) throw new Error("订阅服务器时间无效，请刷新重试");
  return {
    available: item.available, id: item.id, product_id: item.product_id, enterprise_id: item.enterprise_id,
    version_id: item.version_id, version_code: text(item.version_code), anchor_at: date(item.anchor_at),
    generation: count(item.generation), expires_at: date(item.expires_at), status: text(item.status), can_renew: item.can_renew === true,
    server_time: date(item.server_time), client_received_at: receivedAt,
    terms: item.terms.map(term => ({ order_id: text(term?.order_id), order_no: text(term?.order_no),
      version_code: text(term?.version_code), starts_at: date(term?.starts_at), ends_at: date(term?.ends_at),
      months: count(term?.months), status: text(term?.status), generation: count(term?.generation), start_month: count(term?.start_month) })),
    current_period: period ? { starts_at: date(period.starts_at), ends_at: date(period.ends_at), index: count(period.index) } : null,
    limits: item.limits ? Object.fromEntries(["rate_limit_per_minute", "daily_quota", "monthly_quota"].map(key => [key, count(item.limits[key])])) : null,
  };
}

export function subscriptionDisplayStatus(subscription, at = Date.now()) {
  if (!subscription?.id) return "unavailable";
  if (subscription.status === "active" && subscription.expires_at && Date.parse(subscription.expires_at) <= serverTimeNow(subscription, at)) return "expired";
  return subscription.status;
}

export function termDisplayStatus(term, at = Date.now()) {
  if (term.status !== "active") return term.status;
  if (term.starts_at && Date.parse(term.starts_at) > at) return "scheduled";
  if (term.ends_at && Date.parse(term.ends_at) <= at) return "expired";
  return "active";
}

export function canRenewSubscription(order, subscription, user, enterprise) {
  if (!order?.id || order.payment_status !== "paid" || !subscription?.id || subscription.can_renew !== true ||
      order.snapshot_version !== 1 || !["api", "model_api", "tenant_access"].includes(order.delivery_method || order.delivery_method_snapshot) ||
      ["cancelled", "rejected", "closed", "refunded"].includes(order.main_status) ||
      Number(order.refunded_amount || 0) !== 0 ||
      (order.paid_amount != null && order.amount != null && Number(order.paid_amount) !== Number(order.amount)) ||
      !["active", "expired"].includes(subscription.status) || !subscription.expires_at ||
      !Number.isSafeInteger(subscription.generation) || subscription.generation < 1 ||
      order.product_version_id !== subscription.version_id || order.product_version_code !== subscription.version_code ||
      !subscription.terms?.some(term => term.order_id === order.id && term.status === "active" && term.version_code === subscription.version_code &&
        term.generation === subscription.generation) ||
      !enterprise?.id || order.buyer_enterprise_id !== enterprise.id || subscription.enterprise_id !== enterprise.id ||
      (order.product_id && order.product_id !== subscription.product_id) ||
      !user || user.platform_role || user.is_active === false || user.verified_status !== "verified" ||
      (user.activation_status && user.activation_status !== "active") ||
      enterprise.verification_status !== "verified" || enterprise.is_active === false ||
      (enterprise.status && enterprise.status !== "active")) return false;
  const memberships = user.enterprise_memberships;
  const membership = Array.isArray(memberships)
    ? memberships.find(item => item.enterprise_id === enterprise.id && (item.status || item.membership_status) === "active")
    : user.membership_status === "active" ? { role: user.enterprise_role } : null;
  return ["super_admin", "enterprise_admin"].includes(membership?.role);
}

export function renewalPayload(months, key) {
  const duration = typeof months === "number" || typeof months === "string" && months.trim() ? Number(months) : NaN;
  if (!Number.isInteger(duration) || duration < 1 || duration > 36) throw new Error("订阅月数须为1至36的整数");
  if (!text(key)) throw new Error("续费请求标识不可用");
  return { subscription_months: duration, idempotency_key: key };
}

export function renewalOrder(data, source, subscription) {
  const order = data;
  if (!text(order?.id) || order.id === source.id || !text(order.order_no) ||
      order.payment_status !== "unpaid" ||
      (order.product_version_id && order.product_version_id !== subscription.version_id) ||
      (order.version_id && order.version_id !== subscription.version_id) ||
      (order.product_id && order.product_id !== subscription.product_id) ||
      (order.buyer_enterprise_id && order.buyer_enterprise_id !== source.buyer_enterprise_id)) {
    throw new Error("续费订单未确认，请刷新订单后核对");
  }
  return { id: order.id, order_no: order.order_no };
}

export function newRenewalRequestKey(crypto = globalThis.crypto) {
  if (typeof crypto?.randomUUID === "function") return crypto.randomUUID();
  if (typeof crypto?.getRandomValues !== "function") throw new Error("浏览器无法安全生成续费请求标识");
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function createSubscriptionSession({ api, getContext, onChange = () => {}, makeKey = newRenewalRequestKey }) {
  let state = { orderId: "", item: null, loading: false, error: "", modal: null };
  let generation = 0, busy = false, disposed = false;
  const emit = () => { if (!disposed) onChange({ ...state, modal: state.modal ? { ...state.modal } : null }); };
  const contextKey = context => JSON.stringify([context.order?.id, context.user?.id, context.enterprise?.id, context.token]);
  const eligible = () => {
    const context = getContext();
    return state.orderId === context.order?.id && !state.loading && canRenewSubscription(context.order, state.item, context.user, context.enterprise);
  };
  function reset() {
    generation++;
    state = { orderId: "", item: null, loading: false, error: "", modal: null };
    emit();
  }
  async function load(order) {
    if (disposed) return;
    reset();
    const request = generation, context = contextKey(getContext());
    state.orderId = order.id;
    state.loading = true;
    emit();
    const current = () => !disposed && request === generation && context === contextKey(getContext()) && getContext().order?.id === order.id;
    try {
      const { data } = await api.get(`/orders/${encodeURIComponent(order.id)}/subscription`);
      if (current()) state.item = sanitizeSubscription(data);
    } catch {
      if (current()) state.error = "订阅信息加载失败，请重试";
    } finally {
      if (current()) { state.loading = false; emit(); }
    }
  }
  function open() {
    if (disposed || busy || state.modal || !eligible()) return;
    state.modal = { months: 1, key: makeKey(), attempted: false, submitting: false, error: "" };
    emit();
  }
  function close() { if (!busy) { state.modal = null; emit(); } }
  function setMonths(value) {
    if (!state.modal || state.modal.attempted || busy) return;
    state.modal.months = value;
    state.modal.error = "";
    emit();
  }
  async function submit() {
    if (disposed || busy || !state.modal || !eligible()) return null;
    const modal = state.modal, request = generation, context = contextKey(getContext());
    const { order } = getContext(), item = state.item;
    let body;
    try { body = renewalPayload(modal.months, modal.key); }
    catch (error) { modal.error = error.message; emit(); return null; }
    busy = true;
    modal.attempted = true;
    modal.submitting = true;
    modal.error = "";
    emit();
    const current = () => !disposed && request === generation && state.modal === modal && context === contextKey(getContext());
    try {
      const { data } = await api.post(`/orders/${encodeURIComponent(order.id)}/renewal`, body);
      if (!current() || !eligible()) return null;
      const created = renewalOrder(data, order, item);
      state.modal = null;
      return created;
    } catch {
      if (current()) modal.error = "续费订单未确认，可重试或刷新订单核对";
      return null;
    } finally {
      busy = false;
      modal.submitting = false;
      if (current() || request === generation && !disposed) emit();
    }
  }
  return { load, reset, open, close, setMonths, submit, dispose() { reset(); disposed = true; } };
}
