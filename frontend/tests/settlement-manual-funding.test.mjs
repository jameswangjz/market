import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { canReviewManual, createManualSession, manualPayload } from '../src/settlementManualFunding.js';
const user = { id: 'reviewer', platform_role: 'finance_settlement', is_active: true };
const context = { user, businessView: '', api: {} };
const request = { id: 'funding', creator_id: 'maker', kind: 'external_receipt', status: 'approved', version: 2,
  amount: '0.03', currency: 'CNY', can_verify: true, trusted_receipt_verifier_available: true };
const evidence = { id: 'evidence', funding_request_id: request.id, creator_id: 'registrar', amount: '0.03', currency: 'CNY', status: 'registered', version: 1, bank_receipt_verified: false };
const form = { reference: 'receipt', paid_on: '2026-01-01', evidence_refs: 'invoice-a\ninvoice-b', note: 'manual proof' };
test('register exact backend keys with full gap and canonical date', () => {
  assert.deepEqual(manualPayload('register', form, request, context), { funding_request_id: 'funding', amount: '0.03', currency: 'CNY', reference: 'receipt', paid_on: '2026-01-01', evidence_refs: ['invoice-a', 'invoice-b'], note: 'manual proof' });
  for (const paid_on of ['2026-02-30', '2026-1-01', '9999-01-01']) assert.throws(() => manualPayload('register', { ...form, paid_on }, request, context));
  assert.throws(() => manualPayload('register', form, { ...request, status: 'covered' }, context));
});
test('independent immutable review and read-only isolation', () => {
  assert.equal(canReviewManual(evidence, context), true);
  assert.equal(canReviewManual({ ...evidence, creator_id: user.id }, context), false);
  assert.deepEqual(manualPayload('review', { decision: 'verified', note: 'checked' }, evidence, context), { expected_version: 1, decision: 'verified', note: 'checked' });
  assert.throws(() => manualPayload('review', { decision: 'approved', note: 'checked' }, evidence, context));
  assert.throws(() => manualPayload('register', form, request, { ...context, user: { ...user, platform_role: 'security_compliance' } }));
});
test('verification only uses matching reviewed manual evidence and does not claim bank verification', () => {
  const reviewed = { ...evidence, status: 'verified', version: 2 };
  assert.deepEqual(manualPayload('verify', { evidence: reviewed }, request, context), { expected_version: 2, receipt_reference: 'manual:evidence' });
  for (const patch of [{ funding_request_id: 'other' }, { amount: '0.04' }, { currency: 'USD' }, { bank_receipt_verified: true }, { status: 'registered' }])
    assert.throws(() => manualPayload('verify', { evidence: { ...reviewed, ...patch } }, request, context));
  assert.throws(() => manualPayload('verify', { evidence: reviewed }, { ...request, creator_id: user.id }, context));
});
test('retry keeps request ID and duplicate submission is blocked', async () => {
  const calls = []; let finish;
  const state = {};
  const api = { post: (path, body) => { calls.push({ path, body }); return new Promise(resolve => { finish = resolve; }); } };
  const session = createManualSession(() => ({ ...context, api }), state);
  const first = session.submit('register', form, request);
  assert.equal(await session.submit('register', form, request), false);
  finish(Promise.reject(new Error('offline'))); assert.equal(await first, false);
  const second = session.submit('register', form, request);
  assert.equal(calls[0].body.request_id, calls[1].body.request_id);
  assert.equal(calls[0].path, '/settlement-finance/manual-funding-evidence');
  finish({ data: evidence }); assert.equal(await second, true);
  session.dispose();
});
test('stale context and unmount discard pending reads', async () => {
  let finish, current = context;
  const state = {};
  const api = { get: () => new Promise(resolve => { finish = resolve; }) };
  current = { ...context, api };
  const session = createManualSession(() => current, state);
  const pending = session.load(); current = { ...current, user: { ...user, id: 'other' } }; session.reset();
  finish({ data: { items: [request] } }); await pending;
  assert.deepEqual(state.requests, []);
  const last = session.load(); session.dispose(); finish({ data: { items: [request] } }); await last;
  assert.deepEqual(state.items, []);
});
test('manual SFC compiles with real Vue compiler', () => {
  const source = readFileSync(new URL('../src/SettlementManualFunding.vue', import.meta.url), 'utf8');
  const { descriptor } = parse(source);
  const compiled = compileScript(descriptor, { id: 'manual' });
  const template = compileTemplate({ source: descriptor.template.content, filename: 'SettlementManualFunding.vue', id: 'manual', compilerOptions: { bindingMetadata: compiled.bindings } });
  assert.deepEqual(template.errors, []);
});
test('real backend serializer produces reviewable DTO and manual verification reference', () => {
  const data = JSON.parse(execFileSync('python3', ['-c', `
import ast,json,sys
from types import SimpleNamespace as R
from datetime import date
from decimal import Decimal
tree=ast.parse(open(sys.argv[1]).read())
install=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='install')
serialize=next(n for n in install.body if isinstance(n,ast.FunctionDef) and n.name=='serialize')
row=R(id='evidence',funding_request_id='funding',settlement_id='s',creator_id='registrar',amount=Decimal('0.03'),currency='CNY',reference='receipt',paid_on=date(2026,1,1),evidence_hash='hash',evidence_json=json.dumps(dict(evidence_refs=['invoice'],note='proof')))
class DB:
    def __init__(self,review): self.review=review
    def get(self,*args): return self.review
ns=dict(json=json,Review=object())
exec(compile(ast.Module(body=[serialize],type_ignores=[]),sys.argv[1],'exec'),ns)
print(json.dumps([ns['serialize'](DB(None),row),ns['serialize'](DB(R(reviewer_id='independent',note='checked',decision='verified')),row)]))
`, fileURLToPath(new URL('../../backend/app/settlement_manual_funding.py', import.meta.url))], { encoding: 'utf8' }));
  assert.equal(canReviewManual(data[0], context), true);
  assert.equal(canReviewManual(data[1], context), false);
  assert.equal(data[1].method, 'manual_independent_review');
  assert.deepEqual(manualPayload('verify', { evidence: data[1] }, request, context), {
    expected_version: 2, receipt_reference: data[1].receipt_reference,
  });
});
