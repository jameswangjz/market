<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';
import axios from 'axios';
import { RefreshCw, Archive, ArchiveRestore, ShieldAlert } from 'lucide-vue-next';
import ActionDialog from './ActionDialog.vue';
import { newRenewalRequestKey } from './subscriptionTerms.js';

const snapshot = ref(null), tasks = ref([]), activeTask = ref(null), ownTask = ref(null);
const busy = ref(false), loading = ref(false), error = ref(''), reason = ref(''), acceptGap = ref(false), dialog = ref(null);
const pending = ref(null), fresh = ref(false);
const endpoint = '/api/admin/maintenance/redis';
const actions = { inspect: '刷新状态', prepare: 'Redis维护准备', resume: 'Redis维护后恢复', recover: 'Redis异常重启恢复' };
const actionIcons = { inspect: RefreshCw, prepare: Archive, resume: ArchiveRestore, recover: ShieldAlert };
const statuses = { queued: '排队中', running: '执行中', succeeded: '成功', failed: '失败', manual_review: '需人工复核' };
const stages = { queued: '等待执行', inspect: '检查状态', prepare: '维护准备', resume: '维护后恢复', recover: '异常恢复', backup: '备份数据库', verify_seal: '校验封存证据', verify: '检查恢复状态', open: '登记缺口并建立新分代', snapshot: '采集状态', validate: '校验', seal: '封存来源', completed: '已完成', failed: '执行失败' };
const reasonLabels = { managed_source_missing: '缺少受管来源', stream_missing_or_wrong_type: '来源流缺失或类型异常', cursor_registration_mismatch: '游标注册不一致', cursor_entry_missing: '游标记录缺失', marker_mismatch: '连续性标记不一致', physical_run_id_mismatch: 'Redis 进程已变化', no_paused_seal_continuity_not_proven: '未封存，连续性无法证明', sealed_evidence_or_drained_cursor_mismatch: '封存证据不一致', seal_verification_unavailable: '封存校验不可用', persistence_not_healthy_or_loading: '持久化异常或正在加载', source_changed_during_inspection: '检查期间来源发生变化', database_source_changed_during_inspection: '检查期间来源登记变化' };
const snapshotLabels = { active_source_healthy: '当前来源健康', safe_sealed_resume_candidate: '已封存，可执行维护后恢复', explicit_gap_required: '恢复需确认未知缺口' };
const isActive = task => ['queued', 'running'].includes(task?.status);
const blocked = computed(() => busy.value || loading.value || isActive(activeTask.value) || isActive(ownTask.value));
const auditTasks = computed(() => [...new Map([activeTask.value, ownTask.value, ...tasks.value].filter(Boolean).map(task => [task.id, task])).values()]);
function recentSnapshot() {
  const captured = Date.parse(snapshot.value?.captured_at || '');
  return fresh.value && Number.isFinite(captured) && Date.now() - captured >= -5000 && Date.now() - captured < 90000;
}
const safeMaintenance = computed(() => recentSnapshot() && snapshot.value?.status === 'safe_sealed_resume_candidate' && [...auditTasks.value].filter(task => task.action !== 'inspect').sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || ''))).slice(0, 1).some(task => task.action === 'prepare' && task.status === 'succeeded'));
function allowed(action) {
  if (blocked.value) return false;
  if (action === 'inspect') return true;
  return recentSnapshot() && (action !== 'prepare' || snapshot.value?.worker?.ready === true)
    && snapshot.value?.status === ({ prepare: 'active_source_healthy', resume: 'safe_sealed_resume_candidate', recover: 'explicit_gap_required' })[action];
}
function headers() { return { Authorization: `Bearer ${localStorage.getItem('market_token') || ''}` }; }
let timer = null, disposed = false;
function syncPolling() {
  if (disposed) return;
  if (isActive(activeTask.value) || isActive(ownTask.value)) {
    if (timer === null) timer = setInterval(load, 3000);
  } else if (timer !== null) { clearInterval(timer); timer = null; }
}
async function load() {
  if (loading.value || disposed) return;
  loading.value = true;
  try {
    const { data } = await axios.get(endpoint, { headers: headers() });
    if (disposed) return;
    snapshot.value = data.snapshot; activeTask.value = data.active_task; tasks.value = data.tasks || [];
    if (ownTask.value) ownTask.value = [data.active_task, ...tasks.value].find(task => task?.id === ownTask.value.id) || ownTask.value;
    fresh.value = true; error.value = '';
  } catch { fresh.value = false; error.value = '状态读取失败，请稍后重试刷新状态。'; }
  finally { loading.value = false; syncPolling(); }
}
function request(action) {
  if (!allowed(action)) return;
  if (action === 'inspect') { submit(action); return; }
  if (!reason.value.trim() || (action === 'recover' && !acceptGap.value)) { error.value = '请填写操作原因；异常恢复还需确认接受未知缺口。'; return; }
  dialog.value = { title: actions[action], message: `${actions[action]}将改变 Redis 来源连续性状态。操作原因：${reason.value.trim()}${action === 'recover' ? '。已确认接受未知数据缺口。' : ''}`, confirmText: '确认执行', action };
}
function confirm() { const action = dialog.value?.action; dialog.value = null; if (action) return submit(action); }
async function submit(action) {
  if (!allowed(action)) return;
  const payload = { action, reason: reason.value.trim(), accept_gap: action === 'recover' && acceptGap.value };
  if (action !== 'inspect' && (!payload.reason || (action === 'recover' && !payload.accept_gap))) return;
  if (!pending.value || JSON.stringify(pending.value.payload) !== JSON.stringify(payload)) pending.value = { payload, key: newRenewalRequestKey() };
  busy.value = true; error.value = '';
  try {
    const { data } = await axios.post(`${endpoint}/tasks`, { ...pending.value.payload, idempotency_key: pending.value.key }, { headers: headers() });
    if (disposed) return;
    ownTask.value = data; pending.value = null;
    await load();
  } catch { error.value = '任务提交失败，可使用相同内容重试；请先核对任务记录。'; }
  finally { busy.value = false; syncPolling(); }
}
onMounted(load);
onUnmounted(() => { disposed = true; if (timer !== null) clearInterval(timer); });
</script>

