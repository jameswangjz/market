import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { ref, computed, watch } from "vue";
import { sanitizeSubscription, serverTimeNow } from "../src/subscriptionTerms.js";
import { canPreviewVersionChange, sanitizeVersionChangeOptions, versionChangeQuotePayload, sanitizeVersionChangeQuote,
  versionChangeError, createVersionChangeSession } from "../src/versionChangeQuote.js";

const at = Date.parse("2030-02-15T00:00:00Z");
const order = { id: "order-1", buyer_enterprise_id: "buyer", product_id: "product", snapshot_version: 1,
  delivery_method: "api", payment_status: "paid", main_status: "completed", delivery_status: "accepted",
  product_version_id: "v1", product_version_code: "v1", amount: 100, paid_amount: 100 };
const user = { id: "user", verified_status: "verified", enterprise_role: "member", enterprise_memberships: [
  { enterprise_id: "other", role: "member", status: "active" },
  { enterprise_id: "buyer", role: "enterprise_admin", status: "active" },
] };
const buyerEnterprise = { id: "buyer", verification_status: "verified" };
const subscription = { id: "combination", enterprise_id: "buyer", product_id: "product", version_id: "v1", version_code: "v1",
  available: true, status: "active", generation: 1, can_renew: false, expires_at: "2030-04-01T00:00:00Z", terms: [
    { order_id: "order-1", status: "active", generation: 1, starts_at: "2030-02-01T00:00:00Z", ends_at: "2030-03-01T00:00:00Z" },
    { order_id: "order-2", status: "active", generation: 1, starts_at: "2030-03-01T00:00:00Z", ends_at: "2030-04-01T00:00:00Z" },
  ] };
const options = { order_id: order.id, combination_id: subscription.id, delivery_method: "api",
  current_version_id: "v1", current_version_code: "v1", expires_at: subscription.expires_at, versions: [
    { id: "v1", version_code: "v1", monthly_price: 100, compatibility_mode: "compatible", compatibility_notes: "" },
    { id: "v2", version_code: "v2", monthly_price: 200, compatibility_mode: "compatible", compatibility_notes: "" },
    { id: "v3", version_code: "v3", monthly_price: 50, compatibility_mode: "breaking", compatibility_notes: "接口变更" },
  ] };
