import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { flattenSyncTree, selectedSyncIds, syncActionPayload, syncActionBatches } from "../src/saasSyncManagement.js";
import { subscriptionDisplayStatus } from "../src/subscriptionTerms.js";

const tree = {
  departments: [{ id: "parent", name: "Parent", users: [{ id: "u", name: "User" }], children: [
    { id: "child", name: "Child", users: [{ id: "u", name: "User" }], children: [] },
  ] }],
  mapped_departments: [
    { id: "mc", platform_department_id: "child", external_department_id: "rc", parent_mapping_id: "mp" },
    { id: "mp", platform_department_id: "parent", external_department_id: "rp", parent_mapping_id: "" },
  ],
  mapped_users: [{ id: "mu", department_ids: ["rp", "rc"] }, { id: "leftover", department_ids: [] }],
};

test("user trees retain nonselectable department groups and unique department/user keys", () => {
  for (const mapped of [false, true]) {
    const rows = flattenSyncTree(tree, "users", mapped);
    assert.ok(rows.some(row => row.type === "department"));
    assert.ok(rows.filter(row => row.type === "department").every(row => row.selectable === false));
    assert.equal(new Set(rows.map(row => row.key)).size, rows.length);
    const ids = selectedSyncIds(rows, "users", rows.map(row => row.id));
    assert.deepEqual(ids, mapped ? ["mu", "leftover"] : ["u"]);
    assert.deepEqual(syncActionPayload("remove", "users", ids).ids, ids);
  }
  const rows = flattenSyncTree(tree, "departments", true);
  assert.deepEqual(rows.map(row => row.id), ["mp", "mc"]);
  assert.deepEqual(selectedSyncIds(rows, "departments", ["mu", "mc", "mp", "mc"]), ["mc", "mp"]);
});

test("an empty selected action never turns into an all action", () => {
  assert.throws(() => syncActionPayload("remove", "users", []), /请选择/);
  assert.throws(() => syncActionPayload("remove", "users"), /请选择/);
});

const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
function component(api) {
  const source = readFileSync(new URL("../src/SaaSSyncManagement.vue", import.meta.url), "utf8")
    .split("<script setup>")[1].split("</script>")[0].replace(/^import .*;$/gm, "");
  const props = { api, subscriptionId: "one", initialKind: "users" };
  let unmount;
  const context = vm.createContext({ defineProps: () => props, defineEmits: () => () => {},
    ref: value => ({ value }), computed: getter => ({ get value() { return getter(); } }),
    watch() {}, onMounted() {}, onBeforeUnmount(callback) { unmount = callback; }, AbortController,
    flattenSyncTree, selectedSyncIds, syncActionPayload, syncActionBatches });
  vm.runInContext(source + "\nglobalThis.state = { load, move, tree, kind, error, notice, loading, submitting, requestRemoveAll, removeAllPending, removalIds, close, reloadScope };", context);
  return { props, unmount: () => unmount(), ...context.state };
}

test("late tree and operation responses cannot update a switched subscription", async () => {
  const late = deferred();
  const state = component({ get: () => late.promise });
  const task = state.load();
  state.props.subscriptionId = "two";
  late.resolve({ data: tree });
  await task;
  assert.equal(state.tree.value.departments.length, 0);
  const action = deferred();
  const other = component({ post: () => action.promise, get: async () => ({ data: tree }) });
  other.tree.value = tree;
  const moving = other.move("add");
  other.props.subscriptionId = "two";
  action.resolve({ data: { succeeded: 1, failed: 0, skipped: 0 } });
  await moving;
  assert.equal(other.notice.value, "");
});

function usersTree(count = 23) {
  return { departments: [], unassigned_users: Array.from({ length: count }, (_, index) => ({ id: `u${index}`, name: `User ${index}` })),
    mapped_users: Array.from({ length: count }, (_, index) => ({ id: `m${index}`, platform_user_id: `u${index}` })), mapped_departments: [] };
}
const success = ids => ({ data: { items: ids.map(id => ({ id, status: "succeeded" })) } });

