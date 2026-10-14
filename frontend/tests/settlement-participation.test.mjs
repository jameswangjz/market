import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { settlementSide, settlementDetailRequest, settlementContextKey, canProposeSettlement, canDecideSettlement, canLockSettlement, settlementAdjustmentItem } from '../src/settlementParticipation.js';
import { businessViewParams } from '../src/consoleBusinessView.js';

const enterprise = { id: 'provider-a' };
const provider = { id: 'provider-admin', email: 'provider@example.test', verified_status: 'verified', enterprise_memberships: [{ enterprise_id: enterprise.id, status: 'active', role: 'enterprise_admin' }] };
const platform = { id: 'finance', email: 'finance@example.test', platform_role: 'finance_settlement' };
const detail = { id: 'settlement-a', status: 'calculated', batch: { id: 'batch-a', batch_no: 'B001', status: 'pending_confirm' }, order: { id: 'order-a', order_no: 'O001' }, amounts: { gross_amount: 100, cost_amount: 20, profit_amount: 80 }, participants: [{ participant_type: 'platform', rate: 10 }, { participant_type: 'provider', rate: 90 }], proposals: [] };

test('only verified active provider admins and settlement operators can propose', () => {
  assert.equal(settlementSide(provider, enterprise, 'provider'), 'provider');
  assert.equal(canProposeSettlement(detail, provider, enterprise, 'provider'), true);
  for (const user of [{ ...provider, verified_status: 'pending' }, { ...provider, enterprise_memberships: [] }, { ...provider, enterprise_memberships: [{ ...provider.enterprise_memberships[0], role: 'member' }] }, { platform_role: 'product_manager' }]) {
    assert.equal(canProposeSettlement(detail, user, enterprise, 'provider'), false);
  }
  assert.equal(canProposeSettlement(detail, provider, enterprise, 'buyer'), false);
  assert.equal(canProposeSettlement(detail, provider, { id: 'provider-b' }, 'provider'), false);
  assert.equal(canProposeSettlement(detail, platform, enterprise, 'buyer'), true);
  for (const status of ['locked', 'paid', 'superseded']) assert.equal(canProposeSettlement({ ...detail, status }, provider, enterprise, 'provider'), false);
  for (const status of ['confirmed', 'paid', 'superseded']) assert.equal(canProposeSettlement({ ...detail, batch: { status } }, provider, enterprise, 'provider'), false);
  assert.equal(canProposeSettlement({ ...detail, can_propose: false }, provider, enterprise, 'provider'), false);
  assert.equal(canProposeSettlement({ ...detail, proposals: [{ status: 'pending' }] }, provider, enterprise, 'provider'), false);
});

test('decisions require the opposite side, pending dispute, and backend permission', () => {
  const disputed = { ...detail, status: 'disputed' };
  const proposal = { id: 'proposal', status: 'pending', proposer_side: 'platform', can_decide: true };
  assert.equal(canDecideSettlement(proposal, disputed, provider, enterprise, 'provider'), true);
  assert.equal(canDecideSettlement(proposal, disputed, platform, enterprise, 'provider'), false);
  assert.equal(canDecideSettlement({ ...proposal, proposer_side: 'provider' }, disputed, platform, enterprise, 'provider'), true);
  assert.equal(canDecideSettlement({ ...proposal, proposer_side: 'provider' }, disputed, provider, enterprise, 'provider'), false);
  assert.equal(canDecideSettlement({ ...proposal, can_decide: false }, disputed, provider, enterprise, 'provider'), false);
  assert.equal(canDecideSettlement({ ...proposal, can_decide: undefined }, disputed, provider, enterprise, 'provider'), false);
  assert.equal(canDecideSettlement({ ...proposal, status: 'accepted' }, disputed, provider, enterprise, 'provider'), false);
  assert.equal(canDecideSettlement(proposal, detail, provider, enterprise, 'provider'), false);
  assert.equal(canDecideSettlement({ status: 'pending', proposed_by: 'other-operator' }, disputed, provider, enterprise, 'provider'), false);
  assert.equal(canDecideSettlement({ status: 'pending', proposed_by: platform.email }, disputed, platform, enterprise, 'provider'), false);
});

test('enterprise detail uses scoped route, platform uses global route, and adjustment reads authoritative amounts/rates', () => {
  assert.deepEqual(settlementDetailRequest(detail, provider, enterprise, 'provider'), { url: '/enterprise-settlements/settlement-a', params: { enterprise_id: enterprise.id, business_view: 'provider' } });
  assert.deepEqual(settlementDetailRequest(detail, platform, enterprise, 'provider'), { url: '/settlements/settlement-a/detail', params: {} });
  const item = settlementAdjustmentItem(detail);
  assert.equal(item.gross_amount, 100); assert.equal(item.platform_rate, 10); assert.equal(item.provider_rate, 90);
  assert.equal(item.batch_id, 'batch-a'); assert.equal(item.order_id, 'order-a');
});

