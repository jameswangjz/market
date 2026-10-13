<script setup>
import { computed, onMounted, ref } from "vue";
import { Check, Pencil, Plus, RefreshCw, Trash2, X } from "lucide-vue-next";
import axios from "axios";

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
const children = computed(() => items.value.find(item => item.id === selected.value)?.children || []);
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
  <div class="category-heading"><h2>产品目录</h2><button class="secondary-btn" :disabled="busy || saving" @click="load"><RefreshCw :size="16" />刷新</button></div>
  <p v-if="error" class="form-error" role="alert">{{ error }}</p>
  <section class="panel category-settings">
    <div class="category-parent-list">
      <div v-for="parent in items" :key="parent.id" class="category-parent" :class="{ active: selected === parent.id }">
        <template v-if="editing === parent.id"><input v-model="editName" aria-label="编辑一级目录名称" maxlength="80" @click.stop /><button class="icon-btn" title="保存" aria-label="保存" :disabled="saving" @click="saveEdit(parent)"><Check :size="15" /></button><button class="icon-btn" title="取消" aria-label="取消" :disabled="saving" @click="cancelEdit"><X :size="15" /></button></template>
        <template v-else><button class="category-name" @click="selected = parent.id">{{ parent.name }}</button><button class="icon-btn" title="编辑一级目录" aria-label="编辑一级目录" :disabled="saving" @click="beginEdit(parent)"><Pencil :size="14" /></button><button v-if="confirmDelete === parent.id" class="icon-btn confirm-delete" title="确认删除" aria-label="确认删除" :disabled="saving" @click="remove(parent.id)"><Check :size="15" /></button><button v-else class="icon-btn" title="删除一级目录" aria-label="删除一级目录" :disabled="saving" @click="confirmDelete = parent.id"><Trash2 :size="15" /></button><button v-if="confirmDelete === parent.id" class="icon-btn" title="取消删除" aria-label="取消删除" :disabled="saving" @click="confirmDelete = ''"><X :size="15" /></button></template>
      </div>
      <form class="category-add" @submit.prevent="add()"><input v-model="parentName" aria-label="一级目录名称" placeholder="新增一级目录" maxlength="80" /><button class="icon-btn" title="新增一级目录" aria-label="新增一级目录" :disabled="saving || !parentName.trim()"><Plus :size="17" /></button></form>
    </div>
    <div class="category-child-list">
      <div class="panel-heading"><h3>{{ items.find(item => item.id === selected)?.name || '二级目录' }}</h3></div>
      <div v-for="child in children" :key="child.id" class="category-child">
        <template v-if="editing === child.id"><input v-model="editName" aria-label="编辑二级目录名称" maxlength="80" /><select v-model="editDeliveryMethod" aria-label="适用交付方式"><option value="">全部交付方式</option><option value="file">文件交付</option><option value="api">API 交付</option><option value="model_api">模型 API</option><option value="tenant_access">租户访问</option><option value="training">培训</option><option value="consulting">咨询</option><option value="custom">定制开发</option></select><button class="icon-btn" title="保存" aria-label="保存" :disabled="saving" @click="saveEdit(child)"><Check :size="15" /></button><button class="icon-btn" title="取消" aria-label="取消" :disabled="saving" @click="cancelEdit"><X :size="15" /></button></template>
        <template v-else><span>{{ child.name }}</span><small>{{ child.delivery_method || '全部交付方式' }}</small><button class="icon-btn" title="编辑二级目录" aria-label="编辑二级目录" :disabled="saving" @click="beginEdit(child)"><Pencil :size="14" /></button><button v-if="confirmDelete === child.id" class="icon-btn confirm-delete" title="确认删除" aria-label="确认删除" :disabled="saving" @click="remove(child.id)"><Check :size="15" /></button><button v-else class="icon-btn" title="删除二级目录" aria-label="删除二级目录" :disabled="saving" @click="confirmDelete = child.id"><Trash2 :size="15" /></button><button v-if="confirmDelete === child.id" class="icon-btn" title="取消删除" aria-label="取消删除" :disabled="saving" @click="confirmDelete = ''"><X :size="15" /></button></template>
      </div>
      <form v-if="selected" class="category-add category-add-child" @submit.prevent="add(selected)"><input v-model="childName" aria-label="二级目录名称" placeholder="新增二级目录" maxlength="80" /><select v-model="deliveryMethod" aria-label="适用交付方式"><option value="">全部交付方式</option><option value="file">文件交付</option><option value="api">API 交付</option><option value="model_api">模型 API</option><option value="tenant_access">租户访问</option><option value="training">培训</option><option value="consulting">咨询</option><option value="custom">定制开发</option></select><button class="icon-btn" title="新增二级目录" aria-label="新增二级目录" :disabled="saving || !childName.trim()"><Plus :size="17" /></button></form>
      <p v-if="selected && !children.length" class="muted">暂无二级目录</p>
    </div>
  </section>
</template>

<style scoped>
.category-heading{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}.category-heading h2{font-size:18px;margin:0}.category-settings{display:grid;grid-template-columns:minmax(220px, .8fr) minmax(320px, 1.4fr);gap:28px}.category-parent-list,.category-child-list{min-width:0}.category-parent,.category-child{display:flex;align-items:center;gap:8px;min-height:44px;border-bottom:1px solid var(--border,#e5e7eb)}.category-parent.active{background:var(--surface-muted,#f5f7f8)}.category-parent input,.category-child input{min-width:0;flex:1}.category-name{flex:1;text-align:left;border:0;background:none;padding:10px 12px;color:inherit;cursor:pointer}.category-child span{flex:1}.category-child small{color:var(--muted,#697586)}.category-add{display:flex;gap:8px;margin-top:12px}.category-add input,.category-add select{min-width:0;flex:1}.category-add-child{max-width:620px}.category-child select{max-width:165px;min-width:0}.confirm-delete{color:#b42318}.form-error{color:#b42318}@media(max-width:700px){.category-settings{grid-template-columns:1fr}}
</style>
