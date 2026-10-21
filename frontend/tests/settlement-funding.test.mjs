import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createSSRApp, compile } from 'vue';
import { renderToString } from '@vue/server-renderer';
import * as funding from '../src/settlementFunding.js';

const source = readFileSync(new URL('../src/SettlementFundingManagement.vue', import.meta.url), 'utf8');
const operator = { id: 'maker', platform_role: 'finance_settlement', is_active: true, activation_status: 'active' };
const context = (user = operator) => ({ user, enterprise: null, businessView: '', api: {} });
// Execute the actual nested serializer in isolation, without importing app or DB.
const backendFixtures = JSON.parse(execFileSync('python3', ['-c', `
import ast, json, sys
from types import SimpleNamespace as Row
tree = ast.parse(open(sys.argv[1]).read())
install = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'install')
function = next(node for node in install.body if isinstance(node, ast.FunctionDef) and node.name == 'funding_request_dict')
Decision, Proof = object(), object()
def serialize(kind, status, configured=False):
    decision = None if status == 'proposed' else Row(decision='rejected' if status == 'rejected' else 'approved', reviewer_id='reviewer')
    proof = Row(request_id='request') if status == 'covered' else None
    class Database:
        def get(self, model, key):
            return decision if model is Decision else proof
    payload = dict(settlement_id='settlement', baseline_hash='hash', currency='CNY', amount='0.10', actual_receipts='9.90', kind=kind,
        bearer=dict(participant_type='platform', participant_id='platform', participant_name='Platform'), reason='evidence', evidence_refs=['proof'])
    namespace = dict(json=json, FundingDecision=Decision, FundingProof=Proof,
        ns={'settlement_funding_proofs': {'verify': lambda *args: None}} if configured else {})
    exec(compile(ast.Module(body=[function], type_ignores=[]), sys.argv[1], 'exec'), namespace)
    return namespace['funding_request_dict'](Database(), Row(id='request', creator_id='maker', settlement_id='settlement', payload_json=json.dumps(payload)))
print(json.dumps(dict(proposed=serialize('platform_absorption', 'proposed'), approved=serialize('platform_absorption', 'approved'),
    covered=serialize('platform_absorption', 'covered'), external=serialize('external_receipt', 'approved'),
    configured_external=serialize('external_receipt', 'approved', True))))
`, fileURLToPath(new URL('../../backend/app/settlement_finance.py', import.meta.url))], { encoding: 'utf8' }));
const { proposed, approved } = backendFixtures;
const form = { settlement_id: 'settlement', kind: 'platform_absorption', amount: '0.10', currency: 'CNY', reason: '依据', evidence_refs: 'proof-a\nproof-b' };
const freshState = () => ({ items: [], busy: false, saving: false, error: '', notice: '' });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

test('platform operators write, security reads, inactive identities are denied', () => {
  for (const role of ['super_admin', 'platform_operator', 'finance_settlement']) assert.deepEqual(funding.fundingAccess(context({ ...operator, platform_role: role })), { read: true, write: true });
  assert.deepEqual(funding.fundingAccess(context({ ...operator, platform_role: 'security_compliance' })), { read: true, write: false });
  for (const patch of [{ is_active: false }, { activation_status: 'inactive' }, { id: '' }, { platform_role: 'buyer' }]) assert.deepEqual(funding.fundingAccess(context({ ...operator, ...patch })), { read: false, write: false });
});

test('verified provider scope remains read-only and cannot fall back to global', () => {
  const provider = { id: 'provider', is_active: true, activation_status: 'active', verified_status: 'verified', enterprise_memberships: [{ enterprise_id: 'enterprise', status: 'active', role: 'enterprise_admin', business_roles: 'provider,buyer' }] };
  const scoped = { user: provider, enterprise: { id: 'enterprise', verification_status: 'verified' }, businessView: 'provider' };
  assert.deepEqual(funding.fundingAccess(scoped), { read: true, write: false });
  assert.deepEqual(funding.fundingScope(scoped), { enterprise_id: 'enterprise', business_view: 'provider' });
  assert.throws(() => funding.fundingPayload('propose', form, null, scoped));
  assert.throws(() => funding.fundingScope({ ...scoped, businessView: 'buyer' }));
  assert.throws(() => funding.fundingScope({ ...scoped, enterprise: { id: 'foreign', verification_status: 'verified' } }));
  assert.deepEqual(funding.fundingScope({ ...context(), enterprise: { id: 'ambient' } }), {});
});

