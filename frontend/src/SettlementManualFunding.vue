<script setup>
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue';
import { Check, ClipboardCheck, Plus, RefreshCw, Save, X } from 'lucide-vue-next';
import PaginationControls from './PaginationControls.vue';
import { money } from './settlementFinance.js';
import { canVerifyFunding, fundingAccess, fundingContext } from './settlementFunding.js';
import { canReviewManual, createManualSession, manualStatuses } from './settlementManualFunding.js';
const props = defineProps({ api: { type: Object, required: true }, user: Object, enterprise: Object, businessView: String });
const emit = defineEmits(['changed']);
const context = () => ({ api: props.api, user: props.user, enterprise: props.enterprise, businessView: props.businessView });
const access = computed(() => fundingAccess(context()));
const state = reactive({ items: [], requests: [], busy: false, saving: false, error: '' });
const session = createManualSession(context, state);
const modal = ref(null), dialog = ref(null), page = ref(1), pageSize = ref(20), query = ref('');
const form = reactive({});
const eligible = computed(() => state.requests.filter(row => row.kind === 'external_receipt' && row.status === 'approved' && row.version === 2 && !state.items.some(item => item.funding_request_id === row.id)));
const selected = computed(() => eligible.value.find(row => row.id === form.funding_request_id));
const filtered = computed(() => state.items.filter(row => [row.reference, row.settlement_id, row.funding_request_id].some(value => String(value || '').toLowerCase().includes(query.value.trim().toLowerCase()))));
const rows = computed(() => filtered.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value));
const requestFor = row => state.requests.find(request => request.id === row.funding_request_id);
let previousFocus;
function open(action, row) {
  if (state.saving || !access.value.write) return;
  previousFocus = document.activeElement;
  Object.assign(form, { funding_request_id: '', reference: '', paid_on: '', evidence_refs: '', note: '', decision: 'verified', evidence: action === 'verify' ? row : null });
  modal.value = { action, row, key: fundingContext(context()) };
  state.error = '';
  nextTick(() => dialog.value?.querySelector('button:not(:disabled)')?.focus());
}
function close() { if (!state.saving) { modal.value = null; previousFocus?.focus?.(); } }
function keydown(event) {
  if (event.key === 'Escape') { close(); return; }
  if (event.key !== 'Tab') return;
  const controls = [...dialog.value.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled)')];
  const first = controls[0], last = controls.at(-1);
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
}
async function submit() {
  const active = modal.value;
  if (!active || active.key !== fundingContext(context()) || state.busy) return;
  const row = active.action === 'register' ? selected.value : active.action === 'verify' ? requestFor(active.row) : active.row;
  if (await session.submit(active.action, form, row)) {
    if (modal.value !== active || active.key !== fundingContext(context())) return;
    modal.value = null; previousFocus?.focus?.(); emit('changed'); await session.load();
  }
}
function resize(size) { pageSize.value = size; page.value = 1; }
watch(query, () => { page.value = 1; });
watch(() => filtered.value.length, () => { page.value = Math.min(page.value, Math.max(1, Math.ceil(filtered.value.length / pageSize.value))); });
watch(() => fundingContext(context()), () => { session.reset(); modal.value = null; page.value = 1; query.value = ''; session.load(); }, { immediate: true });
onBeforeUnmount(() => session.dispose());
</script>

