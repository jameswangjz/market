import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { compile, createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { refundAmount, refundMoney, refundReason, refundPreviewState, refundPlanning, refundStatus, refundSource, isRefundApprover, canRequestRefund } from "../src/refunds.js";
import { orderWorkflowActions } from "../src/orderWorkflow.js";

const buyer = { id: "buyer", verified_status: "verified", enterprise_memberships: [{ enterprise_id: "buyer-ent", role: "enterprise_admin", status: "active" }] };
test("refund amounts use exact positive cents without rounding invalid input", () => {
  assert.equal(refundAmount(" 12.5 "), "12.50"); assert.equal(refundAmount("0.01"), "0.01");
  assert.equal(refundAmount("999999999999.99"), "999999999999.99");
  for (const value of ["", null, "0", "-1", "1.001", "1e2", "Infinity", "01", "1000000000000", "NaN"])
    assert.throws(() => refundAmount(value));
  assert.equal(refundMoney(null), "—"); assert.equal(refundMoney(0), "¥0.00");
});
test("only selected active buyer administrators request, and finance never approves", () => {
  const order = { buyer_enterprise_id: "buyer-ent", payment_status: "paid" };
  assert.equal(canRequestRefund(order, buyer, "buyer-ent"), true);
  assert.equal(canRequestRefund(order, buyer, "other-ent"), false);
  assert.equal(canRequestRefund(order, { ...buyer, enterprise_memberships: [{ enterprise_id: "buyer-ent", role: "member", status: "active" }] }, "buyer-ent"), false);
  for (const platform_role of ["finance_settlement", "delivery_monitor", "security_compliance"])
    assert.equal(isRefundApprover({ id: "platform", verified_status: "verified", platform_role }), false);
  for (const platform_role of ["super_admin", "platform_operator"])
    assert.equal(isRefundApprover({ id: "platform", verified_status: "verified", platform_role }), true);
});
test("preview absence and missing history never imply zero usage or refundable amount", () => {
  assert.equal(refundPreviewState(null).available, false);
  assert.match(refundPreviewState({ reason: "usage_basis_pending" }).reason, /扣减额度账本尚未就绪/);
  assert.match(refundPreviewState({ reason: "historical_evidence_missing" }).reason, /不能按零用量/);
  assert.equal(refundStatus("future"), "状态未知"); assert.equal(refundSource("future"), "来源未明确");
  assert.throws(() => refundReason("  ")); assert.equal(refundReason(" 理由 "), "理由");
});
test("request form renders unavailable without manufactured monetary preview", async () => {
  const source = readFileSync(new URL("../src/RefundRequestForm.vue", import.meta.url), "utf8");
  const { descriptor } = parse(source);
  const html = await renderToString(createSSRApp({ components: { Send: { render: () => null } }, setup: () => ({
    busy: false, enabled: false, error: "", validation: "", orderNo: "ORD-ISOLATED", preview: null,
    previewState: refundPlanning(null), quoteBusy: false, quote: () => {}, amount: "", reason: "", ticketId: "", paymentId: "", refundMoney, submit: () => {},
  }), render: compile(descriptor.template.content) }));
  assert.match(html, /试算不可用/); assert.match(html, /不推断可退金额/); assert.doesNotMatch(html, /¥0\.00/);
});
test("finance review template has no approval buttons and unknown approval plan stays disabled", async () => {
  const source = readFileSync(new URL("../src/RefundReviewForm.vue", import.meta.url), "utf8");
  const { descriptor } = parse(source);
  const render = async permitted => renderToString(createSSRApp({ components: { Check: { render: () => null }, X: { render: () => null } },
    setup: () => ({ permitted, allowedActions: ["approve", "reject"], approvalAvailable: false, busy: false, reason: "审核理由", error: "", submit: () => {} }), render: compile(descriptor.template.content) }));
  assert.doesNotMatch(await render(false), /批准申请|拒绝申请/);
  assert.match(await render(true), /可信试算及经济方案尚不可用/); assert.match(await render(true), /disabled/);
});
test("pure refund components compile without execution or legacy refund transitions", () => {
  for (const name of ["RefundRequestForm", "RefundList", "RefundDetail", "RefundReviewForm", "RefundManualAssessment", "RefundCenter"]) {
    const source = readFileSync(new URL(`../src/${name}.vue`, import.meta.url), "utf8");
    const { descriptor, errors } = parse(source); assert.deepEqual(errors, []);
    const script = compileScript(descriptor, { id: name });
    assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: `${name}.vue`, id: name,
      compilerOptions: { bindingMetadata: script.bindings } }).errors, []);
    assert.doesNotMatch(source, /approve_refund|complete_refund|\/execute|\.post\(|cost_amount|cost_price/);
  }
});

test("legacy refund buttons are removed and finance cannot confirm payment", () => {
  const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
  const actions = source.slice(source.indexOf("function nextActions(order)"), source.indexOf("onMounted(() =>", source.indexOf("function nextActions(order)")));
  assert.doesNotMatch(actions, /approve_refund|complete_refund/);
  assert.deepEqual(orderWorkflowActions({ snapshot_version: 1, main_status: "pending_payment", payment_status: "paying" },
    { id: "finance", platform_role: "finance_settlement" }, null), []);
});
