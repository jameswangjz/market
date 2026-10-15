import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { computed, nextTick, ref, watch } from "vue";
import { pageCount, pageSizes, pageSizeValue, validPage } from "../src/pagination.js";
import { registrationProductTypes } from "../src/productRegistration.js";
import { businessViewParams } from "../src/consoleBusinessView.js";
import { canAccessConsoleView } from "../src/consolePermissions.js";
const source = name => readFileSync(new URL(`../src/${name}`, import.meta.url), "utf8");
const store = source("Storefront.vue"), messages = source("MessageCenter.vue"), consoleSource = source("ConsoleApp.vue"), ledger = source("LedgerComparison.vue");
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

test("page validation rejects invalid, fractional, unsafe and out-of-range jumps", () => {
  for (const value of ["", " ", "0", "-1", "1.5", "1e1", "Infinity", "NaN", "9007199254740992", "4", null]) assert.equal(validPage(value, 3), null, String(value));
  assert.equal(validPage("2", 3), 2);
  assert.equal(pageCount(0, 20), 1);
  assert.equal(pageCount(21, 20), 2);
  assert.deepEqual(pageSizes, [10, 20, 50, 100]);
  for (const size of pageSizes) assert.equal(pageSizeValue(String(size)), size);
  assert.equal(pageSizeValue("12", 20, true), 12);
  for (const value of [null, "", "-1", "101", "12"]) assert.equal(pageSizeValue(value), 20);
});

test("shared controls emit validated page/size events, synchronize jump, and block while busy", async () => {
  const text = source("PaginationControls.vue");
  const props = ref({ page: 1, pageSize: 20, total: 45, busy: false });
  const events = [];
  const context = vm.createContext({ computed, ref, watch, pageCount, pageSizes, validPage,
    defineProps: () => props.value, defineEmits: () => (...args) => events.push(args) });
  const script = text.split("<script setup>")[1].split("</script>")[0].replace(/^import .*;$/gm, "");
  vm.runInContext(script + "\nthis.jump = jump; this.error = error;", context);
  context.go("4"); assert.ok(context.error.value); assert.deepEqual(events, []);
  context.go("2"); context.resize({ target: { value: "100" } });
  assert.deepEqual(events, [["page", 2], ["size", 100]]);
  context.resize({ target: { value: "12" } }); assert.equal(events.length, 2);
  props.value.busy = true; context.go(3); context.resize({ target: { value: "50" } }); assert.equal(events.length, 2);
  props.value.page = 2; await nextTick(); assert.equal(context.jump.value, "2");
  assert.match(text, /type="button"/);
  assert.match(text, /min-height:68px/);
  assert.match(text, /本地分页/);
});

function storefront(search, get) {
  const paths = [];
  const context = { URLSearchParams, AbortController, registrationProductTypes, pageCount, pageSizeValue, props: { search, productId: "" }, api: { get }, navigate: path => paths.push(path), controller: undefined };
  for (const key of ["busy", "error", "notFound", "product", "selectedVersion", "selectedEnterprise", "subscriptionMonths", "items", "query", "type", "parentId", "categoryId", "page", "categories", "total", "pageSize"]) context[key] = ref(null);
  vm.createContext(context);
  vm.runInContext(store.slice(store.indexOf("function applyFilters("), store.indexOf("watch(() => [props.productId")), context);
  return { context, paths };
}
test("storefront sends all sizes including legacy 12, preserves size on filters and recovers an empty last page", async () => {
  for (const size of [...pageSizes, 12]) {
    const calls = [];
    const h = storefront(`?q=term&page=9&page_size=${size}`, async (path, config) => {
      calls.push({ path, config });
      return { data: path === "/categories" ? { items: [] } : { items: [], total: size + 1, page: 9, page_size: size } };
    });
    await h.context.load();
    assert.equal(calls[0].config.params.page_size, size);
    assert.equal(new URL(h.paths[0], "https://example.invalid").searchParams.get("page"), "2");
    h.context.applyFilters();
    const params = new URL(h.paths.at(-1), "https://example.invalid").searchParams;
    assert.equal(params.has("page"), false);
    assert.equal(params.get("page_size"), String(size));
    assert.equal(params.get("q"), "term");
  }
});

