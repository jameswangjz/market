<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue';
import { Save, X } from 'lucide-vue-next';
import MainListFilters from './MainListFilters.vue';
import PaginationControls from './PaginationControls.vue';
import { listState, applyListFilters, changeListPage, fetchListPage } from './mainList.js';
import { bindingPayload } from './slaManagement.js';
const props = defineProps({ api: Object, user: Object, levels: { type: Array, default: () => [] }, assignments: { type: Array, default: () => [] } });
const emit = defineEmits(['changed']);
const state = reactive(listState({ q: '', service_level_id: '', assigned_by: '', expires_after: '', expires_before: '' }));
const items = ref([]), editing = ref(null), saving = ref(false), error = ref(''), notice = ref('');
const form = reactive({ enterprise_id: '', service_level_id: '', expires_at: '', reason: '' });
const allowed = computed(() => ['super_admin', 'platform_operator'].includes(props.user?.platform_role));
const fields = computed(() => [{ key: 'q', label: '企业名称' }, { key: 'service_level_id', label: '当前级别', options: props.levels.map(level => ({ value: level.id, label: level.name })) }, { key: 'assigned_by', label: '操作人' }, { key: 'expires_after', label: '到期开始', type: 'datetime-local' }, { key: 'expires_before', label: '到期结束', type: 'datetime-local' }]);
let disposed = false;
async function load() {
  items.value = []; error.value = '';
  const applied = { ...state.applied };
  try {
    for (const key of ['expires_after', 'expires_before']) if (applied[key]) applied[key] = new Date(applied[key]).toISOString();
    if (applied.expires_after && applied.expires_before && applied.expires_after > applied.expires_before) throw new Error('到期结束时间不能早于开始时间');
  } catch { state.error = '请检查到期时间范围'; return; }
  state.applied = applied;
  await fetchListPage(props.api, '/sla/enterprise-bindings', state, {}, () => !disposed, (rows, data) => { items.value = rows; if (data?.page) state.page = data.page; });
}
function search(reset = false) { applyListFilters(state, reset); load(); }
function page(value, size) { if (changeListPage(state, value, size)) load(); }
function edit(item) {
  editing.value = item; error.value = ''; notice.value = '';
  const expiry = item.expires_at ? new Date(item.expires_at) : null;
  Object.assign(form, { enterprise_id: item.enterprise_id, service_level_id: item.service_level_id || props.levels.find(level => level.code === 'standard')?.id || '', expires_at: expiry ? new Date(expiry.getTime() - expiry.getTimezoneOffset() * 60000).toISOString().slice(0, 16) : '', reason: '' });
}
async function save() {
  if (!allowed.value || saving.value) return;
  let payload;
  try { payload = bindingPayload(form); } catch (failure) { error.value = failure.message; return; }
  saving.value = true; error.value = '';
  try {
    await props.api.post('/sla/service-level-assignments', payload);
    if (disposed) return;
    editing.value = null; notice.value = '企业级别已更新，仅影响新冻结承诺'; await load(); emit('changed');
  } catch (failure) { if (!disposed) error.value = failure.response?.data?.detail || '绑定失败，输入已保留'; }
  finally { if (!disposed) saving.value = false; }
}
watch(() => props.user?.id, load, { immediate: true });
onBeforeUnmount(() => { disposed = true; state.generation++; });
const date = value => value ? new Date(value).toLocaleString() : '-';
</script>
<template>
  <section><h3>企业级别绑定</h3><p class="muted">未绑定及到期企业当前默认标准级；到期恢复仅影响新冻结承诺，已有订单和工单保持原承诺。</p>
    <MainListFilters :state="state" :fields="fields" @search="search()" @reset="search(true)" />
    <p v-if="error" class="error-text" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
    <form v-if="editing" class="sla-binding-form" @submit.prevent="save"><div class="sla-toolbar"><h4>{{ editing.enterprise_name }} · 修改级别</h4><button type="button" class="icon-btn" title="取消修改" aria-label="取消修改" :disabled="saving" @click="editing = null"><X :size="16" /></button></div><div class="form-grid"><label>服务级别<select v-model="form.service_level_id" :disabled="saving" required><option value="">请选择</option><option v-for="level in levels.filter(item => item.status === 'active' || item.id === form.service_level_id)" :key="level.id" :value="level.id">{{ level.name }}</option></select></label><label>到期时间<input v-model="form.expires_at" type="datetime-local" :disabled="saving" /></label><label class="wide">调整原因<textarea v-model="form.reason" rows="2" maxlength="2000" :disabled="saving" required /></label></div><button class="primary-btn" :disabled="saving || !form.reason.trim()"><Save :size="15" />{{ saving ? '保存中' : '保存绑定' }}</button></form>
    <div class="table-wrap" :aria-busy="state.busy"><table class="data-table compact"><thead><tr><th>企业</th><th>当前级别</th><th>生效时间</th><th>到期时间</th><th>操作人</th><th>来源</th><th>操作</th></tr></thead><tbody><tr v-for="item in items" :key="item.enterprise_id"><td>{{ item.enterprise_name }}</td><td>{{ item.service_level?.name || '标准级' }}</td><td>{{ date(item.effective_at) }}</td><td>{{ item.expires_at ? date(item.expires_at) : '长期' }}</td><td>{{ item.assigned_by || '-' }}</td><td>{{ { default: '默认标准', manual: '手动调整', verification: '企业认证' }[item.source] || item.source }}</td><td><button v-if="allowed" class="text-btn" :disabled="state.busy || saving || item.verification_status !== 'verified'" @click="edit(item)">修改</button></td></tr><tr v-if="!items.length"><td colspan="7" class="empty-state">{{ state.busy ? '读取中' : '暂无匹配企业' }}</td></tr></tbody></table></div>
    <PaginationControls :page="state.page" :page-size="state.page_size" :total="state.total" :busy="state.busy || saving" label="企业级别绑定分页" @page="page($event)" @size="page(1, $event)" />
    <h4>企业等级生效记录</h4><div class="table-wrap"><table class="data-table compact"><thead><tr><th>企业</th><th>级别</th><th>生效时间</th><th>到期时间</th><th>操作人 / 来源</th><th>原因</th></tr></thead><tbody><tr v-for="item in assignments" :key="item.id"><td>{{ item.enterprise_name || item.enterprise_id }}</td><td>{{ item.service_level?.name || '-' }}</td><td>{{ date(item.effective_at) }}</td><td>{{ date(item.expires_at) }}</td><td>{{ item.created_by || '-' }} / {{ item.source || '-' }}</td><td>{{ item.reason || '-' }}</td></tr><tr v-if="!assignments.length"><td colspan="6">暂无等级生效记录</td></tr></tbody></table></div>
  </section>
</template>
