import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parse, compileScript, compileTemplate, compileStyle } from "@vue/compiler-sfc";
import { compile, createSSRApp, h } from "vue";
import { renderToString } from "@vue/server-renderer";
import { refundStatus, refundMoney, refundSource, refundPlanning, canAssessRefund, refundExecutionActions } from "../src/refunds.js";

test("refund forms own scoped compact control styling independent of ConsoleApp", () => {
  const css = readFileSync(new URL("../src/refundForms.css", import.meta.url), "utf8");
  assert.match(css, /flex-direction: column/);
  assert.match(css, /width: 100%/);
  assert.match(css, /font-size: 13px/);
  assert.match(css, /@media \(max-width: 640px\)/);
  const style = compileStyle({ source: css, filename: "refundForms.css", id: "data-v-refund", scoped: true });
  assert.deepEqual(style.errors, []);
  assert.match(style.code, /\[data-v-refund\]/);
  for (const name of ["RefundRequestForm", "RefundManualAssessment", "RefundReviewForm"]) {
    const { descriptor, errors } = parse(readFileSync(new URL(`../src/${name}.vue`, import.meta.url), "utf8"));
    assert.deepEqual(errors, []);
    assert.ok(descriptor.styles.some(value => value.scoped && value.src === "./refundForms.css"));
    const script = compileScript(descriptor, { id: name });
    assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: `${name}.vue`, id: name, compilerOptions: { bindingMetadata: script.bindings } }).errors, []);
    assert.match(descriptor.template.content, /refund-form/);
  }
});

test("only requested refund details render review inputs; later states retain read-only review reason", async () => {
  const { descriptor } = parse(readFileSync(new URL("../src/RefundDetail.vue", import.meta.url), "utf8"));
  const stub = { render: () => null };
  for (const status of ["requested", "approved", "executing", "failed", "completed", "rejected"]) {
    const html = await renderToString(createSSRApp({
      components: { RefundReviewForm: { render: () => h("textarea", { "data-review-input": "true" }) },
        RefundManualAssessment: stub, Play: stub, RotateCcw: stub, RefreshCw: stub, X: stub },
      setup: () => ({ item: { id: "refund", status, source: "ordinary", version: 1,
        review_reason: "已记录的审核理由", execution_available: false },
      user: { id: "admin", verified_status: "verified", platform_role: "super_admin" },
      busy: false, error: "", allowedActions: [], approvalAvailable: false, progress: null,
      date: () => "", emit: () => {}, refundStatus, refundMoney, refundSource, refundPlanning,
      canAssessRefund, refundExecutionActions }), render: compile(descriptor.template.content),
    }));
    assert.equal(html.includes("data-review-input"), status === "requested", status);
    assert.match(html, /已记录的审核理由/);
  }
});
