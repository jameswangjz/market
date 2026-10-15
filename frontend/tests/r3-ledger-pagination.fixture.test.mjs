import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { compile, computed, createSSRApp, h, nextTick, reactive, ref, watch } from "vue";
import { renderToString } from "@vue/server-renderer";
import { ArrowLeft, ArrowRight, CornerDownLeft } from "lucide-vue-next";
import { pageCount, pageSizes, validPage } from "../src/pagination.js";

const source = name => readFileSync(new URL(`../src/${name}`, import.meta.url), "utf8");
const ledger = source("LedgerComparison.vue");
const controls = source("PaginationControls.vue");
const controlsScript = controls.split("<script setup>")[1].split("</script>")[0].replace(/^import .*;$/gm, "");
const rows = count => Array.from({ length: count }, (_, id) => ({ id, order_no: `isolated-ledger-${id}` }));

function fixture(count = 231) {
  const state = { computed, watch, pageCount, selectedReport: ref({ items: rows(count) }),
    itemPage: ref(1), itemPageSize: ref(50), resolution: ref({}), error: ref("") };
  vm.createContext(state);
  vm.runInContext(ledger.slice(ledger.indexOf("const visibleItems ="), ledger.indexOf("const ledgerName =")) + "\nthis.visible = visibleItems;", state);
  vm.runInContext(ledger.slice(ledger.indexOf("function openReport("), ledger.indexOf("async function importAndCompare(")), state);
  const events = [];
  const props = reactive({ get page() { return state.itemPage.value; }, get pageSize() { return state.itemPageSize.value; },
    get total() { return state.selectedReport.value.items.length; }, busy: false, local: true, label: "账单报告本地分页" });
  const control = vm.createContext({ computed, ref, watch, pageCount, pageSizes, validPage,
    defineProps: () => props, defineEmits: () => (event, value) => {
      events.push([event, value]);
      if (event === "page") state.itemPage.value = value;
      if (event === "size") { state.itemPageSize.value = value; state.itemPage.value = 1; }
    } });
  vm.runInContext(controlsScript + "\nthis.exposed = { pages, canJump, jump, error, go, resize };", control);
  return { state, control, props, events };
}

test("isolated ledger uses all four sizes and real controls to jump through local slices", () => {
  for (const size of pageSizes) {
    const f = fixture();
    f.control.resize({ target: { value: String(size) } });
    assert.equal(f.state.itemPageSize.value, size);
    f.control.go("2");
    assert.equal(f.state.itemPage.value, 2);
    assert.equal(f.state.visible.value.length, size);
    assert.equal(f.state.visible.value[0].id, size);
    f.control.resize({ target: { value: String(size === 10 ? 20 : 10) } });
    assert.equal(f.state.itemPage.value, 1);
  }
});

test("isolated ledger rejects invalid jumps, blocks busy events, and clamps after data shrink", async () => {
  const f = fixture();
  for (const page of ["0", "-1", "1.5", "999"]) f.control.go(page);
  assert.equal(f.state.itemPage.value, 1);
  f.props.busy = true;
  f.control.go("2"); f.control.resize({ target: { value: "100" } });
  assert.deepEqual(f.events, []);
  f.props.busy = false; f.control.go("5");
  assert.equal(f.state.visible.value.length, 31);
  f.state.selectedReport.value.items = rows(1); await nextTick();
  assert.equal(f.state.itemPage.value, 1);
  f.state.selectedReport.value.items = []; await nextTick();
  assert.equal(f.state.visible.value.length, 0);
  f.control.go("2"); assert.equal(f.state.itemPage.value, 1);
});

test("opening another isolated report resets local page and leaves fixture data in memory", () => {
  const f = fixture(); f.control.go("3");
  f.state.openReport({ items: rows(2) });
  assert.equal(f.state.itemPage.value, 1);
  assert.equal(f.state.visible.value.length, 2);
  assert.match(ledger, /@size="itemPageSize = \$event; itemPage = 1"/);
  assert.match(ledger, /local label="账单报告本地分页"/);
});

test("real pagination component renders explicit local label and disabled empty jump in SSR fixture", async () => {
  const component = {
    props: ["page", "pageSize", "total", "busy", "local", "label"],
    components: { ArrowLeft, ArrowRight, CornerDownLeft },
    setup(props, { emit }) {
      const context = vm.createContext({ computed, ref, watch, pageCount, pageSizes, validPage,
        defineProps: () => props, defineEmits: () => emit });
      vm.runInContext(controlsScript + "\nthis.exposed = { pages, pageSizes, canJump, jump, error, go, resize };", context);
      return context.exposed;
    },
    render: compile(controls.split("<template>")[1].split("</template>")[0]),
  };
  const html = await renderToString(createSSRApp({ render: () => h(component,
    { page: 1, pageSize: 20, total: 0, busy: false, local: true, label: "账单报告本地分页" }) }));
  assert.match(html, /本地分页/);
  assert.match(html, /共 0 条/);
  assert.match(html, /aria-label="跳转页码"[^>]*disabled/);
  assert.match(html, /aria-label="跳转到指定页"[^>]*disabled/);
  assert.doesNotMatch(html, /<select[^>]*disabled/);
});
