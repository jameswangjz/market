import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { computed, markRaw, nextTick, reactive, ref, watch } from "vue";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { listState, applyListFilters, changeListPage, fetchListPage } from "../src/mainList.js";
const source = readFileSync(new URL("../src/GatewayRouteList.vue", import.meta.url), "utf8");
function harness(api, prompt = async () => "说明") {
  const document = { activeElement: null, body: { style: { overflow: "auto" } } };
  const cleanup = [];
  const context = vm.createContext({ computed, nextTick, reactive, ref, watch, document, onMounted() {}, onBeforeUnmount(fn) { cleanup.push(fn); },
    defineProps: () => ({ api, dialogs: { prompt } }), listState, applyListFilters, changeListPage, fetchListPage,
    newRenewalRequestKey: () => "stable-request" });
  const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*;$/gm, "");
  vm.runInContext(script + "\nthis.state = { lists, rows, selected, details, stats, tab, search, loadRoutes, select, showTab, page, act, audit, auditAlert, actionError, panel, closeDetail, detailKeydown };", context);
  context.state.document = document;
  context.state.unmount = () => cleanup.forEach(fn => fn());
  return context.state;
}
test("gateway component compiles and has no route configuration mutation entries", () => {
  const { descriptor } = parse(source);
  const script = compileScript(descriptor, { id: "gateway-list" });
  assert.equal(compileTemplate({ source: descriptor.template.content, filename: "GatewayRouteList.vue", id: "gateway-list", compilerOptions: { bindingMetadata: script.bindings } }).errors.length, 0);
  assert.doesNotMatch(source, /gateway-config|\.put\(|publishGateway|rollbackGateway/);
});
test("modal uses standard tabs, accessible title, explicit close and no outside dismissal or body writes", () => {
  assert.match(source, /class="modal-scrim route-detail-scrim"/);
  assert.match(source, /role="dialog" aria-modal="true" aria-labelledby="gateway-route-detail-title"/);
  assert.match(source, /aria-label="关闭路由详情"/);
  assert.match(source, /<ConsoleTabs /);
  assert.doesNotMatch(source, /@click\.self|body\.style\.overflow\s*=/);
  assert.match(source, /z-index:90/);
});
test("modal focuses, traps Tab, restores focus and leaves nested dialog Escape and overflow alone", async () => {
  let restored = 0, focused = 0, prevented = 0;
  const state = harness({ get: async () => ({ data: { items: [], total: 0 } }) });
  const trigger = { isConnected: true, focus() { restored++; } };
  const first = { getClientRects: () => [1], focus() { focused++; } };
  const panel = markRaw({ focus() { state.document.activeElement = panel; }, contains: element => element === panel || element === first, querySelectorAll: () => [first] });
  state.document.activeElement = trigger; state.panel.value = panel;
  await state.select({ id: "r1" });
  assert.equal(state.document.activeElement, panel);
  state.detailKeydown({ key: "Tab", target: panel, preventDefault() { prevented++; } });
  assert.equal(focused, 1); assert.equal(prevented, 1);
  state.detailKeydown({ key: "Escape", target: {}, preventDefault() { throw new Error("nested Escape intercepted"); } });
  assert.equal(state.selected.value.id, "r1");
  state.detailKeydown({ key: "Escape", target: panel, preventDefault() {}, stopPropagation() {} });
  assert.equal(state.selected.value, null); assert.equal(restored, 1);
  state.document.body.style.overflow = "hidden";
  state.unmount(); assert.equal(state.document.body.style.overflow, "hidden");
});
test("closing and reopening the same route ignores stale detail and prompt results", async () => {
  let resolveDetail, resolvePrompt, writes = 0;
  const state = harness({ get: () => new Promise(done => { resolveDetail = done; }), post: async () => { writes++; } }, () => new Promise(done => { resolvePrompt = done; }));
  const old = state.select({ id: "r1" });
  await nextTick();
  const finishOld = resolveDetail;
  const action = state.act({ id: "alert", version: 1 }, "close");
  state.closeDetail();
  const fresh = state.select({ id: "r1" });
  await nextTick();
  finishOld({ data: { items: [{ id: "stale" }], total: 1 } });
  resolvePrompt("reason"); await old; await action;
  assert.equal(state.details.stats.length, 0); assert.equal(writes, 0);
  resolveDetail({ data: { items: [{ id: "fresh" }], total: 1 } }); await fresh;
  assert.equal(state.details.stats[0].id, "fresh");
});
test("late action failures after close do not leak into the reopened modal", async () => {
  let reject;
  const state = harness({ get: async () => ({ data: { items: [], total: 0 } }), post: () => new Promise((_, fail) => { reject = fail; }) });
  await state.select({ id: "r1" });
  const action = state.act({ id: "a", version: 1 }, "close");
  await new Promise(setImmediate); state.closeDetail(); await state.select({ id: "r1" });
  reject(new Error("late")); await action;
  assert.equal(state.actionError.value, "");
});
test("audit paging stays independent and close discards pending audit results", async () => {
  let finish;
  const calls = [];
  const state = harness({ get: async (path, config) => {
    calls.push([path, config.params.page]);
    if (path.endsWith("/audit") && config.params.page === 2) return new Promise(done => { finish = done; });
    return { data: { items: [{ id: "entry" }], total: 60 } };
  } });
  await state.select({ id: "r1" }); await state.showTab("alerts");
  await state.page("alerts", 2); await state.audit({ id: "a1" });
  const pending = state.page("audit", 2);
  assert.equal(state.lists.alerts.page, 2); assert.equal(state.lists.stats.page, 1);
  assert.equal(calls.at(-1)[0], "/gateway/routes/r1/alerts/a1/audit");
  state.closeDetail(); finish({ data: { items: [{ id: "stale" }], total: 60 } }); await pending;
  assert.equal(state.auditAlert.value, null); assert.equal(state.details.audit[0].id, "entry");
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