function messageHarness(get) {
  const context = { scopeVersion: 0, loadVersion: 0, disposed: false, pageCount,
    folder: ref("inbox"), page: ref(4), pageSize: ref(10), busy: ref(false), sentFolder: ref(false),
    filters: ref({ q: "term", start: "", end: "" }), data: ref({ items: [], total: 0 }), selected: ref([]), error: ref(""),
    dateBoundary: value => value, api: { get }, emit: () => {}, failure: error => { throw error; }, scopeActive: () => true };
  vm.createContext(context);
  vm.runInContext(messages.slice(messages.indexOf("async function load(background"), messages.indexOf("function changeFolder(")), context);
  return context;
}
test("message pagination sends selected size on inbox/sent and retreats after deletion", async () => {
  for (const sent of [false, true]) {
    const calls = [];
    const state = messageHarness(async (path, { params }) => { calls.push({ path, params }); return { data: { items: [], total: 11 } }; });
    state.sentFolder.value = sent;
    await state.load();
    assert.deepEqual(calls.map(call => call.params.page), [4, 2]);
    assert.ok(calls.every(call => call.params.page_size === 10 && call.params.q === "term"));
    assert.equal(calls[0].path, sent ? "/notifications/sent" : "/notifications");
    assert.equal(state.page.value, 2); assert.equal(state.busy.value, false);
  }
});
test("old message page response cannot replace a newer size selection", async () => {
  const pending = deferred();
  const state = messageHarness(() => pending.promise);
  const first = state.load();
  state.page.value = 1; state.pageSize.value = 50;
  state.api.get = async () => ({ data: { items: [{ id: "new" }], total: 1 } });
  await state.load();
  pending.resolve({ data: { items: [{ id: "old" }], total: 100 } }); await first;
  assert.equal(state.data.value.items[0].id, "new"); assert.equal(state.pageSize.value, 50);
});

function auditHarness() {
  const calls = [];
  const context = { pageCount, canAccessConsoleView, token: ref("token"), businessContextEpoch: 0, user: ref({ platform_role: "super_admin", verified_status: "verified" }), enterprise: ref(null), businessView: ref(""),
    auditLoadVersion: 0, auditDetailVersion: 0, auditTimelineVersion: 0,
    auditBusy: ref(false), auditError: ref(""), auditItems: ref([]), auditCategories: ref([]),
    auditFilters: ref({ page: 5, page_size: 20, total: 0, q: "audit" }),
    auditTimelineRoot: ref("root"), auditTimelineBusy: ref(false), auditTimelineError: ref(""), auditTimeline: ref([]), auditSelected: ref(null), auditTimelineTotal: ref(0), auditTimelinePage: ref(5), auditTimelinePageSize: ref(10),
    businessViewParams: () => ({}), auditFailure: error => error.message,
    api: { get: async (path, config) => { calls.push({ path, config }); return { data: { items: [], total: path.endsWith("/timeline") ? 11 : 21, page: config.params.page, page_size: config.params.page_size } }; } },
  };
  context.auditQuery = () => ({ ...context.auditFilters.value });
  vm.createContext(context);
  vm.runInContext(consoleSource.slice(consoleSource.indexOf("async function loadAuditLogs()"), consoleSource.indexOf("function searchAuditLogs()")) +
    consoleSource.slice(consoleSource.indexOf("async function loadAuditTimeline("), consoleSource.indexOf("const auditFieldLabels =")), context);
  return { context, calls };
}
test("audit list and timeline send selected sizes and retreat to valid server pages", async () => {
  const { context, calls } = auditHarness();
  await context.loadAuditLogs();
  assert.deepEqual(calls.filter(call => call.path === "/audit-logs").map(call => call.config.params.page), [5, 2]);
  assert.equal(context.auditFilters.value.page, 2);
  assert.equal(calls[0].config.params.page_size, 20);
  await context.loadAuditTimeline(5);
  assert.deepEqual(calls.filter(call => call.path.endsWith("/timeline")).map(call => call.config.params.page), [5, 2]);
  assert.equal(context.auditTimelinePage.value, 2);
  assert.equal(calls.at(-1).config.params.page_size, 10);
  assert.equal(context.auditTimelineBusy.value, false);
});

test("message and both audit endpoints accept every offered page size", async () => {
  for (const size of pageSizes) {
    const requests = [];
    const message = messageHarness(async (_path, { params }) => { requests.push(params); return { data: { items: [], total: 0 } }; });
    message.page.value = 1; message.pageSize.value = size; await message.load();
    assert.equal(requests[0].page_size, size);
    const { context, calls } = auditHarness();
    context.auditFilters.value.page = 1; context.auditFilters.value.page_size = size;
    context.auditTimelinePageSize.value = size;
    await context.loadAuditLogs(); await context.loadAuditTimeline(1);
    assert.equal(calls[0].config.params.page_size, size);
    assert.equal(calls.at(-1).config.params.page_size, size);
  }
});

test("audit list/timeline discard stale pages after a newer size request", async () => {
  for (const timeline of [false, true]) {
    const { context } = auditHarness();
    const pending = deferred();
    context.auditFilters.value.page = 1;
    context.api.get = () => pending.promise;
    const first = timeline ? context.loadAuditTimeline(1) : context.loadAuditLogs();
    context.auditFilters.value.page_size = 50; context.auditTimelinePageSize.value = 50;
    context.api.get = async (_path, { params }) => ({ data: { items: [{ id: "new", created_at: "2026-10-14" }], total: 1, page: 1, page_size: params?.page_size } });
    await (timeline ? context.loadAuditTimeline(1) : context.loadAuditLogs());
    pending.resolve({ data: { items: [{ id: "old" }], total: 100, page: 1, page_size: 10 } }); await first;
    assert.equal((timeline ? context.auditTimeline : context.auditItems).value[0].id, "new");
    assert.equal((timeline ? context.auditTimelinePageSize.value : context.auditFilters.value.page_size), 50);
  }
});

