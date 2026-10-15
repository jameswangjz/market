import { createApp, h, nextTick, reactive } from "vue";
import RefundCenter from "../../src/RefundCenter.vue";
import "../../src/style.css";
// Strict BE010-shaped memory responses. No real HTTP, funds, resources or billing.
const view = reactive({ role: "buyer", epoch: 0 });
const params = reactive({ enterprise_id: "qa-refund-buyer", business_view: "buyer" });
const calls = [];
const users = {
  buyer: { id: "qa-buyer", verified_status: "verified", enterprise_memberships: [{ enterprise_id: "qa-refund-buyer", role: "enterprise_admin", status: "active" }] },
  provider: { id: "qa-provider", verified_status: "verified", enterprise_memberships: [{ enterprise_id: "qa-refund-provider", role: "enterprise_admin", status: "active" }] },
  admin: { id: "qa-admin", verified_status: "verified", platform_role: "super_admin" },
  finance: { id: "qa-finance", verified_status: "verified", platform_role: "finance_settlement" },
};
const order = { id: "qa-order", order_no: "ORD-QA-REFUND", buyer_enterprise_id: "qa-refund-buyer", provider_enterprise_id: "qa-refund-provider", payment_status: "paid" };
let record, failNext = 0;
const clone = value => JSON.parse(JSON.stringify(value));
function assert(value, text) { if (!value) throw new Error(text); }
async function until(condition, text) { for (let i = 0; i < 120; i++) { await new Promise(resolve => setTimeout(resolve, 20)); if (condition()) return; } throw new Error(`timeout: ${text}`); }
const button = text => [...document.querySelectorAll("button")].find(node => node.textContent.trim() === text);
function fill(node, value) { assert(node, "input exists"); node.value = value; node.dispatchEvent(new Event("input", { bubbles: true })); }
const posts = () => calls.filter(call => call.method === "POST");
const api = {
  async get(path, options) {
    calls.push({ method: "GET", path, params: clone(options.params) });
    const item = { ...record, allowed_actions: view.role === "admin" && record.status === "requested" ? ["reject"] : [] };
    if (path === "/refunds") return { data: { items: [clone(item)], total: 1, page: options.params.page, page_size: options.params.page_size } };
    assert(path === "/refunds/qa-refund", "exact detail path"); return { data: clone(item) };
  },
  async post(path, body, options) {
    const data = clone(body); calls.push({ method: "POST", path, body: data, params: clone(options.params) });
    await new Promise(resolve => setTimeout(resolve, 180));
    if (failNext) { const status = failNext; failNext = 0; if (status === 409) record.version++; throw { response: { status, data: { detail: "隔离模拟失败" } } }; }
    if (path === "/orders/qa-order/refund-quote") {
      assert(Object.keys(data).length === 1 && data.amount === "12.50", "strict quote amount only");
      return { data: { status: "unavailable", reason: "charge_ledger_incomplete_or_unallocated", item: null, execution_available: false } };
    }
    assert(data.idempotency_key.length >= 8 && data.reason.trim(), "reason and idempotency required");
    if (path === "/orders/qa-order/refund-requests") {
      assert(Object.keys(data).every(key => ["amount", "reason", "payment_id", "ticket_id", "idempotency_key"].includes(key)), "strict request DTO");
      record.amount = data.amount; record.reason = data.reason;
    } else if (path === "/refunds/qa-refund/manual-assessments") {
      assert(view.role === "admin" && data.expected_version === record.version, "manual authority and version");
      assert(Object.keys(data).sort().join() === ["amount", "refundable_limit", "used_calls", "reason", "evidence_refs", "expected_version", "idempotency_key"].sort().join(), "strict manual DTO");
      assert(data.used_calls === 4 && data.evidence_refs[0] === "qa:document", "explicit manual evidence, not inferred zero");
      record.version++; record.planning = { status: "manual_ready", source_trust: "manual_determination", manual_assessment_allowed: true, automatic_quote_available: false };
    } else {
      assert(path === "/refunds/qa-refund/review" && view.role === "admin", "only isolated admin review");
      assert(data.expected_version === record.version && data.decision === "reject", "no approval or execution fixture");
      assert(Object.keys(data).every(key => ["decision", "reason", "expected_version", "idempotency_key"].includes(key)), "strict review DTO");
      record.status = "rejected"; record.review_reason = data.reason; record.version++;
    }
    return { data: clone(record) };
  },
};
createApp({ render() { return h("main", { style: "padding:16px;max-width:1100px;margin:auto" }, [h("h1", "退款隔离组件验证"),
  h(RefundCenter, { key: view.epoch, api, user: users[view.role], enterpriseId: params.enterprise_id, order, params })]); } }).mount("#fixture");
