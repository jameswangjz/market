import { safeApplicationUrl } from "./routes.js";

export function isNewFileOrder(order) {
  return order?.snapshot_version === 1 && (order.delivery_method || order.delivery_method_snapshot) === "file";
}

export function deliveryMethod(order) {
  const method = order?.delivery_method || order?.delivery_method_snapshot || "";
  return method === "tenant_access" ? "saas" : method;
}

export function canAccessBuyerDelivery(order, user, buyerEnterprise) {
  if (!order || order.snapshot_version !== 1 || !["file", "api", "model_api", "saas", "tenant_access"].includes(deliveryMethod(order)) ||
      order.payment_status !== "paid" || order.main_status !== "completed" || order.after_sales_status === "refunded" ||
      order.delivery_status !== "accepted" || Number(order.refunded_amount || 0) > 0 ||
      !user || user.platform_role || user.verified_status !== "verified" || user.is_active === false ||
      (user.activation_status && user.activation_status !== "active") || !buyerEnterprise?.id ||
      buyerEnterprise.id !== order.buyer_enterprise_id || buyerEnterprise.verification_status !== "verified" ||
      buyerEnterprise.is_active === false || (buyerEnterprise.status && buyerEnterprise.status !== "active")) return false;
  const member = Array.isArray(user.enterprise_memberships) ? user.enterprise_memberships.find(item =>
    item.enterprise_id === order.buyer_enterprise_id && (item.status || item.membership_status) === "active") : null;
  return ["super_admin", "enterprise_admin", "admin"].includes(member?.role);
}

export function canAccessBuyerSaas(order, user, buyerEnterprise) {
  return deliveryMethod(order) === "saas" && canAccessBuyerDelivery(order, user, buyerEnterprise);
}

export function canAccessDeliveryFiles(order, user, buyerEnterprise) {
  if (!isNewFileOrder(order) || order.payment_status !== "paid" ||
      order.main_status !== "completed" || order.delivery_status !== "accepted" ||
      !user || user.platform_role || user.verified_status !== "verified" || user.is_active === false ||
      (user.activation_status && user.activation_status !== "active") ||
      !buyerEnterprise?.id || buyerEnterprise.id !== order.buyer_enterprise_id ||
      buyerEnterprise.verification_status !== "verified" || buyerEnterprise.is_active === false ||
      (buyerEnterprise.status && buyerEnterprise.status !== "active")) return false;
  const member = Array.isArray(user.enterprise_memberships) ? user.enterprise_memberships.find(item =>
    item.enterprise_id === order.buyer_enterprise_id && (item.status || item.membership_status) === "active") : null;
  return ["super_admin", "enterprise_admin", "admin"].includes(member?.role);
}

export function deliveryAccessPlaceholder(order) {
  const method = deliveryMethod(order);
  if (!["file", "api", "model_api", "saas"].includes(method)) return "-";
  if (order?.after_sales_status === "refunded" || Number(order?.refunded_amount || 0) > 0 || order?.payment_status === "refunded") return "已退款";
  if (order?.after_sales_status === "revoked" || order?.delivery_status === "revoked") return "已撤销";
  if (["cancelled", "closed", "rejected"].includes(order?.main_status)) return "已取消";
  if (order?.payment_status && order.payment_status !== "paid") return "未支付";
  if (["retrying", "rectifying"].includes(order?.delivery_status)) return "等待重试";
  if (order?.delivery_status === "exception") return "交付异常";
  if (["awaiting_start", "in_delivery"].includes(order?.delivery_status)) return "交付中";
  if (order?.delivery_status === "pending_acceptance") return "待验收";
  return method === "file" ? "下载文件" : ["api", "model_api"].includes(method) ? "查看接入信息" : method === "saas" ? "访问应用" : "-";
}

