<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
import { ArrowRightLeft, Check, CircleAlert, CreditCard, RefreshCw, RotateCw, X } from "lucide-vue-next";
import { createVersionChangeExecution, versionChangeExecutionPermissions, versionChangeReachedTerminal, versionChangeRefundStatusLabel } from "./versionChangeExecution.js";

const props = defineProps({ api: { type: Object, required: true }, order: { type: Object, required: true }, user: { type: Object, required: true },
  enterprise: { type: Object, default: null }, quoteSession: { type: Object, required: true }, quote: { type: Object, default: null },
  validated: Boolean, canRequest: Boolean, drawerOpen: Boolean, tenantId: { type: String, default: "" }, sessionToken: { type: String, default: "" }, combinationId: { type: String, default: "" } });
const emit = defineEmits(["refresh"]);
const state = ref({ open: false, items: [], loading: false, submitting: false, actionId: "", error: "", confirmItem: null });
const reason = ref("");
const submitConfirm = ref(false);
const execution = createVersionChangeExecution({ api: props.api,
  getContext: () => ({ order: props.order, user: props.user, enterprise: props.enterprise, tenantId: props.tenantId, token: props.sessionToken, combinationId: props.combinationId }), quoteSession: props.quoteSession,
  onChange: value => {
    const refreshParent = versionChangeReachedTerminal(state.value.items, value.items);
    state.value = value;
    if (refreshParent) emit("refresh");
  } });
const quoteReady = computed(() => props.canRequest && props.validated && Boolean(props.quote));
const quotePending = computed(() => state.value.submitting || state.value.loading);
function money(value, currency) {
  if (value == null || !currency) return "-";
  try { return new Intl.NumberFormat("zh-CN", { style: "currency", currency }).format(value); }
  catch { return `${value} ${currency}`; }
}
function date(value) { return value ? new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "-"; }
const statusNames = { pending_payment: "待支付", resource_pending: "资源处理中", refund_pending: "退款处理中", retrying: "重试处理中",
  exception: "处理异常", completed: "已完成", compensating: "补偿处理中", restoring: "恢复处理中", resource_applying: "资源处理中", compensated: "已补偿", cancelled: "已取消" };
const moneyStatuses = { unpaid: "未支付", paying: "支付处理中", paid: "已支付", refunding: "退款处理中", refunded: "已退款", pending: "处理中", not_required: "无需处理", failed: "失败" };
const moneyStatus = value => moneyStatuses[value] || (value ? "处理中" : "无");
const statusClass = status => ["completed", "compensated"].includes(status) ? "done" : status === "exception" ? "error" : "pending";
const permission = item => versionChangeExecutionPermissions(props.user, props.order, item, props.enterprise);
function submit() { submitConfirm.value = true; }
async function confirmSubmit() { submitConfirm.value = false; if (await execution.submit()) { props.quoteSession.reset(); emit("refresh"); } }
function openHistory() { execution.open(); }
async function confirmAction(item) { await execution.confirmPayment(item, reason.value); reason.value = ""; execution.cancelConfirm(); }
watch(() => [props.drawerOpen, props.order.id, props.order.buyer_enterprise_id, props.user.id, props.enterprise?.id, props.tenantId, props.sessionToken, props.combinationId], (current, previous = []) => {
  const [open] = current;
  if (previous.length && current.slice(1).some((value, index) => value !== previous[index + 1])) {
    execution.close();
    if (open) openHistory();
  } else if (open && !previous[0]) openHistory();
  else if (!open && previous[0]) execution.close();
}, { immediate: true });
watch(() => props.quote?.quote_id, (value, previous) => { if (value !== previous && state.value.open) execution.load(); });
watch(() => [props.validated, props.quote?.quote_id], ([validated, quoteId]) => { if (!validated || !quoteId) submitConfirm.value = false; });
onUnmounted(() => execution.dispose());
</script>

