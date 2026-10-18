<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue';
import { Save, Plus, Trash2, Check, X, RefreshCw, Eye } from 'lucide-vue-next';
import ConsoleTabs from './ConsoleTabs.vue';
import PaginationControls from './PaginationControls.vue';
import SettlementFundingManagement from './SettlementFundingManagement.vue';
import { financeContext, financeScope, safeError, money } from './settlementFinance.js';
import { partyKinds, managementSide, canDecideCostException, allocationPayload, allocationCandidates, costExceptionPayload, managementSession, managementDirectory } from './settlementManagement.js';
const props = defineProps({ api: Object, user: Object, enterprise: Object, businessView: String });
const emit = defineEmits(['changed']);
const side = computed(() => managementSide(props.user, props.enterprise, props.businessView));
const context = () => JSON.stringify([financeContext(props.user, props.enterprise, props.businessView, props.api), tab.value, side.value, props.enterprise?.verification_status, props.user?.enterprise_memberships]);
const scope = () => financeScope(props.user, props.enterprise, props.businessView);
const session = managementSession(props.api, context, scope, () => !!side.value);
const tab = ref('parties'), parties = ref([]), exceptions = ref([]), detail = ref(null), editing = ref('');
const busy = ref(false), saving = ref(false), error = ref(''), notice = ref(''), productId = ref(''), configuration = ref(null), allocations = ref([]), decision = ref(null), page = ref(1), pageSize = ref(20);
const products = ref([]), refunds = ref([]), selectedRefund = ref(null), refundBasis = ref([]), optionsBusy = ref(false), optionQuery = ref(''), optionPage = ref(1), optionPageSize = ref(100), optionTotal = ref(0);
let optionSequence = 0;
const party = reactive({ name: '', kind: 'provider', enterprise_id: '', user_id: '' });
const exception = reactive({ refund_id: '', retained_cost: '', bearer_participant_id: '', reason: '', evidence_refs: '' });
const reason = ref('');
const tabs = [{ key: 'parties', label: '收益主体' }, { key: 'allocations', label: '产品分配' }, { key: 'exceptions', label: '成本例外' }, { key: 'funding', label: '补差责任' }];
const visibleRows = computed(() => (tab.value === 'exceptions' ? exceptions.value : parties.value).slice((page.value - 1) * pageSize.value, page.value * pageSize.value));
const productParties = computed(() => allocationCandidates(parties.value, configuration.value));
function resize(value) { pageSize.value = value; page.value = 1; }
async function loadOptions(kind, targetPage = 1) {
  if (!side.value) return;
  const sequence = ++optionSequence, key = context(); optionsBusy.value = true; error.value = '';
  try {
    const request = managementDirectory(props.user, kind, optionQuery.value.trim(), targetPage, optionPageSize.value);
    const { data } = await props.api.get(request.path, { params: { ...scope(), ...request.params } });
    if (sequence !== optionSequence || key !== context()) return;
    const rows = data.items || [];
    if (kind === 'products') products.value = [...new Map(rows.map(row => [row.product_id || row.id, { id: row.product_id || row.id, name: row.product_name || row.name }])).values()];
    else refunds.value = rows.map(row => row.refund_id ? { ...row, id: row.refund_id, amount: String(row.gross_amount || '').replace(/^-/, ''), status: 'completed' } : row).filter(row => ['approved', 'completed'].includes(row.status));
    optionPage.value = targetPage; optionTotal.value = data.total ?? rows.length;
  } catch (e) { if (sequence === optionSequence && key === context()) error.value = safeError(e); }
  finally { if (sequence === optionSequence && key === context()) optionsBusy.value = false; }
}
async function chooseRefund() {
  const row = refunds.value.find(r => r.id === exception.refund_id);
  selectedRefund.value = row || null; refundBasis.value = [];
  if (!row) return;
  const sequence = ++optionSequence, key = context(); optionsBusy.value = true;
  try {
    const { data } = await props.api.get('/settlement-reporting/report', { params: { ...scope(), start: '1900-01-01', end: '9998-12-31', q: row.order_no || row.product_name || '', page: 1, page_size: 500 } });
    if (sequence === optionSequence && key === context() && selectedRefund.value === row) refundBasis.value = (data.items || []).filter(r => r.order_id === row.order_id && r.baseline_hash);
  } catch (e) { if (sequence === optionSequence && key === context()) error.value = safeError(e); }
  finally { if (sequence === optionSequence && key === context()) optionsBusy.value = false; }
}
const statusLabel = value => ({ pending: '待对侧审批', approved: '已批准', rejected: '已拒绝' })[value] || value;
async function load() {
  const key = context(); busy.value = true; error.value = '';
  try {
    await session.read('/settlement-parties', data => { parties.value = data.items || []; });
    if (key === context() && tab.value === 'exceptions') await session.read('/settlement-cost-exceptions', data => { exceptions.value = data.items || []; });
  }
  catch (e) { error.value = safeError(e); }
  finally { if (key === context()) busy.value = false; }
}
async function loadConfiguration() {
  if (!productId.value.trim() || saving.value) return;
  configuration.value = null; allocations.value = []; error.value = ''; busy.value = true;
  const id = productId.value.trim(), key = context();
  try { await session.read(`/settlement-parties/products/${encodeURIComponent(id)}/allocations`, data => { if (id === productId.value.trim()) { configuration.value = data; allocations.value = (data.allocations || []).map(a => ({ ...a })); } }); }
  catch (e) { error.value = safeError(e); }
  finally { if (key === context()) busy.value = false; }
}
function open(kind) { if (saving.value) return; editing.value = kind; decision.value = null; error.value = ''; notice.value = ''; if (kind === 'party') Object.assign(party, { name: '', kind: 'provider', enterprise_id: side.value === 'provider' ? props.enterprise.id : '', user_id: '' }); else { Object.assign(exception, { refund_id: '', retained_cost: '', bearer_participant_id: '', reason: '', evidence_refs: '' }); selectedRefund.value = null; refundBasis.value = []; optionQuery.value = ''; loadOptions('refunds'); } }
function review(row, value) { if (!canDecideCostException(row, props.user, side.value) || saving.value) return; decision.value = { row, value }; reason.value = ''; editing.value = ''; }
async function submit(kind) {
  if (saving.value || !side.value) return;
  const key = context(); saving.value = true; error.value = ''; notice.value = '';
  try {
    let path, payload;
    if (kind === 'party') {
      if (!party.name.trim() || party.kind !== 'platform' && !party.enterprise_id.trim() && !party.user_id.trim()) throw new Error('请填写名称及真实企业或用户编号');
      path = '/settlement-parties'; payload = { ...party, name: party.name.trim(), enterprise_id: party.enterprise_id.trim(), user_id: party.user_id.trim(), currency: 'CNY' };
      if (side.value === 'provider' && (payload.enterprise_id !== props.enterprise.id || party.kind === 'platform')) throw new Error('只能配置本企业收益主体');
    } else if (kind === 'allocations') {
      if (!configuration.value || configuration.value.product_id !== productId.value.trim()) throw new Error('请先读取当前产品配置');
      path = `/settlement-parties/products/${encodeURIComponent(configuration.value.product_id)}/allocations`; payload = { allocations: allocationPayload(allocations.value, productParties.value) };
    } else if (kind === 'exception') { if (!selectedRefund.value || selectedRefund.value.id !== exception.refund_id || optionsBusy.value) throw new Error('请先选择并核对退款对象'); path = '/settlement-cost-exceptions'; payload = costExceptionPayload(exception); }
    else {
      const action = decision.value;
      if (!action || !canDecideCostException(action.row, props.user, side.value)) throw new Error('仅对侧独立审批人可审批');
      if (!reason.value.trim()) throw new Error('请填写审批依据');
      path = `/settlement-cost-exceptions/${encodeURIComponent(action.row.id)}/decision`; payload = { decision: action.value, expected_version: action.row.version, reason: reason.value.trim() };
    }
    const done = await session.write(path, payload, data => { if (kind === 'allocations') configuration.value = data; });
    if (!done || key !== context()) return;
    editing.value = ''; decision.value = null; notice.value = '已保存'; await load();
  } catch (e) { if (key === context()) error.value = safeError(e); }
  finally { if (key === context()) saving.value = false; }
}
watch(() => [context(), tab.value], () => { session.reset(); optionSequence++; products.value = []; refunds.value = []; selectedRefund.value = null; refundBasis.value = []; optionsBusy.value = false; optionQuery.value = ''; parties.value = []; exceptions.value = []; detail.value = null; configuration.value = null; allocations.value = []; productId.value = ''; editing.value = ''; decision.value = null; saving.value = false; busy.value = false; notice.value = ''; page.value = 1; if (side.value && tab.value !== 'funding') { load(); if (tab.value === 'allocations') loadOptions('products'); } }, { immediate: true });
watch(productId, () => { configuration.value = null; allocations.value = []; });
onBeforeUnmount(() => { optionSequence++; session.dispose(); });
</script>
<template>
  <section v-if="side" class="settlement-management" aria-label="清算主体与成本例外管理">
    <ConsoleTabs v-model="tab" :tabs="tabs" label="清算管理" />
    <SettlementFundingManagement v-if="tab === 'funding'" :key="context()" :api="api" :user="user" :enterprise="enterprise" :business-view="businessView" @changed="emit('changed', { kind: 'funding' })" />
    <template v-else>
    <div class="toolbar"><button class="icon-btn" aria-label="刷新管理记录" title="刷新管理记录" :disabled="busy || saving" @click="load"><RefreshCw :size="16" /></button><button v-if="tab !== 'allocations'" class="secondary-btn" :disabled="saving || busy" @click="open(tab === 'parties' ? 'party' : 'exception')"><Plus :size="15" />{{ tab === 'parties' ? '登记收益主体' : '申请成本例外' }}</button></div>
    <p v-if="error" class="error" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p><p v-if="busy" role="status">正在加载…</p>
    <form v-if="editing === 'party'" class="editor" @submit.prevent="submit('party')"><h3>登记收益主体</h3><fieldset :disabled="saving"><label>名称<input v-model="party.name" required maxlength="180" /></label><label>收益类别<select v-model="party.kind"><option v-for="(name, kind) in partyKinds" v-show="side === 'platform' || kind !== 'platform'" :key="kind" :value="kind">{{ name }}</option></select></label><label>企业编号<input v-model="party.enterprise_id" :readonly="side === 'provider'" maxlength="36" /></label><label>关联用户编号<input v-model="party.user_id" maxlength="36" /></label><span>币种：CNY</span><div class="toolbar"><button class="primary-btn"><Save :size="15" />保存主体</button><button type="button" class="secondary-btn" @click="editing = ''"><X :size="15" />取消</button></div></fieldset></form>
    <form v-if="editing === 'exception'" class="editor" @submit.prevent="submit('exception')">
      <h3>不可返还成本例外申请</h3><fieldset :disabled="saving">
        <div class="product-filter"><label>退款单检索<input v-model="optionQuery" /></label><button type="button" class="secondary-btn" :disabled="optionsBusy" @click="loadOptions('refunds')"><RefreshCw :size="15" />检索</button></div>
        <label>退款对象<select v-model="exception.refund_id" required :disabled="optionsBusy" @change="chooseRefund"><option value="">请选择已批准或已退款记录</option><option v-for="r in refunds" :key="r.id" :value="r.id">{{ r.refund_no || r.settlement_no || r.id }} · {{ r.product_name || r.order_no }} · {{ money(r.amount) }} CNY</option></select></label>
        <PaginationControls :page="optionPage" :page-size="optionPageSize" :total="optionTotal" :busy="optionsBusy" @page="loadOptions('refunds', $event)" @size="optionPageSize = $event; loadOptions('refunds')" />
        <dl v-if="selectedRefund"><dt>原订单</dt><dd>{{ selectedRefund.order_no || selectedRefund.order_id }}</dd><dt>退款金额</dt><dd>{{ money(selectedRefund.amount) }} {{ selectedRefund.currency || 'CNY' }}</dd></dl>
        <div v-if="selectedRefund" class="table-scroll"><table><thead><tr><th>已冻结清算依据</th><th>批次</th><th>最终校验值</th><th>成本</th></tr></thead><tbody><tr v-for="b in refundBasis" :key="b.id"><td>{{ b.settlement_no }}</td><td>{{ b.batch_no || '—' }}</td><td>{{ b.baseline_hash }}</td><td>{{ money(b.cost_amount) }}</td></tr><tr v-if="!refundBasis.length"><td colspan="4">服务端尚未提供可展示的冻结依据；申请资格仍以服务端校验为准</td></tr></tbody></table></div>
        <label>保留成本（CNY）<input v-model="exception.retained_cost" required inputmode="decimal" /></label><label>损失承担主体<select v-model="exception.bearer_participant_id" required><option value="">请选择</option><option v-for="p in parties" :key="p.id" :value="p.id">{{ p.name }} · {{ partyKinds[p.kind] }}</option></select></label><label>申请依据<textarea v-model="exception.reason" required maxlength="4000" /></label><label>证据引用（每行一条）<textarea v-model="exception.evidence_refs" required /></label><div class="toolbar"><button class="primary-btn" :disabled="optionsBusy || !selectedRefund"><Save :size="15" />提交申请</button><button type="button" class="secondary-btn" @click="editing = ''"><X :size="15" />取消</button></div>
      </fieldset></form>
    <form v-if="decision" class="editor" @submit.prevent="submit('decision')"><h3>{{ decision.value === 'approve' ? '批准' : '拒绝' }}成本例外 · {{ decision.row.id }}</h3><fieldset :disabled="saving"><label>审批依据<textarea v-model="reason" required maxlength="4000" /></label><div class="toolbar"><button class="primary-btn"><Check :size="15" />确认{{ decision.value === 'approve' ? '批准' : '拒绝' }}</button><button type="button" class="secondary-btn" @click="decision = null"><X :size="15" />取消</button></div></fieldset></form>
    <template v-if="tab === 'allocations'">
      <form class="product-filter" @submit.prevent="loadOptions('products')"><label>产品名称检索<input v-model="optionQuery" :disabled="saving" /></label><button class="secondary-btn" :disabled="optionsBusy || saving"><RefreshCw :size="15" />检索</button></form>
      <form class="product-filter" @submit.prevent="loadConfiguration"><label>产品<select v-model="productId" required :disabled="saving || optionsBusy"><option value="">请选择产品</option><option v-for="p in products" :key="p.id" :value="p.id">{{ p.name }} · {{ p.id }}</option></select></label><button class="secondary-btn" :disabled="busy || saving || !productId"><RefreshCw :size="15" />读取配置</button></form>
      <PaginationControls :page="optionPage" :page-size="optionPageSize" :total="optionTotal" :busy="optionsBusy" @page="loadOptions('products', $event)" @size="optionPageSize = $event; loadOptions('products')" />
      <form v-if="configuration" class="editor" @submit.prevent="submit('allocations')"><h3>产品分配 · 版本 {{ configuration.version }}</h3><fieldset :disabled="saving || busy"><div v-for="(row, i) in allocations" :key="i" class="allocation-row"><label>收益主体<select v-model="row.participant_id" required><option value="">请选择</option><option v-for="p in productParties" :key="p.id" :value="p.id">{{ p.name }} · {{ partyKinds[p.kind] }}{{ p.configuredOnly ? ' · 当前产品共享主体' : '' }}</option></select></label><label>类别内权重（%）<input v-model="row.weight" inputmode="decimal" required /></label><button type="button" class="icon-btn" title="移除分配" aria-label="移除分配" @click="allocations.splice(i, 1)"><Trash2 :size="15" /></button></div><div class="toolbar"><button type="button" class="secondary-btn" @click="allocations.push({ participant_id: '', weight: '' })"><Plus :size="15" />增加主体</button><button class="primary-btn"><Save :size="15" />保存新版本</button></div></fieldset></form></template>
    <div v-else class="table-scroll"><table><thead><tr v-if="tab === 'parties'"><th>名称</th><th>类别</th><th>企业编号</th><th>用户编号</th><th>币种</th></tr><tr v-else><th>申请编号 / 退款关联</th><th>保留成本</th><th>承担主体</th><th>申请侧</th><th>状态</th><th>操作</th></tr></thead><tbody><tr v-for="row in visibleRows" :key="row.id"><template v-if="tab === 'parties'"><td>{{ row.name }}</td><td>{{ partyKinds[row.kind] }}</td><td>{{ row.enterprise_id || '—' }}</td><td>{{ row.user_id || '—' }}</td><td>{{ row.currency }}</td></template><template v-else><td>{{ row.id }}<br />{{ row.refund_id }}</td><td>{{ money(row.retained_cost) }} {{ row.currency }}</td><td>{{ row.bearer_participant_name }}</td><td>{{ row.creator_side === 'platform' ? '平台' : '提供方' }}</td><td>{{ statusLabel(row.status) }}</td><td><div class="toolbar"><button class="icon-btn" title="查看例外证据" aria-label="查看例外证据" @click="detail = row"><Eye :size="15" /></button><template v-if="canDecideCostException(row, user, side)"><button class="secondary-btn" :disabled="saving" @click="review(row, 'approve')"><Check :size="15" />批准</button><button class="secondary-btn" :disabled="saving" @click="review(row, 'reject')"><X :size="15" />拒绝</button></template></div></td></template></tr><tr v-if="!visibleRows.length"><td :colspan="tab === 'parties' ? 5 : 6" class="empty">暂无记录</td></tr></tbody></table></div>
    <PaginationControls v-if="tab !== 'allocations'" :page="page" :page-size="pageSize" :total="(tab === 'exceptions' ? exceptions : parties).length" :busy="busy" @page="page = $event" @size="resize" />
    <aside v-if="detail && tab === 'exceptions'" class="editor"><div class="toolbar"><h3>成本例外证据</h3><button class="icon-btn" title="关闭证据" aria-label="关闭证据" @click="detail = null"><X :size="15" /></button></div><dl><template v-for="[key, label] in [['order_id','订单关联'],['baseline_id','最终基线'],['baseline_hash','基线校验值'],['creator_id','申请人'],['reviewer_id','审批人'],['approved_at','批准时间'],['reason','申请依据'],['decision_reason','审批依据']]" :key="key"><dt>{{ label }}</dt><dd>{{ detail[key] || '—' }}</dd></template><dt>证据引用</dt><dd v-for="(ref, i) in detail.evidence_refs || []" :key="i">{{ ref }}</dd></dl></aside>
    </template>
  </section>
