import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { sanitizeSubscription, canRenewSubscription, subscriptionDisplayStatus, termDisplayStatus, renewalPayload, renewalOrder, createSubscriptionSession,
  monthlyQuotaCalls, monthlyQuotaThousands } from "../src/subscriptionTerms.js";

const order = { id: "order-1", order_no: "O1", product_id: "product", product_version_id: "v1", product_version_code: "v1.0", snapshot_version: 1,
  delivery_method: "api", buyer_enterprise_id: "buyer", payment_status: "paid", main_status: "completed" };
const enterprise = { id: "buyer", verification_status: "verified", status: "active" };
const user = { id: "user", verified_status: "verified", is_active: true, enterprise_role: "enterprise_admin", membership_status: "active" };
const item = {
  available: true, id: "subscription", product_id: "product", enterprise_id: "buyer", version_id: "v1", version_code: "v1.0",
  anchor_at: "2026-01-31T00:00:00Z", generation: 2, expires_at: "2026-03-31T00:00:00Z", status: "active", can_renew: true,
  terms: [{ order_id: "order-1", order_no: "O1", version_code: "v1.0", starts_at: "2026-01-31T00:00:00Z", ends_at: "2026-02-28T00:00:00Z", months: 1, status: "active", generation: 2, start_month: 0 },
    { order_id: "order-2", order_no: "O2", version_code: "v1.0", starts_at: "2026-02-28T00:00:00Z", ends_at: "2026-03-31T00:00:00Z", months: 1, status: "scheduled", generation: 2, start_month: 1 }],
  current_period: { starts_at: "2026-01-31T00:00:00Z", ends_at: "2026-02-28T00:00:00Z", index: 0 },
  limits: { rate_limit_per_minute: 60, daily_quota: 0, monthly_quota: 1001 },
};
const created = { id: "renewal", order_no: "R1", payment_status: "unpaid", product_version_id: "v1" };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
function harness() {
  const context = { order: { ...order }, user: { ...user }, enterprise: { ...enterprise }, token: "token" };
  const calls = [], states = [];
  const api = {
    get: async url => { calls.push(["get", url]); return { data: { item } }; },
    post: async (url, body) => { calls.push(["post", url, body]); return { data: created }; },
  };
  let keys = 0;
  const session = createSubscriptionSession({ api, getContext: () => context, makeKey: () => `key-${++keys}`, onChange: state => states.push(state) });
  return { context, api, session, calls, state: () => states.at(-1) };
}

test("subscription DTO whitelists nested fields and keeps monthly limits independent of term count", () => {
  const result = sanitizeSubscription({ item: { ...item, cost: 9, secret: "hidden", terms: item.terms.map(term => ({ ...term, cost: 4 })),
    limits: { ...item.limits, cost: 1 }, current_period: { ...item.current_period, secret: "hidden" } } });
  assert.deepEqual(result, item);
  assert.equal(result.limits.monthly_quota, 1001);
  assert.doesNotMatch(JSON.stringify(result), /cost|secret|hidden/);
  assert.deepEqual(sanitizeSubscription({ item: { available: false, cost: 10, terms: item.terms } }), { available: false });
  for (const data of [null, {}, { item: {} }, { item: { ...item, terms: {} } }]) assert.throws(() => sanitizeSubscription(data));
  const malformed = sanitizeSubscription({ item: { ...item, expires_at: "invalid", limits: { monthly_quota: -1, daily_quota: "10" } } });
  assert.equal(malformed.expires_at, null);
  assert.equal(malformed.limits.monthly_quota, null);
  assert.equal(malformed.limits.daily_quota, null);
});

test("only verified active buyer administrators renew, with no platform override", () => {
  for (const role of ["super_admin", "enterprise_admin"]) assert.equal(canRenewSubscription(order, item, { ...user, enterprise_role: role }, enterprise), true);
  for (const changes of [{ enterprise_role: "member" }, { verified_status: "pending" }, { membership_status: "disabled" },
    { is_active: false }, { activation_status: "pending_activation" }, { platform_role: "super_admin" },
    { enterprise_memberships: [] }, { enterprise_memberships: [{ enterprise_id: "buyer", role: "super_admin", status: "disabled" }] }]) {
    assert.equal(canRenewSubscription(order, item, { ...user, ...changes }, enterprise), false);
  }
  assert.equal(canRenewSubscription(order, item, { ...user, enterprise_role: "member", enterprise_memberships: [{ enterprise_id: "buyer", role: "enterprise_admin", status: "active" }] }, enterprise), true);
  for (const changes of [{ id: "provider" }, { status: "disabled" }, { is_active: false }, { verification_status: "pending" }]) {
    assert.equal(canRenewSubscription(order, item, user, { ...enterprise, ...changes }), false);
  }
});

