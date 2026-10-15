import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { compile, createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { refundMoney, refundPlanning } from "../src/refunds.js";

const { descriptor } = parse(readFileSync(new URL("../src/RefundRequestForm.vue", import.meta.url), "utf8"));
async function render(item) {
  const preview = { status: "available", item };
  return renderToString(createSSRApp({
    components: { Send: { render: () => null }, Calculator: { render: () => null } },
    setup: () => ({ preview, previewState: refundPlanning(preview), refundMoney, busy: false, quoteBusy: false,
      enabled: true, error: "", validation: "", orderNo: "ORD-ISOLATED", amount: "", reason: "", ticketId: "", paymentId: "", submit() {}, quote() {} }),
    render: compile(descriptor.template.content),
  }));
}
test("time-based refund quote uses original time basis, never missing call usage", async () => {
  const html = await render({ basis: "time", refundable_balance_before: "12.50", effective_ends_at: "2026-12-01T00:00:00Z" });
  assert.match(html, /时间口径：按原订阅时段计算/);
  assert.match(html, /¥12\.50/);
  assert.match(html, /试算后预计截止：2026-12-01T00:00:00Z/);
  assert.doesNotMatch(html, /实际扣减次数|未知用量|¥0\.00|原时段：/);
  const script = compileScript(descriptor, { id: "refund-quote" });
  assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: "RefundRequestForm.vue", id: "refund-quote", compilerOptions: { bindingMetadata: script.bindings } }).errors, []);
});
test("calls quote preserves server zero but never manufactures it for missing evidence", async () => {
  assert.match(await render({ basis: "calls", charged_calls: 0 }), /实际扣减次数：0/);
  const missing = await render({ basis: "calls" });
  assert.match(missing, /实际扣减次数：未知/);
  assert.doesNotMatch(missing, /实际扣减次数：0|时间口径/);
  const unknown = await render({});
  assert.match(unknown, /计量口径未知/);
  assert.doesNotMatch(unknown, /实际扣减次数|时间口径/);
});
