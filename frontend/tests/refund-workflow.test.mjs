import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRefundSession, refundAllowedActions, refundPlanning, refundManualValues } from "../src/refunds.js";

const buyer = { id: "buyer", verified_status: "verified", enterprise_memberships: [{ enterprise_id: "buyer-ent", role: "enterprise_admin", status: "active" }] };
const admin = { id: "admin", verified_status: "verified", platform_role: "super_admin" };
const record = { id: "refund", refund_no: "REF-ISOLATED", status: "requested", source: "ordinary", version: 3, amount: "12.50", allowed_actions: ["reject"], planning: { status: "unavailable", reason: "economic_plan_not_available" }, execution_available: false };
function harness(actor = buyer, overrides = {}) {
  const calls = [], state = { items: [], total: 0, item: { ...record }, busy: false, detailBusy: false }, context = { user: actor, enterpriseId: "buyer-ent", params: { enterprise_id: "buyer-ent", business_view: "buyer" }, order: { id: "order", order_no: "ORD-ISOLATED", buyer_enterprise_id: "buyer-ent", payment_status: "paid" } };
  const api = { get: async (...args) => { calls.push(["get", ...args]); return { data: args[0] === "/refunds" ? { items: [record], total: 1 } : { ...record } }; },
    post: async (...args) => { calls.push(["post", ...args]); return { data: { ...record } }; }, ...overrides };
  let keys = 0;
  return { context, api, state, calls, session: createRefundSession(api, state, () => context, () => `refund-key-${++keys}`) };
}

