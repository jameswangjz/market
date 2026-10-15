import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { compile, createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { slaClockState, slaParty, slaSeconds, slaMinutes, slaMetricNames, slaEvidenceReason } from "../src/slaEvidence.js";

test("ticket clock status distinguishes ongoing overdue, completed breach and unconfirmed closure", () => {
  assert.equal(slaClockState({ status: "breached", lifecycle: "open" }).label, "进行中逾期");
  assert.equal(slaClockState({ status: "breached", lifecycle: "completed" }).label, "完成后违约");
  assert.match(slaClockState({ status: "breached", lifecycle: "closed_incomplete" }).label, /未确认解决/);
  assert.equal(slaClockState({ status: "closed_without_confirmation", lifecycle: "closed_incomplete" }).className, "status-todo");
  assert.equal(slaClockState({ status: "within_target", lifecycle: "open" }).label, "进行中未逾期");
  assert.equal(slaClockState({ status: "within_target", lifecycle: "completed" }).className, "status-done");
  assert.equal(slaClockState({ status: "met", lifecycle: "interrupted" }).className, "status-todo");
  for (const status of ["insufficient_data", "unknown", "not_applicable", "future", "constructor"])
    assert.equal(slaClockState({ status }).className, "status-todo");
});

test("responsibility is explicit and absent durations are never zero or percentages", () => {
  assert.equal(slaParty("buyer"), "买方配合"); assert.equal(slaParty("provider"), "提供方");
  assert.equal(slaParty("platform"), "平台"); assert.equal(slaParty("future"), "责任未明确");
  for (const value of [null, undefined, NaN, -1, "10", true]) assert.equal(slaSeconds(value), "—");
  assert.equal(slaSeconds(0), "0 秒"); assert.equal(slaSeconds(12.5), "12.5 秒");
  assert.equal(slaMinutes(90), "1.5 分钟"); assert.equal(slaMinutes(21600), "360 分钟");
  assert.equal(slaMinutes(null), "—");
});

test("actual ticket metrics template shows all parties and safely escaped source event identifiers", async () => {
  const source = readFileSync(new URL("../src/SlaTicketMetrics.vue", import.meta.url), "utf8");
  const { descriptor } = parse(source);
  const items = ["provider", "platform", "buyer"].map((responsibility, index) => ({ id: String(index), responsibility,
    metric: "resolution", status: "insufficient_data", lifecycle: "open", elapsed_seconds: null,
    target_seconds: 100, warning_seconds: 80, source_event_ids: ["<script>event</script>"], rule_version: "frozen-rule",
    calendar_bindings: [{ year: 2026, calendar_version_id: "calendar-version-recorded", version: "legacy-fallback" }] }));
  const html = await renderToString(createSSRApp({ setup: () => ({ items, slaClockState, slaParty, slaMinutes, slaEvidenceReason,
    metricName: value => slaMetricNames[value], date: () => "—" }), render: compile(descriptor.template.content) }));
  assert.match(html, /提供方/); assert.match(html, /平台/); assert.match(html, /买方配合/);
  assert.match(html, /数据不足/); assert.match(html, /frozen-rule/); assert.match(html, /&lt;script&gt;event&lt;\/script&gt;/);
  assert.match(html, /2026：calendar-version-recorded/); assert.doesNotMatch(html, /legacy-fallback/);
  assert.doesNotMatch(html, /status-done|status-blocked|<script>|null 秒/);
});

test("new SLA-only components compile without touching after-sales components", () => {
  for (const name of ["SlaTicketMetrics", "SlaEnterpriseAdjustment", "SlaSourcesDrawer", "SlaTicketEvidence", "TicketSlaEvidence"]) {
    const { descriptor, errors } = parse(readFileSync(new URL(`../src/${name}.vue`, import.meta.url), "utf8"));
    assert.deepEqual(errors, []); const script = compileScript(descriptor, { id: name });
    assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: `${name}.vue`, id: name,
      compilerOptions: { bindingMetadata: script.bindings } }).errors, []);
  }
});
