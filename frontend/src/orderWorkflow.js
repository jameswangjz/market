function currentMembership(user, enterprise) {
  if (!user || !enterprise?.id || user.is_active === false ||
    (user.activation_status && user.activation_status !== "active") || user.platform_role) return null;
  const memberships = user.enterprise_memberships;
  if (Array.isArray(memberships)) {
    return memberships.find(item => item.enterprise_id === enterprise.id && (item.status || item.membership_status) === "active") || null;
  }
  // /auth/me currently supplies the role of the current active membership.
  if (user.membership_status !== "active") return null;
  return { role: user.enterprise_role };
}

export function canReviewProviderOrder(order, user, enterprise) {
  const member = currentMembership(user, enterprise);
  return Boolean(order?.main_status === "pending_provider_review" &&
    order.provider_enterprise_id === enterprise?.id && member &&
    ["super_admin", "enterprise_admin"].includes(member.role));
}

export function isOfflineFulfillmentOrder(order) {
  return order?.snapshot_version === 1 && ["training", "consulting", "custom"].includes(order.delivery_method);
}

export function offlineDeliveryActions(order, user, enterprise) {
  const member = currentMembership(user, enterprise);
  if (!isOfflineFulfillmentOrder(order) || order.payment_status !== "paid" ||
    !["awaiting_start", "in_delivery", "pending_acceptance", "rectifying"].includes(order.main_status) ||
    order.main_status !== order.delivery_status ||
    !["super_admin", "enterprise_admin"].includes(member?.role)) return [];
  const actions = [];
  if (order.provider_enterprise_id === enterprise.id) {
    if (order.delivery_status === "awaiting_start") actions.push(["start_delivery", "开始履约"]);
    if (["in_delivery", "rectifying"].includes(order.delivery_status)) actions.push(["submit_delivery", "提交交付物"]);
  }
  if (order.buyer_enterprise_id === enterprise.id && order.delivery_status === "pending_acceptance") {
    actions.push(["accept_delivery", "验收通过"], ["reject_delivery", "拒绝并退回整改"]);
  }
  return actions;
}

export function canUploadDeliveryAttachment(order, user, enterprise) {
  return offlineDeliveryActions(order, user, enterprise).some(([action]) => ["start_delivery", "submit_delivery"].includes(action));
}

export function deliveryTransitionPayload(action, reason = "") {
  if (!["start_delivery", "submit_delivery", "accept_delivery", "reject_delivery"].includes(action)) throw new Error("不支持的交付动作");
  const text = String(reason).trim();
  if (action === "reject_delivery" && !text) throw new Error("请填写拒绝验收原因");
  return { action, reason: text || "工作台操作" };
}

export function orderWorkflowActions(order, user, enterprise) {
  if (!order || !user || user.is_active === false || (user.activation_status && user.activation_status !== "active")) return [];
  if (order.main_status === "pending_provider_review") {
    return canReviewProviderOrder(order, user, enterprise) ? [["provider_review", "提供方审核"]] : [];
  }
  if (isOfflineFulfillmentOrder(order) && order.main_status !== "pending_payment") return offlineDeliveryActions(order, user, enterprise);
  const legacyPayable = order.snapshot_version !== 1 &&
    ["created", "pending_review", "pending_fulfillment", "fulfilling", "pending_confirmation", "completed"].includes(order.main_status);
  if (order.main_status !== "pending_payment" && !legacyPayable) return [];
  const member = currentMembership(user, enterprise);
  const buyer = order.buyer_enterprise_id === enterprise?.id && member?.role === "super_admin";
  const actions = [];
  if (buyer && order.payment_status === "unpaid") actions.push(["start_payment", "发起模拟支付"]);
  if (buyer && order.payment_status === "paying") actions.push(["confirm_payment", "模拟确认支付"]);
  if (["super_admin", "finance_settlement"].includes(user.platform_role) && ["unpaid", "paying"].includes(order.payment_status)) {
    actions.push(["confirm_payment", "人工确认支付"]);
  }
  return actions;
}

export function providerReviewPayload(quote, form, decision) {
  if (!["approve", "reject"].includes(decision)) throw new Error("请选择审核结果");
  if (!quote?.expected_updated_at) throw new Error("报价信息不完整，请重新打开审核");
  const reason = String(form.reason || "").trim();
  if (decision === "reject") {
    if (!reason) throw new Error("请填写拒绝原因");
    return { decision, reason, expected_updated_at: quote.expected_updated_at };
  }
  const amount = Number(form.amount);
  const cost = Number(form.cost);
  if (form.amount === "" || form.cost === "" || form.amount == null || form.cost == null ||
    !Number.isFinite(amount) || !Number.isFinite(cost) || amount < 0 || cost < 0) throw new Error("请输入有效的非负金额和成本");
  if (amount < cost) throw new Error("订单金额不能低于成本");
  if ((amount !== Number(quote.amount) || cost !== Number(quote.cost)) && !reason) throw new Error("调整金额或成本时请填写原因");
  return { decision, amount, cost, reason, expected_updated_at: quote.expected_updated_at };
}