export function createSaasListAccessSession({ api, order, getContext, onChange = () => {}, onNavigate = () => {} }) {
  let state = { loading: false, ready: false, applicationUrl: "", reason: "", error: "" };
  let generation = 0, disposed = false, loading = false;
  const emit = () => { if (!disposed) onChange({ ...state }); };
  const eligible = () => {
    const context = getContext();
    return context.order?.id === order.id && canAccessBuyerSaas(context.order, context.user, context.buyerEnterprise);
  };
  async function load() {
    if (disposed || loading || !eligible()) return;
    loading = true;
    const request = ++generation, context = JSON.stringify(getContext());
    const current = () => !disposed && request === generation && context === JSON.stringify(getContext()) && eligible();
    state = { loading: true, ready: false, applicationUrl: "", reason: "", error: "" };
    emit();
    try {
      const { data } = await api.get(`/orders/${encodeURIComponent(order.id)}/delivery-access`);
      const clean = sanitizeDelivery(data, order.id, "saas");
      const url = clean.ready ? safeApplicationUrl(data.saas?.application_url) : "";
      if (current()) state = { loading: false, ready: clean.ready && Boolean(url), applicationUrl: url, reason: clean.reason, error: "" };
    } catch (error) {
      if (current()) state = { loading: false, ready: false, applicationUrl: "", reason: "", error: deliveryAccessError(error) };
    } finally {
      loading = false;
      if (current()) emit();
    }
  }
  async function launch() {
    const request = generation, context = JSON.stringify(getContext());
    const current = () => !disposed && request === generation && context === JSON.stringify(getContext()) && eligible();
    if (!current() || state.loading || !state.ready || !state.applicationUrl) return;
    state = { ...state, loading: true, error: "" };
    emit();
    try {
      const path = `/api/orders/${encodeURIComponent(order.id)}/delivery-launch`;
      const { data } = await api.post(path.slice(4));
      if (current()) {
        if (data?.order_id !== order.id) throw new Error("应用启动订单不匹配");
        onNavigate(safeApplicationUrl(data?.launch_url));
      }
    } catch (error) {
      if (current()) state = { ...state, error: deliveryAccessError(error) };
    } finally {
      if (current()) { state = { ...state, loading: false }; emit(); }
    }
  }
  return { load, launch, dispose() { generation++; disposed = true; } };
}

function sanitizeDelivery(data, orderId, method) {
  const expectedMethod = method === "saas" ? "tenant_access" : method;
  if (data?.order_id !== orderId || ![method, expectedMethod].includes(data.method) || typeof data.ready !== "boolean" || !Array.isArray(data.files)) throw new Error("交付信息无效");
  const result = { ready: data.ready, files: [], reason: typeof data.reason === "string" ? data.reason : "" };
  if (method === "file") return { ...result, files: data.ready ? sanitizeAccess(data, orderId).files : [] };
  if (["api", "model_api"].includes(method) && (data.ready || data.api)) {
    const api = data.api;
    if (!api) throw new Error("API 接入信息无效");
    let gatewayUrl = "";
    if (api.gateway_url !== undefined) {
      if (api.gateway_url === "" && !data.ready) gatewayUrl = "";
      else {
        if (typeof api.gateway_url !== "string" || /[\\\u0000-\u0020\u007f]/.test(api.gateway_url)) throw new Error("API 网关地址无效");
        try {
          const url = new URL(api.gateway_url);
          if (!["http:", "https:"].includes(url.protocol) || !url.hostname || url.username || url.password || url.search || url.hash || url.pathname !== api.gateway_base_path) throw new Error();
          gatewayUrl = api.gateway_url;
        } catch { throw new Error("API 网关地址无效"); }
      }
    }
    if (typeof api.gateway_base_path !== "string" || !/^\/gateway\/[A-Za-z0-9_-]{1,100}$/.test(api.gateway_base_path) ||
        api.credentials_path !== `/api/orders/${encodeURIComponent(orderId)}/api-credentials` || api.shared_scope !== "enterprise" ||
        typeof api.version_code !== "string" || !api.version_code || !validDate(api.expires_at) || !api.limits || typeof api.limits !== "object") throw new Error("API 接入信息无效");
    const limitKeys = ["rate_limit_per_minute", "daily_quota", "monthly_quota"];
    if (limitKeys.some(key => !Number.isSafeInteger(api.limits[key]) || api.limits[key] < 0) ||
        (api.limits.quota_amount !== undefined && (!Number.isSafeInteger(api.limits.quota_amount) || api.limits.quota_amount < 0))) throw new Error("API 额度信息无效");
    return { ...result, api: { gateway_base_path: api.gateway_base_path, credentials_path: api.credentials_path,
      shared_scope: api.shared_scope, version_code: api.version_code, expires_at: api.expires_at,
      gateway_url: gatewayUrl, limits: Object.fromEntries([...limitKeys, "quota_amount"].filter(key => api.limits[key] !== undefined).map(key => [key, api.limits[key]])) } };
  }
  if (!data.ready) return result;
  if (method === "saas" || data.method === "saas") {
    const saas = data.saas;
    if (!saas || typeof saas.subscription_id !== "string" || saas.launch_path !== `/api/orders/${encodeURIComponent(orderId)}/delivery-launch` || !validDate(saas.expires_at)) throw new Error("应用访问信息无效");
    const applicationUrl = saas.application_url === undefined ? "" : safeApplicationUrl(saas.application_url);
    return { ...result, saas: { subscription_id: saas.subscription_id, launch_path: saas.launch_path, expires_at: saas.expires_at, application_url: applicationUrl } };
  }
  throw new Error("交付方式不受支持");
}

function validDate(value) {
  return typeof value === "string" && Number.isFinite(Date.parse(value));
}

