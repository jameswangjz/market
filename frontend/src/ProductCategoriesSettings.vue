<script setup>
import { computed, onMounted, ref } from "vue";
import { Check, Pencil, Plus, RefreshCw, Trash2, X } from "lucide-vue-next";
import axios from "axios";
import { methodLabels } from "./mainList.js";

const api = axios.create({ baseURL: "/api", headers: { Authorization: `Bearer ${localStorage.getItem("market_token") || ""}` } });
const items = ref([]);
const selected = ref("");
const parentName = ref("");
const childName = ref("");
const deliveryMethod = ref("");
const busy = ref(false);
const saving = ref(false);
const editing = ref(null);
const editName = ref("");
const editDeliveryMethod = ref("");
const confirmDelete = ref("");
const error = ref("");
const activeParent = computed(() => items.value.find(item => item.id === selected.value));
const levels = computed(() => [
  { label: "一级目录", child: false, rows: items.value },
  { label: "二级目录", child: true, rows: activeParent.value?.children || [] },
]);
const deliveryOptions = ["file", "api", "model_api", "tenant_access", "training", "consulting", "custom"];
const deliveryLabel = value => methodLabels[value] || value || "全部交付方式";
async function load() {
  if (busy.value) return;
  busy.value = true; error.value = "";
  try {
    items.value = (await api.get("/admin/product-categories")).data.items || [];
    if (!items.value.some(item => item.id === selected.value)) selected.value = items.value[0]?.id || "";
  }
  catch (e) { error.value = e.response?.data?.detail || "产品目录加载失败"; }
  finally { busy.value = false; }
}
async function add(parentId = "") {
  const name = parentId ? childName : parentName;
  if (!name.value.trim() || saving.value || busy.value) return;
  saving.value = true;
  try {
    await api.post("/admin/product-categories", { name: name.value.trim(), parent_id: parentId, delivery_method: parentId ? deliveryMethod.value : "" });
    name.value = ""; if (parentId) deliveryMethod.value = ""; await load();
  } catch (e) { error.value = e.response?.data?.detail || "目录新增失败"; }
  finally { saving.value = false; }
}
function beginEdit(item) { editing.value = item.id; editName.value = item.name; editDeliveryMethod.value = item.delivery_method || ""; confirmDelete.value = ""; }
function cancelEdit() { editing.value = null; editName.value = ""; editDeliveryMethod.value = ""; }
async function saveEdit(item) {
  if (!editName.value.trim() || saving.value || busy.value) return;
  saving.value = true; error.value = "";
  try {
    await api.put(`/admin/product-categories/${item.id}`, { name: editName.value.trim(), parent_id: item.parent_id || "", delivery_method: item.parent_id ? editDeliveryMethod.value : "" });
    cancelEdit(); await load();
  } catch (e) { error.value = e.response?.data?.detail || "目录保存失败"; }
  finally { saving.value = false; }
}
async function remove(id) {
  if (confirmDelete.value !== id || saving.value || busy.value) return;
  saving.value = true;
  try { await api.delete(`/admin/product-categories/${id}`); confirmDelete.value = ""; await load(); }
  catch (e) { error.value = e.response?.data?.detail || "目录删除失败"; }
  finally { saving.value = false; }
}
onMounted(load);
</script>

