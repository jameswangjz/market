import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { canReviewProviderOrder, orderWorkflowActions, providerReviewPayload, isOfflineFulfillmentOrder, offlineDeliveryActions, canUploadDeliveryAttachment, deliveryTransitionPayload } from "../src/orderWorkflow.js";

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

test("only current buyer super admin can start or simulate confirm payment", () => {
  const pending = { ...order, main_status: "pending_payment" };
  const buyer = { ...admin, enterprise_role: "super_admin" };
  assert.deepEqual(orderWorkflowActions(pending, buyer, { id: "buyer" }), [["start_payment", "发起模拟支付"]]);
  assert.deepEqual(orderWorkflowActions({ ...pending, payment_status: "paying" }, buyer, { id: "buyer" }), [["confirm_payment", "模拟确认支付"]]);
  for (const user of [admin, { ...buyer, membership_status: "disabled" }, { ...buyer, enterprise_memberships: [] }]) {
    assert.deepEqual(orderWorkflowActions(pending, user, { id: "buyer" }), []);
  }
  assert.deepEqual(orderWorkflowActions(pending, buyer, enterprise), []);
  for (const main_status of ["pending_provider_review", "rejected", "cancelled", "closed", "completed", "created"]) {
    assert.deepEqual(orderWorkflowActions({ ...pending, main_status }, buyer, { id: "buyer" }), []);
  }
});

test("platform finance and super admin have manual confirmation only", () => {
  for (const platform_role of ["super_admin", "finance_settlement"]) {
    for (const payment_status of ["unpaid", "paying"]) {
      assert.deepEqual(orderWorkflowActions({ ...order, main_status: "pending_payment", payment_status }, { platform_role }, null), [["confirm_payment", "人工确认支付"]]);
    }
    assert.deepEqual(orderWorkflowActions(order, { platform_role }, null), []);
    assert.deepEqual(orderWorkflowActions({ ...order, main_status: "pending_payment", payment_status: "paid" }, { platform_role }, null), []);
  }
  for (const platform_role of ["platform_operator", "delivery_monitor", "security_compliance"]) {
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

const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
function harness() {
  const state = {
    selectedOrder: { value: { order: { ...order } } }, user: { value: { ...admin } }, enterprise: { value: enterprise },
    providerReview: { value: null }, providerReviewForm: { value: { amount: "", cost: "", reason: "" } },
    providerReviewLoading: { value: false }, providerReviewSubmitting: { value: false }, orderActionSubmitting: { value: false },
    canReviewProviderOrder, orderWorkflowActions, providerReviewPayload, isOfflineFulfillmentOrder, canUploadDeliveryAttachment, deliveryTransitionPayload,
    deliveryAttachmentUploading: { value: false }, deliveryAttachmentDescription: { value: "" },
    orderDeliveryAttachments: { value: { orderId: "", tasks: [], items: [], loading: false, error: "" } }, FormData,
    calls: [], messages: [], refreshes: [], api: {}, confirmDialog: async () => true, promptDialog: async () => "reason",
    fmtMoney: value => String(value),
  };
  state.api.get = async url => { state.calls.push(["get", url]); return { data: quote }; };
  state.api.post = async (url, body) => { state.calls.push(["post", url, body]); };
  state.notify = message => state.messages.push(message);
  state.openOrder = async item => state.refreshes.push(item.id);
  state.refreshData = async () => state.refreshes.push("list");
  vm.createContext(state);
  vm.runInContext(source.slice(source.indexOf("async function loadOrderDeliveryAttachments(order)"), source.indexOf("async function saasOrderAction(action)")) + source.slice(source.indexOf("function nextActions(order)"), source.indexOf("onMounted(() =>")), state);
  return state;
}

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
