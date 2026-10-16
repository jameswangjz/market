import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import { ref, reactive, computed } from 'vue';
import { matrixPayload, customLevelPayload, activeMatrixLevels, metricNames, matrixMetadata, metadataFields, sortedMatrices } from '../src/slaManagement.js';

const read = name => readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8');
const matrix = { service_level_id: 'standard-id', code: 'standard', version: 4, metrics: { delivery: { target: 4, warning: 3.2, unit: 'minutes', direction: 'upper', responsibility: 'delivery' }, availability: { target: 90, warning: 92, unit: 'percent', direction: 'lower' } } };
test('matrix writes only changed target/warning values with optimistic version', () => {
  const payload = matrixPayload(matrix, { delivery: { target: '5', warning: '3.2', unit: 'changed' }, availability: { target: 90, warning: 92 } });
  assert.deepEqual(payload, { expected_version: 4, metrics: { delivery: { target: 5, warning: 3.2 } } });
  assert.equal(matrix.metrics.delivery.target, 4);
});
test('minute targets are positive integers while warnings and percentages support decimals', () => {
  const draft = { delivery: { target: 5, warning: 3.2 }, availability: { target: 90.5, warning: 92.5 } };
  assert.doesNotThrow(() => matrixPayload(matrix, draft));
  for (const target of ['', 0, -1, 0.5, 4.5, Infinity, 'invalid']) assert.throws(() => matrixPayload(matrix, { ...draft, delivery: { target, warning: 0.1 } }));
  for (const edit of [{ target: 4, warning: 5 }, { target: 4, warning: 0 }, { target: 4, warning: '' }]) assert.throws(() => matrixPayload(matrix, { ...draft, delivery: edit }));
  for (const edit of [{ target: 101, warning: 102 }, { target: 90, warning: 89 }]) assert.throws(() => matrixPayload(matrix, { ...draft, availability: edit }));
  const ms = { version: 0, metrics: { latency: { target: 1, warning: 0.5, unit: 'milliseconds', direction: 'upper' } } };
  assert.deepEqual(matrixPayload(ms, { latency: { target: 1.5, warning: 0.75 } }).metrics.latency, { target: 1.5, warning: 0.75 });
});
test('copy requests use a built-in base and never copy read-only fields into the POST', () => {
  assert.deepEqual(customLevelPayload({ code: ' custom ', name: '自定义', base_code: 'enterprise', description: ' 说明 ', calendar: {}, metrics: {} }), { code: 'custom', name: '自定义', base_code: 'enterprise', description: '说明' });
  for (const code of ['standard', 'a', 'UPPER', 'bad-code']) assert.throws(() => customLevelPayload({ code, name: '级别', base_code: 'standard', description: '' }));
});
test('approval levels include active complete custom matrices and exclude incomplete or disabled levels', () => {
  const metrics = Object.fromEntries(Object.keys(metricNames).map(key => [key, { target: 1, warning: 0.8 }]));
  const levels = activeMatrixLevels([{ service_level_id: 'custom', code: 'custom', status: 'active', metrics }, { service_level_id: 'bad', code: 'bad', status: 'active', metrics: {} }, { service_level_id: 'off', code: 'off', status: 'disabled', metrics }, { service_level_id: 'std', code: 'standard', status: 'active', metrics }]);
  assert.deepEqual(levels.map(item => item.id), ['std', 'custom']);
  assert.deepEqual(sortedMatrices([{ code: 'strategic' }, { code: 'custom' }, { code: 'standard' }, { code: 'enterprise' }]).map(item => item.code), ['standard', 'enterprise', 'strategic', 'custom']);
});
test('CSV metadata retains every read-only column and excludes only editable values', () => {
  const row = { metric_key: 'delivery', '指标名称': '交付', '字段允许范围': '正整数', '已确认基准': '4', '问题等级': '全部', '开始事件': '支付', '结束事件': '交付', '填写统计周期': '自然月', '填写超时升级对象': '运营', '备注': '<script>escaped</script>', '填写目标值': '4', '填写预警阈值': '3.2' };
  assert.deepEqual(matrixMetadata({ display_metadata: { rows: [row] } }, 'delivery'), row);
  assert.equal(metadataFields(row).length, 9);
  assert.ok(metadataFields(row).every(([key]) => !['metric_key', '填写目标值', '填写预警阈值'].includes(key)));
  const source = read('SlaMatrixPanel.vue');
  assert.ok(source.includes('supportRows')); assert.ok(source.includes('metadataFields(row)')); assert.ok(source.includes('metadataFields(matrixMetadata(matrix, key))'));
});
function saveHarness() {
  const source = read('SlaMatrixPanel.vue'), start = source.indexOf('async function save()'), end = source.indexOf('async function history()');
  const calls = [], events = [], state = vm.createContext({ allowed: ref(true), saving: ref(false), busy: ref(false), matrix: ref(matrix), draft: reactive({ delivery: { target: 5, warning: 3.2 }, availability: { target: 90, warning: 92 } }), matrixPayload, error: ref(''), notice: ref(''), selected: ref('standard-id'), generation: 1, disposed: false, matrices: ref([matrix]), props: { api: { patch: async (...args) => { calls.push(args); return { data: { item: { ...matrix, version: 5 } } }; } } }, select: () => {}, emit: event => events.push(event) });
  vm.runInContext(source.slice(start, end), state);
  return { state, calls, events };
}
test('matrix save is single-flight, preserves input on conflict and refuses unauthorized writes', async () => {
  const h = saveHarness(); let reject;
  h.state.props.api.patch = (...args) => { h.calls.push(args); return new Promise((_resolve, fail) => { reject = fail; }); };
  const request = h.state.save(); await h.state.save(); assert.equal(h.calls.length, 1);
  reject({ response: { status: 409 } }); await request;
  assert.match(h.state.error.value, /当前输入已保留/); assert.equal(h.state.draft.delivery.target, 5); assert.equal(h.state.saving.value, false); assert.deepEqual(h.events, []);
  h.state.allowed.value = false; await h.state.save(); assert.equal(h.calls.length, 1);
});
test('identity lists share server pagination and tabs show server totals', () => {
  const source = read('ConsoleApp.vue');
  assert.ok(source.includes('count: identityLists[tab.key].total'));
  assert.ok(source.includes('fetchListPage(api, key === "users" ? "/users" : "/admin/enterprises"'));
  assert.ok(source.includes('label="用户分页"')); assert.ok(source.includes('label="企业分页"'));
  assert.ok(source.includes('identityReviewLevels.value = activeMatrixLevels(response.data.items)'));
  assert.ok(source.includes('{ key: "enterprise_name", label: "企业名称" }'));
  assert.ok(source.includes('{ value: "expired", label: "邀请过期" }'));
});
