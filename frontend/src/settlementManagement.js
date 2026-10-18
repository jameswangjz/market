import { activeEnterpriseMembership } from './consoleBusinessView.js';
import { cents, requestGuard, retryIdentity } from './settlementFinance.js';

export const partyKinds = { platform: '平台', provider: '提供方', service: '数据服务方', expert: '专家', channel: '渠道' };
export function managementSide(user, enterprise, view) {
  if (!user?.id || user.is_active === false || user.activation_status && user.activation_status !== 'active' || view === 'beneficiary') return '';
  if (user.platform_role) return ['super_admin', 'platform_operator', 'finance_settlement'].includes(user.platform_role) ? 'platform' : '';
  const m = activeEnterpriseMembership(user, enterprise?.id);
  const roles = Array.isArray(m?.business_roles) ? m.business_roles : String(m?.business_roles || '').split(',');
  return view === 'provider' && user.verified_status === 'verified' && enterprise?.verification_status === 'verified' &&
    ['super_admin', 'enterprise_admin'].includes(m?.role) && roles.includes('provider') ? 'provider' : '';
}
export function canDecideCostException(row, user, side) {
  return !!side && row?.status === 'pending' && row.version === 1 && !!row.creator_id &&
    row.creator_id !== user?.id && ['platform', 'provider'].includes(row.creator_side) && row.creator_side !== side;
}
export function canFinalizeAnnual(row, user, writable) {
  return !!writable && row?.status === 'draft' && !!row.actor_id && row.actor_id !== user?.id && /^[a-f0-9]{64}$/.test(row.digest || '');
}
export function managementDirectory(user, kind, query, page, pageSize = 100) {
  if (user.platform_role === 'finance_settlement') return { path: '/settlement-reporting/report', params: { start: '1900-01-01', end: '9998-12-31', q: query, ...(kind === 'refunds' ? { source_kind: 'refund' } : {}), page, page_size: pageSize } };
  return { path: kind === 'products' ? '/products' : '/refunds', params: { [kind === 'products' ? 'q' : 'refund_id']: query, page, page_size: pageSize } };
}
export function allocationPayload(rows, parties) {
  const seen = new Set(), totals = new Map();
  const result = rows.map(row => {
    const party = parties.find(p => p.id === row.participant_id);
    if (!party || !Object.hasOwn(partyKinds, party.kind) || seen.has(party.id)) throw new Error('请选择有效且不重复的收益主体');
    seen.add(party.id);
    const weight = String(row.weight).trim();
    if (!/^\d+(?:\.\d{1,8})?$/.test(weight)) throw new Error('权重最多保留八位小数');
    const [whole, fraction = ''] = weight.split('.');
    const value = BigInt(whole) * 100000000n + BigInt(fraction.padEnd(8, '0'));
    if (value <= 0n || value > 10000000000n) throw new Error('权重须大于零且不超过100');
    totals.set(party.kind, (totals.get(party.kind) || 0n) + value);
    return { participant_id: party.id, participant_type: party.kind, weight };
  });
  if ([...totals.values()].some(total => total !== 10000000000n)) throw new Error('每个收益类别的权重须分别合计100%');
  return result;
}
export function allocationCandidates(parties, configuration) {
  const result = new Map(parties.map(p => [p.id, p]));
  for (const row of configuration?.allocations || []) {
    if (!result.has(row.participant_id) && Object.hasOwn(partyKinds, row.participant_type)) result.set(row.participant_id, {
      id: row.participant_id, name: row.participant_name, kind: row.participant_type, currency: row.currency, configuredOnly: true,
    });
  }
  return [...result.values()];
}
export function costExceptionPayload(form) {
  if (!form.refund_id?.trim() || !form.bearer_participant_id || !form.reason?.trim()) throw new Error('请填写退款编号、承担主体和申请依据');
  if (cents(form.retained_cost) <= 0n) throw new Error('保留成本须大于零');
  const evidence_refs = String(form.evidence_refs || '').split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  if (!evidence_refs.length) throw new Error('至少填写一条证据引用');
  return { refund_id: form.refund_id.trim(), bearer_participant_id: form.bearer_participant_id, retained_cost: String(form.retained_cost).trim(), reason: form.reason.trim(), evidence_refs };
}

// Each editor has independent read/write tickets; retries retain the same payload key.
export function managementSession(api, context, scope, allowed) {
  const reads = requestGuard(context), writes = requestGuard(context);
  let busy = false;
  const identity = retryIdentity();
  return {
    async read(path, apply) {
      if (!allowed()) return;
      const current = reads.begin();
      try { const { data } = await api.get(path, { params: scope() }); if (current() && allowed()) apply(data); }
      catch (error) { if (current()) throw error; }
    },
    async write(path, payload, apply) {
      if (busy || !allowed()) return false;
      busy = true; const current = writes.begin();
      try {
        const request_id = identity({ context: context(), path, payload });
        const { data } = await api.post(path, { ...payload, request_id }, { params: scope() });
        if (current() && allowed()) { apply(data); return true; }
        return false;
      } catch (error) { if (current()) throw error; return false; }
      finally { if (current()) busy = false; }
    },
    reset() { reads.invalidate(); writes.invalidate(); busy = false; },
    dispose() { reads.dispose(); writes.dispose(); busy = false; },
  };
}