async function reset(role) {
  calls.length = 0; failNext = 0;
  record = { id: "qa-refund", refund_no: "REF-QA-ISOLATED", order_id: order.id, order_no: order.order_no, payment_id: "qa-payment", amount: "12.50", status: "requested", source: "ordinary", version: 3, reason: "隔离申请理由", created_at: "2026-10-15T00:00:00Z",
    planning: { status: "unavailable", reason: "manual_assessment_required", manual_assessment_allowed: true, automatic_quote_available: false }, execution_available: false, execution_mode: "unavailable" };
  view.role = role; params.enterprise_id = role === "provider" ? "qa-refund-provider" : "qa-refund-buyer"; params.business_view = role === "provider" ? "provider" : "buyer"; view.epoch++;
  await nextTick(); if (role !== "finance") await until(() => document.querySelector(".refund-list tbody .task-code") && !document.querySelector(".refund-list [role=status]"), "list ready");
}
async function detail() { document.querySelector(".refund-list .task-code").click(); await until(() => document.querySelector(".refund-detail h3"), "detail ready"); }
function layout() {
  const root = document.querySelector(".refund-center"), rect = root.getBoundingClientRect();
  assert(rect.left >= 0 && rect.right <= innerWidth + 1, "refund content fits viewport");
  for (const node of root.querySelectorAll("button")) { node.scrollIntoView({ block: "center", inline: "nearest" }); const r = node.getBoundingClientRect(); assert(r.width > 0 && r.height > 0 && r.left >= 0 && r.right <= innerWidth + 1, "buttons reachable by scrolling"); }
  return "viewport bounds and scroll-reachable buttons";
}
window.__refund = { async run(scenario) {
  const checks = [];
  if (scenario.startsWith("layout-")) {
    const kind = scenario.slice(7);
    await reset(kind === "request" ? "buyer" : "admin");
    if (kind === "request") { button("申请退款").click(); await nextTick(); } else await detail();
    const root = document.querySelector(kind === "request" ? ".refund-request-form" : kind === "manual" ? ".refund-manual-assessment" : ".refund-review-form");
    assert(root && getComputedStyle(root).fontSize === "13px", "compact 13px form");
    for (const control of root.querySelectorAll("input,textarea")) {
      const label = control.closest("label"), style = getComputedStyle(control), labelStyle = getComputedStyle(label);
      assert(labelStyle.display === "flex" && labelStyle.flexDirection === "column", "label stacked above control");
      const rect = control.getBoundingClientRect(), box = label.getBoundingClientRect();
      assert(Math.abs(rect.width - box.width) < 2 && rect.top > box.top + 15, "full width control below label");
      assert(style.fontSize === "13px" && parseFloat(style.paddingLeft) >= 8 && style.borderTopStyle === "solid" && parseFloat(style.borderRadius) >= 4, "own control styling, not native fallback");
      assert(rect.left >= 0 && rect.right <= innerWidth + 1, "control within viewport");
    }
    for (const node of root.querySelectorAll("button")) assert(node.querySelector("svg"), "lucide action icon");
    const grid = root.querySelector(".form-grid");
    if (grid) {
      const labels = grid.querySelectorAll("label"), first = labels[0].getBoundingClientRect(), second = labels[1].getBoundingClientRect();
      assert(innerWidth <= 640 ? Math.abs(first.left - second.left) < 2 && second.top > first.bottom : Math.abs(first.top - second.top) < 2 && second.left > first.right, "responsive form geometry");
    }
    checks.push(layout());
    root.scrollIntoView({ block: "start" });
    checks.push("13px own scoped styles; labels above full-width styled controls; responsive grid; lucide buttons");
  } else if (scenario === "request") {
    await reset("buyer"); button("申请退款").click(); await nextTick();
    const form = document.querySelector(".refund-request-form form"), amount = form.querySelector('input[inputmode=decimal]'), reason = form.querySelector("textarea");
    assert(form.querySelector("button[type=submit]").disabled, "blank request blocked"); assert(document.body.innerText.includes("试算不可用"), "no invented quote");
    fill(amount, "12.345"); fill(reason, "隔离退款理由"); await nextTick(); form.querySelector("button[type=submit]").click(); await nextTick(); assert(posts().length === 0, "invalid precision never rounded or sent");
    fill(amount, "12.50"); await nextTick(); failNext = 503; const submit = form.querySelector("button[type=submit]"); submit.click(); submit.click();
    await until(() => document.querySelector(".refund-request-form [role=alert]")?.textContent.includes("隔离模拟失败"), "failed request alert"); assert(posts().length === 1 && reason.value === "隔离退款理由" && amount.value === "12.50", "single request retains failed input");
    const key = posts()[0].body.idempotency_key; checks.push("buyer required fields, exact cents, single-flight and failure retention");
    submit.click(); await until(() => document.body.innerText.includes("申请已提交"), "request submitted"); assert(posts()[1].body.idempotency_key === key, "same retry key"); assert(!document.querySelector(".refund-request-form"), "request editor closes only on success");
    checks.push("BE010 stable retry key; request remains requested, no refund success claim"); checks.push(layout());
  } else if (scenario === "review") {
    await reset("admin"); await detail();
    assert(!button("批准申请") && button("拒绝申请").disabled, "approval unavailable, rejection requires reason");
    assert(!document.querySelector(".refund-manual-assessment fieldset").disabled && document.querySelector(".refund-manual-assessment button").disabled && document.body.innerText.includes("用量未知（unknown）"), "manual fields explicit, blank assessment blocked");
    assert(![...document.querySelectorAll("button")].some(node => /确认退款完成|执行退款/.test(node.textContent)), "no fake executor"); checks.push("admin rejection only; manual unknown and executor unavailable explicit");
    fill(document.querySelector(".refund-review-form textarea"), "隔离拒绝理由"); await nextTick(); button("拒绝申请").click(); button("拒绝申请").click(); await until(() => record.status === "rejected" && !document.querySelector(".refund-review-form fieldset:disabled"), "rejected");
    assert(posts().length === 1 && posts()[0].body.expected_version === 3, "single versioned rejection"); checks.push("admin rejects with reason/current version/idempotency, no funds side effect"); checks.push(layout());
  } else if (scenario === "manual") {
    await reset("admin"); await detail();
    const form = document.querySelector(".refund-manual-assessment form"), inputs = form.querySelectorAll("input"), areas = form.querySelectorAll("textarea");
    fill(inputs[0], "12.50"); fill(inputs[1], "20"); fill(inputs[2], "4"); fill(areas[0], "人工核定依据"); fill(areas[1], "qa:document"); await nextTick();
    failNext = 503; form.querySelector("button").click(); form.querySelector("button").click();
    await until(() => document.querySelector(".refund-detail [role=alert]")?.textContent.includes("隔离模拟失败"), "manual failure");
    assert(posts().length === 1 && inputs[2].value === "4" && areas[1].value === "qa:document", "manual single flight and input retained");
    const key = posts()[0].body.idempotency_key; form.querySelector("button").click();
    await until(() => document.body.innerText.includes("人工核定已记录"), "manual ready");
    assert(posts()[1].body.idempotency_key === key && document.body.innerText.includes("人工核定就绪 / manual"), "manual retry stable, distinct trusted state");
    checks.push("BE011 strict manual DTO, explicit usage/limit/evidence, stable retry and no execution"); checks.push(layout());
  } else if (scenario === "quote") {
    await reset("buyer"); button("申请退款").click(); await nextTick();
    fill(document.querySelector('.refund-request-form input[inputmode=decimal]'), "12.50"); await nextTick(); button("服务端试算").click();
    await until(() => posts().length === 1 && !button("试算中"), "quote returned");
    assert(document.body.innerText.includes("unknown") && !document.body.innerText.includes("¥0.00"), "insufficient ledger not automatic zero");
    checks.push("BE011 quote actual endpoint and strict amount, unavailable evidence never zero"); checks.push(layout());
  } else if (scenario === "conflict") {
    await reset("admin"); await detail(); fill(document.querySelector(".refund-review-form textarea"), "冲突后保留理由"); await nextTick(); failNext = 409; button("拒绝申请").click();
    await until(() => document.querySelector(".refund-detail [role=alert]")?.textContent.includes("退款记录或余额已变化"), "conflict refreshed"); assert(document.querySelector(".refund-review-form textarea").value === "冲突后保留理由", "409 reason preserved");
    assert(calls.filter(item => item.path === "/refunds/qa-refund" && item.method === "GET").length === 2, "409 detail refresh once"); checks.push("409 refreshes authoritative detail, retains reason and does not replay POST");
  } else if (scenario === "roles") {
    await reset("provider"); await detail(); assert(!button("申请退款") && !document.querySelector(".refund-review-form"), "provider read-only");
    assert(calls.filter(item => item.method === "GET").every(item => item.params.enterprise_id === "qa-refund-provider" && item.params.business_view === "provider"), "provider scoped reads"); checks.push("provider sees scoped records, no buyer request or platform approval");
    await reset("finance"); assert(!document.querySelector(".refund-center") && calls.length === 0, "finance no ordinary refunds UI or API"); checks.push("finance no refund approval/request; receipt confirmation covered by isolated existing function test");
    await reset("admin"); await detail();
  } else throw new Error("unknown refund scenario");
  return { checks, requests: calls.map(item => ({ method: item.method, path: item.path, params: item.params, decision: item.body?.decision, expected_version: item.body?.expected_version, idempotency_present: !!item.body?.idempotency_key })) };
} };