</template>
<style scoped>
.settlement-management{min-width:0;width:100%;font-size:12px;color:#27313f}.toolbar{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin:12px 0}.editor{border-top:1px solid #e5e6eb;padding:14px 0;margin:16px 0}.editor h3{font-size:14px;margin:0 0 12px}.editor fieldset{border:0;margin:0;padding:0;display:grid;gap:12px;max-width:720px;min-width:0}label{display:grid;gap:5px;min-width:0}input,select,textarea{box-sizing:border-box;width:100%;min-width:0;border:1px solid #d9dde3;border-radius:4px;padding:7px 8px;font:inherit;color:inherit;background:white}input,select{height:34px}textarea{min-height:70px}.product-filter{display:flex;align-items:end;flex-wrap:wrap;gap:10px}.product-filter label{width:320px;max-width:100%}.allocation-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(100px,160px) 32px;align-items:end;gap:8px}.table-scroll{max-width:100%;overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px}th,td{text-align:left;border-bottom:1px solid #e5e6eb;padding:10px;white-space:nowrap}th{background:#f7f8fa;font-weight:500;color:#596575}.empty{padding:28px;text-align:center;color:#697586}.error{color:#bd2929}button{display:inline-flex;align-items:center;justify-content:center;gap:6px}.icon-btn{width:32px;height:32px}dl{display:grid;grid-template-columns:140px minmax(0,1fr);gap:10px;max-width:900px}dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}dt{color:#697586}@media(max-width:600px){.allocation-row{grid-template-columns:minmax(0,1fr) 100px 32px}dl{grid-template-columns:1fr;gap:6px}dd{margin-bottom:10px}}
</style>
