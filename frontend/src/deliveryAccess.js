export function isNewFileOrder(order) {
  return order?.snapshot_version === 1 && (order.delivery_method || order.delivery_method_snapshot) === "file";
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
  return ["api", "model_api", "tenant_access"].includes(order?.delivery_method || order?.delivery_method_snapshot)
    ? "待接入信息" : "-";
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

export function createDeliveryAccessSession({ api, getContext, onChange = () => {}, downloadProtectedFile }) {
  const empty = () => ({ orderId: "", ready: false, files: [], reason: "", loading: false, error: "", downloadError: "", downloading: "" });
  let state = empty(), epoch = 0, nonce = 0, disposed = false;
  const emit = () => { if (!disposed) onChange({ ...state, files: [...state.files] }); };
  const key = () => JSON.stringify(getContext());
  const eligible = () => {
    const { order, user, buyerEnterprise } = getContext();
    return canAccessDeliveryFiles(order, user, buyerEnterprise);
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
      if (current()) state = { ...state, ...sanitizeAccess(data, order.id) };
    } catch (error) {
      if (current()) state = { ...empty(), orderId: order.id, loading: true, error: deliveryAccessError(error) };
    } finally {
      if (current()) { state.loading = false; emit(); }
    }
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
  return { load, download, reset, dispose() { reset(); disposed = true; } };
}
