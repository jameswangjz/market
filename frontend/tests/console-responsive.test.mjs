import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import postcss from "postcss";
import { parse, compileScript, compileTemplate, compileStyle } from "@vue/compiler-sfc";

const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
const css = readFileSync(new URL("../src/style.css", import.meta.url), "utf8");
const root = postcss.parse(css);
function declarations(selector, media = null) {
  const values = {};
  root.walkRules(rule => {
    const query = rule.parent.type === "atrule" ? rule.parent.params : null;
    if (rule.selectors.includes(selector) && query === media) {
      rule.walkDecls(decl => { values[decl.prop] = decl.value; });
    }
  });
  return values;
}

test("all console views including settings use uncapped content with existing padding", () => {
  const content = declarations(".content");
  assert.equal(content.width, "100%");
  assert.equal(content["min-width"], "0");
  assert.equal(content.padding, "34px 38px 60px");
  root.walkRules(rule => {
    if (!rule.selectors.some(selector => /^\.content(?::has\(\.settings-tabs\))?$/.test(selector))) return;
    rule.walkDecls(decl => {
      assert.notEqual(decl.prop, "max-width", rule.selector);
      if (decl.prop === "margin") assert.doesNotMatch(decl.value, /auto/);
    });
  });
  assert.equal(declarations(".content:has(.settings-tabs)")["padding-top"], "28px");
  assert.equal(declarations(".content", "(max-width:1050px)").padding, "28px 24px 50px");
  assert.equal(declarations(".content", "(max-width:760px)").padding, "24px 16px 40px");
});

test("browser scaling has no manual DPI, viewport ratio, or CSS zoom override", () => {
  assert.doesNotMatch(source, /updateUiScale|devicePixelRatio|--ui-scale|window\.(?:innerWidth|innerHeight)/);
  assert.doesNotMatch(css, /--ui-scale/);
  root.walkDecls(decl => {
    assert.notEqual(decl.prop, "zoom");
    if (decl.prop === "font-size") assert.doesNotMatch(decl.value, /\b\d*\.?\d+(?:vw|vh|vmin|vmax|vi|vb)\b/);
  });
});

test("desktop sidebar offsets and mobile drawer conventions stay intact", () => {
  assert.equal(declarations(".sidebar").width, "248px");
  assert.equal(declarations(".sidebar.collapsed").width, "76px");
  for (const [selector, width] of [[".main-shell", "248px"], [".sidebar.collapsed~.main-shell", "76px"]]) {
    const desktop = declarations(selector);
    assert.equal(desktop.width, `calc(100% - ${width})`);
    assert.equal(desktop["margin-left"], width);
    const mobile = declarations(selector, "(max-width:760px)");
    assert.equal(mobile.width, "100%");
    assert.equal(mobile["margin-left"], "0");
  }
  assert.equal(declarations(".main-shell")["min-width"], "0");
  assert.equal(declarations(".sidebar", "(max-width:760px)").transform, "translateX(-100%)");
  assert.equal(declarations(".sidebar.open", "(max-width:760px)").transform, "translateX(0)");
  assert.equal(declarations(".sidebar.collapsed", "(max-width:760px)").width, "248px");
});

test("component readability and overlay constraints remain local", () => {
  assert.equal(declarations(".modal-card").width, "min(720px,calc(100% - 32px))");
  assert.equal(declarations(".order-drawer").width, "min(510px,100%)");
  assert.equal(declarations(".login-card").width, "min(390px,80%)");
  assert.equal(declarations(".data-table td strong")["max-width"], "260px");
  assert.equal(declarations(".table-wrap").overflow, "auto");
});

test("console script, template, and shared CSS compile", () => {
  const { descriptor, errors } = parse(source, { filename: "ConsoleApp.vue" });
  assert.deepEqual(errors, []);
  const script = compileScript(descriptor, { id: "console-responsive" });
  const template = compileTemplate({ source: descriptor.template.content,
    filename: "ConsoleApp.vue", id: "console-responsive",
    compilerOptions: { bindingMetadata: script.bindings } });
  assert.deepEqual(template.errors, []);
  assert.deepEqual(compileStyle({ source: css, filename: "style.css", id: "console-responsive" }).errors, []);
});
