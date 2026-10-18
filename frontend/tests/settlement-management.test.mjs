import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { parse, compileScript, compileTemplate, compileStyle } from '@vue/compiler-sfc';
import { computed, ref, reactive } from 'vue';
import * as management from '../src/settlementManagement.js';
import * as finance from '../src/settlementFinance.js';
const enterprise = { id: 'provider_tenant', verification_status: 'verified' };
const provider = { id: 'provider', verified_status: 'verified', enterprise_memberships: [{ enterprise_id: enterprise.id, role: 'enterprise_admin', status: 'active', business_roles: ['provider'] }] };
const operator = { id: 'ops', platform_role: 'platform_operator' };
const source = readFileSync(new URL('../src/SettlementManagement.vue', import.meta.url), 'utf8');
// Execute actual nested serializers rather than assuming ideal frontend DTOs.
const dto = JSON.parse(execFileSync('python3', ['-c', `
import ast,json,sys
from types import SimpleNamespace as N
from datetime import timezone
tree=ast.parse(open(sys.argv[1]).read())
install=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='install')
nodes=[n for n in install.body if isinstance(n,ast.FunctionDef) and n.name in ('participant_dict','exception_dict')]
ns=dict(json=json,timezone=timezone,Decision=object())
exec(compile(ast.Module(body=nodes,type_ignores=[]),'serializers','exec'),ns)
p=N(id='party',name='Real fixture expert',enterprise_id='provider_tenant',user_id='',kind='expert',currency='CNY',creator_id='provider',owner_enterprise_id='provider_tenant')
e=N(id='exception',payload_json=json.dumps(dict(retained_cost='20.00',currency='CNY',reason='Invoice',evidence_refs=['invoice:1'],bearer_participant_id=p.id,bearer_participant_name=p.name)),refund_id='refund',order_id='order',baseline_id='baseline',creator_id='provider',creator_side='provider')
single=ns['exception_dict'](N(get=lambda *a:None),e)
allocations=[dict(segment_id='segment-'+str(i),baseline_id='baseline-'+str(i),retained_cost=value,evidence_refs=['invoice:'+str(i)],baseline_hash='hash-'+str(i),basis=dict(segment_id='segment-'+str(i),reference_settlement_id='baseline-'+str(i),baseline_hash='hash-'+str(i),source_order_id='order',cost_amount='-20.00')) for i,value in [(1,'0.01'),(2,'0.02')]]
e.payload_json=json.dumps(dict(schema_version=2,source_allocations=allocations,retained_cost='0.03',currency='CNY',reason='Two invoices',evidence_refs=['invoice:1','invoice:2'],bearer_participant_id=p.id,bearer_participant_name=p.name))
print(json.dumps(dict(party=ns['participant_dict'](p),exception=single,multi=ns['exception_dict'](N(get=lambda *a:None),e))))
`, new URL('../../backend/app/settlement_parties.py', import.meta.url).pathname], { encoding: 'utf8' }));