export function protectedDeliveryUrl(file, orderId) {
  if (typeof file?.url !== "string" || !file.id || !orderId) return "";
  const prefix = `/api/files/${encodeURIComponent(file.id)}/download?`;
  if (!file.url.startsWith(prefix)) return "";
  const params = new URLSearchParams(file.url.slice(prefix.length));
  if (params.size !== 1 || params.get("order_id") !== orderId || file.url.includes("#")) return "";
  // The authenticated axios client already has /api as its base URL.
  return file.url.slice(4);
}

export function deliveryAccessError(error) {
  const detail = error?.response?.data?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (error instanceof Error && error.message) return error.message;
  if (error?.response?.status === 403) return "无权访问交付文件，请核对买方企业权限后重试";
  if (error?.response?.status === 409) return "订单交付状态已变更，请刷新后重试";
  return "交付文件加载或下载失败，请重试";
}

function sanitizeAccess(data, orderId) {
  if (data?.order_id !== orderId || data.method !== "file" || typeof data.ready !== "boolean" || !Array.isArray(data.files)) {
    throw new Error("交付信息无效");
  }
  const count = value => Number.isSafeInteger(value) && value >= 0;
  const files = data.ready ? data.files.map(file => {
    if (typeof file?.id !== "string" || !file.id || typeof file.name !== "string" ||
        !count(file.download_limit) || !count(file.used_downloads) ||
        !(file.remaining_downloads === null && file.download_limit === 0 || count(file.remaining_downloads))) {
      throw new Error("交付文件信息无效");
    }
    return { ...file, url: protectedDeliveryUrl(file, orderId) ? file.url : "" };
  }) : [];
  return { ready: data.ready, files, reason: typeof data.reason === "string" ? data.reason : "" };
}

export function canDownloadDeliveryFile(state, file) {
  return Boolean(state.ready && !state.loading && !state.error && !state.downloading &&
    protectedDeliveryUrl(file, state.orderId) && (file.download_limit === 0 || file.remaining_downloads > 0));
}

export function createDeliveryAccessSession({ api, getContext, onChange = () => {}, downloadProtectedFile, onNavigate = () => {} }) {
  const empty = () => ({ orderId: "", ready: false, files: [], reason: "", loading: false, error: "", downloadError: "", downloading: "", launching: false, launchUrl: "", api: null, saas: null });
  let state = empty(), epoch = 0, nonce = 0, disposed = false;
  const emit = () => { if (!disposed) onChange({ ...state, files: [...state.files] }); };
  const key = () => JSON.stringify(getContext());
  const eligible = () => {
    const { order, user, buyerEnterprise } = getContext();
    return canAccessBuyerDelivery(order, user, buyerEnterprise);
  };
  function reset() { epoch++; nonce++; state = empty(); emit(); }
  async function load(order) {
    if (disposed || !eligible() || getContext().order?.id !== order?.id) { reset(); return; }
    const request = ++nonce, generation = epoch, context = key();
    state = { ...empty(), orderId: order.id, loading: true };
    emit();
    const current = () => !disposed && generation === epoch && request === nonce && context === key() && eligible();
    try {
      const { data } = await api.get(`/orders/${encodeURIComponent(order.id)}/delivery-access`);
      if (current()) state = { ...state, ...sanitizeDelivery(data, order.id, deliveryMethod(order)) };
    } catch (error) {
      if (current()) state = { ...empty(), orderId: order.id, loading: true, error: deliveryAccessError(error) };
    } finally {
      if (current()) { state.loading = false; emit(); }
    }
  }
  async function launch() {
    const order = getContext().order;
    const request = nonce, generation = epoch, context = key();
    const current = () => !disposed && generation === epoch && request === nonce && context === key() && eligible();
    if (!current() || deliveryMethod(order) !== "saas" || !state.ready || state.loading || state.launching || !state.saas) return;
    state.launching = true; state.error = ""; emit();
    try {
      const { data } = await api.post(state.saas.launch_path.slice(4));
      if (current()) onNavigate(safeApplicationUrl(data?.launch_url));
    } catch (error) {
      if (current()) state.error = deliveryAccessError(error);
    } finally { if (current()) { state.launching = false; emit(); } }
  }
  async function download(fileId) {
    const file = state.files.find(item => item.id === fileId);
    if (disposed || !eligible() || !canDownloadDeliveryFile(state, file)) return;
    const request = nonce, generation = epoch, context = key(), order = getContext().order;
    const current = () => !disposed && generation === epoch && request === nonce && context === key() && eligible();
    state.downloading = file.id;
    state.downloadError = "";
    emit();
    let errorMessage = "";
    try {
      await downloadProtectedFile(protectedDeliveryUrl(file, order.id), file.name, current);
    } catch (error) {
      errorMessage = deliveryAccessError(error);
    } finally {
      if (current()) {
        // Quotas may change even when the download response fails.
        await load(order);
        if (!disposed && generation === epoch && nonce === request + 1 && context === key()) {
          state.downloadError = errorMessage;
          emit();
        }
      }
    }
  }
  return { load, download, launch, reset, dispose() { reset(); disposed = true; } };
}
