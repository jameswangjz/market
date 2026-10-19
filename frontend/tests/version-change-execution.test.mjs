import test from "node:test";
import assert from "node:assert/strict";
import { canReadVersionChangeHistory, createVersionChangeExecution, sanitizeVersionChangeExecution, versionChangeExecutionPermissions, versionChangeReachedTerminal, versionChangeRefundStatusLabel } from "../src/versionChangeExecution.js";
import { canPreviewVersionChange } from "../src/versionChangeQuote.js";

const order = { id: "ord-1", buyer_enterprise_id: "buyer" };
const buyerAdmin = { id: "u1", verified_status: "verified", enterprise_memberships: [{ enterprise_id: "buyer", role: "enterprise_admin", status: "active" }] };
const quote = { quote_id: "q1", order_id: order.id };
const buyerEnterprise = { id: "buyer", verification_status: "verified", eligible: true };
const item = { id: "vc-1", enterprise_id: "buyer", source_order_id: "ord-1", combination_id: "combo-1", status: "pending_payment", current_version_code: "1", target_version_code: "2",
  net_difference: 20, payment_amount: 20, refund_amount: 0, currency: "CNY", payment_status: "unpaid", refund_status: "not_required",
  retry_count: 1, last_error: "private detail", created_at: "2030-01-01T00:00:00Z", updated_at: "2030-01-01T00:01:00Z" };

function harness({ response = item, postError = null, pollMs = 15 } = {}) {
  let context = { order, user: buyerAdmin, enterprise: buyerEnterprise, combinationId: "combo-1", tenantId: "tenant-1", token: "secret-session" };
  const calls = [];
  let listCalls = 0;
  const api = {
    get: async (path, config) => { listCalls++; calls.push({ method: "GET", path, config }); return { data: { items: [response] } }; },
    post: async (path, body, config) => {
      calls.push({ method: "POST", path, body, config });
      if (postError) throw postError;
      return { data: response };
    },
  };
  const quoteSession = {
    state: () => ({ quote: { ...quote }, validated: true }),
    executionPayload: key => ({ quote_token: "signed-quote-secret", idempotency_key: key }),
  };
  const changes = [];
  const execution = createVersionChangeExecution({ api, getContext: () => context, quoteSession,
    onChange: state => changes.push(state), pollMs, makeKey: () => "stable-key" });
  return { execution, calls, changes, context: value => { context = value; }, listCalls: () => listCalls };
}

test("request visibility is buyer-admin only; payment and confirmation roles are distinct", () => {
  assert.equal(versionChangeExecutionPermissions(buyerAdmin, order, null, buyerEnterprise).request, true);
  assert.equal(versionChangeExecutionPermissions(buyerAdmin, order).request, false);
  assert.equal(versionChangeExecutionPermissions({ ...buyerAdmin, enterprise_memberships: [{ enterprise_id: "buyer", role: "member", status: "active" }] }, order, null, buyerEnterprise).request, false);
  const itemPending = { ...item, payment_status: "unpaid" };
  const buyerSuper = { ...buyerAdmin, enterprise_memberships: [{ enterprise_id: "buyer", role: "super_admin", status: "active" }] };
  assert.equal(versionChangeExecutionPermissions(buyerSuper, order, itemPending, buyerEnterprise).startPayment, true);
  assert.equal(versionChangeExecutionPermissions(buyerSuper, order, { ...item, payment_status: "paying" }, buyerEnterprise).confirmPayment, false);
  assert.equal(versionChangeExecutionPermissions({ ...buyerAdmin, enterprise_memberships: [{ enterprise_id: "buyer", role: "enterprise_admin", status: "active" }] }, order, { ...item, payment_status: "paying" }, buyerEnterprise).confirmPayment, false);
  assert.equal(versionChangeExecutionPermissions({ platform_role: "finance_settlement", is_active: true }, order, { ...item, payment_status: "paying" }).confirmPayment, false);
  assert.equal(versionChangeExecutionPermissions({ platform_role: "finance_settlement", is_active: false }, order, { ...item, payment_status: "paying" }).confirmPayment, false);
  assert.equal(versionChangeExecutionPermissions({ platform_role: "finance_settlement", is_active: false }, order, { ...item, status: "exception" }).retry, false);
  assert.equal(versionChangeExecutionPermissions({ platform_role: "platform_operator", is_active: true }, order, { ...item, payment_status: "paying" }).confirmPayment, true);
  for (const platform_role of ["super_admin", "platform_operator"]) {
    for (const payment_status of ["unpaid", "paying"]) {
      const pending = { ...item, payment_status };
      assert.equal(versionChangeExecutionPermissions({ platform_role, is_active: true }, order, pending).confirmPayment, true);
      assert.equal(versionChangeExecutionPermissions({ platform_role, is_active: false }, order, pending).confirmPayment, false);
      assert.equal(versionChangeExecutionPermissions({ platform_role, activation_status: "disabled" }, order, pending).confirmPayment, false);
    }
    assert.equal(versionChangeExecutionPermissions({ platform_role }, order, { ...item, payment_status: "paid" }).confirmPayment, false);
    assert.equal(versionChangeExecutionPermissions({ platform_role }, order, { ...item, status: "completed", payment_status: "paying" }).confirmPayment, false);
  }
});