test("selected and all user actions use sequential explicit batches, deduplicated and capped at ten", async () => {
  for (const action of ["add", "remove"]) for (const selected of [false, true]) {
    const fixture = usersTree();
    const calls = [];
    const first = deferred();
    let refreshes = 0;
    const state = component({
      post: async (url, body) => { calls.push({ url, body }); return calls.length === 1 ? first.promise : success(body.ids); },
      get: async () => { refreshes++; return { data: fixture }; },
    });
    state.tree.value = fixture;
    const ids = Array.from({ length: 23 }, (_, index) => `${action === 'add' ? 'u' : 'm'}${index}`);
    const moving = state.move(action, selected ? [...ids, ids[0]] : null);
    assert.equal(calls.length, 1);
    assert.equal(state.submitting.value, true);
    first.resolve(success(calls[0].body.ids));
    await moving;
    assert.deepEqual(calls.map(call => call.body.ids.length), [10, 10, 3]);
    assert.deepEqual(calls.flatMap(call => call.body.ids), ids);
    assert.ok(calls.every(call => !('all' in call.body) && call.body.kind === "users"));
    assert.equal(refreshes, 1);
    assert.match(state.notice.value, /成功 23 项/);
    assert.equal(state.submitting.value, false);
  }
});

function departmentsTree(count = 23) {
  let child = null;
  for (let index = count - 1; index >= 0; index--) child = { id: `d${index}`, name: `Department ${index}`, users: [], children: child ? [child] : [] };
  return { departments: [child], mapped_users: [], mapped_departments: Array.from({ length: count }, (_, index) => ({
    id: `m${index}`, platform_department_id: `d${index}`, parent_mapping_id: index ? `m${index - 1}` : "",
  })).reverse() };
}

test("department order is global across batches for both selected and all actions", async () => {
  for (const action of ["add", "remove"]) for (const selected of [false, true]) {
    const fixture = departmentsTree();
    const calls = [];
    const processed = new Set();
    const state = component({
      post: async (url, body) => {
        calls.push(body);
        for (const id of body.ids) {
          const index = Number(id.slice(1));
          if (action === "add" && index > 0) assert.ok(processed.has(`d${index - 1}`), "parent must exist before child, including across batches");
          if (action === "remove" && index < 22) assert.ok(processed.has(`m${index + 1}`), "child must be removed before parent, including across batches");
          processed.add(id);
        }
        return success(body.ids);
      }, get: async () => ({ data: fixture }),
    });
    state.kind.value = "departments";
    state.tree.value = fixture;
    const ids = Array.from({ length: 23 }, (_, index) => `${action === "add" ? "d" : "m"}${index}`);
    await state.move(action, selected ? [...ids].reverse() : null);
    assert.equal(calls.length, 3);
    assert.ok(calls.every(body => body.ids.length <= 10 && body.kind === "departments"));
    assert.deepEqual(calls.flatMap(body => body.ids), action === "add" ? ids : ids.reverse());
    assert.match(state.notice.value, /成功 23 项/);
  }
});

test("partial failures accumulate across batches and a transport failure aborts the queue then refreshes", async () => {
  const fixture = usersTree(35);
  let posts = 0, refreshes = 0;
  const state = component({
    post: async (url, body) => {
      posts++;
      if (posts === 3) throw new Error("network stopped");
      return { data: { items: body.ids.map((id, index) => ({ id, status: index === 0 ? "failed" : "succeeded", detail: "protected" })) } };
    },
    get: async () => { refreshes++; throw new Error("refresh stopped"); },
  });
  state.tree.value = fixture;
  await state.move("remove");
  assert.equal(posts, 3);
  assert.equal(refreshes, 1);
  assert.match(state.error.value, /m0: protected/);
  assert.match(state.error.value, /m10: protected/);
  assert.match(state.error.value, /network stopped/);
  assert.match(state.error.value, /结果未确认：m20/);
  assert.match(state.error.value, /未尝试：m30/);
  assert.match(state.error.value, /同步树加载失败/);
  assert.match(state.notice.value, /成功 18 项，失败 2 项.*未确认 10 项，未尝试 5 项/);
  assert.equal(state.submitting.value, false);
});

test("whole removal confirmation snapshots users and never schedules implicit department removal", async () => {
  const fixture = usersTree();
  fixture.mapped_departments = tree.mapped_departments;
  const calls = [];
  const state = component({ post: async (url, body) => { calls.push(body); return success(body.ids); }, get: async () => ({ data: fixture }) });
  state.tree.value = fixture;
  state.requestRemoveAll();
  assert.equal(calls.length, 0);
  assert.equal(state.removeAllPending.value, true);
  assert.equal(state.removalIds.value.length, 23);
  fixture.mapped_users.push({ id: "new-after-confirm" });
  await state.move("remove", state.removalIds.value);
  assert.equal(calls.flatMap(body => body.ids).length, 23);
  assert.ok(calls.every(body => body.kind === "users"));
  assert.ok(!calls.flatMap(body => body.ids).includes("new-after-confirm"));
});

