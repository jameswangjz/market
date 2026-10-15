import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { compile, createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createRefundSession, refundExecutionActions, refundExecutionProgress, refundStatus, refundMoney, refundSource, refundPlanning, canAssessRefund } from "../src/refunds.js";

const admin = { id: "admin", verified_status: "verified", platform_role: "super_admin" };
const approved = { id: "refund", source: "ordinary", status: "approved", version: 4,
  allowed_actions: ["execute"], execution_available: true };
function harness(timers) {
  const state = { item: { ...approved }, items: [], busy: false, detailBusy: false };
  const context = { user: admin, enterpriseId: "enterprise", params: { enterprise_id: "enterprise" } };
  const calls = [];
  const api = { post: async (path, body, options) => { calls.push([path, body, options]); return { data: { ...approved, status: "executing" } }; },
    get: async path => { calls.push([path]); return { data: path === "/refunds" ? { items: [], total: 0 } : { ...approved, status: "executing" } }; } };
  let keys = 0;
  return { state, context, calls, api, session: createRefundSession(api, state, () => context, () => `execution-key-${++keys}`, timers) };
}
function fakeTimers() {
  let sequence = 0;
  const pending = new Map();
  return { pending, setTimeout(callback, delay) { assert.equal(delay, 3000); pending.set(++sequence, callback); return sequence; },
    clearTimeout(id) { pending.delete(id); }, async tick() {
      const next = pending.entries().next().value;
      if (!next) return;
      pending.delete(next[0]); await next[1]();
    } };
}
test("bounded execution refresh fetches only selected detail, stops terminal, and manual refresh restarts", async () => {
  const timers = fakeTimers(), h = harness(timers);
  h.state.item.status = "executing"; h.session.observeExecution();
  assert.equal(timers.pending.size, 1);
  for (let i = 0; i < 40; i++) await timers.tick();
  assert.equal(timers.pending.size, 0); assert.equal(h.calls.length, 40);
  assert(h.calls.every(call => call[0] === "/refunds/refund"));
  h.session.observeExecution(); assert.equal(timers.pending.size, 0);
  await h.session.refreshDetail(); assert.equal(timers.pending.size, 1);
  h.api.get = async () => ({ data: { ...approved, status: "completed" } });
  await timers.tick(); assert.equal(h.state.item.status, "completed"); assert.equal(timers.pending.size, 0);
  h.state.item = { ...approved, status: "executing" }; h.session.observeExecution();
  h.api.get = async () => ({ data: { ...approved, status: "failed" } });
  await timers.tick(); assert.equal(timers.pending.size, 0);
});
test("reset, close, unmount and lost visibility clear execution timers", () => {
  for (const stop of [h => h.session.reset(), h => h.session.closeDetail(), h => h.session.dispose(),
    h => { h.context.user = null; h.session.observeExecution(); }]) {
    const timers = fakeTimers(), h = harness(timers);
    h.state.item.status = "executing"; h.session.observeExecution(); assert.equal(timers.pending.size, 1);
    stop(h); assert.equal(timers.pending.size, 0);
  }
});
test("in-flight execution refresh cannot cross enterprises, details, close or unmount", async () => {
  for (const change of [h => { h.context.enterpriseId = "another"; h.session.reset(); },
    h => { h.session.closeDetail(); h.state.item = { ...approved, id: "another" }; },
    h => h.session.closeDetail(), h => h.session.dispose()]) {
    const timers = fakeTimers(), h = harness(timers); let finish;
    h.api.get = () => new Promise(resolve => { finish = resolve; });
    h.state.item.status = "executing"; h.session.observeExecution();
    const pending = timers.tick(); assert.equal(timers.pending.size, 0);
    change(h); const previous = h.state.item;
    finish({ data: { ...approved, status: "completed" } }); await pending;
    assert.equal(h.state.item, previous); assert.equal(timers.pending.size, 0);
  }
});
test("execution refresh does not overlap requests or overwrite mutation result", async () => {
  const timers = fakeTimers(), h = harness(timers); let finish;
  h.state.item.status = "executing";
  h.api.get = () => new Promise(resolve => { finish = resolve; });
  h.session.observeExecution(); const pending = timers.tick();
  h.session.observeExecution(); assert.equal(timers.pending.size, 0);
  h.state.busy = true; h.state.item = { ...approved, status: "failed" };
  finish({ data: { ...approved, status: "executing" } }); await pending;
  assert.equal(h.state.item.status, "failed"); assert.equal(timers.pending.size, 0);
});
test("execute/retry require strict server capability, ordinary source, status, version and approver", () => {
  assert.deepEqual(refundExecutionActions(approved, admin), ["execute"]);
  assert.deepEqual(refundExecutionActions({ ...approved, status: "failed", allowed_actions: ["retry"] }, admin), ["retry"]);
  for (const changes of [{ execution_available: false }, { execution_available: "true" }, { allowed_actions: [] },
    { source: "version_change" }, { source: "legacy" }, { version: null }, { version: 0 }, { status: "requested" },
    { status: "executing" }, { status: "completed" }, { status: "failed" }])
    assert.deepEqual(refundExecutionActions({ ...approved, ...changes }, admin), []);
  for (const platform_role of ["finance_settlement", "delivery_monitor", "enterprise_admin", null])
    assert.deepEqual(refundExecutionActions(approved, { ...admin, platform_role }), []);
  assert.deepEqual(refundExecutionActions(approved, { ...admin, platform_role: "platform_operator" }), ["execute"]);
});
test("execute sends strict body then refreshes list and detail; retry uses failed version", async () => {
  const h = harness();
  assert.equal(await h.session.send("execute", { reason: " 确认执行 ", amount: "900", cost: "private" }), true);
  assert.deepEqual(h.calls[0], ["/refunds/refund/execute", { expected_version: 4, reason: "确认执行", idempotency_key: "execution-key-1" }, { params: h.context.params }]);
  assert.deepEqual(h.calls.slice(1).map(call => call[0]), ["/refunds", "/refunds/refund"]);
  assert.match(h.state.message, /不代表银行已到账/);
  h.state.item = { ...approved, status: "failed", version: 5, allowed_actions: ["retry"] };
  assert.equal(await h.session.send("retry", { reason: "核对后重试" }), true);
  assert.equal(h.calls[3][0], "/refunds/refund/retry"); assert.equal(h.calls[3][1].expected_version, 5);
});
test("no execution request without permission or reason", async () => {
  const h = harness();
  assert.equal(await h.session.send("execute", { reason: " " }), false);
  h.context.user = { ...admin, platform_role: "finance_settlement" };
  assert.equal(await h.session.send("execute", { reason: "执行" }), false);
  assert.equal(h.calls.length, 0);
});
test("execution is single flight and network retry keeps key without exposing error payload", async () => {
  const h = harness(); let reject;
  h.api.post = (path, body) => { h.calls.push([path, body]); return new Promise((resolve, fail) => { reject = fail; }); };
  const pending = h.session.send("execute", { reason: "执行" });
  assert.equal(await h.session.send("execute", { reason: "执行" }), false);
  reject({ response: { status: 502, data: { detail: "PRIVATE_SECRET_COST" } } }); await pending;
  assert.doesNotMatch(h.state.error, /PRIVATE/);
  h.api.post = async (path, body) => { h.calls.push([path, body]); throw Error("offline"); };
  await h.session.send("execute", { reason: "执行" });
  assert.equal(h.calls[0][1].idempotency_key, h.calls[1][1].idempotency_key);
});
test("late execution result cannot overwrite changed enterprise or selected detail", async () => {
  for (const change of [h => { h.context.enterpriseId = "other"; h.session.reset(); },
    h => { h.state.item = { ...approved, id: "another-refund" }; }]) {
    const h = harness(); let finish;
    h.api.post = () => new Promise(resolve => { finish = resolve; });
    const pending = h.session.send("execute", { reason: "执行" }); change(h);
    const item = h.state.item; finish({ data: { ...approved, status: "completed" } });
    assert.equal(await pending, false); assert.equal(h.state.item, item);
  }
});
test("409 refreshes detail and requires new confirmation/version", async () => {
  const h = harness(); h.api.post = async () => { throw { response: { status: 409 } }; };
  assert.equal(await h.session.send("execute", { reason: "执行" }), false);
  assert.equal(h.calls[0][0], "/refunds/refund"); assert.match(h.state.error, /重新确认/);
});
test("execution projection allowlists progress and never passes cost, secrets or raw failures", () => {
  const safe = refundExecutionProgress({ execution: { status: "failed", resource_status: "completed", ledger_status: "pending",
    last_error: "PRIVATE_SECRET", cost: "PRIVATE_COST", legs: [{ status: "completed", amount: "12.50", secret: "PRIVATE_KEY" },
      { status: "failed", amount: "2.00", last_error: "PRIVATE_TOKEN" }] } });
  assert.doesNotMatch(JSON.stringify(safe), /PRIVATE|secret|cost/); assert.equal(safe.legs.length, 2);
  assert.equal(safe.legs[0].amount, "¥12.50");
});
test("current workflow top-level execution output drives real progress without invented legs", () => {
  const realShape = { ...approved, execution_status: "resources_pending",
    execution_error_code: "resource_failed", execution_mode: "simulated" };
  const progress = refundExecutionProgress(realShape);
  assert.equal(progress.status, "等待资源回收");
  assert.equal(progress.failure, "资源回收失败"); assert.equal(progress.simulated, true);
  assert.equal(progress.resource, "状态待确认"); assert.equal(progress.ledger, "状态待确认");
  assert.equal(progress.legsAvailable, false); assert.deepEqual(progress.legs, []);
  assert.equal(refundExecutionProgress({ execution_status: null, execution_error_code: null }), null);
});
test("backend applied resource and completion_failed code have safe labels", () => {
  const progress = refundExecutionProgress({ execution: { status: "failed", resource_status: "applied",
    error_code: "completion_failed", ledger_status: "pending_be013" } });
  assert.equal(progress.resource, "已回收");
  assert.equal(progress.failure, "退款完成确认失败，请由平台核对已完成资金腿后重试");
  assert.equal(progress.ledger, "待清算接入");
});
test("pending_be013 is pending clearing integration, never posted or completed", () => {
  for (const item of [{ execution: { status: "completed", ledger_status: "pending_be013", payment_legs: [] } },
    { execution_status: "completed", ledger_status: "pending_be013", payment_legs: [] }]) {
    const progress = refundExecutionProgress(item);
    assert.equal(progress.ledger, "待清算接入"); assert.doesNotMatch(progress.ledger, /已入账|已完成/);
    assert.equal(progress.legsAvailable, true);
  }
});
test("BE013 ledger labels distinguish monthly clearing from refund bank settlement", () => {
  for (const [ledger_status, label] of Object.entries({ pending_monthly: "待纳入月度清算",
    settlement_generated: "负向清算单已生成", in_batch: "已纳入清算批次", paid: "清算已付款",
    pending_be013: "待清算接入" })) {
    for (const item of [{ execution: { status: "completed", ledger_status, funds_mode: "simulated", payment_legs: [] } },
      { execution_status: "completed", ledger_status, execution_mode: "simulated", payment_legs: [] }]) {
      const progress = refundExecutionProgress(item);
      assert.equal(progress.ledger, label);
      assert.equal(progress.status, "已完成");
      assert.equal(progress.simulated, true);
      assert.doesNotMatch(progress.ledger, /银行|退款已到账|退款已结算/);
    }
  }
  assert.equal(refundStatus("completed"), "已退款");
});
test("BE013 execution DTO projection excludes private financial and source evidence", () => {
  for (const ledger_status of ["pending_monthly", "settlement_generated", "in_batch", "paid"]) {
    const privateFields = { cost_reversal: "PRIVATE_COST", settlement_rule: { account: "PRIVATE_ACCOUNT" },
      payment_id: "PRIVATE_PAYMENT", source_order_id: "PRIVATE_SOURCE", token: "PRIVATE_TOKEN",
      resource_plan_json: "PRIVATE_RESOURCE", receipt: "PRIVATE_RECEIPT", last_error: "PRIVATE_URL" };
    for (const item of [{ ...privateFields, execution: { ...privateFields, status: "completed", ledger_status,
      resource_status: "applied", funds_mode: "simulated", payment_legs: [{ ...privateFields, amount: "12.50", status: "completed" }] } },
    { ...privateFields, execution_status: "completed", ledger_status, resource_status: "applied",
      execution_mode: "simulated", payment_legs: [{ ...privateFields, amount: "12.50", status: "completed" }] }]) {
      const progress = refundExecutionProgress(item);
      assert.doesNotMatch(JSON.stringify(progress), /PRIVATE|cost_reversal|settlement_rule|payment_id|source_order_id|token|resource_plan_json|receipt|last_error/);
      assert.deepEqual(progress.legs, [{ label: "资金腿 1", amount: "¥12.50", status: "已完成", failure: "执行未完成，请由平台核对后处理" }]);
    }
  }
});
test("execution uses existing modal and components compile", () => {
  for (const name of ["RefundCenter", "RefundDetail"]) {
    const source = readFileSync(new URL(`../src/${name}.vue`, import.meta.url), "utf8");
    assert.doesNotMatch(source, /window\.prompt|window\.confirm|JSON\.stringify\(item/);
    const { descriptor, errors } = parse(source); assert.deepEqual(errors, []);
    const script = compileScript(descriptor, { id: name });
    assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: `${name}.vue`, id: name,
      compilerOptions: { bindingMetadata: script.bindings } }).errors, []);
    if (name === "RefundCenter") assert.match(source, /<ActionDialog/);
  }
});
test("execution capability controls actions, not in-progress or completed service wording", async () => {
  const { descriptor } = parse(readFileSync(new URL("../src/RefundDetail.vue", import.meta.url), "utf8"));
  async function render(item) {
    const stub = { render: () => null };
    return renderToString(createSSRApp({ components: { RefundReviewForm: stub, RefundManualAssessment: stub,
      Play: stub, RotateCcw: stub, RefreshCw: stub, X: stub },
    setup: () => ({ item, user: admin, busy: false, error: "", allowedActions: [], approvalAvailable: false,
      progress: refundExecutionProgress(item), date: () => "", emit: () => {},
      refundStatus, refundMoney, refundSource, refundPlanning, canAssessRefund, refundExecutionActions }),
    render: compile(descriptor.template.content) }));
  }
  const executing = await render({ ...approved, status: "executing", execution_available: false });
  assert.match(executing, /退款执行中/); assert.doesNotMatch(executing, /资金与资源执行尚不可用/);
  const progress = await render({ ...approved, status: "executing", execution_available: false, execution_status: "funds_pending" });
  assert.match(progress, /等待资金处理/); assert.doesNotMatch(progress, /资金与资源执行尚不可用/);
  const completed = await render({ ...approved, status: "completed", execution_available: false, execution_mode: "simulated" });
  assert.match(completed, /模拟退款已完成/); assert.doesNotMatch(completed, /资金与资源执行尚不可用/);
  const unsupported = await render({ ...approved, execution_available: false });
  assert.match(unsupported, /资金与资源执行尚不可用/);
  const failed = await render({ ...approved, status: "failed", execution_available: false, execution_status: "failed" });
  assert.doesNotMatch(failed, /资金与资源执行尚不可用/);
});