test('provider lock requires backend permission, eligible status/batch, and no pending proposal', () => {
  const item = { ...detail, status: 'adjusted', actions: { can_lock: true } };
  for (const status of ['pending', 'adjusted', 'proposal_rejected']) assert.equal(canLockSettlement({ ...item, status }, provider, enterprise, 'provider'), true);
  for (const status of ['locked', 'paid', 'superseded', 'disputed']) assert.equal(canLockSettlement({ ...item, status }, provider, enterprise, 'provider'), false);
  for (const status of ['confirmed', 'paid', 'payment_processing', 'superseded']) assert.equal(canLockSettlement({ ...item, batch: { status } }, provider, enterprise, 'provider'), false);
  assert.equal(canLockSettlement({ ...item, proposals: [{ status: 'pending' }] }, provider, enterprise, 'provider'), false);
  assert.equal(canLockSettlement({ ...item, actions: { can_lock: false } }, provider, enterprise, 'provider'), false);
  assert.equal(canLockSettlement({ ...item, actions: {} }, provider, enterprise, 'provider'), false);
  assert.equal(canLockSettlement(item, provider, enterprise, 'buyer'), false);
  assert.equal(canLockSettlement(item, { platform_role: 'security_compliance' }, enterprise, 'provider'), false);
});

const source = readFileSync(new URL('../src/ConsoleApp.vue', import.meta.url), 'utf8');
function consoleState() {
  const state = vm.createContext({ user: { value: provider }, enterprise: { value: enterprise }, businessView: { value: 'provider' }, token: { value: 'token' }, settlementDetailRequestVersion: 0, settlementDetail: { value: null }, settlementAdjusting: { value: null }, settlementSubmitting: { value: false }, settlementSide, settlementDetailRequest, businessViewParams, notify: () => assert.fail('stale notification') });
  state.settlementContext = () => settlementContextKey(state.user.value, state.enterprise.value, state.businessView.value, state.token.value);
  return state;
}

test('enterprise detail cannot repopulate the modal after a company switch or close', async () => {
  for (const switchCompany of [true, false]) {
    const state = consoleState(); let resolve;
    state.api = { get: (path, config) => { assert.equal(path, '/enterprise-settlements/settlement-a'); assert.equal(config.params.business_view, 'provider'); return new Promise(yes => { resolve = yes; }); } };
    vm.runInContext(source.slice(source.indexOf('async function openSettlementDetail('), source.indexOf('function openSettlementAdjustment(')), state);
    const pending = state.openSettlementDetail(detail);
    if (switchCompany) state.enterprise.value = { id: 'provider-b' }; else state.closeSettlementDetail();
    resolve({ data: detail }); await pending;
    assert.equal(state.settlementDetail.value, null);
  }
});

test('proposal sends current provider query scope and cannot release a new submission after switch', async () => {
  const state = consoleState(); let resolve;
  state.settlementAdjusting.value = detail;
  state.settlementAdjustForm = { value: { gross_amount: 100, cost_amount: 20, profit_amount: 80, platform_rate: 10, provider_rate: 90, service_rate: 0, expert_rate: 0, channel_rate: 0, reason: 'correct allocation' } };
  state.canProposeCurrentSettlement = item => canProposeSettlement(item, state.user.value, state.enterprise.value, state.businessView.value);
  state.api = { post: (path, payload, config) => {
    assert.equal(path, '/settlements/settlement-a/proposals'); assert.equal(payload.profit_amount, 80);
    assert.equal(config.params.enterprise_id, enterprise.id); assert.equal(config.params.business_view, 'provider');
    return new Promise(yes => { resolve = yes; });
  } };
  vm.runInContext(source.slice(source.indexOf('async function adjustSettlement('), source.indexOf('async function lockSettlement(')), state);
  const pending = state.adjustSettlement();
  state.enterprise.value = { id: 'provider-b' }; state.settlementSubmitting.value = true;
  resolve({}); await pending;
  assert.equal(state.settlementSubmitting.value, true);
});

test('lock confirmation cannot submit after switching company or opening another detail row', async () => {
  for (const switchCompany of [true, false]) {
    const state = consoleState(); let confirm;
    const item = { ...detail, status: 'adjusted', actions: { can_lock: true } };
    state.settlementDetail.value = item;
    state.canLockCurrentSettlement = row => canLockSettlement(row, state.user.value, state.enterprise.value, state.businessView.value);
    state.confirmDialog = () => new Promise(resolve => { confirm = resolve; });
    state.api = { post: () => assert.fail('stale lock submission') };
    vm.runInContext(source.slice(source.indexOf('async function lockSettlement('), source.indexOf('async function decideSettlementProposal(')), state);
    const pending = state.lockSettlement(item);
    if (switchCompany) state.enterprise.value = { id: 'provider-b' };
    else { state.settlementDetailRequestVersion++; state.settlementDetail.value = { ...item, id: 'different-row' }; }
    confirm(true); await pending;
  }
});

test('provider lock sends selected scope and refreshes only its own detail', async () => {
  const state = consoleState();
  const item = { ...detail, status: 'adjusted', can_lock: true };
  state.settlementDetail.value = item;
  state.canLockCurrentSettlement = row => canLockSettlement(row, provider, enterprise, 'provider');
  state.confirmDialog = async () => true;
  let calls = 0;
  state.notify = () => {};
  state.openSettlementDetail = async row => assert.equal(row.id, item.id);
  state.loadViewData = async view => assert.equal(view, 'settlements');
  state.api = { post: async (path, payload, config) => {
    calls++; assert.equal(path, '/settlements/settlement-a/lock');
    assert.equal(config.params.enterprise_id, enterprise.id); assert.equal(config.params.business_view, 'provider');
  } };
  vm.runInContext(source.slice(source.indexOf('async function lockSettlement('), source.indexOf('async function decideSettlementProposal(')), state);
  await state.lockSettlement(item);
  assert.equal(calls, 1); assert.equal(state.settlementSubmitting.value, false);
});