function quote(changes = {}) {
  return { order_id: order.id, combination_id: subscription.id, quote_id: "quote-1", quote_token: "private-signed-token",
    created_at: "2030-02-15T00:00:00Z", expires_at: "2030-02-15T00:05:00Z",
    current_version_code: "v1", target_version_code: "v2", action: "upgrade",
    payment_amount: 150, refund_amount: 0, net_difference: 150, currency: "CNY",
    compatibility: { mode: "compatible", notes: "", confirmation_required: false }, execution_ready: false,
    segments: subscription.terms.map((term, index) => ({
      order_id: term.order_id, order_no: "ORD-" + (index + 1), version_code: "v1", subscription_months: 1,
      starts_at: term.starts_at, ends_at: term.ends_at,
      remaining_starts_at: index === 0 ? "2030-02-15T00:00:00Z" : term.starts_at, remaining_ends_at: term.ends_at,
      remaining_ratio: index === 0 ? "0.5" : "1.0", original_amount: 100, original_remaining_amount: index === 0 ? 50 : 100,
      target_full_amount: 200, target_remaining_amount: index === 0 ? 100 : 200, net_difference: index === 0 ? 50 : 100,
    })), ...changes };
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function harness() {
  const context = structuredClone({ order, subscription, user, buyerEnterprise, token: "session-token", enterpriseId: "other" });
  let clock = at;
  const states = [], calls = [];
  const api = {
    get: async path => { calls.push({ method: "GET", path }); return { data: structuredClone(options) }; },
    post: async (path, body) => { calls.push({ method: "POST", path, body }); return { data: quote() }; },
  };
  const session = createVersionChangeSession({ api, getContext: () => context, onChange: state => states.push(state), now: () => clock });
  return { context, states, calls, api, session, state: () => states.at(-1), setTime: value => { clock = value; } };
}

test("verified buyer administrators use actual multi-enterprise memberships independently of renewal eligibility", () => {
  for (const role of ["super_admin", "enterprise_admin", "admin"]) {
    const actor = { ...user, enterprise_memberships: [{ enterprise_id: "buyer", role, membership_status: "active" }] };
    assert.equal(canPreviewVersionChange(order, subscription, actor, buyerEnterprise, at), true);
  }
  for (const memberships of [undefined, [], [{ enterprise_id: "other", role: "super_admin", status: "active" }],
    [{ enterprise_id: "buyer", role: "member", status: "active" }], [{ enterprise_id: "buyer", role: "admin", status: "disabled" }]]) {
    assert.equal(canPreviewVersionChange(order, subscription, { ...user, enterprise_role: "super_admin", membership_status: "active", enterprise_memberships: memberships }, buyerEnterprise, at), false);
  }
});

test("platform, unverified, inactive, unrelated buyers and invalid paid monthly terms cannot open", () => {
  for (const actor of [{ ...user, platform_role: "super_admin" }, { ...user, platform_role: "finance_settlement" },
    { ...user, verified_status: "pending_review" }, { ...user, is_active: false }, { ...user, activation_status: "pending_activation" }]) {
    assert.equal(canPreviewVersionChange(order, subscription, actor, buyerEnterprise, at), false);
  }
  for (const buyer of [null, { ...buyerEnterprise, id: "other" }, { ...buyerEnterprise, verification_status: "pending_review" },
    { ...buyerEnterprise, is_active: false }, { ...buyerEnterprise, status: "disabled" }]) {
    assert.equal(canPreviewVersionChange(order, subscription, user, buyer, at), false);
  }
  for (const changes of [{ snapshot_version: 0 }, { delivery_method: "file" }, { payment_status: "unpaid" }, { payment_status: "refunded" },
    { main_status: "cancelled" }, { main_status: "closed" }, { refunded_amount: 1 }, { paid_amount: 99 },
    { product_version_id: "old" }, { product_version_code: "old" }, { delivery_status: "awaiting_start" }]) {
    assert.equal(canPreviewVersionChange({ ...order, ...changes }, subscription, user, buyerEnterprise, at), false);
  }
  for (const changes of [{ id: "" }, { enterprise_id: "other" }, { product_id: "other" }, { available: false }, { status: "expired" },
    { expires_at: "2030-02-15T00:00:00Z" }, { generation: undefined }, { generation: 2 }, { terms: [] },
    { terms: [{ ...subscription.terms[0], order_id: "other" }] }, { terms: [{ ...subscription.terms[0], status: "revoked" }] },
    { terms: [{ ...subscription.terms[0], ends_at: "2030-02-14T00:00:00Z" }] }]) {
    assert.equal(canPreviewVersionChange(order, { ...subscription, ...changes }, user, buyerEnterprise, at), false);
  }
  for (const delivery_method of ["api", "model_api", "tenant_access"]) {
    assert.equal(canPreviewVersionChange({ ...order, delivery_method }, subscription, user, buyerEnterprise, at), true);
  }
  assert.equal(canPreviewVersionChange({ ...order, id: "order-2" }, subscription, user, buyerEnterprise, at), true);
});

test("options whitelist versions and reject wrong binding, duplicate IDs, bad prices and compatibility", () => {
  const clean = sanitizeVersionChangeOptions({ ...options, cost: 999, versions: options.versions.map(v => ({ ...v, monthly_cost: 90, secret: "private" })) }, order, subscription, at);
  assert.equal("cost" in clean, false);
  assert.equal("monthly_cost" in clean.versions[0], false);
  assert.equal("secret" in clean.versions[0], false);
  for (const changes of [{ order_id: "other" }, { combination_id: "other" }, { delivery_method: "file" },
    { current_version_id: "other" }, { current_version_code: "other" }, { expires_at: "bad" },
    { expires_at: "2030-02-14T00:00:00Z" }, { versions: null }, { versions: [options.versions[0], options.versions[0]] },
    ...[{ monthly_price: "200" }, { monthly_price: NaN }, { monthly_price: -1 }]
      .map(change => ({ versions: [{ ...options.versions[1], ...change }] }))]) {
    assert.throws(() => sanitizeVersionChangeOptions({ ...options, ...changes }, order, subscription, at));
  }
  assert.deepEqual(sanitizeVersionChangeOptions({ ...options, versions: [] }, order, subscription, at).versions, []);
});

test("API options normalize legacy breaking and invalid compatibility while SaaS remains strict", () => {
  const legacy = { ...options, versions: [{ ...options.versions[2], compatibility_mode: "legacy-invalid", compatibility_notes: "old API note" }] };
  assert.deepEqual(sanitizeVersionChangeOptions(legacy, order, subscription, at).versions[0], {
    id: "v3", version_code: "v3", monthly_price: 50, compatibility_mode: "compatible", compatibility_notes: "",
  });
  const saasOrder = { ...order, delivery_method: "tenant_access" };
  assert.throws(() => sanitizeVersionChangeOptions({ ...legacy, delivery_method: "tenant_access" }, saasOrder, subscription, at));
  assert.throws(() => sanitizeVersionChangeOptions({ ...options, delivery_method: "tenant_access",
    versions: [{ ...options.versions[1], compatibility_notes: null }] }, saasOrder, subscription, at));
});

test("API payload ignores legacy breaking flags while SaaS breaking still requires confirmation", () => {
  assert.deepEqual(versionChangeQuotePayload(options, "v2", false, at), { target_version_id: "v2", compatibility_confirmed: false });
  assert.deepEqual(versionChangeQuotePayload(options, "v3", false, at), { target_version_id: "v3", compatibility_confirmed: false });
  const saasOptions = { ...options, delivery_method: "tenant_access" };
  assert.deepEqual(versionChangeQuotePayload(saasOptions, "v3", true, at), { target_version_id: "v3", compatibility_confirmed: true });
  assert.throws(() => versionChangeQuotePayload(saasOptions, "v3", false, at));
  for (const [target, confirmed] of [["v1", false], ["missing", true], ["v2", "true"]]) {
    assert.throws(() => versionChangeQuotePayload(options, target, confirmed, at));
  }
  assert.throws(() => versionChangeQuotePayload({ ...options, expires_at: "2030-02-14T00:00:00Z" }, "v2", false, at));
});

test("quote whitelist strips every cost and token from public and nested state", () => {
  const data = quote({ cost_amount: 42, secret: "private", compatibility: { mode: "compatible", notes: "", confirmation_required: false, cost: 5 } });
  data.segments = data.segments.map(segment => ({ ...segment, original_cost: 12, target_cost: 30, quote_token: "hidden" }));
  const clean = sanitizeVersionChangeQuote(data, options, "v2", false, at);
  assert.equal(clean.segments.length, 2);
  assert.equal(clean.net_difference, 150);
  assert.equal(clean.execution_ready, false);
  assert.doesNotMatch(JSON.stringify(clean), /cost|quote_token|hidden|private/);
});

test("upgrade, downgrade and no-payment public previews enforce net amount and action consistency", () => {
  for (const [action, multiplier, payment_amount, refund_amount, net_difference] of [
    ["upgrade", 2, 150, 0, 150], ["downgrade", 0.5, 0, 75, -75], ["no_payment", 1, 0, 0, 0],
  ]) {
    const data = quote({ action, payment_amount, refund_amount, net_difference });
    data.segments = data.segments.map(s => ({ ...s, target_full_amount: s.original_amount * multiplier,
      target_remaining_amount: s.original_remaining_amount * multiplier, net_difference: s.original_remaining_amount * (multiplier - 1) }));
    assert.equal(sanitizeVersionChangeQuote(data, options, "v2", false, at).action, action);
  }
  for (const changes of [{ net_difference: 200 }, { refund_amount: 1 }, { action: "downgrade" }, { action: "no_payment" },
    { payment_amount: "150" }, { refund_amount: -1 }, { net_difference: Infinity }]) {
    assert.throws(() => sanitizeVersionChangeQuote(quote(changes), options, "v2", false, at));
  }
});

test("quote rejects malformed dates, ratio, segment arithmetic, bound identity and execution flag", () => {
  for (const changes of [{ order_id: "other" }, { combination_id: "other" }, { quote_id: "" }, { quote_token: "" },
    { current_version_code: "wrong" }, { target_version_code: "wrong" }, { currency: "usd" }, { currency: "USDT" }, { execution_ready: true },
    { created_at: "bad" }, { expires_at: "2030-02-15T00:06:00Z" }, { expires_at: "2030-02-15T00:00:00Z" },
    { segments: [] }, { compatibility: { mode: "unknown", notes: "", confirmation_required: false } }]) {
    assert.throws(() => sanitizeVersionChangeQuote(quote(changes), options, "v2", false, at));
  }
  for (const changes of [{ subscription_months: 0 }, { original_amount: "100" }, { remaining_ratio: 0.5 },
    { remaining_ratio: "1.01" }, { remaining_ratio: "-1" }, { net_difference: 100 },
    { remaining_starts_at: "2030-01-01T00:00:00Z" }, { remaining_ends_at: "2030-05-01T00:00:00Z" },
    { target_remaining_amount: 500 }, { order_no: "" }]) {
    const data = quote(); data.segments[0] = { ...data.segments[0], ...changes };
    assert.throws(() => sanitizeVersionChangeQuote(data, options, "v2", false, at));
  }
  const duplicate = quote(); duplicate.segments.push(duplicate.segments[0]);
  assert.throws(() => sanitizeVersionChangeQuote(duplicate, options, "v2", false, at));
  const scientific = quote(); scientific.segments[0].remaining_ratio = "5E-1";
  assert.equal(sanitizeVersionChangeQuote(scientific, options, "v2", false, at).segments[0].remaining_ratio, "5E-1");
});

test("five-minute TTL and an earlier current segment end both invalidate quotes", () => {
  assert.throws(() => sanitizeVersionChangeQuote(quote(), options, "v2", false, at + 300000), /过期/);
  const data = quote();
  data.segments[0].ends_at = data.segments[0].remaining_ends_at = "2030-02-15T00:02:00Z";
  assert.throws(() => sanitizeVersionChangeQuote(data, options, "v2", false, at));
  data.expires_at = "2030-02-15T00:02:00Z";
  assert.equal(sanitizeVersionChangeQuote(data, options, "v2", false, at).expires_at, data.expires_at);
});

test("three uppercase currency codes are retained without changing global money formatting", () => {
  for (const currency of ["CNY", "USD", "EUR"]) {
    assert.equal(sanitizeVersionChangeQuote(quote({ currency }), options, "v2", false, at).currency, currency);
  }
  for (const currency of [null, 123, "usd", "US", "USDT", " CNY"]) {
    assert.throws(() => sanitizeVersionChangeQuote(quote({ currency }), options, "v2", false, at));
  }
});

test("positive and negative 20-hour client skew use server receipt clocks; five minutes elapsed expires the same quote", async () => {
  for (const skew of [-20, 20]) {
    const h = harness(), clientAt = at + skew * 3600000;
    h.setTime(clientAt);
    h.context.subscription = sanitizeSubscription({ item: { ...subscription, server_time: new Date(at).toISOString() } }, clientAt);
    const serverOptions = { ...options, server_time: new Date(at).toISOString(), client_received_at: 0 };
    h.api.get = async () => ({ data: serverOptions });
    assert.equal(canPreviewVersionChange(order, h.context.subscription, user, buyerEnterprise, clientAt), true);
    await h.session.open(); h.session.setTarget("v2"); await h.session.quote();
    assert.ok(h.state().quote);
    assert.equal(h.state().options.client_received_at, clientAt);
    h.setTime(clientAt + 240000);
    await h.session.validate();
    assert.equal(h.state().validated, true);
    assert.equal(h.state().quote.created_at, quote().created_at);
    assert.deepEqual(h.calls.at(-1).body, { quote_token: "private-signed-token" });
    h.setTime(clientAt + 300000); h.session.expire();
    assert.equal(h.state().quote, null); assert.match(h.state().error, /过期/);
    const count = h.calls.length; await h.session.validate(); assert.equal(h.calls.length, count);
  }
});

test("subscription server time is a fallback when options omit clock; malformed server time fails closed", () => {
  const clientAt = at + 20 * 3600000;
  const sub = { ...subscription, server_time: new Date(at).toISOString(), client_received_at: clientAt };
  const fallback = sanitizeVersionChangeOptions(options, order, sub, clientAt + 60000);
  assert.equal(serverTimeNow(fallback, clientAt + 60000), at + 60000);
  assert.ok(sanitizeVersionChangeQuote(quote(), fallback, "v2", false, clientAt + 60000));
  assert.throws(() => sanitizeVersionChangeQuote(quote(), fallback, "v2", false, clientAt + 300000), /过期/);
  assert.throws(() => sanitizeVersionChangeOptions({ ...options, server_time: "bad" }, order, sub, clientAt));
  const expiring = { ...sub, expires_at: new Date(at + 120000).toISOString() };
  assert.equal(canPreviewVersionChange(order, expiring, user, buyerEnterprise, clientAt + 120000), false);
});

test("session obtains and validates quotes without exposing signed token or providing execution API", async () => {
  const h = harness();
  await h.session.open(); h.session.setTarget("v2");
  await h.session.quote();
  assert.equal(h.state().quote.net_difference, 150);
  assert.equal(h.state().validated, false);
  assert.doesNotMatch(JSON.stringify(h.states), /private-signed-token|quote_token/);
  await h.session.validate();
  assert.equal(h.state().validated, true);
  assert.deepEqual(h.calls.map(c => c.path), ["/orders/order-1/version-change-options", "/orders/order-1/version-change-quote", "/orders/order-1/version-change-quote/validate"]);
  assert.deepEqual(h.calls[1].body, { target_version_id: "v2", compatibility_confirmed: false });
  assert.deepEqual(h.calls[2].body, { quote_token: "private-signed-token" });
  assert.equal("execute" in h.session, false);
});

test("breaking SaaS target needs confirmation; target and confirmation changes invalidate quote", async () => {
  const h = harness(); h.context.order.delivery_method = "tenant_access";
  h.api.get = async path => { h.calls.push({ method: "GET", path }); return { data: { ...options, delivery_method: "tenant_access" } }; };
  await h.session.open(); h.session.setTarget("v3");
  await h.session.quote(); assert.equal(h.calls.length, 1); assert.match(h.state().error, /不兼容/);
  h.session.setConfirmed(true);
  h.api.post = async () => ({ data: quote({ target_version_code: "v3", compatibility: { mode: "breaking", notes: "接口变更", confirmation_required: true } }) });
  await h.session.quote(); assert.ok(h.state().quote);
  h.session.setConfirmed(false); assert.equal(h.state().quote, null);
  await h.session.validate(); assert.equal(h.state().validated, false);
  h.session.setConfirmed(true); await h.session.quote();
  h.session.setTarget("v2"); assert.equal(h.state().confirmed, false); assert.equal(h.state().quote, null);
});

test("duplicate open, quote and validate clicks send a single request", async () => {
  const h = harness(), load = deferred();
  h.api.get = async path => { h.calls.push({ path }); return load.promise; };
  const opening = h.session.open(); await h.session.open();
  assert.equal(h.calls.length, 1);
  load.resolve({ data: options }); await opening; h.session.setTarget("v2");
  const pending = deferred(); let posts = 0;
  h.api.post = async () => { posts++; return pending.promise; };
  const quoting = h.session.quote(); await h.session.quote(); assert.equal(posts, 1);
  pending.resolve({ data: quote() }); await quoting;
  const validation = deferred(); h.api.post = async () => { posts++; return validation.promise; };
  const validating = h.session.validate(); await h.session.validate(); assert.equal(posts, 2);
  validation.resolve({ data: quote() }); await validating; assert.equal(h.state().validated, true);
});

test("400, 403, 409 and network failures clear stale quote, hide raw detail and allow retry", async () => {
  for (const status of [400, 403, 409, 500, undefined]) {
    const h = harness(); await h.session.open(); h.session.setTarget("v2"); await h.session.quote();
    h.api.post = async () => { throw { response: { status, data: { detail: "cost=900 private-signed-token" } } }; };
    await h.session.validate();
    assert.equal(h.state().quote, null); assert.equal(h.state().validating, false); assert.ok(h.state().error);
    assert.doesNotMatch(h.state().error, /cost|private-signed-token/);
    h.api.post = async () => ({ data: quote() }); await h.session.quote(); assert.ok(h.state().quote);
  }
  assert.doesNotMatch(versionChangeError(new Error("secret cost=99")), /secret|cost/);
});

test("failed option loads show retry and clear every old quote and selection", async () => {
  const h = harness(); await h.session.open(); h.session.setTarget("v2"); await h.session.quote();
  h.api.get = async () => { throw { response: { status: 403 } }; };
  await h.session.refresh();
  assert.equal(h.state().options, null); assert.equal(h.state().quote, null); assert.equal(h.state().targetId, "");
  assert.equal(h.state().loading, false); assert.ok(h.state().error);
  h.api.get = async () => ({ data: options }); await h.session.refresh(); assert.ok(h.state().options);
});

test("newer option loads win over stale successes and failures", async () => {
  for (const fail of [false, true]) {
    const h = harness(), old = deferred(); h.api.get = () => old.promise;
    const loading = h.session.open();
    h.api.get = async () => ({ data: { ...options, versions: [options.versions[1]] } });
    await h.session.refresh();
    if (fail) old.reject({ response: { status: 403 } }); else old.resolve({ data: options });
    await loading;
    assert.equal(h.state().options.versions.length, 1); assert.equal(h.state().error, "");
  }
});

test("target change and refresh discard stale quotation without releasing a newer request", async () => {
  const h = harness(); h.context.order.delivery_method = "tenant_access";
  h.api.get = async () => ({ data: { ...options, delivery_method: "tenant_access" } });
  await h.session.open(); h.session.setTarget("v2");
  const old = deferred(); h.api.post = () => old.promise; const pending = h.session.quote();
  h.session.setTarget("v3"); h.session.setConfirmed(true);
  const recent = deferred(); h.api.post = () => recent.promise; const current = h.session.quote();
  old.resolve({ data: quote() }); await pending; assert.equal(h.state().quoting, true); assert.equal(h.state().quote, null);
  recent.resolve({ data: quote({ target_version_code: "v3", compatibility: { mode: "breaking", notes: "接口变更", confirmation_required: true } }) });
  await current; assert.equal(h.state().quote.target_version_code, "v3");
  const late = deferred(); h.api.post = () => late.promise; const validating = h.session.validate();
  await h.session.refresh(); late.resolve({ data: quote() }); await validating;
  assert.equal(h.state().quote, null); assert.equal(h.state().targetId, "");
});

test("epoch and order/user/token/enterprise/membership context discard pending responses", async () => {
  for (const change of [h => h.session.close(), h => h.session.dispose(), h => { h.context.order.id = "other"; },
    h => { h.context.user.id = "other"; }, h => { h.context.token = "changed"; }, h => { h.context.enterpriseId = "buyer"; },
    h => { h.context.user.enterprise_memberships[1].role = "member"; }, h => { h.context.subscription.generation = 2; }]) {
    for (const phase of ["options", "quote", "validate"]) {
      const h = harness(), old = deferred();
      let pending;
      if (phase === "options") { h.api.get = () => old.promise; pending = h.session.open(); }
      else {
        await h.session.open(); h.session.setTarget("v2");
        if (phase === "validate") await h.session.quote();
        h.api.post = () => old.promise; pending = phase === "quote" ? h.session.quote() : h.session.validate();
      }
      change(h); const last = h.state();
      old.resolve({ data: phase === "options" ? options : quote() }); await pending;
      assert.equal(h.state(), last);
    }
  }
});

test("expiry clears visible quote and private token, including pending validation", async () => {
  const h = harness(); await h.session.open(); h.session.setTarget("v2"); await h.session.quote();
  const old = deferred(); h.api.post = () => old.promise; const pending = h.session.validate();
  h.setTime(at + 300000); h.session.expire();
  assert.equal(h.state().quote, null); assert.equal(h.state().validating, false); assert.match(h.state().error, /过期/);
  old.resolve({ data: quote() }); await pending;
  assert.equal(h.state().quote, null); assert.equal(h.state().validated, false);
  const calls = h.calls.length; await h.session.validate(); assert.equal(h.calls.length, calls);
});

test("validation rejects altered token or public fields even if returned amounts are internally consistent", async () => {
  for (const changes of [{ quote_token: "different" }, { quote_id: "different" }, { expires_at: "2030-02-15T00:04:00Z" }]) {
    const h = harness(); await h.session.open(); h.session.setTarget("v2"); await h.session.quote();
    h.api.post = async () => ({ data: quote(changes) }); await h.session.validate();
    assert.equal(h.state().quote, null); assert.ok(h.state().error);
  }
});

const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
test("Vue subscription binding opens only buyer preview and closes immediately on context changes", async () => {
  const context = { ref, computed, watch, serverTimeNow, canPreviewVersionChange: (o, s, u, e) => canPreviewVersionChange(o, s, u, e, at),
    createVersionChangeSession: args => createVersionChangeSession({ ...args, now: () => at }),
    selectedOrder: ref({ order: structuredClone(order) }), subscriptionState: ref({ orderId: order.id, item: structuredClone(subscription), loading: false, error: "" }),
    user: ref(structuredClone(user)), enterprise: ref({ id: "other" }), token: ref("token"),
    deliveryBuyerEnterprise: () => buyerEnterprise, api: { get: async () => ({ data: options }) } };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf("const versionChangeState ="), source.indexOf("watch([() => user.value?.id")) +
    "\nglobalThis.bindings = { versionChangeSession, versionChangeAllowed, versionChangeModal };", context);
  assert.equal(context.bindings.versionChangeAllowed.value, true);
  await context.bindings.versionChangeSession.open(); assert.ok(context.bindings.versionChangeModal.value);
  context.user.value.enterprise_memberships[1].role = "member";
  assert.equal(context.bindings.versionChangeAllowed.value, false);
  assert.equal(context.bindings.versionChangeModal.value, null);
  assert.match(source, /v-if="versionChangeAllowed"/);
  assert.match(source, /v-if="renewalAllowed"/);
  const template = source.slice(source.indexOf('<div v-if="versionChangeModal"'), source.indexOf('<div v-if="renewalModal"'));
  assert.doesNotMatch(template, /quote_token|cost|执行支付|执行退款|确认支付/);
  assert.match(template, /校验报价/);
});