test("unpaid, unavailable, revoked and mismatched entitlements cannot renew", () => {
  for (const payment_status of ["unpaid", "paying", "refunded", "refunding"]) assert.equal(canRenewSubscription({ ...order, payment_status }, item, user, enterprise), false);
  for (const changes of [{ id: null }, { terms: [] }, { status: "revoked" }, { status: "closed" }, { expires_at: null },
    { enterprise_id: "other" }, { product_id: "other" }, { terms: [{ ...item.terms[0], status: "revoked" }] }]) {
    assert.equal(canRenewSubscription(order, { ...item, ...changes }, user, enterprise), false);
  }
  assert.equal(canRenewSubscription(order, { ...item, status: "expired" }, user, enterprise), true);
});

test("actual expired DTO retains the combination, terms and renewal eligibility without active limits", () => {
  const subscription = sanitizeSubscription({ item: { ...item, available: false, status: "expired", current_period: null, limits: null } });
  assert.equal(subscription.id, item.id);
  assert.deepEqual(subscription.terms, item.terms);
  assert.equal(subscription.limits, null);
  assert.equal(subscriptionDisplayStatus(subscription, Date.parse("2026-03-31T00:00:00Z")), "expired");
  assert.equal(subscriptionDisplayStatus({ ...subscription, status: "active" }, Date.parse("2026-03-30T00:00:00Z")), "active");
  assert.equal(canRenewSubscription(order, subscription, user, enterprise), true);
  const { product_id, ...actualOrderDTO } = order;
  assert.equal(canRenewSubscription(actualOrderDTO, subscription, user, enterprise), true);
  const active = sanitizeSubscription({ item: { ...item, limits: { ...item.limits, quota_amount: 9000 } } });
  assert.equal("quota_amount" in active.limits, false);
  for (const changes of [{ product_version_id: "v2" }, { product_version_code: "v0" }, { snapshot_version: 0 },
    { delivery_method: "file" }, { refunded_amount: 1 }, { paid_amount: 5, amount: 10 }]) {
    assert.equal(canRenewSubscription({ ...order, ...changes }, subscription, user, enterprise), false);
  }
  assert.equal(canRenewSubscription(order, { ...subscription, generation: 0 }, user, enterprise), false);
  const at = Date.parse("2026-02-10T00:00:00Z");
  assert.equal(termDisplayStatus(item.terms[0], at), "active");
  assert.equal(termDisplayStatus({ ...item.terms[1], status: "active" }, at), "scheduled");
  assert.equal(termDisplayStatus(item.terms[0], Date.parse(item.terms[0].ends_at)), "expired");
  assert.equal(termDisplayStatus({ ...item.terms[0], status: "revoked" }, at), "revoked");
});

test("renewal requires the explicit current term generation and a strict server can_renew flag", async () => {
  for (const generation of [1, 3, null, undefined, "2"]) {
    const historical = { ...item, terms: [{ ...item.terms[0], generation }] };
    assert.equal(canRenewSubscription(order, historical, user, enterprise), false);
  }
  for (const can_renew of [false, undefined, null, "true", 1]) {
    const denied = sanitizeSubscription({ item: { ...item, can_renew } });
    assert.equal(denied.can_renew, false);
    assert.equal(canRenewSubscription(order, denied, user, enterprise), false);
  }
  const missing = sanitizeSubscription({ item: { ...item, terms: [{ ...item.terms[0], generation: undefined, start_month: undefined }] } });
  assert.equal(missing.terms[0].generation, null);
  assert.equal(missing.terms[0].start_month, null);
  assert.equal(canRenewSubscription(order, missing, user, enterprise), false);
  assert.equal(canRenewSubscription(order, { ...item, anchor_at: "2026-02-01T00:00:00Z" }, user, enterprise), true);
  const s = harness();
  s.api.get = async () => ({ data: { item: { ...item, terms: [{ ...item.terms[0], generation: 1 }] } } });
  await s.session.load(order); s.session.open();
  assert.equal(s.state().modal, null);
  assert.equal(await s.session.submit(), null);
  assert.equal(s.calls.length, 0);
});

