<script setup>
import { computed, ref } from 'vue';
import { CheckCircle2, CircleAlert, ExternalLink, RefreshCw, X } from 'lucide-vue-next';
import ConsoleTabs from './ConsoleTabs.vue';
import SlaMatrixPanel from './SlaMatrixPanel.vue';
import SlaBindingsPanel from './SlaBindingsPanel.vue';
import SlaEvidencePanel from './SlaEvidencePanel.vue';
import { slaTabs } from './consoleTabs.js';
import { slaResultState, slaMetric, slaResultReason } from './slaResultDisplay.js';
import { metricRows } from './slaEvidence.js';
import './slaManagement.css';
const props = defineProps({ api: Object, user: Object, overview: Object, params: Object });
const emit = defineEmits(['refresh']);
const tab = ref('overview'), selected = ref(null), evaluating = ref(false), error = ref('');
const evidenceResult = ref(null);
const overviewData = computed(() => props.overview || {});
const selectedMetrics = computed(() => metricRows(selected.value));
function openEvidence() { evidenceResult.value = selected.value; tab.value = 'evidence'; }
const allowed = computed(() => ['super_admin', 'platform_operator'].includes(props.user?.platform_role));
const summary = computed(() => {
  const counts = { met: 0, warning: 0, breached: 0 };
  for (const key of Object.keys(counts)) {
    const value = overviewData.value.summary?.[key];
    if (Number.isSafeInteger(value) && value >= 0) counts[key] = value;
  }
  return counts;
});
async function evaluate() {
  if (!allowed.value || evaluating.value) return;
  evaluating.value = true; error.value = '';
  try { await props.api.post('/sla/evaluate'); emit('refresh'); }
  catch (failure) { error.value = failure.response?.data?.detail || 'SLA 考核失败'; }
  finally { evaluating.value = false; }
}
const date = value => value ? new Date(value).toLocaleString() : '-';
</script>
<template>
  <div class="sla-management"><div class="page-heading"><div><div class="eyebrow">SERVICE ASSURANCE · SLA</div><h1>平台服务与 SLA 保障</h1></div><button class="secondary-btn" :disabled="evaluating" @click="emit('refresh')"><RefreshCw :size="15" />刷新</button></div>
    <ConsoleTabs v-model="tab" :tabs="slaTabs" label="SLA 管理" />
    <section v-if="tab === 'overview'" role="tabpanel" aria-label="总览"><div class="sla-toolbar"><h3>服务考核总览</h3><button v-if="allowed" class="primary-btn" :disabled="evaluating" @click="evaluate"><RefreshCw :size="15" />{{ evaluating ? '考核中' : '立即考核' }}</button></div><p v-if="error" class="error-text" role="alert">{{ error }}</p>
      <div class="metric-grid"><div v-for="metric in [{ key: 'met', label: '达标结果', icon: CheckCircle2, color: 'green' }, { key: 'warning', label: '预警结果', icon: CircleAlert, color: 'orange' }, { key: 'breached', label: '违约结果', icon: CircleAlert, color: 'red' }]" :key="metric.key" class="metric-card"><div :class="['metric-icon', metric.color]"><component :is="metric.icon" :size="17" /></div><span>{{ metric.label }}</span><strong>{{ summary[metric.key] }}</strong><small>当前周期考核结果</small></div></div>
      <section><h3>周期考核结果</h3><div class="table-wrap"><table class="data-table compact"><thead><tr><th>规则 / 时间</th><th>可用性</th><th>平均延迟</th><th>错误率</th><th>交付及时率</th><th>结论</th></tr></thead><tbody><tr v-for="item in overviewData.results || []" :key="item.id"><td><strong>{{ item.profile_name }}</strong><small>{{ date(item.calculated_at) }}</small></td><td>{{ slaMetric(item.availability, '%') }}</td><td>{{ slaMetric(item.avg_latency_ms, ' ms') }}</td><td>{{ slaMetric(item.error_rate, '%') }}</td><td>{{ slaMetric(item.delivery_compliance, '%') }}</td><td><button class="sla-result-button" @click="selected = item"><span :class="['status-pill', slaResultState(item.status).className]">{{ slaResultState(item.status).label }}</span><small>{{ slaResultReason(item) }}</small></button></td></tr><tr v-if="!overviewData.results?.length"><td colspan="6" class="empty-state">暂无考核结果</td></tr></tbody></table></div></section>
      <section v-if="selected" class="sla-detail">
        <div class="sla-toolbar"><h3>{{ slaResultState(selected.status).title }} · {{ selected.profile_name }}</h3><button class="icon-btn" title="关闭结果详情" aria-label="关闭结果详情" @click="selected = null"><X :size="16" /></button></div>
        <p>{{ date(selected.period_start) }} 至 {{ date(selected.period_end) }}</p><h4>异常内容</h4><p class="sla-result-reason">{{ slaResultReason(selected) }}</p>
        <h4>考核指标</h4><dl class="sla-readonly-grid"><div v-for="metric in [{ key: 'availability', label: '可用性', unit: '%' }, { key: 'avg_latency_ms', label: '平均延迟', unit: ' ms' }, { key: 'error_rate', label: '错误率', unit: '%' }, { key: 'delivery_compliance', label: '交付及时率', unit: '%' }, { key: 'sample_count', label: '请求样本', unit: '' }, { key: 'success_count', label: '成功请求', unit: '' }]" :key="metric.key"><dt>{{ metric.label }}</dt><dd>{{ slaMetric(selected[metric.key], metric.unit) }}</dd></div></dl>
        <div v-if="selectedMetrics.length" class="table-wrap"><table class="data-table compact"><thead><tr><th>指标</th><th>实测</th><th>冻结目标</th><th>样本</th><th>结论</th><th>异常原因</th></tr></thead><tbody><tr v-for="metric in selectedMetrics" :key="metric.key"><td>{{ metric.label }}</td><td>{{ slaMetric(metric.value, metric.unit) }}</td><td>{{ slaMetric(metric.target, metric.unit) }}</td><td>{{ slaMetric(metric.sample_count) }}</td><td>{{ slaResultState(metric.status).label }}</td><td>{{ slaResultReason(metric) }}</td></tr></tbody></table></div>
        <h4>处理建议</h4><p class="sla-result-advice">{{ slaResultState(selected.status).advice }}</p><button class="secondary-btn" @click="openEvidence"><ExternalLink :size="15" />查看冻结承诺与指标证据</button>
      </section>
    </section>
    <SlaMatrixPanel v-else-if="tab === 'levels'" :api="api" :user="user" :levels="overviewData.service_levels || []" @changed="emit('refresh')" />
    <SlaBindingsPanel v-else-if="tab === 'assignments'" :api="api" :user="user" :levels="overviewData.service_levels || []" @changed="emit('refresh')" />
    <SlaEvidencePanel v-else :api="api" :params="params" :results="overviewData.results || []" :focused-result="evidenceResult" @clear-focus="evidenceResult = null" />
  </div>
</template>
