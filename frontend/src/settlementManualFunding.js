import { cents, retryIdentity, safeError } from './settlementFinance.js';
import { canVerifyFunding, fundingAccess, fundingContext, fundingPath, fundingScope } from './settlementFunding.js';

export const manualFundingPath = '/settlement-finance/manual-funding-evidence';
export const manualStatuses = { registered: '待独立复核', verified: '人工凭据已复核', rejected: '凭据已拒绝' };
export function canReviewManual(row, context) {
  return fundingAccess(context).write && row?.status === 'registered' && row.version === 1 &&
    !!row.creator_id && row.creator_id !== context.user.id;
}
export function manualPayload(action, form, row, context, today = new Date().toISOString().slice(0, 10)) {
  if (!fundingAccess(context).write) throw new Error('仅平台清算操作者可登记及复核人工凭据');
  const text = (value, size, label) => {
    const result = String(value || '').trim();
    if (!result || result.length > size) throw new Error(`请填写有效的${label}`);
    return result;
  };
  if (action === 'review') {
    if (!canReviewManual(row, context)) throw new Error('凭据须由登记人以外的操作者独立复核');
    if (!['verified', 'rejected'].includes(form.decision)) throw new Error('请选择复核结果');
    return { expected_version: 1, decision: form.decision, note: text(form.note, 4000, '复核依据') };
  }
  if (action === 'verify') {
    if (!canVerifyFunding(row, context) || form.evidence?.status !== 'verified' ||
      form.evidence.version !== 2 || form.evidence.bank_receipt_verified !== false ||
      form.evidence.funding_request_id !== row.id || cents(form.evidence.amount) !== cents(row.amount) ||
      form.evidence.currency !== row.currency || !form.evidence.id) throw new Error('请选择该提案已经独立复核的人工凭据');
    return { expected_version: 2, receipt_reference: `manual:${form.evidence.id}` };
  }
  if (action !== 'register' || row?.kind !== 'external_receipt' || row.status !== 'approved' || row.version !== 2)
    throw new Error('请选择已批准、尚未覆盖的外部补差提案');
  const amount = cents(row.amount);
  if (amount <= 0n) throw new Error('凭据金额须等于完整资金缺口');
  const paid = String(form.paid_on || '');
  const date = new Date(`${paid}T00:00:00Z`);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(paid) || !Number.isFinite(date.getTime()) ||
    date.toISOString().slice(0, 10) !== paid || paid > today) throw new Error('付款日期须为有效日期且不能晚于今天');
  const refs = String(form.evidence_refs || '').split('\n').map(value => value.trim()).filter(Boolean);
  if (!refs.length || refs.length > 100 || refs.some(value => value.length > 240)) throw new Error('请填写 1 至 100 条证据引用，每条最多 240 字符');
  return { funding_request_id: row.id, amount: `${amount / 100n}.${String(amount % 100n).padStart(2, '0')}`,
    currency: row.currency, reference: text(form.reference, 240, '付款凭据引用'), paid_on: paid,
    evidence_refs: refs, note: text(form.note, 4000, '登记说明') };
}
export function manualError(error) {
  const labels = {
    different_manual_funding_reviewer_required: '登记人不能复核自己的付款凭据。',
    manual_funding_exact_gap_required: '人工凭据金额必须等于该提案的完整资金缺口。',
    manual_funding_request_unavailable: '补差提案已变化或不再接受人工凭据，请刷新。',
    manual_funding_basis_mismatch: '最终基线、承担主体或缺口已变化，请重新核对。',
    manual_funding_conflict_refresh_required: '该提案或付款引用已有凭据，请刷新后核对。',
    manual_funding_date_invalid: '付款日期无效或晚于今天。',
    manual_funding_evidence_required: '请补充有效付款证据引用。',
    manual_funding_review_version_conflict: '凭据已复核或版本变化，请刷新。',
    manual_funding_evidence_mismatch: '人工凭据与最终基线不匹配，请重新核对。',
  };
  const detail = error?.response?.data?.detail;
  return typeof detail === 'string' && Object.hasOwn(labels, detail) ? labels[detail] : safeError(error);
}
export function createManualSession(getContext, state) {
  let epoch = 0, reads = 0, disposed = false, writing = false, identity = retryIdentity();
  function ticket() { const generation = epoch, key = fundingContext(getContext()); return () => !disposed && epoch === generation && key === fundingContext(getContext()); }
  function reset() { epoch++; reads++; Object.assign(state, { items: [], requests: [], busy: false, saving: false, error: '' }); }
  async function load() {
    const context = getContext();
    if (disposed || !fundingAccess(context).read) { reset(); return; }
    const valid = ticket(), read = ++reads, current = () => valid() && read === reads;
    state.busy = true; state.error = '';
    try {
      const params = fundingScope(context);
      const requests = await context.api.get(fundingPath, { params });
      if (!current()) return;
      const evidence = await context.api.get(manualFundingPath, { params });
      if (current()) Object.assign(state, { requests: requests.data.items || [], items: evidence.data.items || [] });
    } catch (error) { if (current()) state.error = manualError(error); }
    finally { if (current()) state.busy = false; }
  }
  async function submit(action, form, row) {
    if (disposed || writing || !fundingAccess(getContext()).write) return false;
    const current = ticket(), context = getContext(); writing = true; state.saving = true; state.error = '';
    try {
      const payload = manualPayload(action, form, row, context);
      const path = action === 'register' ? manualFundingPath : action === 'review' ?
        `${manualFundingPath}/${encodeURIComponent(row.id)}/review` : `${fundingPath}/${encodeURIComponent(row.id)}/verify`;
      await context.api.post(path, { ...payload, request_id: identity([fundingContext(context), path, payload]) });
      if (!current()) return false;
      identity = retryIdentity(); return true;
    } catch (error) { if (current()) state.error = manualError(error); return false; }
    finally { writing = false; if (current()) state.saving = false; }
  }
  return { load, submit, reset, dispose() { disposed = true; reset(); } };
}
