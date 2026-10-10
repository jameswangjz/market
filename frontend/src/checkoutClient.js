import axios from "axios";

export function createPublicStorefrontClient() {
  const client = axios.create({ baseURL: "/api/storefront", timeout: 15000 });
  client.interceptors.request.use(config => {
    config.headers.delete("Authorization");
    return config;
  });
  return client;
}

export function createCheckoutClient({ getToken = () => globalThis.localStorage?.getItem("market_token") || "", adapter } = {}) {
  const client = axios.create({ baseURL: "/api", timeout: 15000, ...(adapter ? { adapter } : {}) });
  client.interceptors.request.use(config => {
    const token = getToken();
    if (!token) throw Object.assign(new Error("请先登录"), { response: { status: 401 } });
    config.headers.set("Authorization", `Bearer ${token}`);
    return config;
  });
  return { hasToken: () => Boolean(getToken()),
    identity: signal => client.get("/auth/me", { signal }).then(result => result.data),
    enterprises: signal => client.get("/storefront/buyer-enterprises", { signal }).then(result => result.data),
    quote: (body, signal) => client.post("/orders/quote", body, { signal }).then(result => result.data),
    order: body => client.post("/orders", body).then(result => result.data) };
}

export function isMonthlyDelivery(product) {
  return product?.delivery_category !== "offline" && ["api", "model_api", "tenant_access"].includes(product?.delivery_method);
}

export function buyerEligibility(user, enterprise) {
  if (!user) return "请先登录";
  if (user.verified_status !== "verified") return "请先完成个人实名认证";
  if (user.platform_role) return "平台角色账号不能作为企业购买方";
  if (!enterprise) return "请选择购买企业";
  if (!["super_admin", "enterprise_admin"].includes(enterprise.role)) return "仅企业超级管理员和企业管理员可订阅";
  if (enterprise.verification_status !== "verified") return "请先完成企业实名认证";
  if (enterprise.eligible !== true) return enterprise.reason || "该企业暂不具备购买资格";
  return "";
}

export function checkoutPayload(product, versionId, enterpriseId, months) {
  if (!product?.id || !product.versions?.some(version => version.id === versionId)) throw new Error("请选择可售版本");
  if (!enterpriseId) throw new Error("请选择购买企业");
  const duration = isMonthlyDelivery(product) ? Number(months) : 1;
  if (!Number.isInteger(duration) || duration < 1 || duration > 36) throw new Error("订阅月数须为1至36的整数");
  return { product_id: product.id, product_version_id: versionId, buyer_enterprise_id: enterpriseId, subscription_months: duration };
}

function publicQuote(data, body, product) {
  const monetary = value => (typeof value === "number" || typeof value === "string" && value.trim() !== "") && Number.isFinite(Number(value)) && Number(value) >= 0;
  if (!data || typeof data.quote_id !== "string" || !data.quote_id || data.product_id !== body.product_id || data.product_version_id !== body.product_version_id ||
      data.subscription_months !== body.subscription_months || data.currency !== "CNY" ||
      data.billing_unit !== (isMonthlyDelivery(product) ? "month" : "order") ||
      !monetary(data.unit_price) || !monetary(data.amount)) throw new Error("报价数据暂时不可用，请重新获取");
  return Object.fromEntries(["quote_id", "product_id", "product_version_id", "product_version_code", "delivery_method", "delivery_category", "billing_unit", "subscription_months", "unit_price", "amount", "currency"].map(key => [key, data[key]]));
}

export function createCheckoutSession(client, onChange = () => {}) {
  const state = { user: null, enterprises: [], identityBusy: false, quote: null, quoteBusy: false, submitting: false, error: "" };
  let selection = {}, body = null, quoteRequest, identityRequest, generation = 0, disposed = false;
  const emit = () => { if (!disposed) onChange({ ...state }); };
  function failure(error, fallback) {
    if (error.response?.status === 401) {
      state.user = null;
      state.enterprises = [];
      state.quote = null;
      return "登录已失效，请重新登录";
    }
    if (error.response?.status === 403) return "购买资格已变化，请刷新企业资格后重试";
    return error.response ? fallback : error.message || fallback;
  }
  async function loadIdentity() {
    if (disposed) return;
    identityRequest?.abort();
    const request = identityRequest = new AbortController();
    state.user = null;
    state.enterprises = [];
    state.error = "";
    setSelection(selection);
    if (!client.hasToken()) { state.identityBusy = false; emit(); return; }
    state.identityBusy = true;
    emit();
    try {
      const identity = await client.identity(request.signal);
      const enterprises = await client.enterprises(request.signal);
      if (disposed || request.signal.aborted) return;
      if (!identity?.user || !Array.isArray(enterprises?.items)) throw new Error("购买资格数据暂时不可用");
      state.user = identity.user;
      state.enterprises = enterprises.items;
    } catch (error) {
      if (disposed || request.signal.aborted) return;
      state.error = failure(error, "无法加载购买企业，请重试");
    } finally {
      if (!disposed && !request.signal.aborted) { state.identityBusy = false; emit(); }
    }
    if (state.user) setSelection(selection);
  }
  async function setSelection(next) {
    if (disposed) return;
    selection = { ...next };
    const current = ++generation;
    quoteRequest?.abort();
    state.quote = null;
    state.quoteBusy = false;
    state.error = "";
    body = null;
    const enterprise = state.enterprises.find(item => item.id === selection.enterpriseId);
    if (buyerEligibility(state.user, enterprise) || !selection.product) { emit(); return; }
    try { body = checkoutPayload(selection.product, selection.versionId, selection.enterpriseId, selection.months); }
    catch (error) { state.error = error.message; emit(); return; }
    const payload = body;
    const request = quoteRequest = new AbortController();
    state.quoteBusy = true;
    emit();
    try {
      const data = await client.quote(payload, request.signal);
      if (disposed || current !== generation) return;
      state.quote = publicQuote(data, payload, selection.product);
    } catch (error) {
      if (disposed || current !== generation) return;
      state.error = failure(error, "无法获取报价，请检查版本和月数后重试");
    } finally {
      if (!disposed && current === generation) { state.quoteBusy = false; emit(); }
    }
  }
  async function submit() {
    if (disposed || state.submitting || state.quoteBusy || !state.quote || !body) return null;
    if (buyerEligibility(state.user, state.enterprises.find(item => item.id === body.buyer_enterprise_id))) return null;
    state.submitting = true;
    state.error = "";
    emit();
    try {
      const order = await client.order({ ...body, quote_id: state.quote.quote_id });
      if (!order?.id || !order.order_no) throw new Error("订单响应异常，请前往订单页核对后再操作");
      return order;
    } catch (error) {
      if (error.response?.status === 409) {
        await setSelection(selection);
        state.error = state.quote ? "报价已变化，请确认最新报价后重新订阅" : state.error || "报价已失效，请重新获取报价";
        state.submitting = false;
        emit();
        return null;
      }
      state.error = failure(error, "下单未确认，请前往订单页核对后再操作");
      state.quote = null;
      state.submitting = false;
      emit();
      return null;
    }
  }
  return { loadIdentity, setSelection, submit, dispose() { disposed = true; generation++; quoteRequest?.abort(); identityRequest?.abort(); } };
}