test('management matches actual operator and verified provider scope, not beneficiary or read-only roles', () => {
  assert.equal(management.managementSide(operator, null, 'platform'), 'platform');
  assert.equal(management.managementSide(provider, enterprise, 'provider'), 'provider');
  for (const role of ['security_compliance', 'auditor', 'expert', 'channel']) assert.equal(management.managementSide({ ...operator, platform_role: role }, enterprise, 'provider'), '');
  assert.equal(management.managementSide(provider, enterprise, 'buyer'), '');
  assert.equal(management.managementSide(operator, enterprise, 'beneficiary'), '');
  assert.equal(management.managementSide({ ...provider, is_active: false }, enterprise, 'provider'), '');
  assert.equal(management.managementSide(provider, { ...enterprise, verification_status: 'pending' }, 'provider'), '');
  for (const m of [{ role: 'member' }, { business_roles: ['buyer'] }, { status: 'inactive' }]) {
    assert.equal(management.managementSide({ ...provider, enterprise_memberships: [{ ...provider.enterprise_memberships[0], ...m }] }, enterprise, 'provider'), '');
  }
});
test('cost exception decisions require opposite side, independent creator, pending actual version', () => {
  const row = dto.exception;
  assert.equal(management.canDecideCostException(row, operator, 'platform'), true);
  assert.equal(management.canDecideCostException(row, provider, 'provider'), false);
  assert.equal(management.canDecideCostException(row, { id: 'other-provider' }, 'provider'), false);
  assert.equal(management.canDecideCostException(row, { id: row.creator_id }, 'platform'), false);
  for (const patch of [{ version: 2 }, { status: 'approved' }, { creator_side: '' }, { creator_id: '' }]) assert.equal(management.canDecideCostException({ ...row, ...patch }, operator, 'platform'), false);
});
test('per-category allocations are exact decimals, preserve real kind, and reject duplicate/foreign subjects', () => {
  const parties = [dto.party, { id: 'second', kind: 'expert' }, { id: 'provider', kind: 'provider' }];
  const rows = [{ participant_id: 'party', weight: '33.33333333' }, { participant_id: 'second', weight: '66.66666667' }, { participant_id: 'provider', weight: '100' }];
  assert.equal(management.allocationPayload(rows, parties)[0].participant_type, 'expert');
  assert.equal(management.allocationPayload(rows, parties)[0].weight, '33.33333333');
  assert.deepEqual(management.allocationPayload([], parties), []);
  for (const invalid of [[rows[0]], [rows[0], rows[0]], [{ participant_id: 'foreign', weight: '100' }], [{ participant_id: 'party', weight: '1e2' }], [{ participant_id: 'party', weight: '0' }]]) assert.throws(() => management.allocationPayload(invalid, parties));
});
test('authorized product configuration contributes shared candidates without exposing unrelated subjects', () => {
  const config = { allocations: [{ participant_id: 'global-expert', participant_type: 'expert', participant_name: 'Shared authorized expert', currency: 'CNY', weight: '100' }] };
  const choices = management.allocationCandidates([dto.party], config);
  assert.equal(choices.length, 2); assert.equal(choices[1].configuredOnly, true);
  assert.equal(choices[1].kind, 'expert'); assert.equal(choices[1].name, 'Shared authorized expert');
  assert.equal(management.allocationPayload(config.allocations, choices)[0].participant_id, 'global-expert');
  assert.throws(() => management.allocationPayload([{ participant_id: 'unrelated', weight: '100' }], choices));
  assert.equal(management.allocationCandidates([dto.party], null).length, 1);
});
test('directories never use forbidden finance role product/refund APIs', () => {
  assert.equal(management.managementDirectory({ platform_role: 'finance_settlement' }, 'products', 'name', 2).path, '/settlement-reporting/report');
  const refunds = management.managementDirectory({ platform_role: 'finance_settlement' }, 'refunds', 'order', 1);
  assert.equal(refunds.params.source_kind, 'refund');
  assert.equal(management.managementDirectory(operator, 'products', 'Test', 1).path, '/products');
  assert.equal(management.managementDirectory(provider, 'refunds', 'REF', 1).params.refund_id, 'REF');
});
test('cost exception payload requires positive cents, actual refund/bearer/reason/evidence fields', () => {
  const form = { refund_id: ' refund ', retained_cost: '20.00', bearer_participant_id: dto.party.id, reason: ' Invoice ', evidence_refs: 'invoice:1\n\n receipt:2' };
  assert.deepEqual(management.costExceptionPayload(form), { refund_id: 'refund', retained_cost: '20.00', bearer_participant_id: dto.party.id, reason: 'Invoice', evidence_refs: ['invoice:1', 'receipt:2'] });
  for (const change of [{ retained_cost: '0' }, { retained_cost: '1.001' }, { evidence_refs: '' }, { reason: '' }, { refund_id: '' }]) assert.throws(() => management.costExceptionPayload({ ...form, ...change }));
});
function multiReport() {
  return [...dto.multi.references.map((ref, i) => ({ id: ref.reference_settlement_id, order_id: 'order',
    order_no: 'ORD-MULTI', settlement_no: `SET-SOURCE-${i + 1}`, batch_no: `BATCH-SOURCE-${i + 1}`,
    product_version_name: 'Original order version must not impersonate a source version', baseline_hash: ref.baseline_hash })),
  { id: 'negative', order_id: 'order', refund_id: 'refund', economic_segments: dto.multi.references }];
}
const refundChoice = { id: 'refund', order_id: 'order', order_no: 'ORD-MULTI', amount: '50.00' };
test('multi-cost sources come from actual immutable reference DTOs and show business names without guessing source version', () => {
  const sources = management.costExceptionSources(refundChoice, multiReport());
  assert.equal(sources.length, 2); assert.equal(sources[0].max_retained_cost, '20.00');
  assert.equal(sources[0].order_no, 'ORD-MULTI'); assert.equal(sources[0].settlement_no, 'SET-SOURCE-1'); assert.equal(sources[1].batch_no, 'BATCH-SOURCE-2');
  assert.equal(sources[0].version_name, '');
  assert.equal(management.costSourceLabel({ ...dto.multi.references[0], source_version_name: 'v2' }, multiReport()).version_name, 'v2');
  const wrong = multiReport(); wrong.at(-1).economic_segments = [{ ...dto.multi.references[0], source_order_id: 'foreign' }, dto.multi.references[1]];
  assert.throws(() => management.costExceptionSources(refundChoice, wrong));
  const duplicate = multiReport(); duplicate.at(-1).economic_segments = [dto.multi.references[0], dto.multi.references[0]];
  assert.throws(() => management.costExceptionSources(refundChoice, duplicate));
});
test('multi-cost request includes every source, exact BigInt total and separate mandatory evidence, including zero amounts', () => {
  const sources = management.costExceptionSources(refundChoice, multiReport());
  const form = { refund_id: 'refund', bearer_participant_id: dto.party.id, reason: 'Invoice evidence', retained_cost: '999.00',
    source_allocations: sources.map((s, i) => ({ ...s, retained_cost: i ? '0.20' : '0.10', evidence_refs: `invoice:${i}\nshared:1` })) };
  const result = management.costExceptionPayload(form, sources);
  assert.equal(result.retained_cost, '0.30'); assert.equal(result.schema_version, 2); assert.equal(result.source_allocations.length, 2);
  assert.deepEqual(result.evidence_refs, ['invoice:0', 'invoice:1', 'shared:1']);
  assert.deepEqual(Object.keys(result.source_allocations[0]).sort(), ['baseline_id', 'evidence_refs', 'retained_cost', 'segment_id']);
  const zero = { ...form, source_allocations: form.source_allocations.map((r, i) => ({ ...r, retained_cost: i ? '0.01' : '0.00' })) };
  assert.equal(management.costExceptionPayload(zero, sources).retained_cost, '0.01');
  for (const change of [
    { source_allocations: form.source_allocations.slice(1) },
    { source_allocations: [form.source_allocations[0], form.source_allocations[0]] },
    { source_allocations: form.source_allocations.map(r => ({ ...r, retained_cost: '0' })) },
    { source_allocations: form.source_allocations.map(r => ({ ...r, retained_cost: '20.01' })) },
    { source_allocations: form.source_allocations.map(r => ({ ...r, retained_cost: '0.001' })) },
    { source_allocations: form.source_allocations.map(r => ({ ...r, evidence_refs: '' })) },
    { source_allocations: form.source_allocations.map(r => ({ ...r, baseline_id: 'foreign' })) },
  ]) assert.throws(() => management.costExceptionPayload({ ...form, ...change }, sources));
  assert.throws(() => management.costExceptionPayload(form));
});
test('new multi-cost business failures are understandable and unknown codes remain safe to trace', () => {
  for (const code of ['all_source_allocations_required', 'retained_cost_total_mismatch', 'source_evidence_refs_required', 'multi_refund_basis_changed']) assert.match(management.costExceptionError({ response: { data: { detail: code } } }), /[\u3400-\u9fff]/);
  assert.match(management.costExceptionError({ response: { data: { detail: 'future_multi_policy' } } }), /future_multi_policy/);
  assert.equal(management.costExceptionError({ response: { data: { detail: 'SELECT secret FROM accounts' } } }), '操作失败，请稍后重试');
});
test('annual finalization requires immutable digest, draft, writable and independent actor', () => {
  const row = { status: 'draft', digest: 'a'.repeat(64), actor_id: 'maker' };
  assert.equal(management.canFinalizeAnnual(row, operator, true), true);
  assert.equal(management.canFinalizeAnnual(row, { id: 'maker' }, true), false);
  assert.equal(management.canFinalizeAnnual(row, operator, false), false);
  assert.equal(management.canFinalizeAnnual({ ...row, status: 'formal' }, operator, true), false);
  assert.equal(management.canFinalizeAnnual({ ...row, digest: '' }, operator, true), false);
});
test('management session blocks duplicate writes, retains retry key and ignores context/unmount responses', async () => {
  let context = 'one', allow = true, resolve, reject; const calls = [], applied = [];
  const api = { post: (path, body, opts) => { calls.push({ path, body, opts }); return new Promise((yes, no) => { resolve = yes; reject = no; }); }, get: () => new Promise(yes => { resolve = yes; }) };
  const session = management.managementSession(api, () => context, () => ({ enterprise_id: context }), () => allow);
  const first = session.write('/settlement-parties', { name: 'one' }, d => applied.push(d));
  assert.equal(await session.write('/settlement-parties', { name: 'one' }, () => {}), false);
  reject(new Error('network')); await assert.rejects(first);
  const retry = session.write('/settlement-parties', { name: 'one' }, d => applied.push(d));
  assert.equal(calls[0].body.request_id, calls[1].body.request_id);
  context = 'two'; session.reset(); resolve({ data: 'old' }); await retry; assert.deepEqual(applied, []);
  const read = session.read('/settlement-parties', d => applied.push(d)); session.dispose(); resolve({ data: 'old' }); await read; assert.deepEqual(applied, []);
  allow = false; await session.write('/settlement-parties', {}, () => {}); assert.equal(calls.length, 2);
});
function runtime(user = operator, api) {
  const state = vm.createContext({ computed, ref, reactive, watch: () => {}, onBeforeUnmount: () => {}, defineEmits: () => () => {}, defineProps: () => ({ api, user, enterprise, businessView: user.platform_role ? 'platform' : 'provider' }), ...finance, ...management });
  const script = parse(source).descriptor.scriptSetup.content.replace(/^import .*;\n/gm, '');
  vm.runInContext(script + '\n globalThis.editor = { load, open, submit, review, loadConfiguration, party, exception, decision, reason, productId, configuration, allocations, parties, exceptions, error, side, page, pageSize, resize, visibleRows, tab, chooseRefund, refunds, selectedRefund, costSources, sourcesReady, optionsBusy, costTotal, showEvidence, shownSources, evidenceLoading };', state);
  return state.editor;
}
test('actual SFC workflows use real endpoint bodies and scoped approval metadata', async () => {
  const calls = [];
  const api = { get: async path => ({ data: path.includes('/allocations') ? { product_id: 'product', version: 1, allocations: [] } : { items: [dto.party] } }), post: async (path, body, options) => { calls.push({ path, body, options }); return { data: dto.party }; } };
  const editor = runtime(provider, api); await editor.load(); editor.open('party'); Object.assign(editor.party, { name: 'New expert', kind: 'expert', user_id: 'provider' }); await editor.submit('party');
  assert.equal(calls[0].path, '/settlement-parties'); assert.equal(calls[0].body.enterprise_id, enterprise.id); assert.equal(calls[0].body.currency, 'CNY'); assert.equal(calls[0].options.params.business_view, 'provider');
  editor.productId.value = 'product'; await editor.loadConfiguration(); editor.allocations.value = [{ participant_id: dto.party.id, weight: '100' }]; await editor.submit('allocations');
  assert.equal(calls[1].path, '/settlement-parties/products/product/allocations'); assert.equal(calls[1].body.allocations[0].participant_type, 'expert');
  const reviewer = runtime(operator, api); reviewer.review(dto.exception, 'approve'); reviewer.reason.value = 'Independent review'; await reviewer.submit('decision');
  assert.equal(calls[2].path, '/settlement-cost-exceptions/exception/decision'); assert.equal(calls[2].body.expected_version, 1); assert.equal(calls[2].body.decision, 'approve');
  editor.review(dto.exception, 'approve'); assert.equal(editor.decision.value, null);
});
test('actual management page size controls slicing and resets page', () => {
  const editor = runtime(operator, {}); editor.parties.value = Array.from({ length: 75 }, (_, i) => ({ id: i }));
  editor.page.value = 2; assert.equal(editor.visibleRows.value[0].id, 20);
  editor.resize(50); assert.equal(editor.page.value, 1); assert.equal(editor.pageSize.value, 50); assert.equal(editor.visibleRows.value.length, 50);
  editor.page.value = 2; assert.equal(editor.visibleRows.value.length, 25);
});
test('actual SFC selects server sources, submits one whole proposal and keeps review source evidence readonly', async () => {
  const writes = [], reads = [], rows = multiReport();
  const discovery = { refund_id: 'refund', order_id: 'order', status: 'ready', kind: 'multi_source_refund',
    basis_kind: 'multi', basis_origin: 'raw_multi_basis', proposal_allowed: true, reason: '',
    references: dto.multi.references.map((ref, i) => ({ ...ref, baseline_hash: String(i + 1).repeat(64), currency: 'CNY',
      order_no: rows[i].order_no, settlement_no: rows[i].settlement_no, batch_nos: [rows[i].batch_no] })) };
  const api = { get: async (path, options) => {
    reads.push({ path, options });
    assert.ok(!path.includes('/enterprise-settlements/') && !path.includes('/settlements/'), 'Applied assessment must not supply proposal cost caps');
    return { data: path === '/settlement-cost-exceptions/basis' ? discovery : { items: path.includes('/report') ? rows : [], total: rows.length } };
  }, post: async (path, payload) => { writes.push({ path, payload }); return { data: dto.multi }; } };
  const editor = runtime(provider, api); editor.refunds.value = [refundChoice]; editor.exception.refund_id = 'refund';
  await editor.chooseRefund(); assert.equal(editor.costSources.value.length, 2); assert.equal(editor.sourcesReady.value, true);
  assert.equal(reads.length, 1); assert.equal(reads[0].path, '/settlement-cost-exceptions/basis');
  assert.equal(reads[0].options.params.refund_id, 'refund'); assert.equal(reads[0].options.params.enterprise_id, enterprise.id);
  assert.equal(editor.costSources.value[0].settlement_no, 'SET-SOURCE-1');
  assert.equal(editor.costSources.value[1].batch_no, 'BATCH-SOURCE-2');
  Object.assign(editor.exception, { bearer_participant_id: dto.party.id, reason: 'Invoice evidence' });
  editor.exception.source_allocations.forEach((r, i) => { r.retained_cost = i ? '0.02' : '0.01'; r.evidence_refs = `invoice:${i}`; });
  assert.equal(editor.costTotal.value, '0.03'); await editor.submit('exception');
  assert.equal(writes.length, 1); assert.equal(writes[0].path, '/settlement-cost-exceptions'); assert.equal(writes[0].payload.source_allocations.length, 2);
  assert.equal(writes[0].payload.schema_version, 2); assert.equal(writes[0].payload.basis_kind, undefined);
  const reviewer = runtime(operator, api); reviewer.review(dto.multi, 'approve');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(reviewer.shownSources.value[0].settlement_no, 'SET-SOURCE-1');
  reviewer.reason.value = 'Whole-proposal opposite-party approval'; await reviewer.submit('decision');
  assert.equal(writes.length, 2); assert.equal(writes[1].path, '/settlement-cost-exceptions/exception/decision');
  assert.equal(writes[1].payload.expected_version, 1); assert.equal(writes[1].payload.source_allocations, undefined);
  editor.review(dto.multi, 'approve'); assert.equal(editor.decision.value, null);
});
test('changing selected refund while source query is pending never populates old segment inputs', async () => {
  let resolve;
  const editor = runtime(provider, { get: () => new Promise(yes => { resolve = yes; }) });
  editor.refunds.value = [refundChoice]; editor.exception.refund_id = 'refund'; const pending = editor.chooseRefund();
  editor.exception.refund_id = ''; await editor.chooseRefund(); resolve({ data: { items: multiReport(), total: 3 } }); await pending;
  assert.equal(editor.costSources.value.length, 0); assert.equal(editor.exception.source_allocations.length, 0); assert.equal(editor.sourcesReady.value, false);
});
test('switching management tabs cannot let an old chained load invalidate current participant choices', async () => {
  const pending = [], paths = [];
  const editor = runtime(operator, { get: path => { paths.push(path); return new Promise(resolve => pending.push(resolve)); } });
  const old = editor.load(); editor.tab.value = 'exceptions'; const next = editor.load();
  pending[1]({ data: { items: [dto.party] } }); await new Promise(resolve => setImmediate(resolve));
  pending[0]({ data: { items: [] } }); await old;
  assert.equal(paths.length, 3); assert.equal(paths[2], '/settlement-cost-exceptions');
  pending[2]({ data: { items: [dto.exception] } }); await next;
  assert.equal(editor.parties.value.length, 1); assert.equal(editor.exceptions.value.length, 1);
});
test('new SFCs compile with fluid styles; no native dialogs or unstructured JSON evidence', () => {
  for (const name of ['SettlementManagement', 'SettlementRefundEvidence', 'SettlementReportEvidence', 'SettlementEconomicSegments', 'SettlementBalanceBridge', 'SettlementFinanceWorkspace']) {
    const text = readFileSync(new URL(`../src/${name}.vue`, import.meta.url), 'utf8'), { descriptor, errors } = parse(text);
    assert.deepEqual(errors, []); const compiled = compileScript(descriptor, { id: name });
    assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: name, id: name, compilerOptions: { bindingMetadata: compiled.bindings } }).errors, []);
    for (const style of descriptor.styles.filter(s => !s.src)) assert.deepEqual(compileStyle({ source: style.content, id: name, scoped: true }).errors, []);
    assert.doesNotMatch(text, /window\.(?:prompt|confirm)\(|JSON\.stringify\(evidence/);
  }
});
