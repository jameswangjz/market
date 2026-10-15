<script setup>
import { onBeforeUnmount, reactive, ref, watch } from "vue";
import TicketSlaEvidence from "./TicketSlaEvidence.vue";
import { createSlaReadSession } from "./slaEvidence.js";
const props = defineProps({ api: Object, params: { type: Object, default: () => ({}) } });
const list = reactive({ data: null, busy: false, error: "" });
const listSession = createSlaReadSession(props.api, list);
const selected = ref("");
async function loadList() { await listSession.read("/after-sales", props.params); }
watch(() => JSON.stringify(props.params), () => { selected.value = ""; loadList(); }, { immediate: true });
onBeforeUnmount(listSession.dispose);
</script>
<template>
  <section class="sla-ticket-evidence">
    <div class="sla-ticket-controls"><label>工单<select v-model="selected" :disabled="list.busy"><option value="">请选择工单</option><option v-for="item in list.data?.items || []" :key="item.id" :value="item.id">{{ item.ticket_no }} · {{ item.order_no || item.order_id }}</option></select></label></div>
    <p v-if="list.busy" role="status">读取中</p><p v-if="list.error" role="alert" class="error-text">{{ list.error }}</p>
    <p v-if="list.data && !list.data.items.length" class="muted">暂无可见工单。</p>
    <TicketSlaEvidence v-if="selected" :key="`${selected}:${JSON.stringify(params)}`" :api="api" :ticket-id="selected" :params="params" />
  </section>
</template>
<style scoped>
.sla-ticket-controls{display:flex;align-items:end;gap:12px;flex-wrap:wrap}.sla-ticket-controls label{min-width:0;max-width:100%}.sla-ticket-controls select{max-width:100%;min-width:120px}.sla-ticket-evidence{min-width:0}.sla-ticket-evidence small{display:block;overflow-wrap:anywhere}
</style>