test("context switch, close, and unmount abort pending work without stale output or queued requests", async () => {
  for (const event of ["switch", "close", "unmount", "kind"]) {
    const fixture = usersTree();
    const pending = deferred();
    let posts = 0, refreshes = 0, signal;
    const state = component({
      post: (url, body, options) => { posts++; signal = options.signal; return pending.promise; },
      get: async () => { refreshes++; return { data: fixture }; },
    });
    state.tree.value = fixture;
    const moving = state.move("add");
    if (event === "switch") { state.props.subscriptionId = "two"; state.reloadScope(); }
    else if (event === "kind") { state.kind.value = "departments"; state.reloadScope(); }
    else state[event]();
    const expectedRefreshes = refreshes;
    assert.equal(signal.aborted, true);
    state.notice.value = "new context";
    state.error.value = "new error";
    pending.resolve(success(Array.from({ length: 10 }, (_, index) => `u${index}`)));
    await moving;
    assert.equal(posts, 1);
    assert.equal(refreshes, expectedRefreshes);
    assert.equal(state.notice.value, "new context");
    assert.equal(state.error.value, "new error");
  }
});

test("old request cannot release a new submission after switching away and back", async () => {
  const first = deferred(), second = deferred();
  let posts = 0;
  const fixture = usersTree(1);
  const state = component({ post: () => ++posts === 1 ? first.promise : second.promise, get: async () => ({ data: fixture }) });
  state.tree.value = fixture;
  const old = state.move("add");
  state.props.subscriptionId = "two";
  state.reloadScope();
  state.props.subscriptionId = "one";
  state.reloadScope();
  await state.load();
  const current = state.move("add");
  assert.equal(posts, 2);
  first.resolve(success(["u0"]));
  await old;
  assert.equal(state.submitting.value, true);
  assert.equal(state.notice.value, "");
  second.resolve(success(["u0"]));
  await current;
  assert.equal(state.submitting.value, false);
  assert.match(state.notice.value, /成功 1 项/);
});

test("item failures survive refresh failures and invalid selection sends no request", async () => {
  let posts = 0;
  const state = component({
    post: async () => { posts++; return { data: { succeeded: 0, failed: 1, skipped: 0, items: [{ id: "u", status: "failed", detail: "blocked" }] } }; },
    get: async () => { throw new Error("refresh failed"); },
  });
  state.tree.value = tree;
  await state.move("remove", ["mp"]);
  assert.equal(posts, 0);
  await state.move("add", ["u"]);
  assert.equal(posts, 1);
  assert.match(state.error.value, /u: blocked/);
  assert.match(state.error.value, /同步树加载失败/);
  state.requestRemoveAll(["mp"]);
  assert.equal(state.removeAllPending.value, false);
  state.requestRemoveAll(["mu"]);
  assert.equal(state.removeAllPending.value, true);
});

const consoleSource = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
const syncOrder = { id: "order-sync", subscription_id: "saas-id", buyer_enterprise_id: "buyer", product_id: "product", payment_status: "unpaid" };
function consoleSync(api, changes = {}) {
  const state = vm.createContext({
    computed: getter => ({ get value() { return getter(); } }), subscriptionDisplayStatus,
    orderDetailRequest: 0, selectedOrder: { value: null },
    user: { value: { id: "admin", is_active: true, activation_status: "active", verified_status: "verified", enterprise_role: "enterprise_admin", ...changes } },
    enterprise: { value: { id: "buyer" } }, token: { value: "token" },
    subscriptionState: { value: { orderId: syncOrder.id, loading: false, error: "", item: {
      id: "combination-id", enterprise_id: "buyer", product_id: "product", status: "active", expires_at: "2099-01-01T00:00:00Z",
    } } },
    subscriptionSession: { reset() {}, load: async () => {} }, api,
    deliveryAttachmentDescription: { value: "" }, saasOrderState: { value: {} }, saasSyncDialog: { value: null },
    apiOrderState: { value: {} }, apiCredentialReveal: { value: null }, orderProductFiles: { value: {} },
    orderDeliveryAttachments: { value: {} }, isOfflineFulfillmentOrder: () => false,
  });
  vm.runInContext(consoleSource.slice(consoleSource.indexOf("function saasSyncBuyerAllowed(order)"), consoleSource.indexOf("const apiOrderState =")) +
    consoleSource.slice(consoleSource.indexOf("async function openOrder(order)"), consoleSource.indexOf("async function downloadOrderProductFile(file)")) +
    consoleSource.slice(consoleSource.indexOf("function closeOrder()"), consoleSource.indexOf("const orderProductFiles =")) +
    "\nglobalThis.allowed = saasSyncAllowed; globalThis.unavailable = saasSyncUnavailableReason;", state);
  return state;
}