<template>
  <section v-if="access.read" class="manual-funding" aria-label="人工付款凭据">
    <header><h3>人工付款凭据</h3><div class="tools"><label>筛选<input v-model="query" placeholder="付款引用 / 清算单" /></label><button class="icon-btn" title="刷新人工凭据" aria-label="刷新人工凭据" :disabled="state.busy || state.saving" @click="session.load"><RefreshCw :size="15" /></button><button v-if="access.write" class="icon-btn" title="登记人工付款凭据" aria-label="登记人工付款凭据" :disabled="state.busy || state.saving" @click="open('register')"><Plus :size="15" /></button></div></header>
    <p>人工凭据登记及独立复核，不是银行自动对账。银行到账验证：否。</p>
    <p v-if="state.error && !modal" class="error" role="alert">{{ state.error }}</p>
    <div class="table-scroll"><table><thead><tr><th>清算单 / 补差提案</th><th>付款引用 / 日期</th><th>金额</th><th>状态 / 复核人</th><th>证据 / 说明</th><th>操作</th></tr></thead><tbody>
      <tr v-for="row in rows" :key="row.id"><td>{{ row.settlement_id }}<small>{{ row.funding_request_id }}</small></td><td>{{ row.reference }}<small>{{ row.paid_on }}</small></td><td>{{ money(row.amount) }} {{ row.currency }}</td><td>{{ manualStatuses[row.status] || row.status }}<small>{{ row.reviewer_id || '—' }}</small></td><td><small v-for="reference in row.evidence_refs" :key="reference">{{ reference }}</small>{{ row.note }}<small v-if="row.review_note">复核：{{ row.review_note }}</small></td><td><div class="tools"><button v-if="canReviewManual(row, context())" class="icon-btn" title="独立复核人工凭据" aria-label="独立复核人工凭据" :disabled="state.saving" @click="open('review', row)"><Check :size="15" /></button><button v-if="row.status === 'verified' && canVerifyFunding(requestFor(row), context())" class="icon-btn" title="使用人工凭据核验补差" aria-label="使用人工凭据核验补差" :disabled="state.saving" @click="open('verify', row)"><ClipboardCheck :size="15" /></button></div></td></tr>
      <tr v-if="!rows.length"><td colspan="6">{{ state.busy ? '读取中…' : '暂无人工付款凭据' }}</td></tr>
    </tbody></table></div>
    <PaginationControls :page="page" :page-size="pageSize" :total="filtered.length" :busy="state.busy || state.saving" local label="人工凭据分页" @page="page = $event" @size="resize" />
    <div v-if="modal" class="overlay"><section ref="dialog" class="manual-dialog" role="dialog" aria-modal="true" aria-labelledby="manual-title" @keydown="keydown"><header><h3 id="manual-title">{{ { register: '登记人工付款凭据', review: '独立复核人工凭据', verify: '核验补差覆盖' }[modal.action] }}</h3><button class="icon-btn" title="关闭" aria-label="关闭" :disabled="state.saving" @click="close"><X :size="16" /></button></header>
      <form @submit.prevent="submit"><fieldset :disabled="state.saving || state.busy">
        <template v-if="modal.action === 'register'"><label>已批准外部补差提案<select v-model="form.funding_request_id" required><option value="">请选择</option><option v-for="row in eligible" :key="row.id" :value="row.id">{{ row.settlement_no || row.settlement_id }} · {{ row.bearer?.participant_name }} · {{ money(row.amount) }} {{ row.currency }} · {{ row.reason }}</option></select></label><p v-if="!eligible.length">暂无可登记的已批准外部补差提案。</p><p v-if="selected">完整缺口：{{ money(selected.amount) }} {{ selected.currency }}；承担主体：{{ selected.bearer?.participant_name }}</p><label>付款凭据引用<input v-model="form.reference" maxlength="240" required /></label><label>实际付款日期<input v-model="form.paid_on" type="date" required /></label><label>证据引用（每行一条）<textarea v-model="form.evidence_refs" required /></label></template>
        <template v-else><p>{{ modal.row.reference }} · {{ money(modal.row.amount) }} {{ modal.row.currency }} · {{ modal.row.paid_on }}</p><p v-for="reference in modal.row.evidence_refs" :key="reference">{{ reference }}</p><p>登记人：{{ modal.row.creator_id }}；复核人：{{ modal.row.reviewer_id || '—' }}</p></template>
        <label v-if="modal.action === 'review'">复核结果<select v-model="form.decision"><option value="verified">人工凭据有效</option><option value="rejected">拒绝</option></select></label>
        <label v-if="modal.action !== 'verify'">{{ modal.action === 'review' ? '复核依据' : '登记说明' }}<textarea v-model="form.note" maxlength="4000" required /></label>
        <p>银行到账验证：否。{{ modal.action === 'verify' ? '仅使用已经独立复核的人工凭据登记资金覆盖。' : '' }}</p>
      </fieldset><p v-if="state.error" class="error" role="alert">{{ state.error }}</p><footer><button type="button" :disabled="state.saving" @click="close">取消</button><button type="submit" :disabled="state.saving || state.busy || modal.action === 'register' && !selected"><Save :size="14" />确认提交</button></footer></form>
    </section></div>
  </section>
</template>

<style scoped>
.manual-funding{border-top:1px solid #e6e8eb;margin-top:20px;padding-top:12px;font-size:12px;color:#303642;letter-spacing:0}.manual-funding header,.tools,footer{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.manual-funding header{justify-content:space-between}h3{font-size:13px;margin:0}.tools label{display:flex;align-items:center;gap:6px}input,select,textarea{box-sizing:border-box;font:inherit;border:1px solid #d8dce1;border-radius:4px;background:#fff;padding:7px 8px;min-width:0;max-width:100%}input,select{min-height:34px}.icon-btn{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;padding:0;border:1px solid #d8dce1;border-radius:4px;background:#fff;color:#344b5f}.table-scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-size:12px}th,td{text-align:left;padding:8px;border-bottom:1px solid #e6e8eb;max-width:260px;overflow-wrap:anywhere}th{background:#f5f6f8;font-weight:500}small{display:block;font-size:12px;color:#697481}.error{color:#b42318}.overlay{position:fixed;inset:0;z-index:1110;background:#0006;display:flex;align-items:center;justify-content:center;padding:16px}.manual-dialog{box-sizing:border-box;width:540px;max-width:100%;max-height:90vh;overflow:auto;background:#fff;padding:20px;border-radius:6px}fieldset{border:0;padding:0;min-width:0}.manual-dialog label{display:flex;flex-direction:column;gap:4px;margin:10px 0}textarea{min-height:72px}footer{justify-content:flex-end}footer button{display:inline-flex;gap:6px;align-items:center;padding:7px 10px;background:#fff;border:1px solid #d8dce1;border-radius:4px;font:inherit}button:disabled{opacity:.5}:focus-visible{outline:2px solid #237a68;outline-offset:2px}@media(max-width:600px){table{min-width:700px}.manual-dialog{padding:14px}}
</style>
