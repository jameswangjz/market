import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { compile, createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { slaResultState, slaMetric, slaResultReason } from "../src/slaResultDisplay.js";
import { metricRows } from "../src/slaEvidence.js";

const source = readFileSync(new URL("../src/SlaManagement.vue", import.meta.url), "utf8");
const listStart = source.indexOf('<section><h3>周期考核结果');
const detailEnd = source.indexOf('\n    </section>\n    <SlaMatrixPanel', listStart);
assert.ok(listStart >= 0 && detailEnd > listStart);
const render = compile(`<section>${source.slice(listStart, detailEnd)}</section>`);
async function resultHtml(result) {
  return renderToString(createSSRApp({ components: { X: { render: () => null }, ExternalLink: { render: () => null } },
    setup: () => ({ overviewData: { results: [result] }, selected: result, selectedMetrics: metricRows(result), openEvidence: () => {},
      slaResultState, slaMetric, slaResultReason, date: () => "-" }), render }));
}

test("five explicit SLA states have consistent titles, labels and safe neutral fallbacks", () => {
  for (const [status, label, className] of [["met", "达标", "status-done"], ["warning", "预警", "status-review"],
    ["breached", "违约", "status-blocked"], ["insufficient_data", "数据不足", "status-todo"], ["unknown", "状态未知", "status-todo"]]) {
    const state = slaResultState(status);
    assert.equal(state.label, label); assert.equal(state.className, className);
    assert.ok(state.title.includes(label === "状态未知" ? "未知状态" : label));
    assert.ok(state.reason); assert.ok(state.advice);
  }
  for (const status of [null, undefined, "", "new-backend-state", "constructor", "toString", "__proto__"])
    assert.equal(slaResultState(status), slaResultState("unknown"));
});

test("missing or invalid SLA numbers are a dash without fabricated units or zeroes", () => {
  for (const value of [null, undefined, "", "100", false, NaN, Infinity, -1]) {
    assert.equal(slaMetric(value), "—"); assert.equal(slaMetric(value, "%"), "—"); assert.equal(slaMetric(value, " ms"), "—");
  }
  assert.equal(slaMetric(0, "%"), "0%"); assert.equal(slaMetric(100, "%"), "100%");
  assert.equal(slaMetric(12.5, " ms"), "12.5 ms"); assert.equal(slaMetric(0), "0");
});

test("reasons distinguish missing sources, snapshots and insufficient samples without blanket success", () => {
  for (const reason of ["source_missing", "missing_snapshot", "invalid_snapshot", "sample_count", "probe_coverage", "duration_completeness", "upstream_insufficient", "invalid_measurement"])
    assert.match(slaResultReason({ status: "insufficient_data", reason }), /无法完成评估/);
  assert.equal(slaResultReason({ status: "warning", breach_reason: "  已记录异常  " }), "已记录异常");
  assert.match(slaResultReason({ status: "insufficient_data", reason: "new_reason" }), /无法判定/);
  for (const status of ["met", "warning", "breached", "insufficient_data", "unknown"])
    assert.doesNotMatch(slaResultReason({ status }), /各项指标均达到目标|无需额外处置/);
});

test("real result table and drawer render insufficient/unknown neutrally with null metrics", async () => {
  for (const status of ["insufficient_data", "unknown", "future_status", null]) {
    const html = await resultHtml({ id: "isolated", profile_name: "Fixture", status,
      availability: null, avg_latency_ms: null, error_rate: null, delivery_compliance: null, sample_count: null, success_count: null });
    assert.match(html, /status-todo/);
    assert.doesNotMatch(html, /status-done|status-blocked|SLA 达标详情|SLA 违约详情|各项指标均达到目标|无需额外处置/);
    assert.ok((html.match(/—/g) || []).length >= 10);
    assert.doesNotMatch(html, />100%<|>0 ms<|>0%<|null%|undefined/);
  }
});

test("real result markup preserves known status styling, reason escaping and mean latency semantics", async () => {
  for (const status of ["met", "warning", "breached"]) {
    const html = await resultHtml({ id: "isolated", status, profile_name: "Fixture", availability: 99,
      avg_latency_ms: 25, latency_p95: 987, error_rate: 0, delivery_compliance: 100,
      sample_count: 100, success_count: 99, breach_reason: "<script>fixture</script>" });
    assert.ok(html.includes(slaResultState(status).title)); assert.ok(html.includes(slaResultState(status).className));
    assert.match(html, /平均延迟/); assert.match(html, /25 ms/); assert.doesNotMatch(html, /987/);
    assert.match(html, /&lt;script&gt;fixture&lt;\/script&gt;/); assert.doesNotMatch(html, /<script>/);
  }
  const p95Only = await resultHtml({ id: "isolated", status: "insufficient_data", latency_p95: 987, reason: "source_missing" });
  assert.doesNotMatch(p95Only, /987/); assert.match(p95Only, /数据源缺失/);
});

test("merged ConsoleApp SFC compiles with the result helpers", () => {
  const { descriptor, errors } = parse(source, { filename: "ConsoleApp.vue" });
  assert.deepEqual(errors, []);
  const script = compileScript(descriptor, { id: "sla-result-display" });
  assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: "ConsoleApp.vue",
    id: "sla-result-display", compilerOptions: { bindingMetadata: script.bindings } }).errors, []);
});
