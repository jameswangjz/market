import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { parse, compileScript, compileTemplate, compileStyle } from '@vue/compiler-sfc';
import { compile, createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';
import * as helpers from '../src/settlementFinance.js';

const provider = { id: 'admin', verified_status: 'verified', enterprise_memberships: [{ enterprise_id: 'a', status: 'active', role: 'enterprise_admin' }] };
const enterprise = { id: 'a' };
const platform = { id: 'finance', platform_role: 'finance_settlement' };
const source = readFileSync(new URL('../src/SettlementFinanceWorkspace.vue', import.meta.url), 'utf8');
const { descriptor, errors } = parse(source);

// Execute the real backend serializers with small database adapters, so frontend
// contract fixtures change when backend response fields change.
const backendFixtures = JSON.parse(execFileSync('python3', ['-c', `
import ast, json, sys, re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace as Obj
finance = ast.parse((Path(sys.argv[1]) / 'settlement_finance.py').read_text())
reporting = ast.parse((Path(sys.argv[1]) / 'settlement_reporting.py').read_text())
def install(tree, name, context):
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<backend serializer>', 'exec'), context)
g = dict(json=json, Decimal=Decimal, CENT=Decimal('.01'), Confirmation='confirmation', Baseline='baseline', Finalization='finalization', date=date, datetime=datetime, timedelta=timedelta, timezone=timezone, re=re, SHANGHAI=ZoneInfo('Asia/Shanghai'), Query=lambda default, **kwargs: default, HTTPException=Exception)
install(finance, 'money', g)
install(finance, 'balance', g)
install(finance, 'serialize_transaction', g)
install(finance, 'position_dict', g)
install(finance, 'list_positions', g)
install(reporting, 'metadata', g)
install(reporting, 'filters', g)
tx = Obj(id='tx-real', kind='payout', mode='manual', reference='REF-42', note='Approved evidence', creator_id='maker', payload_json=json.dumps(dict(allocations=[dict(position_id='p-real', amount='40.00')], counter_position_id=None)))
draft = g['serialize_transaction'](Obj(get=lambda model, key: None), tx)
confirmed = g['serialize_transaction'](Obj(get=lambda model, key: Obj(reviewer_id='reviewer')), tx)
annual = g['metadata'](Obj(id='annual-real', year=2026, version=1, scope_key='platform', actor_id='maker', cutoff='2026-10-18T12:00:00+00:00', status='draft', note='Snapshot', digest='abc123', request_id='request-real'), Obj(get=lambda model, key: None))
baseline = Obj(snapshot_json=json.dumps(dict(settlement_no='SET-42', order_no='ORD-42')))
g['applied'] = lambda db, key: Decimal('10.00')
position = Obj(id='p-real', settlement_id='set-real', order_id='ord-real', participant_type='enterprise', participant_id='provider-real', participant_name='Actual provider', kind='profit', share_kind='provider_share', direction='payable', amount=Decimal('50.00'), currency='CNY', blocked_reason='')
position_model = position
position = g['position_dict'](None, position_model, baseline)
g['Position'] = Obj(id='id')
g['positions_query'] = lambda *args, **kwargs: Obj(order_by=lambda field: None)
usd = Obj(**(vars(position_model) | dict(id='p-usd', currency='USD')))
funds = g['list_positions'](Obj(scalars=lambda query: Obj(all=lambda: [position_model, usd]), get=lambda model, key: baseline), None)
filters = [g['filters'](start=start, end=end, source_kind=kind) for start, end, kind in [('2026-01-01', '2026-01-31', 'original'), ('2028-02-29', '2028-02-29', 'refund'), ('2026-12-01', '2026-12-31', 'version_change')]]
source_kinds = next(ast.literal_eval(n.comparators[0]) for n in ast.walk(reporting) if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name) and n.left.id == 'source_kind' and isinstance(n.ops[0], ast.NotIn))
roles = next(ast.literal_eval(n.value) for n in reporting.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'READ_ROLES' for t in n.targets))
print(json.dumps(dict(draft=draft, confirmed=confirmed, annual=annual, position=position, funds=funds, filters=filters, source_kinds=sorted(source_kinds), read_roles=sorted(roles))))
`, fileURLToPath(new URL('../../backend/app/', import.meta.url))], { encoding: 'utf8' }));

test('SFC script and template compile with shared console components', () => {
  assert.deepEqual(errors, []);
  const script = compileScript(descriptor, { id: 'finance-test' });
  const template = compileTemplate({ id: 'finance-test', source: descriptor.template.content, compilerOptions: { bindingMetadata: script.bindings } });
  assert.deepEqual(template.errors, []);
  for (const style of descriptor.styles) assert.deepEqual(compileStyle({ source: style.content, id: 'finance-test', scoped: true }).errors, []);
  for (const component of ['ConsoleTabs', 'MainListFilters', 'PaginationControls']) assert.ok(script.bindings[component]);
  assert.ok(!/window\.(prompt|confirm)\(/.test(source));
});

test('provider reads are scoped and never grant financial writes', () => {
  assert.deepEqual(helpers.financeAccess(provider, enterprise, 'provider'), { read: true, write: false, platform: false });
  assert.deepEqual(helpers.financeScope(provider, enterprise, 'provider'), { enterprise_id: 'a', business_view: 'provider' });
  for (const [user, company, view] of [[provider, enterprise, 'buyer'], [provider, { id: 'b' }, 'provider'], [{ ...provider, verified_status: 'pending' }, enterprise, 'provider']]) assert.throws(() => helpers.financeScope(user, company, view));
  assert.equal(helpers.financeAccess({ platform_role: 'security_compliance' }).write, false);
  assert.equal(helpers.financeAccess({ platform_role: 'security_compliance' }).read, true);
  assert.equal(helpers.financeAccess({ platform_role: 'auditor' }).read, false);
  assert.throws(() => helpers.financeScope({ platform_role: 'auditor' }));
  assert.equal(helpers.financeAccess(platform).write, true);
});

test('decimal allocation never loses cents or derives balances with floating point', () => {
  assert.equal(helpers.money('99999999999999999.01'), '99,999,999,999,999,999.01');
  assert.equal(helpers.money('-0.10'), '-0.10');
  assert.deepEqual(helpers.allocation({ id: 'p', balance: '99999999999999999.01' }, '99999999999999999.01'), { position_id: 'p', amount: '99999999999999999.01' });
  for (const amount of ['0', '-1', '1.001', '1e2', 'NaN', '2.01']) assert.throws(() => helpers.allocation({ id: 'p', balance: '2.00' }, amount));
});

test('Beijing month boundaries, scoped filters, and safe errors', () => {
  assert.deepEqual(helpers.monthRange(new Date('2026-12-31T16:00:00Z')), { start: '2027-01-01', end: '2027-01-31' });
  assert.deepEqual(helpers.monthRange(new Date('2028-02-15T00:00:00Z')), { start: '2028-02-01', end: '2028-02-29' });
  assert.deepEqual(helpers.monthRange(new Date('2026-02-15T00:00:00Z')), { start: '2026-02-01', end: '2026-02-28' });
  assert.deepEqual(helpers.monthRange(new Date('2026-04-15T00:00:00Z')), { start: '2026-04-01', end: '2026-04-30' });
  const params = helpers.reportParams({ start: '2026-01-01', end: '2026-01-31', provider_id: 'foreign', q: ' ORD001 ' }, { enterprise_id: 'a', business_view: 'provider' }, 3, 50);
  assert.equal(params.provider_id, undefined); assert.equal(params.q, 'ORD001'); assert.equal(params.page, 3);
  assert.equal(params.start, '2026-01-01'); assert.equal(params.end, '2026-01-31');
  assert.equal(helpers.reportParams({ start: '2026-01-01', end: '2026-01-01' }, {}).end, '2026-01-01');
  for (const end of ['2026-02-30', '2026-01-31T00:00:00+08:00']) assert.throws(() => helpers.reportParams({ end }, {}));
  assert.throws(() => helpers.reportParams({ start: '2026-02-01', end: '2026-01-01' }, {}));
  assert.equal(helpers.safeError({ response: { data: { detail: [{ msg: 'bad' }] } } }), '输入校验失败，请检查必填字段、日期和金额格式。');
});

test('network retries preserve identity until payload or scope changes', () => {
  const identity = helpers.retryIdentity();
  const key = identity({ scope: 'a', amount: '0.10' });
  assert.equal(identity({ scope: 'a', amount: '0.10' }), key);
  assert.notEqual(identity({ scope: 'a', amount: '0.11' }), key);
  assert.notEqual(identity({ scope: 'b', amount: '0.10' }), key);
});

test('participant columns display authoritative cost, profit share, and total receivable independently', () => {
  const state = vm.createContext({
    computed: getter => ({ get value() { return getter(); } }),
    tab: { value: 'reports' }, reportView: { value: 'participants' },
    amountLabels: helpers.amountLabels, participantAmountLabels: helpers.participantAmountLabels,
    countLabels: helpers.countLabels, money: helpers.money, labels: {},
    sourceKinds: helpers.sourceKinds, positionSubjectLabels: helpers.positionSubjectLabels,
  });
  vm.runInContext(source.slice(source.indexOf('const columns = computed('), source.indexOf('async function load()')) + '\nglobalThis.participantColumns = columns.value;', state);
  const keys = state.participantColumns.map(([key]) => key);
  for (const key of ['cost_amount', 'profit_share', 'total_receivable']) assert.ok(keys.includes(key));
  const row = { cost_amount: '100.00', profit_share: '40.00', total_receivable: '140.00' };
  assert.equal(state.cell(row, 'cost_amount'), '100.00');
  assert.equal(state.cell(row, 'profit_share'), '40.00');
  assert.equal(state.cell(row, 'total_receivable'), '140.00');
  assert.equal(state.cell({ cost_amount: '100.00', profit_share: '40.00' }, 'total_receivable'), '—');
});

test('filter enumerations and read roles agree with actual backend declarations', () => {
  assert.deepEqual(helpers.sourceKinds.map(item => item.value).sort(), backendFixtures.source_kinds);
  for (const source_kind of backendFixtures.source_kinds) assert.equal(helpers.reportParams({ source_kind }, {}).source_kind, source_kind);
  for (const filter of backendFixtures.filters) {
    const params = helpers.reportParams(filter, {});
    for (const key of ['start', 'end', 'source_kind']) assert.equal(params[key], filter[key]);
  }
  for (const platform_role of [...backendFixtures.read_roles, 'auditor', 'product_manager']) {
    assert.equal(helpers.financeAccess({ platform_role }).read, backendFixtures.read_roles.includes(platform_role));
  }
});

test('actual currency-grouped position summaries render each currency independently', async () => {
  const start = source.indexOf('<template v-if="tab === \'funds\'">');
  const end = source.indexOf('</template>', start) + '</template>'.length;
  const html = await renderToString(createSSRApp({
    setup: () => ({ tab: 'funds', result: backendFixtures.funds, labels: { payable: '应付', receivable: '应追回', internal: '内部收益' }, money: helpers.money }),
    render: compile(source.slice(start, end)),
  }));
  assert.match(html, /CNY/); assert.match(html, /USD/);
  assert.equal((html.match(/40\.00/g) || []).length, 2);
});

test('voucher totals use exact cents and currency resolution uses scoped actual positions', async () => {
  assert.equal(helpers.transactionAmount({ allocations: [{ amount: '0.10' }, { amount: '0.20' }] }), '0.30');
  assert.equal(helpers.transactionAmount({ allocations: [{ amount: '99999999999999999.01' }, { amount: '0.09' }] }), '99999999999999999.10');
  const positions = await helpers.transactionPositions({ get: async (path, { params }) => {
    assert.equal(path, '/settlement-finance/positions'); assert.equal(params.enterprise_id, 'a');
    assert.equal(params.business_view, 'provider'); assert.equal(params.page_size, 100);
    return { data: backendFixtures.funds };
  } }, [backendFixtures.draft], { enterprise_id: 'a', business_view: 'provider' }, {}, () => true);
  assert.equal(helpers.transactionAmount(backendFixtures.draft), '40.00');
  assert.equal(helpers.transactionCurrency(backendFixtures.draft, positions), 'CNY');
  assert.equal(helpers.transactionCurrency(backendFixtures.draft, {}), '待核对币种');
  const offset = { ...backendFixtures.draft, kind: 'offset', counter_position_id: 'p-usd' };
  assert.equal(helpers.transactionAmount(offset), '40.00');
  assert.equal(helpers.transactionCurrency(offset, positions), '币种不一致');
  let live = true;
  const discarded = await helpers.transactionPositions({ get: async () => { live = false; return { data: backendFixtures.funds }; } }, [backendFixtures.draft], {}, {}, () => live);
  assert.equal(discarded, null);
});

test('backend serialized creator_id enforces maker-reviewer and version checks', () => {
  const row = backendFixtures.draft;
  assert.equal(helpers.canReviewTransaction(row, { id: row.creator_id }, true), false);
  assert.equal(helpers.canReviewTransaction(row, { id: 'reviewer' }, true), true);
  assert.equal(helpers.canReviewTransaction(row, { id: 'reviewer' }, false), false);
  assert.equal(helpers.canReviewTransaction(backendFixtures.confirmed, { id: 'reviewer' }, true), false);
  assert.equal(helpers.canReviewTransaction({ ...row, creator_id: undefined }, { id: 'reviewer' }, true), false);
  const state = vm.createContext({ access: { value: { write: true } }, props: { user: { id: row.creator_id } }, canReviewTransaction: helpers.canReviewTransaction });
  vm.runInContext(source.slice(source.indexOf('function canConfirm('), source.indexOf('async function submit()')), state);
  assert.equal(state.canConfirm(row), false);
});

test('table cells render actual backend transaction, annual metadata, and position fields', () => {
  const state = vm.createContext({
    computed: getter => ({ get value() { return getter(); } }), tab: { value: 'transactions' }, reportView: { value: 'details' },
    ...helpers, labels: {}, access: { value: { write: true } }, knownPositions: { value: { [backendFixtures.position.id]: backendFixtures.position } },
  });
  vm.runInContext(source.slice(source.indexOf('const columns = computed('), source.indexOf('async function load()')) + '\nglobalThis.currentColumns = () => columns.value;', state);
  for (const [tab, row] of [['transactions', backendFixtures.draft], ['annual', backendFixtures.annual], ['funds', backendFixtures.position]]) {
    state.tab.value = tab;
    for (const [key] of state.currentColumns()) {
      if (tab !== 'transactions' || !['amount', 'currency'].includes(key)) assert.ok(Object.hasOwn(row, key), `${tab}.${key} must exist in actual backend serializer`);
      if (row[key] !== null && row[key] !== '') assert.notEqual(state.cell(row, key), '—', `${tab}.${key}`);
    }
  }
  state.tab.value = 'transactions'; assert.equal(state.cell(backendFixtures.draft, 'id'), 'tx-real');
  assert.equal(state.cell(backendFixtures.draft, 'amount'), '40.00'); assert.equal(state.cell(backendFixtures.draft, 'currency'), 'CNY');
  state.tab.value = 'annual'; assert.equal(state.cell(backendFixtures.annual, 'cutoff'), backendFixtures.annual.cutoff);
  state.tab.value = 'funds'; assert.equal(state.cell(backendFixtures.position, 'participant_type'), '企业主体');
  assert.equal(state.canRegister(backendFixtures.position), true);
  assert.equal(state.canRegister({ ...backendFixtures.position, blocked_reason: 'missing_subject_mapping' }), false);
  assert.equal(state.canRegister({ ...backendFixtures.position, balance: '0.00' }), false);
});

function runtime() {
  let liveContext = 'a';
  const state = vm.createContext({
    props: { api: null }, context: () => liveContext,
    guard: helpers.requestGuard(() => liveContext), access: { value: { read: true } },
    result: { value: {} }, detail: { value: null }, state: { filters: {}, busy: false, error: '' },
    tab: { value: 'funds' }, page: { value: 1 }, pageSize: { value: 20 },
    scope: () => ({ enterprise_id: 'a', business_view: 'provider' }), safeError: helpers.safeError,
    knownPositions: { value: {} }, transactionPositions: helpers.transactionPositions,
  });
  state.switchContext = () => { liveContext = 'b'; };
  vm.runInContext(source.slice(source.indexOf('async function load()'), source.indexOf('function search()')), state);
  return state;
}
test('VM async requests reject stale context, out-of-order responses, and unmounted updates', async () => {
  for (const change of ['scope', 'unmount', 'new-request']) {
    const state = runtime(); const pending = [];
    state.props.api = { get: (path, config) => {
      assert.equal(path, '/settlement-finance/positions'); assert.equal(config.params.enterprise_id, 'a');
      return new Promise((resolve, reject) => pending.push({ resolve, reject }));
    } };
    const first = state.load();
    if (change === 'scope') state.switchContext();
    if (change === 'unmount') state.guard.dispose();
    if (change === 'new-request') { const next = state.load(); pending[1].resolve({ data: { items: ['new'] } }); await next; }
    pending[0].resolve({ data: { items: ['stale'] } }); await first;
    assert.notEqual(state.result.value.items?.[0], 'stale');
  }
});

test('VM stale failures cannot overwrite a newer successful result', async () => {
  const state = runtime(); let reject;
  state.props.api = { get: () => new Promise((yes, no) => { reject = no; }) };
  const pending = state.load(); state.switchContext();
  reject(new Error('old scope denied')); await pending;
  assert.equal(state.state.error, '');
});

test('live authentication header contributes to stale context key', () => {
  const api = { defaults: { headers: { common: { Authorization: 'Bearer old' } } } };
  const before = helpers.financeContext(provider, enterprise, 'provider', api);
  api.defaults.headers.common.Authorization = 'Bearer new';
  assert.notEqual(helpers.financeContext(provider, enterprise, 'provider', api), before);
});

function mutationRuntime() {
  let key = 'a';
  const state = vm.createContext({
    props: { api: null }, context: () => key, scope: () => ({}),
    submitting: { value: false }, access: { value: { write: true } },
    modal: { value: { kind: 'create', context: 'a', row: backendFixtures.position } },
    form: { kind: 'payout', mode: 'manual', reference: 'REF1', note: 'Approved evidence', amount: '0.10' },
    mutationGuard: helpers.requestGuard(() => key), identity: helpers.retryIdentity(), retryIdentity: helpers.retryIdentity,
    allocation: helpers.allocation, cents: helpers.cents, safeError: helpers.safeError, state: { error: '' }, notice: { value: '' },
    canConfirm: () => true, emit: () => {}, load: async () => {},
    paymentLoading: { value: false }, paymentLoadError: { value: '' },
    paymentRows: { value: [backendFixtures.position] }, selectedPaymentIds: { value: [backendFixtures.position.id] },
    paymentAmounts: { value: { [backendFixtures.position.id]: '0.10' } }, paymentSelection: helpers.paymentSelection,
  });
  state.switchContext = () => { key = 'b'; };
  vm.runInContext(source.slice(source.indexOf('function canRegister('), source.indexOf('const detailFields = computed(')), state);
  vm.runInContext(source.slice(source.indexOf('async function submit()'), source.indexOf('async function download(')), state);
  return state;
}

test('VM duplicate submissions are blocked and network retry retains request_id', async () => {
  const state = mutationRuntime(); const calls = []; let reject;
  state.props.api = { post: (path, payload) => { calls.push(payload); return new Promise((yes, no) => { reject = no; }); } };
  const pending = state.submit(); await state.submit(); assert.equal(calls.length, 1);
  reject(new Error('network interrupted')); await pending;
  assert.equal(state.submitting.value, false);
  const retry = state.submit(); assert.equal(calls.length, 2);
  assert.equal(calls[0].request_id, calls[1].request_id);
  assert.equal(calls[1].allocations[0].amount, '0.10');
  reject(new Error('still offline')); await retry;
});

test('Q2 combined payout keeps cost and profit as independent allocations with exact partial totals', async () => {
  const profit = { ...backendFixtures.position, id: 'profit', kind: 'profit', balance: '40.00' };
  const cost = { ...backendFixtures.position, id: 'cost', kind: 'cost', share_kind: '', balance: '100.00' };
  const selection = helpers.paymentSelection(cost, [profit, cost], ['profit', 'cost'], { cost: '100.00', profit: '40.00' });
  assert.equal(selection.total, '140.00');
  assert.deepEqual(selection.allocations, [{ position_id: 'cost', amount: '100.00' }, { position_id: 'profit', amount: '40.00' }]);
  assert.equal(helpers.paymentSelection(cost, [profit, cost], ['cost', 'profit'], { cost: '0.10', profit: '0.20' }).total, '0.30');
  for (const changed of [{ participant_id: 'another' }, { participant_type: 'platform' }, { currency: 'USD' }, { direction: 'receivable' }, { blocked_reason: 'subject_unassigned' }, { balance: '0.00' }]) {
    assert.throws(() => helpers.paymentSelection(cost, [cost, { ...profit, ...changed }], ['cost', 'profit'], { cost: '100.00', profit: '40.00' }));
  }
  assert.throws(() => helpers.paymentSelection(cost, [cost], [], {}));
  assert.throws(() => helpers.paymentSelection(cost, [cost], ['cost', 'cost'], { cost: '100.00' }));
  assert.throws(() => helpers.paymentSelection(cost, [cost], ['cost'], { cost: '100.01' }));
  const state = mutationRuntime(); const requests = [];
  state.modal.value.row = cost; state.paymentRows.value = [cost, profit]; state.selectedPaymentIds.value = ['profit', 'cost']; state.paymentAmounts.value = { cost: '100.00', profit: '40.00' };
  state.props.api = { post: async (path, payload) => { requests.push(payload); assert.equal(path, '/settlement-finance/transactions'); } };
  await state.submit();
  assert.equal(requests.length, 1); assert.equal(requests[0].kind, 'payout');
  assert.deepEqual(JSON.parse(JSON.stringify(requests[0].allocations)), selection.allocations);
});

test('combined payment dialog renders separate cost and profit checkboxes and their exact total', async () => {
  const cost = { ...backendFixtures.position, id: 'cost', kind: 'cost', balance: '100.00' };
  const profit = { ...backendFixtures.position, id: 'profit', kind: 'profit', balance: '40.00' };
  const ids = ['cost', 'profit'], amounts = { cost: '100.00', profit: '40.00' };
  const start = source.indexOf('<fieldset v-if="form.kind === \'payout\'"');
  const end = source.indexOf('</fieldset>', start) + '</fieldset>'.length;
  const html = await renderToString(createSSRApp({
    components: { RefreshCw: { render: () => null } },
    setup: () => ({ form: { kind: 'payout' }, submitting: false, modal: { row: cost }, paymentLoading: false, paymentLoadError: '', paymentRows: [cost, profit], selectedPaymentIds: ids, paymentAmounts: amounts,
      paymentPreview: { ...helpers.paymentSelection(cost, [cost, profit], ids, amounts), error: '' }, labels: { cost: '供货成本', profit: '利润分成' }, money: helpers.money, loadPaymentRows() {} }),
    render: compile(source.slice(start, end)),
  }));
  assert.equal((html.match(/type="checkbox"/g) || []).length, 2);
  assert.match(html, /供货成本/); assert.match(html, /利润分成/); assert.match(html, /合计 140\.00 CNY/);
});

test('modal enters an enabled control while payment candidates load and preserves existing dialog focus', async () => {
  let callback, focusCalls = 0;
  const outside = { focus() {} }, checkbox = { disabled: true };
  const state = vm.createContext({
    modal: { value: { kind: 'create' } }, paymentLoading: { value: true }, paymentGuard: { invalidate() {} },
    document: { activeElement: outside }, nextTick: async () => {},
    watch: (value, handler) => { callback = handler; }, previousFocus: null,
  });
  const closeButton = { focus() { focusCalls++; state.document.activeElement = closeButton; } };
  const select = { focus() { state.document.activeElement = select; } };
  state.modalElement = { value: {
    contains: node => node === closeButton || node === select || node === checkbox,
    querySelector(selector) {
      for (const control of ['button', 'select', 'input', 'textarea']) assert.ok(selector.includes(`${control}:not(:disabled)`));
      assert.equal(state.paymentLoading.value, true); assert.equal(checkbox.disabled, true);
      return closeButton;
    },
  } };
  vm.runInContext(source.slice(source.indexOf('watch(modal, async value =>'), source.indexOf('onBeforeUnmount(')), state);
  await callback(state.modal.value);
  assert.equal(state.document.activeElement, closeButton); assert.equal(focusCalls, 1);
  state.paymentLoading.value = false; checkbox.disabled = false;
  assert.equal(state.document.activeElement, closeButton);
  state.document.activeElement = select;
  await callback(state.modal.value);
  assert.equal(state.document.activeElement, select); assert.equal(focusCalls, 1);
});

test('payable selection searches beyond current list page and rejects stale dialog results', async () => {
  const anchor = backendFixtures.position;
  const cost = { ...anchor, id: 'cost', kind: 'cost', balance: '100.00' };
  const calls = [];
  const result = await helpers.payablePositions({ get: async (path, { params }) => {
    calls.push(params.page); assert.equal(params.enterprise_id, 'a'); assert.equal(params.direction, 'payable');
    return { data: params.page === 1 ? { items: [anchor, { ...cost, participant_id: 'other' }], total: 101 } : { items: [cost], total: 101 } };
  } }, anchor, { enterprise_id: 'a', business_view: 'provider' }, () => true);
  assert.deepEqual(calls, [1, 2]); assert.deepEqual(result.map(row => row.id), ['cost', anchor.id]);
  let live = true;
  assert.equal(await helpers.payablePositions({ get: async () => { live = false; return { data: { items: [cost], total: 1 } }; } }, anchor, {}, () => live), null);
});

test('finance errors explain structured business codes without exposing database details', async () => {
  const error = detail => ({ message: 'Request failed with status code 409', response: { data: { detail } } });
  assert.match(helpers.safeError(error({ code: 'positive_payment_evidence_required', funds: { payable_balance: '140.00' } })), /付款凭证/);
  for (const code of ['different_reviewer_required', 'payment_evidence', 'final_baseline_not_ready', 'subject_unassigned', 'position_direction_or_balance', 'confirmed_payment_batch_required']) {
    const message = helpers.safeError(error(code)); assert.match(message, /[\u3400-\u9fff]/); assert.ok(!message.includes(code));
  }
  assert.match(helpers.safeError(error({ reason: 'future_finance_policy' })), /业务码：future_finance_policy/);
  for (const message of ['SELECT bank_account FROM private_accounts', 'sqlalchemy.exc.IntegrityError: secret', 'Traceback private-path', '数据库错误 [SQL: INSERT INTO private_accounts]']) assert.equal(helpers.safeError(error(message)), '操作失败，请稍后重试');
  const consoleSource = readFileSync(new URL('../src/ConsoleApp.vue', import.meta.url), 'utf8');
  const messages = [];
  const state = vm.createContext({ api: { post: async () => { throw error({ code: 'positive_payment_evidence_required' }); } }, notify: message => messages.push(message), safeError: helpers.safeError });
  vm.runInContext(consoleSource.slice(consoleSource.indexOf('async function batchAction('), consoleSource.indexOf('async function closeReconciliation(')), state);
  await state.batchAction({ id: 'batch' }, 'pay'); assert.match(messages[0], /付款凭证/);
});

test('VM financial writes are denied to providers and stale writes cannot clear new UI', async () => {
  const denied = mutationRuntime(); denied.access.value.write = false;
  denied.props.api = { post: () => assert.fail('provider write') }; await denied.submit();
  const state = mutationRuntime(); let resolve;
  state.props.api = { post: () => new Promise(yes => { resolve = yes; }) };
  state.emit = () => assert.fail('stale changed event');
  const pending = state.submit(); state.switchContext();
  state.modal.value = { kind: 'annual', context: 'b' }; state.submitting.value = true;
  resolve({}); await pending;
  assert.equal(state.modal.value.context, 'b'); assert.equal(state.submitting.value, true);
});

test('VM exports remove pagination and stale downloads never create a file', async () => {
  let resolve, context = 'a'; let config;
  const state = vm.createContext({
    downloading: { value: false }, access: { value: { read: true } }, state: { filters: { q: 'ORD1' }, error: '' },
    downloadGuard: helpers.requestGuard(() => context), reportParams: helpers.reportParams, safeError: helpers.safeError,
    scope: () => ({ enterprise_id: 'a', business_view: 'provider' }),
    props: { api: { get: (path, options) => { assert.equal(path, '/settlement-reporting/export'); config = options; return new Promise(yes => { resolve = yes; }); } } },
    URL: { createObjectURL: () => assert.fail('stale file download') },
  });
  vm.runInContext(source.slice(source.indexOf('async function download('), source.indexOf('watch(() => [context()')), state);
  const pending = state.download('details');
  assert.equal(config.params.enterprise_id, 'a'); assert.equal(config.params.page, undefined); assert.equal(config.params.kind, 'details'); assert.equal(config.responseType, 'blob');
  context = 'b'; resolve({ data: 'csv' }); await pending;
});

test('ConsoleApp integrates both settlement scopes with live auth keys and retains legacy actions', () => {
  const consoleSource = readFileSync(new URL('../src/ConsoleApp.vue', import.meta.url), 'utf8');
  const { descriptor, errors } = parse(consoleSource, { filename: 'ConsoleApp.vue' });
  assert.deepEqual(errors, []);
  const script = compileScript(descriptor, { id: 'console-finance' });
  assert.ok(script.bindings.SettlementFinanceWorkspace);
  const template = compileTemplate({ source: descriptor.template.content, filename: 'ConsoleApp.vue', id: 'console-finance', compilerOptions: { bindingMetadata: script.bindings } });
  assert.deepEqual(template.errors, []);
  const workspaces = [...consoleSource.matchAll(/<SettlementFinanceWorkspace\b[^>]+\/>/g)];
  assert.equal(workspaces.length, 2);
  for (const [markup] of workspaces) {
    for (const name of ['api', 'user', 'enterprise', 'dialogs']) assert.ok(markup.includes(`:${name}="${name}"`));
    assert.ok(markup.includes(':business-view="businessView"'));
    assert.ok(markup.includes(':key="settlementContextKey(user, enterprise, businessView, token)"'));
    assert.ok(markup.includes('@changed="loadViewData(\'settlements\')"'));
  }
  const provider = consoleSource.slice(consoleSource.indexOf(`<template v-else-if="activeView === 'settlements' && !user?.platform_role">`), consoleSource.indexOf(`<template v-else-if="activeView === 'settlements'"`));
  assert.match(provider, /<ConsoleTabs/);
  assert.match(provider, /openSettlementDetail\(item\)/);
  assert.match(provider, /SettlementFinanceWorkspace v-if="settlementTab === 'reports'"/);
  assert.match(consoleSource, /openSettlementAdjustment\(settlementDetail\)/);
  for (const tab of ['rules', 'batches', 'reconciliation', 'corrections', 'measurements']) assert.ok(consoleSource.includes(`v-${tab === 'rules' ? 'if' : 'else-if'}="settlementTab === '${tab}'"`));
  assert.doesNotMatch(consoleSource, /\/settlement-reports|退款流程尚未上线|当前版本尚未实现退款/);
  assert.match(consoleSource, /买方退款与参与方追款独立跟踪/);
});
