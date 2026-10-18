import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { compile, createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';
import * as helpers from '../src/settlementFinance.js';
import { canAccessConsoleView, canViewOwnEarnings } from '../src/consolePermissions.js';

const user = { id: 'expert-user', verified_status: 'verified', is_active: true, activation_status: 'active', enterprise_memberships: [] };
const identity = { participant_id: 'expert-own', participant_type: 'expert', participant_name: '本人专家', currency: 'CNY', enterprise_id: '' };
const source = readFileSync(new URL('../src/SettlementFinanceWorkspace.vue', import.meta.url), 'utf8');
const consoleSource = readFileSync(new URL('../src/ConsoleApp.vue', import.meta.url), 'utf8');
function report() {
  const row = { position_id: 'own-position', settlement_no: 'SET-OWN', participant_id: identity.participant_id, participant_type: identity.participant_type, participant_name: identity.participant_name,
    product_name: '收益关联产品', provider_name: '提供方', direction: 'payable', source_kind: 'original', currency: 'CNY', profit_share: '20.00', total_receivable: '20.00', paid_amount: '5.00', unpaid_amount: '15.00',
    cost_amount: 'PRIVATE-COST', platform_fee: 'PRIVATE-PLATFORM', bank_account: 'PRIVATE-BANK', order_no: 'PRIVATE-ORDER', participants: [{ participant_id: 'other', amount: 'PRIVATE-OTHER' }] };
  return { scope: { kind: 'beneficiary', ...identity }, summary: { ...row, income_order_count: 1, refund_count: 0, adjustment_count: 0 },
    items: [row], monthly: [{ month: '2026-10', ...row }], products: [row], providers: [row], participants: [row], rankings: [{ amount: 'PRIVATE-RANK' }], total: 1, page: 1, page_size: 20 };
}
test('own reporting grants never imply settlement listing or financial writes', () => {
  assert.equal(canViewOwnEarnings(user, [identity]), true);
  assert.equal(canAccessConsoleView(user, null, 'earnings', [identity]), true);
  assert.equal(canAccessConsoleView(user, null, 'settlements', [identity]), false);
  assert.equal(canAccessConsoleView(user, null, 'earnings'), false);
  assert.equal(canViewOwnEarnings({ ...user, verified_status: 'pending' }, [identity]), false);
  assert.equal(canViewOwnEarnings({ ...user, is_active: false }, [identity]), false);
  assert.deepEqual(helpers.financeAccess(user, null, 'beneficiary', identity), { read: true, write: false, platform: false });
  assert.deepEqual(helpers.financeScope(user, null, 'beneficiary', identity), { business_view: 'beneficiary', participant_id: 'expert-own' });
  assert.deepEqual(helpers.financeScope(user, { id: 'owned-enterprise' }, 'beneficiary', identity), { business_view: 'beneficiary', participant_id: 'expert-own', enterprise_id: 'owned-enterprise' });
  assert.throws(() => helpers.financeScope(user, null, 'provider'));
});
test('own-only response projection removes costs, other shares, order identity, banking and rankings', () => {
  const clean = helpers.ownBeneficiaryReport(report(), identity);
  assert.doesNotMatch(JSON.stringify(clean), /PRIVATE|cost_amount|platform_fee|bank_account|order_no/);
  assert.equal(clean.summary.profit_share, '20.00'); assert.equal(clean.items[0].unpaid_amount, '15.00');
  assert.deepEqual(clean.rankings, []);
  for (const modified of [{ kind: 'platform' }, { participant_id: 'other' }, { participant_type: 'channel' }, { currency: 'USD' }]) {
    const data = report(); Object.assign(data.scope, modified); assert.throws(() => helpers.ownBeneficiaryReport(data, identity));
  }
  const foreign = report(); foreign.items[0].participant_id = 'other'; assert.throws(() => helpers.ownBeneficiaryReport(foreign, identity));
});
test('discovery uses only the authorized identity selector and discards stale enterprise/auth results', async () => {
  let context = { user, enterpriseId: '', token: 'first' }, release;
  const state = { items: [], selectedId: '', error: '', busy: false }, calls = [];
  const discovery = helpers.createBeneficiaryDiscovery({ get: (path, { params }) => {
    calls.push({ path, params }); return new Promise(resolve => { release = resolve; });
  } }, state, () => context);
  const pending = discovery.load(); assert.equal(discovery.load(), pending);
  assert.equal(calls[0].path, '/settlement-reporting/beneficiaries'); assert.deepEqual(calls[0].params, {});
  context = { ...context, token: 'new', enterpriseId: 'other-enterprise' };
  release({ data: { items: [identity] } }); await pending; assert.deepEqual(state.items, []);
  const next = discovery.load(); assert.deepEqual(calls[1].params, { enterprise_id: 'other-enterprise' });
  release({ data: { items: [identity, { participant_id: 'provider', participant_type: 'provider', currency: 'CNY' }] } }); await next;
  assert.equal(state.items.length, 1); assert.equal(state.selectedId, identity.participant_id);
  const closing = discovery.load(); discovery.dispose(); release({ data: { items: [identity] } }); await closing; assert.deepEqual(state.items, []);
});
test('discovery denies errors and inactive users without granting unrelated navigation', async () => {
  const state = {}, context = { user, enterpriseId: '', token: 'token' };
  let calls = 0;
  const discovery = helpers.createBeneficiaryDiscovery({ get: async () => { calls++; throw new Error('Network Error'); } }, state, () => context);
  await discovery.load(); assert.deepEqual(state.items, []); assert.equal(canViewOwnEarnings(user, state.items), false);
  assert.match(state.error, /网络/);
  context.user = { ...user, is_active: false }; await discovery.load(); assert.equal(calls, 1);
});
test('beneficiary columns, summaries and tabs expose only own report fields', async () => {
  const data = helpers.ownBeneficiaryReport(report(), identity);
  const state = vm.createContext({ ...helpers, computed: getter => ({ get value() { return getter(); } }), isBeneficiary: { value: true }, reportView: { value: 'details' }, tab: { value: 'reports' }, labels: {} });
  vm.runInContext(source.slice(source.indexOf('const columns = computed('), source.indexOf('async function load()')) + '\nglobalThis.ownColumns = columns.value;', state);
  const keys = state.ownColumns.map(([key]) => key);
  assert.ok(keys.includes('profit_share')); assert.ok(keys.includes('paid_amount'));
  for (const key of ['cost_amount', 'gross_amount', 'platform_fee', 'provider_share', 'order_no']) assert.equal(keys.includes(key), false);
  const html = await renderToString(createSSRApp({ setup: () => ({ result: data, summaryAmountLabels: helpers.beneficiaryAmountLabels, money: helpers.money }),
    render: compile('<dl><div v-for="(label, key) in summaryAmountLabels"><dt>{{ label }}</dt><dd>{{ money(result.summary[key]) }}</dd></div></dl>') }));
  assert.match(html, /本主体利润分成/); assert.doesNotMatch(html, /成本|平台分成|PRIVATE/);
  assert.match(source, /isBeneficiary.value \? tabs.filter\(item => item.key === 'reports'\)/);
  assert.match(source, /isBeneficiary.value \? reportViews.filter\(\(\[key\]\) => key !== 'rankings'\)/);
});
test('earnings route uses separate capability and never loads enterprise settlements', async () => {
  assert.match(consoleSource, /key: "earnings", label: "我的收益"/);
  assert.match(consoleSource, /canAccessConsoleView\(user, enterprise, 'earnings', beneficiaryState.items\)/);
  const earnings = consoleSource.slice(consoleSource.indexOf(`<template v-else-if="activeView === 'earnings'`), consoleSource.indexOf(`<template v-else-if="activeView === 'settlements' &&`));
  assert.match(earnings, /business-view="beneficiary"/); assert.match(earnings, /:beneficiary="selectedBeneficiary"/);
  assert.doesNotMatch(earnings, /openSettlementDetail|settlementItems|@changed/);
  const context = vm.createContext({ visibleNav: { value: [{ key: 'earnings' }] }, api: { get: () => assert.fail('earnings must not call legacy settlement lists') } });
  const fn = consoleSource.slice(consoleSource.indexOf('async function loadViewData('), consoleSource.indexOf('  const contextEpoch = businessContextEpoch;', consoleSource.indexOf('async function loadViewData('))) + '\n}';
  vm.runInContext(fn, context); await context.loadViewData('earnings');
});
test('beneficiary exports stay own-scoped and block annual snapshots and rankings', async () => {
  let resolve, config;
  const state = vm.createContext({
    downloading: { value: false }, access: { value: { read: true } }, isBeneficiary: { value: true }, state: { filters: helpers.monthRange(), error: '' },
    downloadGuard: helpers.requestGuard(() => 'own'), reportParams: helpers.reportParams, safeError: helpers.safeError,
    scope: () => helpers.financeScope(user, null, 'beneficiary', identity),
    props: { api: { get: (path, options) => { config = options; return new Promise(yes => { resolve = yes; }); } } },
  });
  vm.runInContext(source.slice(source.indexOf('async function download('), source.indexOf('watch(() => [context()')), state);
  await state.download('rankings'); await state.download('', { id: 'annual' }); assert.equal(config, undefined);
  const pending = state.download('details');
  assert.equal(config.params.participant_id, identity.participant_id); assert.equal(config.params.business_view, 'beneficiary');
  assert.equal(config.params.page, undefined); state.downloadGuard.dispose(); resolve({ data: 'csv' }); await pending;
});
