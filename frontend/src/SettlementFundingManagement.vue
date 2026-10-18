<script setup>
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue';
import { Check, ClipboardCheck, Eye, Plus, RefreshCw, Save, X } from 'lucide-vue-next';
import PaginationControls from './PaginationControls.vue';
import { money } from './settlementFinance.js';
import { canDecideFunding, canVerifyFunding, createFundingDirectory, createFundingSession, eligibleFundingSettlement, fundingAccess, fundingContext, fundingKinds, fundingStatuses } from './settlementFunding.js';

const props = defineProps({ api: { type: Object, required: true }, user: Object, enterprise: Object,
  businessView: String, initialPageSize: { type: Number, default: 20 } });
const emit = defineEmits(['changed']);
const context = () => ({ api: props.api, user: props.user, enterprise: props.enterprise, businessView: props.businessView });
const access = computed(() => fundingAccess(context()));
const state = reactive({ items: [], busy: false, saving: false, error: '', notice: '' });
const session = createFundingSession(context, state);
const options = reactive({ items: [], total: 0, page: 1, pageSize: 100, busy: false, error: '' });
const directory = createFundingDirectory(context, options), settlementQuery = ref(''), selectedSettlement = ref(null);
const page = ref(1), pageSize = ref(props.initialPageSize), query = ref(''), modal = ref(null), dialog = ref(null);
const form = reactive({});
const filtered = computed(() => state.items.filter(row => [row.settlement_id, row.id, row.bearer?.participant_name, row.reason].some(value => String(value || '').toLowerCase().includes(query.value.toLowerCase().trim()))));
const rows = computed(() => filtered.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value));
let previousFocus;
function open(action, row = null) {
  if (state.saving || action !== 'detail' && !access.value.write) return;
  previousFocus = typeof document === 'undefined' ? null : document.activeElement;
  Object.assign(form, { settlement_id: '', kind: 'platform_absorption', amount: '', currency: 'CNY',
    bearer_enterprise_id: '', reason: '', evidence_refs: '', decision: 'approved', note: '', receipt_reference: '' });
  modal.value = { action, row, context: fundingContext(context()) };
  directory.reset(); settlementQuery.value = ''; selectedSettlement.value = null;
  if (action === 'propose') directory.load();
  state.error = ''; state.notice = '';
  nextTick(() => dialog.value?.querySelector('button:not(:disabled),input:not(:disabled)')?.focus());
}
function close() { if (!state.saving) { directory.reset(); modal.value = null; previousFocus?.focus?.(); } }
function chooseSettlement() { selectedSettlement.value = options.items.find(row => row.id === form.settlement_id) || (selectedSettlement.value?.id === form.settlement_id ? selectedSettlement.value : null); }
function keydown(event) {
  if (event.key !== 'Tab') return;
  const controls = [...dialog.value.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled)')];
  const first = controls[0], last = controls.at(-1);
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
}
async function submit() {
  const active = modal.value;
  if (!active || active.context !== fundingContext(context())) return;
  if (active.action === 'propose' && (options.busy || !eligibleFundingSettlement(selectedSettlement.value) || selectedSettlement.value.id !== form.settlement_id)) {
    state.error = '请先选择有最终基线的正收入清算单'; return;
  }
  if (await session.submit(active.action, form, active.row)) {
    if (modal.value !== active || active.context !== fundingContext(context())) return;
    modal.value = null; previousFocus?.focus?.(); emit('changed'); await session.load();
  }
}
function resize(size) { pageSize.value = size; page.value = 1; }
watch(query, () => { page.value = 1; });
watch(() => filtered.value.length, () => { page.value = Math.min(page.value, Math.max(1, Math.ceil(filtered.value.length / pageSize.value))); });
watch(() => fundingContext(context()), () => { session.reset(); directory.reset(); selectedSettlement.value = null; modal.value = null; page.value = 1; query.value = ''; session.load(); }, { immediate: true });
onBeforeUnmount(() => { session.dispose(); directory.dispose(); });
</script>

