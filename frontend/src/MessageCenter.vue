<script setup>
import { ref, onMounted } from 'vue';
import axios from 'axios';
import { Search, RefreshCw, X, Bell } from 'lucide-vue-next';

const emit = defineEmits(['changed']);
const data = ref({ items: [], total: 0 });
const folder = ref('inbox');
const query = ref('');
const page = ref(1);
const selected = ref([]);
const detail = ref(null);
const error = ref('');
const busy = ref(false);
const api = axios.create({ baseURL: '/api' });
api.interceptors.request.use(config => { config.headers.Authorization = `Bearer ${localStorage.getItem('market_token')}`; return config; });
const folders = [['inbox', '收件箱'], ['unread', '未读'], ['urgent', '待确认紧急消息'], ['archived', '已归档'], ['trash', '回收站']];
const date = value => value ? new Date(value).toLocaleString('zh-CN') : '-';
const severity = value => ({ normal: '普通', important: '重要', urgent: '紧急' }[value] || value);
async function load() {
  busy.value = true;
  try {
    const result = await api.get('/notifications', { params: { folder: folder.value, q: query.value, page: page.value, page_size: 20 } });
    data.value = result.data;
    selected.value = [];
    error.value = '';
    emit('changed');
  } catch (e) { error.value = e.response?.data?.detail || '消息加载失败'; }
  finally { busy.value = false; }
}
function changeFolder(value) { folder.value = value; page.value = 1; load(); }
async function open(item) {
  try {
    const result = await api.get(`/notifications/${item.id}`);
    detail.value = result.data;
    if (folder.value !== 'trash') {
      const read = await api.post(`/notifications/${item.id}/read`);
      detail.value = read.data;
      await load();
    }
  } catch (e) { error.value = e.response?.data?.detail || '消息读取失败'; }
}
async function action(name, ids) {
  try {
    await api.post('/notifications/batch-actions', { action: name, ids });
    detail.value = null;
    await load();
  } catch (e) { error.value = e.response?.data?.detail || '操作失败'; }
}
onMounted(load);
</script>

<template>
  <div class="page-heading"><div><div class="eyebrow">消息通知</div><h1>消息中心</h1></div><button class="secondary-btn" :disabled="busy" @click="load"><RefreshCw :size="15" />刷新</button></div>
  <div class="tabs"><button v-for="[key, title] in folders" :key="key" :class="{ active: folder === key }" @click="changeFolder(key)">{{ title }}</button></div>
  <form class="message-filter" @submit.prevent="page = 1; load()"><input v-model="query" placeholder="搜索标题或内容" /><button class="secondary-btn"><Search :size="15" />检索</button><span class="muted">共 {{ data.total }} 条 · 未读 {{ data.unread || 0 }} 条</span></form>
  <p v-if="error" role="alert" class="error-text">{{ error }}</p>
  <div v-if="selected.length" class="action-list message-batch"><button v-if="folder !== 'trash'" class="text-btn" @click="action('read', selected)">标记已读</button><button v-if="folder !== 'trash'" class="text-btn" @click="action('archive', selected)">归档</button><button v-if="folder !== 'trash'" class="text-btn danger-text" @click="action('delete', selected)">删除</button><button v-else class="text-btn" @click="action('restore', selected)">恢复</button></div>
  <div class="table-wrap"><table class="data-table"><thead><tr><th></th><th>等级</th><th>消息</th><th>状态</th><th>发送时间</th><th>操作</th></tr></thead><tbody><tr v-for="item in data.items" :key="item.id"><td><input v-model="selected" type="checkbox" :value="item.id" aria-label="选择消息" /></td><td>{{ severity(item.severity) }}</td><td><button class="product-link" @click="open(item)">{{ item.title }}</button><small>{{ item.content }}</small></td><td>{{ item.status === 'unread' ? '未读' : '已读' }}<small v-if="item.severity === 'urgent'">{{ item.acknowledged_at ? '已确认' : '待确认' }}</small></td><td>{{ date(item.created_at) }}</td><td><button v-if="folder === 'trash'" class="text-btn" @click="action('restore', [item.id])">恢复</button><button v-else class="text-btn danger-text" @click="action('delete', [item.id])">删除</button></td></tr></tbody></table><div v-if="!data.items.length" class="empty-state"><Bell :size="22" />暂无消息</div></div>
  <div class="audit-pagination"><button class="secondary-btn" :disabled="page === 1 || busy" @click="page--; load()">上一页</button><span class="muted">{{ page }} / {{ Math.max(1, Math.ceil(data.total / 20)) }}</span><button class="secondary-btn" :disabled="page * 20 >= data.total || busy" @click="page++; load()">下一页</button></div>
  <div v-if="detail" class="modal-scrim" @click.self="detail = null"><section class="modal-card message-detail"><div class="drawer-head"><div><span class="eyebrow">{{ severity(detail.severity) }}</span><h2>{{ detail.title }}</h2></div><button class="icon-btn" title="关闭" @click="detail = null"><X :size="18" /></button></div><p class="message-body">{{ detail.content }}</p><div class="state-grid"><div><small>发送时间</small><strong>{{ date(detail.sent_at) }}</strong></div><div><small>站内送达</small><strong>{{ date(detail.delivered_at) }}</strong></div><div><small>首次阅读</small><strong>{{ date(detail.first_read_at) }}</strong></div><div><small>最后阅读</small><strong>{{ date(detail.last_read_at) }}</strong></div><div><small>确认时间</small><strong>{{ date(detail.acknowledged_at) }}</strong></div></div><div class="modal-actions"><button v-if="detail.severity === 'urgent' && !detail.acknowledged_at && folder !== 'trash'" class="primary-btn" @click="action('acknowledge', [detail.id])">确认收到</button><button class="secondary-btn" @click="detail = null">关闭</button></div></section></div>
</template>

<style scoped>
.message-filter{display:flex;align-items:center;gap:12px;margin:18px 0}.message-filter input{border:1px solid #d9dce1;border-radius:7px;padding:9px 11px;font-size:12px}.message-batch{margin-bottom:12px}.message-detail{width:min(700px,calc(100% - 32px));max-height:90vh}.message-body{white-space:pre-wrap;font-size:13px;line-height:1.7;overflow-wrap:anywhere}.message-detail h2{font-size:18px}.audit-pagination{align-items:center}@media(max-width:600px){.message-filter{flex-wrap:wrap}}
</style>
