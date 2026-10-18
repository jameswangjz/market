import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import { parse } from '@vue/compiler-sfc';
import { ref, reactive, computed } from 'vue';
import * as management from '../src/settlementManagement.js';
import * as finance from '../src/settlementFinance.js';
import { fundedBasis, ordinaryBasis, refund, provider, enterprise } from './fixtures/funded-cost-basis.mjs';
const source = readFileSync(new URL('../src/SettlementManagement.vue', import.meta.url), 'utf8');
function form(sources) {
  return { refund_id: refund.id, bearer_participant_id: 'expert', reason: 'Invoice evidence',
    source_allocations: sources.map((s, i) => ({ segment_id: s.segment_id, baseline_id: s.baseline_id,
      retained_cost: i ? '0.00' : '0.01', evidence_refs: `invoice:${i}` })) };
}
function runtime(api) {
  const state = vm.createContext({ computed, ref, reactive, watch: () => {}, onBeforeUnmount: () => {},
    defineEmits: () => () => {}, defineProps: () => ({ api, user: provider, enterprise, businessView: 'provider' }),
    ...finance, ...management });
  vm.runInContext(parse(source).descriptor.scriptSetup.content.replace(/^import .*;\n/gm, '') +
    '\n globalThis.editor={chooseRefund,refunds,exception,costSources,sourcesReady,error,submit,optionsBusy};', state);
  state.editor.refunds.value = [refund]; state.editor.exception.refund_id = refund.id;
  return state.editor;
}
for (const count of [1, 2]) test(`funded ${count} sources preserve exact identities and submit every source`, () => {
  const sources = management.costExceptionSources(refund, [], fundedBasis(count));
  const payload = management.costExceptionPayload(form(sources), sources);
  assert.equal(payload.basis_kind, 'funded'); assert.equal(payload.schema_version, 2);
  assert.equal(payload.retained_cost, '0.01'); assert.equal(payload.source_allocations.length, count);
  assert.equal(sources[0].max_retained_cost, '20.00'); assert.equal(sources[0].settlement_no, 'SET-0');
  const missing = form(sources); missing.source_allocations.pop();
  assert.throws(() => management.costExceptionPayload(missing, sources));
  const evidence = form(sources); evidence.source_allocations.at(-1).evidence_refs = '';
  assert.throws(() => management.costExceptionPayload(evidence, sources));
  const over = form(sources); over.source_allocations[0].retained_cost = '20.01';
  assert.throws(() => management.costExceptionPayload(over, sources));
});
test('discovery denies forbidden, pending, adjusted, cross-order and malformed origins without report fallback', () => {
  const fakeReport = [{ refund_id: refund.id, order_id: refund.order_id, economic_segments: fundedBasis(2).references }];
  for (const patch of [{ status: 'waiting' }, { proposal_allowed: false }, { proposal_allowed: 'true' },
    { basis_origin: 'applied_funded_basis' }, { basis_origin: '' }, { kind: 'multi_source_refund' },
    { order_id: 'other' }, { refund_id: 'other' }, { references: [] }]) {
    assert.throws(() => management.costExceptionSources(refund, fakeReport, { ...fundedBasis(), ...patch }));
  }
  for (const value of [null, undefined, {}]) assert.throws(() => management.costExceptionSources(refund, fakeReport, value));
  for (const patch of [{ source_order_id: 'other' }, { baseline_hash: '' }, { currency: 'USD' }, { cost_amount: '20.00' }]) {
    const basis = fundedBasis(); Object.assign(basis.references[0], patch);
    assert.throws(() => management.costExceptionSources(refund, [], basis));
  }
  const duplicate = fundedBasis(2); duplicate.references[1] = duplicate.references[0];
  assert.throws(() => management.costExceptionSources(refund, [], duplicate));
});
test('ordinary single and multi preserve their existing wire formats', () => {
  for (const count of [1, 2]) {
    const sources = management.costExceptionSources(refund, [], ordinaryBasis(count));
    const payload = management.costExceptionPayload(form(sources), sources);
    assert.equal(payload.basis_kind, undefined);
    assert.equal(payload.schema_version, count === 1 ? undefined : 2);
    assert.equal(payload.source_allocations?.length, count === 1 ? undefined : 2);
    assert.equal(payload.retained_cost, '0.01');
  }
});
test('actual SFC reads only scoped discovery and never assessment or report for the ordinary ceiling', async () => {
  const reads = [], writes = [];
  const editor = runtime({ get: async (path, options) => {
    reads.push({ path, options }); return { data: path.endsWith('/basis') ? fundedBasis() : { items: [] } };
  }, post: async (path, payload) => { writes.push({ path, payload }); return { data: {} }; } });
  await editor.chooseRefund(); assert.equal(editor.sourcesReady.value, true);
  assert.equal(reads[0].path, '/settlement-cost-exceptions/basis');
  assert.equal(reads[0].options.params.refund_id, refund.id);
  assert.equal(reads[0].options.params.enterprise_id, enterprise.id);
  Object.assign(editor.exception, form(editor.costSources.value)); await editor.submit('exception');
  assert.equal(writes.length, 1); assert.equal(writes[0].payload.basis_kind, 'funded');
  assert.ok(!reads.some(r => /report|settlements\//.test(r.path)));
});
test('actual SFC cannot submit after denied discovery even with manually filled legacy inputs', async () => {
  let writes = 0;
  for (const data of [null, { ...fundedBasis(), status: 'waiting' }, { ...fundedBasis(), proposal_allowed: false },
    { ...fundedBasis(), basis_origin: 'applied_funded_basis' }]) {
    const editor = runtime({ get: async () => ({ data }), post: async () => { writes++; return { data: {} }; } });
    await editor.chooseRefund(); assert.equal(editor.sourcesReady.value, false);
    Object.assign(editor.exception, { bearer_participant_id: 'expert', retained_cost: '1.00', reason: 'Invoice', evidence_refs: 'invoice' });
    await editor.submit('exception');
  }
  assert.equal(writes, 0);
});
test('stale discovery cannot replace another refund or leave submission enabled', async () => {
  let resolve;
  const editor = runtime({ get: () => new Promise(done => { resolve = done; }) });
  const pending = editor.chooseRefund(); editor.exception.refund_id = ''; await editor.chooseRefund();
  resolve({ data: fundedBasis() }); await pending;
  assert.equal(editor.sourcesReady.value, false); assert.equal(editor.costSources.value.length, 0);
});