test("BE010 request sends only actual strict DTO fields and current enterprise scope", async () => {
  const h = harness(); assert.equal(await h.session.send("request", { amount: "12.5", reason: " 理由 ", paymentId: "payment", ticketId: "ticket", source: "version_change", manual_usage: 0, economic_plan: {} }), true);
  assert.deepEqual(h.calls[0], ["post", "/orders/order/refund-requests", { amount: "12.50", reason: "理由", payment_id: "payment", ticket_id: "ticket", idempotency_key: "refund-key-1" }, { params: { enterprise_id: "buyer-ent", business_view: "buyer" } }]);
  assert.match(h.state.message, /等待平台审核；尚未退款/);
});
test("list and detail preserve scope, order filter and server pagination", async () => {
  const h = harness(); await h.session.list(2, 50); await h.session.detail("refund");
  assert.deepEqual(h.calls[0], ["get", "/refunds", { params: { enterprise_id: "buyer-ent", business_view: "buyer", order_id: "order", page: 2, page_size: 50 } }]);
  assert.deepEqual(h.calls[1][2].params, h.context.params); assert.equal(h.state.total, 1);
});
test("ordinary review requires server capability, exact version and admin/operator identity", async () => {
  const h = harness(admin); assert.equal(await h.session.send("review", { decision: "approve", reason: "理由" }), false);
  assert.equal(await h.session.send("review", { decision: "reject", reason: " 拒绝理由 " }), true);
  assert.deepEqual(h.calls[0][2], { decision: "reject", reason: "拒绝理由", expected_version: 3, idempotency_key: "refund-key-1" });
  assert.match(h.state.message, /未执行资金或资源变更/);
  for (const role of ["finance_settlement", "delivery_monitor"]) {
    h.context.user = { ...admin, platform_role: role };
    assert.equal(await h.session.send("review", { decision: "reject", reason: "理由" }), false);
  }
  assert.equal(h.calls.length, 1);
});
test("legacy/version-change refunds never expose ordinary approval even if capability is forged", () => {
  for (const source of ["legacy", "version_change", null]) assert.deepEqual(refundAllowedActions({ ...record, source, allowed_actions: ["approve", "reject"] }, admin), []);
  assert.deepEqual(refundAllowedActions({ ...record, version: null }, admin), []);
});
test("network failure retains stable idempotency; changed payload receives a fresh key", async () => {
  const h = harness(); h.api.post = async (...args) => { h.calls.push(["post", ...args]); throw new Error("isolated transport"); };
  const values = { amount: "12.50", reason: "申请理由" };
  await h.session.send("request", values); await h.session.send("request", values);
  assert.equal(h.calls[0][2].idempotency_key, h.calls[1][2].idempotency_key); assert.match(h.state.error, /输入已保留/);
  await h.session.send("request", { ...values, reason: "新理由" }); assert.notEqual(h.calls[1][2].idempotency_key, h.calls[2][2].idempotency_key);
});
test("refund submissions are single flight and new business identity discards pending results", async () => {
  const h = harness(); let finish;
  h.api.post = (...args) => { h.calls.push(["post", ...args]); return new Promise(resolve => { finish = resolve; }); };
  const pending = h.session.send("request", { amount: "12", reason: "理由" });
  assert.equal(await h.session.send("request", { amount: "12", reason: "理由" }), false); assert.equal(h.calls.length, 1);
  h.context.enterpriseId = "new-ent"; h.session.reset();
  finish({ data: { ...record } }); assert.equal(await pending, false); assert.equal(h.state.item, null); assert.equal(h.state.message, "");
});
test("409 refresh followed by identity switch cannot overwrite new context error", async () => {
  const h = harness(admin); let finish;
  h.api.post = async () => { throw { response: { status: 409 } }; };
  h.api.get = () => new Promise(resolve => { finish = resolve; });
  const pending = h.session.send("review", { decision: "reject", reason: "理由" });
  for (let i = 0; i < 10 && !finish; i++) await Promise.resolve(); assert.ok(finish);
  h.context.enterpriseId = "new-ent"; h.session.reset(); h.state.error = "new-context-error";
  finish({ data: { ...record, version: 4 } }); await pending;
  assert.equal(h.state.item, null); assert.equal(h.state.error, "new-context-error");
});
test("historical/API unavailable planning is explicit unknown, not zero and not successful execution", () => {
  assert.match(refundPlanning({ reason: "historical_ledger_policy_pending" }).label, /unknown/);
  assert.match(refundPlanning({ reason: "api_usage_policy_pending" }).reason, /不以SLA日志或零用量替代/);
  assert.match(refundPlanning({ status: "frozen" }).reason, /不代表资金已执行/);
});
test("manual historical fields require explicit usage, limit and evidence with gated permission", () => {
  const source = readFileSync(new URL("../src/RefundManualAssessment.vue", import.meta.url), "utf8");
  assert.match(source, /用量未知（unknown）/); assert.match(source, /核定退款金额/); assert.match(source, /核定已扣减用量/); assert.match(source, /核定依据/);
  assert.match(source, /!props\.enabled/); assert.doesNotMatch(source, /\.post\(|\/sla|usedCalls = ref\("0"\)/);
  const detail = readFileSync(new URL("../src/RefundDetail.vue", import.meta.url), "utf8"); assert.match(detail, /canAssessRefund\(item, user\)/);
});
const manual = { amount: "12.50", refundableLimit: "20", usedCalls: "4", basis: "人工核对扣减记录", evidenceRefs: "ledger:one\n statement:two " };
test("BE011 manual assessment strict DTO, version, permission and single-flight", async () => {
  const h = harness(admin); h.state.item.planning = { manual_assessment_allowed: true };
  assert.equal(await h.session.send("manual", manual), true);
  assert.deepEqual(h.calls[0][2], { amount: "12.50", refundable_limit: "20.00", used_calls: 4, reason: manual.basis, evidence_refs: ["ledger:one", "statement:two"], expected_version: 3, idempotency_key: "refund-key-1" });
  assert.match(h.state.message, /人工核定.*尚未退款/);
  for (const role of ["finance_settlement", "delivery_monitor"]) {
    h.context.user = { ...admin, platform_role: role }; h.state.item.planning = { manual_assessment_allowed: true };
    assert.equal(await h.session.send("manual", manual), false);
  }
  assert.equal(h.calls.length, 1);
  for (const usedCalls of ["", "unknown", "-1", "1.5", "9007199254740992"]) assert.throws(() => refundManualValues({ ...manual, usedCalls }));
  assert.throws(() => refundManualValues({ ...manual, evidenceRefs: " " }));
  assert.equal(refundManualValues({ ...manual, usedCalls: "0" }).used_calls, 0);
  assert.match(refundPlanning({ status: "manual_ready" }).label, /manual/);
});
test("BE011 quote is server-only amount, unknown is not zero and changed amount discards response", async () => {
  const h = harness(); let finish;
  h.api.post = (...args) => { h.calls.push(["post", ...args]); return new Promise(resolve => { finish = resolve; }); };
  const pending = h.session.quote("12.5");
  assert.deepEqual(h.calls[0].slice(1, 3), ["/orders/order/refund-quote", { amount: "12.50" }]);
  h.session.clearQuote(); finish({ data: { status: "available", item: { charged_calls: 0 } } }); await pending;
  assert.equal(h.state.quote, null);
  h.api.post = async () => ({ data: { status: "unavailable", item: null, reason: "charge_ledger_incomplete_or_unallocated" } });
  await h.session.quote("12.50"); assert.equal(h.state.quote.item, null); assert.match(refundPlanning(h.state.quote).label, /unknown/);
  h.context.user = { ...admin, platform_role: "finance_settlement" }; await h.session.quote("12.50"); assert.equal(h.calls.length, 1);
});
test("integrated ConsoleApp compiles with narrow refund entries", () => {
  const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
  const { descriptor, errors } = parse(source); assert.deepEqual(errors, []);
  const script = compileScript(descriptor, { id: "refunds" });
  assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: "ConsoleApp.vue", id: "refunds", compilerOptions: { bindingMetadata: script.bindings } }).errors, []);
  assert.match(source, /<RefundCenter v-if="canViewRefunds/);
});
