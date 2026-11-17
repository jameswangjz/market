import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
const source = readFileSync(new URL('../src/RedisMaintenance.vue', import.meta.url), 'utf8');
function harness({ get = async () => ({ data: { snapshot: null, tasks: [], active_task: null } }), post = async () => ({ data: { id: 'own', status: 'queued' } }) } = {}) {
  const calls = [], intervals = new Map(); let mount, unmount, counter = 0;
  const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*;\n/gm, '');
  const state = new Function('ref', 'computed', 'onMounted', 'onUnmounted', 'axios', 'localStorage', 'crypto', 'setInterval', 'clearInterval', `const RefreshCw={}, Archive={}, ArchiveRestore={}, ShieldAlert={}; ${script}; return {snapshot,tasks,activeTask,ownTask,error,reason,acceptGap,dialog,pending,fresh,allowed,request,confirm,load,safeMaintenance};`)(
    value => ({ value }), fn => ({ get value() { return fn(); } }), fn => { mount = fn; }, fn => { unmount = fn; },
    { get: async (...args) => { calls.push(['get', ...args]); return get(...args); }, post: async (...args) => { calls.push(['post', ...args]); return post(...args); } },
    { getItem: () => 'token' }, { randomUUID: () => `key-${++counter}` }, (fn, ms) => { assert.equal(ms, 3000); intervals.set(1, fn); return 1; }, id => intervals.delete(id));
  return { ...state, calls, intervals, mount: () => mount(), unmount: () => unmount() };
}
test('component compiles and settings tab and component share super_admin guard', () => {
  const { descriptor } = parse(source), compiled = compileScript(descriptor, { id: 'redis' });
  assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: 'RedisMaintenance.vue', id: 'redis', compilerOptions: { bindingMetadata: compiled.bindings } }).errors, []);
  const console = readFileSync(new URL('../src/ConsoleApp.vue', import.meta.url), 'utf8');
  assert.match(console, /v-if="user.platform_role === 'super_admin'"[^\n]*系统维护/);
  assert.match(console, /systemSettingsTab === 'maintenance' && user.platform_role === 'super_admin'"><RedisMaintenance/);
  assert.match(source, /<ActionDialog/);
});
test('mount only reads cache; no idle polling; a paused collector does not prevent resume/recovery', async () => {
  const h = harness(); await h.mount(); assert.equal(h.calls.length, 1); assert.equal(h.calls[0][0], 'get'); assert.equal(h.intervals.size, 0);
  for (const [action, status] of Object.entries({ prepare: 'active_source_healthy', resume: 'safe_sealed_resume_candidate', recover: 'explicit_gap_required' })) {
    assert.equal(h.allowed(action), false); h.snapshot.value = { status, captured_at: new Date().toISOString(), worker: { ready: true } }; assert.equal(h.allowed(action), true);
    h.snapshot.value.worker = { ready: false }; assert.equal(h.allowed(action), action !== 'prepare');
    h.snapshot.value.captured_at = '2000-01-01T00:00:00Z'; assert.equal(h.allowed(action), false); h.snapshot.value = null;
  }
  h.activeTask.value = { status: 'running' }; assert.equal(h.allowed('inspect'), false);
});
test('mutations require reason, gap acknowledgement and modal; retry preserves key and reason', async () => {
  const h = harness({ post: async () => { throw Error('network'); } });
  h.fresh.value = true; h.snapshot.value = { status: 'explicit_gap_required', captured_at: new Date().toISOString() };
  h.request('recover'); assert.equal(h.dialog.value, null); assert.equal(h.calls.length, 0);
  h.reason.value = '异常恢复审批'; h.request('recover'); assert.equal(h.dialog.value, null);
  h.acceptGap.value = true; h.request('recover'); assert.equal(h.calls.length, 0); assert.ok(h.dialog.value);
  await h.confirm(); const first = h.calls[0][2]; assert.equal(first.accept_gap, true);
  assert.equal(h.reason.value, '异常恢复审批'); h.request('recover'); await h.confirm(); assert.deepEqual(h.calls[1][2], first);
  h.reason.value = '另一审批'; h.request('recover'); await h.confirm(); assert.notEqual(h.calls[2][2].idempotency_key, first.idempotency_key);
});
test('inspect polls only GET until task completes, and unmount clears active polling', async () => {
  let complete = false;
  const h = harness({ get: async () => ({ data: { snapshot: null, active_task: null, tasks: complete ? [{ id: 'own', status: 'succeeded' }] : [] } }) });
  await h.mount(); h.request('inspect'); await new Promise(resolve => setImmediate(resolve)); assert.equal(h.intervals.size, 1);
  await h.intervals.get(1)(); assert.equal(h.calls.filter(c => c[0] === 'post').length, 1);
  complete = true; await h.load(); assert.equal(h.intervals.size, 0);
  complete = false; h.ownTask.value = { id: 'own', status: 'running' }; await h.load(); assert.equal(h.intervals.size, 1);
  h.unmount(); assert.equal(h.intervals.size, 0);
});
test('safe note requires successful prepare and sealed snapshot; read failure invalidates mutation gates', async () => {
  const h = harness({ get: async () => { throw Error('offline'); } });
  h.fresh.value = true; h.snapshot.value = { status: 'safe_sealed_resume_candidate', captured_at: new Date().toISOString() };
  assert.equal(h.safeMaintenance.value, false); h.tasks.value = [{ id: 1, action: 'prepare', status: 'failed' }]; assert.equal(h.safeMaintenance.value, false);
  h.tasks.value[0].status = 'succeeded'; assert.equal(h.safeMaintenance.value, true);
  await h.load(); assert.equal(h.allowed('resume'), false); assert.equal(h.safeMaintenance.value, false);
});
