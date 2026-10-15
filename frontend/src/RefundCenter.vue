<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { Plus } from "lucide-vue-next";
import RefundList from "./RefundList.vue";
import RefundDetail from "./RefundDetail.vue";
import RefundRequestForm from "./RefundRequestForm.vue";
import PaginationControls from "./PaginationControls.vue";
import { canViewRefunds, canRequestRefund, refundAllowedActions, createRefundSession } from "./refunds.js";
const props = defineProps({ api: Object, user: Object, enterpriseId: String, order: Object, params: { type: Object, default: () => ({}) } });
const state = reactive({ items: [], total: 0, item: null, listBusy: false, detailBusy: false, busy: false, error: "", listError: "", message: "" });
const page = ref(1), size = ref(20), createOpen = ref(false);
const session = createRefundSession(props.api, state, () => ({ user: props.user, enterpriseId: props.enterpriseId, order: props.order, params: props.params }));
const visible = computed(() => canViewRefunds(props.user, props.enterpriseId));
const requestAllowed = computed(() => canRequestRefund(props.order, props.user, props.enterpriseId, props.params.business_view));
const actions = computed(() => refundAllowedActions(state.item, props.user));
watch(() => JSON.stringify([props.user?.id, props.enterpriseId, props.order?.id, props.params]), () => {
  session.reset(); page.value = 1; createOpen.value = false; if (visible.value) session.list(page.value, size.value);
}, { immediate: true });
onBeforeUnmount(session.dispose);
function load() { return session.list(page.value, size.value); }
function go(value) { page.value = value; load(); }
async function request(values) { if (await session.send("request", values)) { createOpen.value = false; await load(); } }
async function review(values) { if (await session.send("review", values)) await load(); }
async function assess(values) { if (await session.send("manual", values)) await load(); }
</script>
<template>
  <section v-if="visible" class="refund-center">
    <button v-if="requestAllowed && !createOpen" class="secondary-btn" :disabled="state.busy" @click="createOpen = true"><Plus :size="15" />申请退款</button>
    <RefundRequestForm v-if="createOpen" :busy="state.busy" :quote-busy="state.quoteBusy" :preview="state.quote" :enabled="requestAllowed" :order-no="order?.order_no" :error="state.error" @submit="request" @quote="session.quote" @amount-change="session.clearQuote" />
    <p v-if="state.message" role="status">{{ state.message }}</p>
    <RefundList :items="state.items" :busy="state.listBusy || state.busy" :error="state.listError" @refresh="load" @select="session.detail" />
    <PaginationControls :page="page" :page-size="size" :total="state.total" :busy="state.listBusy || state.busy" label="退款申请分页" @page="go" @size="value => { size = value; go(1); }" />
    <RefundDetail v-if="state.item || state.detailBusy" :key="state.item?.id || 'loading'" :item="state.item" :user="user" :busy="state.detailBusy || state.busy" :error="state.error" :allowed-actions="actions" :approval-available="actions.includes('approve')" @review="review" @assess="assess" />
    <p v-else-if="state.error && !createOpen" role="alert" class="error-text">{{ state.error }}</p>
  </section>
</template>
<style scoped>.refund-center{padding:16px 0;border-top:1px solid #e5e6eb;min-width:0}.refund-center>p{overflow-wrap:anywhere}</style>