test('proposal body matches actual backend with exact cents and evidence references', () => {
  assert.deepEqual(funding.fundingPayload('propose', form, null, context()), { settlement_id: 'settlement', kind: 'platform_absorption', amount: '0.10', currency: 'CNY', reason: '依据', evidence_refs: ['proof-a', 'proof-b'] });
  const external = funding.fundingPayload('propose', { ...form, kind: 'external_receipt', bearer_enterprise_id: 'payer' }, null, context());
  assert.equal(external.bearer_enterprise_id, 'payer');
  assert.equal(Object.hasOwn(external, 'bank_receipt_verified'), false);
  for (const change of [{ currency: 'USD' }, { amount: '0' }, { amount: '1.001' }, { amount: '-1' }, { amount: '1000000000000' }, { reason: '' }, { evidence_refs: '' }, { settlement_id: 'x'.repeat(37) }, { kind: 'external_receipt', bearer_enterprise_id: '' }]) assert.throws(() => funding.fundingPayload('propose', { ...form, ...change }, null, context()));
});

test('different operator decision and verify use exact expected_version contract', () => {
  assert.equal(funding.canDecideFunding(proposed, context()), false);
  const reviewer = context({ ...operator, id: 'reviewer' });
  assert.equal(funding.canDecideFunding(proposed, reviewer), true);
  assert.deepEqual(funding.fundingPayload('decision', { decision: 'approved', note: 'reviewed' }, proposed, reviewer), { decision: 'approved', note: 'reviewed', expected_version: 1 });
  assert.deepEqual(funding.fundingPayload('verify', {}, approved, reviewer), { expected_version: 2 });
  assert.equal(funding.canVerifyFunding(approved, context()), false);
  assert.equal(funding.canVerifyFunding({ ...approved, version: 1 }, reviewer), false);
  assert.equal(funding.canVerifyFunding({ ...approved, can_verify: false }, reviewer), false);
});

test('unconfigured external verifier cannot be bypassed or reported as bank verified', () => {
  const row = backendFixtures.external;
  const reviewer = context({ ...operator, id: 'reviewer' });
  assert.equal(funding.canVerifyFunding(row, reviewer), false);
  assert.throws(() => funding.fundingPayload('verify', { receipt_reference: 'cash' }, row, reviewer));
  assert.deepEqual(funding.fundingPayload('verify', { receipt_reference: 'cash' }, backendFixtures.configured_external, reviewer), { expected_version: 2, receipt_reference: 'cash' });
  assert.match(funding.fundingError({ response: { data: { detail: 'trusted_funding_receipt_verifier_unavailable' } } }), /未配置/);
  assert.match(funding.fundingError({ response: { data: { detail: 'platform_absorption_exceeds_proven_available_funds' } } }), /已有平台利润/);
});

test('current real serializer supplies truthful capability flags and explicit false always denies verify', () => {
  const reviewer = context({ ...operator, id: 'reviewer' });
  for (const row of Object.values(backendFixtures)) {
    assert.equal(typeof row.can_verify, 'boolean');
    assert.equal(typeof row.trusted_receipt_verifier_available, 'boolean');
    assert.equal(typeof row.verification_unavailable_reason, 'string');
    assert.equal(row.bank_receipt_verified, false);
    if (row.can_verify === false) assert.equal(funding.canVerifyFunding(row, reviewer), false);
  }
  assert.equal(approved.can_verify, true);
  assert.equal(approved.trusted_receipt_verifier_available, false);
  assert.equal(backendFixtures.external.can_verify, false);
  assert.equal(backendFixtures.external.verification_unavailable_reason, 'trusted_funding_receipt_verifier_unavailable');
  assert.equal(backendFixtures.configured_external.can_verify, true);
  assert.equal(backendFixtures.configured_external.trusted_receipt_verifier_available, true);
});

test('legacy serialized platform approval permits verify without optional capabilities; external stays closed', () => {
  const legacy = Object.fromEntries(Object.entries(approved).filter(([key]) => !['can_verify', 'trusted_receipt_verifier_available', 'verification_unavailable_reason'].includes(key)));
  const reviewer = context({ ...operator, id: 'reviewer' });
  assert.equal(funding.canVerifyFunding(legacy, reviewer), true);
  assert.deepEqual(funding.fundingPayload('verify', {}, legacy, reviewer), { expected_version: 2 });
  assert.equal(funding.canVerifyFunding({ ...legacy, kind: 'external_receipt' }, reviewer), false);
});

test('list rejects stale scope, stale token, disposal, and out-of-order responses', async () => {
  for (const change of ['scope', 'token', 'dispose', 'new-read']) {
    const current = context(), state = freshState(), queue = [];
    current.api = { defaults: { headers: { Authorization: 'old' } }, get: () => { const item = deferred(); queue.push(item); return item.promise; } };
    const session = funding.createFundingSession(() => current, state);
    const pending = session.load();
    if (change === 'scope') current.businessView = 'provider';
    if (change === 'token') current.api.defaults.headers.Authorization = 'new';
    if (change === 'dispose') session.dispose();
    if (change === 'new-read') { const next = session.load(); queue[1].resolve({ data: { items: ['new'] } }); await next; }
    queue[0].resolve({ data: { items: ['stale'] } }); await pending;
    assert.notEqual(state.items[0], 'stale');
  }
});

