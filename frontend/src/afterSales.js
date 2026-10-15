import { newRenewalRequestKey } from "./subscriptionTerms.js";
export const newAfterSalesRequestKey = newRenewalRequestKey;
export const afterSalesStatuses = {
  awaiting_response: "待提供方响应", processing: "处理中", awaiting_buyer_info: "待买方补充材料",
  awaiting_buyer_confirmation: "待买方确认", platform_intervention: "平台介入中",
  completed: "已完成", withdrawn: "已撤回", force_closed: "已强制关闭", resolved: "已解决（历史）", closed: "已关闭（历史）",
};
export const afterSalesActions = {
  respond: "响应工单", request_info: "要求补充材料", supplement: "补充材料", submit_resolution: "提交解决方案",
  confirm: "确认解决", reject_resolution: "拒绝解决方案", withdraw: "撤回工单", request_intervention: "申请平台介入",
  respond_intervention: "介入响应", review_intervention: "完成平台审查", force_close: "强制关闭",
};
export function ticketStatus(value) { return Object.hasOwn(afterSalesStatuses, value) ? afterSalesStatuses[value] : "未知状态"; }
export function timelineActor(event) { return event.actor_name || event.actor_email || event.actor_id || "历史操作者未记录"; }
export function timelineFrom(event) { return event.before?.status ? ticketStatus(event.before.status) : "创建"; }
export function evidenceAuthor(item) { return item.author_name || item.author_email || item.author_id || "历史作者未记录"; }
export function createAfterSalesCloseGuard({ isBusy, isDirty, confirmDiscard, onClose }) {
  let confirming = false;
  return async function requestClose() {
    if (isBusy() || confirming) return;
    if (!isDirty()) { onClose(); return; }
    confirming = true;
    try { if (await confirmDiscard() && !isBusy()) onClose(); }
    finally { confirming = false; }
  };
}
export function ticketActions(ticket, user) {
  if (!Array.isArray(ticket?.allowed_actions)) return [];
  return ticket.allowed_actions.filter(action => Object.hasOwn(afterSalesActions, action)
    && (action !== "force_close" || ["super_admin", "platform_operator"].includes(user?.platform_role)));
}
export function canCollaborate(ticket, user, enterpriseId) {
  if (!user || user.is_active === false || (user.activation_status && user.activation_status !== "active")
    || (user.verified_status && user.verified_status !== "verified")) return false;
  if (user?.platform_role) return ["super_admin", "platform_operator", "delivery_monitor"].includes(user.platform_role);
  return [ticket?.buyer_enterprise_id, ticket?.provider_enterprise_id].includes(enterpriseId)
    && user?.enterprise_memberships?.some(item => item.enterprise_id === enterpriseId && item.status === "active"
      && ["super_admin", "enterprise_admin"].includes(item.role));
}
export function canCreateAfterSales(order, user, enterpriseId) {
  return !user?.platform_role && order?.buyer_enterprise_id === enterpriseId
    && ["paid", "refunding", "refunded"].includes(order.payment_status) && Number(order.paid_amount) > 0
    && canCollaborate({ buyer_enterprise_id: enterpriseId }, user, enterpriseId);
}
export function createAfterSalesSession(api, state, params = {}, makeKey = newAfterSalesRequestKey) {
  let generation = 0;
  let disposed = false;
  params = { ...params };
  let operation;
  const root = id => `/after-sales/${encodeURIComponent(id)}`;
  function keyFor(kind, payload) {
    const fingerprint = JSON.stringify([kind, state.ticket?.id, payload]);
    if (operation?.fingerprint !== fingerprint) operation = { fingerprint, key: makeKey() };
    return operation.key;
  }
  async function load(id) {
    if (disposed) return;
    const current = ++generation;
    state.loading = true; state.error = "";
    if (state.ticket?.id !== id) state.ticket = null;
    try {
      const responses = await Promise.all([api.get(root(id), { params }), ...["timeline", "comments", "attachments"].map(
        section => api.get(`${root(id)}/${section}`, { params }))]);
      if (current !== generation) return;
      state.ticket = responses[0].data;
      [state.timeline, state.comments, state.attachments] = responses.slice(1).map(response => response.data.items || []);
    } catch (error) {
      if (current === generation) { state.ticket = null; state.error = error.response?.data?.detail || "工单详情加载失败，请重试"; }
    } finally { if (current === generation) state.loading = false; }
  }
  async function mutate(kind, payload, file) {
    if (disposed || state.busy || state.loading || !state.ticket) return false;
    const id = state.ticket.id, current = generation;
    const body = { ...payload, expected_version: state.ticket.version };
    let key;
    try { key = keyFor(kind, file ? { ...body, name: file.name, size: file.size, lastModified: file.lastModified } : body); }
    catch { state.error = "浏览器无法安全生成请求标识，请更换浏览器后重试"; return false; }
    state.busy = true; state.error = "";
    try {
      let request = { ...body, idempotency_key: key };
      if (file) {
        request = new FormData();
        for (const [field, value] of Object.entries(body)) request.append(field, value);
        request.append("idempotency_key", key); request.append("upload", file);
      }
      await api.post(`${root(id)}/${kind}`, request, { params });
      operation = null;
      if (current === generation) { await load(id); return !disposed; }
      return false;
    } catch (error) {
      if (current !== generation) return false;
      if (error.response?.status === 409) {
        operation = null;
        const refreshGeneration = generation + 1;
        await load(id);
        if (disposed || generation !== refreshGeneration) return false;
        state.error = "工单已变化，已尝试刷新；请核对最新状态后重新确认。原输入已保留。";
      } else state.error = error.response?.data?.detail || "提交失败，输入已保留，可重试";
      return false;
    } finally { if (!disposed) state.busy = false; }
  }
  return { load, mutate, dispose() { disposed = true; generation++; state.ticket = null; } };
}
