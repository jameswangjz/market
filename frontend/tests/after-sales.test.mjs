import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { compile, createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { afterSalesActions, ticketStatus, ticketActions, canCollaborate, canCreateAfterSales, createAfterSalesSession, newAfterSalesRequestKey, timelineActor, timelineFrom } from "../src/afterSales.js";

const user = { id: "buyer", enterprise_memberships: [{ enterprise_id: "buyer-ent", status: "active", role: "enterprise_admin" }] };
const ticket = { id: "ticket", version: 2, buyer_enterprise_id: "buyer-ent", provider_enterprise_id: "provider-ent", status: "processing", allowed_actions: ["withdraw", "request_intervention"] };
function harness(overrides = {}) {
  const state = { ticket: null, timeline: [], comments: [], attachments: [], busy: false, loading: false, error: "" };
  const calls = [];
  const api = { get: async (url, options) => { calls.push(["get", url, options]); return { data: url.endsWith("/ticket") ? { ...ticket } : { items: [] } }; },
    post: async (...args) => { calls.push(["post", ...args]); return { data: { ...ticket, version: 3 } }; }, ...overrides };
  let keys = 0;
  return { state, calls, session: createAfterSalesSession(api, state, { enterprise_id: "buyer-ent", business_view: "buyer" }, () => `isolated-key-${++keys}`) };
}

test("Chinese states, server-authoritative actions, monitoring cannot force close", () => {
  assert.equal(ticketStatus("awaiting_buyer_info"), "待买方补充材料"); assert.equal(ticketStatus("future"), "未知状态");
  assert.deepEqual(ticketActions({}, user), []);
  const all = { allowed_actions: [...Object.keys(afterSalesActions), "future-action"] };
  assert.ok(!ticketActions(all, { platform_role: "delivery_monitor" }).includes("force_close"));
  assert.ok(ticketActions(all, { platform_role: "platform_operator" }).includes("force_close"));
  assert.deepEqual(ticketActions(ticket, user), ticket.allowed_actions);
});

test("enterprise selection and active administrator role govern collaboration and buyer creation", () => {
  assert.equal(canCollaborate(ticket, user, "buyer-ent"), true);
  for (const actor of [{ ...user, enterprise_memberships: [{ enterprise_id: "buyer-ent", role: "member", status: "active" }] }, { platform_role: "finance_settlement" }])
    assert.equal(Boolean(canCollaborate(ticket, actor, "buyer-ent")), false);
  assert.equal(canCollaborate(ticket, user, "provider-ent"), false);
  const order = { buyer_enterprise_id: "buyer-ent", payment_status: "paid", paid_amount: 12 };
  assert.equal(canCreateAfterSales(order, user, "buyer-ent"), true);
  assert.equal(canCreateAfterSales(order, { platform_role: "super_admin" }, "buyer-ent"), false);
  assert.equal(canCreateAfterSales({ ...order, paid_amount: 0 }, user, "buyer-ent"), false);
  assert.equal(canCreateAfterSales(order, user, "provider-ent"), false);
});

test("all detail GETs include current enterprise and business view; no SLA materialization GET", async () => {
  const h = harness(); await h.session.load("ticket");
  assert.equal(h.calls.length, 4); assert.equal(h.state.ticket.version, 2);
  for (const [, url, options] of h.calls) { assert.deepEqual(options.params, { enterprise_id: "buyer-ent", business_view: "buyer" }); assert.ok(!url.endsWith("/sla")); }
});

test("stale detail response is discarded after opening another ticket or disposing", async () => {
  const waits = [];
  const h = harness({ get: (url) => new Promise(resolve => waits.push(() => resolve({ data: url.endsWith("/old") ? { id: "old" } : url.endsWith("/new") ? { ...ticket, id: "new" } : { items: [] } }))) });
  const old = h.session.load("old"), newer = h.session.load("new");
  waits.slice(4).forEach(resolve => resolve()); await newer; assert.equal(h.state.ticket.id, "new");
  waits.slice(0, 4).forEach(resolve => resolve()); await old; assert.equal(h.state.ticket.id, "new");
  h.session.dispose(); assert.equal(h.state.ticket, null);
});

test("action sends reason, version, stable retry idempotency and attachments", async () => {
  const posts = [];
  const h = harness({ post: async (...args) => { posts.push(args); throw new Error("isolated transport failure"); } });
  await h.session.load("ticket");
  const payload = { action: "request_intervention", reason: "双方争议", attachments: ["owned-attachment"] };
  assert.equal(await h.session.mutate("actions", payload), false);
  assert.equal(await h.session.mutate("actions", payload), false);
  assert.equal(posts[0][1].expected_version, 2); assert.equal(posts[0][1].reason, "双方争议");
  assert.equal(posts[0][1].idempotency_key, posts[1][1].idempotency_key);
  assert.deepEqual(posts[0][1].attachments, ["owned-attachment"]); assert.match(h.state.error, /输入已保留/);
  await h.session.mutate("actions", { ...payload, reason: "新理由" });
  assert.notEqual(posts[2][1].idempotency_key, posts[0][1].idempotency_key);
});

test("409 refreshes current detail and reports conflict without blindly retrying POST", async () => {
  let posts = 0;
  const h = harness({ post: async () => { posts++; throw { response: { status: 409 } }; } });
  await h.session.load("ticket"); await h.session.mutate("actions", { action: "withdraw", reason: "原因" });
  assert.equal(posts, 1); assert.equal(h.calls.length, 8); assert.match(h.state.error, /已变化.*刷新.*重新确认/);
});

test("concurrent mutations are single flight and release busy after completion", async () => {
  let finish, count = 0;
  const h = harness({ post: () => { count++; return new Promise(resolve => { finish = resolve; }); } });
  await h.session.load("ticket"); const first = h.session.mutate("comments", { content: "comment", attachments: [] });
  assert.equal(await h.session.mutate("comments", { content: "comment", attachments: [] }), false);
  assert.equal(count, 1); finish({ data: {} }); assert.equal(await first, true); assert.equal(h.state.busy, false);
});

test("controlled upload uses dedicated multipart fields and version, never generic file API", async () => {
  const h = harness(); await h.session.load("ticket");
  const file = new File(["owned isolated test"], "evidence.txt", { type: "text/plain" });
  await h.session.mutate("attachments", { purpose: "audit_comment", description: "" }, file);
  const post = h.calls.find(call => call[0] === "post"); assert.equal(post[1], "/after-sales/ticket/attachments");
  assert.equal(post[2].get("expected_version"), "2"); assert.equal(post[2].get("purpose"), "audit_comment");
  assert.equal(post[2].get("upload").name, "evidence.txt"); assert.ok(post[2].get("idempotency_key"));
});

test("all new components and integrated ConsoleApp compile", () => {
  for (const name of ["AfterSalesModalShell", "AfterSalesDetail", "AfterSalesCreate", "ConsoleApp"]) {
    const source = readFileSync(new URL(`../src/${name}.vue`, import.meta.url), "utf8");
    const { descriptor, errors } = parse(source); assert.deepEqual(errors, []);
    const script = compileScript(descriptor, { id: name });
    assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: `${name}.vue`, id: name,
      compilerOptions: { bindingMetadata: script.bindings } }).errors, []);
    if (name.startsWith("AfterSales")) assert.doesNotMatch(source, /\b(?:prompt|confirm)\s*\(|cost_price|provider_cost|cost_amount/);
  }
});

