import test from "node:test";
import assert from "node:assert/strict";
import { compile, createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { canCreateAfterSales } from "../src/afterSales.js";
import { canReviewProviderOrder, orderWorkflowActions, providerReviewPayload, isOfflineFulfillmentOrder, offlineDeliveryActions, canUploadDeliveryAttachment, deliveryTransitionPayload, orderTypeLabel, orderServiceDates } from "../src/orderWorkflow.js";

const enterprise = { id: "provider" };
const admin = { enterprise_role: "enterprise_admin", membership_status: "active" };
const order = { id: "order-1", order_no: "ORD-1", provider_enterprise_id: "provider", buyer_enterprise_id: "buyer", snapshot_version: 1, main_status: "pending_provider_review", payment_status: "unpaid", delivery_status: "not_started" };
const quote = { order_id: order.id, amount: "100", cost: "60", unit_price: "100", unit_cost: "60", main_status: "pending_provider_review", payment_status: "unpaid", expected_updated_at: "2026-10-10T12:00:00Z" };

test("provider review requires current active provider admin membership", () => {
  assert.equal(canReviewProviderOrder(order, admin, enterprise), true);
  assert.equal(canReviewProviderOrder(order, { ...admin, enterprise_role: "super_admin" }, enterprise), true);
  for (const user of [{ ...admin, enterprise_role: "member" }, { ...admin, membership_status: "disabled" }, { ...admin, is_active: false }, { ...admin, activation_status: "pending_activation" }, { ...admin, platform_role: "super_admin" }]) {
    assert.equal(canReviewProviderOrder(order, user, enterprise), false);
  }
  assert.equal(canReviewProviderOrder(order, admin, { id: "buyer" }), false);
  assert.equal(canReviewProviderOrder(order, admin, null), false);
  assert.equal(canReviewProviderOrder({ ...order, main_status: "pending_payment" }, admin, enterprise), false);
  assert.equal(canReviewProviderOrder(order, { ...admin, enterprise_memberships: [{ enterprise_id: "provider", role: "super_admin", status: "disabled" }, { enterprise_id: "buyer", role: "super_admin", status: "active" }] }, enterprise), false);
  assert.equal(canReviewProviderOrder(order, { ...admin, enterprise_role: "member", enterprise_memberships: [{ enterprise_id: "provider", role: "super_admin", membership_status: "active" }] }, enterprise), true);
});

test("only current buyer super admin can initiate payment and buyers never confirm", () => {
  const pending = { ...order, main_status: "pending_payment" };
  const buyer = { ...admin, enterprise_role: "super_admin" };
  assert.deepEqual(orderWorkflowActions(pending, buyer, { id: "buyer" }), [["start_payment", "发起模拟支付"]]);
  assert.deepEqual(orderWorkflowActions({ ...pending, payment_status: "paying" }, buyer, { id: "buyer" }), []);
  for (const user of [admin, { ...buyer, membership_status: "disabled" }, { ...buyer, enterprise_memberships: [] }]) {
    assert.deepEqual(orderWorkflowActions(pending, user, { id: "buyer" }), []);
  }
  assert.deepEqual(orderWorkflowActions(pending, buyer, enterprise), []);
  for (const main_status of ["pending_provider_review", "rejected", "cancelled", "closed", "completed", "created"]) {
    assert.deepEqual(orderWorkflowActions({ ...pending, main_status }, buyer, { id: "buyer" }), []);
  }
});

test("only platform operator and super admin have manual confirmation", () => {
  for (const platform_role of ["super_admin", "platform_operator"]) {
    for (const payment_status of ["unpaid", "paying"]) {
      assert.deepEqual(orderWorkflowActions({ ...order, main_status: "pending_payment", payment_status }, { platform_role }, null), [["confirm_payment", "人工确认支付"]]);
    }
    assert.deepEqual(orderWorkflowActions(order, { platform_role }, null), []);
    assert.deepEqual(orderWorkflowActions({ ...order, main_status: "pending_payment", payment_status: "paid" }, { platform_role }, null), []);
  }
  for (const platform_role of ["finance_settlement", "delivery_monitor", "security_compliance"]) {
    assert.deepEqual(orderWorkflowActions({ ...order, main_status: "pending_payment" }, { platform_role }, null), []);
  }
});

test("legacy unpaid orders retain payment with the same buyer permissions", () => {
  const legacy = { ...order, snapshot_version: 0, main_status: "pending_fulfillment" };
  assert.deepEqual(orderWorkflowActions(legacy, { ...admin, enterprise_role: "super_admin" }, { id: "buyer" }), [["start_payment", "发起模拟支付"]]);
  assert.deepEqual(orderWorkflowActions(legacy, admin, { id: "buyer" }), []);
  assert.deepEqual(orderWorkflowActions({ ...legacy, main_status: "rejected" }, { platform_role: "super_admin" }, null), []);
});

test("review validates economics, changed reasons, rejection and timestamp", () => {
  const form = { amount: "100", cost: "60", reason: "" };
  assert.deepEqual(providerReviewPayload(quote, form, "approve"), { decision: "approve", amount: 100, cost: 60, reason: "", expected_updated_at: quote.expected_updated_at });
  for (const changes of [{ amount: 59 }, { amount: "" }, { cost: null }, { amount: Infinity }, { cost: -1 }, { amount: 101 }, { cost: 61 }]) {
    assert.throws(() => providerReviewPayload(quote, { ...form, ...changes }, "approve"));
  }
  assert.equal(providerReviewPayload(quote, { ...form, amount: 101, reason: " change " }, "approve").reason, "change");
  assert.equal(providerReviewPayload(quote, { ...form, amount: 60, reason: "adjust" }, "approve").amount, 60);
});

test("reject requires reason and sends no edited economics", () => {
  assert.throws(() => providerReviewPayload(quote, { reason: "  " }, "reject"));
  assert.deepEqual(providerReviewPayload(quote, { amount: "invalid", cost: -1, reason: " declined " }, "reject"), { decision: "reject", reason: "declined", expected_updated_at: quote.expected_updated_at });
  assert.throws(() => providerReviewPayload({ ...quote, expected_updated_at: null, updated_at: "wrong-field" }, { reason: "yes" }, "approve"));
});

test("confirmed payment applies server order values and only patches the matching drawer", () => {
  const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
  const state = {
    ordersListRequest: 4,
    orders: { value: [{ ...order }, { ...order, id: "other" }] },
    selectedOrder: { value: { order: { ...order }, payment: { status: "paying" }, logs: [] } },
  };
  vm.createContext(state);
  vm.runInContext(source.slice(source.indexOf("function applyConfirmedPaymentOrder("), source.indexOf("async function loadViewData(")), state);
  const authoritative = { ...order, payment_status: "paid", main_status: "pending_fulfillment", paid_amount: 100, updated_at: "server-time" };
  state.applyConfirmedPaymentOrder(authoritative, order.id);
  assert.equal(state.ordersListRequest, 5);
  assert.deepEqual({ ...state.orders.value[0] }, authoritative);
  assert.equal(state.orders.value[1].payment_status, "unpaid");
  assert.deepEqual({ ...state.selectedOrder.value.order }, authoritative);
  assert.equal(state.selectedOrder.value.payment.status, "paid");
  assert.equal(state.selectedOrder.value.payment.amount, 100);

  state.selectedOrder.value = { order: { ...order, id: "other" }, payment: null };
  const currentDrawer = state.selectedOrder.value;
  state.applyConfirmedPaymentOrder(authoritative, order.id);
  assert.equal(state.selectedOrder.value, currentDrawer);
  const transition = source.slice(source.indexOf("async function transition(action)"), source.indexOf("function closeProviderReview()"));
  assert.match(transition, /const \{ data: updatedOrder \} = await api\.post/);
  assert.match(transition, /if \(action === "confirm_payment"\) \{[\s\S]*?updatedOrder\?\.id === order\.id\) applyConfirmedPaymentOrder/);
  assert.match(transition, /try \{ await refreshData\(\); \}[\s\S]*?catch \{ notify\("订单状态已更新，列表刷新失败"\); \}/);
});

const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
test("refund and non-applicable orders never offer payment, including inconsistent pending-payment rows", async () => {
  const buyer = { ...admin, enterprise_role: "super_admin" };
  for (const actor of [buyer, { platform_role: "super_admin" }, { platform_role: "platform_operator" }]) {
    for (const patch of [{ amount: -12.5 }, { amount: "-12.50" }, { order_type: "refund" },
      { payment_status: "not_applicable" }, { delivery_status: "not_applicable" },
      { order_type: "downgrade", main_status: "pending_fulfillment", payment_status: "not_applicable" }]) {
      const item = { ...order, main_status: "pending_payment", payment_status: "unpaid", ...patch };
      assert.deepEqual(orderWorkflowActions(item, actor, { id: "buyer" }), []);
      const s = harness();
      s.selectedOrder.value = { order: item };
      s.user.value = actor;
      s.enterprise.value = { id: "buyer" };
      await s.transition("start_payment");
      await s.transition("confirm_payment");
      assert.deepEqual(s.calls, []);
    }
  }
});

test("refund list renders negative amounts and Chinese refund and non-applicable states", async () => {
  const state = {};
  vm.createContext(state);
  vm.runInContext(source.slice(source.indexOf("const statusLabels ="), source.indexOf("const settlementStatusLabels")) +
    source.slice(source.indexOf("function fmtMoney("), source.indexOf("const settlementLifecycleLabels")) +
    "\nfunction label(value) { return statusLabels[value] || value; }", state);
  const rowStart = source.indexOf('<tr v-for="order in filteredOrders"');
  const row = source.slice(rowStart, source.indexOf("</tr>", rowStart) + 5);
  for (const [main_status, expected] of Object.entries({ refund_pending: "退款待处理", refund_completed: "退款已完成", refund_rejected: "退款已拒绝", pending_fulfillment: "待履约" })) {
    const order = { id: "refund", order_no: "REF-1", order_type: "refund", amount: -12.5, main_status,
      payment_status: "not_applicable", delivery_status: "not_applicable", after_sales_status: "none" };
    const html = await renderToString(createSSRApp({
      components: { ChevronRight: { render: () => null }, ExternalLink: { render: () => null }, Download: { render: () => null } },
      setup: () => ({ filteredOrders: [order], fmtMoney: state.fmtMoney, label: state.label, orderTypeLabel, orderServiceDates,
        fmtDate: () => "-", partyName: () => "企业", deliveryMethod: () => "file", canOpenDeliveryAccess: () => false,
        deliveryAccessPlaceholder: () => "-", openOrder: () => {} }),
      render: compile(`<table><tbody>${row}</tbody></table>`),
    }));
    assert.match(html, /¥-12\.50/);
    assert.ok(html.includes(expected));
    assert.equal((html.match(/不适用/g) || []).length, 2);
  }
});

test("order types and service dates use API values without inventing a start date", () => {
  for (const [order_type, expected] of Object.entries({ new_purchase: "新购", renewal: "续费", upgrade: "升级", downgrade: "降级", refund: "退费" })) {
    assert.equal(orderTypeLabel({ order_type }), expected);
  }
  assert.equal(orderTypeLabel({}), "-");
  const format = value => `formatted:${value}`;
  const fields = { service_starts_at: "2030-01-01", service_ends_at: "2030-02-01", service_effective_at: "2030-01-02" };
  assert.deepEqual(orderServiceDates(fields, format), { starts: "formatted:2030-01-01", ends: "formatted:2030-02-01", effective: "formatted:2030-01-02" });
  assert.deepEqual(orderServiceDates({ delivery_method: "file", created_at: "2030-01-01", updated_at: "2030-01-02" }, format),
    { starts: "待交付确定", ends: "无限期", effective: "待交付确定" });
  assert.equal(orderServiceDates({ delivery_method_snapshot: "file" }, format).ends, "无限期");
  assert.equal(orderServiceDates({ delivery_method: "api" }, format).ends, "待交付确定");
  assert.equal(orderServiceDates({ delivery_method: "file", ...fields }, format).ends, "formatted:2030-02-01");
});

test("list and drawer show immutable order metadata and retain offline status labels", () => {
  for (const expression of ["orderTypeLabel(order)", "orderTypeLabel(selectedOrder.order)", "order.product_version_code", "selectedOrder.order.product_version_code"]) {
    assert.ok(source.includes(expression), expression);
  }
  for (const field of ["starts", "ends", "effective"]) {
    assert.ok(source.includes(`orderServiceDates(order, fmtDate).${field}`));
    assert.ok(source.includes(`orderServiceDates(selectedOrder.order, fmtDate).${field}`));
  }
  assert.match(source, /awaiting_effective: "待生效"/);
  for (const [key, value] of Object.entries({ pending_fulfillment: "待履约", fulfilling: "履约中", completed: "已完成", awaiting_start: "待开始履约", in_delivery: "交付中", pending_acceptance: "待验收", rectifying: "整改中", accepted: "验收通过", exception: "交付异常" })) {
    assert.ok(source.includes(`${key}: "${value}"`));
  }
});

test("confirmed version change refreshes orders even when detail reload fails", async () => {
  const calls = [];
  const state = { selectedOrder: { value: { order } }, versionChangeSession: { reset: () => calls.push("reset") },
    openOrder: async () => { calls.push("detail"); throw new Error("detail unavailable"); },
    refreshData: async () => calls.push("orders") };
  vm.createContext(state);
  vm.runInContext(source.slice(source.indexOf("async function refreshVersionChangeContext()"), source.indexOf("const orderProductFiles")), state);
  await assert.rejects(state.refreshVersionChangeContext(), /detail unavailable/);
  assert.deepEqual(calls, ["reset", "detail", "orders"]);
  const executionSource = readFileSync(new URL("../src/VersionChangeExecution.vue", import.meta.url), "utf8");
  assert.match(executionSource, /if \(await execution.submit\(\)\) \{ props.quoteSession.reset\(\); emit\("refresh"\); \}/);
});

test("buyer transition cannot call confirmation even when invoked directly", async () => {
  const s = harness();
  s.selectedOrder.value = { order: { ...order, main_status: "pending_payment", payment_status: "paying" } };
  s.enterprise.value = { id: "buyer" };
  s.user.value = { ...admin, enterprise_role: "super_admin" };
  await s.transition("confirm_payment");
  assert.deepEqual(s.calls, []);
});

test("order drawer does not expose tenant close or restore actions", () => {
  assert.doesNotMatch(source, /关闭租户|恢复租户/);
  assert.doesNotMatch(source, /saasOrderAction\('(?:close|restore)'\)/);
});
function harness() {
  const state = {
    selectedOrder: { value: { order: { ...order } } }, user: { value: { ...admin } }, enterprise: { value: enterprise },
    providerReview: { value: null }, providerReviewForm: { value: { amount: "", cost: "", reason: "" } },
    providerReviewLoading: { value: false }, providerReviewSubmitting: { value: false }, orderActionSubmitting: { value: false },
    canReviewProviderOrder, orderWorkflowActions, providerReviewPayload, isOfflineFulfillmentOrder, canUploadDeliveryAttachment, deliveryTransitionPayload, canCreateAfterSales,
    afterSalesCreateOrderId: { value: null },
    deliveryAttachmentUploading: { value: false }, deliveryAttachmentDescription: { value: "" },
    orderDeliveryAttachments: { value: { orderId: "", tasks: [], items: [], loading: false, error: "" } }, FormData,
    calls: [], messages: [], refreshes: [], api: {}, confirmDialog: async () => true, promptDialog: async () => "reason",
    fmtMoney: value => String(value),
  };
  state.api.get = async url => { state.calls.push(["get", url]); return { data: quote }; };
  state.api.post = async (url, body) => { state.calls.push(["post", url, body]); return { data: undefined }; };
  state.notify = message => state.messages.push(message);
  state.openOrder = async item => state.refreshes.push(item.id);
  state.refreshData = async () => state.refreshes.push("list");
  vm.createContext(state);
  vm.runInContext(source.slice(source.indexOf("async function loadOrderDeliveryAttachments(order)"), source.indexOf("async function saasSyncUser()")) + source.slice(source.indexOf("function nextActions(order)"), source.indexOf("onMounted(() =>")), state);
  return state;
}

test("service retry is restricted to active delivery operators and retryable activation states", async () => {
  for (const platform_role of ["super_admin", "platform_operator", "delivery_monitor", "finance_settlement", "security_compliance", ""]) {
    for (const service_activation_status of ["scheduled", "retrying", "needs_manual", "activation_verified", "active", "completed"]) {
      const s = harness();
      s.user.value = { ...admin, platform_role, enterprise_role: "super_admin" };
      s.selectedOrder.value.order = { ...order, main_status: "fulfilling", delivery_status: "exception", service_activation_status };
      const expected = ["super_admin", "platform_operator", "delivery_monitor"].includes(platform_role) && ["retrying", "needs_manual"].includes(service_activation_status);
      assert.equal(s.nextActions(s.selectedOrder.value.order).some(([action]) => action === "service_retry"), expected);
      assert.equal(s.nextActions(s.selectedOrder.value.order).some(([action]) => action === "retry_delivery"), false);
      if (!expected) {
        await s.transition("service_retry");
        assert.deepEqual(s.calls, []);
      }
    }
  }
  for (const patch of [{ is_active: false }, { activation_status: "disabled" }]) {
    const s = harness();
    s.user.value = { platform_role: "platform_operator", ...patch };
    s.selectedOrder.value.order.service_activation_status = "needs_manual";
    await s.transition("service_retry");
    assert.deepEqual(s.calls, []);
  }
});

test("service retry posts once to its dedicated endpoint and reloads authoritative detail and list", async () => {
  const s = harness();
  s.user.value = { platform_role: "delivery_monitor" };
  s.selectedOrder.value.order = { ...order, main_status: "fulfilling", service_activation_status: "needs_manual", service_activation_attempts: 3 };
  let finish;
  s.api.post = async (...args) => { s.calls.push(args); await new Promise(resolve => { finish = resolve; }); return { data: { order_id: order.id, status: "scheduled" } }; };
  const pending = s.transition("service_retry");
  await s.transition("service_retry");
  assert.equal(s.calls.length, 1);
  assert.equal(s.calls[0][0], "/orders/order-1/service-retry");
  assert.equal(s.calls[0].length, 1);
  finish();
  await pending;
  assert.deepEqual(s.refreshes, ["order-1", "list"]);
  assert.equal(s.orderActionSubmitting.value, false);
});

test("service retry errors release busy state and detail failure still refreshes list", async () => {
  const s = harness();
  s.user.value = { platform_role: "super_admin" };
  s.selectedOrder.value.order.service_activation_status = "retrying";
  s.api.post = async () => { throw new Error("retry failed"); };
  await s.transition("service_retry");
  assert.ok(s.messages.includes("retry failed"));
  assert.equal(s.orderActionSubmitting.value, false);
  s.api.post = async () => ({ data: { status: "scheduled" } });
  s.openOrder = async () => { throw new Error("detail failed"); };
  await s.transition("service_retry");
  assert.deepEqual(s.refreshes, ["list"]);
  assert.equal(s.orderActionSubmitting.value, false);
});

test("activation detail renders escaped error, attempt count and Chinese manual handoff", async () => {
  const start = source.indexOf('<section v-if="selectedOrder.order.service_activation_status"');
  const template = source.slice(start, source.indexOf("</section>", start) + 10);
  for (const [status, expected] of Object.entries({ scheduled: "待生效检查", retrying: "重试中", needs_manual: "待人工处理", activation_verified: "生效已验证", active: "已生效", completed: "已结束" })) {
    const html = await renderToString(createSSRApp({ setup: () => ({ selectedOrder: { order: {
      service_activation_status: status, service_activation_attempts: 3, service_activation_error: '<script>alert("failed")</script>',
    } } }), render: compile(template) }));
    assert.ok(html.includes(expected));
    assert.match(html, /尝试次数<\/dt><dd>3<\/dd>/);
    assert.match(html, /&lt;script&gt;/);
    assert.doesNotMatch(html, /<script>/);
  }
});

test("upgrade and downgrade detail render only public related order and service periods", async () => {
  const start = source.indexOf('<div v-if="[\'upgrade\', \'downgrade\'].includes(selectedOrder.order.order_type)');
  const template = source.slice(start, source.indexOf("</div>", start) + 6);
  const render = (order_type, service_segments) => renderToString(createSSRApp({
    setup: () => ({ selectedOrder: { order: { order_type, service_segments } }, fmtDate: value => `日期:${value}` }),
    render: compile(template),
  }));
  const segments = [{ order_id: "source-1", order_no: "ORD-SOURCE-1", starts_at: "2030-01-01", ends_at: "2030-02-01", cost: "PRIVATE-COST" },
    { order_id: "source-2", order_no: "ORD-SOURCE-2", starts_at: null, ends_at: null }];
  for (const type of ["upgrade", "downgrade"]) {
    const html = await render(type, segments);
    assert.match(html, /ORD-SOURCE-1/);
    assert.match(html, /ORD-SOURCE-2/);
    assert.match(html, /日期:2030-01-01/);
    assert.match(html, /日期:2030-02-01/);
    assert.match(html, /待交付确定/);
    assert.doesNotMatch(html, /PRIVATE-COST/);
    assert.doesNotMatch(await render(type, undefined), /<table/);
  }
  assert.doesNotMatch(await render("new_purchase", segments), /<table/);
  const relatedStart = source.indexOf('<div v-if="selectedOrder.order.related_order_no"');
  const related = source.slice(relatedStart, source.indexOf("</div>", relatedStart) + 6);
  const html = await renderToString(createSSRApp({ setup: () => ({ selectedOrder: { order: { related_order_no: "ORD-ORIGINAL" } } }), render: compile(related) }));
  assert.match(html, /原订单号/);
  assert.match(html, /ORD-ORIGINAL/);
});

test("quote is fetched only for provider and stays out of buyer state", async () => {
  const s = harness();
  s.enterprise.value = { id: "buyer" };
  await s.openProviderReview();
  assert.equal(s.calls.length, 0);
  assert.equal(s.providerReview.value, null);
  s.enterprise.value = enterprise;
  await s.openProviderReview();
  assert.equal(s.calls[0][1], "/orders/order-1/provider-quote");
  assert.equal(s.providerReview.value.quote.cost, "60");
  assert.equal(s.selectedOrder.value.order.cost, undefined);
  s.closeProviderReview();
  assert.equal(s.providerReview.value, null);
  assert.equal(s.providerReviewForm.value.cost, "");
});

test("review posts once with expected timestamp and refreshes detail/list", async () => {
  const s = harness();
  await s.openProviderReview();
  let finish;
  s.api.post = async (url, body) => { s.calls.push(["post", url, body]); await new Promise(resolve => { finish = resolve; }); };
  const pending = s.submitProviderReview("approve");
  await s.submitProviderReview("reject");
  s.closeProviderReview();
  assert.notEqual(s.providerReview.value, null);
  finish(); await pending;
  const posts = s.calls.filter(item => item[0] === "post");
  assert.equal(posts.length, 1);
  assert.equal(posts[0][1], "/orders/order-1/provider-review");
  assert.equal(posts[0][2].expected_updated_at, quote.expected_updated_at);
  assert.deepEqual(s.refreshes, ["order-1", "list"]);
  assert.equal(s.providerReview.value, null);
  assert.equal(s.providerReviewSubmitting.value, false);
});

test("actual quote preserves expected timestamp and row number; malformed or stale responses stay closed", async () => {
  const s = harness();
  s.api.get = async () => ({ data: { ...quote, updated_at: "wrong-field" } });
  await s.openProviderReview();
  assert.equal(s.providerReview.value.quote.expected_updated_at, quote.expected_updated_at);
  assert.equal(s.providerReview.value.quote.order_no, order.order_no);
  s.closeProviderReview();
  s.api.get = async () => ({ data: { ...quote, cost: "bad" } });
  await s.openProviderReview();
  assert.equal(s.providerReview.value, null);
  let finish;
  s.api.get = async () => new Promise(resolve => { finish = resolve; });
  const pending = s.openProviderReview();
  s.enterprise.value = { id: "buyer" };
  finish({ data: quote }); await pending;
  assert.equal(s.providerReview.value, null);
  assert.equal(s.providerReviewLoading.value, false);
});

test("permission or selected order changes during payment dialog cancel submission", async () => {
  for (const change of [s => { s.user.value.membership_status = "disabled"; }, s => { s.selectedOrder.value = null; }]) {
    const s = harness();
    s.selectedOrder.value.order.main_status = "pending_payment";
    s.user.value.enterprise_role = "super_admin";
    s.enterprise.value = { id: "buyer" };
    s.confirmDialog = async () => { change(s); return true; };
    await s.transition("start_payment");
    assert.equal(s.calls.length, 0);
    assert.equal(s.orderActionSubmitting.value, false);
  }
});

test("conflict clears stale quote and refreshes; ordinary failure permits retry", async () => {
  for (const status of [409, 500]) {
    const s = harness();
    await s.openProviderReview();
    s.api.post = async () => { throw { response: { status, data: { detail: "failure" } } }; };
    await s.submitProviderReview("approve");
    assert.equal(s.providerReviewSubmitting.value, false);
    assert.equal(s.providerReview.value === null, status === 409);
    assert.deepEqual(s.refreshes, status === 409 ? ["order-1", "list"] : []);
  }
});

test("cancelled and unauthorized payments send no request; duplicate clicks share one dialog", async () => {
  const s = harness();
  s.selectedOrder.value.order.main_status = "pending_payment";
  await s.transition("start_payment");
  assert.equal(s.calls.length, 0);
  s.user.value.enterprise_role = "super_admin";
  s.enterprise.value = { id: "buyer" };
  let finish, dialogs = 0;
  s.confirmDialog = async () => { dialogs++; return new Promise(resolve => { finish = resolve; }); };
  const pending = s.transition("start_payment");
  await s.transition("start_payment");
  assert.equal(dialogs, 1);
  finish(false); await pending;
  assert.equal(s.calls.length, 0);
  assert.equal(s.orderActionSubmitting.value, false);
  s.confirmDialog = async () => true;
  await s.transition("start_payment");
  assert.equal(s.calls[0][1], "/orders/order-1/transition");
  assert.equal(s.calls[0][2].action, "start_payment");
});

test("new pending and rejected orders never offer legacy review or delivery", () => {
  const s = harness();
  assert.deepEqual(Array.from(s.nextActions(order), item => item[0]), ["provider_review"]);
  assert.equal(s.nextActions({ ...order, main_status: "rejected" }).length, 0);
  assert.equal(s.nextActions({ ...order, main_status: "pending_payment" }).length, 0);
});

const offline = { ...order, delivery_method: "training", payment_status: "paid", main_status: "in_delivery", delivery_status: "in_delivery" };

test("offline fulfillment follows party permissions through start, delivery, rejection and acceptance", () => {
  for (const delivery_method of ["training", "consulting", "custom"]) {
    for (const role of ["super_admin", "enterprise_admin"]) {
      const actor = { ...admin, enterprise_role: role };
      for (const [delivery_status, providerActions, buyerActions] of [
        ["awaiting_start", ["start_delivery"], []],
        ["in_delivery", ["submit_delivery"], []],
        ["pending_acceptance", [], ["accept_delivery", "reject_delivery"]],
        ["rectifying", ["submit_delivery"], []],
        ["accepted", [], []],
      ]) {
        const item = { ...offline, delivery_method, delivery_status, main_status: delivery_status === "accepted" ? "completed" : delivery_status };
        assert.deepEqual(orderWorkflowActions(item, actor, enterprise).map(([action]) => action), providerActions);
        assert.deepEqual(orderWorkflowActions(item, actor, { id: "buyer" }).map(([action]) => action), buyerActions);
        assert.equal(canUploadDeliveryAttachment(item, actor, enterprise), providerActions.length > 0);
        assert.equal(canUploadDeliveryAttachment(item, actor, { id: "buyer" }), false);
      }
    }
  }
});

test("offline lifecycle excludes inactive users, unrelated enterprises, platform overrides and terminal orders", () => {
  for (const actor of [
    { ...admin, enterprise_role: "member" }, { ...admin, membership_status: "disabled" },
    { ...admin, is_active: false }, { ...admin, activation_status: "pending_activation" },
    { ...admin, enterprise_memberships: [] },
    ...["super_admin", "finance_settlement", "delivery_monitor", "platform_operator"].map(platform_role => ({ ...admin, platform_role })),
  ]) assert.deepEqual(offlineDeliveryActions(offline, actor, enterprise), []);
  assert.deepEqual(offlineDeliveryActions(offline, admin, { id: "outsider" }), []);
  assert.deepEqual(offlineDeliveryActions(offline, admin, null), []);
  assert.deepEqual(offlineDeliveryActions({ ...offline, main_status: "rectifying" }, admin, enterprise), []);
  for (const main_status of ["completed", "closed", "cancelled", "rejected", "pending_provider_review", "pending_payment"]) {
    assert.deepEqual(offlineDeliveryActions({ ...offline, main_status }, admin, enterprise), []);
  }
  for (const payment_status of ["unpaid", "paying", "refunding", "refunded"]) {
    assert.deepEqual(offlineDeliveryActions({ ...offline, payment_status }, admin, enterprise), []);
  }
  for (const item of [{ ...offline, snapshot_version: 0 }, { ...offline, delivery_method: "file" }, { ...offline, delivery_method: "api" }]) {
    assert.equal(isOfflineFulfillmentOrder(item), false);
    assert.deepEqual(offlineDeliveryActions(item, admin, enterprise), []);
  }
  assert.deepEqual(offlineDeliveryActions(offline, { enterprise_memberships: [{ enterprise_id: "provider", role: "enterprise_admin", status: "active" }] }, enterprise).map(([action]) => action), ["submit_delivery"]);
});

test("rejection requires a trimmed reason and delivery payloads use existing action names", () => {
  for (const reason of ["", "   ", "\n"]) assert.throws(() => deliveryTransitionPayload("reject_delivery", reason));
  assert.deepEqual(deliveryTransitionPayload("reject_delivery", " needs fixes "), { action: "reject_delivery", reason: "needs fixes" });
  for (const action of ["start_delivery", "submit_delivery", "accept_delivery"]) {
    assert.deepEqual(deliveryTransitionPayload(action), { action, reason: "工作台操作" });
  }
  assert.throws(() => deliveryTransitionPayload("confirm_order"));
});

test("offline drawer suppresses legacy completion, task creation and platform actions", () => {
  const s = harness();
  for (const delivery_status of ["awaiting_start", "in_delivery", "pending_acceptance", "rectifying", "accepted"]) {
    const item = { ...offline, delivery_status, main_status: delivery_status === "accepted" ? "completed" : delivery_status, after_sales_status: "none" };
    assert.deepEqual(Array.from(s.nextActions(item), ([action]) => action), offlineDeliveryActions(item, s.user.value, s.enterprise.value).map(([action]) => action));
    s.user.value.platform_role = "super_admin";
    assert.equal(s.nextActions(item).length, 0);
    delete s.user.value.platform_role;
  }
});

test("delivery rejection uses styled required dialog and posts its reason; cancellation sends nothing", async () => {
  for (const reason of [null, "   ", " needs fixes "]) {
    const s = harness();
    s.selectedOrder.value.order = { ...offline, main_status: "pending_acceptance", delivery_status: "pending_acceptance" };
    s.enterprise.value = { id: "buyer" };
    s.promptDialog = async (message, initial, options) => {
      assert.equal(options.required, true);
      assert.equal(options.multiline, true);
      return reason;
    };
    await s.transition("reject_delivery");
    assert.equal(s.calls.length, reason?.trim() ? 1 : 0);
    if (s.calls.length) {
      assert.equal(s.calls[0][1], "/orders/order-1/transition");
      assert.equal(s.calls[0][2].reason, "needs fixes");
      assert.deepEqual(s.refreshes, ["order-1", "list"]);
    }
    assert.equal(s.orderActionSubmitting.value, false);
  }
});

test("delivery dialogs recheck current party permissions and prevent duplicate submission", async () => {
  for (const action of ["start_delivery", "submit_delivery", "accept_delivery", "reject_delivery"]) {
    const s = harness();
    s.selectedOrder.value.order = { ...offline, delivery_status: action === "start_delivery" ? "awaiting_start" : action === "submit_delivery" ? "rectifying" : "pending_acceptance" };
    s.selectedOrder.value.order.main_status = s.selectedOrder.value.order.delivery_status;
    if (["accept_delivery", "reject_delivery"].includes(action)) s.enterprise.value = { id: "buyer" };
    let finish, count = 0;
    const dialog = async () => { count++; return new Promise(resolve => { finish = resolve; }); };
    s.confirmDialog = dialog; s.promptDialog = dialog;
    const pending = s.transition(action);
    await s.transition(action);
    assert.equal(count, 1);
    s.enterprise.value = { id: "outsider" };
    finish(action === "reject_delivery" ? "fix" : true);
    await pending;
    assert.equal(s.calls.length, 0);
    assert.equal(s.orderActionSubmitting.value, false);
  }
});

test("attachment lists resolve order tasks and ignore responses after switching orders", async () => {
  const s = harness();
  s.selectedOrder.value.order = offline;
  s.api.get = async url => {
    s.calls.push(["get", url]);
    return { data: url === "/delivery-tasks" ? { items: [{ id: "task", order_id: offline.id }, { id: "other", order_id: "other-order" }] } : { items: [{ id: "attachment", name: "result.txt" }] } };
  };
  await s.loadOrderDeliveryAttachments(offline);
  assert.deepEqual(s.calls.map(([, url]) => url), ["/delivery-tasks", "/delivery-tasks/task/attachments"]);
  assert.equal(s.orderDeliveryAttachments.value.items[0].name, "result.txt");
  let finish;
  s.selectedOrder.value.delivery = { id: "task" };
  s.api.get = async () => new Promise(resolve => { finish = resolve; });
  const pending = s.loadOrderDeliveryAttachments(offline);
  s.selectedOrder.value = { order: { ...offline, id: "other-order" } };
  s.orderDeliveryAttachments.value = { orderId: "other-order", items: [] };
  finish({ data: { items: [{ id: "stale" }] } }); await pending;
  assert.equal(s.orderDeliveryAttachments.value.items.length, 0);
});

test("attachment upload uses multipart delivery task endpoint and provider-only editable states", async () => {
  const s = harness();
  s.selectedOrder.value.order = offline;
  s.orderDeliveryAttachments.value = { orderId: offline.id, tasks: [{ id: "task", status: "in_delivery" }], items: [] };
  s.deliveryAttachmentDescription.value = " evidence ";
  s.loadOrderDeliveryAttachments = async item => s.refreshes.push(item.id);
  const event = () => ({ target: { files: [new Blob(["proof"])], value: "chosen" } });
  const input = event();
  await s.uploadOrderDeliveryAttachment(input);
  assert.equal(input.target.value, "");
  assert.equal(s.calls[0][1], "/delivery-tasks/task/attachments");
  assert.equal(s.calls[0][2].get("description"), "evidence");
  assert.equal(await s.calls[0][2].get("upload").text(), "proof");
  assert.equal(s.deliveryAttachmentUploading.value, false);
  s.enterprise.value = { id: "buyer" };
  await s.uploadOrderDeliveryAttachment(event());
  assert.equal(s.calls.length, 1);
});

test("confirmed offline lifecycle actions post once and conflicts refresh current state", async () => {
  for (const [action, delivery_status, party] of [
    ["start_delivery", "awaiting_start", "provider"],
    ["submit_delivery", "rectifying", "provider"],
    ["accept_delivery", "pending_acceptance", "buyer"],
  ]) {
    const s = harness();
    s.selectedOrder.value.order = { ...offline, main_status: delivery_status, delivery_status };
    s.enterprise.value = { id: party };
    await s.transition(action);
    assert.equal(s.calls.length, 1);
    assert.equal(s.calls[0][2].action, action);
    assert.deepEqual(s.refreshes, ["order-1", "list"]);
    s.api.post = async () => { throw { response: { status: 409, data: { detail: "state changed" } } }; };
    await s.transition(action);
    assert.deepEqual(s.refreshes, ["order-1", "list", "order-1", "list"]);
    assert.equal(s.orderActionSubmitting.value, false);
  }
});

test("attachment failures are visible and upload locks release after failures", async () => {
  const s = harness();
  s.selectedOrder.value.order = offline;
  s.api.get = async () => { throw { response: { data: { detail: "unavailable" } } }; };
  await s.loadOrderDeliveryAttachments(offline);
  assert.equal(s.orderDeliveryAttachments.value.loading, false);
  assert.equal(s.orderDeliveryAttachments.value.error, "unavailable");
  s.orderDeliveryAttachments.value = { orderId: offline.id, tasks: [{ id: "task", status: "in_delivery" }] };
  s.api.post = async () => { throw { response: { data: { detail: "scan rejected" } } }; };
  await s.uploadOrderDeliveryAttachment({ target: { files: [new Blob(["proof"])], value: "chosen" } });
  assert.equal(s.deliveryAttachmentUploading.value, false);
  assert.equal(s.messages.at(-1), "scan rejected");
});
