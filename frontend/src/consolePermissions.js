import { safeApplicationUrl } from "./routes.js";

const operators = ["super_admin", "platform_operator"];
const auditors = [...operators, "finance_settlement", "security_compliance"];

export function canAccessConsoleView(user, enterprise, view) {
  if (!user) return false;
  if (view === "applications") return canViewApplications(user);
  // Keep account, verification and enterprise onboarding available to all users.
  if (["users", "messages"].includes(view)) return true;
  if (view === "products") return [...operators, "product_manager", "quality_reviewer", "security_compliance"].includes(user.platform_role) || Boolean(!user.platform_role && activeConsoleMembership(user, enterprise));
  if (user.verified_status !== "verified") return false;
  const role = user.platform_role;
  if (view === "development") return role === "super_admin";
  if (["gateway", "sla", "settings"].includes(view)) return operators.includes(role);
  const enterpriseAdmin = !role && ["super_admin", "enterprise_admin"].includes(activeConsoleMembership(user, enterprise)?.role);
  if (["settlements", "audit"].includes(view)) return auditors.includes(role) || enterpriseAdmin;
  if (view === "delivery") return [...operators, "delivery_monitor"].includes(role) || enterpriseAdmin;
  if (view === "orders") return [...auditors, "delivery_monitor"].includes(role) || Boolean(!role && activeConsoleMembership(user, enterprise));
  if (view === "overview") return Boolean((!role && activeConsoleMembership(user, enterprise)?.role === "super_admin") || [...operators, "security_compliance"].includes(role));
  return false;
}

function activeConsoleMembership(user, enterprise) {
  return enterprise?.id && user?.enterprise_memberships?.find(member => member.enterprise_id === enterprise.id &&
    (member.status || member.membership_status) === "active");
}

export function canManageProductRegistration(user, enterprise) {
  if (!user) return false;
  if ([...operators, "product_manager", "quality_reviewer", "security_compliance"].includes(user.platform_role)) return true;
  return !user.platform_role && user.verified_status === "verified" && ["super_admin", "enterprise_admin"].includes(activeConsoleMembership(user, enterprise)?.role);
}

export function activeApplicationMembership(user, enterpriseId) {
  return user?.enterprise_memberships?.find(member => member.enterprise_id === enterpriseId &&
    (member.status || member.membership_status) === "active");
}

export function canViewApplications(user) {
  return Boolean(user?.id && !user.platform_role && user.verified_status === "verified" &&
    user.is_active !== false && (!user.activation_status || user.activation_status === "active") &&
    user.enterprise_memberships?.some(member => (member.status || member.membership_status) === "active"));
}

export function canManageApplication(user, item) {
  return canViewApplications(user) && item?.is_enterprise_admin === true &&
    ["super_admin", "enterprise_admin"].includes(activeApplicationMembership(user, item.enterprise_id)?.role);
}

const appText = value => typeof value === "string" ? value : "";
const appId = value => typeof value === "string" && value.length > 0 && value.length <= 180 && !/[\\/\u0000-\u0020\u007f]/.test(value);

export function sanitizeApplications(data, user, enterpriseId = "") {
  if (!Array.isArray(data?.items)) throw new Error("应用数据无效");
  const seen = new Set();
  return data.items.map(item => {
    if (![item.subscription_id, item.enterprise_id, item.product_id].every(appId) ||
        !activeApplicationMembership(user, item.enterprise_id) || (enterpriseId && item.enterprise_id !== enterpriseId) || seen.has(item.subscription_id) ||
        typeof item.available !== "boolean" || typeof item.is_enterprise_admin !== "boolean") throw new Error("应用数据无效");
    seen.add(item.subscription_id);
    return { subscription_id: item.subscription_id, enterprise_id: item.enterprise_id, product_id: item.product_id,
      enterprise_name: appText(item.enterprise_name), product_name: appText(item.product_name),
      provider_name: appText(item.provider_name), version_code: appText(item.version_code),
      expires_at: appText(item.expires_at), available: item.available, blocked_reason: appText(item.blocked_reason),
      is_enterprise_admin: item.is_enterprise_admin, logo_url: appText(item.logo_url) };
  });
}

export function sanitizeApplicationMembers(data) {
  if (!Array.isArray(data?.items)) throw new Error("成员数据无效");
  const seen = new Set();
  return data.items.map(item => {
    if (!appId(item.user_id) || seen.has(item.user_id) || typeof item.granted !== "boolean") throw new Error("成员数据无效");
    seen.add(item.user_id);
    return { user_id: item.user_id, name: appText(item.name), login_name: appText(item.login_name),
      email: appText(item.email), role: appText(item.role), granted: item.granted };
  });
}

export function applicationError(error) {
  const status = error?.response?.status;
  return status === 401 ? "登录已失效，请重新登录" : status === 403 ? "当前账号无权访问该应用" :
    [404, 409, 410].includes(status) ? "应用或授权请求已失效，请刷新后重试" : "请求未完成，请重试";
}

