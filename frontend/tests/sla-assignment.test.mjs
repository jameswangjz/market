import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { ref, reactive } from 'vue';
import { bindingPayload } from '../src/slaManagement.js';

const source = readFileSync(new URL('../src/SlaBindingsPanel.vue', import.meta.url), 'utf8');
const assignment = source.slice(source.indexOf('async function save()'), source.indexOf('watch(()'));
function harness(role = 'super_admin') {
  const calls = [], refreshes = [];
  const state = vm.createContext({
    allowed: ref(['super_admin', 'platform_operator'].includes(role)), saving: ref(false),
    form: reactive({ enterprise_id: 'enterprise', service_level_id: 'enterprise-level', expires_at: '', reason: '  企业升级支持权益  ' }),
    bindingPayload, error: ref(''), notice: ref(''), editing: ref({}), disposed: false,
    props: { api: { post: async (...args) => { calls.push(args); } } },
    emit: event => refreshes.push(event), load: async () => {},
  });
  vm.runInContext(assignment, state);
  return { state, calls, refreshes };
}
test('SLA binding validates enterprise, level and bounded reason', async () => {
  for (const changes of [{ enterprise_id: '' }, { service_level_id: '' }, { reason: '' }, { reason: ' \n\t ' }, { reason: 'x'.repeat(2001) }]) {
    const h = harness(); Object.assign(h.state.form, changes); await h.state.save();
    assert.equal(h.calls.length, 0); assert.ok(h.state.error.value); assert.equal(h.state.saving.value, false);
  }
});
test('both platform managers send trimmed reason with enterprise-wide binding', async () => {
  for (const role of ['super_admin', 'platform_operator']) {
    const h = harness(role); await h.state.save();
    assert.deepEqual(h.calls[0], ['/sla/service-level-assignments', { enterprise_id: 'enterprise', service_level_id: 'enterprise-level', user_id: '', expires_at: null, reason: '企业升级支持权益' }]);
    assert.deepEqual(h.refreshes, ['changed']); assert.equal(h.state.saving.value, false);
  }
});
test('non-managers cannot assign and duplicate requests are single-flight', async () => {
  for (const role of ['finance_settlement', 'delivery_monitor', 'security_compliance', 'quality_reviewer', 'product_manager', '']) {
    const h = harness(role); await h.state.save(); assert.equal(h.calls.length, 0);
  }
  const h = harness(); let reject, requests = 0;
  h.state.props.api.post = () => { requests++; return new Promise((_yes, no) => { reject = no; }); };
  const before = JSON.stringify(h.state.form), first = h.state.save();
  await h.state.save(); assert.equal(requests, 1);
  reject({ response: { data: { detail: '模拟失败' } } }); await first;
  assert.equal(h.state.saving.value, false); assert.equal(JSON.stringify(h.state.form), before);
  assert.equal(h.state.error.value, '模拟失败'); assert.deepEqual(h.refreshes, []);
});
test('expiry uses timezone-aware ISO and rejects expired or malformed values', () => {
  const input = { enterprise_id: 'e', service_level_id: 'custom', reason: '升级', expires_at: '2099-01-01T12:30' };
  assert.equal(bindingPayload(input).expires_at, new Date(input.expires_at).toISOString());
  for (const expires_at of ['invalid', '2000-01-01T00:00']) assert.throws(() => bindingPayload({ ...input, expires_at }), /到期时间/);
});
test('binding form requires bounded reason and historic profiles have no write surface', () => {
  assert.match(source, /v-model="form.reason"[^>]*maxlength="2000"[^>]*required/);
  const management = readFileSync(new URL('../src/SlaManagement.vue', import.meta.url), 'utf8');
  const consoleSource = readFileSync(new URL('../src/ConsoleApp.vue', import.meta.url), 'utf8');
  assert.ok(management.includes('历史规则（只读）'));
  assert.doesNotMatch(management + consoleSource, /createSlaProfile|toggleSlaProfile|\/sla\/profiles/);
});
