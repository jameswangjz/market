<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { Eye, RefreshCw } from "lucide-vue-next";
import PaginationControls from "./PaginationControls.vue";
import SlaSourcesDrawer from "./SlaSourcesDrawer.vue";
import SlaTicketEvidence from "./SlaTicketEvidence.vue";
import { metricRows, createSlaReadSession } from "./slaEvidence.js";
import { slaMetric, slaResultState, slaResultReason } from "./slaResultDisplay.js";
const props = defineProps({ results: { type: Array, default: () => [] }, assignments: { type: Array, default: () => [] }, api: Object, params: Object });
const state = reactive({ data: null, busy: false, error: "" });
const session = props.api ? createSlaReadSession(props.api, state) : null;
const page = ref(1), size = ref(20), activeTab = ref("results"), selectedSource = ref(null);
const displayResults = computed(() => props.api ? state.data?.items || [] : props.results);
function load() { return session?.read("/sla/results", { page: page.value, page_size: size.value }); }
function go(value) { page.value = value; selectedSource.value = null; load(); }
watch(() => JSON.stringify(props.params), () => { selectedSource.value = null; go(1); }, { immediate: true });
onBeforeUnmount(() => session?.dispose());
const date = value => value ? new Date(value).toLocaleString() : "—";
</script>

<template>
  <section class="sla-evidence">
    <h3>冻结承诺与指标证据</h3>
    <p class="muted">企业等级调整仅影响新工单，已有工单与订单仍按各自冻结承诺考核。</p>
    <div v-if="api" class="tabs" role="tablist" aria-label="SLA证据视图"><button role="tab" :aria-selected="activeTab === 'results'" @click="activeTab = 'results'">周期来源</button><button role="tab" :aria-selected="activeTab === 'tickets'" @click="activeTab = 'tickets'">工单责任</button></div>
    <SlaTicketEvidence v-if="api && activeTab === 'tickets'" :api="api" :params="params" />
    <div v-else>
    <button v-if="api" class="icon-btn" title="刷新周期证据列表" aria-label="刷新周期证据列表" :disabled="state.busy" @click="load"><RefreshCw :size="16" /></button>
    <p v-if="state.busy" role="status">读取中</p><p v-if="state.error" role="alert" class="error-text">{{ state.error }}</p>
    <details v-for="result in displayResults" :key="result.id">
      <summary>{{ result.order_no || result.profile_name || result.id }} · {{ slaResultState(result.status).label }}</summary>
      <dl class="evidence-meta">
        <div><dt>订单</dt><dd>{{ result.order_no || result.order_id || '—' }}</dd></div>
        <div><dt>冻结等级</dt><dd>{{ result.frozen_tier || '—' }}</dd></div>
        <div><dt>冻结规则版本</dt><dd>{{ result.rule_version || '—' }}</dd></div>
        <div><dt>来源版本</dt><dd>{{ result.source_summary?.source_version || '—' }}</dd></div>
        <div><dt>证据摘要</dt><dd>{{ result.source_summary?.evidence_digest || '—' }}</dd></div>
        <div><dt>请求统计截止</dt><dd>{{ date(result.source_summary?.request_source_cutoff) }}</dd></div>
      </dl>
      <div class="table-wrap"><table class="data-table compact">
        <thead><tr><th>指标</th><th>实测</th><th>冻结目标</th><th>样本</th><th>覆盖率 / 耗时完整率</th><th>结论与异常</th><th>来源</th></tr></thead>
        <tbody><tr v-for="metric in metricRows(result)" :key="metric.key">
          <td>{{ metric.label }}</td><td>{{ slaMetric(metric.value, metric.unit) }}</td>
          <td>{{ slaMetric(metric.target, metric.unit) }}</td><td>{{ slaMetric(metric.sample_count) }}</td>
          <td>{{ slaMetric(metric.coverage_percent, '%') }} / {{ slaMetric(metric.completeness_percent, '%') }}</td>
          <td><span :class="['status-pill', metric.sourceMissing ? 'status-todo' : slaResultState(metric.status).className]">{{ metric.sourceMissing ? '无数据源' : slaResultState(metric.status).label }}</span><small>{{ slaResultReason(metric.sourceMissing ? { status: 'insufficient_data', reason: 'source_missing' } : metric) }}</small></td>
          <td><button v-if="api && !result.legacy && result.version" class="icon-btn" :title="`查看${metric.label}冻结来源`" :aria-label="`查看${metric.label}冻结来源`" @click="selectedSource = { result, metric: metric.key }"><Eye :size="16" /></button><span v-else>历史来源未提供</span></td>
        </tr><tr v-if="!metricRows(result).length"><td colspan="7">缺少逐指标证据，不能推断全部达标。</td></tr></tbody>
      </table></div>
      <p class="muted">探测采样可用性不等于精确请求可用率。历史未保存的逐条证据不以当前数据补造。</p>
    </details>
    <p v-if="!displayResults.length && !state.busy" class="muted">暂无周期证据。</p>
    <PaginationControls v-if="api" :page="page" :page-size="size" :total="state.data?.total || 0" :busy="state.busy" label="周期SLA证据分页" @page="go" @size="value => { size = value; go(1); }" />
    </div>
    <h3>企业等级生效记录</h3>
    <div class="table-wrap"><table class="data-table compact"><thead><tr><th>企业</th><th>等级</th><th>生效时间</th><th>到期时间</th><th>操作人 / 来源</th></tr></thead><tbody>
      <tr v-for="item in assignments" :key="item.id"><td>{{ item.enterprise_name || item.enterprise_id }}</td><td>{{ item.service_level?.name || '—' }}</td><td>{{ date(item.effective_at) }}</td><td>{{ date(item.expires_at) }}</td><td>{{ item.created_by || '—' }} / {{ item.source || '—' }}</td></tr>
      <tr v-if="!assignments.length"><td colspan="5">暂无等级生效记录。</td></tr>
    </tbody></table></div>
    <SlaSourcesDrawer v-if="selectedSource" :api="api" :result="selectedSource.result" :metric="selectedSource.metric" @close="selectedSource = null" />
  </section>
</template>

<style scoped>
.sla-evidence { margin: 20px 0; min-width: 0; }
details { border-bottom: 1px solid #dce2e8; padding: 12px 0; }
summary { cursor: pointer; overflow-wrap: anywhere; }
.evidence-meta { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
dt { color: #647078; font-size: 12px; }
dd { margin: 4px 0 0; overflow-wrap: anywhere; }
@media (max-width: 640px) { .evidence-meta { grid-template-columns: minmax(0, 1fr); } }
</style>
