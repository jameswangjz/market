import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { computed, reactive, ref } from "vue";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { listState, applyListFilters, changeListPage, fetchListPage } from "../src/mainList.js";
const source = readFileSync(new URL("../src/GatewayRouteList.vue", import.meta.url), "utf8");
function harness(api, prompt = async () => "说明") {
  const context = vm.createContext({ computed, reactive, ref, onMounted() {}, onBeforeUnmount() {},
    defineProps: () => ({ api, dialogs: { prompt } }), listState, applyListFilters, changeListPage, fetchListPage,
    newRenewalRequestKey: () => "stable-request" });
  const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*;$/gm, "");
  vm.runInContext(script + "\nthis.state = { lists, rows, selected, details, stats, tab, search, loadRoutes, select, showTab, page, act, actionError };", context);
  return context.state;
}
test("gateway component compiles and has no route configuration mutation entries", () => {
  const { descriptor } = parse(source);
  const script = compileScript(descriptor, { id: "gateway-list" });
  assert.equal(compileTemplate({ source: descriptor.template.content, filename: "GatewayRouteList.vue", id: "gateway-list", compilerOptions: { bindingMetadata: script.bindings } }).errors.length, 0);
  assert.doesNotMatch(source, /gateway-config|\.put\(|publishGateway|rollbackGateway/);
});
test("published default, explicit history, and independent route detail pages", async () => {
  const calls = [];
  const state = harness({ get: async (path, config) => { calls.push([path, config.params]); return { data: { items: [{ id: "r1" }], total: 40 } }; } });
  await state.loadRoutes(); assert.equal(calls[0][1].product_status, "published");
  state.lists.routes.filters.product_status = "all"; await state.search();
  assert.equal(calls[1][1].product_status, "all");
  await state.select({ id: "r1" }); assert.equal(calls.at(-1)[0], "/gateway/routes/r1/stats");
  await state.page("stats", 2); await state.showTab("alerts");
  assert.equal(state.lists.stats.page, 2); assert.equal(state.lists.alerts.page, 1);
  assert.equal(calls.at(-1)[0], "/gateway/routes/r1/alerts");
  await state.select({ id: "r2" }); assert.equal(calls.at(-1)[0], "/gateway/routes/r2/alerts");
  assert.equal(state.lists.stats.page, 1);
});
test("alert actions carry version and stable retry identity; blank close sends no POST", async () => {
  const calls = []; let fail = true;
  const api = { get: async () => ({ data: { items: [], total: 0 } }), post: async (path, body) => { calls.push([path, body]); if (fail) { fail = false; throw new Error("network"); } } };
  const state = harness(api); state.selected.value = { id: "route" };
  const item = { id: "alert", version: 3 };
  await state.act(item, "acknowledge"); await state.act(item, "acknowledge");
  assert.equal(calls[0][0], "/gateway/routes/route/alerts/alert/acknowledge");
  assert.equal(calls[0][1].request_id, calls[1][1].request_id);
  assert.equal(calls[0][1].expected_version, 3);
  const blank = harness(api, async () => " "); blank.selected.value = { id: "route" };
  await blank.act(item, "close"); assert.equal(calls.length, 2); assert.match(blank.actionError.value, /填写原因/);
});
test("route selection changed during action prompt cancels the pending write", async () => {
  let resolve, writes = 0;
  const state = harness({ post: async () => { writes++; } }, () => new Promise(done => { resolve = done; }));
  state.selected.value = { id: "old" }; const pending = state.act({ id: "alert", version: 0 }, "close");
  state.selected.value = { id: "new" }; resolve("原因"); await pending; assert.equal(writes, 0);
});