test('double submit blocked and retry reuses request_id with identical body', async () => {
  const current = context(), state = freshState(), calls = [], queue = [];
  current.api.post = (path, body) => { calls.push({ path, body }); const item = deferred(); queue.push(item); return item.promise; };
  const session = funding.createFundingSession(() => current, state);
  const first = session.submit('propose', form); assert.equal(await session.submit('propose', form), false);
  assert.equal(calls.length, 1);
  queue[0].reject(new Error('offline')); assert.equal(await first, false);
  const second = session.submit('propose', form); queue[1].resolve({}); assert.equal(await second, true);
  assert.equal(calls[0].path, funding.fundingPath); assert.deepEqual(calls[0].body, calls[1].body);
  const third = session.submit('propose', { ...form, amount: '0.20' }); queue[2].resolve({}); await third;
  assert.notEqual(calls[2].body.request_id, calls[1].body.request_id);
});

test('stale mutation never updates a new scope and readonly roles never post', async () => {
  const current = context(), state = freshState(), pending = deferred();
  current.api.post = () => pending.promise;
  const session = funding.createFundingSession(() => current, state);
  const result = session.submit('propose', form); current.user = { ...operator, id: 'another' }; session.reset();
  state.notice = 'new scope'; pending.resolve({}); assert.equal(await result, false); assert.equal(state.notice, 'new scope');
  current.user = { ...operator, platform_role: 'security_compliance' };
  current.api.post = () => assert.fail('readonly write'); assert.equal(await session.submit('propose', form), false);
});

test('stale failures cannot expose old scope errors and independent writes get fresh identities', async () => {
  const current = context(), state = freshState(), pending = deferred();
  current.api.get = () => pending.promise;
  const session = funding.createFundingSession(() => current, state);
  const load = session.load(); current.user = { ...operator, id: 'new' }; session.reset();
  pending.reject(new Error('private old scope')); await load; assert.equal(state.error, '');
  const ids = []; current.api.post = async (path, body) => { ids.push(body.request_id); };
  await session.submit('propose', form); await session.submit('propose', form);
  assert.notEqual(ids[0], ids[1]);
});

test('decision and verify paths encode identity and never set bank_verified', async () => {
  const current = context({ ...operator, id: 'reviewer' }), state = freshState(), calls = [];
  current.api.post = async (path, body) => { calls.push({ path, body }); };
  const session = funding.createFundingSession(() => current, state);
  await session.submit('decision', { decision: 'rejected', note: 'reason' }, { ...proposed, id: 'a/b' });
  await session.submit('verify', {}, approved);
  assert.equal(calls[0].path, `${funding.fundingPath}/a%2Fb/decision`);
  assert.equal(calls[1].path, `${funding.fundingPath}/request/verify`);
  assert.deepEqual(Object.keys(calls[1].body).sort(), ['expected_version', 'request_id']);
});

test('component compiles with compact pagination and modal never closes on backdrop or Escape', () => {
  const { descriptor, errors } = parse(source); assert.deepEqual(errors, []);
  const script = compileScript(descriptor, { id: 'funding' });
  assert.ok(script.bindings.PaginationControls);
  const template = compileTemplate({ source: descriptor.template.content, filename: 'SettlementFundingManagement.vue', id: 'funding', compilerOptions: { bindingMetadata: script.bindings } });
  assert.deepEqual(template.errors, []);
  assert.match(source, /font-size:12px/); assert.match(source, /:page-size="pageSize"/); assert.match(source, /@size="resize"/);
  assert.doesNotMatch(source, /window\.(alert|confirm|prompt)|@click\.self|@keydown\.esc/);
  assert.match(source, /role="dialog" aria-modal="true"/);
  assert.match(source, /value="CNY" readonly/); assert.doesNotMatch(source, /v-model="form.currency"/);
  assert.match(source, /onBeforeUnmount\(\(\) => \{ session.dispose\(\); directory.dispose\(\); \}\)/);
});