test("actual detail template escapes evidence, shows Chinese timeline and no hidden cost fields", async () => {
  const source = readFileSync(new URL("../src/AfterSalesDetail.vue", import.meta.url), "utf8");
  const { descriptor } = parse(source);
  const output = await renderToString(createSSRApp({ components: {
    AfterSalesModalShell: { template: "<section><slot /></section>" }, Download: { render: () => null }, RefreshCw: { render: () => null }, Upload: { render: () => null }, Send: { render: () => null },
  }, setup: () => ({ state: { ticket: { ...ticket, description: "<script>safe</script>", cost_price: "SECRET-COST" }, timeline: [{ id: "event", action: "request_intervention", before: { status: "processing" }, after: { status: "platform_intervention" }, reason: "争议", actor_role: "enterprise_admin" }], attachments: [], comments: [], loading: false, busy: false, error: "" },
    ticketStatus, afterSalesActions, timelineActor, timelineFrom, session: {}, action: "", upload: () => {}, allowed: [], collaborate: false, terminal: false, date: () => "—", emit: () => {} }), render: compile(descriptor.template.content) }));
  assert.match(output, /平台介入中/); assert.match(output, /申请平台介入/); assert.match(output, /&lt;script&gt;/);
  assert.doesNotMatch(output, /SECRET-COST|<script>/); assert.match(output, /无可用处理动作/);
});