export function createApplicationsSession({ api, getContext, onChange, onNavigate }) {
  let epoch = 0, disposed = false, listNonce = 0, memberNonce = 0, saveNonce = 0;
  let state = empty();
  function empty() { return { items: [], loading: false, error: "", launching: "", modal: null, members: [], memberLoading: false, memberError: "", saving: "" }; }
  function emit() { onChange({ ...state, items: state.items.map(item => ({ ...item })), members: state.members.map(item => ({ ...item })), modal: state.modal ? { ...state.modal } : null }); }
  function key() { return JSON.stringify(getContext()); }
  function capture() { return { epoch, key: key() }; }
  function current(ticket) { return !disposed && ticket.epoch === epoch && ticket.key === key() && canViewApplications(getContext().user); }
  function config() { return { headers: { Authorization: `Bearer ${getContext().token}` }, withCredentials: true }; }
  function inSelectedEnterprise(item) { return !getContext().enterprise?.id || item?.enterprise_id === getContext().enterprise.id; }
  function reset() { epoch++; listNonce++; memberNonce++; saveNonce++; state = empty(); emit(); }
  function closeMembers() { memberNonce++; saveNonce++; state.modal = null; state.members = []; state.memberError = ""; state.memberLoading = false; state.saving = ""; emit(); }
  async function load() {
    reset();
    if (disposed || !getContext().token || !canViewApplications(getContext().user)) return;
    const ticket = capture(), nonce = ++listNonce;
    const enterpriseId = getContext().enterprise?.id || "";
    state.loading = true; emit();
    try {
      const { data } = await api.get("/applications", { ...config(), ...(enterpriseId ? { params: { enterprise_id: enterpriseId } } : {}) });
      if (current(ticket) && nonce === listNonce) state.items = sanitizeApplications(data, getContext().user, enterpriseId);
    } catch (error) { if (current(ticket) && nonce === listNonce) state.error = applicationError(error); }
    finally { if (current(ticket) && nonce === listNonce) { state.loading = false; emit(); } }
  }
  async function launch(id) {
    const item = state.items.find(item => item.subscription_id === id);
    if (!item?.available || !inSelectedEnterprise(item) || state.launching || state.loading || !canViewApplications(getContext().user)) return;
    const ticket = capture();
    state.launching = id; state.error = ""; emit();
    try {
      const { data } = await api.post(`/applications/${encodeURIComponent(id)}/launch`, undefined, config());
      if (current(ticket)) onNavigate(safeApplicationUrl(data?.launch_url));
    } catch (error) { if (current(ticket)) state.error = applicationError(error); }
    finally { if (current(ticket)) { state.launching = ""; emit(); } }
  }
  async function loadMembers() {
    const item = state.modal;
    if (!item || !inSelectedEnterprise(item) || !canManageApplication(getContext().user, item)) return;
    const ticket = capture(), nonce = ++memberNonce;
    state.memberLoading = true; state.memberError = ""; state.members = []; emit();
    try {
      const { data } = await api.get(`/applications/${encodeURIComponent(item.subscription_id)}/members`, config());
      if (current(ticket) && nonce === memberNonce && state.modal?.subscription_id === item.subscription_id) state.members = sanitizeApplicationMembers(data);
    } catch (error) { if (current(ticket) && nonce === memberNonce) state.memberError = applicationError(error); }
    finally { if (current(ticket) && nonce === memberNonce) { state.memberLoading = false; emit(); } }
  }
  async function openMembers(id) {
    const item = state.items.find(item => item.subscription_id === id);
    if (!inSelectedEnterprise(item) || !canManageApplication(getContext().user, item)) return;
    closeMembers(); state.modal = item; emit(); await loadMembers();
  }
  async function grant(userId, granted) {
    const item = state.modal;
    const member = state.members.find(member => member.user_id === userId);
    if (!item || !inSelectedEnterprise(item) || state.saving || state.memberLoading || typeof granted !== "boolean" ||
        !member || ["super_admin", "enterprise_admin"].includes(member.role) || !canManageApplication(getContext().user, item)) return;
    const ticket = capture(), nonce = memberNonce, savingTicket = ++saveNonce;
    state.saving = userId; state.memberError = ""; emit();
    try {
      const { data } = await api.post(`/applications/${encodeURIComponent(item.subscription_id)}/grants`, { user_id: userId, granted }, config());
      if (!current(ticket) || nonce !== memberNonce) return;
      if (data?.granted !== granted || (data.user_id !== undefined && data.user_id !== userId)) throw new Error("授权响应无效");
      await loadMembers();
    } catch (error) { if (current(ticket) && nonce === memberNonce) state.memberError = applicationError(error); }
    finally { if (current(ticket) && savingTicket === saveNonce) { state.saving = ""; emit(); } }
  }
  return { load, launch, openMembers, loadMembers, closeMembers, grant, reset, dispose() { reset(); disposed = true; } };
}
