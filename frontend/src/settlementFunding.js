import { activeEnterpriseMembership } from './consoleBusinessView.js';
import { cents, retryIdentity, safeError } from './settlementFinance.js';

export const fundingPath = '/settlement-finance/funding-adjustments';
const operators = ['super_admin', 'platform_operator', 'finance_settlement'];
export const fundingKinds = { external_receipt: '外部回执', platform_absorption: '平台利润承担' };
export const fundingStatuses = { proposed: '待审批', approved: '待核验', rejected: '已拒绝', covered: '缺口已覆盖' };

export function fundingAccess({ user, enterprise, businessView }) {
  if (!user?.id || user.is_active !== true || user.activation_status && user.activation_status !== 'active') return { read: false, write: false };
  const tenant = businessView === 'provider' || !user.platform_role && !!enterprise?.id;
  if (!tenant && operators.includes(user.platform_role)) return { read: true, write: true };
  if (!tenant && user.platform_role === 'security_compliance') return { read: true, write: false };
  const membership = activeEnterpriseMembership(user, enterprise?.id);
  const roles = Array.isArray(membership?.business_roles) ? membership.business_roles : String(membership?.business_roles || '').split(',');
  return { read: !!enterprise?.id && businessView === 'provider' && user.verified_status === 'verified' &&
    enterprise.verification_status === 'verified' && ['super_admin', 'enterprise_admin'].includes(membership?.role) && roles.includes('provider'), write: false };
}
export function fundingScope(context) {
  if (!fundingAccess(context).read) throw new Error('当前身份无资金补足查看权限');
  return context.businessView === 'provider' || !context.user?.platform_role ?
    { enterprise_id: context.enterprise.id, business_view: 'provider' } : {};
}
export function fundingContext(context) {
  return JSON.stringify([context.user, context.enterprise, context.businessView,
    context.api?.defaults?.headers?.common?.Authorization || context.api?.defaults?.headers?.Authorization || '']);
}
export function canDecideFunding(row, context) {
  return fundingAccess(context).write && row?.status === 'proposed' && row.version === 1 &&
    !!row.creator_id && String(row.creator_id) !== String(context.user.id);
}
export function canVerifyFunding(row, context) {
  return fundingAccess(context).write && row?.status === 'approved' && row.version === 2 &&
    !!row.creator_id && String(row.creator_id) !== String(context.user.id) && row.can_verify !== false &&
    !row.verification_unavailable_reason && (row.kind === 'platform_absorption' ||
      row.kind === 'external_receipt' && row.can_verify === true && row.trusted_receipt_verifier_available === true);
}
function text(value, max, label) {
  const result = String(value || '').trim();
  if (!result || result.length > max) throw new Error(`请填写有效的${label}（最多 ${max} 字符）`);
  return result;
}
export function fundingPayload(action, form, row, context) {
  if (!fundingAccess(context).write) throw new Error('仅平台清算操作者可登记资金补足');
  if (action === 'propose') {
    if (form.currency && form.currency !== 'CNY') throw new Error('当前资金补足仅支持 CNY');
    if (!Object.hasOwn(fundingKinds, form.kind)) throw new Error('请选择资金补足方式');
    const amount = cents(form.amount);
    if (amount <= 0n || amount > 99999999999999n) throw new Error('补足金额须为有效的正金额，且等于完整缺口');
    const refs = String(form.evidence_refs || '').split('\n').map(value => value.trim()).filter(Boolean);
    if (!refs.length || refs.length > 100 || refs.some(value => value.length > 240)) throw new Error('请填写 1 至 100 条证据引用，每条最多 240 字符');
    const body = { settlement_id: text(form.settlement_id, 36, '清算单编号'), kind: form.kind,
      amount: `${amount / 100n}.${String(amount % 100n).padStart(2, '0')}`, currency: 'CNY',
      reason: text(form.reason, 4000, '补足依据'), evidence_refs: refs };
    if (form.kind === 'external_receipt') body.bearer_enterprise_id = text(form.bearer_enterprise_id, 36, '承担企业编号');
    return body;
  }
  if (action === 'decision') {
    if (!canDecideFunding(row, context)) throw new Error('须由不同操作者审批待审批提案');
    if (!['approved', 'rejected'].includes(form.decision)) throw new Error('请选择审批结果');
    return { expected_version: 1, decision: form.decision, note: text(form.note, 4000, '审批依据') };
  }
  if (action !== 'verify' || !canVerifyFunding(row, context)) throw new Error('当前提案不可核验，需独立操作者及有效核验依据');
  return { expected_version: 2, ...(row.kind === 'external_receipt' ?
    { receipt_reference: text(form.receipt_reference, 240, '回执引用') } : {}) };
}
export function fundingError(error) {
  const labels = {
    trusted_funding_receipt_verifier_unavailable: '可信外部回执核验器未配置，不能确认到账或完成外部回执核验。',
    platform_absorption_exceeds_proven_available_funds: '已有平台利润或实际收款不足，不能承担此缺口。',
    different_reviewer_required: '提案人不能审批自己的提案。', different_verifier_required: '提案人不能核验自己的提案。',
    funding_version_conflict: '提案状态已变化，请刷新后核对。',
    funding_gap_mismatch_or_already_covered: '金额须等于完整资金缺口，且缺口尚未覆盖。',
    approved_unfunded_request_required: '仅已批准且未覆盖的提案可以核验。',
    verified_funding_receipt_mismatch: '可信回执与金额、币种或承担主体不一致。',
    locked_funding_baseline_required: '需要已锁定的最终清算基线。',
    funding_snapshot_mismatch: '清算基线或缺口已变化，请重新核对。',
  };
  return labels[error?.response?.data?.detail] || safeError(error);
}

