import { settlementSide, settlementContextKey } from './settlementParticipation.js';

export const reportViews = [
  ['details', '清算明细'], ['monthly', '月度'], ['products', '产品'],
  ['providers', '提供方'], ['participants', '收益主体'], ['rankings', '贡献排名'],
];
export const amountLabels = {
  gross_amount: '清算收入', cost_amount: '供货成本', profit_amount: '可分配利润',
  platform_fee: '平台分成', provider_share: '提供方利润分成', service_share: '数据服务方分成',
  expert_fee: '专家分成', channel_fee: '渠道分成', provider_total_receivable: '提供方合计应收',
};
export const countLabels = { income_order_count: '收入业务订单数', refund_count: '完成退款笔数', adjustment_count: '业务调整笔数', settlement_count: '清算记录数', order_count: '去重关联订单数' };
export const participantAmountLabels = { cost_amount: '成本回收', profit_share: '利润分成', loss_amount: '承担损失', total_receivable: '合计净应收（成本+利润-损失）', funding_absorption_loss: '平台补差吸收损失', baseline_total_receivable: '基线应收合计', net_total_receivable: '补差后净应收' };
export const sourceKinds = [{ value: 'original', label: '原始收入' }, { value: 'refund', label: '退款冲回' }, { value: 'version_change', label: '版本变更' }];
export const positionSubjectLabels = { enterprise: '企业主体', platform: '平台主体', unassigned: '未分配主体' };
export const beneficiaryAmountLabels = { profit_share: '本主体利润分成', loss_amount: '本主体承担损失', total_receivable: '本主体净应收', paid_amount: '已付收益', unpaid_amount: '未付收益', receivable_amount: '应追回', recovered_amount: '已追回', unrecovered_amount: '待追回余额', payable_offset_amount: '应付已抵扣', receivable_offset_amount: '应收已抵扣' };
export function beneficiaryContextKey({ user, enterpriseId, token }) {
  return JSON.stringify([token, user?.id, user?.platform_role, user?.verified_status, user?.is_active, user?.activation_status, enterpriseId, user?.enterprise_memberships]);
}
export function createBeneficiaryDiscovery(api, state, getContext) {
  let generation = 0, disposed = false, pending, pendingKey;
  function reset() { generation++; pending = null; pendingKey = null; Object.assign(state, { items: [], selectedId: '', busy: false, error: '' }); }
  function load() {
    const context = getContext(), key = beneficiaryContextKey(context);
    if (pending && pendingKey === key) return pending;
    reset();
    const user = context.user;
    if (disposed || !context.token || !user?.id || user.verified_status !== 'verified' || user.is_active === false || user.activation_status && user.activation_status !== 'active') return Promise.resolve();
    const version = generation, current = () => !disposed && version === generation && key === beneficiaryContextKey(getContext());
    state.busy = true; pendingKey = key;
    const task = (async () => {
      try {
        const { data } = await api.get('/settlement-reporting/beneficiaries', { params: context.enterpriseId ? { enterprise_id: context.enterpriseId } : {} });
        if (!current()) return;
        state.items = (data.items || []).filter(item => item.participant_id && ['service', 'expert', 'channel'].includes(item.participant_type) && item.currency).map(item => ({ participant_id: item.participant_id, participant_type: item.participant_type, participant_name: item.participant_name, currency: item.currency, enterprise_id: item.enterprise_id }));
        state.selectedId = state.items[0]?.participant_id || '';
      } catch (error) { if (current()) state.error = safeError(error, '本人收益身份暂时无法读取，请刷新重试'); }
      finally { if (current()) state.busy = false; }
    })();
    pending = task;
    task.finally(() => { if (pending === task) { pending = null; pendingKey = null; } });
    return task;
  }
  return { load, reset, dispose() { disposed = true; reset(); } };
}
export function ownBeneficiaryReport(data, identity) {
  const scope = data?.scope;
  if (scope?.kind !== 'beneficiary' || scope.participant_id !== identity?.participant_id || scope.participant_type !== identity?.participant_type || scope.currency !== identity?.currency) throw new Error('收益报表作用域不匹配，请重新选择本人收益主体');
  const amounts = [...Object.keys(beneficiaryAmountLabels), 'payable_amount'];
  const counts = [...Object.keys(countLabels), 'position_count'];
  const fields = ['position_id', 'position_kind', 'settlement_no', 'product_id', 'product_name', 'provider_id', 'provider_name', 'participant_id', 'participant_type', 'participant_name', 'status', 'direction', 'month', 'financial_at', 'source_kind', 'currency', 'provisional', 'funds_known', 'blocked_reason', ...amounts, ...counts];
  function project(row) {
    if (row.participant_id && row.participant_id !== identity.participant_id || row.participant_type && row.participant_type !== identity.participant_type || row.currency && row.currency !== identity.currency) throw new Error('收益报表含有其他主体或币种，已停止展示');
    const clean = {};
    for (const key of fields) if (Object.hasOwn(row, key) && (row[key] == null || ['string', 'number', 'boolean'].includes(typeof row[key]))) clean[key] = row[key];
    return clean;
  }
  const clean = { scope: { kind: scope.kind, participant_id: scope.participant_id, participant_type: scope.participant_type, participant_name: scope.participant_name, currency: scope.currency, cutoff: scope.cutoff, filters: scope.filters }, summary: project(data.summary || {}), total: data.total, page: data.page, page_size: data.page_size, rankings: [] };
  for (const key of ['items', 'monthly', 'products', 'providers', 'participants']) clean[key] = (data[key] || []).map(project);
  return clean;
}
export function canReviewTransaction(row, user, writable) {
  return !!writable && row.status === 'draft' && row.version === 1 && !!row.creator_id &&
    String(row.creator_id) !== String(user?.id) && row.can_confirm !== false && row.actions?.can_confirm !== false;
}
export function transactionAmount(row) {
  if (row.amount != null) return row.amount;
  if (!Array.isArray(row.allocations) || !row.allocations.length) return null;
  const total = row.allocations.reduce((sum, item) => sum + cents(item.amount), 0n);
  return `${total / 100n}.${String(total % 100n).padStart(2, '0')}`;
}
export function transactionCurrency(row, positions) {
  if (row.currency) return row.currency;
  const ids = [...(row.allocations || []).map(item => item.position_id), ...(row.counter_position_id ? [row.counter_position_id] : [])];
  if (!ids.length || ids.some(id => !positions[id]?.currency)) return '待核对币种';
  const currencies = new Set(ids.map(id => positions[id].currency));
  return currencies.size === 1 ? [...currencies][0] : '币种不一致';
}
export async function transactionPositions(api, rows, scope, known, current) {
  const positions = { ...known };
  const required = new Set(rows.filter(row => !row.currency).flatMap(row => [...(row.allocations || []).map(item => item.position_id), ...(row.counter_position_id ? [row.counter_position_id] : [])]));
  const missing = () => [...required].some(id => !positions[id]?.currency);
  let page = 1;
  while (current() && missing()) {
    const { data } = await api.get('/settlement-finance/positions', { params: { ...scope, page, page_size: 100 } });
    if (!current()) return null;
    for (const position of data.items || []) positions[position.id] = position;
    if (!data.items?.length || page * 100 >= data.total) break;
    page++;
  }
  return current() ? positions : null;
}
export function financeAccess(user, enterprise, view, beneficiary) {
  if (view === 'beneficiary') return { read: !!user?.id && user.verified_status === 'verified' && user.is_active !== false && (!user.activation_status || user.activation_status === 'active') && !!beneficiary?.participant_id && ['service', 'expert', 'channel'].includes(beneficiary.participant_type), write: false, platform: false };
  const side = settlementSide(user, enterprise, view);
  return { read: !!side || user?.platform_role === 'security_compliance', write: side === 'platform', platform: !!user?.platform_role };
}
export function financeScope(user, enterprise, view, beneficiary) {
  if (!financeAccess(user, enterprise, view, beneficiary).read) throw new Error('当前身份无清算报表权限');
  if (view === 'beneficiary') return { business_view: 'beneficiary', participant_id: beneficiary.participant_id, ...(enterprise?.id ? { enterprise_id: enterprise.id } : {}) };
  return user?.platform_role ? {} : { enterprise_id: enterprise.id, business_view: view };
}
export function financeContext(user, enterprise, view, api, beneficiary) {
  return JSON.stringify([settlementContextKey(user, enterprise, view, api?.defaults?.headers?.common?.Authorization || api?.defaults?.headers?.Authorization || ''), beneficiary?.participant_id, beneficiary?.participant_type, beneficiary?.currency, user?.is_active, user?.activation_status]);
}
export function monthRange(now = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit' }).formatToParts(now);
  const year = Number(parts.find(p => p.type === 'year').value), month = Number(parts.find(p => p.type === 'month').value);
  const prefix = `${year}-${String(month).padStart(2, '0')}`;
  return { start: `${prefix}-01`, end: `${prefix}-${new Date(Date.UTC(year, month, 0)).getUTCDate()}` };
}
export function reportParams(filters, scope, page = 1, pageSize = 20) {
  const params = { ...scope, page, page_size: pageSize };
  for (const key of ['start', 'end', 'q', 'provider_id', 'product_id', 'batch_id', 'status', 'source_kind', 'participant_type']) {
    const value = String(filters[key] || '').trim();
    if (value && !(key === 'provider_id' && scope.enterprise_id)) params[key] = value;
  }
  for (const key of ['start', 'end']) {
    const value = params[key];
    if (value && (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(Date.parse(value)) || new Date(value).toISOString().slice(0, 10) !== value)) throw new Error('日期须为有效的 YYYY-MM-DD');
  }
  if (params.start && params.end && params.start > params.end) throw new Error('结束日期不得早于开始日期（包含结束日）');
  return params;
}
export function money(value) {
  if (value == null || value === '') return '—';
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(String(value));
  if (!match) return '—';
  return `${match[1]}${match[2].replace(/\B(?=(\d{3})+(?!\d))/g, ',')}.${(match[3] || '').padEnd(2, '0')}`;
}
export function cents(value) {
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(String(value));
  if (!match) throw new Error('请输入最多两位小数的正金额');
  return BigInt(match[1]) * 100n + BigInt((match[2] || '').padEnd(2, '0'));
}
export function allocation(position, amount) {
  const value = cents(amount);
  if (value <= 0n || value > cents(position.balance)) throw new Error('金额必须大于零且不超过未结余额');
  return { position_id: position.id, amount: `${value / 100n}.${String(value % 100n).padStart(2, '0')}` };
}
export function samePayee(position, anchor) {
  try {
    const kinds = anchor.kind === 'funding_return' ? ['funding_return'] : ['cost', 'profit'];
    if (!kinds.includes(anchor.kind) || anchor.direction !== 'payable' || anchor.blocked_reason || anchor.status === 'blocked' || cents(anchor.balance) <= 0n) return false;
    return position.direction === 'payable' && !!position.participant_id && position.participant_id !== 'unassigned' &&
      position.participant_type !== 'unassigned' && !!position.currency && !position.blocked_reason && position.status !== 'blocked' &&
      kinds.includes(position.kind) && position.participant_type === anchor.participant_type &&
      position.participant_id === anchor.participant_id && position.currency === anchor.currency && cents(position.balance) > 0n;
  } catch { return false; }
}
export function paymentSelection(anchor, rows, ids, amounts) {
  if (!ids.length || ids.length > 1000 || new Set(ids).size !== ids.length) throw new Error('请选择 1 至 1000 条不重复的付款资金行');
  const allocations = [...ids].sort().map(id => {
    const row = rows.find(item => item.id === id);
    if (!row || !samePayee(row, anchor)) throw new Error('付款资金行须为同主体、同币种的有效应付；补差返还须独立成组，不与成本利润混付');
    return allocation(row, amounts[id]);
  });
  return { allocations, total: transactionAmount({ allocations }) };
}
export async function payablePositions(api, anchor, scope, current) {
  const rows = [];
  let page = 1;
  while (current()) {
    const { data } = await api.get('/settlement-finance/positions', { params: { ...scope, direction: 'payable', page, page_size: 100 } });
    if (!current()) return null;
    rows.push(...(data.items || []).filter(row => samePayee(row, anchor)));
    if (!data.items?.length || page * 100 >= data.total) break;
    page++;
  }
  return current() ? rows.sort((a, b) => (a.kind === 'cost' ? 0 : 1) - (b.kind === 'cost' ? 0 : 1) || a.id.localeCompare(b.id)) : null;
}
const errorLabels = {
  funding_return_requires_manual_review: '补差返还只能人工登记付款，并由另一位操作者独立复核。',
  opposite_party_required: '须由对侧独立审批人处理，申请人及同侧人员不能审批。',
  exception_version_conflict: '成本例外已被处理或版本变化，请刷新后核对。',
  category_weights_must_equal_100: '每个收益类别的权重须分别合计100%。',
  invalid_participant_identity_or_kind: '收益主体不存在、类别不符或不在当前授权范围。',
  participant_identity_outside_scope: '只能关联本企业及本企业有效成员。',
  verified_provider_admin_required: '仅已认证提供方管理员可管理本企业主体与成本例外。',
  real_identity_required: '非平台主体须关联真实企业或用户。',
  identity_not_found: '关联企业或用户不存在，请核对编号。',
  evidence_refs_required: '请提供至少一条有效证据引用。',
  invalid_bearer: '承担主体不存在、币种不符或不在提供方授权范围。',
  provider_bearer_must_match_order_provider: '提供方承担主体必须与退款订单的提供方一致。',
  unique_positive_final_baseline_required: '尚无唯一且有正成本的最终基线，不能申请成本例外。',
  exact_completed_segment_final_baseline_required: '跨版本退款须具备可校验的唯一经济段及最终清算基线。',
  retained_cost_exceeds_refund_cost: '保留成本不能超过本次退款应冲回成本。',
  retained_cost_exceeds_baseline: '累计保留成本不能超过最终基线成本。',
  baseline_changed: '最终基线已经变化，请重新核对证据。',
  existing_reversal_requires_explicit_adjustment: '已存在成本冲回，须走明确调整流程，不能覆盖原结果。',
  approved_or_completed_refund_required: '仅已批准或已完成的退款可申请成本例外。',
  independent_annual_reviewer_required: '年度草稿生成人不能自行定稿，请由独立清算人员复核。',
  explicit_snapshot_reconciliation_required: '请核对快照校验值，并填写对账凭据及复核依据。',
  annual_data_changed_after_cutoff: '快照生成后数据已有变化，请生成新版本并重新核对。',
  annual_current_data_unresolved: '当前年度仍有待处理事项，不能定稿。',
  annual_report_finalize_forbidden: '当前角色不能将年度快照定稿。',
  annual_finalization_conflict: '该年度快照已完成其他定稿请求，请刷新核对。',
  approved_cost_exception_requires_final_basis_refresh: '成本例外已批准，等待刷新最终基线后重新确认冲回。',
  different_reviewer_required: '登记人不能复核自己的凭证，请由另一位清算人员复核。',
  positive_payment_evidence_required: '正向付款凭证尚未完成，请在资金台账登记付款并由另一位清算人员复核后再完成批次付款。',
  payment_evidence: '缺少有效付款依据，请补充人工登记或模拟付款凭证并完成复核。',
  payment_evidence_required: '缺少有效付款依据，请先登记并复核付款凭证。',
  final_baseline_not_ready: '最终清算基线尚未就绪，请先处理调整提案并锁定清算单。',
  final_baseline_missing: '尚无最终清算基线，请先锁定清算单。买方退款与追款独立跟踪。',
  locked_settlement_required: '清算单须先锁定，才能形成资金台账。',
  subject_unassigned: '收益主体尚未配置，请先补齐实际收款主体。',
  missing_subject_mapping: '收益主体映射缺失，请补齐实际收款主体后再登记。',
  position_cash_blocked: '资金行尚不具备外部收付款条件，请检查主体配置和阻塞原因。',
  position_direction_or_balance: '资金方向或可用余额已变化，请刷新资金台账并调整分配金额。',
  confirmed_payment_batch_required: '尚无可付款的已确认批次，请先锁定清算单并确认所属批次。',
  no_confirmed_batch: '尚无已确认批次，请先完成批次确认。',
  position_batch_changed: '资金行所属批次已变化，请刷新后重新核对。',
  transaction_version_conflict: '凭证版本或状态已变化，请刷新后重新核对。',
  transaction_not_found: '凭证不存在或已不在当前授权范围，请刷新列表。',
  position_not_found: '资金行不存在或已不在当前授权范围，请刷新台账。',
  subject_currency_mismatch: '所选资金行必须属于同一实际主体、同一币种。',
  offset_counter_mismatch_or_balance: '对向应付行的主体、币种或余额不匹配，请重新选择。',
  offset_counter_required: '抵扣必须选择同主体、同币种的对向应付行。',
  invalid_money_cents: '金额须为有效的两位小数，且不得超过系统金额上限。',
  idempotency_payload_mismatch: '重试内容与原请求不一致，请保留凭证依据并重新核对。',
  finance_request_conflict: '凭证请求发生并发冲突，请刷新列表确认是否已创建，再重试。',
  finance_confirmation_conflict: '凭证复核发生并发冲突，请刷新凭证状态。',
  settlement_finance_operator_required: '当前账号没有资金操作权限，请由平台清算人员处理。',
  annual_report_creation_forbidden: '当前账号只能查看年度报告，不能创建快照。',
  annual_report_unresolved: '年度报告仍有未解决事项，请先完成清算与资金核对。',
  date_must_be_YYYY_MM_DD: '日期须使用 YYYY-MM-DD 格式。',
  invalid_date_range: '日期范围无效，结束日期不得早于开始日期。',
};
export function safeError(error, fallback = '操作失败，请稍后重试') {
  const detail = error?.response?.data?.detail;
  if (Array.isArray(detail)) return '输入校验失败，请检查必填字段、日期和金额格式。';
  const value = typeof detail === 'string' ? detail : detail?.code || detail?.reason || detail?.message || error?.message;
  if (typeof value === 'string') {
    if (Object.hasOwn(errorLabels, value)) return errorLabels[value];
    if (/^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/i.test(value) && value.length <= 100) return `${fallback}（业务码：${value}）`;
    if (/sqlalchemy|psycopg|sqlite|traceback|sqlstate|\[SQL:|\b(?:SELECT|INSERT|UPDATE|DELETE)\b[\s\S]*\b(?:FROM|INTO|SET|WHERE)\b/i.test(value)) return fallback;
    if (/Network Error|Failed to fetch|timeout/i.test(value)) return '网络连接失败，请保留当前内容并重试。';
    if (/[\u3400-\u9fff]/.test(value) && value.length <= 500) return value;
  }
  return fallback;
}
export function requestKey() { return globalThis.crypto?.randomUUID?.() || `sf-${Date.now()}-${Math.random().toString(36).slice(2)}`; }
// A ticket compares both request order and live authentication context after every await.
export function requestGuard(context) {
  let version = 0, alive = true;
  return { begin() { const id = ++version, key = context(); return () => alive && id === version && key === context(); }, invalidate() { version++; }, dispose() { alive = false; version++; } };
}
export function retryIdentity() {
  let signature, key;
  return payload => { const next = JSON.stringify(payload); if (next !== signature) { signature = next; key = requestKey(); } return key; };
}