<template>
  <section class="vce-panel" aria-label="版本变更执行">
    <div class="vce-heading"><div><h3>版本变更执行</h3><span class="vce-caption">请求与处理进度</span></div>
      <button class="vce-icon" type="button" title="刷新版本变更请求" aria-label="刷新版本变更请求" :disabled="state.loading" @click="execution.load()"><RefreshCw :size="15" /></button>
    </div>
    <p v-if="state.error" class="vce-error" role="alert">{{ state.error }}</p>
    <p v-if="quoteReady" class="vce-quote"><span>{{ quote.current_version_code }} <ArrowRightLeft :size="13" /> {{ quote.target_version_code }}</span>
      <strong>{{ money(quote.net_difference, quote.currency) }}</strong>
      <button type="button" class="vce-submit" :disabled="quotePending" @click="submit"><Check :size="15" />{{ quotePending ? '提交中' : '提交变更请求' }}</button>
    </p>
    <p v-else-if="canRequest && quote && !validated" class="vce-caption">请先校验报价后提交变更请求。</p>
    <p v-if="state.loading && !state.items.length" class="vce-caption">正在加载请求…</p>
    <p v-else-if="!state.items.length" class="vce-caption">暂无版本变更请求</p>
    <article v-for="item in state.items" :key="item.id" class="vce-request">
      <div class="vce-request-head"><strong>{{ item.current_version_code || '-' }} → {{ item.target_version_code || '-' }}</strong>
        <span :class="['vce-status', statusClass(item.status)]">{{ statusNames[item.status] }}</span></div>
      <dl class="vce-fields">
        <div><dt>净差额</dt><dd>{{ money(item.net_difference, item.currency) }}</dd></div>
        <div><dt>支付</dt><dd>{{ moneyStatus(item.payment_status) }}<template v-if="item.payment_amount != null"> · {{ money(item.payment_amount, item.currency) }}</template></dd></div>
        <div><dt>退款</dt><dd>{{ versionChangeRefundStatusLabel(item.refund_status) }}<template v-if="item.refund_amount != null"> · {{ money(item.refund_amount, item.currency) }}</template></dd></div>
        <div v-if="item.funding_order_no"><dt>支付订单</dt><dd>{{ item.funding_order_no }}</dd></div>
        <div><dt>尝试次数</dt><dd>{{ item.attempts }}</dd></div>
        <div><dt>创建时间</dt><dd>{{ date(item.created_at) }}</dd></div>
        <div><dt>更新时间</dt><dd>{{ date(item.updated_at || item.created_at) }}</dd></div>
        <div v-if="item.completed_at"><dt>完成时间</dt><dd>{{ date(item.completed_at) }}</dd></div>
      </dl>
      <p v-if="item.error" class="vce-error"><CircleAlert :size="14" />{{ item.error }}</p>
      <div class="vce-actions">
        <button v-if="permission(item).startPayment" type="button" :disabled="state.actionId === item.id" @click="execution.startPayment(item)"><CreditCard :size="14" />发起模拟支付</button>
        <button v-if="permission(item).confirmPayment" type="button" :disabled="state.actionId === item.id" @click="execution.confirm(item); reason = ''"><Check :size="14" />确认人工支付</button>
        <button v-if="permission(item).retry" type="button" :disabled="state.actionId === item.id" @click="execution.retry(item)"><RotateCw :size="14" />重试</button>
      </div>
    </article>
    <div v-if="state.confirmItem || submitConfirm" class="vce-confirm-backdrop" @click.self="state.confirmItem ? execution.cancelConfirm() : submitConfirm = false">
      <form v-if="state.confirmItem" class="vce-confirm" role="dialog" aria-modal="true" aria-labelledby="vce-confirm-title" @submit.prevent="confirmAction(state.confirmItem)">
        <div class="vce-confirm-head"><h3 id="vce-confirm-title">{{ user.platform_role ? '确认人工支付' : '模拟确认支付' }}</h3><button type="button" class="vce-icon" aria-label="关闭确认" @click="execution.cancelConfirm()"><X :size="16" /></button></div>
        <label>确认说明<textarea v-model="reason" rows="3" maxlength="500" required /></label>
        <div class="vce-confirm-actions"><button type="button" @click="execution.cancelConfirm()">取消</button><button type="submit" :disabled="!reason.trim() || Boolean(state.actionId)"><Check :size="14" />{{ user.platform_role ? '确认支付' : '模拟确认支付' }}</button></div>
      </form>
      <section v-else class="vce-confirm" role="dialog" aria-modal="true" aria-labelledby="vce-submit-title">
        <div class="vce-confirm-head"><h3 id="vce-submit-title">提交版本变更请求</h3><button type="button" class="vce-icon" aria-label="关闭确认" @click="submitConfirm = false"><X :size="16" /></button></div>
        <p>{{ quote?.current_version_code }} → {{ quote?.target_version_code }}</p>
        <p class="vce-submit-total">净差额 <strong>{{ money(quote?.net_difference, quote?.currency) }}</strong> · {{ quote?.currency }}</p>
        <p class="vce-caption">提交后将按当前报价创建版本变更请求。</p>
        <div class="vce-confirm-actions"><button type="button" @click="submitConfirm = false">返回</button><button type="button" :disabled="quotePending" @click="confirmSubmit"><Check :size="14" />确认提交</button></div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.vce-panel { border-top: 1px solid #e5e6eb; padding: 14px 0; color: #252a31; }
.vce-heading, .vce-request-head, .vce-confirm-head { display:flex; align-items:center; justify-content:space-between; gap:10px; }
.vce-heading h3, .vce-confirm h3 { margin:0; font-size:14px; }
.vce-caption { display:block; margin-top:4px; color:#78818c; font-size:12px; }
.vce-icon { display:inline-grid; place-items:center; width:30px; height:30px; border:1px solid #d9dde2; border-radius:6px; background:#fff; color:#505963; cursor:pointer; }
.vce-icon:disabled, button:disabled { opacity:.55; cursor:not-allowed; }
.vce-quote { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin:12px 0; padding:10px; background:#f4f8f6; border-left:3px solid #25815e; font-size:13px; }
.vce-quote span { display:flex; align-items:center; gap:5px; flex:1 1 auto; }
.vce-submit, .vce-actions button, .vce-confirm-actions button { display:inline-flex; align-items:center; justify-content:center; gap:6px; min-height:34px; padding:6px 10px; border:1px solid #cbd3ce; border-radius:6px; background:#fff; color:#245f49; font:inherit; cursor:pointer; }
.vce-submit { background:#237953; color:white; border-color:#237953; }
.vce-request { margin-top:10px; padding:10px 0; border-top:1px solid #edf0f2; }
.vce-request-head strong { font-size:13px; overflow-wrap:anywhere; }
.vce-status { flex:none; padding:3px 7px; border-radius:4px; font-size:11px; background:#fff2d9; color:#865900; }
.vce-status.done { background:#e5f4ec; color:#246543; }.vce-status.error { background:#fff0ed; color:#a23a2b; }
.vce-fields { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:9px 12px; margin:10px 0; }
.vce-fields div { min-width:0; }.vce-fields dt { color:#78818c; font-size:11px; }.vce-fields dd { margin:3px 0 0; font-size:12px; overflow-wrap:anywhere; }
.vce-error { display:flex; align-items:flex-start; gap:5px; color:#a23a2b; font-size:12px; }.vce-actions { display:flex; flex-wrap:wrap; gap:7px; }
.vce-actions button { min-height:30px; font-size:12px; }
.vce-confirm-backdrop { position:fixed; inset:0; z-index:1200; display:grid; place-items:center; padding:16px; background:#17212b66; }
.vce-confirm { width:min(420px,100%); padding:18px; border-radius:8px; background:#fff; box-shadow:0 18px 60px #10182033; }
.vce-confirm label { display:grid; gap:7px; margin-top:14px; font-size:13px; }.vce-confirm textarea { width:100%; resize:vertical; border:1px solid #ccd2d8; border-radius:5px; padding:8px; font:inherit; }
.vce-confirm-actions { display:flex; justify-content:flex-end; gap:8px; margin-top:14px; }
.vce-submit-total { display:flex; justify-content:space-between; padding:10px 0; border-bottom:1px solid #e5e6eb; }
@media(max-width:540px) { .vce-fields { grid-template-columns:repeat(2,minmax(0,1fr)); } .vce-quote { align-items:flex-start; } }
</style>
