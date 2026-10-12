import { canAccessBuyerDelivery, deliveryMethod } from "./deliveryAccess.js";

export function canDownloadApiCredential(order, user, buyerEnterprise, credential) {
  return ["api", "model_api"].includes(deliveryMethod(order)) &&
    canAccessBuyerDelivery(order, user, buyerEnterprise) &&
    credential?.status === "active" && credential.secret_download_available === true;
}

export async function downloadApiCredential({ api, order, user, buyerEnterprise, credential, saveBlob }) {
  if (!api || !credential?.id || !canDownloadApiCredential(order, user, buyerEnterprise, credential)) return false;
  const { data } = await api.get(
    `/orders/${encodeURIComponent(order.id)}/api-credentials/${encodeURIComponent(credential.id)}/download`,
    { responseType: "blob" },
  );
  await saveBlob(data, `api-credential-${String(credential.id).replace(/[^A-Za-z0-9_-]/g, "_")}.json`);
  return true;
}
