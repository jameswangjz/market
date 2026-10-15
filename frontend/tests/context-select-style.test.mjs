import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import postcss from "postcss";
import { parse, compileScript, compileTemplate, compileStyle } from "@vue/compiler-sfc";

const style = readFileSync(new URL("../src/style.css", import.meta.url), "utf8");
const root = postcss.parse(style);
function declarations(selector) {
  const values = {};
  root.walkRules(selector, rule => rule.walkDecls(decl => { values[decl.prop] = decl.value; }));
  return values;
}

test("context auxiliary labels use scoped accessible visually-hidden styling", () => {
  const hidden = declarations(".context-select-label>.sr-only");
  assert.equal(hidden.position, "absolute");
  assert.equal(hidden.width, "1px"); assert.equal(hidden.height, "1px");
  assert.equal(hidden.padding, "0"); assert.equal(hidden.margin, "-1px");
  assert.equal(hidden.overflow, "hidden");
  assert.equal(hidden.clip, "rect(0,0,0,0)");
  assert.equal(hidden["clip-path"], "inset(50%)");
  assert.equal(hidden["white-space"], "nowrap");
  assert.equal(hidden.border, "0");
  assert.equal(hidden.display, undefined);
  assert.equal(hidden.visibility, undefined);
});

test("context selects preserve full role label width without increasing global typography", () => {
  const label = declarations(".context-select-label");
  const select = declarations(".context-select-label select");
  assert.equal(label["min-width"], "78px");
  assert.equal(label["flex-shrink"], "0");
  assert.equal(label["max-width"], "110px");
  assert.equal(select["min-width"], "78px");
  assert.equal(select["max-width"], "100%");
  assert.equal(select.height, "30px");
  assert.equal(select["font-size"], "12px");
  assert.equal(select.padding, "4px 7px");
});

test("ConsoleApp script/template and updated context CSS compile with accessible role labels", () => {
  const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
  const parsed = parse(source, { filename: "ConsoleApp.vue" });
  assert.deepEqual(parsed.errors, []);
  const script = compileScript(parsed.descriptor, { id: "context-select-test" });
  const template = compileTemplate({ source: parsed.descriptor.template.content,
    filename: "ConsoleApp.vue", id: "context-select-test", compilerOptions: { bindingMetadata: script.bindings } });
  assert.deepEqual(template.errors, []);
  assert.deepEqual(compileStyle({ source: style, filename: "style.css", id: "context-select-test" }).errors, []);
  assert.match(source, /<span class="sr-only">业务身份<\/span>/);
  assert.match(source, /aria-label="业务身份"/);
  assert.match(source, /<option value="provider">提供方<\/option><option value="buyer">购买方<\/option>/);
});
