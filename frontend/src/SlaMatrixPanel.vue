<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue';
import { Copy, History, RefreshCw, Save, X } from 'lucide-vue-next';
import { builtInLevels, sortedMatrices, metricNames, slaTerms, matrixPayload, customLevelPayload, matrixMetadata, metadataFields } from './slaManagement.js';
const props = defineProps({ api: Object, user: Object, levels: { type: Array, default: () => [] } });
const emit = defineEmits(['changed']);
const allowed = computed(() => ['super_admin', 'platform_operator'].includes(props.user?.platform_role));
const matrices = ref([]), selected = ref(''), busy = ref(false), saving = ref(false), error = ref(''), notice = ref(''), versions = ref(null), versionsBusy = ref(false);
const draft = reactive({});
const form = reactive({ code: '', name: '', description: '', base_code: 'standard' });
const matrix = computed(() => matrices.value.find(item => item.service_level_id === selected.value));
const supportRows = computed(() => (matrix.value?.display_metadata?.rows || matrix.value?.readonly_metadata?.rows || []).filter(row => !row.metric_key));
let generation = 0, historyGeneration = 0, disposed = false;
function select(id) {
  selected.value = id; versions.value = null; historyGeneration++; versionsBusy.value = false; error.value = ''; notice.value = '';
  for (const key of Object.keys(draft)) delete draft[key];
  for (const [key, rule] of Object.entries(matrix.value?.metrics || {})) draft[key] = { target: rule.target, warning: rule.warning };
}
async function load() {
  const request = ++generation; busy.value = true; error.value = '';
  try {
    const { data } = await props.api.get('/sla/matrices');
    if (disposed || request !== generation) return;
    matrices.value = sortedMatrices(data.items || []);
    select(matrices.value.some(item => item.service_level_id === selected.value) ? selected.value : matrices.value[0]?.service_level_id || '');
  } catch (failure) { if (!disposed && request === generation) error.value = failure.response?.data?.detail || '指标矩阵读取失败'; }
  finally { if (!disposed && request === generation) busy.value = false; }
}
async function save() {
  if (!allowed.value || saving.value || busy.value || !matrix.value) return;
  error.value = ''; notice.value = '';
  let payload;
  try { payload = matrixPayload(matrix.value, draft); } catch (failure) { error.value = failure.message; return; }
  const id = selected.value, request = generation;
  saving.value = true;
  try {
    const { data } = await props.api.patch(`/sla/service-levels/${encodeURIComponent(id)}/matrix`, payload);
    if (disposed || request !== generation) return;
    matrices.value = matrices.value.map(item => item.service_level_id === id ? data.item : item);
    select(id); notice.value = '指标已保存，仅影响新冻结承诺'; emit('changed');
  } catch (failure) {
    if (!disposed && request === generation) error.value = failure.response?.status === 409 ? '矩阵已被更新，请刷新后核对并重新修改。当前输入已保留。' : failure.response?.data?.detail || '矩阵保存失败，输入已保留';
  } finally { if (!disposed) saving.value = false; }
}
async function history() {
  if (!matrix.value || versionsBusy.value) return;
  const id = selected.value, request = ++historyGeneration;
  versionsBusy.value = true; error.value = '';
  try {
    const { data } = await props.api.get(`/sla/service-levels/${encodeURIComponent(id)}/matrix/versions`);
    if (!disposed && request === historyGeneration && id === selected.value) versions.value = data.items || [];
  } catch (failure) { if (!disposed && request === historyGeneration) error.value = failure.response?.data?.detail || '版本记录读取失败'; }
  finally { if (!disposed && request === historyGeneration) versionsBusy.value = false; }
}
async function create() {
  if (!allowed.value || saving.value || busy.value) return;
  let payload;
  try { payload = customLevelPayload(form); } catch (failure) { error.value = failure.message; return; }
  saving.value = true; error.value = '';
  try {
    const { data } = await props.api.post('/sla/service-levels', payload);
    if (disposed) return;
    selected.value = data.item.id; await load();
    Object.assign(form, { code: '', name: '', description: '', base_code: 'standard' });
    notice.value = '已复制完整指标矩阵和支持权益'; emit('changed');
  } catch (failure) { if (!disposed) error.value = failure.response?.data?.detail || '服务级别创建失败，输入已保留'; }
  finally { if (!disposed) saving.value = false; }
}
watch(() => props.user?.id, load, { immediate: true });
onBeforeUnmount(() => { disposed = true; generation++; historyGeneration++; });
const term = value => slaTerms[value] || value || '-';
const level = computed(() => props.levels.find(item => item.id === selected.value));
</script>
<template>
  <section class="sla-matrix-panel">
    <div class="sla-toolbar"><h3>服务级别支持权益</h3><button class="icon-btn" title="刷新矩阵" aria-label="刷新矩阵" :disabled="busy || saving" @click="load"><RefreshCw :size="16" /></button></div>
    <div class="sla-level-selector" role="group" aria-label="服务级别"><button v-for="item in matrices" :key="item.service_level_id" type="button" :aria-pressed="selected === item.service_level_id" :class="['secondary-btn', { selected: selected === item.service_level_id }]" :disabled="saving || busy" @click="select(item.service_level_id)">{{ item.name }}</button></div>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p><p v-if="busy" role="status">读取中</p>
    <template v-if="matrix">
      <div class="sla-toolbar"><div><strong>{{ matrix.name }}</strong><small class="muted"> · 版本 {{ matrix.version }} · {{ matrix.status === 'active' ? '启用' : '停用' }}</small></div><button class="secondary-btn" :disabled="versionsBusy || saving" @click="history"><History :size="15" />版本记录</button></div>
      <dl class="sla-readonly-grid"><div><dt>支持日历</dt><dd>每周 {{ matrix.calendar?.support_days_per_week ?? '-' }} 天 / 每天 {{ matrix.calendar?.support_hours_per_day ?? '-' }} 小时</dd></div><div><dt>时区</dt><dd>{{ matrix.calendar?.timezone || '-' }}</dd></div><div><dt>支持时段</dt><dd>{{ matrix.calendar?.windows?.map(window => window.join(' - ')).join('、') || '-' }}</dd></div><div><dt>公共节假日</dt><dd>{{ matrix.calendar?.exclude_public_holidays ? '排除' : '不排除' }} · {{ matrix.calendar?.holiday_calendar_status === 'unconfigured' ? '日历未配置' : matrix.calendar?.holiday_calendar_version || '-' }}</dd></div></dl>
      <dl v-if="level" class="sla-readonly-grid"><div v-for="(label, key) in { online_docs: '在线文档', knowledge_base: '知识库', standard_api: '标准 API', online_customer_service: '在线客服', dedicated_manager: '专属经理', technical_support: '技术支持', quarterly_report: '季度报告', annual_optimization: '年度优化' }" :key="key"><dt>{{ label }}</dt><dd>{{ level[key] ? '包含' : '不包含' }}</dd></div></dl>
      <section v-for="row in supportRows" :key="row['指标编号']" class="sla-support-row"><h4>{{ row['指标名称'] }} · {{ row['填写目标值'] }} {{ row['单位'] }}（只读）</h4><dl class="sla-readonly-grid"><div v-for="[key, value] in metadataFields(row)" :key="key"><dt>{{ key }}</dt><dd>{{ value || '-' }}</dd></div></dl></section>
      <form @submit.prevent="save"><div class="table-wrap"><table class="data-table compact sla-matrix-table"><thead><tr><th>指标</th><th>目标</th><th>预警</th><th>单位</th><th>方向</th><th>责任方</th><th>计时</th><th>暂停状态</th></tr></thead><tbody><tr v-for="(rule, key) in matrix.metrics" :key="key"><th scope="row"><strong>{{ matrixMetadata(matrix, key)['指标名称'] || metricNames[key] || key }}</strong><details class="sla-metric-metadata"><summary>只读参数</summary><dl><div v-for="[field, value] in metadataFields(matrixMetadata(matrix, key))" :key="field"><dt>{{ field }}</dt><dd>{{ value || '-' }}</dd></div><div v-if="!Object.keys(matrixMetadata(matrix, key)).length"><dd>未提供附加参数</dd></div></dl></details></th><td><input v-if="allowed" v-model="draft[key].target" type="number" :step="rule.unit === 'minutes' ? 1 : 'any'" :min="rule.unit === 'percent' ? 0 : rule.unit === 'minutes' ? 1 : undefined" :max="rule.unit === 'percent' ? 100 : undefined" :aria-label="`${metricNames[key] || key}目标`" :disabled="saving || busy" required /><span v-else>{{ rule.target }}</span></td><td><input v-if="allowed" v-model="draft[key].warning" type="number" step="any" :min="rule.unit === 'percent' ? 0 : undefined" :max="rule.unit === 'percent' ? 100 : undefined" :aria-label="`${metricNames[key] || key}预警`" :disabled="saving || busy" required /><span v-else>{{ rule.warning }}</span></td><td>{{ term(rule.unit) }}</td><td>{{ term(rule.direction) }}</td><td>{{ term(rule.responsibility) }}</td><td>{{ term(rule.clock) }}</td><td>{{ rule.pause_states?.map(term).join('、') || '不暂停' }}</td></tr></tbody></table></div><div class="sla-toolbar"><span class="muted">修改仅对新冻结承诺生效，已有承诺保持原版本。</span><button v-if="allowed" class="primary-btn" :disabled="saving || busy"><Save :size="15" />{{ saving ? '保存中' : '保存指标' }}</button></div></form>
      <section v-if="versions !== null" class="sla-history"><div class="sla-toolbar"><h4>矩阵版本记录</h4><button class="icon-btn" title="关闭版本记录" aria-label="关闭版本记录" @click="versions = null"><X :size="16" /></button></div><details v-for="version in versions" :key="version.id"><summary>版本 {{ version.version }} · {{ version.created_by }} · {{ version.created_at }}</summary><div class="table-wrap"><table class="data-table compact"><thead><tr><th>指标</th><th>目标</th><th>预警</th><th>单位</th></tr></thead><tbody><tr v-for="(rule, key) in version.metrics" :key="key"><td>{{ metricNames[key] || key }}</td><td>{{ rule.target }}</td><td>{{ rule.warning }}</td><td>{{ term(rule.unit) }}</td></tr></tbody></table></div></details><p v-if="!versions.length">暂无版本记录</p></section>
    </template>
    <p v-else-if="!busy && !error" class="empty-state">暂无完整指标矩阵</p>
    <form v-if="allowed" class="sla-create-level" @submit.prevent="create"><h4>复制为自定义级别</h4><div class="form-grid"><label>复制来源<select v-model="form.base_code" :disabled="saving"><option v-for="code in builtInLevels" :key="code" :value="code">{{ { standard: '标准级', enterprise: '企业级', strategic: '战略级' }[code] }}</option></select></label><label>编码<input v-model="form.code" pattern="[a-z0-9_]{2,30}" minlength="2" maxlength="30" required :disabled="saving" /></label><label>名称<input v-model="form.name" minlength="2" maxlength="80" required :disabled="saving" /></label><label>说明<input v-model="form.description" maxlength="2000" :disabled="saving" /></label></div><button class="primary-btn" :disabled="saving || busy"><Copy :size="15" />复制完整矩阵</button></form>
  </section>
</template>