<template>
  <div class="page-heading">
    <div>
      <div class="eyebrow">平台管理</div>
      <h1>产品目录</h1>
    </div>
    <button class="secondary-btn" type="button" :disabled="busy || saving" @click="load">
      <RefreshCw :size="16" />刷新
    </button>
  </div>

  <p v-if="error" class="error-text" role="alert">{{ error }}</p>
  <div class="category-settings" :aria-busy="busy">
    <section v-for="level in levels" :key="level.label" class="panel">
      <div class="panel-heading">
        <h3>{{ level.label }}</h3>
        <span v-if="level.child && activeParent" class="muted">{{ activeParent.name }}</span>
      </div>
      <div class="table-wrap">
        <table class="data-table category-table">
          <thead>
            <tr>
              <th scope="col">目录名称</th>
              <th v-if="level.child" scope="col">交付方式</th>
              <th scope="col" class="category-action-cell">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in level.rows" :key="item.id" :class="{ selected: !level.child && selected === item.id }">
              <td>
                <input v-if="editing === item.id" v-model="editName" :aria-label="'编辑' + level.label + '名称'"
                  maxlength="80" @keydown.enter.prevent="saveEdit(item)" />
                <span v-else-if="level.child">{{ item.name }}</span>
                <button v-else type="button" class="text-btn category-link" :aria-pressed="selected === item.id"
                  @click="selected = item.id">{{ item.name }}</button>
              </td>
              <td v-if="level.child">
                <select v-if="editing === item.id" v-model="editDeliveryMethod" aria-label="适用交付方式">
                  <option value="">全部交付方式</option>
                  <option v-for="value in deliveryOptions" :key="value" :value="value">{{ deliveryLabel(value) }}</option>
                </select>
                <span v-else>{{ deliveryLabel(item.delivery_method) }}</span>
              </td>
              <td class="category-action-cell">
                <div class="action-list category-actions">
                  <template v-if="editing === item.id">
                    <button class="icon-btn" type="button" title="保存" aria-label="保存"
                      :disabled="saving || !editName.trim()" @click="saveEdit(item)"><Check :size="15" /></button>
                    <button class="icon-btn" type="button" title="取消" aria-label="取消"
                      :disabled="saving" @click="cancelEdit"><X :size="15" /></button>
                  </template>
                  <template v-else-if="confirmDelete === item.id">
                    <button class="icon-btn confirm-delete" type="button" title="确认删除" aria-label="确认删除"
                      :disabled="saving" @click="remove(item.id)"><Check :size="15" /></button>
                    <button class="icon-btn" type="button" title="取消删除" aria-label="取消删除"
                      :disabled="saving" @click="confirmDelete = ''"><X :size="15" /></button>
                  </template>
                  <template v-else>
                    <button class="icon-btn" type="button" :title="'编辑' + level.label" :aria-label="'编辑' + level.label"
                      :disabled="saving" @click="beginEdit(item)"><Pencil :size="14" /></button>
                    <button class="icon-btn" type="button" :title="'删除' + level.label" :aria-label="'删除' + level.label"
                      :disabled="saving" @click="confirmDelete = item.id"><Trash2 :size="15" /></button>
                  </template>
                </div>
              </td>
            </tr>
            <tr v-if="!level.rows.length">
              <td :colspan="level.child ? 3 : 2" class="muted">
                {{ busy ? "正在加载…" : level.child && !activeParent ? "请先选择一级目录" : "暂无" + level.label }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <form v-if="!level.child || activeParent" class="category-add" @submit.prevent="add(level.child ? selected : '')">
        <input v-if="level.child" v-model="childName" aria-label="二级目录名称" placeholder="新增二级目录" maxlength="80" />
        <input v-else v-model="parentName" aria-label="一级目录名称" placeholder="新增一级目录" maxlength="80" />
        <select v-if="level.child" v-model="deliveryMethod" aria-label="适用交付方式">
          <option value="">全部交付方式</option>
          <option v-for="value in deliveryOptions" :key="value" :value="value">{{ deliveryLabel(value) }}</option>
        </select>
        <button class="secondary-btn" type="submit"
          :disabled="busy || saving || !(level.child ? childName : parentName).trim()"><Plus :size="15" />新增</button>
      </form>
    </section>
  </div>
</template>

<style scoped>
.category-settings { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 3fr); gap: 16px; align-items: start; }
.category-settings .panel { min-width: 0; }
.category-table { min-width: 0; }
.category-table :is(th, td) { padding: 8px 12px; }
.category-table td:first-child { overflow-wrap: anywhere; }
.category-table .selected td { background: #fff7f0; }
.category-action-cell { width: 1%; text-align: right; }
.category-actions { flex-wrap: nowrap; justify-content: flex-end; gap: 3px; }
.category-link { padding: 0; text-align: left; overflow-wrap: anywhere; }
.category-add { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
.category-add :is(input, select) { flex: 1 1 130px; }
.category-settings :is(input, select) { width: 100%; min-width: 90px; padding: 7px 9px; border: 1px solid #e5e6eb; border-radius: 6px; background: #fff; color: #1d2129; font-size: 12px; }
.confirm-delete { color: #b42318; }
@media (max-width: 1000px) { .category-settings { grid-template-columns: minmax(0, 1fr); } }
</style>
