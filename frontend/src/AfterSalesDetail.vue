<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { Download, RefreshCw, Upload, Send } from "lucide-vue-next";
import AfterSalesModalShell from "./AfterSalesModalShell.vue";
import TicketSlaEvidence from "./TicketSlaEvidence.vue";
import { afterSalesActions, ticketStatus, ticketActions, canCollaborate, createAfterSalesSession, timelineActor, timelineFrom, evidenceAuthor } from "./afterSales.js";
const props = defineProps({ ticketId: String, api: Object, params: Object, user: Object, enterpriseId: String });
const emit = defineEmits(["close", "changed"]);
const state = reactive({ ticket: null, timeline: [], comments: [], attachments: [], loading: false, busy: false, error: "" });
let session;
let lifecycle = 0;
const reason = ref(""), comment = ref(""), action = ref(""), selectedAttachments = ref([]);
const fileInput = ref(null);
const slaExpanded = ref(false);
const allowed = computed(() => ticketActions(state.ticket, props.user));
const collaborate = computed(() => canCollaborate(state.ticket, props.user, props.enterpriseId));
const terminal = computed(() => ["completed", "withdrawn", "force_closed", "resolved", "closed"].includes(state.ticket?.status));
function selectableAttachment(item) { return collaborate.value && item.available && item.author_id === props.user.id && (!terminal.value || item.purpose === "audit_comment"); }
const date = value => value ? new Date(value).toLocaleString() : "—";
watch(() => JSON.stringify([props.ticketId, props.params, props.user?.id, props.enterpriseId]), () => {
  lifecycle++;
  session?.dispose();
  state.timeline = []; state.comments = []; state.attachments = []; state.busy = false;
  reason.value = ""; comment.value = ""; action.value = ""; selectedAttachments.value = [];
  session = createAfterSalesSession(props.api, state, { ...props.params });
  session.load(props.ticketId);
}, { immediate: true });
onBeforeUnmount(() => { lifecycle++; session?.dispose(); });
async function submitAction() {
  if (!reason.value.trim() || !allowed.value.includes(action.value)) return;
  if (await session.mutate("actions", { action: action.value, reason: reason.value.trim(), attachments: selectedAttachments.value })) {
    reason.value = ""; action.value = ""; selectedAttachments.value = []; emit("changed");
  }
}
async function submitComment() {
  if (!collaborate.value || !comment.value.trim()) return;
  if (await session.mutate("comments", { content: comment.value.trim(), attachments: selectedAttachments.value })) {
    comment.value = ""; selectedAttachments.value = []; emit("changed");
  }
}
async function upload(event) {
  const file = event.target.files?.[0];
  if (!file || !collaborate.value) return;
  if (file.size === 0 || file.size > 20 * 1024 * 1024) { state.error = "附件须为1字节至20MB"; return; }
  if (await session.mutate("attachments", { purpose: terminal.value ? "audit_comment" : "evidence", description: "" }, file)) event.target.value = "";
}
async function download(item) {
  if (!item.available || state.busy) return;
  state.busy = true; state.error = "";
  const current = lifecycle;
  try {
    const response = await props.api.get(`/after-sales/${encodeURIComponent(props.ticketId)}/attachments/${encodeURIComponent(item.id)}/download`, { params: props.params, responseType: "blob" });
    if (current !== lifecycle) return;
    const url = URL.createObjectURL(response.data), link = document.createElement("a");
    link.href = url; link.download = item.name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch { if (current === lifecycle) state.error = "附件下载失败或已无权限"; } finally { if (current === lifecycle) state.busy = false; }
}
</script>
<template>
  <AfterSalesModalShell :title="state.ticket?.ticket_no || '售后工单详情'" :busy="state.busy" :dirty="!!reason || !!comment || !!selectedAttachments.length" @close="emit('close')">
    <div class="after-sales-actions"><button class="icon-btn" title="刷新工单" aria-label="刷新工单" :disabled="state.loading || state.busy" @click="session.load(ticketId)"><RefreshCw :size="16" /></button><span v-if="state.loading" role="status">加载中</span></div>
    <p v-if="state.error" class="error-text" role="alert">{{ state.error }}</p>
    <template v-if="state.ticket">
      <dl class="after-sales-meta"><div><dt>状态</dt><dd>{{ ticketStatus(state.ticket.status) }}</dd></div><div><dt>订单</dt><dd>{{ state.ticket.order_no || state.ticket.order_id }}</dd></div><div><dt>类别</dt><dd>{{ state.ticket.type }}</dd></div><div><dt>版本 / 冻结等级</dt><dd>{{ state.ticket.version }} / {{ state.ticket.service_level_code || '—' }}</dd></div></dl>
      <p>{{ state.ticket.description }}</p>
      <details class="ticket-sla-expander" @toggle="slaExpanded = $event.target.open"><summary>工单 SLA 指标与来源（只读）</summary><TicketSlaEvidence v-if="slaExpanded" :key="`${ticketId}:${JSON.stringify(params)}`" :api="api" :ticket-id="ticketId" :params="params" /></details>
      <fieldset :disabled="state.busy || state.loading"><legend>工单处理</legend>
        <div class="after-sales-actions"><button v-for="name in allowed" :key="name" type="button" class="secondary-btn" :aria-pressed="action === name" @click="action = name">{{ afterSalesActions[name] }}</button></div>
        <p v-if="!allowed.length" class="muted">当前状态或身份无可用处理动作。</p>
        <form v-if="allowed.includes(action)" @submit.prevent="submitAction"><label>{{ afterSalesActions[action] }}理由（必填）<textarea v-model="reason" rows="3" required maxlength="10000" /></label><button class="primary-btn" type="submit" :disabled="!reason.trim()">确认{{ afterSalesActions[action] }}</button></form>
      </fieldset>
      <h3>附件</h3>
      <fieldset :disabled="state.busy || state.loading"><input ref="fileInput" type="file" hidden @change="upload" /><button v-if="collaborate" type="button" class="secondary-btn" @click="fileInput.click()"><Upload :size="15" />上传附件</button>
        <div v-for="item in state.attachments" :key="item.id" class="after-sales-record"><label v-if="selectableAttachment(item)"><input v-model="selectedAttachments" type="checkbox" :value="item.id" :disabled="selectedAttachments.length >= 10 && !selectedAttachments.includes(item.id)" />用于本次处理或评论</label><span>{{ item.name }} · {{ item.available ? '安全扫描通过' : '暂不可用' }}</span><small>上传人：{{ evidenceAuthor(item) }} · {{ item.author_role }}</small><button class="icon-btn" :disabled="!item.available" :title="`下载 ${item.name}`" :aria-label="`下载 ${item.name}`" @click="download(item)"><Download :size="16" /></button></div>
        <p v-if="!state.attachments.length" class="muted">暂无附件</p>
      </fieldset>
      <h3>沟通记录</h3><article v-for="item in state.comments" :key="item.id" class="after-sales-record"><small>{{ evidenceAuthor(item) }} · {{ item.author_role }} · {{ date(item.created_at) }}</small><p>{{ item.content }}</p><small v-if="item.attachments.length">附件：{{ item.attachments.map(id => state.attachments.find(file => file.id === id)?.name || id).join('、') }}</small></article><p v-if="!state.comments.length" class="muted">暂无评论</p>
      <form v-if="collaborate" @submit.prevent="submitComment"><fieldset :disabled="state.busy || state.loading"><label>{{ terminal ? '追加审计评论' : '新增评论' }}<textarea v-model="comment" rows="3" required maxlength="10000" /></label><button type="submit" class="secondary-btn" :disabled="!comment.trim()"><Send :size="15" />发送</button></fieldset></form>
      <h3>处理时间线</h3><article v-for="item in state.timeline" :key="item.id" class="after-sales-record"><strong>{{ afterSalesActions[item.action] || (item.action === 'create' ? '创建工单' : '历史操作') }}</strong><small>{{ date(item.occurred_at) }} · {{ timelineActor(item) }} · {{ item.actor_role }}</small><p>{{ item.reason }}</p><small>{{ timelineFrom(item) }} → {{ ticketStatus(item.after?.status) }}</small><small v-if="item.request_id">请求：{{ item.request_id }}</small></article><p v-if="!state.timeline.length" class="muted">暂无处理记录</p>
    </template>
  </AfterSalesModalShell>
</template>