test("renewal validates duration and creates only a same-version unpaid DTO with no economics", () => {
  for (const value of [1, "12", 36]) assert.equal(renewalPayload(value, "key").subscription_months, Number(value));
  for (const value of [0, 37, -1, 1.5, "", " ", null, true, Infinity, "bad"]) assert.throws(() => renewalPayload(value, "key"));
  assert.throws(() => renewalPayload(1, ""));
  assert.deepEqual(renewalOrder({ ...created, cost: 99 }, order, item), { id: "renewal", order_no: "R1" });
  assert.throws(() => renewalOrder({ item: created }, order, item));
  for (const changes of [{ id: order.id }, { payment_status: "paid" }, { product_version_id: "v2" }, { product_id: "other" }, { buyer_enterprise_id: "other" }, { id: null }]) {
    assert.throws(() => renewalOrder({ ...created, ...changes }, order, item));
  }
});

test("duplicate submissions post once and ambiguous retries retain both key and months", async () => {
  const s = harness(); await s.session.load(order); s.session.open(); s.session.setMonths(12);
  const response = deferred();
  s.api.post = async (url, body) => { s.calls.push(["post", url, body]); return response.promise; };
  const pending = s.session.submit();
  assert.equal(await s.session.submit(), null);
  s.session.close(); s.session.setMonths(24);
  assert.equal(s.state().modal.submitting, true);
  response.reject(new Error("network")); await pending;
  assert.equal(s.state().modal.submitting, false);
  assert.ok(s.state().modal.error);
  const body = s.calls.at(-1)[2];
  s.api.post = async (url, retryBody) => { assert.deepEqual(retryBody, body); return { data: created }; };
  assert.deepEqual(await s.session.submit(), { id: "renewal", order_no: "R1" });
  assert.equal(s.state().modal, null);
  assert.deepEqual(body, { subscription_months: 12, idempotency_key: "key-1" });
  s.session.open(); assert.equal(s.state().modal.key, "key-2");
});

test("invalid duration and changed permissions block requests at submission time", async () => {
  const s = harness(); await s.session.load(order); s.session.open(); s.session.setMonths(37);
  assert.equal(await s.session.submit(), null); assert.ok(s.state().modal.error);
  s.session.setMonths(2); s.context.user.membership_status = "disabled";
  assert.equal(await s.session.submit(), null);
  assert.equal(s.calls.filter(([method]) => method === "post").length, 0);
});

test("stale subscription successes and failures never overwrite a newer order or refresh", async () => {
  for (const fail of [false, true]) {
    const s = harness(), old = deferred(); s.api.get = async () => old.promise;
    const pending = s.session.load(order);
    s.context.order = { ...order, id: "new" };
    s.api.get = async () => ({ data: { item: { available: false } } });
    await s.session.load(s.context.order);
    if (fail) old.reject(new Error("late")); else old.resolve({ data: { item } });
    await pending;
    assert.equal(s.state().orderId, "new");
    assert.deepEqual(s.state().item, { available: false });
    assert.equal(s.state().error, "");
  }
  const s = harness(), first = deferred(); s.api.get = async () => first.promise;
  const pending = s.session.load(order); s.api.get = async () => ({ data: { item: { available: false } } });
  await s.session.load(order); first.resolve({ data: { item } }); await pending;
  assert.deepEqual(s.state().item, { available: false });
});

test("switching identity or order during renewal cannot navigate and retains the global submit guard", async () => {
  for (const change of [s => { s.context.enterprise.id = "other"; }, s => { s.context.token = "new-token"; },
    s => { s.context.user.membership_status = "disabled"; }, s => { s.context.order = { ...order, id: "other" }; s.session.reset(); }]) {
    const s = harness(); await s.session.load(order); s.session.open();
    const response = deferred(); s.api.post = async () => response.promise;
    const pending = s.session.submit(); change(s); s.session.open();
    response.resolve({ data: created }); assert.equal(await pending, null);
  }
});

test("load failures expose retry state, unavailable orders never open, and disposal suppresses pending results", async () => {
  const s = harness(); s.api.get = async () => { throw new Error("failure"); };
  await s.session.load(order); assert.ok(s.state().error); assert.equal(s.state().loading, false);
  s.session.open(); assert.equal(s.state().modal, null);
  s.api.get = async () => ({ data: { item: { available: false } } }); await s.session.load(order);
  s.session.open(); assert.equal(s.state().modal, null);
  const pending = deferred(); s.api.get = async () => pending.promise;
  const task = s.session.load(order); s.session.dispose(); pending.resolve({ data: { item } }); await task;
  assert.equal(s.state().item, null);
});

