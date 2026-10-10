<script setup>
import { computed, onUnmounted, ref, watch } from 'vue';
import axios from 'axios';
import { Upload, Download, RefreshCw, FileJson, Eye, X, CheckCircle2 } from 'lucide-vue-next';

const props = defineProps({ batches: { type: Array, default: () => [] }, canOperate: { type: Boolean, default: false } });
const api = axios.create({ baseURL: '/api' });
api.interceptors.request.use(config => {
  const token = localStorage.getItem('market_token');
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});
const batchId = ref('');
const ledgerType = ref('platform');
const sourceKind = ref('manual_import');
const sourceName = ref('');
const entries = ref([]);
const eventKey = ref('');
const importedId = ref('');
const reports = ref([]);
const selectedReport = ref(null);
const loading = ref(false);
const parsing = ref(false);
const busy = ref(false);
const downloading = ref(false);
const error = ref('');
const notice = ref('');
const resolution = ref({ reason: '', evidence_ref: '' });
const itemPage = ref(1);
const fileInput = ref(null);
let loadVersion = 0;
let fileVersion = 0;
let disposed = false;
const batch = computed(() => props.batches.find(item => item.id === batchId.value));
const visibleItems = computed(() => (selectedReport.value?.items || []).slice((itemPage.value - 1) * 50, itemPage.value * 50));
const ledgerName = value => ({ platform: '平台账', payment: '支付账', bank: '银行账', distribution: '分账账' }[value] || '未知账簿');
const sourceLabel = value => ({ simulation: '模拟', manual_import: '人工导入' }[value] || '未知来源');
const statusLabel = value => ({ missing: '缺失', unexpected: '非预期', difference: '金额差异', matched: '金额一致', resolved: '已记录差异处理' }[value] || '待比对');
const statusClass = value => value === 'matched' ? 'status-done' : value === 'resolved' ? 'status-review' : 'status-blocked';
const date = value => value ? new Date(value).toLocaleString('zh-CN') : '-';
function amount(value) {
  if (value === null || value === undefined || value === '') return '-';
  const text = String(value);
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(text);
  if (!match) return text;
  return `${match[1]}${match[2].replace(/\B(?=(\d{3})+(?!\d))/g, ',')}.${(match[3] || '').padEnd(2, '0')}`;
}
const failure = (e, fallback) => {
  if (typeof e.response?.data?.detail === 'string') return e.response.data.detail;
  return e.response || e.request ? fallback : e.message || fallback;
};
const path = id => `/settlement-batches/${encodeURIComponent(id)}`;
function resetImport() {
  ++fileVersion;
  sourceName.value = ''; entries.value = []; eventKey.value = ''; importedId.value = '';
  if (fileInput.value) fileInput.value.value = '';
}
function createEventKey() {
  if (typeof globalThis.crypto?.randomUUID === 'function') return globalThis.crypto.randomUUID();
  if (typeof globalThis.crypto?.getRandomValues !== 'function') throw new Error('浏览器不支持安全随机数生成');
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0'));
  return [hex.slice(0, 4), hex.slice(4, 6), hex.slice(6, 8), hex.slice(8, 10), hex.slice(10)].map(part => part.join('')).join('-');
}
function renewKey() {
  importedId.value = '';
  try { eventKey.value = entries.value.length ? createEventKey() : ''; }
  catch (e) { eventKey.value = ''; error.value = e.message; }
}
function parseEntries(payload) {
  const rows = Array.isArray(payload) ? payload : payload?.entries;
  if (!Array.isArray(rows) || !rows.length) throw new Error('账单 entries 必须是非空数组');
  if (rows.length > 5000) throw new Error('账单最多包含 5000 行');
  return rows.map((row, index) => {
    if (!row || typeof row !== 'object' || Array.isArray(row)) throw new Error(`第 ${index + 1} 行格式无效`);
    if (typeof row.entry_no !== 'string' || !row.entry_no.trim() || row.entry_no.length > 120) throw new Error(`第 ${index + 1} 行流水号无效`);
    if (typeof row.order_no !== 'string' || !row.order_no.trim() || row.order_no.length > 80) throw new Error(`第 ${index + 1} 行订单号无效`);
    const value = String(row.amount ?? '');
    const decimal = /^-?(\d+)(?:\.\d{1,2})?$/.exec(value);
    if (!['string', 'number'].includes(typeof row.amount) || !decimal || decimal[1].replace(/^0+/, '').length > 16 || (typeof row.amount === 'number' && Math.abs(row.amount) > Number.MAX_SAFE_INTEGER / 100)) throw new Error(`第 ${index + 1} 行金额无效，金额最多两位小数`);
    if (row.currency !== undefined && row.currency !== 'CNY') throw new Error(`第 ${index + 1} 行币种仅支持 CNY`);
    return { entry_no: row.entry_no.trim(), order_no: row.order_no.trim(), amount: value, currency: 'CNY' };
  });
}
async function chooseFile(event) {
  const file = event.target.files?.[0];
  resetImport(); error.value = ''; notice.value = '';
  if (!file || !props.canOperate || !batch.value) return;
  if (file.size > 5 * 1024 * 1024) { error.value = 'JSON 账单不能超过 5 MB'; return; }
  if (file.name.length > 180) { error.value = '文件名不能超过 180 个字符'; return; }
  const version = fileVersion;
  parsing.value = true;
  try {
    const text = await file.text();
    if (version !== fileVersion || disposed) return;
    let payload;
    try { payload = JSON.parse(text.replace(/^\uFEFF/, '')); }
    catch { throw new Error('JSON 账单格式无效'); }
    const rows = parseEntries(payload);
    const key = createEventKey();
    sourceName.value = file.name; entries.value = rows; eventKey.value = key;
  } catch (e) { if (version === fileVersion && !disposed) error.value = failure(e, '账单读取失败'); }
  finally { if (version === fileVersion) parsing.value = false; }
}
async function loadReports() {
  const version = ++loadVersion;
  const id = batchId.value;
  if (!id || !props.canOperate) { reports.value = []; loading.value = false; return; }
  loading.value = true;
  try {
    const result = await api.get(`${path(id)}/ledger-reports`);
    if (version !== loadVersion || disposed) return;
    reports.value = (result.data.items || []).slice().sort((a, b) => new Date(b.compared_at) - new Date(a.compared_at));
  } catch (e) { if (version === loadVersion && !disposed) error.value = failure(e, '报告加载失败'); }
  finally { if (version === loadVersion) loading.value = false; }
}
function openReport(report) {
  selectedReport.value = report; itemPage.value = 1;
  resolution.value = { reason: '', evidence_ref: '' };
  error.value = '';
}
async function importAndCompare() {
  if (!props.canOperate || !batch.value || busy.value || parsing.value || !entries.value.length || !eventKey.value) return;
  const id = batchId.value;
  const version = fileVersion;
  const body = { ledger_type: ledgerType.value, source_kind: sourceKind.value, source_name: sourceName.value, event_key: eventKey.value, entries: entries.value };
  busy.value = true; error.value = ''; notice.value = '';
  try {
    if (!importedId.value) {
      const imported = await api.post(`${path(id)}/ledger-import`, body);
      if (version !== fileVersion || disposed) return;
      importedId.value = imported.data.id;
    }
    const result = await api.post(`${path(id)}/ledger-imports/${encodeURIComponent(importedId.value)}/compare`);
    if (version !== fileVersion || disposed) return;
    const report = result.data;
    resetImport();
    openReport(report);
    notice.value = '账单比对报告已生成';
    await loadReports();
  } catch (e) { if (version === fileVersion && !disposed) error.value = failure(e, importedId.value ? '账单已导入，比对失败，请重试比对' : '账单导入失败'); }
  finally { busy.value = false; }
}
async function resolveReport() {
  const report = selectedReport.value;
  if (!props.canOperate || busy.value || report?.status !== 'difference') return;
  const reason = resolution.value.reason.trim(), evidence_ref = resolution.value.evidence_ref.trim();
  if (reason.length < 3 || reason.length > 1000 || evidence_ref.length < 3 || evidence_ref.length > 500) { error.value = '处理说明和依据至少填写 3 个字符'; return; }
  const id = batchId.value;
  busy.value = true; error.value = '';
  try {
    const result = await api.post(`${path(id)}/ledger-reports/${encodeURIComponent(report.id)}/resolve`, { reason, evidence_ref });
    if (disposed || id !== batchId.value || !props.canOperate) return;
    selectedReport.value = { ...report, status: result.data.status, resolution: reason, evidence_ref };
    notice.value = '差异处理已记录';
    await loadReports();
  } catch (e) { if (!disposed && id === batchId.value) error.value = failure(e, '差异处理失败'); }
  finally { busy.value = false; }
}
function downloadJSON(data, name) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' }));
  const link = document.createElement('a'); link.href = url; link.download = name;
  document.body.appendChild(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function downloadTemplate() {
  downloadJSON({ ledger_type: ledgerType.value, source_kind: sourceKind.value, source_name: '', event_key: '', entries: [] }, 'ledger-template.json');
}
async function downloadReport(report) {
  if (!props.canOperate || downloading.value || !batchId.value) return;
  downloading.value = true; error.value = '';
  try {
    const result = await api.get(`${path(batchId.value)}/ledger-reports/${encodeURIComponent(report.id)}/download`, { responseType: 'blob' });
    if (disposed || !props.canOperate) return;
    const filename = /(?:^|;)\s*filename="?([^";]+)"?/i.exec(result.headers?.['content-disposition'] || '')?.[1] || 'ledger-report.json';
    const url = URL.createObjectURL(result.data);
    const link = document.createElement('a'); link.href = url; link.download = filename;
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) { error.value = failure(e, '报告下载失败'); }
  finally { downloading.value = false; }
}
watch(() => props.batches.map(item => item.id), ids => {
  if (!ids.includes(batchId.value)) batchId.value = ids[0] || '';
}, { immediate: true });
watch([batchId, () => props.canOperate], () => {
  resetImport(); parsing.value = false;
  reports.value = []; selectedReport.value = null; error.value = ''; notice.value = '';
  loadReports();
}, { immediate: true });
watch([ledgerType, sourceKind], renewKey);
onUnmounted(() => { disposed = true; ++loadVersion; ++fileVersion; });
</script>

