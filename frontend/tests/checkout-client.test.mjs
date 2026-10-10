import test from "node:test";
import assert from "node:assert/strict";
import axios from "axios";
import { buyerEligibility, checkoutPayload, createCheckoutClient, createCheckoutSession, createPublicStorefrontClient, isMonthlyDelivery } from "../src/checkoutClient.js";

const user = { verified_status: "verified", platform_role: "" };
const enterprise = { id: "buyer-1", name: "买方企业", role: "enterprise_admin", verification_status: "verified", eligible: true, reason: "" };
const product = { id: "p-1", delivery_method: "api", delivery_category: "online", versions: [{ id: "v-1" }, { id: "v-2" }] };
const selection = { product, versionId: "v-1", enterpriseId: enterprise.id, months: 1 };
const quote = body => ({ ...body, quote_id: `snapshot-${body.product_version_id}-${body.subscription_months}`, product_version_code: "1.0", delivery_method: "api", delivery_category: "online", billing_unit: "month", unit_price: "7.35", amount: "22.05", currency: "CNY", cost: "999", upstream_secret: "private" });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

async function ready(overrides = {}) {
  let state;
  const calls = [];
  const client = { hasToken: () => true, identity: async () => ({ user }), enterprises: async () => ({ items: [enterprise] }),
    quote: async body => { calls.push(body); return quote(body); }, order: async () => ({ id: "o-1", order_no: "ORDER-1", amount: "22.05" }), ...overrides };
  const session = createCheckoutSession(client, next => { state = next; });
  await session.loadIdentity();
  return { session, calls, state: () => state };
}

test("public browsing strips inherited bearer; checkout reads market_token for each authenticated request", async () => {
  const previous = axios.defaults.headers.common.Authorization;
  axios.defaults.headers.common.Authorization = "Bearer console-global";
  const calls = [];
  const adapter = async config => { calls.push(config); return { data: {}, status: 200, statusText: "OK", headers: {}, config }; };
  try {
    const publicClient = createPublicStorefrontClient();
    publicClient.defaults.adapter = adapter;
    await publicClient.get("/products", { headers: { Authorization: "Bearer accidental" } });
    assert.equal(calls[0].headers.get("Authorization"), undefined);
    let token = "login-token";
    const checkout = createCheckoutClient({ getToken: () => token, adapter });
    await checkout.identity();
    await checkout.enterprises();
    await checkout.quote({ product_id: "p-1" });
    token = "renewed-token";
    await checkout.order({ quote_id: "snapshot" });
    assert.deepEqual(calls.slice(1).map(call => [call.url, call.headers.get("Authorization")]), [
      ["/auth/me", "Bearer login-token"], ["/storefront/buyer-enterprises", "Bearer login-token"],
      ["/orders/quote", "Bearer login-token"], ["/orders", "Bearer renewed-token"]
    ]);
    token = "";
    await assert.rejects(checkout.identity(), /请先登录/);
    assert.equal(calls.length, 5);
  } finally {
    if (previous === undefined) delete axios.defaults.headers.common.Authorization;
    else axios.defaults.headers.common.Authorization = previous;
  }
});

test("only the three online monthly methods accept 1..36 integer months; one-time sends one", () => {
  for (const method of ["api", "model_api", "tenant_access"]) {
    const item = { ...product, delivery_method: method };
    assert.equal(isMonthlyDelivery(item), true);
    for (const months of [1, 36]) assert.equal(checkoutPayload(item, "v-1", "buyer-1", months).subscription_months, months);
    for (const months of [0, 37, 1.5, "", NaN]) assert.throws(() => checkoutPayload(item, "v-1", "buyer-1", months), /1至36/);
  }
  for (const method of ["file", "offline", "manual", "training", "consulting", "custom"]) {
    assert.equal(checkoutPayload({ ...product, delivery_method: method }, "v-1", "buyer-1", 36).subscription_months, 1);
  }
  assert.equal(checkoutPayload({ ...product, delivery_category: "offline" }, "v-1", "buyer-1", 36).subscription_months, 1);
  assert.throws(() => checkoutPayload(product, "v-1", "", 1), /请选择购买企业/);
  assert.throws(() => checkoutPayload(product, "version-code-not-id", "buyer-1", 1), /可售版本/);
});

test("eligibility requires verified user, verified enterprise, privileged membership and server eligibility", () => {
  for (const role of ["super_admin", "enterprise_admin"]) assert.equal(buyerEligibility(user, { ...enterprise, role }), "");
  assert.match(buyerEligibility(user, { ...enterprise, role: "member" }), /仅企业/);
  assert.match(buyerEligibility({ ...user, verified_status: "pending" }, enterprise), /个人实名认证/);
  assert.match(buyerEligibility(user, { ...enterprise, verification_status: "pending" }), /企业实名认证/);
  assert.equal(buyerEligibility(user, { ...enterprise, eligible: false, reason: "成员已禁用" }), "成员已禁用");
  assert.match(buyerEligibility({ ...user, platform_role: "super_admin" }, enterprise), /平台角色/);
});

