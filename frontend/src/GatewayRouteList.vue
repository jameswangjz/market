<script setup>
import { computed, nextTick, onMounted, onBeforeUnmount, reactive, ref, watch } from "vue";
import { Eye, RefreshCw, Check, Play, X } from "lucide-vue-next";
import PaginationControls from "./PaginationControls.vue";
import MainListFilters from "./MainListFilters.vue";
import ConsoleTabs from "./ConsoleTabs.vue";
import { listState, applyListFilters, changeListPage, fetchListPage } from "./mainList.js";
import { newRenewalRequestKey } from "./subscriptionTerms.js";
const props = defineProps({ api: Object, dialogs: Object });
const rows = ref([]), selected = ref(null), tab = ref("stats"), stats = ref({});
const lists = reactive({ routes: listState({ route_name: "", product_name: "", provider: "", product_status: "published", route_status: "", health_status: "" }), stats: listState(), revisions: listState(), alerts: listState(), audit: listState() });
const details = reactive({ stats: [], revisions: [], alerts: [], audit: [] });
const auditAlert = ref(null), actionBusy = ref(false), actionError = ref("");
const operations = new Map();
let alive = true;
let session = 0, previousFocus;
const panel = ref(null);
const detailTabs = [{ key: "stats", label: "调用统计" }, { key: "revisions", label: "发布记录" }, { key: "alerts", label: "告警" }];
watch(selected, (row, old) => {
  ++session;
  if (row) {
    if (!old) previousFocus = document.activeElement;
  } else if (panel.value?.contains(document.activeElement)) {
    previousFocus?.isConnected && previousFocus.focus();
  }
}, { flush: "sync" });
function closeDetail() { selected.value = null; auditAlert.value = null; }
function detailKeydown(event) {
  // ActionDialog is teleported above this modal and owns its keyboard events.
  if (event.defaultPrevented || actionBusy.value || !panel.value?.contains(event.target)) return;
  if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeDetail(); }
  if (event.key !== "Tab") return;
  const controls = [...panel.value.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]')].filter(element => element.getClientRects().length);
  const first = controls[0], last = controls.at(-1);
  if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.value)) {
    event.preventDefault(); (last || panel.value).focus();
  } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === panel.value)) {
    event.preventDefault(); (first || panel.value).focus();
  }
}
onBeforeUnmount(() => { alive = false; selected.value = null; });
const labels = { published: "已发布", draft: "草稿", disabled: "已停用", unpublished: "已下架", security_unpublished: "安全下架", rejected: "已拒绝", pending_review: "待审核", pending_integration: "待接入验证", active: "生效中", pending_health: "待健康检查", publish_failed: "发布失败", healthy: "正常", unhealthy: "异常", unknown: "未知", open: "待确认", acknowledged: "已确认", processing: "处理中", closed: "已关闭", failed: "失败", success: "成功", error: "错误", acknowledge: "确认", process: "处理", close: "关闭" };
const display = value => labels[value] || (value ? `未知（${value}）` : "未记录");
const sourceLabel = value => ({ application_health_observations: "应用健康观测", route_control_plane_state: "路由控制面状态", api_usage: "API调用记录", sla_gateway_terminal_events: "网关终态事件", none: "未采集" }[value] || "未知来源");
const opts = values => values.map(value => ({ value, label: value === "all" ? "全部产品状态" : display(value) }));
const fields = computed(() => [{ key: "route_name", label: "路由名称" }, { key: "product_name", label: "产品名称" }, { key: "provider", label: "提供方" }, { key: "product_status", label: "产品状态", options: opts(["all", "published", "disabled", "unpublished", "security_unpublished", "draft", "rejected", "pending_review", "pending_integration"]) }, { key: "route_status", label: "路由状态", options: opts(["draft", "active", "disabled", "pending_health", "publish_failed"]) }, { key: "health_status", label: "健康状态", options: opts(["healthy", "unhealthy", "unknown"]) }]);
const date = value => value ? new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "未记录";
const path = () => `/gateway/routes/${encodeURIComponent(selected.value.id)}`;
async function loadRoutes() {
  await fetchListPage(props.api, "/gateway/routes", lists.routes, {}, () => alive, items => { rows.value = items; });
  if (selected.value && !rows.value.some(row => row.id === selected.value.id)) selected.value = null;
}
async function search(reset = false) {
  applyListFilters(lists.routes, reset);
  if (reset) { lists.routes.filters.product_status = "published"; lists.routes.applied.product_status = "published"; }
  await loadRoutes();
}
async function loadDetail(kind = tab.value) {
  if (!selected.value || (kind === "audit" && !auditAlert.value)) return;
  const id = selected.value.id, alertId = auditAlert.value?.id, current = session;
  const endpoint = kind === "audit" ? `${path()}/alerts/${encodeURIComponent(alertId)}/audit` : `${path()}/${kind}`;
  await fetchListPage(props.api, endpoint, lists[kind], {}, () => alive && session === current && selected.value?.id === id && (kind !== "audit" || auditAlert.value?.id === alertId), (items, data) => { details[kind] = items; if (kind === "stats") stats.value = data || {}; });
}
async function select(row) {
  selected.value = row; auditAlert.value = null; stats.value = {}; actionError.value = "";
  const current = session;
  for (const kind of ["stats", "revisions", "alerts", "audit"]) { lists[kind].generation++; lists[kind].page = 1; lists[kind].total = 0; lists[kind].busy = false; lists[kind].error = ""; details[kind] = []; }
  // Wait after the reactive update queues rendering, not inside its sync watcher.
  await nextTick();
  if (!alive || session !== current) return;
  panel.value?.focus();
  await loadDetail();
}
async function page(kind, value, size) { if (changeListPage(lists[kind], value, size)) await (kind === "routes" ? loadRoutes() : loadDetail(kind)); }
async function showTab(kind) { tab.value = kind; await loadDetail(kind); }
async function audit(item) { auditAlert.value = item; lists.audit.page = 1; await loadDetail("audit"); }
async function act(item, action) {
  if (actionBusy.value || !selected.value) return;
  actionBusy.value = true; actionError.value = "";
  const routeId = selected.value.id, current = session;
  const isCurrent = () => alive && session === current && selected.value?.id === routeId;
  try {
    const note = await props.dialogs.prompt(action === "close" ? "请填写关闭原因（关闭不代表故障恢复）" : "请填写处置说明", "");
    if (!isCurrent()) return;
    if (note === null || note === undefined) return;
    if (action === "close" && !note.trim()) { actionError.value = "关闭告警必须填写原因"; return; }
    if (note.length > 2000) { actionError.value = "说明不能超过2000字"; return; }
    const fingerprint = JSON.stringify([routeId, item.id, item.version, action, note]);
    if (!operations.has(fingerprint)) operations.set(fingerprint, newRenewalRequestKey());
    await props.api.post(`/gateway/routes/${encodeURIComponent(routeId)}/alerts/${encodeURIComponent(item.id)}/${action}`, { request_id: operations.get(fingerprint), expected_version: item.version, note });
    operations.delete(fingerprint);
    if (isCurrent()) { await loadDetail("alerts"); if (isCurrent() && auditAlert.value?.id === item.id) await loadDetail("audit"); }
  } catch (error) {
    if (!isCurrent()) return;
    actionError.value = error.response?.status === 409 ? "告警已变化，请核对刷新后的状态" : typeof error.response?.data?.detail === "string" ? error.response.data.detail : "操作失败，请重试";
    if (error.response?.status === 409 && selected.value?.id === routeId) await loadDetail("alerts");
  } finally { actionBusy.value = false; }
}
onMounted(loadRoutes);
</script>
<template>
  <div class="page-heading"><h1>API 网关</h1><button class="secondary-btn" :disabled="lists.routes.busy" @click="loadRoutes"><RefreshCw :size="16" />刷新</button></div>
  <MainListFilters :state="lists.routes" :fields="fields" @search="search()" @reset="search(true)" />
  <div class="table-wrap"><table class="data-table"><thead><tr><th>路由名称</th><th>产品</th><th>版本</th><th>提供方</th><th>产品状态</th><th>路由状态</th><th>健康状态</th><th>详情</th></tr></thead><tbody><tr v-for="row in rows" :key="row.id"><td>{{ row.route_name || row.route_key }}<small>{{ row.id }}</small></td><td>{{ row.product_name }}</td><td>{{ row.version || '未记录' }}</td><td>{{ row.provider_name || '未记录' }}</td><td>{{ display(row.product_status) }}</td><td>{{ display(row.route_status) }}</td><td>{{ display(row.health_status) }}<small>{{ sourceLabel(row.health_source) }}</small><small>探测时间：{{ date(row.health_checked_at) }}</small></td><td><button class="icon-btn" title="查看路由详情" @click="select(row)"><Eye :size="16" /></button></td></tr><tr v-if="!rows.length"><td colspan="8" class="empty-state">暂无匹配路由</td></tr></tbody></table></div>
  <PaginationControls :page="lists.routes.page" :page-size="lists.routes.page_size" :total="lists.routes.total" :busy="lists.routes.busy" label="网关路由分页" @page="page('routes', $event)" @size="page('routes', 1, $event)" />
  <Teleport to="body">
  <div v-if="selected" class="modal-scrim route-detail-scrim">
  <section ref="panel" class="modal-card route-details" role="dialog" aria-modal="true" aria-labelledby="gateway-route-detail-title" tabindex="-1" @keydown="detailKeydown">
    <div class="drawer-head"><h2 id="gateway-route-detail-title">{{ selected.route_name || selected.route_key }} · {{ selected.version }}</h2><button type="button" class="icon-btn" title="关闭路由详情" aria-label="关闭路由详情" :disabled="actionBusy" @click="closeDetail"><X :size="18" /></button></div>
    <ConsoleTabs :model-value="tab" :tabs="detailTabs" label="路由详情" @update:model-value="showTab" />
    <p v-if="lists[tab].error" class="danger-text" role="alert">{{ lists[tab].error }}</p>
    <template v-if="tab === 'stats'">
      <p>统计窗口：{{ date(stats.window_start) }} 至 {{ date(stats.window_end) }}（北京时间） · 来源：{{ sourceLabel(stats.source) }} · {{ stats.counts_complete === true ? '完整统计' : '仅已观测记录，覆盖范围未知' }}</p>
      <p>调用 {{ stats.summary?.total ?? '未知' }} · 成功 {{ stats.summary?.success ?? '未知' }} · 失败 {{ stats.summary?.error ?? '未知' }} · 平均延迟 {{ stats.summary?.avg_latency_ms ?? '未知' }} ms</p>
      <div class="table-wrap"><table class="data-table"><thead><tr><th>时间</th><th>方法</th><th>路径</th><th>状态码</th><th>延迟</th></tr></thead><tbody><tr v-for="item in details.stats" :key="item.id"><td>{{ date(item.created_at) }}</td><td>{{ item.method }}</td><td>{{ item.path }}</td><td>{{ item.status_code }}</td><td>{{ item.latency_ms ?? '未知' }} ms</td></tr></tbody></table></div>
    </template>
    <div v-else-if="tab === 'revisions'" class="table-wrap"><table class="data-table"><thead><tr><th>发布记录</th><th>状态</th><th>操作人</th><th>时间</th><th>错误</th></tr></thead><tbody><tr v-for="item in details.revisions" :key="item.id"><td>{{ item.revision_id || item.id }}</td><td>{{ display(item.status) }}</td><td>{{ item.created_by || '未记录' }}</td><td>{{ date(item.created_at) }}</td><td>{{ item.error_message || '-' }}</td></tr></tbody></table></div>
    <template v-else>
      <p v-if="actionError" role="alert" class="danger-text">{{ actionError }}</p>
      <div class="table-wrap"><table class="data-table"><thead><tr><th>等级</th><th>告警</th><th>说明</th><th>状态</th><th>时间</th><th>操作</th></tr></thead><tbody><tr v-for="item in details.alerts" :key="item.id"><td>{{ display(item.severity) }}</td><td>路由发布失败</td><td>{{ item.message }}</td><td>{{ display(item.status) }}</td><td>{{ date(item.created_at) }}</td><td><div class="route-actions"><button v-if="item.status === 'open'" class="text-btn" :disabled="actionBusy" @click="act(item, 'acknowledge')"><Check :size="14" />确认</button><button v-if="item.status === 'acknowledged'" class="text-btn" :disabled="actionBusy" @click="act(item, 'process')"><Play :size="14" />处理</button><button v-if="item.status !== 'closed'" class="text-btn" :disabled="actionBusy" @click="act(item, 'close')"><X :size="14" />关闭</button><button class="text-btn" @click="audit(item)"><Eye :size="14" />处置记录</button></div></td></tr></tbody></table></div>
    </template>
    <div v-if="!details[tab].length && !lists[tab].busy" class="empty-state">暂无记录</div>
    <PaginationControls :page="lists[tab].page" :page-size="lists[tab].page_size" :total="lists[tab].total" :busy="lists[tab].busy" :label="{ stats: '调用统计分页', revisions: '发布记录分页', alerts: '告警分页' }[tab]" @page="page(tab, $event)" @size="page(tab, 1, $event)" />
    <template v-if="tab === 'alerts' && auditAlert"><h4>告警处置记录</h4><p v-if="lists.audit.error" role="alert">{{ lists.audit.error }}</p><div class="table-wrap"><table class="data-table"><thead><tr><th>操作人</th><th>时间</th><th>操作</th><th>变更前</th><th>变更后</th><th>说明</th></tr></thead><tbody><tr v-for="item in details.audit" :key="item.id"><td>{{ item.actor }}</td><td>{{ date(item.created_at) }}</td><td>{{ display(item.action) }}</td><td>{{ display(item.before_status) }}</td><td>{{ display(item.after_status) }}</td><td>{{ item.note || '-' }}</td></tr></tbody></table></div><PaginationControls :page="lists.audit.page" :page-size="lists.audit.page_size" :total="lists.audit.total" :busy="lists.audit.busy" label="告警处置记录分页" @page="page('audit', $event)" @size="page('audit', 1, $event)" /></template>
  </section>
  </div>
  </Teleport>
</template>
<style scoped>
.route-detail-scrim{z-index:90;overscroll-behavior:contain}.modal-card.route-details{width:min(1080px,calc(100% - 32px));max-height:calc(100dvh - 32px);min-width:0;overflow:auto;overscroll-behavior:contain;border-radius:8px}.route-details .drawer-head h2{min-width:0;overflow-wrap:anywhere}.route-details .drawer-head .icon-btn{flex-shrink:0}.route-actions{display:flex;flex-wrap:wrap;gap:8px}td{overflow-wrap:anywhere}td small{display:block;color:#69717e;font-size:11px;margin-top:4px}.route-details p{font-size:13px;overflow-wrap:anywhere}
</style>
