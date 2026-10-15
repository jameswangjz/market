import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { compile, createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { parse } from "@vue/compiler-sfc";
import { createSlaReadSession, sourcePageTotal, sourceFactTotal, metricRows, slaMetricUnit, slaTier, slaEvidenceReason, slaSourceReason, slaMetricNames, slaParty } from "../src/slaEvidence.js";
import { slaMetric, slaResultState, slaResultReason } from "../src/slaResultDisplay.js";

test("source requests retain exact assessment version and context and are GET only", async () => {
  const calls = [], state = {};
  const session = createSlaReadSession({ get: async (...args) => { calls.push(args); return { data: { version: 1, as_of: "2026-09-01", metric_result: { value: 12 } } }; } }, state);
  await session.read("/sla/results/assessment/sources", { metric: "latency_p95", version: 1, page: 2, page_size: 20 });
  assert.deepEqual(calls[0], ["/sla/results/assessment/sources", { params: { metric: "latency_p95", version: 1, page: 2, page_size: 20 } }]);
  assert.equal(state.data.as_of, "2026-09-01"); assert.equal(state.data.metric_result.value, 12);
});

test("stale scope reads and their errors cannot replace a newer response", async () => {
  const waits = [], state = {};
  const session = createSlaReadSession({ get: () => new Promise((resolve, reject) => waits.push({ resolve, reject })) }, state);
  const old = session.read("old"), current = session.read("new");
  waits[1].resolve({ data: { scope: "new" } }); await current;
  waits[0].reject(new Error("old error")); await old;
  assert.equal(state.data.scope, "new"); assert.equal(state.error, ""); assert.equal(state.busy, false);
});

test("reset/dispose invalidates pending reads and wrong-version evidence fails closed", async () => {
  const state = {}; let finish;
  const session = createSlaReadSession({ get: () => new Promise(resolve => { finish = resolve; }) }, state);
  const pending = session.read("old"); session.reset(); finish({ data: { secret: "old-context" } }); await pending;
  assert.equal(state.data, null); assert.equal(state.busy, false);
  const second = session.read("second"); session.dispose(); finish({ data: { old: true } }); await second; assert.equal(state.data, null);
  const safe = createSlaReadSession({ get: async () => ({ data: { version: 2 } }) }, state);
  await safe.read("version-one", { version: 1 }, data => data.version === 1);
  assert.equal(state.data, null); assert.match(state.error, /版本不一致/);
});

test("truncated evidence pages only retained rows, never the full calculation count", () => {
  assert.equal(sourcePageTotal({ total: 9000, retained_count: 200, truncated: true }), 200);
  assert.equal(sourcePageTotal({ total: 9000 }), 0);
  assert.equal(sourceFactTotal({ total: 200, retained_count: 200, source_total: 9000 }), 9000);
  assert.equal(sourceFactTotal({ total: 200 }), null);
  assert.equal(sourceFactTotal({ observed_total: 42 }), 42);
});

test("source reason translations retain codes and metric units never relabel P95 as average", () => {
  assert.equal(slaEvidenceReason("upstream_failure"), "上游服务故障"); assert.equal(slaEvidenceReason("caller_parameters"), "调用参数问题");
  assert.equal(slaEvidenceReason("constructor"), "未识别原因");
  assert.equal(slaMetricUnit("latency_p95", { unit: "ms" }), " ms");
  assert.equal(slaMetricUnit("delivery_duration", { unit: "seconds" }), " 秒");
  assert.equal(slaParty("caller"), "调用方");
  assert.equal(slaSourceReason("http_failure"), "HTTP响应失败"); assert.equal(slaSourceReason("probe_timeout"), "探测超时");
  for (const code of [null, undefined, "future", "constructor"]) assert.equal(slaSourceReason(code), "实际原因未知");
});

test("real source drawer uses selected-version metrics/counts/causes and not the current result", async () => {
  const { descriptor } = parse(readFileSync(new URL("../src/SlaSourcesDrawer.vue", import.meta.url), "utf8"));
  const state = { busy: false, error: "", data: { evidence_status: "recorded", as_of: "OLD-AS-OF", request_source_cutoff: "OLD-CUTOFF", input_hash: "old-hash",
    total: 200, retained_count: 200, source_total: 900, truncated: true,
    metric_result: { value: 12, target: 50, warning: 40, sample_count: 100, unit: "ms", status: "warning", frozen_tier: "enterprise", rule_version: "old-rule" },
    items: [{ fact_id: "fact", occurred_at: "OLD-OCCURRED", duration_ms: 12, status: "failed", status_code: 502, reason: "upstream_failure", source_reason: "http_failure", responsibility: "provider", policy_version: "source-policy-v1" }] } };
  const ctx = {};
  await renderToString(createSSRApp({ components: { X: { render: () => null }, RefreshCw: { render: () => null }, PaginationControls: { render: () => null } },
    setup: () => ({ state, result: { version: 1, value: 99999, frozen_tier: "strategic", rule_version: "CURRENT-RULE" }, metric: "latency_p95", total: 200,
      page: 1, size: 20, date: value => value || "—", status: () => "失败", emit: () => {}, load: () => {}, go: () => {}, resize: () => {},
      slaMetricNames, slaMetric, slaMetricUnit, slaTier, slaEvidenceReason, slaSourceReason, sourceFactTotal, slaParty, slaResultState, slaResultReason }), render: compile(descriptor.template.content) }), ctx);
  const html = ctx.teleports.body;
  assert.match(html, /全访问事实 900 条 · 可查询 200 条/); assert.match(html, /证据已截断/);
  assert.match(html, /50 ms/); assert.match(html, /40 ms/); assert.match(html, /企业级/); assert.match(html, /old-rule/);
  assert.match(html, /OLD-AS-OF/); assert.match(html, /OLD-OCCURRED/); assert.match(html, /source-policy-v1/);
  assert.match(html, /title="upstream_failure">上游服务故障/); assert.match(html, /title="http_failure">实际：HTTP响应失败/); assert.doesNotMatch(html, /99999|CURRENT-RULE|战略级|平均延迟/);
});

test("not_started/not_applicable remain neutral even when a source is unavailable", () => {
  for (const status of ["not_started", "not_applicable"]) {
    const [row] = metricRows({ metrics: { availability: { status, value: null } }, source_summary: { source_available: { availability: false } } });
    assert.equal(row.sourceMissing, false); assert.equal(slaResultState(status).className, "status-todo");
  }
  assert.equal(slaResultState("historical_missing").label, "历史证据缺失");
  assert.match(slaResultReason({ reason: "source_missing" }), /数据源缺失/);
  assert.match(slaResultReason({ reason: "insufficient_samples" }), /有效样本不足/);
});

test("after-sales entry is lazy, scope-aware and uses only the new read-only SLA endpoint", () => {
  const detail = readFileSync(new URL("../src/AfterSalesDetail.vue", import.meta.url), "utf8");
  const panel = readFileSync(new URL("../src/TicketSlaEvidence.vue", import.meta.url), "utf8");
  assert.match(detail, /<TicketSlaEvidence v-if="slaExpanded"/); assert.match(detail, /:params="params"/);
  assert.match(panel, /\/sla\/evidence`/); assert.doesNotMatch(panel, /\.post\(|\/sla`|\/sla"/);
  assert.match(panel, /以下仅为观察区间，不是买方达标或违约评估/);
  assert.match(panel, /clockSession\.reset\(\); eventSession\.reset\(\)/);
});

function adjustmentHarness() {
  const source = readFileSync(new URL("../src/SlaEnterpriseAdjustment.vue", import.meta.url), "utf8");
  const start = source.indexOf("async function save()"), end = source.indexOf("watch(", start);
  const calls = [];
  const dependencies = { permitted: { value: true }, saving: { value: false }, loading: { value: false },
    levels: { value: [{ id: "enterprise-level" }] }, levelId: { value: "enterprise-level" }, reason: { value: " 调整原因 " }, error: { value: "" }, message: { value: "" },
    props: { enterprise: { id: "enterprise-id" }, api: { post: async (...args) => { calls.push(args); throw new Error("isolated failure"); } } },
    date: value => value, load: async () => {} };
  const save = new Function(...Object.keys(dependencies), `let epoch = 1; ${source.slice(start, end)};return save;`)(...Object.values(dependencies));
  return { ...dependencies, calls, save };
}
test("enterprise adjustment enforces manager/level/reason and retains failed inputs", async () => {
  const h = adjustmentHarness(); h.reason.value = " "; await h.save(); assert.equal(h.calls.length, 0);
  h.reason.value = " 调整原因 "; await h.save();
  assert.deepEqual(h.calls[0], ["/sla/service-level-assignments", { enterprise_id: "enterprise-id", service_level_id: "enterprise-level", reason: "调整原因" }]);
  assert.equal(h.reason.value, " 调整原因 "); assert.equal(h.saving.value, false);
  h.permitted.value = false; await h.save(); assert.equal(h.calls.length, 1);
});
test("enterprise adjustment duplicate submit is single-flight", async () => {
  const h = adjustmentHarness(); let finish;
  h.props.api.post = (...args) => { h.calls.push(args); return new Promise(resolve => { finish = resolve; }); };
  const first = h.save(); await h.save(); assert.equal(h.calls.length, 1);
  finish({ data: { item: { effective_at: "2026-10-15T12:00:00Z" } } }); await first;
  assert.equal(h.reason.value, ""); assert.match(h.message.value, /已有工单与订单冻结承诺不变/);
});
