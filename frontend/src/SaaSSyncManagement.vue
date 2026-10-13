<script setup>
import { computed, onMounted, onBeforeUnmount, ref, watch } from "vue";
import { ArrowLeft, ArrowRight, ChevronsLeft, ChevronsRight, RefreshCw, X } from "lucide-vue-next";
import { flattenSyncTree, selectedSyncIds, syncActionPayload } from "./saasSyncManagement.js";

const props = defineProps({ api: { type: Object, required: true }, subscriptionId: { type: String, required: true }, initialKind: { type: String, default: "users" } });
const emit = defineEmits(["close"]);
const tree = ref({ departments: [], unassigned_users: [], mappings: { users: [], departments: [] } });
const kind = ref(props.initialKind);
const leftSelection = ref([]);
const rightSelection = ref([]);
const loading = ref(false);
const submitting = ref(false);
const error = ref("");
const notice = ref("");
const removeAllPending = ref(false);
const removalIds = ref(null);
let loadSequence = 0;
const leftRows = computed(() => flattenSyncTree(tree.value, kind.value, false));
const rightRows = computed(() => flattenSyncTree(tree.value, kind.value, true));

async function load(preserveError = false) {
  if (!props.subscriptionId) return;
  const request = ++loadSequence;
  const subscriptionId = props.subscriptionId;
  const requestKind = kind.value;
  loading.value = true;
  if (preserveError !== true) error.value = "";
  try {
    const { data } = await props.api.get(`/saas-subscriptions/${subscriptionId}/sync-tree`);
    if (request !== loadSequence || subscriptionId !== props.subscriptionId || requestKind !== kind.value) return;
    tree.value = data;
    leftSelection.value = [];
    rightSelection.value = [];
  } catch (cause) {
    if (request === loadSequence && subscriptionId === props.subscriptionId && requestKind === kind.value) {
      const detail = cause.response?.data?.detail || "同步树加载失败";
      error.value = preserveError === true && error.value ? `${error.value}；${detail}` : detail;
    }
  } finally {
    if (request === loadSequence) loading.value = false;
  }
}

async function move(action, ids = null) {
  if (submitting.value || loading.value) return;
  submitting.value = true;
  error.value = "";
  notice.value = "";
  const subscriptionId = props.subscriptionId;
  const operationKind = kind.value;
  const requestScope = loadSequence;
  const inScope = () => requestScope === loadSequence && subscriptionId === props.subscriptionId && operationKind === kind.value;
  try {
    const { data } = await props.api.post(`/saas-subscriptions/${subscriptionId}/sync-actions`,
      syncActionPayload(action, operationKind, ids === null ? null : selectedSyncIds(action === 'add' ? leftRows.value : rightRows.value, operationKind, ids)));
    if (!inScope()) return;
    notice.value = `成功 ${data.succeeded} 项，失败 ${data.failed} 项，跳过 ${data.skipped} 项`;
    if (data.items?.some(item => item.status === "failed")) {
      error.value = data.items.filter(item => item.status === "failed").map(item => `${item.id}: ${item.detail}`).join("；");
    }
    await load(true);
  } catch (cause) {
    if (inScope()) error.value = cause.response?.data?.detail || "同步操作失败";
  } finally {
    submitting.value = false;
  }
}

function requestRemoveAll(ids = null) {
  removalIds.value = ids === null ? null : selectedSyncIds(rightRows.value, kind.value, ids);
  if (removalIds.value && !removalIds.value.length) return;
  if (rightRows.value.some(row => row.type === (kind.value === 'users' ? 'user' : 'department'))) removeAllPending.value = true;
}

watch(kind, () => { leftSelection.value = []; rightSelection.value = []; removeAllPending.value = false; error.value = ''; notice.value = ''; load(); });
watch(() => props.initialKind, value => { kind.value = value; });
watch(() => props.subscriptionId, () => { removeAllPending.value = false; notice.value = ''; load(); });
onMounted(load);
onBeforeUnmount(() => { loadSequence += 1; });
</script>

