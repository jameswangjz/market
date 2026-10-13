<script setup>
import { computed, onMounted, onBeforeUnmount, ref, watch } from "vue";
import { ArrowLeft, ArrowRight, ChevronsLeft, ChevronsRight, RefreshCw, X } from "lucide-vue-next";
import { flattenSyncTree, selectedSyncIds, syncActionPayload, syncActionBatches } from "./saasSyncManagement.js";

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
const removalAll = ref(false);
let loadSequence = 0;
let scopeSequence = 0;
let closed = false;
let activeController = null;
const leftRows = computed(() => flattenSyncTree(tree.value, kind.value, false));
const rightRows = computed(() => flattenSyncTree(tree.value, kind.value, true));

async function load(preserveError = false) {
  if (closed || !props.subscriptionId) return;
  const request = ++loadSequence;
  const scope = scopeSequence;
  const subscriptionId = props.subscriptionId;
  const requestKind = kind.value;
  loading.value = true;
  if (preserveError !== true) error.value = "";
  try {
    const { data } = await props.api.get(`/saas-subscriptions/${subscriptionId}/sync-tree`);
    if (closed || scope !== scopeSequence || request !== loadSequence || subscriptionId !== props.subscriptionId || requestKind !== kind.value) return;
    tree.value = data;
    leftSelection.value = [];
    rightSelection.value = [];
  } catch (cause) {
    if (!closed && scope === scopeSequence && request === loadSequence && subscriptionId === props.subscriptionId && requestKind === kind.value) {
      const detail = cause.response?.data?.detail || "同步树加载失败";
      error.value = preserveError === true && error.value ? `${error.value}；${detail}` : detail;
    }
  } finally {
    if (!closed && scope === scopeSequence && request === loadSequence) loading.value = false;
  }
}

async function move(action, ids = null) {
  if (closed || submitting.value || loading.value) return;
  let batches;
  try {
    batches = syncActionBatches(action === 'add' ? leftRows.value : rightRows.value, action, kind.value, ids);
  } catch (cause) {
    error.value = cause.message;
    return;
  }
  submitting.value = true;
  error.value = "";
  notice.value = "";
  const subscriptionId = props.subscriptionId;
  const operationKind = kind.value;
  const requestScope = scopeSequence;
  const controller = new AbortController();
  activeController = controller;
  const inScope = () => !closed && requestScope === scopeSequence && subscriptionId === props.subscriptionId && operationKind === kind.value;
  const results = [];
  let attempted = 0;
  let unconfirmed = [];
  try {
    for (const batch of batches) {
      if (!inScope()) return;
      unconfirmed = batch;
      attempted += batch.length;
      const { data } = await props.api.post(`/saas-subscriptions/${subscriptionId}/sync-actions`,
        syncActionPayload(action, operationKind, batch), { signal: controller.signal });
      if (!inScope()) return;
      if (!Array.isArray(data.items) || data.items.length !== batch.length ||
          data.items.some(item => !['succeeded', 'failed', 'skipped'].includes(item.status))) {
        throw new Error("同步结果不完整，请刷新核实");
      }
      results.push(...data.items);
      unconfirmed = [];
    }
  } catch (cause) {
    if (inScope()) error.value = cause.response?.data?.detail || cause.message || "同步操作失败";
  } finally {
    if (inScope()) {
      const succeeded = results.filter(item => item.status === 'succeeded').length;
      const failed = results.filter(item => item.status === 'failed');
      const skipped = results.filter(item => item.status === 'skipped').length;
      const unattempted = batches.flat().slice(attempted);
      notice.value = `成功 ${succeeded} 项，失败 ${failed.length} 项，跳过 ${skipped} 项，未确认 ${unconfirmed.length} 项，未尝试 ${unattempted.length} 项`;
      error.value = [...failed.map(item => `${item.id}: ${item.detail || '同步失败'}`), error.value,
        unconfirmed.length ? `结果未确认：${unconfirmed.join('、')}` : '',
        unattempted.length ? `未尝试：${unattempted.join('、')}` : ''].filter(Boolean).join('；');
      await load(true);
      if (inScope()) { submitting.value = false; activeController = null; }
    }
  }
}

function requestRemoveAll(ids = null) {
  if (closed || loading.value || submitting.value) return;
  removalAll.value = ids === null;
  removalIds.value = selectedSyncIds(rightRows.value, kind.value, ids === null ? rightRows.value.map(row => row.id) : ids);
  if (!removalIds.value.length) return;
  if (rightRows.value.some(row => row.type === (kind.value === 'users' ? 'user' : 'department'))) removeAllPending.value = true;
}

function invalidateScope() {
  scopeSequence += 1;
  loadSequence += 1;
  activeController?.abort();
  activeController = null;
  submitting.value = false;
  loading.value = false;
  removeAllPending.value = false;
  leftSelection.value = [];
  rightSelection.value = [];
}

function close() {
  closed = true;
  invalidateScope();
  emit('close');
}

function reloadScope() {
  invalidateScope();
  error.value = '';
  notice.value = '';
  tree.value = { departments: [], unassigned_users: [], mapped_users: [], mapped_departments: [] };
  load();
}

watch(kind, reloadScope, { flush: 'sync' });
watch(() => props.initialKind, value => { kind.value = value; });
watch(() => props.subscriptionId, reloadScope, { flush: 'sync' });
onMounted(load);
onBeforeUnmount(() => { closed = true; invalidateScope(); });
</script>

<template>
  <section class="saas-sync-management" aria-label="SaaS 用户与部门同步" tabindex="0" @keydown.esc.stop="close">
    <div class="saas-sync-toolbar">
      <strong>{{ kind === 'users' ? '用户管理' : '部门管理' }}</strong>
      <div class="saas-sync-toolbar-actions"><button type="button" class="icon-btn" title="刷新同步树" aria-label="刷新同步树" :disabled="loading || submitting" @click="load"><RefreshCw :size="15" /></button><button type="button" class="icon-btn" title="关闭同步管理" aria-label="关闭同步管理" @click="close"><X :size="16" /></button></div>
    </div>
    <p v-if="error" class="danger-text" role="alert">{{ error }}</p>
    <p v-if="notice" class="muted" role="status">{{ notice }}</p>
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
        <button type="button" class="icon-btn" title="添加所选" aria-label="添加所选" :disabled="!leftSelection.length || loading || submitting" @click="move('add', leftSelection)"><ArrowRight :size="17" /></button>
        <button type="button" class="icon-btn" title="添加全部" aria-label="添加全部" :disabled="!leftRows.some(row => row.selectable) || loading || submitting" @click="move('add')"><ChevronsRight :size="17" /></button>
        <button type="button" class="icon-btn" title="移除全部" aria-label="移除全部" :disabled="!rightRows.some(row => row.selectable) || loading || submitting" @click="requestRemoveAll()"><ChevronsLeft :size="17" /></button>
        <button type="button" class="icon-btn" title="移除所选" aria-label="移除所选" :disabled="!rightSelection.length || loading || submitting" @click="requestRemoveAll(rightSelection)"><ArrowLeft :size="17" /></button>
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
        <h3 id="sync-remove-all-title">{{ removalAll ? '移除全部已同步项目？' : '移除所选已同步项目？' }}</h3>
        <p>确认移除 {{ removalIds?.length || 0 }} 项？仍被子部门、成员或管理员依赖的项目会保留并报告失败。</p>
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