<template>
  <section class="funding-management" aria-label="资金补足管理">
    <div class="funding-toolbar">
      <label>筛选<input v-model="query" placeholder="清算单 / 主体 / 依据" aria-label="筛选资金补足" /></label>
      <span v-if="access.read && !access.write">只读</span>
      <button class="icon-btn" type="button" title="刷新资金补足" aria-label="刷新资金补足" :disabled="!access.read || state.busy || state.saving" @click="session.load"><RefreshCw :size="15" /></button>
      <button v-if="access.write" class="icon-btn" type="button" title="新增补足提案" aria-label="新增补足提案" :disabled="state.saving" @click="open('propose')"><Plus :size="15" /></button>
    </div>
    <p class="funding-boundary">平台承担仅使用已有可用平台利润，不新增外部现金。资金覆盖不代表银行到账，银行到账验证始终为否。</p>
    <p v-if="!access.read" role="status">当前身份无资金补足查看权限。</p>
    <p v-if="state.error && !modal" role="alert" class="funding-error">{{ state.error }}</p>
    <p v-if="state.notice" role="status">{{ state.notice }}</p>
    <div v-if="access.read" class="funding-table" :aria-busy="state.busy">
      <table><thead><tr><th>清算单 / 提案</th><th>方式 / 承担主体</th><th>金额</th><th>状态</th><th>核验条件</th><th>银行到账验证</th><th>操作</th></tr></thead>
        <tbody><tr v-for="row in rows" :key="row.id">
          <td>{{ row.settlement_id }}<small>{{ row.id }}</small></td>
          <td>{{ fundingKinds[row.kind] || row.kind }}<small>{{ row.bearer?.participant_name || '—' }}</small></td>
          <td class="funding-amount">{{ money(row.amount) }} {{ row.currency }}</td>
          <td>{{ fundingStatuses[row.status] || row.status }}</td>
          <td><span v-if="row.kind === 'external_receipt' && row.trusted_receipt_verifier_available !== true">可信核验器未配置，不能确认到账</span><span v-else-if="row.kind === 'platform_absorption'">仅已有可用平台利润</span><span v-else>可信回执核验</span></td>
          <td>否</td>
          <td><div class="funding-actions">
            <button class="icon-btn" type="button" title="查看补足依据" aria-label="查看补足依据" @click="open('detail', row)"><Eye :size="15" /></button>
            <button v-if="canDecideFunding(row, context())" class="icon-btn" type="button" title="审批补足提案" aria-label="审批补足提案" :disabled="state.saving" @click="open('decision', row)"><Check :size="15" /></button>
            <button v-if="canVerifyFunding(row, context())" class="icon-btn" type="button" title="核验资金覆盖" aria-label="核验资金覆盖" :disabled="state.saving" @click="open('verify', row)"><ClipboardCheck :size="15" /></button>
          </div></td>
        </tr><tr v-if="!rows.length"><td colspan="7">{{ state.busy ? '读取中…' : '暂无资金补足提案' }}</td></tr></tbody>
      </table>
    </div>
    <PaginationControls v-if="access.read" :page="page" :page-size="pageSize" :total="filtered.length" :busy="state.busy || state.saving" local label="资金补足分页" @page="page = $event" @size="resize" />
    <div v-if="modal" class="funding-overlay">
      <section ref="dialog" class="funding-dialog" role="dialog" aria-modal="true" aria-labelledby="funding-dialog-title" @keydown="keydown">
        <header><h3 id="funding-dialog-title">{{ ({ propose: '新增补足提案', decision: '独立审批', verify: '核验资金覆盖', detail: '补足依据' })[modal.action] }}</h3><button type="button" class="icon-btn" title="关闭" aria-label="关闭" :disabled="state.saving" @click="close"><X :size="16" /></button></header>
        <template v-if="modal.action === 'detail'">
          <dl><dt>提案人</dt><dd>{{ modal.row.creator_id }}</dd><dt>审批人</dt><dd>{{ modal.row.reviewer_id || '—' }}</dd><dt>基线校验</dt><dd>{{ modal.row.baseline_hash }}</dd><dt>实际收款基数</dt><dd>{{ money(modal.row.actual_receipts) }} {{ modal.row.currency }}</dd><dt>依据</dt><dd>{{ modal.row.reason }}</dd><dt>证据引用</dt><dd v-for="reference in modal.row.evidence_refs" :key="reference">{{ reference }}</dd><dt>银行到账验证</dt><dd>否</dd></dl>
        </template>
        <form v-else @submit.prevent="submit">
          <fieldset :disabled="state.saving">
            <template v-if="modal.action === 'propose'">
              <div class="funding-toolbar"><label>清算单检索<input v-model="settlementQuery" placeholder="SET 编号 / 提供方" aria-label="检索最终清算单" @keydown.enter.prevent="directory.load(settlementQuery)" /></label><button class="icon-btn" type="button" title="检索清算单" aria-label="检索清算单" :disabled="options.busy" @click="directory.load(settlementQuery)"><RefreshCw :size="15" /></button></div>
              <label>最终清算单<select v-model="form.settlement_id" required :disabled="options.busy" @change="chooseSettlement"><option value="">请选择已锁定的正收入清算单</option><option v-if="selectedSettlement && !options.items.some(row => row.id === selectedSettlement.id)" :value="selectedSettlement.id">{{ selectedSettlement.settlement_no }} · {{ selectedSettlement.provider_name }} · 收入 {{ money(selectedSettlement.gross_amount) }} / 成本 {{ money(selectedSettlement.cost_amount) }} CNY</option><option v-for="row in options.items" :key="row.id" :value="row.id">{{ row.settlement_no }} · {{ row.provider_name }} · 收入 {{ money(row.gross_amount) }} / 成本 {{ money(row.cost_amount) }} CNY</option></select></label>
              <p v-if="options.error" role="alert" class="funding-error">{{ options.error }}</p>
              <p v-else-if="!options.busy && !options.items.length">未找到符合条件的清算单。</p>
              <PaginationControls :page="options.page" :page-size="options.pageSize" :total="options.total" :busy="options.busy || state.saving" label="清算单选择分页" @page="directory.load(settlementQuery, $event, options.pageSize)" @size="directory.load(settlementQuery, 1, $event)" />
              <label>补足方式<select v-model="form.kind"><option value="platform_absorption">平台利润承担</option><option value="external_receipt">外部回执</option></select></label>
              <label>完整缺口金额<input v-model="form.amount" inputmode="decimal" required /></label><label>币种<input value="CNY" readonly aria-label="币种 CNY" /></label>
              <label v-if="form.kind === 'external_receipt'">已认证承担企业编号<input v-model="form.bearer_enterprise_id" maxlength="36" required /></label>
              <p v-if="form.kind === 'external_receipt'">外部回执须由已配置的可信核验器核验；提案和审批均不表示到账。</p>
              <label>补足依据<textarea v-model="form.reason" maxlength="4000" required /></label><label>证据引用（每行一条）<textarea v-model="form.evidence_refs" required /></label>
            </template>
            <template v-else-if="modal.action === 'decision'">
              <p>{{ modal.row.settlement_id }} · {{ money(modal.row.amount) }} {{ modal.row.currency }}</p>
              <label>审批结果<select v-model="form.decision"><option value="approved">批准</option><option value="rejected">拒绝</option></select></label><label>审批依据<textarea v-model="form.note" maxlength="4000" required /></label>
            </template>
            <template v-else>
              <p>{{ modal.row.settlement_id }} · {{ money(modal.row.amount) }} {{ modal.row.currency }}</p>
              <label v-if="modal.row.kind === 'external_receipt'">可信回执引用<input v-model="form.receipt_reference" maxlength="240" required /></label>
              <p v-else>仅核销已有可用平台利润；系统将重新校验余额及实际收款约束。</p>
              <p>银行到账验证：否。</p>
            </template>
          </fieldset>
          <p v-if="state.error" role="alert" class="funding-error">{{ state.error }}</p>
          <footer><button type="button" :disabled="state.saving" @click="close">取消</button><button type="submit" :disabled="state.saving"><Save :size="14" />{{ state.saving ? '提交中…' : '确认提交' }}</button></footer>
        </form>
      </section>
    </div>
  </section>
