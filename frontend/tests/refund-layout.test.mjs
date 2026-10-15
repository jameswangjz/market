import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { parse, compileScript, compileTemplate, compileStyle } from "@vue/compiler-sfc";

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