test("distinct combination and SaaS IDs show launchers only after the actual SaaS readiness read", async () => {
  const ready = deferred(), operations = deferred(), calls = [];
  const state = consoleSync({ get: async url => {
    calls.push(url);
    if (url.endsWith("/sync-tree")) return ready.promise;
    if (url.endsWith("/operations")) return operations.promise;
    return { data: { order: syncOrder } };
  } });
  const opening = state.openOrder(syncOrder);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(state.allowed.value, false);
  assert.ok(calls.includes("/saas-subscriptions/saas-id/sync-tree"));
  assert.ok(!calls.some(url => url.includes("combination-id")));
  ready.resolve({ data: { departments: [], mapped_users: [], mapped_departments: [] } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(state.allowed.value, true);
  operations.resolve({ data: { items: [] } });
  await opening;
  assert.equal(state.allowed.value, true, "operations response must preserve the independent readiness result");
  assert.equal(state.saasOrderState.value.subscriptionId, "saas-id");
  assert.match(consoleSource, /v-if="saasSyncAllowed" class="saas-sync-launchers"/);
  assert.match(consoleSource, /saasSyncDialog && saasSyncAllowed && selectedOrder\?\.order\?\.id === saasSyncDialog.orderId && selectedOrder.order.subscription_id === saasSyncDialog.subscriptionId/);
  assert.doesNotMatch(consoleSource, /subscriptionState\.item\?\.id === (selectedOrder\.order\.subscription_id|saasSyncDialog\.subscriptionId)/);
  for (const change of [{ status: "closed" }, { expires_at: "2000-01-01T00:00:00Z" }, { enterprise_id: "other" }, { product_id: "other" }]) {
    const original = state.subscriptionState.value.item;
    state.subscriptionState.value.item = { ...original, ...change };
    assert.equal(state.allowed.value, false);
    state.subscriptionState.value.item = original;
  }
  for (const field of ["loading", "error"]) {
    state.subscriptionState.value[field] = field === "loading" ? true : "failed";
    assert.equal(state.allowed.value, false);
    state.subscriptionState.value[field] = field === "loading" ? false : "";
  }
});

test("provider, platform, ordinary and inactive accounts do not attempt readiness reads", async () => {
  for (const change of [{ platform_role: "super_admin" }, { enterprise_role: "member" },
    { is_active: false }, { activation_status: "pending_activation" }, { verified_status: "pending" }, { provider: true }]) {
    const calls = [];
    const state = consoleSync({ get: async url => { calls.push(url); return url.endsWith("/operations") ? { data: { items: [] } } : { data: { order: syncOrder } }; } }, change);
    if (change.provider) state.enterprise.value = { id: "provider" };
    await state.openOrder(syncOrder);
    assert.ok(!calls.some(url => url.endsWith("/sync-tree")));
    assert.equal(state.allowed.value, false);
  }
});

test("closed SaaS readiness displays its reason; stale successes and failures cannot rebind another drawer", async () => {
  for (const lateFailure of [false, true]) {
    const late = deferred();
    const state = consoleSync({ get: async url => {
      if (url.endsWith("/sync-tree")) return late.promise;
      return url.endsWith("/operations") ? { data: { items: [] } } : { data: { order: syncOrder } };
    } });
    const opening = state.openOrder(syncOrder);
    await new Promise(resolve => setImmediate(resolve));
    state.closeOrder();
    state.saasOrderState.value = { marker: "new context" };
    if (lateFailure) late.reject({ response: { data: { detail: "closed" } } });
    else late.resolve({ data: {} });
    await opening;
    assert.equal(state.saasOrderState.value.marker, "new context");
    assert.equal(state.allowed.value, false);
    assert.equal(state.unavailable.value, "");
  }
  const state = consoleSync({ get: async url => {
    if (url.endsWith("/sync-tree")) throw { response: { status: 409, data: { detail: "SaaS 订阅当前无效或未生效" } } };
    return url.endsWith("/operations") ? { data: { items: [] } } : { data: { order: syncOrder } };
  } });
  await state.openOrder(syncOrder);
  assert.equal(state.allowed.value, false);
  assert.match(state.unavailable.value, /订阅当前无效/);
  state.token.value = "different-token";
  assert.equal(state.unavailable.value, "");
});
