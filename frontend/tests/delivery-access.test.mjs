import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { canAccessDeliveryFiles, canAccessBuyerDelivery, deliveryAccessPlaceholder, protectedDeliveryUrl, canDownloadDeliveryFile, createDeliveryAccessSession } from "../src/deliveryAccess.js";

const order = { id: "order-1", buyer_enterprise_id: "buyer", snapshot_version: 1, delivery_method: "file", payment_status: "paid", main_status: "completed", delivery_status: "accepted" };
const user = { id: "user-1", verified_status: "verified", enterprise_role: "member", enterprise_memberships: [
  { enterprise_id: "other", role: "member", status: "active" },
  { enterprise_id: "buyer", role: "super_admin", status: "active" },
] };
const buyerEnterprise = { id: "buyer", verification_status: "verified" };
const file = { id: "file-1", name: "交付文件.csv", url: "/api/files/file-1/download?order_id=order-1", download_limit: 2, used_downloads: 0, remaining_downloads: 2 };
const access = (changes = {}) => ({ order_id: order.id, method: "file", ready: true, files: [{ ...file }], reason: "", ...changes });
function harness() {
  let context = { order: { ...order }, user: structuredClone(user), buyerEnterprise: { ...buyerEnterprise }, token: "token-1", enterpriseId: "other" };
  const states = [], calls = [], downloads = [];
  const api = { get: async url => { calls.push(url); return { data: access() }; } };
  const session = createDeliveryAccessSession({ api, getContext: () => context, onChange: state => states.push(state),
    downloadProtectedFile: async (...args) => downloads.push(args) });
  return { session, api, states, calls, downloads, context: () => context, setContext: value => { context = value; }, state: () => states.at(-1) };
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test("buyer membership determines access across enterprises and administrator role names", () => {
  for (const role of ["super_admin", "enterprise_admin", "admin"]) {
    assert.equal(canAccessDeliveryFiles(order, { ...user, enterprise_memberships: [{ enterprise_id: "buyer", role, status: "active" }] }, buyerEnterprise), true);
  }
  for (const memberships of [[], [{ enterprise_id: "other", role: "super_admin", status: "active" }],
    [{ enterprise_id: "buyer", role: "member", status: "active" }], [{ enterprise_id: "buyer", role: "admin", status: "disabled" }]]) {
    assert.equal(canAccessDeliveryFiles(order, { ...user, enterprise_role: "super_admin", enterprise_memberships: memberships }, buyerEnterprise), false);
  }
  assert.equal(canAccessDeliveryFiles(order, { ...user, enterprise_memberships: undefined, membership_status: "active", enterprise_role: "super_admin" }, buyerEnterprise), false);
  assert.equal(canAccessDeliveryFiles(order, user, { ...buyerEnterprise, id: "other" }), false);
  assert.equal(canAccessDeliveryFiles(order, { ...user, enterprise_memberships: [{ enterprise_id: "buyer", role: "admin", membership_status: "active" }] }, buyerEnterprise), true);
});

test("platform actors, inactive accounts and unverified buyers have no download entry", () => {
  for (const changes of [{ platform_role: "super_admin" }, { platform_role: "delivery_monitor" }, { platform_role: "finance_settlement" },
    { verified_status: "pending_review" }, { is_active: false }, { activation_status: "pending_activation" }]) {
    assert.equal(canAccessDeliveryFiles(order, { ...user, ...changes }, buyerEnterprise), false);
  }
  for (const enterprise of [null, { ...buyerEnterprise, verification_status: "pending_review" }, { ...buyerEnterprise, is_active: false }, { ...buyerEnterprise, status: "disabled" }]) {
    assert.equal(canAccessDeliveryFiles(order, user, enterprise), false);
  }
});

test("only new paid completed and accepted file orders pass all state gates", () => {
  assert.equal(canAccessDeliveryFiles(order, user, buyerEnterprise), true);
  for (const changes of [{ snapshot_version: 0 }, { delivery_method: "api" }, { delivery_method: "tenant_access" },
    ...["unpaid", "paying", "refunding", "refunded"].map(payment_status => ({ payment_status })),
    ...["pending_payment", "in_delivery", "cancelled", "closed", "rejected"].map(main_status => ({ main_status })),
    ...["not_started", "pending_acceptance", "exception"].map(delivery_status => ({ delivery_status }))]) {
    assert.equal(canAccessDeliveryFiles({ ...order, ...changes }, user, buyerEnterprise), false);
  }
  for (const delivery_method of ["api", "model_api"]) assert.equal(deliveryAccessPlaceholder({ delivery_method }), "查看接入信息");
  assert.equal(deliveryAccessPlaceholder({ delivery_method: "tenant_access" }), "访问应用");
  assert.equal(deliveryAccessPlaceholder({ delivery_method: "file" }), "下载文件");
  for (const delivery_method of ["training", "consulting", "custom", ""]) assert.equal(deliveryAccessPlaceholder({ delivery_method }), "-");
});

test("download URLs must reference the exact protected file and order", () => {
  assert.equal(protectedDeliveryUrl(file, order.id), "/files/file-1/download?order_id=order-1");
  for (const url of ["https://evil.test/api/files/file-1/download?order_id=order-1", "//evil.test/x", "/api/files/other/download?order_id=order-1",
    "/api/files/file-1/download?order_id=other", file.url + "&order_id=order-1", file.url + "&token=secret", file.url + "#hash"]) {
    assert.equal(protectedDeliveryUrl({ ...file, url }, order.id), "");
  }
});

test("loads delivery access and suppresses all links when not ready", async () => {
  const h = harness();
  await h.session.load(order);
  assert.deepEqual(h.calls, ["/orders/order-1/delivery-access"]);
  assert.equal(h.state().files[0].used_downloads, 0);
  h.api.get = async () => ({ data: access({ ready: false, reason: "未交付" }) });
  await h.session.load(order);
  assert.equal(h.state().reason, "未交付");
  assert.deepEqual(h.state().files, []);
  await h.session.download(file.id);
  assert.equal(h.downloads.length, 0);
});

test("403, 409 and network errors clear earlier files and allow retries", async () => {
  for (const status of [403, 409, 500, undefined]) {
    const h = harness();
    await h.session.load(order);
    h.api.get = async () => { throw { response: { status, data: {} } }; };
    await h.session.load(order);
    assert.equal(h.state().loading, false);
    assert.equal(h.state().ready, false);
    assert.deepEqual(h.state().files, []);
    assert.match(h.state().error, /重试/);
    h.api.get = async () => ({ data: access() });
    await h.session.load(order);
    assert.equal(h.state().error, "");
    assert.equal(h.state().ready, true);
  }
});

test("invalid payloads cannot expose usable download links", async () => {
  for (const changes of [{ order_id: "other" }, { method: "api" }, { ready: "true" }, { files: null },
    { files: [{ ...file, remaining_downloads: -1 }] }]) {
    const h = harness();
    h.api.get = async () => ({ data: access(changes) });
    await h.session.load(order);
    assert.equal(h.state().ready, false);
    assert.ok(h.state().error);
  }
  const h = harness();
  h.api.get = async () => ({ data: access({ files: [{ ...file, url: "https://evil.test/file" }] }) });
  await h.session.load(order);
  assert.equal(h.state().files[0].url, "");
  await h.session.download(file.id);
  assert.equal(h.downloads.length, 0);
});

test("nonce discards an older success or failure after a newer load", async () => {
  for (const fail of [false, true]) {
    const h = harness(), pending = deferred();
    h.api.get = () => pending.promise;
    const old = h.session.load(order);
    h.api.get = async () => ({ data: access({ files: [{ ...file, used_downloads: 1, remaining_downloads: 1 }] }) });
    await h.session.load(order);
    if (fail) pending.reject({ response: { status: 403 } });
    else pending.resolve({ data: access() });
    await old;
    assert.equal(h.state().files[0].used_downloads, 1);
    assert.equal(h.state().error, "");
  }
});

test("epoch and context changes discard pending responses after close, user, token or order changes", async () => {
  for (const mutate of [h => h.session.reset(), h => h.session.dispose(), h => { h.context().token = "new-token"; },
    h => { h.context().user.id = "other-user"; }, h => { h.context().enterpriseId = "buyer"; },
    h => { h.context().order.id = "other-order"; }, h => { h.context().user.enterprise_memberships[1].role = "member"; }]) {
    const h = harness(), pending = deferred();
    h.api.get = () => pending.promise;
    const loading = h.session.load(order);
    mutate(h);
    const last = h.state();
    pending.resolve({ data: access() });
    await loading;
    assert.equal(h.state(), last);
    assert.deepEqual(h.state().files, []);
  }
});

test("authenticated downloads refresh per-file counts and respect exhausted or unlimited quotas", async () => {
  const h = harness();
  await h.session.load(order);
  h.api.get = async () => ({ data: access({ files: [{ ...file, used_downloads: 2, remaining_downloads: 0 }] }) });
  await h.session.download(file.id);
  assert.equal(h.downloads[0][0], "/files/file-1/download?order_id=order-1");
  assert.equal(h.downloads[0][1], file.name);
  assert.equal(h.state().files[0].used_downloads, 2);
  await h.session.download(file.id);
  assert.equal(h.downloads.length, 1);
  assert.equal(canDownloadDeliveryFile(h.state(), h.state().files[0]), false);
  const unlimited = { ...file, download_limit: 0, remaining_downloads: null };
  h.api.get = async () => ({ data: access({ files: [unlimited] }) });
  await h.session.load(order);
  assert.equal(canDownloadDeliveryFile(h.state(), h.state().files[0]), true);
});

test("duplicate downloads are locked and a closed download cannot save or refresh stale state", async () => {
  const pending = deferred(), states = [], downloads = [];
  const context = { order, user, buyerEnterprise, token: "token" };
  let loads = 0, saved = false;
  const session = createDeliveryAccessSession({ api: { get: async () => { loads++; return { data: access() }; } },
    getContext: () => context, onChange: state => states.push(state),
    downloadProtectedFile: async (url, name, current) => { downloads.push(url); await pending.promise; saved = current(); } });
  await session.load(order);
  const downloading = session.download(file.id);
  await session.download(file.id);
  assert.equal(downloads.length, 1);
  session.reset();
  pending.resolve(); await downloading;
  assert.equal(saved, false);
  assert.equal(loads, 1);
  assert.equal(states.at(-1).orderId, "");
});

test("download failures refresh access, surface 403/409 and remain retryable", async () => {
  for (const status of [403, 409]) {
    const states = [];
    let attempts = 0, loads = 0;
    const session = createDeliveryAccessSession({ api: { get: async () => { loads++; return { data: access() }; } },
      getContext: () => ({ order, user, buyerEnterprise }), onChange: state => states.push(state),
      downloadProtectedFile: async () => { if (++attempts === 1) throw { response: { status, data: { detail: "重新核对交付" } } }; } });
    await session.load(order);
    await session.download(file.id);
    assert.equal(states.at(-1).downloading, "");
    assert.equal(states.at(-1).downloadError, "重新核对交付");
    assert.equal(loads, 2);
    await session.download(file.id);
    assert.equal(attempts, 2);
    assert.equal(states.at(-1).downloadError, "");
  }
});

test("unified buyer access uses actual verified enterprise admin membership for API and SaaS", () => {
  const apiOrder = { ...order, delivery_method: "api" };
  const saasOrder = { ...order, delivery_method: "tenant_access", product_type: "saas" };
  assert.equal(canAccessBuyerDelivery(apiOrder, user, buyerEnterprise), true);
  assert.equal(canAccessBuyerDelivery(saasOrder, user, buyerEnterprise), true);
  for (const changes of [{ platform_role: "super_admin" }, { enterprise_memberships: [{ enterprise_id: "buyer", role: "member", status: "active" }] },
    { enterprise_memberships: [{ enterprise_id: "other", role: "admin", status: "active" }] }, { verified_status: "pending" }]) {
    assert.equal(canAccessBuyerDelivery(apiOrder, { ...user, ...changes }, buyerEnterprise), false);
  }
  assert.equal(deliveryAccessPlaceholder(apiOrder), "查看接入信息");
  assert.equal(deliveryAccessPlaceholder(saasOrder), "访问应用");
  for (const changes of [{ delivery_status: "in_delivery" }, { refunded_amount: 0.01 }]) {
    assert.equal(canAccessBuyerDelivery({ ...apiOrder, ...changes }, user, buyerEnterprise), false);
  }
});

test("credential management from the list opens the order drawer before scrolling to its panel", async () => {
  const source = await readFile(fileURLToPath(new URL("../src/ConsoleApp.vue", import.meta.url)), "utf8");
  assert.match(source, /const order = currentDeliveryOrder\.value;[\s\S]*?closeDeliveryAccess\(\);[\s\S]*?selectedOrder\.value\?\.order\.id !== order\.id[\s\S]*?await openOrder\(order\)[\s\S]*?document\.querySelector\("\.api-order-panel"\)/);
  assert.match(source, /class="drawer-section api-order-panel"/);
  assert.match(source, /v-if="deliveryAccessState\.api" class="secondary-btn" @click="manageDeliveryCredentials"/);
});

test("API access returns only validated display metadata and supports retry after errors", async () => {
  const h = harness();
  const apiOrder = { ...order, delivery_method: "api" };
  h.setContext({ ...h.context(), order: apiOrder });
  h.api.get = async () => ({ data: { order_id: order.id, method: "api", ready: true, files: [], api: {
    gateway_base_path: "/gateway/api-v2", credentials_path: "/api/orders/order-1/api-credentials", shared_scope: "enterprise",
    gateway_url: "http://api.market.local:30082/gateway/api-v2", version_code: "v2", expires_at: "2030-01-01T00:00:00Z", limits: { rate_limit_per_minute: 60, daily_quota: 0, monthly_quota: 500, quota_amount: 25, total_quota: 999 },
    api_key: "must-not-survive", secret: "must-not-survive", upstream_url: "https://provider.invalid",
  } } });
  await h.session.load(apiOrder);
  assert.deepEqual(h.state().api, { gateway_base_path: "/gateway/api-v2", credentials_path: "/api/orders/order-1/api-credentials",
    shared_scope: "enterprise", gateway_url: "http://api.market.local:30082/gateway/api-v2", version_code: "v2", expires_at: "2030-01-01T00:00:00Z",
    limits: { rate_limit_per_minute: 60, daily_quota: 0, monthly_quota: 500, quota_amount: 25 } });
  assert.equal(JSON.stringify(h.state()).includes("must-not-survive"), false);
  h.api.get = async () => ({ data: { order_id: order.id, method: "api", ready: true, files: [], api: {
    gateway_base_path: "https://evil.invalid", credentials_path: "/api/orders/order-1/api-credentials", shared_scope: "enterprise",
    version_code: "v2", expires_at: "2030-01-01", limits: { rate_limit_per_minute: 60, daily_quota: 0, monthly_quota: 0 },
  } } });
  await h.session.load(apiOrder);
  assert.match(h.state().error, /无效/);
  for (const gateway_url of ["/gateway/api/v2", "javascript:alert(1)", "https://u:p@api.example.test/gateway", "https://api.example.test/gateway?key=secret", "https://api.example.test/gateway#fragment"]) {
    h.api.get = async () => ({ data: { order_id: order.id, method: "api", ready: false, reason: "凭据待配置", files: [], api: {
      gateway_base_path: "/gateway/api-v2", gateway_url, credentials_path: "/api/orders/order-1/api-credentials", shared_scope: "enterprise",
      version_code: "v2", expires_at: "2030-01-01", limits: {},
    } } });
    await h.session.load(apiOrder);
    assert.match(h.state().error, /网关地址无效/);
  }
  h.api.get = async () => ({ data: { order_id: order.id, method: "api", ready: false, reason: "凭据待配置", files: [], api: {
    gateway_base_path: "/gateway/api-v2", gateway_url: "", credentials_path: "/api/orders/order-1/api-credentials", shared_scope: "enterprise",
    version_code: "v2", expires_at: "2030-01-01", limits: { rate_limit_per_minute: 60, daily_quota: 0, monthly_quota: 0 },
  } } });
  await h.session.load(apiOrder);
  assert.equal(h.state().ready, false);
  assert.equal(h.state().api.gateway_base_path, "/gateway/api-v2");
  for (const limits of [{ rate_limit_per_minute: -1, daily_quota: 1, monthly_quota: 1 },
    { rate_limit_per_minute: 1.5, daily_quota: 1, monthly_quota: 1 }, { rate_limit_per_minute: 1, daily_quota: 1, monthly_quota: Number.NaN },
    { rate_limit_per_minute: 1, daily_quota: 1, monthly_quota: 1, quota_amount: -1 }]) {
    h.api.get = async () => ({ data: { order_id: order.id, method: "api", ready: true, files: [], api: {
      gateway_base_path: "/gateway/api-v2", credentials_path: "/api/orders/order-1/api-credentials", shared_scope: "enterprise",
      version_code: "v2", expires_at: "2030-01-01", limits,
    } } });
    await h.session.load(apiOrder);
    assert.match(h.state().error, /额度信息无效/);
  }
});

test("SaaS launch uses authenticated POST only after ready access and ignores stale or unsafe results", async () => {
  const h = harness(), calls = [], navigations = [];
  const saasOrder = { ...order, delivery_method: "tenant_access", product_type: "saas" };
  h.setContext({ ...h.context(), order: saasOrder });
  h.api.get = async () => ({ data: { order_id: order.id, method: "tenant_access", ready: true, files: [], saas: {
    subscription_id: "subscription-1", launch_path: "/api/orders/order-1/delivery-launch", expires_at: "2030-01-01T00:00:00Z",
  } } });
  h.api.post = async path => { calls.push(path); return { data: { launch_url: "https://app.example.test/start" } }; };
  const session = createDeliveryAccessSession({ api: h.api, getContext: h.context, onChange: state => h.states.push(state), onNavigate: url => navigations.push(url) });
  await session.load(saasOrder);
  await session.launch();
  assert.deepEqual(calls, ["/orders/order-1/delivery-launch"]);
  assert.deepEqual(navigations, ["https://app.example.test/start"]);
  h.api.post = async () => ({ data: { launch_url: "javascript:alert(1)" } });
  await session.launch();
  assert.match(h.state().error, /地址无效/);

  const pending = deferred();
  h.api.post = () => pending.promise;
  const launching = session.launch();
  session.reset();
  pending.resolve({ data: { launch_url: "https://stale.example.test" } });
  await launching;
  assert.deepEqual(navigations, ["https://app.example.test/start"]);
});
