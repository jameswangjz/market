<script setup>
import { onBeforeUnmount, reactive, ref, watch } from "vue";
import { RefreshCw, List } from "lucide-vue-next";
import PaginationControls from "./PaginationControls.vue";
import SlaTicketMetrics from "./SlaTicketMetrics.vue";
import { createSlaReadSession } from "./slaEvidence.js";
const props = defineProps({ api: Object, ticketId: String, params: { type: Object, default: () => ({}) } });
const clocks = reactive({ data: null, busy: false, error: "" }), events = reactive({ data: null, busy: false, error: "" });
const clockSession = createSlaReadSession(props.api, clocks), eventSession = createSlaReadSession(props.api, events);
const responsibility = ref(""), page = ref(1), size = ref(20), eventPage = ref(1), showEvents = ref(false);
const date = value => value ? new Date(value).toLocaleString() : "—";
function load() {
  return clockSession.read(`/after-sales/${encodeURIComponent(props.ticketId)}/sla/evidence`, { ...props.params, page: page.value, page_size: size.value, ...(responsibility.value ? { responsibility: responsibility.value } : {}) });
}
function loadEvents() {
  showEvents.value = true;
  return eventSession.read(`/after-sales/${encodeURIComponent(props.ticketId)}/timeline`, { ...props.params, page: eventPage.value, page_size: 20 });
}
watch(() => JSON.stringify([props.ticketId, props.params]), () => {
  clockSession.reset(); eventSession.reset(); page.value = 1; eventPage.value = 1; showEvents.value = false; load();
}, { immediate: true });
watch(responsibility, () => { page.value = 1; load(); });
onBeforeUnmount(() => { clockSession.dispose(); eventSession.dispose(); });
function go(value) { page.value = value; load(); }
</script>
<template>
  <section class="ticket-sla-evidence">
    <div class="sla-ticket-controls"><label>责任方<select v-model="responsibility" :disabled="clocks.busy"><option value="">全部责任方</option><option value="provider">提供方</option><option value="platform">平台</option><option value="buyer">买方</option></select></label><button class="icon-btn" title="刷新只读工单证据" aria-label="刷新只读工单证据" :disabled="clocks.busy" @click="load"><RefreshCw :size="16" /></button></div>
    <p v-if="clocks.busy" role="status">读取中</p><p v-if="clocks.error" role="alert" class="error-text">{{ clocks.error }}</p>
    <template v-if="clocks.data">
      <p>{{ clocks.data.order_no }} · 工单版本 {{ clocks.data.ticket_version }} · {{ clocks.data.evidence_status === 'recorded' ? '事件已记录' : '历史证据不足' }}</p>
      <p v-if="clocks.data.stale" class="error-text">时钟尚未跟上工单最新版本；以下为历史测量，不代表当前状态。</p>
      <SlaTicketMetrics :items="clocks.data.items" />
      <PaginationControls :page="page" :page-size="size" :total="clocks.data.total" :busy="clocks.busy" label="工单责任指标分页" @page="go" @size="value => { size = value; go(1); }" />
      <h3>买方配合与等待区间</h3><p class="muted">以下仅为观察区间，不是买方达标或违约评估。</p><p v-if="clocks.data.buyer_cooperation.status === 'unknown'">历史事件不完整，买方配合时间未知。</p>
      <ol><li v-for="item in clocks.data.buyer_cooperation.items" :key="item.start_event_id">{{ date(item.started_at) }} 至 {{ date(item.ended_at) }} · {{ item.ended_at ? '区间结束' : '仍在等待' }}<small>事件：{{ item.start_event_id }} → {{ item.end_event_id || '尚未结束' }}</small></li></ol>
      <p v-if="clocks.data.buyer_cooperation.truncated">配合区间已截断。</p>
      <button class="secondary-btn" :disabled="events.busy" @click="loadEvents"><List :size="15" />查看工单事件来源</button>
    </template>
    <section v-if="showEvents"><h3>工单事件来源</h3><p v-if="events.busy" role="status">读取中</p><p v-if="events.error" role="alert" class="error-text">{{ events.error }}</p>
      <article v-for="event in events.data?.items || []" :key="event.id" class="sla-event"><strong>{{ event.actor_name || event.actor_id || '历史操作者未记录' }}</strong><small>{{ date(event.occurred_at) }} · {{ event.actor_role }}</small><p>{{ event.reason }}</p><small>事件：{{ event.id }} · 请求：{{ event.request_id || '未记录' }}</small></article>
      <PaginationControls v-if="events.data?.total !== undefined" :page="eventPage" :page-size="20" :total="events.data.total" :busy="events.busy" label="工单来源事件分页" @page="value => { eventPage = value; loadEvents(); }" />
    </section>
  </section>
</template>
<style scoped>
.sla-ticket-controls{display:flex;align-items:end;gap:12px;flex-wrap:wrap}.sla-ticket-controls label{min-width:0;max-width:100%}.sla-ticket-controls select{max-width:100%;min-width:120px}.ticket-sla-evidence{min-width:0}.ticket-sla-evidence small{display:block;overflow-wrap:anywhere}.sla-event{padding:12px 0;border-bottom:1px solid #dce2e8}
</style>
