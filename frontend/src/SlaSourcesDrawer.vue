<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { X, RefreshCw } from "lucide-vue-next";
import PaginationControls from "./PaginationControls.vue";
import { createSlaReadSession, slaMetricNames, sourcePageTotal, sourceFactTotal, slaMetricUnit, slaTier, slaEvidenceReason, slaSourceReason, slaParty } from "./slaEvidence.js";
import { slaMetric, slaResultState, slaResultReason } from "./slaResultDisplay.js";
const props = defineProps({ api: Object, result: Object, metric: String });
const emit = defineEmits(["close"]);
const state = reactive({ data: null, busy: false, error: "" });
const page = ref(1), size = ref(20);
const session = createSlaReadSession(props.api, state);
const total = computed(() => sourcePageTotal(state.data));
const date = value => value ? new Date(value).toLocaleString() : "—";
const status = value => ({ healthy: "健康", failed: "失败", claimed: "采集中", unknown: "未知", verified: "已确认", pending: "待完成", overdue: "已超时" })[value] || "未记录";
function load() {
  const id = props.result.id, version = props.result.version, metric = props.metric;
  return session.read(`/sla/results/${encodeURIComponent(id)}/sources`, { metric, version, page: page.value, page_size: size.value }, data => data.assessment_id === id && data.version === version && data.metric === metric);
}
watch(() => `${props.result.id}:${props.result.version}:${props.metric}`, () => { page.value = 1; load(); }, { immediate: true });
onBeforeUnmount(session.dispose);
function go(value) { page.value = value; load(); }
function resize(value) { size.value = value; go(1); }
</script>
<template>
  <Teleport to="body"><div class="modal-scrim sla-source-scrim"><section class="modal-card sla-source-modal" role="dialog" aria-modal="true" aria-labelledby="sla-source-title">
    <div class="drawer-head"><h2 id="sla-source-title">{{ slaMetricNames[metric] || metric }} · 来源证据</h2><button class="icon-btn" title="关闭来源证据" aria-label="关闭来源证据" @click="emit('close')"><X :size="18" /></button></div>
    <p>{{ result.order_no || result.order_id }} · 评估版本 {{ result.version }}</p><button class="icon-btn" title="重读已冻结证据" aria-label="重读已冻结证据" :disabled="state.busy" @click="load"><RefreshCw :size="16" /></button>
    <p v-if="state.busy" role="status">读取中</p><p v-if="state.error" role="alert" class="error-text">{{ state.error }}</p>
    <template v-if="state.data">
      <p v-if="state.data.evidence_status !== 'recorded'" class="muted">来源记录不足或未保存；不能据此证明达标。{{ slaResultReason({ status: 'historical_missing', reason: state.data.reason }) }}</p>
      <p v-if="state.data.metric_result">该版本结论：<span :class="['status-pill', slaResultState(state.data.metric_result.status).className]">{{ slaResultState(state.data.metric_result.status).label }}</span> · 实测 {{ slaMetric(state.data.metric_result.value, slaMetricUnit(metric, state.data.metric_result)) }} · 样本 {{ slaMetric(state.data.metric_result.sample_count) }}</p>
      <dl v-if="state.data.metric_result" class="source-context">
        <div><dt>冻结目标</dt><dd>{{ slaMetric(state.data.metric_result.target, slaMetricUnit(metric, state.data.metric_result)) }}</dd></div><div><dt>冻结预警</dt><dd>{{ slaMetric(state.data.metric_result.warning, slaMetricUnit(metric, state.data.metric_result)) }}</dd></div>
        <div><dt>冻结等级</dt><dd :title="state.data.metric_result.frozen_tier">{{ slaTier(state.data.metric_result.frozen_tier) }}</dd></div><div><dt>冻结规则版本</dt><dd>{{ state.data.metric_result.rule_version || '—' }}</dd></div><div><dt>合同规则版本</dt><dd>{{ state.data.metric_result.contract_rule_version || '—' }}</dd></div>
      </dl>
      <dl class="source-context"><div><dt>评估截止</dt><dd>{{ date(state.data.as_of) }}</dd></div><div><dt>请求来源截止</dt><dd>{{ date(state.data.request_source_cutoff) }}</dd></div><div><dt>输入摘要</dt><dd>{{ state.data.input_hash || '—' }}</dd></div></dl>
      <p>全访问事实 {{ slaMetric(sourceFactTotal(state.data)) }} 条 · 可查询 {{ state.data.total }} 条 · 保留 {{ state.data.retained_count }} 条<span v-if="state.data.truncated"> · 证据已截断，以下并非全部事实</span></p>
      <div class="table-wrap"><table class="data-table compact"><thead><tr><th>事实 / 订单</th><th>时间</th><th>状态 / HTTP</th><th>耗时</th><th>统计归属</th><th>责任 / 来源策略</th><th>原因</th></tr></thead><tbody>
        <tr v-for="(item, index) in state.data.items" :key="item.fact_id || item.order_id || index"><td>{{ item.fact_id || '—' }}<small>{{ item.order_no || item.order_id || '—' }}</small></td><td>{{ date(item.occurred_at || item.deadline_at) }}</td><td>{{ status(item.status) }} / {{ slaMetric(item.status_code) }}</td><td>{{ slaMetric(item.duration_ms, ' ms') }}</td><td>{{ item.included === true ? '纳入' : item.included === false ? '排除' : '未记录' }}<small>{{ item.attributable_failure === true ? '已认定归责失败' : item.attributable_failure === false ? '非归责失败' : '归责未明确' }}</small></td><td>{{ slaParty(item.responsibility) }}<small>{{ item.policy_version || '策略未记录' }}</small></td><td :title="item.reason">{{ slaEvidenceReason(item.reason) }}<small v-if="item.source_reason" :title="item.source_reason">实际：{{ slaSourceReason(item.source_reason) }}</small></td></tr>
        <tr v-if="!state.data.items.length"><td colspan="7">此版本未提供逐条来源，不以当前事实补造历史。</td></tr>
      </tbody></table></div>
      <PaginationControls :page="page" :page-size="size" :total="total" :busy="state.busy" label="冻结来源分页" @page="go" @size="resize" />
    </template>
  </section></div></Teleport>
</template>
<style scoped>
.sla-source-scrim{z-index:96}.modal-card.sla-source-modal{width:min(1000px,calc(100% - 24px));max-height:calc(100dvh - 24px);overflow:auto;border-radius:8px;min-width:0}.source-context{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.source-context dd{margin:4px 0;overflow-wrap:anywhere}.sla-source-modal td,.sla-source-modal p{overflow-wrap:anywhere}@media(max-width:640px){.source-context{grid-template-columns:minmax(0,1fr)}}
</style>