<template>
  <section class="saas-sync-management" aria-label="SaaS 用户与部门同步" tabindex="0" @keydown.esc.stop="emit('close')">
    <div class="saas-sync-toolbar">
      <strong>{{ kind === 'users' ? '用户管理' : '部门管理' }}</strong>
      <div class="saas-sync-toolbar-actions"><button type="button" class="icon-btn" title="刷新同步树" aria-label="刷新同步树" :disabled="loading || submitting" @click="load"><RefreshCw :size="15" /></button><button type="button" class="icon-btn" title="关闭同步管理" aria-label="关闭同步管理" @click="emit('close')"><X :size="16" /></button></div>
    </div>
    <p v-if="error" class="danger-text" role="alert">{{ error }}</p>
    <p v-else-if="notice" class="muted" role="status">{{ notice }}</p>
    <div class="saas-sync-columns" :aria-busy="loading || submitting">
      <section class="saas-sync-tree" aria-label="企业成员与部门">
        <header><strong>{{ kind === 'users' ? '企业成员' : '企业部门' }}</strong><span>{{ leftRows.length }}</span></header>
        <label v-for="row in leftRows" :key="row.key" class="saas-sync-row" :class="{ 'saas-sync-group': kind === 'users' && row.type === 'department' }" :style="{ '--tree-depth': row.depth }">
          <input v-if="row.selectable" v-model="leftSelection" type="checkbox" :value="row.id" :disabled="loading || submitting" />
          <span>{{ row.name }}</span><small>{{ kind === 'users' ? (row.type === 'department' ? '部门' : row.email || '成员') : '部门' }}</small>
        </label>
        <p v-if="!loading && !leftRows.length" class="muted">没有可添加的项目</p>
      </section>
      <div class="saas-sync-arrows" aria-label="同步操作">
        <button type="button" class="icon-btn" title="添加所选" aria-label="添加所选" :disabled="!leftSelection.length || submitting" @click="move('add', leftSelection)"><ArrowRight :size="17" /></button>
        <button type="button" class="icon-btn" title="添加全部" aria-label="添加全部" :disabled="!leftRows.length || submitting" @click="move('add')"><ChevronsRight :size="17" /></button>
        <button type="button" class="icon-btn" title="移除全部" aria-label="移除全部" :disabled="!rightRows.length || submitting" @click="requestRemoveAll"><ChevronsLeft :size="17" /></button>
        <button type="button" class="icon-btn" title="移除所选" aria-label="移除所选" :disabled="!rightSelection.length || submitting" @click="requestRemoveAll(rightSelection)"><ArrowLeft :size="17" /></button>
      </div>
      <section class="saas-sync-tree" aria-label="SaaS 已同步项目">
        <header><strong>SaaS 已同步</strong><span>{{ rightRows.length }}</span></header>
        <label v-for="row in rightRows" :key="row.key" class="saas-sync-row" :class="{ 'saas-sync-group': !row.selectable }" :style="{ '--tree-depth': row.depth }">
          <input v-if="row.selectable" v-model="rightSelection" type="checkbox" :value="row.id" :disabled="loading || submitting" />
          <span>{{ row.name }}</span><small>{{ kind === 'users' ? row.email || '成员' : '部门' }}</small>
        </label>
        <p v-if="!loading && !rightRows.length" class="muted">暂无已同步项目</p>
      </section>
    </div>
    <div v-if="removeAllPending" class="sync-confirm-scrim" @click.self="removeAllPending = false">
      <section class="sync-confirm" role="alertdialog" aria-modal="true" aria-labelledby="sync-remove-all-title" @keydown.esc.stop="removeAllPending = false">
        <h3 id="sync-remove-all-title">{{ removalIds?.length ? '移除所选已同步项目？' : '移除全部已同步项目？' }}</h3>
        <p>仍被子部门、成员或管理员依赖的项目会保留并报告失败。</p>
        <div><button type="button" class="secondary-btn" @click="removeAllPending = false">取消</button><button type="button" class="danger-btn" :disabled="submitting" @click="removeAllPending = false; move('remove', removalIds)">确认移除</button></div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.saas-sync-management { min-width: 0; }
.saas-sync-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 10px; }
.saas-sync-toolbar-actions { display: flex; gap: 4px; }
.saas-sync-columns { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); gap: 12px; align-items: stretch; }
.saas-sync-tree { min-width: 0; max-height: 340px; overflow: auto; border: 1px solid #e1e5e9; border-radius: 6px; }
.saas-sync-tree header { position: sticky; top: 0; display: flex; justify-content: space-between; padding: 9px 10px; border-bottom: 1px solid #e1e5e9; background: #f7f9fa; font-size: 12px; }
.saas-sync-row { display: flex; align-items: center; gap: 8px; min-height: 34px; padding: 5px 9px; padding-left: calc(9px + var(--tree-depth) * 16px); border-bottom: 1px solid #f0f1f3; }
.saas-sync-row input { flex: 0 0 15px; accent-color: #176b57; }
.saas-sync-row span { min-width: 0; overflow-wrap: anywhere; font-size: 12px; }
.saas-sync-row small { margin-left: auto; color: #77818c; overflow-wrap: anywhere; font-size: 11px; }
.saas-sync-group { min-height: 30px; background: #f6f8f8; font-weight: 600; }
.saas-sync-arrows { display: flex; flex-direction: column; justify-content: center; gap: 6px; }
.saas-sync-tree > p { padding: 0 10px; }
.sync-confirm-scrim { position: fixed; inset: 0; z-index: 5; display: grid; place-items: center; padding: 16px; background: #11182766; }
.sync-confirm { width: min(420px, 100%); border: 1px solid #dce1e5; border-radius: 8px; padding: 18px; background: white; box-shadow: 0 18px 50px #10182833; }
.sync-confirm h3 { margin: 0; font-size: 16px; }
.sync-confirm p { color: #5f6872; font-size: 13px; line-height: 1.5; }
.sync-confirm > div { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
.danger-btn { border: 1px solid #b42318; border-radius: 6px; padding: 7px 11px; color: white; background: #b42318; cursor: pointer; }
@media (max-width: 640px) { .saas-sync-columns { grid-template-columns: minmax(0, 1fr); } .saas-sync-tree { max-height: 260px; } .saas-sync-arrows { flex-direction: row; justify-content: center; } }
</style>