export function eligibleFundingSettlement(row) {
  try { return !!row?.id && !!row.settlement_no && row.status === 'locked' && !!row.baseline_hash && cents(row.gross_amount) > 0n; }
  catch { return false; }
}
export function createFundingDirectory(getContext, state) {
  let sequence = 0, disposed = false;
  function reset() { sequence++; Object.assign(state, { items: [], total: 0, page: 1, pageSize: 100, busy: false, error: '' }); }
  async function load(q = '', page = 1, pageSize = 100) {
    if (disposed || !fundingAccess(getContext()).write) return;
    if (!Number.isInteger(page) || page < 1 || !Number.isInteger(pageSize) || pageSize < 1 || pageSize > 100) return;
    const id = ++sequence, context = getContext(), key = fundingContext(context);
    const current = () => !disposed && sequence === id && key === fundingContext(getContext());
    state.busy = true; state.error = ''; state.items = [];
    try {
      const { data } = await context.api.get('/settlement-reporting/report', { params: {
        ...fundingScope(context), status: 'locked', q: q.trim(), start: '1900-01-01', end: '9998-12-31', page, page_size: pageSize,
      } });
      if (!current()) return;
      Object.assign(state, { items: (data.items || []).filter(eligibleFundingSettlement), total: data.total || 0, page, pageSize });
    } catch (error) { if (current()) state.error = fundingError(error); }
    finally { if (current()) state.busy = false; }
  }
  return { load, reset, dispose() { disposed = true; reset(); } };
}

export function createFundingSession(getContext, state) {
  let generation = 0, reads = 0, disposed = false, writing = false;
  let identity = retryIdentity();
  function ticket() { const version = generation, key = fundingContext(getContext()); return () => !disposed && version === generation && key === fundingContext(getContext()); }
  function reset() { generation++; Object.assign(state, { items: [], busy: false, saving: false, error: '', notice: '' }); }
  async function load() {
    const version = ++reads, valid = ticket(), current = () => valid() && version === reads, context = getContext();
    if (disposed || !fundingAccess(context).read) { reset(); return; }
    state.busy = true; state.error = '';
    try {
      const { data } = await context.api.get(fundingPath, { params: fundingScope(context) });
      if (current()) state.items = data.items || [];
    } catch (error) { if (current()) state.error = fundingError(error); }
    finally { if (current()) state.busy = false; }
  }
  async function submit(action, form, row) {
    if (disposed || writing || !fundingAccess(getContext()).write) return false;
    const current = ticket(), context = getContext();
    writing = true; state.saving = true; state.error = ''; state.notice = '';
    try {
      const payload = fundingPayload(action, form, row, context);
      const path = action === 'propose' ? fundingPath : `${fundingPath}/${encodeURIComponent(row.id)}/${action}`;
      const body = { ...payload, request_id: identity([fundingContext(context), path, payload]) };
      await context.api.post(path, body);
      if (!current()) return false;
      identity = retryIdentity();
      state.notice = '操作已登记，银行到账未验证';
      return true;
    } catch (error) { if (current()) state.error = fundingError(error); return false; }
    finally { writing = false; if (current()) state.saving = false; }
  }
  return { load, submit, reset, dispose() { disposed = true; reset(); } };
}