<template>
  <section class="ledger-comparison">
    <div class="panel-heading"><div><span class="section-kicker">LEDGER COMPARISON</span><h3>独立账单比对</h3></div><span class="status-pill status-review">未接真实渠道</span></div>
    <div class="ledger-toolbar">
      <label>清算批次<select v-model="batchId" :disabled="busy || parsing || !canOperate"><option value="" disabled>选择批次</option><option v-for="item in batches" :key="item.id" :value="item.id">{{ item.batch_no }}</option></select></label>
      <label>账簿<select v-model="ledgerType" :disabled="busy || parsing || !canOperate"><option value="platform">平台账</option><option value="payment">支付账</option><option value="bank">银行账</option><option value="distribution">分账账</option></select></label>
      <label>来源<select v-model="sourceKind" :disabled="busy || parsing || !canOperate"><option value="manual_import">人工导入</option><option value="simulation">模拟</option></select></label>
      <button class="secondary-btn" @click="downloadTemplate"><FileJson :size="15" />空 JSON 模板</button>
      <button class="icon-btn" title="刷新报告" aria-label="刷新报告" :disabled="loading || busy || !batchId || !canOperate" @click="error = ''; loadReports()"><RefreshCw :size="17" /></button>
    </div>
    <div v-if="canOperate" class="ledger-import-row"><label class="ledger-file"><Upload :size="16" /><span>JSON 账单</span><input ref="fileInput" type="file" accept=".json,application/json" :disabled="busy || parsing || !batchId" @change="chooseFile" /></label><span class="muted">{{ parsing ? '读取中' : sourceName ? `${sourceName} · ${entries.length} 行` : '最多 5 MB / 5000 行' }}</span><button class="primary-btn" :disabled="busy || parsing || !entries.length || !batchId" @click="importAndCompare"><RefreshCw v-if="importedId" :size="15" /><Upload v-else :size="15" />{{ busy ? '处理中' : importedId ? '重试比对' : '导入并比对' }}</button></div>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p><p v-if="notice" class="muted" role="status">{{ notice }}</p>
    <div v-if="canOperate" class="table-wrap" :aria-busy="loading"><table class="data-table ledger-report-table"><thead><tr><th>来源文件</th><th>账簿 / 来源</th><th>比对时间</th><th>状态</th><th>差异 / 重复流水</th><th>操作</th></tr></thead><tbody><tr v-for="report in reports" :key="report.id"><td>{{ report.source_name }}</td><td>{{ ledgerName(report.ledger_type) }}<small>{{ sourceLabel(report.source_kind) }} · 未接真实渠道</small></td><td>{{ date(report.compared_at) }}</td><td><span class="status-pill" :class="statusClass(report.status)">{{ statusLabel(report.status) }}</span></td><td>{{ (report.items || []).filter(item => item.status !== 'matched').length }} / {{ report.duplicates?.length || 0 }}</td><td><div class="action-list"><button class="icon-btn" title="查看报告" aria-label="查看报告" @click="openReport(report)"><Eye :size="16" /></button><button class="icon-btn" title="下载 JSON 报告" aria-label="下载 JSON 报告" :disabled="downloading" @click="downloadReport(report)"><Download :size="16" /></button></div></td></tr></tbody></table><div v-if="!reports.length" class="empty-state"><FileJson :size="22" />{{ loading ? '加载中' : batchId ? '暂无账单比对报告' : '暂无清算批次' }}</div></div>
    <p v-else class="muted">无账单比对权限</p>
    <div v-if="selectedReport" class="modal-scrim" @click.self="!busy && (selectedReport = null)" @keydown.esc.stop="!busy && (selectedReport = null)"><section class="modal-card ledger-modal" role="dialog" aria-modal="true" aria-label="账单比对报告"><div class="drawer-head"><div><h2>账单比对报告</h2><span class="muted">{{ selectedReport.batch_no }}</span></div><button class="icon-btn" title="关闭" aria-label="关闭报告" :disabled="busy" @click="selectedReport = null"><X :size="18" /></button></div><div class="state-grid"><div><small>来源文件</small><strong>{{ selectedReport.source_name }}</strong></div><div><small>账簿</small><strong>{{ ledgerName(selectedReport.ledger_type) }}</strong></div><div><small>来源</small><strong>{{ sourceLabel(selectedReport.source_kind) }} · 未接真实渠道</strong></div><div><small>比对时间</small><strong>{{ date(selectedReport.compared_at) }}</strong></div><div><small>报告状态</small><strong>{{ statusLabel(selectedReport.status) }}</strong></div><div><small>金额口径</small><strong>{{ selectedReport.amount_basis === 'allocated_profit' ? '分配利润' : '订单净收款' }}</strong></div></div><div class="table-wrap"><table class="data-table ledger-items-table"><thead><tr><th>订单号</th><th>应有金额</th><th>账单金额</th><th>差额</th><th>状态</th></tr></thead><tbody><tr v-for="(item, index) in visibleItems" :key="`${item.order_no}-${index}`"><td>{{ item.order_no }}</td><td>{{ amount(item.expected_amount) }}</td><td>{{ amount(item.actual_amount) }}</td><td>{{ amount(item.difference_amount) }}</td><td><span class="status-pill" :class="statusClass(item.status)">{{ statusLabel(item.status) }}</span></td></tr></tbody></table></div><div v-if="selectedReport.items?.length > 50" class="ledger-pagination"><button class="secondary-btn" :disabled="itemPage === 1" @click="itemPage--">上一页</button><span class="muted">{{ itemPage }} / {{ Math.ceil(selectedReport.items.length / 50) }}</span><button class="secondary-btn" :disabled="itemPage * 50 >= selectedReport.items.length" @click="itemPage++">下一页</button></div><div class="ledger-duplicates"><h3>重复流水 {{ selectedReport.duplicates?.length || 0 }} 条</h3><ul v-if="selectedReport.duplicates?.length"><li v-for="(item, index) in selectedReport.duplicates" :key="index">{{ item }}</li></ul></div><form v-if="canOperate && selectedReport.status === 'difference'" class="ledger-resolution" @submit.prevent="resolveReport"><label>差异处理说明<textarea v-model="resolution.reason" rows="3" minlength="3" maxlength="1000" :disabled="busy" required /></label><label>处理依据<input v-model="resolution.evidence_ref" minlength="3" maxlength="500" :disabled="busy" required /></label><button class="primary-btn" :disabled="busy"><CheckCircle2 :size="15" />记录差异处理</button></form><div v-if="selectedReport.resolution" class="ledger-resolution"><h3>差异处理说明</h3><p>{{ selectedReport.resolution }}</p><h3>处理依据</h3><p>{{ selectedReport.evidence_ref }}</p><p v-if="selectedReport.resolved_by" class="muted">{{ selectedReport.resolved_by }}</p></div><p v-if="error" class="error-text" role="alert">{{ error }}</p><div class="modal-actions"><button class="secondary-btn" :disabled="downloading" @click="downloadReport(selectedReport)"><Download :size="15" />下载 JSON 报告</button><button class="secondary-btn" :disabled="busy" @click="selectedReport = null">关闭</button></div></section></div>
  </section>
