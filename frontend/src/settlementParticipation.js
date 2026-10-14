import { activeEnterpriseMembership, businessViewParams } from './consoleBusinessView.js';

const operators = ['super_admin', 'platform_operator', 'finance_settlement'];
export function settlementSide(user, enterprise, mode) {
  if (user?.platform_role) return operators.includes(user.platform_role) ? 'platform' : '';
  const membership = activeEnterpriseMembership(user, enterprise?.id);
  return user?.verified_status === 'verified' && mode === 'provider' &&
    ['super_admin', 'enterprise_admin'].includes(membership?.role) ? 'provider' : '';
}

export function settlementContextKey(user, enterprise, mode, token) {
  return JSON.stringify([token, user?.id, user?.platform_role, enterprise?.id, mode,
    user?.verified_status, activeEnterpriseMembership(user, enterprise?.id)?.role]);
}

export function settlementDetailRequest(item, user, enterprise, mode) {
  return { url: user?.platform_role ? `/settlements/${encodeURIComponent(item.id)}/detail` :
    `/enterprise-settlements/${encodeURIComponent(item.id)}`, params: businessViewParams(user, enterprise, mode) };
}

export function canProposeSettlement(item, user, enterprise, mode) {
  if (!settlementSide(user, enterprise, mode) || !item || ['locked', 'paid', 'superseded'].includes(item.status)) return false;
  if (item.can_propose === false || item.actions?.can_propose === false) return false;
  const batchStatus = item.batch?.status || item.batch_status;
  if (batchStatus && !['generated', 'pending_confirm'].includes(batchStatus)) return false;
  return !item.proposals?.some(proposal => proposal.status === 'pending');
}

export function canDecideSettlement(proposal, detail, user, enterprise, mode) {
  const side = settlementSide(user, enterprise, mode);
  if (!side || proposal.status !== 'pending' || detail?.status !== 'disputed' ||
      proposal.can_decide === false || proposal.actions?.can_decide === false) return false;
  const permission = proposal.actions?.can_decide ?? proposal.can_decide;
  if (side === 'provider' && permission !== true) return false;
  const batchStatus = detail.batch?.status;
  if (batchStatus && !['generated', 'pending_confirm'].includes(batchStatus)) return false;
  const identity = proposal.values?._proposer;
  const proposerSide = proposal.proposer_side || identity?.side;
  if (identity?.user_id === user.id || proposal.proposed_by === (user.email || user.name)) return false;
  // Historical platform proposals retain the backend's different-operator rule.
  return proposerSide ? proposerSide !== side : side === 'platform' && !!proposal.proposed_by;
}

export function canLockSettlement(item, user, enterprise, mode) {
  const side = settlementSide(user, enterprise, mode);
  if (!side || !item || !['pending', 'adjusted', 'proposal_rejected'].includes(item.status) ||
      item.proposals?.some(proposal => proposal.status === 'pending')) return false;
  const permission = item.actions?.can_lock ?? item.can_lock;
  if (permission === false || (side === 'provider' && permission !== true)) return false;
  const batchStatus = item.batch?.status || item.batch_status;
  return side === 'provider' ? ['generated', 'pending_confirm'].includes(batchStatus) :
    !batchStatus || ['generated', 'pending_confirm'].includes(batchStatus);
}

export function settlementAdjustmentItem(detail) {
  const result = { ...detail, ...detail.amounts, batch_id: detail.batch?.id || detail.batch_id || '',
    batch_no: detail.batch?.batch_no || detail.batch_no || '', order_id: detail.order?.id || detail.order_id,
    order_no: detail.order?.order_no || detail.order_no, buyer_name: detail.order?.buyer_name || detail.buyer_name };
  for (const participant of detail.participants || []) result[`${participant.participant_type}_rate`] = participant.rate;
  return result;
}