<template>
  <section class="redis-maintenance">
    <div class="page-heading"><div><div class="eyebrow">平台管理</div><h1>系统维护</h1></div></div>
    <p class="muted">维护准备成功后再停机；系统启动后执行维护后恢复。维护期间计费接入暂停。</p>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    <h2>Redis 状态</h2>
    <dl class="redis-status">
      <dt>连续性状态</dt><dd>{{ fresh ? (snapshotLabels[snapshot?.status] || '未知，禁止维护操作') : '未知，等待有效状态' }}</dd>
      <dt>检查结果</dt><dd><span v-for="(item, index) in snapshot?.reasons || []" :key="index">{{ reasonLabels[item] || '其他检查异常，请按任务编号复核' }}；</span><span v-if="!snapshot?.reasons?.length">—</span></dd>
      <dt>采集时间</dt><dd>{{ snapshot?.captured_at || '—' }}</dd>
      <dt>来源</dt><dd>{{ snapshot?.source?.source_id || '—' }} / {{ snapshot?.source?.sealed === true ? '已封存' : snapshot?.source?.sealed === false ? '未封存' : '未知' }}</dd>
      <dt>持久化</dt><dd>加载中：{{ snapshot?.persistence?.loading ?? '未知' }}；AOF：{{ snapshot?.persistence?.aof_enabled ?? '未知' }}；写入：{{ snapshot?.persistence?.aof_last_write_status === 'ok' ? '正常' : '未知或异常' }}</dd>
      <dt>账本</dt><dd>计费记录：{{ snapshot?.ledger?.total_charges ?? '—' }}；缺口：{{ Array.isArray(snapshot?.ledger?.gaps) ? snapshot.ledger.gaps.length : snapshot?.ledger?.gaps ?? '—' }}</dd>
      <dt>计费采集Worker</dt><dd>{{ snapshot?.worker?.ready === true ? '就绪' : snapshot?.worker?.status === 'blocked' ? '已暂停' : '未就绪或未报告' }} {{ snapshot?.worker?.checked_at || '' }}</dd>
      <dt>维护执行器心跳</dt><dd>{{ snapshot?.executor_seen_at || '未报告' }}</dd>
    </dl>
    <p v-if="safeMaintenance" role="status">维护准备已成功且来源已封存，可按运维流程进行维护；完成后请刷新状态，再执行维护后恢复。</p>
    <label>操作原因<textarea v-model="reason" rows="2" maxlength="1000" :disabled="busy" /></label>
    <label class="checkbox-line"><input v-model="acceptGap" type="checkbox" :disabled="busy" />我确认接受异常恢复可能产生的未知数据缺口</label>
    <div class="redis-actions"><button v-for="(label, action) in actions" :key="action" class="secondary-btn" type="button" :disabled="!allowed(action)" @click="request(action)"><component :is="actionIcons[action]" :size="15" />{{ label }}</button></div>
    <h2>最近维护任务</h2>
    <p v-if="!auditTasks.length">暂无任务</p>
    <div class="table-wrap redis-task-table"><table v-if="auditTasks.length" class="data-table compact"><thead><tr><th>任务编号 / 操作</th><th>状态 / 阶段</th><th>操作人 / 原因</th><th>时间</th><th>步骤 / 结果</th></tr></thead><tbody>
      <tr v-for="task in auditTasks" :key="task.id"><td>{{ task.task_no || task.id }}<br />{{ actions[task.action] || '未知操作' }}</td><td>{{ statuses[task.status] || '未知状态' }}<br />{{ stages[task.stage] || '处理中或待复核' }}</td><td>{{ typeof task.actor === 'string' ? task.actor : task.actor?.name || task.actor?.id || '—' }}<br />{{ task.reason || '—' }}</td><td>创建：{{ task.created_at || '—' }}<br />开始：{{ task.started_at || '—' }}<br />结束：{{ task.finished_at || '—' }}</td><td><ul><li v-for="(step, index) in task.steps || []" :key="index">步骤 {{ index + 1 }}：{{ step.label || stages[step.stage || step.name || step.step] || '维护检查' }} — {{ statuses[step.status] || '已记录' }}</li></ul><span v-if="task.error" class="error-text">{{ task.error }}</span></td></tr>
    </tbody></table></div>
    <ActionDialog :dialog="dialog" @submit="confirm" @cancel="dialog = null" />
  </section>
</template>

<style scoped>
.redis-maintenance h2{font-size:14px;margin:22px 0 12px}.redis-maintenance p{font-size:13px;line-height:1.7}.redis-status{display:grid;grid-template-columns:110px 1fr;gap:10px;font-size:13px}.redis-status dd{margin:0;overflow-wrap:anywhere}.redis-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}.redis-maintenance textarea{max-width:720px}.redis-task-table{overflow-x:auto}.redis-task-table td{vertical-align:top;overflow-wrap:anywhere}.redis-task-table ul{padding-left:18px;margin:0}
</style>