</template>

<style scoped>
.ledger-comparison{border-top:1px solid #e5e6eb;margin-top:24px;padding-top:20px;min-width:0}.ledger-toolbar{display:flex;align-items:flex-end;flex-wrap:wrap;gap:12px;margin-bottom:16px}.ledger-toolbar label{display:flex;flex-direction:column;gap:6px;font-size:11px;font-weight:600;color:#4e5969;min-width:140px}.ledger-toolbar select{border:1px solid #d9dce1;border-radius:7px;background:#fff;padding:9px 10px;font-size:12px;max-width:100%}.ledger-import-row{display:flex;align-items:center;flex-wrap:wrap;gap:12px;margin:14px 0 20px}.ledger-file{display:flex;align-items:center;flex-wrap:wrap;gap:8px;font-size:12px}.ledger-file input{max-width:260px;font-size:12px}.ledger-import-row>.muted{overflow-wrap:anywhere;min-width:0;flex:1 1 140px}.ledger-report-table{min-width:750px}.ledger-report-table td:first-child{max-width:240px;overflow-wrap:anywhere}.table-wrap{overflow-x:auto}.ledger-modal{width:min(920px,calc(100% - 32px));max-height:90vh;overflow:auto;border-radius:8px}.ledger-modal h2{font-size:18px;margin:0 0 7px}.ledger-modal h3{font-size:12px;margin:12px 0 8px}.ledger-modal .state-grid strong{overflow-wrap:anywhere}.ledger-items-table{min-width:560px}.ledger-pagination,.modal-actions{display:flex;align-items:center;justify-content:flex-end;gap:10px;flex-wrap:wrap;margin-top:16px}.ledger-duplicates{margin:18px 0}.ledger-duplicates ul{max-height:180px;overflow:auto;padding-left:20px;font-size:12px;line-height:1.7;overflow-wrap:anywhere}.ledger-resolution{border-top:1px solid #e5e6eb;padding-top:12px;margin-top:16px}.ledger-resolution p{font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere}.ledger-comparison button:disabled{opacity:.5;cursor:not-allowed}@media(max-width:600px){.ledger-toolbar label{flex:1 1 140px;min-width:0}.ledger-toolbar select{width:100%}.ledger-modal{padding:18px}.ledger-file input{max-width:100%}}
</style>
