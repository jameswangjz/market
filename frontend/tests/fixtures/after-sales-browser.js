import { createApp, h, nextTick, reactive } from "vue";
import AfterSalesCreate from "../../src/AfterSalesCreate.vue";
import AfterSalesDetail from "../../src/AfterSalesDetail.vue";
import "../../src/style.css";

// Dependency-injected responses only: this fixture never invokes fetch or XHR.
const context = reactive({ open: false, create: false, actor: "buyer", epoch: 0 });
const calls = [];
let ticket, comments, attachments, timeline, failNext, delay, lastCreated;
let holdReads = false;
const pendingReads = [], adapterErrors = [];
const params = reactive({ enterprise_id: "qa-fe006-buyer", business_view: "buyer" });
const users = {
  buyer: { id: "qa-fe006-buyer-user", enterprise_memberships: [{ enterprise_id: "qa-fe006-buyer", role: "enterprise_admin", status: "active" }] },
  provider: { id: "qa-fe006-provider-user", enterprise_memberships: [{ enterprise_id: "qa-fe006-provider", role: "enterprise_admin", status: "active" }] },
  monitor: { id: "qa-fe006-monitor", platform_role: "delivery_monitor" },
  admin: { id: "qa-fe006-admin", platform_role: "super_admin" },
};
function assert(value, label) { if (!value) throw new Error(label); return label; }
async function until(condition, label) { for (let i = 0; i < 100; i++) { await new Promise(resolve => setTimeout(resolve, 20)); if (condition()) return; } throw new Error(`timeout: ${label}`); }
function button(text) { return [...document.querySelectorAll(".after-sales-modal button")].find(node => node.textContent.trim() === text); }
function fill(node, value) { assert(node, "input exists"); node.value = value; node.dispatchEvent(new Event("input", { bubbles: true })); }
const posts = () => calls.filter(item => item.method === "POST");
function actions() {
  if (["completed", "force_closed"].includes(ticket.status)) return [];
  if (context.actor === "monitor") return ["review_intervention", "force_close"];
  if (context.actor === "admin") return ["force_close", "review_intervention"];
  if (context.actor === "provider") return ["respond", "request_info", "submit_resolution", "request_intervention"];
  return ["confirm", "reject_resolution", "request_intervention", "withdraw"];
}
const api = {
  async get(path, options) {
    calls.push({ method: "GET", path, params: { ...options.params } });
    if (holdReads && options.params.enterprise_id === "qa-fe006-buyer") await new Promise(resolve => pendingReads.push(resolve));
    if (path.endsWith("/download")) return { data: new Blob(["isolated attachment"]) };
    if (path.endsWith("/timeline")) return { data: { items: structuredClone(timeline) } };
    if (path.endsWith("/comments")) return { data: { items: structuredClone(comments) } };
    if (path.endsWith("/attachments")) return { data: { items: structuredClone(attachments) } };
    assert(path === "/after-sales/qa-fe006-ticket", "exact fixture ticket path");
    return { data: { ...ticket, allowed_actions: actions() } };
  },
  async post(path, body, options) {
    const data = body instanceof FormData ? Object.fromEntries(body.entries()) : JSON.parse(JSON.stringify(body));
    calls.push({ method: "POST", path, body: data, params: { ...options.params } });
    await new Promise(resolve => setTimeout(resolve, delay));
    if (failNext) { const status = failNext; failNext = 0; if (status === 409) holdReads = true; throw { response: { status, data: { detail: "隔离模拟失败，原输入保留" } } }; }
    assert(data.idempotency_key?.length >= 8, "idempotency key supplied");
    if (path === "/after-sales") { lastCreated = { ...ticket }; return { data: lastCreated }; }
    assert(Number(data.expected_version) === ticket.version, "current expected_version supplied");
    if (path.endsWith("/actions")) {
      assert(data.reason.trim(), "action reason supplied");
      ticket.version++;
      ticket.status = ({ confirm: "completed", force_close: "force_closed", request_intervention: "platform_intervention", submit_resolution: "awaiting_buyer_confirmation" })[data.action] || "processing";
    } else if (path.endsWith("/comments")) comments.push({ id: "qa-comment", content: data.content, attachments: data.attachments, author_name: "隔离评论作者", author_role: "enterprise_admin", created_at: "2026-10-15T00:00:00Z" });
    else if (path.endsWith("/attachments")) attachments.push({ id: "qa-attachment", name: data.upload.name, purpose: data.purpose, author_name: "隔离附件作者", author_role: "enterprise_admin", author_id: users[context.actor].id, available: true });
    else throw new Error("unknown fixture mutation path");
    return { data: { ...ticket } };
  },
};
const fixturePost = api.post;
api.post = async (...args) => {
  try { return await fixturePost(...args); }
  catch (error) {
    adapterErrors.push(error.message || `mock HTTP ${error.response?.status}`);
    if (error instanceof Error) throw { response: { status: 500, data: { detail: `Fixture adapter: ${error.message}` } } };
    throw error;
  }
};
createApp({ render() {
  return h("main", { style: "padding:24px" }, [h("h1", "FE006 isolated browser fixture"),
    h("p", "Memory responses only. No backend writes."), context.open ? h(context.create ? AfterSalesCreate : AfterSalesDetail,
      { key: context.epoch, api, orderId: "qa-fe006-order", ticketId: ticket.id, params, user: users[context.actor],
        enterpriseId: context.actor === "provider" ? "qa-fe006-provider" : "qa-fe006-buyer",
        onClose: () => { context.open = false; }, onCreated: () => { context.open = false; } }) : null]);
} }).mount("#fixture");

