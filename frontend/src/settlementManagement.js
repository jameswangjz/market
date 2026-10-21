import { activeEnterpriseMembership } from './consoleBusinessView.js';
import { cents, requestGuard, retryIdentity, safeError } from './settlementFinance.js';

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
function references(value) { return (Array.isArray(value) ? value : String(value || '').split(/\r?\n/)).map(s => String(s).trim()).filter(Boolean); }
function decimalCents(value) { return `${value < 0n ? '-' : ''}${(value < 0n ? -value : value) / 100n}.${String((value < 0n ? -value : value) % 100n).padStart(2, '0')}`; }
export function costSourceLabel(source, reportRows = []) {
  const id = source.baseline_id || source.reference_settlement_id;
  const basis = source.basis || source;
  const row = reportRows.find(r => r.id === id && (!basis.baseline_hash || r.baseline_hash === basis.baseline_hash));
  return {
    order_no: basis.source_order_no || basis.order_no || row?.order_no || '',
    settlement_no: basis.reference_settlement_no || basis.settlement_no || row?.settlement_no || '',
    batch_no: basis.reference_batch_no || basis.reference_batch_nos?.join(' / ') || basis.batch_no || basis.batch_nos?.join(' / ') || row?.batch_no || '',
    version_name: basis.product_version_name || basis.source_version_name || '',
  };
}
export function costExceptionSources(refund, reportRows = [], discovery = null) {
  if (arguments.length >= 3) {
    if (!discovery || typeof discovery !== 'object') throw new Error('服务端未提供可信的原始退款依据');
    const funded = discovery.basis_kind === 'funded';
    const ordinary = discovery.basis_kind === 'single' && discovery.basis_origin === 'raw_single_basis' && discovery.kind === 'ordinary_single_refund' ||
      discovery.basis_kind === 'multi' && discovery.basis_origin === 'raw_multi_basis' && discovery.kind === 'multi_source_refund';
    if (discovery.refund_id !== refund.id || discovery.order_id !== refund.order_id || discovery.status !== 'ready' ||
        discovery.proposal_allowed !== true || (funded ? discovery.kind !== 'confirmed_funded_refund' || discovery.basis_origin !== 'raw_funded_basis' :
          !ordinary)) {
      throw new Error('原始退款依据未就绪或当前不允许提案，请刷新后重试');
    }
    const refs = discovery.references;
    if (!Array.isArray(refs) || !refs.length) throw new Error('服务端未提供完整原始退款来源');
    if (!funded && (discovery.kind === 'ordinary_single_refund' ? refs.length !== 1 : refs.length < 2)) throw new Error('普通退款来源数量与依据类型不一致');
    const seen = new Set(), baselines = new Set();
    return refs.map(ref => {
      if (!ref.segment_id || !ref.reference_settlement_id || !/^[a-f0-9]{64}$/.test(ref.baseline_hash || '') ||
          ref.source_order_id !== refund.order_id || ref.display_status === 'unavailable' || ref.currency !== 'CNY' ||
          seen.has(ref.segment_id) || baselines.has(ref.reference_settlement_id)) throw new Error('原始退款来源身份不完整或重复');
      seen.add(ref.segment_id); baselines.add(ref.reference_settlement_id);
      if (!/^-\d+\.\d{2}$/.test(String(ref.cost_amount)) && ref.cost_amount !== '0.00') throw new Error('原始退款成本方向或精度无效');
      const cost = ref.cost_amount.startsWith('-') ? cents(ref.cost_amount.slice(1)) : 0n;
      return { segment_id: ref.segment_id, baseline_id: ref.reference_settlement_id, baseline_hash: ref.baseline_hash,
        basis_kind: funded ? 'funded' : refs.length > 1 ? 'multi' : 'single',
        max_retained_cost: decimalCents(cost), ...costSourceLabel(ref, reportRows) };
    });
  }
  const matches = reportRows.filter(r => r.refund_id === refund.id && r.order_id === refund.order_id);
  const multi = matches.filter(r => Array.isArray(r.economic_segments) && r.economic_segments.length > 1);
  if (!multi.length) return [];
  if (multi.length !== 1) throw new Error('退款来源不唯一，请刷新并核对');
  const seen = new Set(), baselines = new Set();
  return multi[0].economic_segments.map(ref => {
    if (!ref.segment_id || !ref.reference_settlement_id || !ref.baseline_hash || ref.display_status === 'unavailable' || ref.source_order_id !== refund.order_id || seen.has(ref.segment_id) || baselines.has(ref.reference_settlement_id)) throw new Error('退款来源身份不完整或重复，不能提交分段例外');
    seen.add(ref.segment_id); baselines.add(ref.reference_settlement_id);
    if (ref.currency && ref.currency !== 'CNY') throw new Error('成本例外来源币种不一致或暂不支持');
    const text = String(ref.cost_amount);
    const cost = text.startsWith('-') ? -cents(text.slice(1)) : cents(text);
    if (cost > 0n) throw new Error('退款来源成本方向无效');
    return { segment_id: ref.segment_id, baseline_id: ref.reference_settlement_id, baseline_hash: ref.baseline_hash,
      max_retained_cost: decimalCents(-cost), ...costSourceLabel(ref, reportRows) };
  });
}
export function costExceptionTotal(rows) {
  return decimalCents(rows.reduce((sum, row) => sum + cents(row.retained_cost), 0n));
}
export function costExceptionError(error) {
  const detail = error?.response?.data?.detail;
  const code = typeof detail === 'string' ? detail : detail?.code || detail?.reason;
  const labels = {
    all_source_allocations_required: '必须提交全部经济段的金额及证据，不能遗漏来源。',
    duplicate_source_allocation: '经济段来源重复，请重新选择退款对象。',
    source_evidence_refs_required: '每个经济段都必须提供证据引用。',
    retained_cost_total_mismatch: '整单保留成本与逐段合计不符，请重新核对。',
    multi_refund_source_identity_mismatch: '来源或最终基线已变化，请重新选择退款并核对证据。',
    multi_refund_basis_changed: '退款分段依据已变化，请刷新后重新申请或审批。',
    ready_multi_refund_basis_required: '尚无完整且可信的多段退款基线，暂不能申请成本例外。',
    raw_multi_refund_basis_unavailable: '多段原始退款依据尚不可用，请稍后刷新。',
    multi_refund_source_not_final: '来源清算尚未最终确认，暂不能申请成本例外。',
  };
  return Object.hasOwn(labels, code) ? labels[code] : safeError(error);
}
export function costExceptionPayload(form, sources = []) {
  if (!form.refund_id?.trim() || !form.bearer_participant_id || !form.reason?.trim()) throw new Error('请填写退款编号、承担主体和申请依据');
  if (sources.length) {
    const funded = sources.every(s => s.basis_kind === 'funded');
    const single = sources.length === 1 && sources[0].basis_kind === 'single';
    if ((!funded && !single && sources.length < 2) || sources.some(s => s.basis_kind === 'funded') !== funded ||
        !Array.isArray(form.source_allocations) || form.source_allocations.length !== sources.length) throw new Error('必须填写全部经济段的金额及证据');
    const seen = new Set();
    const source_allocations = form.source_allocations.map(row => {
      const source = sources.find(s => s.segment_id === row.segment_id && s.baseline_id === row.baseline_id);
      if (!source || seen.has(row.segment_id)) throw new Error('经济段来源已变化或重复，请重新选择退款');
      seen.add(row.segment_id);
      const amount = cents(row.retained_cost);
      if (amount < 0n || amount > cents(source.max_retained_cost)) throw new Error('逐段保留成本不得为负数或超过本段上限');
      const evidence_refs = references(row.evidence_refs);
      if (!evidence_refs.length) throw new Error('每个经济段均须提供证据引用（包括保留成本为零的段）');
      return { segment_id: source.segment_id, baseline_id: source.baseline_id, retained_cost: decimalCents(amount), evidence_refs };
    }).sort((a, b) => a.baseline_id.localeCompare(b.baseline_id) || a.segment_id.localeCompare(b.segment_id));
    const retained_cost = costExceptionTotal(source_allocations);
    if (cents(retained_cost) <= 0n) throw new Error('整张提案保留成本合计须大于零');
    if (single) return { refund_id: form.refund_id.trim(), bearer_participant_id: form.bearer_participant_id,
      retained_cost, reason: form.reason.trim(), evidence_refs: source_allocations[0].evidence_refs };
    return { schema_version: 2, refund_id: form.refund_id.trim(), bearer_participant_id: form.bearer_participant_id,
      ...(funded ? { basis_kind: 'funded' } : {}),
      retained_cost, reason: form.reason.trim(), evidence_refs: [...new Set(source_allocations.flatMap(a => a.evidence_refs))].sort(), source_allocations };
  }
  if (form.source_allocations?.length) throw new Error('尚未取得可信的分段来源，不能提交');
  if (cents(form.retained_cost) <= 0n) throw new Error('保留成本须大于零');
  const evidence_refs = references(form.evidence_refs);
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