test("downgrade without payment remains pending fulfillment with no payment actions", () => {
  const buyerSuper = { ...buyerAdmin, enterprise_memberships: [{ enterprise_id: "buyer", role: "super_admin", status: "active" }] };
  for (const user of [buyerSuper, { platform_role: "super_admin" }, { platform_role: "platform_operator" }]) {
    for (const patch of [{ payment_amount: -20 }, { payment_status: "not_applicable" },
      { status: "pending_fulfillment", payment_status: "not_applicable", payment_amount: 0 }]) {
      const permissions = versionChangeExecutionPermissions(user, order, { ...item, ...patch }, buyerEnterprise);
      assert.equal(permissions.startPayment, false);
      assert.equal(permissions.confirmPayment, false);
    }
  }
});

test("history visibility is limited to API/SaaS orders and authorized active verified users", () => {
  for (const delivery_method of ["api", "model_api", "tenant_access", "saas"]) {
    assert.equal(canReadVersionChangeHistory({ ...order, snapshot_version: 1, delivery_method }, buyerAdmin, buyerEnterprise), true);
    assert.equal(canReadVersionChangeHistory({ ...order, snapshot_version: 1, delivery_method }, {
      platform_role: "super_admin", is_active: true, verified_status: "verified",
    }), true);
    assert.equal(canReadVersionChangeHistory({ ...order, snapshot_version: 1, delivery_method }, {
      platform_role: "finance_settlement", is_active: true, verified_status: "verified",
    }), true);
  }
  for (const delivery_method of ["file", "offline", "unknown"]) {
    assert.equal(canReadVersionChangeHistory({ ...order, snapshot_version: 1, delivery_method }, buyerAdmin, buyerEnterprise), false);
  }
  const snapshotOrder = { ...order, snapshot_version: 1, delivery_method_snapshot: "tenant_access" };
  assert.equal(canReadVersionChangeHistory(snapshotOrder, buyerAdmin, buyerEnterprise), true);
  assert.equal(canReadVersionChangeHistory({ ...snapshotOrder, delivery_method: "file" }, buyerAdmin, buyerEnterprise), false);
  assert.equal(canReadVersionChangeHistory({ ...snapshotOrder, snapshot_version: 0 }, buyerAdmin, buyerEnterprise), false);
  assert.equal(canReadVersionChangeHistory({ ...snapshotOrder, snapshot_version: undefined }, buyerAdmin, buyerEnterprise), false);
  assert.equal(canReadVersionChangeHistory({ ...snapshotOrder, delivery_method: "api" }, { platform_role: "platform_operator", is_active: true, verified_status: "verified" }), true);
  assert.equal(canReadVersionChangeHistory({ ...snapshotOrder, delivery_method: "api" }, { platform_role: "finance_settlement", is_active: false, verified_status: "verified" }), false);
  assert.equal(canReadVersionChangeHistory({ ...snapshotOrder, delivery_method: "api" }, { platform_role: "finance_settlement", is_active: true, verified_status: "pending" }), false);
});

test("immutable legacy source can preview when the subscription explicitly allows version changes", () => {
  const now = Date.now();
  const legacyOrder = { ...order, snapshot_version: 1, delivery_method: "api", payment_status: "paid", main_status: "completed",
    delivery_status: "accepted", product_id: "product", product_version_id: "archived-v1", product_version_code: "v1", amount: 10, paid_amount: 10 };
  const subscription = { id: "combo", available: true, status: "active", enterprise_id: "buyer", product_id: "product",
    version_id: "replacement-v1", version_code: "v1", can_version_change: true, generation: 1,
    expires_at: new Date(now + 86400000).toISOString(), terms: [{ order_id: order.id, status: "active", generation: 1,
      starts_at: new Date(now - 3600000).toISOString(), ends_at: new Date(now + 86400000).toISOString() }] };
  const enterprise = { id: "buyer", verification_status: "verified" };
  assert.equal(canPreviewVersionChange(legacyOrder, subscription, buyerAdmin, enterprise, now), true);
  assert.equal(canPreviewVersionChange(legacyOrder, { ...subscription, can_version_change: false }, buyerAdmin, enterprise, now), false);
});