test('actual table renders truthful coverage, unavailable verifier and bank verification false', async () => {
  const start = source.indexOf('<table>'), end = source.indexOf('</table>', start) + '</table>'.length;
  const html = await renderToString(createSSRApp({
    components: Object.fromEntries(['Eye', 'Check', 'ClipboardCheck'].map(name => [name, { render: () => null }])),
    setup: () => ({ rows: [backendFixtures.external, backendFixtures.covered], state: freshState(), ...funding,
      money: value => value || '—', context: () => context(), open() {} }),
    render: compile(source.slice(start, end)),
  }));
  assert.match(html, /可信核验器未配置，不能确认到账/); assert.match(html, /缺口已覆盖/);
  assert.match(html, /银行到账验证/); assert.equal((html.match(/<td>否<\/td>/g) || []).length, 2);
  assert.doesNotMatch(html, /aria-label="核验资金覆盖"/);
});

test('actual approved platform serialization renders verify for another operator without capability fields', async () => {
  const row = Object.fromEntries(Object.entries(approved).filter(([key]) => !['can_verify', 'trusted_receipt_verifier_available', 'verification_unavailable_reason'].includes(key)));
  const start = source.indexOf('<table>'), end = source.indexOf('</table>', start) + '</table>'.length;
  const html = await renderToString(createSSRApp({
    components: Object.fromEntries(['Eye', 'Check', 'ClipboardCheck'].map(name => [name, { render: () => null }])),
    setup: () => ({ rows: [row], state: freshState(), ...funding, money: value => value || '—',
      context: () => context({ ...operator, id: 'reviewer' }), open() {} }),
    render: compile(source.slice(start, end)),
  }));
  assert.match(html, /aria-label="核验资金覆盖"/);
  assert.match(html, /<td>否<\/td>/);
});

const settlement = { id: 'internal-id', settlement_no: 'SET-2026-001', status: 'locked', baseline_hash: 'baseline', gross_amount: '120.00', cost_amount: '30.00', provider_name: '提供企业' };
test('authorized SET selector queries exact report contract and filters positive baselines', async () => {
  const current = context(), state = {}, calls = [];
  current.api.get = async (path, { params }) => {
    calls.push({ path, params });
    return { data: { total: 103, items: [settlement, ...[{ gross_amount: '-1.00' }, { gross_amount: '0.00' }, { baseline_hash: '' }, { status: 'pending' }].map(patch => ({ ...settlement, ...patch }))] } };
  };
  const directory = funding.createFundingDirectory(() => current, state);
  await directory.load(' SET-2026 ', 2);
  assert.deepEqual(calls, [{ path: '/settlement-reporting/report', params: { status: 'locked', q: 'SET-2026', start: '1900-01-01', end: '9998-12-31', page: 2, page_size: 100 } }]);
  assert.deepEqual(state.items, [settlement]); assert.equal(state.total, 103); assert.equal(state.page, 2);
  assert.equal(Object.hasOwn(state.items[0], 'funding_gap'), false);
});

test('SET directory rejects stale scope, closed dialog, disposal, and read-only requests', async () => {
  for (const change of ['scope', 'close', 'dispose', 'new-request']) {
    const current = context(), state = {}, queue = [];
    current.api.get = () => { const pending = deferred(); queue.push(pending); return pending.promise; };
    const directory = funding.createFundingDirectory(() => current, state);
    const task = directory.load();
    if (change === 'scope') current.user = { ...operator, id: 'other' };
    if (change === 'close') directory.reset();
    if (change === 'dispose') directory.dispose();
    if (change === 'new-request') { const next = directory.load('new', 2); queue[1].resolve({ data: { items: [], total: 0 } }); await next; }
    queue[0].resolve({ data: { items: [settlement], total: 1 } }); await task;
    assert.notEqual(state.items?.[0]?.id, settlement.id);
  }
  const current = context({ ...operator, platform_role: 'security_compliance' });
  current.api.get = () => assert.fail('readonly proposal directory request');
  await funding.createFundingDirectory(() => current, {}).load();
});

test('proposal selector renders SET names with internal values and never guesses gap amount', async () => {
  const start = source.indexOf('<label>最终清算单<select'), end = source.indexOf('</label>', start) + '</label>'.length;
  const html = await renderToString(createSSRApp({ setup: () => ({ form: { settlement_id: '' }, options: { items: [settlement], busy: false }, selectedSettlement: null, money: value => value, chooseSettlement() {} }), render: compile(source.slice(start, end)) }));
  assert.match(html, /value="internal-id"/); assert.match(html, /SET-2026-001/);
  assert.match(html, /提供企业/); assert.match(html, /收入 120.00 \/ 成本 30.00 CNY/);
  assert.doesNotMatch(source, /v-model="form.settlement_id"[^>]*maxlength/);
  assert.match(source, /selectedSettlement.value.id !== form.settlement_id/);
  assert.doesNotMatch(source, /form.amount\s*=.*(actual_receipts|paid_amount|gross_amount)/);
});
