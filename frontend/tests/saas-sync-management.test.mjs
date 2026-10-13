import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { flattenSyncTree, selectedSyncIds, syncActionPayload } from "../src/saasSyncManagement.js";

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
  assert.deepEqual(syncActionPayload("remove", "users"), { action: "remove", kind: "users", all: true });
});

const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
function component(api) {
  const source = readFileSync(new URL("../src/SaaSSyncManagement.vue", import.meta.url), "utf8")
    .split("<script setup>")[1].split("</script>")[0].replace(/^import .*;$/gm, "");
  const props = { api, subscriptionId: "one", initialKind: "users" };
  const context = vm.createContext({ defineProps: () => props, defineEmits: () => () => {},
    ref: value => ({ value }), computed: getter => ({ get value() { return getter(); } }),
    watch() {}, onMounted() {}, onBeforeUnmount() {}, flattenSyncTree, selectedSyncIds, syncActionPayload });
  vm.runInContext(source + "\nglobalThis.state = { load, move, tree, kind, error, notice, loading, submitting, requestRemoveAll, removeAllPending };", context);
  return { props, ...context.state };
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
  const moving = other.move("add");
  other.props.subscriptionId = "two";
  action.resolve({ data: { succeeded: 1, failed: 0, skipped: 0 } });
  await moving;
  assert.equal(other.notice.value, "");
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
