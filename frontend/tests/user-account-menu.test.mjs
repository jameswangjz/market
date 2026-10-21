import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
const source = readFileSync(new URL('../src/UserAccountMenu.vue', import.meta.url), 'utf8');
function harness() {
  const events = [], unmount = [], doc = { activeElement: null, addEventListener: (...args) => events.push(['add', ...args]), removeEventListener: (...args) => events.push(['remove', ...args]) };
  const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*;\n/gm, '');
  const state = new Function('defineProps', 'defineEmits', 'ref', 'nextTick', 'onMounted', 'onBeforeUnmount', 'document',
    `${script}; return {expanded,root,trigger,menu,outside,leave,blur,close,focusMenu,move};`)(
    () => ({}), () => () => {}, value => ({ value }), async () => {}, callback => callback(), callback => unmount.push(callback), doc);
  return { ...state, events, doc, unmount };
}
test('menu compiles, supports pointer and click, and keeps teleported password dialog mounted', () => {
  const { descriptor } = parse(source);
  const script = compileScript(descriptor, { id: 'account-menu' });
  const template = compileTemplate({ source: descriptor.template.content, filename: 'UserAccountMenu.vue', id: 'account-menu', compilerOptions: { bindingMetadata: script.bindings } });
  assert.deepEqual(template.errors, []);
  assert.ok(source.includes('v-show="expanded"'));
  assert.ok(source.includes("$event.pointerType === 'mouse'"));
  assert.ok(source.includes('@click="expanded = !expanded"'));
  assert.ok(source.includes('role="menu"'));
  assert.ok(source.includes('<slot />'));
});
test('outside interaction, focus departure, and Escape close the menu; internal focus retains it', () => {
  const h = harness(), inside = {};
  h.root.value = { contains: node => node === inside };
  h.expanded.value = true; h.outside({ target: inside }); assert.equal(h.expanded.value, true);
  h.doc.activeElement = inside; h.leave(); assert.equal(h.expanded.value, true);
  h.blur({ relatedTarget: null }); assert.equal(h.expanded.value, false);
  h.expanded.value = true; h.outside({ target: {} }); assert.equal(h.expanded.value, false);
  let focused = false; h.trigger.value = { focus: () => focused = true };
  h.expanded.value = true; h.close({ preventDefault() {}, stopPropagation() {} });
  assert.equal(h.expanded.value, false); assert.equal(focused, true);
  h.unmount.forEach(fn => fn());
  assert.equal(h.events[0][0], 'add'); assert.equal(h.events[1][0], 'remove'); assert.equal(h.events[0][2], h.events[1][2]);
});
test('keyboard entry and arrow navigation focus the existing menu actions', async () => {
  const h = harness();
  const items = [0, 1].map(() => ({ focus() { h.doc.activeElement = this; } }));
  h.menu.value = { querySelector: () => items[0], querySelectorAll: () => items };
  await h.focusMenu(); assert.equal(h.expanded.value, true); assert.equal(h.doc.activeElement, items[0]);
  h.move({ key: 'ArrowDown', preventDefault() {} }); assert.equal(h.doc.activeElement, items[1]);
  h.move({ key: 'ArrowDown', preventDefault() {} }); assert.equal(h.doc.activeElement, items[0]);
  h.move({ key: 'End', preventDefault() {} }); assert.equal(h.doc.activeElement, items[1]);
});
