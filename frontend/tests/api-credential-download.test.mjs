import test from "node:test";
import assert from "node:assert/strict";
import { canDownloadApiCredential, downloadApiCredential } from "../src/apiCredentialDownload.js";

const order = {
  id: "order-1",
  snapshot_version: 1,
  delivery_method: "api",
  payment_status: "paid",
  main_status: "completed",
  delivery_status: "accepted",
  after_sales_status: "none",
  refunded_amount: 0,
  buyer_enterprise_id: "enterprise-1",
};
const user = {
  id: "buyer-1",
  verified_status: "verified",
  is_active: true,
  activation_status: "active",
  enterprise_memberships: [{ enterprise_id: "enterprise-1", role: "enterprise_admin", status: "active" }],
};
const enterprise = {
  id: "enterprise-1",
  verification_status: "verified",
  is_active: true,
  status: "active",
};
const credential = { id: "credential-1", status: "active", secret_download_available: true };

test("download is available only for eligible buyers and active downloadable API credentials", () => {
  assert.equal(canDownloadApiCredential(order, user, enterprise, credential), true);
  for (const changes of [
    [{ ...order, payment_status: "unpaid" }, user, enterprise, credential],
    [{ ...order, delivery_method: "file" }, user, enterprise, credential],
    [order, { ...user, verified_status: "pending" }, enterprise, credential],
    [order, { ...user, platform_role: "platform_operator" }, enterprise, credential],
    [order, { ...user, enterprise_memberships: [{ ...user.enterprise_memberships[0], role: "member" }] }, enterprise, credential],
    [order, user, { ...enterprise, verification_status: "pending" }, credential],
    [order, user, enterprise, { ...credential, status: "revoked" }],
    [order, user, enterprise, { ...credential, secret_download_available: false }],
    [order, user, enterprise, { id: credential.id, status: "active" }],
  ]) assert.equal(canDownloadApiCredential(...changes), false);
});

test("downloads a credential blob through the authenticated API client and saves it", async () => {
  const calls = [], saved = [];
  const blob = new Blob(["{\"api_key\":\"secret\"}"], { type: "application/json" });
  const api = { get: async (...args) => { calls.push(args); return { data: blob }; } };
  const result = await downloadApiCredential({ api, order, user, buyerEnterprise: enterprise, credential,
    saveBlob: (...args) => saved.push(args) });
  assert.equal(result, true);
  assert.deepEqual(calls, [[
    "/orders/order-1/api-credentials/credential-1/download",
    { responseType: "blob" },
  ]]);
  assert.equal(saved[0][0], blob);
  assert.equal(saved[0][1], "api-credential-credential-1.json");
});

test("ineligible credentials make no request and never save a blob", async () => {
  let calls = 0, saves = 0;
  const result = await downloadApiCredential({ api: { get: async () => { calls++; } }, order, user,
    buyerEnterprise: enterprise, credential: { ...credential, secret_download_available: false },
    saveBlob: () => { saves++; } });
  assert.equal(result, false);
  assert.equal(calls, 0);
  assert.equal(saves, 0);
});