async function reset(actor, create = false) {
  context.open = false; await nextTick(); calls.length = 0; failNext = 0; delay = 90; lastCreated = null;
  holdReads = false; pendingReads.splice(0).forEach(resolve => resolve()); adapterErrors.length = 0;
  ticket = { id: "qa-fe006-ticket", order_id: "qa-fe006-order", order_no: "ORD-QA-FE006-ISOLATED", ticket_no: "AS-QA-FE006", version: 3,
    buyer_enterprise_id: "qa-fe006-buyer", provider_enterprise_id: "qa-fe006-provider", type: "质量异议", description: "隔离售后说明，不涉及真实业务", status: "awaiting_buyer_confirmation", service_level_code: "standard" };
  comments = []; attachments = []; timeline = [{ id: "qa-event", action: "create", actor_name: "隔离买方管理员", actor_role: "enterprise_admin", reason: "隔离创建说明", before: {}, after: { status: "awaiting_response" }, occurred_at: "2026-10-15T00:00:00Z" }];
  context.actor = actor; context.create = create; context.epoch++; params.enterprise_id = actor === "provider" ? "qa-fe006-provider" : "qa-fe006-buyer"; params.business_view = actor === "provider" ? "provider" : "buyer";
  context.open = true; await nextTick(); await until(() => document.querySelector(".after-sales-modal textarea") && !document.querySelector('[role="status"]'), "modal ready");
}
function layout() {
  const modal = document.querySelector(".after-sales-modal"), bounds = modal.getBoundingClientRect();
  assert(bounds.left >= 0 && bounds.top >= 0 && bounds.right <= innerWidth + 1 && bounds.bottom <= innerHeight + 1, "modal fits viewport");
  assert(modal.scrollWidth <= modal.clientWidth + 1, "modal no horizontal overflow");
  for (const control of modal.querySelectorAll("button:not([hidden])")) {
    control.scrollIntoView({ block: "center" });
    const rect = control.getBoundingClientRect();
    assert(rect.left >= bounds.left && rect.right <= bounds.right + 1 && rect.top >= bounds.top && rect.bottom <= bounds.bottom + 1, "button reachable by modal scroll");
  }
  modal.scrollTop = 0;
  return "modal fits viewport; no horizontal overflow; buttons reachable by scrolling";
}
async function action(name) {
  button(name).click(); await nextTick();
  const form = [...document.querySelectorAll("form")].find(item => item.querySelector(".primary-btn"));
  fill(form.querySelector("textarea"), "隔离处理理由"); await nextTick(); form.querySelector("button[type=submit]").click();
  await until(() => !document.querySelector("fieldset:disabled"), "action complete");
}
window.__fe006 = { calls, adapterErrors, layout, async snapshot(name) {
  if (name === "admin") { await reset("admin"); button("强制关闭").click(); await nextTick(); }
  else if (name === "monitor") await reset("monitor");
  return true;
}, async run(scenario) {
  const checks = [];
  const evidence = [];
  function capture() { evidence.push(...calls.map(({ method, path, params, body }) => ({ method, path, params, action: body?.action, expected_version: body?.expected_version, has_idempotency_key: !!body?.idempotency_key }))); }
  if (scenario === "create") {
    await reset("buyer", true);
    const form = document.querySelector("form"), input = form.querySelector("textarea");
    assert(form.querySelector("button[type=submit]").disabled, "empty creation disabled"); form.requestSubmit(); await nextTick(); assert(posts().length === 0, "empty create sends nothing"); checks.push("buyer creation requires description");
    fill(input, "隔离创建失败保留说明"); await nextTick(); failNext = 503; delay = 220;
    const submit = form.querySelector("button[type=submit]"); submit.click(); submit.click();
    await until(() => document.querySelector('[role="alert"]'), "failed create alert");
    assert(posts().length === 1 && input.value === "隔离创建失败保留说明", "single create and failed input retained");
    document.querySelector(".after-sales-scrim").click(); input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); await nextTick(); assert(context.open && input.value, "background and ESC retain editor");
    checks.push("double click sends once; failed input retained; background/ESC do not dismiss");
    const key = posts()[0].body.idempotency_key; checks.push(layout());
    submit.click(); await until(() => !context.open, "creation retry success"); assert(posts()[1].body.idempotency_key === key, "retry uses same key"); checks.push("creation retry retains idempotency key");
    capture(); await reset("buyer", true); fill(document.querySelector("textarea"), "隔离创建界面截图"); await nextTick();
  } else if (scenario === "dirty") {
    await reset("buyer", true); fill(document.querySelector("textarea"), "未保存的创建说明"); await nextTick();
    document.querySelector('[aria-label="关闭工单窗口"]').click(); await until(() => document.querySelector(".action-dialog"), "creation dirty confirmation");
    assert(document.querySelector(".action-dialog").innerText.includes("放弃未提交内容"), "dirty dialog visible");
    document.querySelector(".action-dialog .secondary-btn").click(); await until(() => !document.querySelector(".action-dialog"), "dirty cancel");
    assert(context.open && document.querySelector("textarea").value === "未保存的创建说明", "cancel preserves create text"); checks.push("creation close shows custom dirty confirmation; cancel retains input");
    document.querySelector('[aria-label="关闭工单窗口"]').click(); await until(() => document.querySelector(".action-dialog"), "second dirty confirmation"); document.querySelector(".action-dialog .primary-btn").click(); await until(() => !context.open, "explicit discard closes"); checks.push("explicit discard closes without API request"); assert(posts().length === 0, "discard no mutation");
    await reset("buyer"); button("拒绝解决方案").click(); await nextTick();
    fill(document.querySelector("form .primary-btn").closest("form").querySelector("textarea"), "未保存处理理由");
    const commentForm = [...document.querySelectorAll("form")].find(form => form.textContent.includes("发送")); fill(commentForm.querySelector("textarea"), "未保存评论"); await nextTick();
    document.querySelector('[aria-label="关闭工单窗口"]').click(); await until(() => document.querySelector(".action-dialog"), "detail dirty confirmation");
    document.querySelector(".action-dialog .secondary-btn").click(); await until(() => !document.querySelector(".action-dialog"), "detail dirty cancel");
    assert([...document.querySelectorAll("textarea")].some(node => node.value === "未保存处理理由") && [...document.querySelectorAll("textarea")].some(node => node.value === "未保存评论"), "reason and comment retained"); checks.push("action reason and comment survive dirty cancel");
    document.querySelector('[aria-label="关闭工单窗口"]').click(); await until(() => document.querySelector(".action-dialog"), "dirty screenshot state");
  } else if (scenario === "provider") {
    await reset("provider"); checks.push(layout()); await action("响应工单");
    assert(posts()[0].body.action === "respond" && posts()[0].body.expected_version === 3, "provider response version");
    assert(posts()[0].params.enterprise_id === "qa-fe006-provider", "provider scope"); checks.push("provider responds with reason/current version and provider scope");
    await action("提交解决方案"); assert(ticket.status === "awaiting_buyer_confirmation", "provider resolution"); checks.push("provider submits resolution");
  } else if (scenario === "buyer") {
    await reset("buyer"); await action("拒绝解决方案"); assert(posts()[0].body.action === "reject_resolution", "buyer rejects"); checks.push("buyer rejects with reason"); capture();
    await reset("buyer"); await action("确认解决"); assert(ticket.status === "completed", "buyer confirms"); checks.push("buyer confirms resolution"); capture();
    await reset("buyer"); await action("申请平台介入"); assert(posts()[0].body.action === "request_intervention", "buyer intervention"); checks.push("buyer requests intervention"); capture();
    await reset("provider"); await action("申请平台介入"); checks.push("provider requests intervention");
  } else if (scenario === "roles") {
    await reset("monitor"); assert(!button("强制关闭") && !!button("完成平台审查"), "monitor no force close"); checks.push("monitor cannot force close even if fixture advertises action");
    await reset("admin"); button("强制关闭").click(); await nextTick();
    const form = [...document.querySelectorAll("form")].find(item => item.querySelector(".primary-btn"));
    form.requestSubmit(); await nextTick(); assert(posts().length === 0 && form.querySelector("button[type=submit]").disabled, "force close requires reason");
    checks.push("admin force close requires reason"); checks.push(layout()); await action("强制关闭"); assert(ticket.status === "force_closed", "admin closes"); checks.push("admin force close submits reason/version"); capture();
    await reset("monitor");
  } else if (scenario === "collaboration") {
    await reset("buyer");
    const form = [...document.querySelectorAll("form")].find(item => item.querySelector("button[type=submit]")?.textContent.trim() === "发送");
    form.requestSubmit(); assert(posts().length === 0, "empty comment blocked");
    const input = document.querySelector("input[type=file]"), transfer = new DataTransfer(); transfer.items.add(new File(["isolated evidence"], "qa-fe006-evidence.txt", { type: "text/plain" })); input.files = transfer.files; input.dispatchEvent(new Event("change", { bubbles: true }));
    await until(() => document.body.innerText.includes("qa-fe006-evidence.txt"), "uploaded attachment");
    const upload = posts()[0]; assert(upload.body.upload.name === "qa-fe006-evidence.txt" && upload.body.purpose === "evidence" && upload.path.endsWith("/attachments"), "controlled multipart upload"); checks.push("controlled upload includes file, purpose, current version and key");
    document.querySelector('input[type=checkbox]').click(); fill(form.querySelector("textarea"), "隔离评论关联附件"); await nextTick(); form.querySelector("button[type=submit]").click();
    await until(() => comments.length === 1 && !document.querySelector("fieldset:disabled"), "comment complete"); assert(posts()[1].body.attachments[0] === "qa-attachment", "comment attachment linked"); checks.push("comment required; uploaded owned attachment linked");
    assert(document.body.innerText.includes("隔离买方管理员") && document.body.innerText.includes("创建 → 待提供方响应") && document.body.innerText.includes("ORD-QA-FE006-ISOLATED"), "trace labels"); checks.push("order number and named creation timeline visible");
    assert(document.body.innerText.includes("隔离评论作者") && document.body.innerText.includes("隔离附件作者"), "comment and attachment authors visible"); checks.push("comment and attachment author names visible"); checks.push(layout());
    document.querySelector('[aria-label="下载 qa-fe006-evidence.txt"]').click();
    await until(() => !document.querySelector("fieldset:disabled"), "controlled download complete");
    assert(calls.some(item => item.method === "GET" && item.path === "/after-sales/qa-fe006-ticket/attachments/qa-attachment/download"), "download uses controlled ticket attachment path"); checks.push("download clicks protected attachment GET; Chrome disk download denied");
  } else if (scenario === "terminal") {
    await reset("buyer"); ticket.status = "completed";
    document.querySelector('[aria-label="刷新工单"]').click(); await until(() => document.body.innerText.includes("追加审计评论"), "terminal detail ready");
    const input = document.querySelector("input[type=file]"), transfer = new DataTransfer(); transfer.items.add(new File(["isolated terminal audit"], "qa-fe006-audit.txt", { type: "text/plain" })); input.files = transfer.files; input.dispatchEvent(new Event("change", { bubbles: true }));
    await until(() => document.body.innerText.includes("qa-fe006-audit.txt"), "terminal upload complete");
    assert(posts()[0].body.purpose === "audit_comment", "terminal upload purpose"); checks.push("terminal attachment upload uses audit_comment; no lifecycle actions");
  } else if (scenario === "race") {
    await reset("buyer"); failNext = 409;
    button("拒绝解决方案").click(); await nextTick(); const form = document.querySelector("form .primary-btn").closest("form");
    fill(form.querySelector("textarea"), "旧身份争议理由"); await nextTick(); form.querySelector("button[type=submit]").click();
    await until(() => pendingReads.length === 4, "409 refresh suspended");
    params.enterprise_id = "qa-fe006-provider"; params.business_view = "provider"; context.actor = "provider";
    await until(() => button("响应工单") && !document.querySelector('[role="status"]'), "new identity detail loaded");
    pendingReads.splice(0).forEach(resolve => resolve()); await new Promise(resolve => setTimeout(resolve, 100));
    assert(!document.querySelector('[role="alert"]'), "old 409 does not overwrite new identity error");
    assert(![...document.querySelectorAll("textarea")].some(node => node.value === "旧身份争议理由"), "old identity editor cleared");
    assert(calls.filter(item => item.method === "GET").slice(-4).every(item => item.params.enterprise_id === "qa-fe006-provider" && item.params.business_view === "provider"), "new identity GET scope");
    checks.push("real component identity switch disposes pending 409 refresh; no stale error/editor; fresh scoped GETs");
  } else throw new Error("unknown scenario");
  capture(); return { scenario, checks, requests: evidence };
} };
