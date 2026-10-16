<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { Plus, Search, RotateCcw } from "lucide-vue-next";
import ActionDialog from "./ActionDialog.vue";
import { createDialogController } from "./dialogController.js";
import RefundList from "./RefundList.vue";
import RefundDetail from "./RefundDetail.vue";
import RefundRequestForm from "./RefundRequestForm.vue";
import PaginationControls from "./PaginationControls.vue";
import { canViewRefunds, canRequestRefund, refundAllowedActions, refundExecutionActions, createRefundSession } from "./refunds.js";
const props = defineProps({ api: Object, user: Object, enterpriseId: String, order: Object, params: { type: Object, default: () => ({}) } });
const state = reactive({ items: [], total: 0, item: null, listBusy: false, detailBusy: false, busy: false, error: "", listError: "", message: "" });
const page = ref(1), size = ref(20), createOpen = ref(false);
const filterDefaults = () => ({ refund_id: "", order_no: "", product: "", provider: "", buyer: "", status: "", start: "", end: "" });
const filters = reactive(filterDefaults());
const appliedFilters = ref({});
const executionDialog = ref(null), executionReason = ref("");
const dialogs = createDialogController(executionDialog);
const session = createRefundSession(props.api, state, () => ({ user: props.user, enterpriseId: props.enterpriseId, order: props.order, params: { ...props.params, ...appliedFilters.value } }));
const visible = computed(() => canViewRefunds(props.user, props.enterpriseId));
const requestAllowed = computed(() => canRequestRefund(props.order, props.user, props.enterpriseId, props.params.business_view));
const actions = computed(() => refundAllowedActions(state.item, props.user));
watch(() => JSON.stringify([props.user?.id, props.user?.platform_role, props.enterpriseId, props.order?.id, props.params]), () => {
  dialogs.cancel(); executionReason.value = ""; session.reset(); page.value = 1; createOpen.value = false; if (visible.value) session.list(page.value, size.value);
}, { immediate: true });
onBeforeUnmount(() => { dialogs.dispose(); session.dispose(); });
watch(() => state.item?.id, (id, previous) => {
  if (id !== previous) { dialogs.cancel(); executionReason.value = ""; }
});
watch(() => [state.item?.id, state.item?.status, visible.value], () => session.observeExecution());
watch(() => state.page, value => { if (Number.isInteger(value) && value > 0) page.value = value; });
function load() { return session.list(page.value, size.value); }
function go(value) { page.value = value; load(); }
function search() {
  appliedFilters.value = Object.fromEntries(Object.entries(filters).filter(([, value]) => String(value).trim()).map(([key, value]) => [key, String(value).trim()]));
  session.closeDetail(); go(1);
}
function resetFilters() { Object.assign(filters, filterDefaults()); search(); }
async function request(values) { if (await session.send("request", values)) { createOpen.value = false; await load(); } }
async function review(values) { if (await session.send("review", values)) await load(); }
async function assess(values) { if (await session.send("manual", values)) await load(); }
async function execute(action) {
  if (state.busy || state.detailBusy || executionDialog.value || !refundExecutionActions(state.item, props.user).includes(action)) return;
  const id = state.item.id, version = state.item.version;
  const reason = await dialogs.prompt(`退款单：${state.item.refund_no || id}。提交后按服务端执行进展核对资金与资源结果。`, executionReason.value,
    { title: action === "retry" ? "重试退款执行" : "执行已批准退款", label: "执行理由", required: true, multiline: true,
      confirmText: action === "retry" ? "确认重试" : "确认执行", danger: true });
  if (reason == null || state.item?.id !== id || state.item.version !== version) return;
  executionReason.value = reason;
  if (await session.send(action, { reason, page: page.value, pageSize: size.value })) executionReason.value = "";
}
</script>
<template>
  <section v-if="visible" class="refund-center">
    <form class="refund-filters" aria-label="退款申请检索" @submit.prevent="search">
      <label>退款申请ID<input v-model="filters.refund_id" maxlength="180" /></label>
      <label>订单ID<input v-model="filters.order_no" maxlength="180" /></label>
      <label>产品<input v-model="filters.product" maxlength="180" /></label>
      <label>提供方<input v-model="filters.provider" maxlength="180" /></label>
      <label>购买方<input v-model="filters.buyer" maxlength="180" /></label>
      <label>状态<select v-model="filters.status"><option value="">全部</option><option v-for="(name, code) in {requested:'待审核',approved:'已批准',rejected:'已拒绝',executing:'执行中',completed:'已退款',failed:'执行失败',pending:'历史待处理'}" :key="code" :value="code">{{ name }}</option></select></label>
      <label>申请开始日期<input v-model="filters.start" type="date" /></label>
      <label>申请结束日期<input v-model="filters.end" type="date" /></label>
      <button class="primary-btn" type="submit" :disabled="state.listBusy"><Search :size="15" />检索</button>
      <button class="secondary-btn" type="button" :disabled="state.listBusy" @click="resetFilters"><RotateCcw :size="15" />重置</button>
    </form>
    <button v-if="requestAllowed && !createOpen" class="secondary-btn" :disabled="state.busy" @click="createOpen = true"><Plus :size="15" />申请退款</button>
    <RefundRequestForm v-if="createOpen" :busy="state.busy" :quote-busy="state.quoteBusy" :preview="state.quote" :enabled="requestAllowed" :order-no="order?.order_no" :error="state.error" @submit="request" @quote="session.quote" @amount-change="session.clearQuote" />
    <p v-if="state.message" role="status">{{ state.message }}</p>
    <RefundList :items="state.items" :busy="state.listBusy || state.busy" :error="state.listError" @refresh="load" @select="session.detail" />
    <PaginationControls :page="page" :page-size="size" :total="state.total" :busy="state.listBusy || state.busy" label="退款申请分页" @page="go" @size="value => { size = value; go(1); }" />
    <RefundDetail v-if="state.item || state.detailBusy" :key="state.item?.id || 'loading'" :item="state.item" :user="user" :busy="state.detailBusy || state.busy" :error="state.error" :allowed-actions="actions" :approval-available="actions.includes('approve')" @review="review" @assess="assess" @execute="execute" @refresh="session.refreshDetail" @close="session.closeDetail" />
    <p v-else-if="state.error && !createOpen" role="alert" class="error-text">{{ state.error }}</p>
  </section>
  <ActionDialog :dialog="executionDialog" @submit="dialogs.submit" @cancel="dialogs.cancel" />
</template>
<style scoped>.refund-center{padding:0;min-width:0}.refund-center>p{overflow-wrap:anywhere}.refund-filters{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;margin:12px 0 18px}.refund-filters.refund-filters label{display:flex;flex-direction:column;gap:5px;font-size:12px;line-height:18px;width:170px;max-width:100%;min-width:0;margin:0}.refund-filters.refund-filters input,.refund-filters.refund-filters select{box-sizing:border-box;width:100%;min-width:0;height:34px;padding:6px 8px;font-size:12px;line-height:20px;border:1px solid #d9dde3;border-radius:4px;background:#fff}@media(max-width:600px){.refund-filters.refund-filters label{width:calc(50% - 6px)}}</style>