test("anonymous browsing sends no identity requests and no enterprise is selected automatically", async () => {
  const anonymous = await ready({ hasToken: () => false, identity: () => assert.fail("anonymous auth request") });
  assert.equal(anonymous.state().user, null);
  const context = await ready();
  assert.equal(context.calls.length, 0);
  await context.session.setSelection({ ...selection, enterpriseId: "" });
  assert.equal(context.calls.length, 0);
  assert.equal(await context.session.submit(), null);
  const member = await ready({ enterprises: async () => ({ items: [{ ...enterprise, role: "member", eligible: false }] }) });
  await member.session.setSelection(selection);
  assert.equal(member.calls.length, 0);
});

test("version, enterprise and month changes invalidate quotes immediately and stale responses cannot overwrite", async () => {
  const requests = [];
  const context = await ready({ enterprises: async () => ({ items: [enterprise, { ...enterprise, id: "buyer-2" }] }),
    quote: (body, signal) => { const pending = deferred(); requests.push({ body, signal, ...pending }); return pending.promise; } });
  const first = context.session.setSelection(selection);
  const second = context.session.setSelection({ ...selection, versionId: "v-2", months: 3, enterpriseId: "buyer-2" });
  assert.equal(requests[0].signal.aborted, true);
  assert.equal(context.state().quote, null);
  assert.equal(await context.session.submit(), null);
  requests[1].resolve(quote(requests[1].body));
  await second;
  requests[0].resolve(quote(requests[0].body));
  await first;
  assert.equal(context.state().quote.product_version_id, "v-2");
  assert.equal(context.state().quote.subscription_months, 3);
  assert.equal(context.state().quote.amount, "22.05");
  assert.equal("cost" in context.state().quote, false);
  assert.equal("upstream_secret" in context.state().quote, false);
  const third = context.session.setSelection({ ...selection, months: 4 });
  assert.equal(context.state().quote, null);
  requests[2].reject({ response: { status: 500 } });
  await third;
  assert.equal(context.state().quote, null);
});

test("order carries the server snapshot and repeated clicks create exactly one order", async () => {
  const pending = deferred();
  const orders = [];
  const context = await ready({ order: body => { orders.push(body); return pending.promise; } });
  await context.session.setSelection({ ...selection, months: 3 });
  const first = context.session.submit();
  assert.equal(await context.session.submit(), null);
  assert.deepEqual(orders, [{ product_id: "p-1", product_version_id: "v-1", buyer_enterprise_id: "buyer-1", subscription_months: 3, quote_id: "snapshot-v-1-3" }]);
  pending.resolve({ id: "o-1", order_no: "ORDER-1", amount: "22.05" });
  assert.equal((await first).id, "o-1");
  assert.equal(await context.session.submit(), null);
});

test("409 refreshes the quote without automatically ordering again", async () => {
  let quotes = 0, orders = 0;
  const context = await ready({ quote: async body => ({ ...quote(body), quote_id: `snapshot-${++quotes}` }),
    order: async () => { orders++; throw { response: { status: 409 } }; } });
  await context.session.setSelection(selection);
  assert.equal(await context.session.submit(), null);
  assert.equal(orders, 1);
  assert.equal(quotes, 2);
  assert.equal(context.state().quote.quote_id, "snapshot-2");
  assert.match(context.state().error, /确认最新报价/);
  assert.equal(context.state().submitting, false);
});

test("expired auth clears buyer and quote; malformed quote cannot enable ordering", async () => {
  const expired = await ready({ quote: async () => { throw { response: { status: 401 } }; } });
  await expired.session.setSelection(selection);
  assert.equal(expired.state().user, null);
  assert.equal(expired.state().quote, null);
  assert.match(expired.state().error, /重新登录/);
  for (const patch of [{ quote_id: undefined }, { amount: null }, { product_version_id: "wrong" }, { subscription_months: 36 }, { billing_unit: "order" }, { currency: "USD" }]) {
    const context = await ready({ quote: async body => ({ ...quote(body), ...patch }) });
    await context.session.setSelection(selection);
    assert.equal(context.state().quote, null);
    assert.equal(await context.session.submit(), null);
  }
});

test("disposed and stale failed requests do not change the active purchase state", async () => {
  const pending = deferred();
  const context = await ready({ quote: () => pending.promise });
  const request = context.session.setSelection(selection);
  await context.session.setSelection({ ...selection, enterpriseId: "" });
  pending.reject({ response: { status: 401 } });
  await request;
  assert.deepEqual(context.state().user, user);
  const before = context.state();
  context.session.dispose();
  await context.session.setSelection(selection);
  assert.equal(context.state(), before);
});
