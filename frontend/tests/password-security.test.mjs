import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
const source = readFileSync(new URL('../src/PasswordSecurity.vue', import.meta.url), 'utf8');
function harness(authenticated = false, post = async () => ({ data: { message: '已完成' } })) {
  const calls = [], emitted = [];
  const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*;\n/gm, '');
  const api = { post: async (...args) => { calls.push(args); return post(...args); } };
  const apiState = new Function('defineProps', 'defineEmits', 'ref', 'reactive', 'axios', 'localStorage',
    `${script};return {show,submit,sendCode,form,open,busy,error,notice};`)(
    () => ({ authenticated }), () => (...args) => emitted.push(args), value => ({ value }),
    value => value, api, { getItem: () => 'current-session' });
  return { ...apiState, calls, emitted };
}
test('component compiles and both entry points share the platform modal, not backdrop dismissal', () => {
  const { descriptor } = parse(source);
  const compiled = compileScript(descriptor, { id: 'password-security' });
  const template = compileTemplate({ source: descriptor.template.content, filename: 'PasswordSecurity.vue', id: 'password-security', compilerOptions: { bindingMetadata: compiled.bindings } });
  assert.deepEqual(template.errors, []);
  assert.ok(source.includes('modal-card password-security-dialog'));
  assert.ok(!source.includes('@click.self'));
  const consoleSource = readFileSync(new URL('../src/ConsoleApp.vue', import.meta.url), 'utf8');
  assert.ok(consoleSource.includes('<PasswordSecurity v-if="!registerMode" />'));
  assert.ok(consoleSource.includes('<PasswordSecurity authenticated @changed="logout();'));
});
test('change submits only old/new password with current login and clears secrets', async () => {
  const h = harness(true); h.show();
  Object.assign(h.form, { current_password: 'Old12345', new_password: 'New12345', confirmation: 'New12345' });
  await h.submit();
  assert.deepEqual(h.calls, [['/api/auth/change-password', { current_password: 'Old12345', new_password: 'New12345' }, { headers: { Authorization: 'Bearer current-session' } }]]);
  assert.deepEqual(h.emitted, [['changed']]); assert.equal(h.form.new_password, ''); assert.equal(h.open.value, false);
});
test('recovery requests a real email code and confirms without an authenticated session', async () => {
  const h = harness(); h.show(); h.form.identifier = 'example@company.test';
  await h.sendCode();
  Object.assign(h.form, { code: '392619', new_password: 'New12345', confirmation: 'New12345' });
  await h.submit();
  assert.equal(h.calls[0][0], '/api/auth/password-reset/request');
  assert.deepEqual(h.calls[1], ['/api/auth/password-reset/confirm', { identifier: 'example@company.test', code: '392619', new_password: 'New12345' }, { headers: {} }]);
  assert.equal(h.emitted.length, 0); assert.equal(h.notice.value, '已完成');
});
test('mismatch, weak password and repeated submission do not issue requests; failure preserves input', async () => {
  const h = harness(true, async () => { throw { response: { data: { detail: '原密码不正确' } } }; });
  Object.assign(h.form, { new_password: 'New12345', confirmation: 'Different123' }); await h.submit();
  assert.equal(h.calls.length, 0);
  h.form.confirmation = h.form.new_password; await h.submit();
  assert.equal(h.error.value, '原密码不正确'); assert.equal(h.form.new_password, 'New12345'); assert.equal(h.busy.value, false);
  h.busy.value = true; await h.submit(); await h.sendCode(); assert.equal(h.calls.length, 1);
});
