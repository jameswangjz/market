<script setup>
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue';
import { BarChart3, Wallet, ArrowLeftRight, Archive, Download, Save, Check, X, RefreshCw, Eye } from 'lucide-vue-next';
import ConsoleTabs from './ConsoleTabs.vue';
import MainListFilters from './MainListFilters.vue';
import PaginationControls from './PaginationControls.vue';
import SettlementManagement from './SettlementManagement.vue';
import SettlementReportEvidence from './SettlementReportEvidence.vue';
import SettlementBalanceBridge from './SettlementBalanceBridge.vue';
import { managementSide, canFinalizeAnnual } from './settlementManagement.js';
import { reportViews, amountLabels, beneficiaryAmountLabels, ownBeneficiaryReport, participantAmountLabels, countLabels, sourceKinds, positionSubjectLabels, canReviewTransaction, transactionAmount, transactionCurrency, transactionPositions, paymentSelection, payablePositions, financeAccess, financeScope, financeContext, monthRange, reportParams, money, cents, allocation, safeError, requestGuard, retryIdentity } from './settlementFinance.js';

const props = defineProps({ api: { type: Object, required: true }, user: { type: Object, required: true }, enterprise: Object, businessView: String, dialogs: Object, beneficiary: Object });
const emit = defineEmits(['changed']);
const tabs = [{ key: 'reports', label: '清算报表', icon: BarChart3 }, { key: 'funds', label: '应收应付', icon: Wallet }, { key: 'transactions', label: '资金凭证', icon: ArrowLeftRight }, { key: 'annual', label: '年度快照', icon: Archive }];
const tab = ref('reports'), reportView = ref('details'), page = ref(1), pageSize = ref(20);
const state = reactive({ filters: { ...monthRange(), q: '', direction: '' }, busy: false, error: '' });
const result = ref({}), detail = ref(null), modal = ref(null), submitting = ref(false), downloading = ref(false), notice = ref('');
const knownPositions = ref({});
const modalElement = ref(null);
const annualYear = ref(new Date().getFullYear()), annualData = ref(null), annualBusy = ref(false), annualError = ref('');
const paymentRows = ref([]), selectedPaymentIds = ref([]), paymentAmounts = ref({});
const paymentLoading = ref(false), paymentLoadError = ref('');
const form = reactive({ kind: 'payout', mode: 'manual', reference: '', note: '', amount: '', counter_position_id: '', year: new Date().getFullYear() });
let identity = retryIdentity();
let previousFocus;
const isBeneficiary = computed(() => props.businessView === 'beneficiary');
const management = computed(() => managementSide(props.user, props.enterprise, props.businessView));
const workspaceTabs = computed(() => isBeneficiary.value ? tabs.filter(item => item.key === 'reports') : [...tabs, ...(management.value ? [{ key: 'management', label: '主体与成本例外' }] : [])]);
const visibleReportViews = computed(() => isBeneficiary.value ? reportViews.filter(([key]) => key !== 'rankings') : reportViews);
const summaryAmountLabels = computed(() => isBeneficiary.value ? beneficiaryAmountLabels : amountLabels);
const access = computed(() => financeAccess(props.user, props.enterprise, props.businessView, props.beneficiary));
const context = () => financeContext(props.user, props.enterprise, props.businessView, props.api, props.beneficiary);
const guard = requestGuard(context), mutationGuard = requestGuard(context), downloadGuard = requestGuard(context);
const paymentGuard = requestGuard(context);
const annualGuard = requestGuard(context);
const scope = () => financeScope(props.user, props.enterprise, props.businessView, props.beneficiary);
const fields = computed(() => tab.value === 'reports' ? [
  { key: 'start', label: '开始日期（北京时间）', type: 'date' }, { key: 'end', label: '结束日期（含当天）', type: 'date' }, { key: 'q', label: isBeneficiary.value ? '清算单/关键词' : '订单号 / 关键词' },
  ...(access.value.platform ? [{ key: 'provider_id', label: '提供方 ID' }] : []), { key: 'product_id', label: '产品 ID' }, { key: 'batch_id', label: '批次 ID' },
  { key: 'status', label: '清算状态', options: ['pending', 'locked', 'confirmed', 'paid', 'superseded'].map(value => ({ value, label: labels[value] || value })) },
  { key: 'source_kind', label: '经济记录类型', options: sourceKinds },
  ...(isBeneficiary.value ? [] : [{ key: 'participant_type', label: '收益类别', options: ['platform', 'provider', 'service', 'expert', 'channel'].map(value => ({ value, label: labels[value] })) }]),
] : tab.value === 'funds' ? [{ key: 'q', label: '业务编号 / 主体名称' }, { key: 'direction', label: '方向', options: ['payable', 'receivable', 'internal'].map(value => ({ value, label: labels[value] })) }] : []);
const labels = { payable: '应付', receivable: '应追回', internal: '内部收益', cost: '供货成本', profit: '利润分成', platform: '平台', provider: '提供方', service: '数据服务方', expert: '专家', channel: '渠道', manual: '人工登记', simulated: '模拟确认', payout: '付款', recovery: '回款', offset: '抵扣', original: '原始收入', refund: '退款冲回', version_change: '版本变更', open: '未结', partial: '部分核销', settled: '已核销', blocked: '已阻塞', formal: '正式快照', draft: '草稿', confirmed: '已确认', pending: '待确认', locked: '已锁定', paid: '正向已付', superseded: '已作废' };
const rows = computed(() => tab.value === 'reports' && reportView.value !== 'details' ? result.value[reportView.value] || [] : Array.isArray(result.value) ? result.value : result.value.items || []);
const columns = computed(() => {
  if (isBeneficiary.value) {
    const dimensions = reportView.value === 'details' ? [['settlement_no', '清算单号'], ['product_name', '产品'], ['provider_name', '提供方'], ['source_kind', '经济类型'], ['direction', '方向']] : [[{ monthly: 'month', products: 'product_name', providers: 'provider_name', participants: 'participant_name' }[reportView.value], '名称 / 月份']];
    return [...dimensions, ...Object.entries(beneficiaryAmountLabels), ...(reportView.value === 'details' ? [['currency', '币种'], ['status', '清算状态']] : Object.entries(countLabels))];
  }
  if (tab.value === 'funds') return [['id', '资金行编号'], ['settlement_no', '清算单号'], ['order_no', '订单号'], ['participant_name', '收益主体'], ['participant_type', '主体类型'], ['kind', '款项'], ['share_kind', '分成项目'], ['direction', '方向'], ['amount', '确认金额'], ['applied_amount', '已核销'], ['balance', '未结余额'], ['currency', '币种'], ['status', '状态'], ['blocked_reason', '阻塞原因']];
  if (tab.value === 'transactions') return [['id', '凭证编号'], ['kind', '业务类型'], ['mode', '登记方式'], ['reference', '业务凭证'], ['amount', '分配金额'], ['currency', '币种'], ['creator_id', '登记人'], ['reviewer_id', '复核人'], ['version', '版本'], ['status', '状态']];
  if (tab.value === 'annual') return [['year', '年度'], ['id', '快照编号'], ['version', '版本'], ['scope_key', '报告范围'], ['status', '状态'], ['cutoff', '数据截止时间']];
  if (reportView.value === 'participants') return [['participant_name', '收益主体'], ['participant_type', '收益类别'], ['subject_type', '主体类型'], ...Object.entries(participantAmountLabels), ['settlement_ids', '关联清算记录数'], ['provisional', '暂估未冻结']];
  if (reportView.value === 'rankings') return [['rank', '名次'], ['provider_name', '提供方'], ['metric', '排名指标'], ['amount', '净额'], ['basis', '排名口径']];
  const dimensions = reportView.value === 'details' ? [['settlement_no', '清算单号'], ['order_no', '订单号'], ['batch_no', '批次号'], ['product_name', '产品'], ['product_version_name', '版本'], ['source_kind', '经济类型']] : reportView.value === 'products' ? [['product_name', '产品'], ['product_version_name', '版本'], ['provider_id', '提供方关联'], ['month', '财务月份']] : [[reportView.value === 'monthly' ? 'month' : 'provider_name', reportView.value === 'monthly' ? '财务月份' : '提供方']];
  return [...dimensions, ...(reportView.value === 'details' ? [] : Object.entries(countLabels)), ...Object.entries(amountLabels), ...(reportView.value === 'details' ? [['status', '清算状态'], ['provisional', '暂估未冻结']] : [])];
});
function cell(row, key) {
  const value = row[key];
  if (tab.value === 'transactions' && key === 'amount') return money(transactionAmount(row));
  if (tab.value === 'transactions' && key === 'currency') return transactionCurrency(row, knownPositions.value);
  if (key === 'settlement_ids') return Array.isArray(value) ? value.length : '—';
  if (key === 'provisional') return value == null ? '—' : value ? '是' : '否';
  if (key === 'participant_type' && tab.value === 'funds' || key === 'subject_type') return positionSubjectLabels[value] || value || '—';
  if (key === 'source_kind') return sourceKinds.find(item => item.value === value)?.label || value || '—';
  if (key === 'share_kind' || key === 'metric') return amountLabels[value] || value || '—';
  if (key === 'basis' && value === 'net') return '冲回后净额';
  if (key === 'blocked_reason' && value) return safeError({ response: { data: { detail: value } } });
  return key in amountLabels || key in beneficiaryAmountLabels || key in participantAmountLabels || ['amount', 'applied_amount', 'balance'].includes(key) ? money(value) : labels[value] || (value == null ? '—' : typeof value === 'object' ? JSON.stringify(value) : value);
}
function canRegister(row) {
  try { return access.value.write && ['payable', 'receivable'].includes(row.direction) && !row.blocked_reason && !!row.participant_id && cents(row.balance) > 0n; }
  catch { return false; }
}
const detailFields = computed(() => isBeneficiary.value ? [['participant_name', '本人收益主体'], ['financial_at', '财务归属时间']] : tab.value === 'annual' ? [['actor_id', '生成人'], ['digest', '快照校验值'], ['note', '原因']] : tab.value === 'transactions' ? [['note', '原因'], ['counter_position_id', '对向应付行编号']] : tab.value === 'funds' ? [['participant_id', '收益主体关联']] : reportView.value === 'details' ? [['baseline_hash', '最终基线校验值'], ['financial_at', '财务归属时间'], ['refund_id', '退款关联'], ['change_id', '版本变更关联']] : []);
const paymentPreview = computed(() => {
  try { return { ...paymentSelection(modal.value?.row, paymentRows.value, selectedPaymentIds.value, paymentAmounts.value), error: '' }; }
  catch (error) { return { total: null, error: safeError(error) }; }
});
async function load() {
  const current = guard.begin(); result.value = {}; detail.value = null; state.error = ''; state.busy = false;
  if (!access.value.read) return;
  if (tab.value === 'management') return;
  if (isBeneficiary.value && tab.value !== 'reports') return;
  state.busy = true;
  try {
    const params = tab.value === 'reports' ? reportParams(state.filters, scope(), page.value, pageSize.value) : { ...scope(), ...(tab.value === 'funds' ? { q: state.filters.q || undefined, direction: state.filters.direction || undefined } : {}), page: page.value, page_size: pageSize.value };
    const path = { reports: '/settlement-reporting/report', funds: '/settlement-finance/positions', transactions: '/settlement-finance/transactions', annual: '/settlement-reporting/annual-reports' }[tab.value];
    const { data } = await props.api.get(path, { params });
    if (!current()) return;
    if (!isBeneficiary.value && data.scope?.kind === 'beneficiary') throw new Error('收益报表作用域不匹配，请进入我的收益');
    result.value = isBeneficiary.value ? ownBeneficiaryReport(data, props.beneficiary) : data;
    if (tab.value === 'funds') for (const position of data.items || []) knownPositions.value[position.id] = position;
    if (tab.value === 'transactions') {
      const positions = await transactionPositions(props.api, data.items || [], scope(), knownPositions.value, current);
      if (current() && positions) knownPositions.value = positions;
    }
  } catch (error) { if (current()) state.error = safeError(error); }
  finally { if (current()) state.busy = false; }
}
function search() { page.value = 1; load(); }
function reset() { state.filters = { ...monthRange(), q: '', direction: '' }; search(); }
function paginate(value) { page.value = value; load(); }
function resize(value) { pageSize.value = value; search(); }
async function loadAnnualView() {
  if (!access.value.read || isBeneficiary.value || annualBusy.value) return;
  const year = Number(annualYear.value);
  if (!Number.isInteger(year) || year < 1900 || year > 9998) { annualError.value = '请输入有效年度'; return; }
  const current = annualGuard.begin(); annualBusy.value = true; annualData.value = null; annualError.value = '';
  try {
    const { data } = await props.api.get('/settlement-reporting/report', { params: { ...scope(), year, start: `${year}-01-01`, end: `${year}-12-31`, page: 1, page_size: 20 } });
    if (current()) annualData.value = data;
  } catch (error) { if (current()) annualError.value = safeError(error); }
  finally { if (current()) annualBusy.value = false; }
}
function open(kind, row = null) {
  if (!access.value.write || submitting.value) return;
  modal.value = { kind, row, context: context() }; state.error = '';
  Object.assign(form, { kind: row?.direction === 'receivable' ? 'recovery' : 'payout', mode: 'manual', reference: '', note: '', amount: row?.balance || '', counter_position_id: '' });
  paymentGuard.invalidate(); paymentRows.value = row ? [row] : []; selectedPaymentIds.value = row ? [row.id] : []; paymentAmounts.value = row ? { [row.id]: row.balance } : {}; paymentLoadError.value = ''; paymentLoading.value = false;
  if (kind === 'create' && row?.direction === 'payable') loadPaymentRows();
}
async function loadPaymentRows() {
  const action = modal.value;
  if (action?.kind !== 'create' || action.row?.direction !== 'payable' || !access.value.write || submitting.value) return;
  const ticket = paymentGuard.begin(), current = () => ticket() && modal.value === action;
  paymentLoading.value = true; paymentLoadError.value = '';
  try {
    const positions = await payablePositions(props.api, action.row, scope(), current);
    if (!current() || !positions) return;
    paymentRows.value = positions;
    selectedPaymentIds.value = selectedPaymentIds.value.filter(id => positions.some(row => row.id === id));
    for (const row of positions) {
      if (!(row.id in paymentAmounts.value)) paymentAmounts.value[row.id] = row.balance;
      knownPositions.value[row.id] = row;
    }
  } catch (error) { if (current()) paymentLoadError.value = safeError(error); }
  finally { if (current()) paymentLoading.value = false; }
}
function close() { if (!submitting.value) modal.value = null; }
function modalKeys(event) {
  if (event.key === 'Escape') { event.preventDefault(); close(); return; }
  if (event.key !== 'Tab') return;
  const nodes = [...event.currentTarget.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled)')];
  const first = nodes[0], last = nodes.at(-1);
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
}
function canConfirm(row) { return canReviewTransaction(row, props.user, access.value.write); }
async function submit() {
  if (submitting.value || !access.value.write || !modal.value || modal.value.context !== context()) return;
  const current = mutationGuard.begin(), action = modal.value;
  try {
    if (!form.note.trim()) throw new Error('请填写原因');
    let path, payload;
    if (action.kind === 'annual') {
      if (!Number.isInteger(Number(form.year)) || Number(form.year) < 1900 || Number(form.year) > 9998) throw new Error('请输入有效年度');
      path = '/settlement-reporting/annual-reports'; payload = { year: Number(form.year), note: form.note.trim() };
    } else if (action.kind === 'finalize') {
      if (!canFinalizeAnnual(action.row, props.user, access.value.write)) throw new Error('年度报告须由独立复核人定稿');
      if (!form.reference.trim()) throw new Error('请填写对账凭据');
      path = `/settlement-reporting/annual-reports/${encodeURIComponent(action.row.id)}/finalize`;
      payload = { snapshot_digest: action.row.digest, reconciliation_reference: form.reference.trim(), note: form.note.trim() };
    } else if (action.kind === 'confirm') {
      if (!canConfirm(action.row)) throw new Error('当前凭证不可复核');
      path = `/settlement-finance/transactions/${encodeURIComponent(action.row.id)}/confirm`;
      payload = { note: form.note.trim(), expected_version: action.row.version };
      if (payload.expected_version == null) throw new Error('缺少凭证版本，请刷新');
    } else {
      if (!form.reference.trim()) throw new Error('请填写业务凭证');
      const row = action.row;
      if (!canRegister(row)) throw new Error('当前资金行不可登记，请刷新余额与主体状态');
      if (!['payable', 'receivable'].includes(row.direction)) throw new Error('内部收益不可登记外部付款');
      if (form.kind !== 'offset' && form.kind !== (row.direction === 'payable' ? 'payout' : 'recovery')) throw new Error('业务方向不匹配');
      if (form.kind === 'payout' && (paymentLoading.value || paymentLoadError.value)) throw new Error('请先完成付款资金行加载');
      const allocations = form.kind === 'payout' ? paymentSelection(row, paymentRows.value, selectedPaymentIds.value, paymentAmounts.value).allocations : [allocation(row, form.amount)];
      payload = { kind: form.kind, mode: form.mode, reference: form.reference.trim(), note: form.note.trim(), allocations };
      if (form.kind === 'offset') { if (row.direction !== 'receivable') throw new Error('抵扣须从应收行登记，对向行为应付'); if (!form.counter_position_id.trim() || form.counter_position_id.trim() === String(row.id)) throw new Error('请选择同主体同币种的对向应付行'); payload.counter_position_id = form.counter_position_id.trim(); }
      path = '/settlement-finance/transactions';
    }
    payload.request_id = identity({ context: context(), path, payload });
    submitting.value = true; state.error = '';
    await props.api.post(path, payload, { params: scope() });
    if (!current()) return;
    identity = retryIdentity(); modal.value = null; notice.value = action.kind === 'annual' ? '年度草稿快照已创建' : action.kind === 'finalize' ? '年度快照已复核定稿' : '资金凭证已保存'; emit('changed', { kind: action.kind }); await load();
  } catch (error) { if (current()) state.error = safeError(error); }
  finally { if (current()) submitting.value = false; }
}
async function download(kind, row) {
  if (downloading.value || !access.value.read) return;
  if (isBeneficiary.value && (row || !['summary', 'details', 'products', 'providers', 'participants', 'refunds'].includes(kind))) return;
  const current = downloadGuard.begin(); downloading.value = true; state.error = '';
  try {
    const params = row ? scope() : reportParams(state.filters, scope());
    delete params.page; delete params.page_size;
    if (!row) params.kind = kind;
    const path = row ? `/settlement-reporting/annual-reports/${encodeURIComponent(row.id)}/export` : '/settlement-reporting/export';
    const { data } = await props.api.get(path, { params, responseType: 'blob' });
    if (!current()) return;
    const url = URL.createObjectURL(data instanceof Blob ? data : new Blob([data], { type: 'text/csv;charset=utf-8' }));
    const link = document.createElement('a'); link.href = url; link.download = `settlement-${row ? row.year : kind}.csv`; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { if (current()) state.error = safeError(error); }
  finally { if (current()) downloading.value = false; }
}
watch(() => [context(), props.api], () => {
  annualGuard.invalidate(); annualData.value = null; annualBusy.value = false; annualError.value = '';
  guard.invalidate(); mutationGuard.invalidate(); downloadGuard.invalidate(); paymentGuard.invalidate(); modal.value = null; paymentRows.value = []; selectedPaymentIds.value = []; paymentAmounts.value = {}; paymentLoading.value = false; paymentLoadError.value = ''; result.value = {}; knownPositions.value = {}; detail.value = null; submitting.value = false; downloading.value = false; notice.value = ''; state.filters = { ...monthRange(), q: '', direction: '' }; page.value = 1;
  if (isBeneficiary.value) { tab.value = 'reports'; if (reportView.value === 'rankings') reportView.value = 'details'; }
  if (tab.value === 'management' && !management.value) tab.value = 'reports';
  load();
}, { immediate: true, flush: 'sync' });
watch(tab, () => { modal.value = null; page.value = 1; load(); });
watch(annualYear, () => { annualGuard.invalidate(); annualData.value = null; annualBusy.value = false; annualError.value = ''; });
watch(modal, async value => {
  if (!value) { paymentGuard.invalidate(); paymentLoading.value = false; previousFocus?.focus?.(); return; }
  previousFocus = document.activeElement;
  await nextTick();
  const dialog = modalElement.value;
  if (modal.value === value && dialog && !dialog.contains(document.activeElement)) {
    dialog.querySelector('button:not(:disabled), select:not(:disabled), input:not(:disabled), textarea:not(:disabled)')?.focus();
  }
});
onBeforeUnmount(() => { guard.dispose(); mutationGuard.dispose(); downloadGuard.dispose(); paymentGuard.dispose(); annualGuard.dispose(); });
</script>

<template>
  <section class="settlement-finance" aria-label="清算报表与资金" :aria-busy="state.busy">
    <header class="workspace-heading"><h2>{{ isBeneficiary ? '我的收益' : '清算报表与资金' }}</h2><span>{{ isBeneficiary ? beneficiary?.participant_name : access.platform ? '平台授权范围' : enterprise?.name || '本企业' }}</span><button class="icon-btn" title="刷新" aria-label="刷新" :disabled="state.busy" @click="load"><RefreshCw :size="16" /></button></header>
    <p v-if="!access.read" role="status">当前身份无清算报表权限</p>
    <template v-else>
      <ConsoleTabs v-model="tab" :tabs="workspaceTabs" label="清算工作区" />
      <SettlementManagement v-if="tab === 'management' && management" :key="context()" :api="api" :user="user" :enterprise="enterprise" :business-view="businessView" @changed="emit('changed', $event)" />
      <template v-else>
      <p class="scope-note">{{ isBeneficiary ? '仅本人收益及追款记录' : access.platform ? '授权清算数据' : '本企业清算数据' }} · 人工登记 / 模拟确认</p>
      <MainListFilters v-if="tab === 'reports' || tab === 'funds'" :state="state" :fields="fields" @search="search" @reset="reset" />
      <p v-else-if="state.error" role="alert" class="error">{{ state.error }}</p>
      <p v-if="notice" role="status">{{ notice }}</p>
      <template v-if="tab === 'reports'">
        <dl class="metrics"><div v-for="(label, key) in countLabels" :key="key"><dt>{{ label }}</dt><dd>{{ result.summary?.[key] ?? '—' }}</dd></div></dl>
        <dl class="metrics amounts"><div v-for="(label, key) in summaryAmountLabels" :key="key"><dt>{{ label }}</dt><dd>{{ money(result.summary?.[key]) }}</dd></div></dl>
        <SettlementBalanceBridge v-if="!isBeneficiary" :bridge="result.balance_bridge" />
        <p class="scope-note">{{ result.scope?.filters?.start || state.filters.start }} 至 {{ result.scope?.filters?.end || state.filters.end }}（含当天，北京时间） · {{ result.scope?.currency || '币种以明细为准' }} · 截止 {{ result.scope?.cutoff || '—' }} · {{ result.scope?.includes_unconfirmed === false ? '不含未确认记录' : '含未确认记录' }}</p>
        <ConsoleTabs v-model="reportView" :tabs="visibleReportViews.map(([key, label]) => ({ key, label }))" label="报表维度" />
        <div class="toolbar"><button v-for="kind in (isBeneficiary ? ['summary', 'details', 'products', 'providers', 'participants', 'refunds'] : ['summary', 'details', 'products', 'providers', 'participants', 'refunds', 'rankings'])" :key="kind" class="secondary-btn" :disabled="downloading || state.busy" @click="download(kind)"><Download :size="14" />{{ ({ summary: '汇总', details: '明细', products: '产品', providers: '提供方', participants: '主体', refunds: '退款', rankings: '净额排名' })[kind] }} CSV</button></div>
      </template>
      <template v-if="tab === 'funds'"><div v-for="(balances, currency) in result.summary || {}" :key="currency"><h3>{{ currency }}</h3><dl class="metrics"><div v-for="key in ['payable', 'receivable', 'internal']" :key="key"><dt>{{ labels[key] }}</dt><dd>{{ money(balances[key]) }}</dd></div></dl></div></template>
      <div v-if="tab === 'annual' && access.write" class="toolbar"><button class="primary-btn" @click="open('annual')"><Save :size="15" />创建年度草稿</button></div>
      <section v-if="tab === 'annual'" class="annual-view" aria-label="年度动态汇总"><form class="toolbar" @submit.prevent="loadAnnualView"><label>年度<input v-model="annualYear" type="number" min="1900" max="9998" required /></label><button class="secondary-btn" :disabled="annualBusy"><RefreshCw :size="15" />查看动态汇总</button></form><p v-if="annualError" class="error" role="alert">{{ annualError }}</p><p v-if="annualBusy" role="status">正在读取年度汇总…</p><template v-if="annualData"><h3>年度动态汇总 · {{ annualYear }}</h3><SettlementBalanceBridge :bridge="annualData.balance_bridge" /><div class="table-scroll"><table><thead><tr><th>月份</th><th v-for="(label, key) in amountLabels" :key="key">{{ label }}</th><th v-for="(label, key) in countLabels" :key="key">{{ label }}</th></tr></thead><tbody><tr v-for="m in annualData.monthly || []" :key="m.month"><td>{{ m.month }}</td><td v-for="(label, key) in amountLabels" :key="key">{{ money(m[key]) }}</td><td v-for="(label, key) in countLabels" :key="key">{{ m[key] ?? '—' }}</td></tr></tbody></table></div></template></section>
      <p v-if="state.busy" role="status">正在加载…</p>
      <div v-else class="table-scroll"><table><thead><tr><th v-for="[key, label] in columns" :key="key">{{ label }}</th><th>操作</th></tr></thead><tbody>
        <tr v-for="(row, index) in rows" :key="row.id || index"><td v-for="[key] in columns" :key="key">{{ cell(row, key) }}</td><td><div class="row-actions"><button class="icon-btn" title="查看详情" aria-label="查看详情" @click="detail = row"><Eye :size="15" /></button><button v-if="tab === 'funds' && canRegister(row)" class="secondary-btn" @click="open('create', row)"><Save :size="14" />登记</button><button v-if="tab === 'transactions' && canConfirm(row)" class="secondary-btn" @click="open('confirm', row)"><Check :size="14" />复核</button><button v-if="tab === 'annual' && canFinalizeAnnual(row, user, access.write)" class="secondary-btn" :disabled="submitting" @click="open('finalize', row)"><Check :size="14" />复核定稿</button><button v-if="tab === 'annual'" class="icon-btn" title="下载年度快照" aria-label="下载年度快照" :disabled="downloading" @click="download('', row)"><Download :size="15" /></button></div></td></tr>
        <tr v-if="!rows.length"><td :colspan="columns.length + 1" class="empty">暂无符合条件的记录</td></tr>
      </tbody></table></div>
      <PaginationControls v-if="tab !== 'reports' || reportView === 'details'" :page="result.page || page" :page-size="result.page_size || pageSize" :total="result.total ?? rows.length" :busy="state.busy" @page="paginate" @size="resize" />
      <aside v-if="detail" class="detail" aria-label="记录详情"><header><h3>记录详情</h3><button class="icon-btn" aria-label="关闭详情" title="关闭详情" @click="detail = null"><X :size="16" /></button></header><dl><div v-for="([key, label]) in columns" :key="key"><dt>{{ label }}</dt><dd>{{ cell(detail, key) }}</dd></div><div v-for="[key, label] in detailFields" :key="key"><dt>{{ label }}</dt><dd>{{ detail[key] ?? '—' }}</dd></div></dl><div v-if="detail.allocations" class="table-scroll"><table><thead><tr><th>分配资金行编号</th><th>分配金额</th></tr></thead><tbody><tr v-for="item in detail.allocations" :key="item.position_id"><td>{{ item.position_id }}</td><td>{{ money(item.amount) }}</td></tr></tbody></table></div></aside>
      </template>
    </template>
    <SettlementReportEvidence v-if="detail && tab === 'reports' && reportView === 'details' && !isBeneficiary" :row="detail" />
    <div v-if="modal" class="modal-backdrop" @click.self="close" @keydown="modalKeys"><section ref="modalElement" class="finance-modal" role="dialog" aria-modal="true" aria-labelledby="finance-modal-title"><header><h3 id="finance-modal-title">{{ modal.kind === 'annual' ? '年度草稿快照' : modal.kind === 'finalize' ? '复核年度正式快照' : modal.kind === 'confirm' ? '复核资金凭证' : '登记资金凭证' }}</h3><button class="icon-btn" title="关闭" aria-label="关闭" :disabled="submitting" @click="close"><X :size="18" /></button></header>
      <form @submit.prevent="submit"><p v-if="modal.row">{{ modal.row.settlement_no || modal.row.id }} · {{ modal.row.participant_name }} · {{ modal.row.currency }}</p>
        <label v-if="modal.kind === 'annual'">年度<input v-model="form.year" type="number" min="1900" max="9998" required /></label>
        <template v-if="modal.kind === 'finalize'"><p>快照校验值：{{ modal.row.digest }}</p><label>对账凭据<input v-model="form.reference" maxlength="240" required /></label></template>
        <template v-if="modal.kind === 'create'">
          <label>业务类型<select v-model="form.kind" :disabled="submitting"><option :value="modal.row.direction === 'receivable' ? 'recovery' : 'payout'">{{ modal.row.direction === 'receivable' ? '回款' : '付款' }}</option><option v-if="modal.row.direction === 'receivable'" value="offset">抵扣</option></select></label>
          <label>登记方式<select v-model="form.mode" :disabled="submitting"><option value="manual">人工登记</option><option value="simulated">模拟确认</option></select></label>
          <fieldset v-if="form.kind === 'payout'" class="payment-selection" :disabled="submitting">
            <legend>付款资金行</legend>
            <div class="payment-heading"><span>{{ modal.row.participant_name }} · {{ modal.row.currency }}</span><button type="button" class="icon-btn" title="重新加载可付款资金行" aria-label="重新加载可付款资金行" :disabled="paymentLoading || submitting" @click="loadPaymentRows"><RefreshCw :size="15" /></button></div>
            <p v-if="paymentLoading" role="status">正在核对同主体、同币种资金行…</p>
            <p v-if="paymentLoadError" class="error" role="alert">{{ paymentLoadError }}</p>
            <div class="payment-options">
              <div v-for="position in paymentRows" :key="position.id" class="payment-row">
                <label class="payment-choice"><input v-model="selectedPaymentIds" type="checkbox" :value="position.id" :disabled="submitting || paymentLoading" /><span><strong>{{ labels[position.kind] }}</strong><small>{{ position.settlement_no }} · {{ position.order_no }}</small><small>未结 {{ money(position.balance) }} {{ position.currency }}</small></span></label>
                <label class="payment-amount">本次金额<input v-model="paymentAmounts[position.id]" inputmode="decimal" :aria-label="`${position.settlement_no} ${labels[position.kind]}本次金额`" :disabled="submitting || paymentLoading || !selectedPaymentIds.includes(position.id)" :required="selectedPaymentIds.includes(position.id)" /></label>
              </div>
              <p v-if="!paymentLoading && !paymentRows.length">暂无可付款资金行</p>
            </div>
            <div class="payment-total"><span>已选 {{ selectedPaymentIds.length }} 项</span><strong>合计 {{ money(paymentPreview.total) }} {{ modal.row.currency }}</strong></div>
            <p v-if="!paymentLoading && paymentPreview.error" class="error" role="alert">{{ paymentPreview.error }}</p>
          </fieldset>
          <label v-else>金额（未结 {{ money(modal.row.balance) }}）<input v-model="form.amount" inputmode="decimal" required /></label>
          <label v-if="form.kind === 'offset'">对向应付行 ID（同主体、同币种）<input v-model="form.counter_position_id" required /></label>
          <label>业务凭证 / 参考号<input v-model="form.reference" maxlength="200" required /></label>
        </template>
        <label>原因<textarea v-model="form.note" maxlength="2000" required /></label><p v-if="state.error" class="error" role="alert">{{ state.error }}</p><footer><button type="button" class="secondary-btn" :disabled="submitting" @click="close">取消</button><button class="primary-btn" :disabled="submitting || paymentLoading || (modal.kind === 'create' && form.kind === 'payout' && (!!paymentLoadError || !!paymentPreview.error))"><component :is="['confirm', 'finalize'].includes(modal.kind) ? Check : Save" :size="15" />{{ submitting ? '提交中…' : modal.kind === 'confirm' ? '确认复核' : modal.kind === 'finalize' ? '确认定稿' : '保存草稿' }}</button></footer>
      </form></section></div>
  </section>
</template>

<style scoped>
.settlement-finance{width:100%;min-width:0;color:#27313f;font-size:12px;letter-spacing:0}.workspace-heading,.detail header,.finance-modal header{display:flex;align-items:center;gap:12px}.workspace-heading h2{font-size:18px;margin:0 auto 16px 0}.workspace-heading span,.scope-note{color:#697586}.scope-note{margin:10px 0;line-height:1.7}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(150px,100%),1fr));margin:18px 0;border-block:1px solid #e5e6eb;gap:0}.metrics>div{padding:14px 12px;min-width:0}.metrics dt{color:#697586;font-size:12px}.metrics dd{margin:8px 0 0;font-size:20px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.amounts dd{font-size:16px}.toolbar,.row-actions{display:flex;align-items:center;flex-wrap:wrap;gap:8px}.toolbar{margin:14px 0}.table-scroll{max-width:100%;overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px}th,td{padding:11px 12px;border-bottom:1px solid #e5e6eb;text-align:left;white-space:nowrap}th{background:#f7f8fa;color:#596575;font-weight:500}td{font-variant-numeric:tabular-nums}.empty{text-align:center;padding:36px;color:#697586}.detail{border-top:2px solid #d9dde3;margin-top:20px;padding-top:14px}.detail h3,.finance-modal h3{font-size:15px;margin-right:auto}.detail dl{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(190px,100%),1fr));gap:16px}.detail dt{color:#697586}.detail dd{margin:5px 0;overflow-wrap:anywhere}.detail pre{white-space:pre-wrap;overflow-wrap:anywhere}.modal-backdrop{position:fixed;inset:0;z-index:1000;background:#0006;display:flex;align-items:center;justify-content:center;padding:20px}.finance-modal{box-sizing:border-box;width:520px;max-width:100%;max-height:90vh;overflow:auto;background:white;border-radius:6px;padding:20px}.finance-modal form{display:grid;gap:14px}.finance-modal label{display:grid;gap:6px;font-size:12px}.finance-modal input,.finance-modal select,.finance-modal textarea{box-sizing:border-box;width:100%;min-width:0;border:1px solid #d9dde3;border-radius:4px;padding:8px;font:inherit}.finance-modal textarea{min-height:80px}.finance-modal footer{display:flex;justify-content:flex-end;gap:10px}.error{color:#bd2929}button{display:inline-flex;align-items:center;justify-content:center;gap:6px;max-width:100%}.icon-btn{width:32px;height:32px;flex-shrink:0}.settlement-finance :focus-visible{outline:2px solid #165dff;outline-offset:2px}@media(max-width:600px){.workspace-heading{flex-wrap:wrap}.workspace-heading h2{width:100%;margin-bottom:4px}.modal-backdrop{padding:10px}.finance-modal{padding:16px}.metrics>div{padding:12px 6px}}
</style>
<style scoped>
.annual-view{min-width:0;padding:12px 0;border-bottom:1px solid #e5e6eb}.annual-view h3{font-size:14px}.annual-view label{display:grid;gap:5px;font-size:12px}.annual-view input{box-sizing:border-box;width:120px;height:34px;padding:7px 8px;border:1px solid #d9dde3;border-radius:4px;font:inherit}
.payment-selection{min-width:0;margin:0;padding:10px 12px;border:1px solid #d9dde3;border-radius:4px}.payment-selection legend{padding:0 5px;font-size:12px}.payment-heading,.payment-total{display:flex;align-items:center;justify-content:space-between;gap:8px;overflow-wrap:anywhere}.payment-heading span{min-width:0}.payment-options{max-height:280px;overflow:auto}.payment-row{display:grid;grid-template-columns:minmax(0,1fr) 110px;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid #e5e6eb}.finance-modal .payment-choice{display:flex;flex-direction:row;align-items:start;gap:8px;min-width:0}.finance-modal .payment-choice input{width:16px;height:16px;flex:0 0 16px;padding:0;margin:2px 0}.payment-choice span{display:grid;gap:5px;min-width:0;overflow-wrap:anywhere}.payment-choice strong{font-weight:500}.payment-choice small{font-size:12px;color:#697586}.finance-modal .payment-amount{min-width:0;font-size:12px}.payment-total{padding-top:12px;flex-wrap:wrap}.payment-total strong{font-size:14px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
</style>
