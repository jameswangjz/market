import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { compile, createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { metricRows, reviewSlaPayload } from "../src/slaEvidence.js";
import { slaMetric, slaResultState, slaResultReason } from "../src/slaResultDisplay.js";

const component = readFileSync(new URL("../src/SlaEvidencePanel.vue", import.meta.url), "utf8");
const { descriptor } = parse(component);
async function html(results = [], assignments = []) {
  return renderToString(createSSRApp({ setup: () => ({ results, assignments, metricRows,
    slaMetric, slaResultState, slaResultReason, date: value => value || "—" }),
    render: compile(descriptor.template.content) }));
}

test("enterprise approval alone binds selected SLA, rejection/personal never do", () => {
  assert.deepEqual(reviewSlaPayload("enterprise", "approve", "level", " reason "), { service_level_id: "level", sla_reason: "reason" });
  assert.deepEqual(reviewSlaPayload("enterprise", "approve", "", ""), { service_level_id: "", sla_reason: "" });
  assert.deepEqual(reviewSlaPayload("enterprise", "reject", "level", "reason"), {});
  assert.deepEqual(reviewSlaPayload("personal", "approve", "level", "reason"), {});
});

test("metric extraction uses supplied frozen values and distinct P95 units", () => {
  assert.deepEqual(metricRows(null), []);
  assert.deepEqual(metricRows({ metrics: [] }), []);
  const [row] = metricRows({ metrics: { latency_p95: { value: 235, target: 4000, status: "met" } } });
  assert.equal(row.label, "请求延迟 P95"); assert.equal(row.unit, " ms"); assert.equal(row.target, 4000);
});

test("actual template independently renders missing sources and insufficient samples neutrally", async () => {
  const output = await html([{ id: "a", order_no: "OWNED-TEST", metrics: {
    availability: { status: "insufficient_data", reason: "sample_count", value: null, sample_count: 2 },
    latency_p95: { status: "met", value: null },
  }, source_summary: { source_available: { latency_p95: false } } }]);
  assert.match(output, /数据不足/); assert.match(output, /无数据源/);
  assert.doesNotMatch(output, /status-done|status-blocked|100%|null ms/);
  assert.match(output, /OWNED-TEST/); assert.match(output, /—/);
});

test("actual template preserves P95, frozen identity and escaped evidence without invented links", async () => {
  const output = await html([{ id: "a", frozen_tier: "strategic", rule_version: "frozen-rule", metrics: {
    latency_p95: { status: "warning", value: 3200, target: 3000, sample_count: 100, completeness_percent: 99 },
  }, source_summary: { source_version: "probe-v1", evidence_digest: "<script>unsafe</script>" } }]);
  assert.match(output, /P95/); assert.match(output, /3200 ms/); assert.doesNotMatch(output, /平均延迟|<script>|href=/);
  assert.match(output, /frozen-rule/); assert.match(output, /strategic/); assert.match(output, /&lt;script&gt;/);
  assert.match(output, /逐条证据未由此接口提供/);
});

test("empty and legacy results do not infer compliance or manufacture evidence", async () => {
  assert.match(await html(), /暂无周期证据/);
  assert.match(await html([{ id: "legacy", status: "unknown" }]), /缺少逐指标证据/);
});

test("SLA evidence and ConsoleApp compile with real binding metadata", () => {
  for (const name of ["SlaEvidencePanel.vue", "ConsoleApp.vue"]) {
    const source = readFileSync(new URL(`../src/${name}`, import.meta.url), "utf8");
    const { descriptor, errors } = parse(source, { filename: name }); assert.deepEqual(errors, []);
    const script = compileScript(descriptor, { id: "sla-evidence" });
    assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: name, id: "sla-evidence",
      compilerOptions: { bindingMetadata: script.bindings } }).errors, []);
  }
});

test("real review submit blocks concurrency and retains SLA inputs after failure", async () => {
  const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
  const start = source.indexOf("async function submitIdentityReview(decision)");
  const end = source.indexOf("async function submitPersonalVerification", start);
  let requests = 0;
  let rejectRequest;
  const identityReview = { value: { kind: "enterprise", item: { id: "owned", verification_status: "pending_review" } } };
  const identityReviewSaving = { value: false };
  const identityReviewSla = { value: { level_id: "enterprise", reason: "preserved reason" } };
  const dependencies = { identityReview, identityReviewSaving, identityReviewSla,
    identityReviewLevelsLoading: { value: false }, identityReviewComment: { value: "review" },
    canReviewIdentity: () => true, notify: () => {}, loadViewData: async () => {}, reviewSlaPayload,
    api: { post: () => { requests++; return new Promise((resolve, reject) => { rejectRequest = reject; }); } } };
  const submit = new Function(...Object.keys(dependencies), `${source.slice(start, end)}; return submitIdentityReview;`)(...Object.values(dependencies));
  const pending = submit("approve");
  await submit("approve"); assert.equal(requests, 1); assert.equal(identityReviewSaving.value, true);
  rejectRequest(new Error("isolated API failure")); await pending;
  assert.equal(identityReviewSaving.value, false); assert.equal(identityReview.value.item.id, "owned");
  assert.deepEqual(identityReviewSla.value, { level_id: "enterprise", reason: "preserved reason" });
  dependencies.identityReviewLevelsLoading.value = true;
  await submit("approve"); assert.equal(requests, 1);
});