test("submit sends only quote token and stable idempotency key; failed retries reuse the key", async () => {
  const h = harness({ postError: Object.assign(new Error("private backend text"), { response: { status: 503 } }) });
  await h.execution.open();
  await h.execution.submit();
  await h.execution.submit();
  const posts = h.calls.filter(call => call.method === "POST");
  assert.equal(posts.length, 2);
  assert.deepEqual(posts.map(call => call.body), [
    { quote_token: "signed-quote-secret", idempotency_key: "stable-key" },
    { quote_token: "signed-quote-secret", idempotency_key: "stable-key" },
  ]);
  assert.match(h.changes.at(-1).error, /暂时无法处理/);
  assert.doesNotMatch(JSON.stringify(h.changes), /private backend text|signed-quote-secret|secret-session/);
  h.execution.close();
});

test("concurrent submission clicks create only one request", async () => {
  let finish;
  const api = { get: async () => ({ data: { items: [] } }), post: () => new Promise(resolve => { finish = resolve; }) };
  const quoteSession = { state: () => ({ quote: { ...quote }, validated: true }), executionPayload: key => ({ quote_token: "token", idempotency_key: key }) };
  const execution = createVersionChangeExecution({ api, getContext: () => ({ order, user: buyerAdmin, enterprise: buyerEnterprise, combinationId: "combo-1" }), quoteSession, makeKey: () => "once" });
  await execution.open();
  const first = execution.submit();
  assert.equal(await execution.submit(), false);
  finish({ data: item });
  assert.equal(await first, true);
  execution.dispose();
});

test("a slow list request is not superseded by its four-second poll", async () => {
  let finish;
  let calls = 0;
  const api = {
    get: () => { calls++; return calls === 1 ? new Promise(resolve => { finish = resolve; }) : Promise.resolve({ data: { items: [] } }); },
    post: async () => ({ data: item }),
  };
  const execution = createVersionChangeExecution({ api, getContext: () => ({ order, user: buyerAdmin, enterprise: buyerEnterprise, combinationId: "combo-1" }),
    quoteSession: {}, pollMs: 10 });
  const opened = execution.open();
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(calls, 1);
  finish({ data: { items: [item] } });
  await opened;
  await new Promise(resolve => setTimeout(resolve, 25));
  assert.equal(calls, 2);
  execution.dispose();
});

test("buyers and finance cannot call payment confirmation; platform operators send the reason", async () => {
  const calls = [];
  const api = {
    get: async () => ({ data: { items: [item] } }),
    post: async (path, body) => { calls.push({ path, body }); return { data: item }; },
  };
  const buyerSuper = { ...buyerAdmin, enterprise_memberships: [{ enterprise_id: "buyer", role: "super_admin", status: "active" }] };
  let user = buyerSuper;
  const execution = createVersionChangeExecution({ api, getContext: () => ({ order, user, enterprise: buyerEnterprise, combinationId: "combo-1" }), quoteSession: {} });
  await execution.open();
  for (const actor of [buyerSuper, buyerAdmin, { platform_role: "finance_settlement", is_active: true }]) {
    user = actor;
    await execution.confirmPayment({ ...item, payment_status: "paying" }, "blocked");
  }
  assert.equal(calls.length, 0);
  user = { platform_role: "platform_operator", is_active: true };
  await execution.confirmPayment({ ...item, payment_status: "paying" }, "模拟支付凭证已核对");
  assert.deepEqual(calls[0], { path: "/version-changes/vc-1/confirm-payment", body: { reason: "模拟支付凭证已核对" } });
  assert.equal(versionChangeExecutionPermissions(buyerAdmin, order, { ...item, payment_status: "paying" }, buyerEnterprise).confirmPayment, false);
  execution.dispose();
});

