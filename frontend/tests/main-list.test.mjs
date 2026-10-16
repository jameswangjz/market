import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { ref, reactive, watch } from "vue";
import { listState, listParams, applyListFilters, changeListPage, fetchListPage, partyName, methodLabel } from "../src/mainList.js";

test("independent filters are applied explicitly and reset only their own page", () => {
  const tasks = listState({ status: "" }), tickets = listState({ status: "" });
  tasks.page = 4; tasks.filters.status = "retrying";
  assert.equal(listParams(tasks).status, undefined);
  applyListFilters(tasks);
  assert.equal(tasks.page, 1); assert.equal(listParams(tasks).status, "retrying");
  assert.equal(tickets.applied.status, "");
  tasks.page_size = 50; applyListFilters(tasks, true);
  assert.equal(tasks.page_size, 50); assert.equal(tasks.applied.status, "");
});
test("query aliases trim text; page changes reject invalid input and sizes reset page", () => {
  const state = listState({ name: "  产品  " }); state.aliases = { name: "product_name" };
  assert.deepEqual(listParams(state), { product_name: "产品", page: 1, page_size: 20 });
  state.total = 43;
  for (const value of [0, -1, "1.5", 4, "abc"]) assert.equal(changeListPage(state, value), false);
  assert.equal(changeListPage(state, 3), true);
  changeListPage(state, 1, 50); assert.equal(state.page, 1); assert.equal(state.page_size, 50);
});
test("out of range page refetches the last valid server page", async () => {
  const state = listState(); state.page = 5;
  const requests = []; let rows;
  const api = { get: async (_url, config) => { requests.push(config.params); return { data: { items: [config.params.page], total: 23 } }; } };
  await fetchListPage(api, "/orders", state, { enterprise_id: "e" }, () => true, items => { rows = items; });
  assert.equal(state.page, 2); assert.deepEqual(rows, [2]);
  assert.deepEqual(requests.map(item => item.page), [5, 2]); assert.equal(requests[1].enterprise_id, "e");
});
test("late requests and obsolete authorization contexts cannot replace list data", async () => {
  const state = listState(); const pending = []; let rows = [];
  const api = { get: () => new Promise(resolve => pending.push(resolve)) };
  const assign = items => { rows = items; };
  const first = fetchListPage(api, "/orders", state, {}, () => true, assign);
  const second = fetchListPage(api, "/orders", state, {}, () => true, assign);
  pending[1]({ data: { items: ["new"], total: 1 } }); await second;
  pending[0]({ data: { items: ["old"], total: 1 } }); await first;
  assert.deepEqual(rows, ["new"]);
  const obsolete = fetchListPage(api, "/orders", state, {}, () => false, assign);
  pending[2]({ data: { items: ["other enterprise"], total: 1 } }); await obsolete;
  assert.deepEqual(rows, ["new"]);
});
test("legacy unpaginated responses surface an integration error, not a fake total", async () => {
  const state = listState(); let rows;
  await fetchListPage({ get: async () => ({ data: { items: [1, 2] } }) }, "/orders", state, {}, () => true, items => { rows = items; });
  assert.match(state.error, /分页总数/); assert.deepEqual(rows, []); assert.equal(state.busy, false);
});
test("party and method fallback never reuse buyer identity as provider", () => {
  assert.equal(partyName({ buyer_name: "购买方" }, "provider"), "未记录");
  assert.equal(partyName({ buyer_display_name: "快照" }, "buyer"), "快照");
  assert.equal(methodLabel("tenant_access"), "租户应用访问"); assert.match(methodLabel("new_method"), /未知方式/);
});
test("ConsoleApp synchronously invalidates pending pages and clears rows on enterprise switch", async () => {
  const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
  const start = source.indexOf("watch(() => [user.value?.id, enterprise.value?.id, token.value, businessView.value]");
  const end = source.indexOf("watch(() => `${user.value?.id}:", start);
  const state = { watch, user: ref({ id: "admin" }), enterprise: ref({ id: "old" }), token: ref("token"), businessView: ref("provider"),
    mainLists: reactive({ products: listState() }), products: ref([{ id: "old" }]), orders: ref([]), deliveryItems: ref([]), afterSalesItems: ref([]), deliveryTab: ref("aftersales"), refundsVisited: ref(true) };
  vm.runInContext(source.slice(start, end), vm.createContext(state));
  let resolve;
  const pending = fetchListPage({ get: () => new Promise(done => { resolve = done; }) }, "/products", state.mainLists.products, {}, () => true, items => { state.products.value = items; });
  state.enterprise.value = { id: "new" };
  assert.equal(state.products.value.length, 0); assert.equal(state.mainLists.products.busy, false);
  assert.equal(state.deliveryTab.value, "tasks");
  resolve({ data: { items: [{ id: "old-response" }], total: 1 } }); await pending;
  assert.equal(state.products.value.length, 0); assert.equal(state.mainLists.products.total, 0);
});