test("monthly quota conversion preserves exact historical calls and requires nonnegative whole calls", () => {
  for (const calls of [0, 1, 999, 1000, 1001, 1234567, Number.MAX_SAFE_INTEGER]) {
    assert.equal(monthlyQuotaCalls(monthlyQuotaThousands(calls)), calls);
  }
  assert.equal(monthlyQuotaCalls("1"), 1000);
  assert.equal(monthlyQuotaCalls("1.001"), 1001);
  assert.equal(monthlyQuotaThousands(1234), "1.234");
  for (const value of ["", null, "-1", "1.0001", "1e3", "Infinity", "NaN", "9007199254740992"]) assert.throws(() => monthlyQuotaCalls(value));
});

const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
test("quota input rejects invalid edits without changing historical calls or legacy metadata", () => {
  const state = { monthlyQuotaCalls };
  vm.createContext(state);
  vm.runInContext(source.slice(source.indexOf("function updateVersionMonthlyQuota("), source.indexOf("function addProductVersion()")), state);
  const version = { monthly_quota: 1234, quota_unit: "10000", quota_amount: 7 };
  let error = "";
  const input = value => ({ target: { value, setCustomValidity: message => { error = message; } } });
  state.updateVersionMonthlyQuota(version, input("1.0001"));
  assert.ok(error); assert.equal(version.monthly_quota, 1234);
  state.updateVersionMonthlyQuota(version, input("1.234"));
  assert.equal(error, ""); assert.equal(version.monthly_quota, 1234);
  state.updateVersionMonthlyQuota(version, input("2"));
  assert.deepEqual(version, { monthly_quota: 2000, quota_unit: "10000", quota_amount: 7 });
});

test("order-detail fetch ignores older results and drawer closure invalidates pending requests", async () => {
  const pending = deferred(), calls = [];
  const state = {
    orderDetailRequest: 0, selectedOrder: { value: null }, user: { value: user }, enterprise: { value: enterprise }, token: { value: "token" },
    subscriptionSession: { reset() {}, load: async value => calls.push(value.id) },
    deliveryAttachmentDescription: { value: "" }, saasOrderState: { value: {} }, apiOrderState: { value: {} }, apiCredentialReveal: { value: null },
    orderProductFiles: { value: {} }, orderDeliveryAttachments: { value: {} }, isOfflineFulfillmentOrder: () => false,
    api: { get: async url => url === "/orders/order-1" ? pending.promise : { data: { order: { ...order, id: "new", payment_status: "unpaid" } } } },
  };
  vm.createContext(state);
  vm.runInContext(source.slice(source.indexOf("async function openOrder(order)"), source.indexOf("async function downloadOrderProductFile(file)")) +
    source.slice(source.indexOf("function closeOrder()"), source.indexOf("const orderProductFiles =")), state);
  const first = state.openOrder(order);
  await state.openOrder({ id: "new" });
  pending.resolve({ data: { order } }); await first;
  assert.equal(state.selectedOrder.value.order.id, "new"); assert.deepEqual(calls, ["new"]);
  const late = deferred(); state.api.get = async () => late.promise;
  const task = state.openOrder(order); state.closeOrder(); late.resolve({ data: { order } }); await task;
  assert.equal(state.selectedOrder.value, null);
});

test("renewal integration refreshes orders and opens the created unpaid order without payment calls", async () => {
  const calls = [], state = {
    orderActionSubmitting: { value: false }, renewalProcessing: { value: false }, selectedOrder: { value: { order } }, user: { value: user }, enterprise: { value: enterprise }, token: { value: "token" },
    subscriptionSession: { submit: async () => created }, notify: () => {}, refreshData: async () => calls.push("refresh"), openOrder: async next => calls.push(next.id),
  };
  vm.createContext(state);
  vm.runInContext(source.slice(source.indexOf("async function submitRenewal()"), source.indexOf("function closeOrder()")), state);
  await state.submitRenewal(); assert.deepEqual(calls, ["refresh", "renewal"]);
  state.refreshData = async () => { state.selectedOrder.value = null; };
  await state.submitRenewal(); assert.deepEqual(calls, ["refresh", "renewal"]);
  assert.doesNotMatch(source, /saasOrderAction\('(?:renew|change)'\)|\/change-version/);
  assert.match(source, /每订阅月配额（千次）/);
  assert.doesNotMatch(source, /v-model="version.quota_unit"|v-model.number="version.quota_amount"/);
});