test("stale list response is aborted and cannot populate a changed tenant or order", async () => {
  let context = { order, user: buyerAdmin, enterprise: buyerEnterprise, combinationId: "combo-1", tenantId: "tenant-1", token: "secret-session" };
  let finish;
  const changes = [];
  let config;
  const api = { get: (_path, options) => { config = options; return new Promise(resolve => { finish = resolve; }); }, post: async () => ({ data: item }) };
  const session = { state: () => ({ quote: null }), executionPayload: () => null };
  const execution = createVersionChangeExecution({ api, getContext: () => context, quoteSession: session, onChange: state => changes.push(state) });
  const pending = execution.open();
  context = { order: { ...order, id: "ord-2" }, user: buyerAdmin, enterprise: buyerEnterprise, combinationId: "combo-2", tenantId: "tenant-2", token: "new-token" };
  execution.close();
  finish({ data: { items: [item] } });
  await pending;
  assert.equal(config.signal.aborted, true);
  assert.equal(changes.at(-1).items.length, 0);
  execution.dispose();
});

test("pending work polls only while open and response whitelist excludes internal fields", async () => {
  const h = harness({ pollMs: 10 });
  await h.execution.open();
  await new Promise(resolve => setTimeout(resolve, 35));
  assert.ok(h.listCalls() >= 2);
  h.execution.close();
  const stoppedAt = h.listCalls();
  await new Promise(resolve => setTimeout(resolve, 35));
  assert.equal(h.listCalls(), stoppedAt);
  const clean = sanitizeVersionChangeExecution({ ...item, quote_json: "secret", prepared_json: "cost", evidence_json: "proof", plan: "private" }, order, "combo-1");
  assert.equal(clean.attempts, 1);
  assert.equal(clean.error, "处理异常，请联系平台管理员");
  assert.equal("quote_json" in clean, false);
  assert.doesNotMatch(JSON.stringify(clean), /secret|cost|proof|private detail/);
  h.execution.dispose();
});

test("unrecognized response fields and statuses fail closed", () => {
  assert.throws(() => sanitizeVersionChangeExecution({ ...item, status: "unknown" }, order, "combo-1"));
  assert.throws(() => sanitizeVersionChangeExecution({ ...item, source_order_id: "other-order" }, order, "combo-2", true));
  assert.throws(() => sanitizeVersionChangeExecution({ ...item, enterprise_id: "other" }, order, "combo-1", true));
  assert.throws(() => sanitizeVersionChangeExecution({ ...item, combination_id: "other" }, order, "combo-1", true));
  assert.throws(() => sanitizeVersionChangeExecution({ quote_token: "secret" }, order, "combo-1"));
});

test("completed refund status is presented as refunded", () => {
  assert.equal(versionChangeRefundStatusLabel("completed"), "已退款");
  assert.equal(versionChangeRefundStatusLabel("refunding"), "退款处理中");
  assert.equal(versionChangeRefundStatusLabel("unknown"), "处理中");
});

test("only a pending-to-completed or compensated transition refreshes parent subscription data", () => {
  assert.equal(versionChangeReachedTerminal([], [{ ...item, status: "completed" }]), false);
  assert.equal(versionChangeReachedTerminal([{ ...item, status: "resource_pending" }], [{ ...item, status: "completed" }]), true);
  assert.equal(versionChangeReachedTerminal([{ ...item, status: "refund_pending" }], [{ ...item, status: "compensated" }]), true);
  assert.equal(versionChangeReachedTerminal([{ ...item, status: "completed" }], [{ ...item, status: "completed" }]), false);
  assert.equal(versionChangeReachedTerminal([{ ...item, status: "exception" }], [{ ...item, status: "completed" }]), false);
});

test("other source orders are accepted only when bound to the active combination", () => {
  const siblingTerm = { ...item, source_order_id: "ord-sibling" };
  assert.equal(sanitizeVersionChangeExecution(siblingTerm, order, "combo-1", true).id, item.id);
  assert.throws(() => sanitizeVersionChangeExecution(siblingTerm, order, "combo-2", true));
  assert.throws(() => sanitizeVersionChangeExecution(siblingTerm, order, "", true));
});

test("validated quote has a reachable submit control without closing and clearing its session", async () => {
  const { readFile } = await import("node:fs/promises");
  const consoleSource = await readFile(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
  const executionSource = await readFile(new URL("../src/VersionChangeExecution.vue", import.meta.url), "utf8");
  assert.match(consoleSource, /ref="versionChangeExecutionRef"/);
  assert.match(consoleSource, /versionChangeModal\.quote && versionChangeModal\.validated/);
  assert.match(consoleSource, /@click="versionChangeExecutionRef\?\.submit\(\)"/);
  assert.match(executionSource, /if \(quoteReady\.value && !quotePending\.value\)/);
  assert.match(executionSource, /defineExpose\(\{ submit \}\)/);
  assert.match(executionSource, /<Teleport to="body">/);
});