test("second-enterprise timeline sends enterprise and business mode and accepts redacted events", async () => {
  const { context, calls } = auditHarness();
  context.user.value = { id: "buyer", platform_role: "", verified_status: "verified", enterprise_memberships: [
    { enterprise_id: "first", role: "enterprise_admin", status: "active" },
    { enterprise_id: "second", role: "enterprise_admin", status: "active" },
  ] };
  context.enterprise.value = { id: "second" }; context.businessView.value = "buyer";
  context.businessViewParams = businessViewParams;
  const event = { id: "event", action: "order_paid", actor: "buyer", created_at: "2026-10-14", result: "success" };
  context.api.get = async (path, config) => { calls.push({ path, config }); return { data: { event, items: [event], page: 1, page_size: 10, total: 1 } }; };
  await context.loadAuditTimeline(1, true);
  assert.deepEqual({ ...calls[0].config.params }, { enterprise_id: "second", business_view: "buyer", page: 1, page_size: 10 });
  assert.equal(context.auditSelected.value.id, "event");
  assert.equal(context.auditTimeline.value[0].id, "event");
  assert.equal(context.auditTimelineError.value, "");
  assert.equal("before" in context.auditSelected.value, false);
  assert.equal("after" in context.auditSelected.value, false);
  assert.match(consoleSource, /<div v-if="user\?\.platform_role" class="drawer-section">\s*<div class="drawer-section-title">变更前后值/);
  assert.match(consoleSource, /<div v-if="canAccessConsoleView\(user, enterprise, 'audit'\)" class="drawer-section">\s*<div class="drawer-head"><div class="drawer-section-title">关联事件时间线/);
});

test("timeline scope changes discard stale success and error without releasing new busy state", async () => {
  for (const change of [state => { state.enterprise.value = { id: "second" }; }, state => { state.businessView.value = "buyer"; }, state => { state.businessContextEpoch++; }, state => { state.token.value = "new-token"; }]) {
    for (const fail of [false, true]) {
      const { context } = auditHarness();
      let resolve, reject;
      context.api.get = () => new Promise((yes, no) => { resolve = yes; reject = no; });
      const request = context.loadAuditTimeline(1, true);
      change(context);
      context.auditTimelineBusy.value = true;
      if (fail) reject(new Error("old scope failure"));
      else resolve({ data: { event: { id: "old" }, items: [{ id: "old" }], total: 1 } });
      await request;
      assert.equal(context.auditSelected.value, null);
      assert.deepEqual(Array.from(context.auditTimeline.value), []);
      assert.equal(context.auditTimelineError.value, "");
      assert.equal(context.auditTimelineBusy.value, true);
    }
  }
});

test("enterprise and mode switches reset timeline root through the drawer close function", () => {
  for (const name of ["selectEnterprise", "selectBusinessView"]) {
    const start = consoleSource.indexOf(`async function ${name}(`);
    const end = consoleSource.indexOf("\nasync function ", start + 1);
    assert.match(consoleSource.slice(start, end), /closeAuditEvent\(\)/);
  }
  const start = consoleSource.indexOf("function closeAuditEvent()");
  const close = consoleSource.slice(start, consoleSource.indexOf("async function openAuditEvent", start));
  assert.match(close, /\+\+auditTimelineVersion/);
  assert.match(close, /auditTimelineRoot.value = ""/);
});

test("all five surfaces bind shared controls with size resets; ledger remains local and clamps after shrink", async () => {
  assert.equal((store.match(/<PaginationControls\b/g) || []).length, 1);
  assert.equal((messages.match(/<PaginationControls\b/g) || []).length, 1);
  assert.equal((consoleSource.match(/<PaginationControls\b/g) || []).length, 2);
  assert.equal((ledger.match(/<PaginationControls\b/g) || []).length, 1);
  assert.match(store, /@size="pageSize = \$event; applyFilters\(\)"/);
  assert.match(messages, /@size="pageSize = \$event; page = 1; load\(\)"/);
  assert.match(consoleSource, /@size="auditFilters.page_size = \$event; searchAuditLogs\(\)"/);
  assert.match(consoleSource, /@size="auditTimelinePageSize = \$event; loadAuditTimeline\(1\)"/);
  const context = vm.createContext({ computed, watch, pageCount, selectedReport: ref({ items: Array.from({ length: 101 }, (_, id) => ({ id })) }), itemPage: ref(2), itemPageSize: ref(50) });
  vm.runInContext(ledger.slice(ledger.indexOf("const visibleItems ="), ledger.indexOf("const ledgerName =")) + "\nthis.items = visibleItems;", context);
  assert.equal(context.items.value[0].id, 50); assert.equal(context.items.value.length, 50);
  context.selectedReport.value.items = [{ id: "remaining" }]; await nextTick();
  assert.equal(context.itemPage.value, 1); assert.equal(context.items.value[0].id, "remaining");
  assert.match(ledger, /local label="账单报告本地分页"/);
  assert.match(ledger, /@size="itemPageSize = \$event; itemPage = 1"/);
});