test("HTTP browser without randomUUID uses existing cryptographic fallback, never Math.random", () => {
  const key = newAfterSalesRequestKey({ getRandomValues(bytes) { bytes.fill(17); return bytes; } });
  assert.match(key, /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
  assert.throws(() => newAfterSalesRequestKey({}));
  const create = readFileSync(new URL("../src/AfterSalesCreate.vue", import.meta.url), "utf8");
  assert.match(create, /newAfterSalesRequestKey\(\)/); assert.doesNotMatch(create, /crypto\.randomUUID/);
});

test("timeline identifies actor and distinguishes creation from unknown historical states", () => {
  assert.equal(timelineActor({ actor_name: "运营甲", actor_email: "actor@example.test", actor_id: "actor-id" }), "运营甲");
  assert.equal(timelineActor({ actor_email: "actor@example.test" }), "actor@example.test");
  assert.equal(timelineActor({ actor_id: "actor-id" }), "actor-id");
  assert.equal(timelineFrom({ before: {} }), "创建");
  assert.equal(timelineFrom({ before: { status: "processing" } }), "处理中");
});

test("detail reinitializes and disposes session on tenant, mode and actor changes", () => {
  const source = readFileSync(new URL("../src/AfterSalesDetail.vue", import.meta.url), "utf8");
  assert.match(source, /JSON\.stringify\(\[props\.ticketId, props\.params, props\.user\?\.id, props\.enterpriseId\]\)/);
  assert.match(source, /session\?\.dispose\(\)/); assert.match(source, /state\.ticket\.order_no \|\| state\.ticket\.order_id/);
  assert.match(source, /terminal\.value \? "audit_comment" : "evidence"/);
  assert.match(source, /!terminal\.value \|\| item\.purpose === "audit_comment"/);
});

test("disposed old tenant cannot overwrite new tenant busy/error after pending mutation", async () => {
  let finish;
  const h = harness({ post: () => new Promise(resolve => { finish = resolve; }) });
  await h.session.load("ticket");
  const pending = h.session.mutate("comments", { content: "old tenant", attachments: [] });
  h.session.dispose(); h.state.ticket = { id: "new-tenant-ticket" }; h.state.busy = true;
  finish({ data: {} });
  assert.equal(await pending, false); assert.equal(h.state.ticket.id, "new-tenant-ticket"); assert.equal(h.state.busy, true);
});

test("load denial clears prior detail and releases loading without exposing stale records", async () => {
  const h = harness({ get: async () => { throw { response: { status: 403, data: { detail: "无权读取" } } }; } });
  h.state.ticket = { ...ticket }; await h.session.load("ticket");
  assert.equal(h.state.ticket, null); assert.equal(h.state.loading, false); assert.equal(h.state.error, "无权读取");
});