</template>

<style scoped>
.funding-management{font-size:12px;line-height:1.5;letter-spacing:0;color:#303642}.funding-toolbar,.funding-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.funding-toolbar{justify-content:flex-end}.funding-toolbar label{display:flex;align-items:center;gap:6px}.funding-management input,.funding-management select,.funding-management textarea{box-sizing:border-box;font:inherit;border:1px solid #d8dce1;border-radius:4px;background:#fff;padding:6px 8px;min-width:0;max-width:100%}.funding-boundary{color:#59636f;margin:12px 0}.funding-table{overflow-x:auto}.funding-table table{width:100%;border-collapse:collapse;font-size:12px}.funding-table th,.funding-table td{padding:8px;text-align:left;border-bottom:1px solid #e6e8eb;max-width:260px;overflow-wrap:anywhere}.funding-table th{background:#f5f6f8;font-weight:500}.funding-table small{display:block;font-size:12px;color:#697481}.funding-amount{font-variant-numeric:tabular-nums}.funding-management .icon-btn{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;flex:0 0 30px;padding:0;border:1px solid #d8dce1;border-radius:4px;background:#fff;color:#344b5f}.funding-management button:disabled{opacity:.5;cursor:default}.funding-error{color:#b42318}.funding-overlay{position:fixed;inset:0;z-index:1100;background:#0006;display:flex;align-items:center;justify-content:center;padding:16px}.funding-dialog{box-sizing:border-box;width:540px;max-width:100%;max-height:90vh;overflow:auto;background:#fff;padding:20px;border-radius:6px}.funding-dialog header,.funding-dialog footer{display:flex;align-items:center;justify-content:space-between;gap:12px}.funding-dialog h3{font-size:15px;margin:0}.funding-dialog fieldset{border:0;padding:0;margin:12px 0;min-width:0}.funding-dialog label{display:flex;flex-direction:column;gap:4px;margin:10px 0}.funding-dialog textarea{min-height:72px;resize:vertical}.funding-dialog footer{justify-content:flex-end;margin-top:16px}.funding-dialog footer button{display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border:1px solid #cbd2da;border-radius:4px;background:#fff;font:inherit}.funding-dialog dd{margin:0 0 10px;overflow-wrap:anywhere}.funding-dialog dt{color:#697481}.funding-management :focus-visible{outline:2px solid #237a68;outline-offset:2px}@media(max-width:600px){.funding-toolbar{justify-content:flex-start}.funding-dialog{padding:14px}.funding-table table{min-width:760px}}
</style>
