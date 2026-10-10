// All order creation callers must supply an explicitly selected enterprise.
export function explicitOrderPayload(payload) {
  const buyerEnterpriseId = payload?.buyer_enterprise_id;
  if (typeof buyerEnterpriseId !== "string" || !buyerEnterpriseId.trim()) {
    throw new Error("请选择购买企业后再创建订单");
  }
  return { ...payload, buyer_enterprise_id: buyerEnterpriseId };
}

export function createOrder(api, payload, buyerEnterpriseId) {
  return api.post("/orders", explicitOrderPayload({ ...payload, buyer_enterprise_id: buyerEnterpriseId }));
}
