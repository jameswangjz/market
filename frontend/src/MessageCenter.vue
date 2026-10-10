<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue';
import axios from 'axios';
import { Search, RefreshCw, X, Bell, Send, Save, Settings, Paperclip, Download, Eye, ExternalLink, CheckCircle2 } from 'lucide-vue-next';
import { useNotificationStream } from './useNotificationStream';

const emit = defineEmits(['changed', 'navigate']);
const props = defineProps({ allowedViews: { type: Array, default: null }, initialId: { type: String, default: '' } });
const api = axios.create({ baseURL: '/api' });
api.interceptors.request.use(config => {
  const token = localStorage.getItem('market_token');
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});
const data = ref({ items: [], total: 0 });
const folder = ref('inbox');
const page = ref(1);
const selected = ref([]);
const detail = ref(null);
const error = ref('');
const notice = ref('');
const busy = ref(false);
const saving = ref(false);
const uploading = ref(false);
const downloading = ref(false);
const filters = ref({ q: '', category: '', severity: '', read: '', start: '', end: '' });
const context = ref({ platform_admin: false, enterprises: [], users: [], settings: {} });
const contextReady = ref(false);
const canSend = computed(() => contextReady.value && !!context.value.users?.length && (context.value.platform_admin || !!context.value.enterprises?.length));
const preferences = ref({ email_enabled: false });
const preferencesReady = ref(false);
const settings = ref({ retention_days: 180, attachment_max_mb: 20, email_enabled: false });
const settingsReady = ref(false);
const settingsOpen = ref(false);
const composer = ref(null);
const preview = ref(null);
const confirmation = ref(null);
const currentUser = ref(null);
const deliveryHealth = ref({ counts: [], failures: [], expired_urgent: [] });
const resolutionReasons = ref({});
const healthBusy = ref(false);
const availableUsers = computed(() => (context.value.users || []).filter(user => !composer.value?.tenant_id || user.enterprise_ids?.includes(composer.value.tenant_id)));
const platformRoles = computed(() => [...new Set(availableUsers.value.map(user => user.platform_role).filter(Boolean))]);
const platformRoleName = role => ({ super_admin: '平台超级管理员', platform_operator: '平台运营', product_manager: '产品经理', business_reviewer: '业务审核', quality_reviewer: '质量审核', security_compliance: '安全合规' }[role] || '其他平台角色');
const pollMs = computed(() => Number(settings.value.poll_interval_seconds || 30) * 1000);
const sentFolder = computed(() => ['sent', 'drafts'].includes(folder.value));
const folders = [['inbox', '收件箱'], ['unread', '未读'], ['urgent', '待确认紧急消息'], ['archived', '已归档'], ['trash', '回收站'], ['drafts', '草稿'], ['sent', '发件箱']];
const categories = [['announcement', '公告'], ['review', '审核'], ['order', '订单'], ['delivery', '交付'], ['settlement', '清算'], ['security', '安全'], ['sla', '服务考核'], ['identity', '身份'], ['system', '系统']];
const date = value => value ? new Date(value).toLocaleString('zh-CN') : '-';
const severity = value => ({ normal: '普通', important: '重要', urgent: '紧急' }[value] || '普通');
const categoryName = value => categories.find(([key]) => key === value)?.[1] || '其他';
const emailState = value => ({ pending: '待发送', processing: '发送中', queued: '排队中', accepted: '已接受', sent: '已发送', succeeded: '已送达', delivered: '已送达', failed: '发送失败', disabled: '未启用', skipped: '未发送', cancelled: '已取消' }[value] || '暂无状态');
const channelName = value => ({ email: '邮件', realtime: '实时通知' }[value] || '通知');
const userName = user => user.name && user.name !== user.id ? user.name : user.email || '用户';
const deliveryFailure = result => ({ SMTPAuthenticationError: '邮件认证失败', SMTPRecipientsRefused: '收件地址被拒绝', SMTPConnectError: '邮件服务连接失败', SMTPServerDisconnected: '邮件服务连接中断', TimeoutError: '投递超时', ConnectionError: '通知服务连接失败' }[result] || '投递失败');
const targetViews = { product: 'products', order: 'orders', refund: 'orders', payment: 'orders', delivery_task: 'delivery', saas_subscription: 'delivery', settlement: 'settlements', settlement_batch: 'settlements', settlement_correction: 'settlements', reconciliation: 'settlements', sla_result: 'sla', enterprise: 'users', user: 'users', membership: 'users', identity_verification: 'users' };
function businessView(item) {
  const view = targetViews[item.target_type];
  if (!view) return null;
  if (props.allowedViews) return props.allowedViews.includes(view) ? view : null;
  // Match App's visibleNav rule when the parent has not supplied its menu list.
  return currentUser.value && (currentUser.value.verified_status === 'verified' || ['products', 'users', 'messages'].includes(view)) ? view : null;
}
async function loadAccess() {
  try { currentUser.value = (await api.get('/auth/me')).data.user; }
  catch { currentUser.value = null; }
}
function dateBoundary(value, exclusive = false) {
  if (!value) return '';
  const timestamp = new Date(`${value}T00:00:00`);
  if (exclusive) timestamp.setDate(timestamp.getDate() + 1);
  return timestamp.toISOString();
}
const isDraft = item => item.status === 'draft' || item.draft === true;
const isRecalled = item => item.status === 'recalled' || !!item.recalled_at;
const failure = (e, fallback) => { error.value = typeof e.response?.data?.detail === 'string' ? e.response.data.detail : fallback; };
let loadVersion = 0;
let detailVersion = 0;
let disposed = false;
async function load(background = false) {
  const version = ++loadVersion;
  const currentFolder = folder.value;
  if (!background) busy.value = true;
  try {
    if (filters.value.start && filters.value.end && filters.value.start > filters.value.end) throw new Error('date');
    const snapshot = { ...filters.value };
    const params = { ...snapshot, start: dateBoundary(snapshot.start), end: dateBoundary(snapshot.end, true), folder: currentFolder, page: page.value, page_size: 20 };
    const result = await api.get(sentFolder.value ? '/notifications/sent' : '/notifications', { params });
    if (version !== loadVersion || disposed) return;
    const resultData = Array.isArray(result.data) ? { items: result.data, total: result.data.length } : result.data;
    data.value = { ...resultData, items: resultData.items || [], total: resultData.total || 0 };
    if (!background) selected.value = [];
    else selected.value = selected.value.filter(id => data.value.items.some(item => item.id === id));
    if (!background) error.value = '';
    emit('changed');
  } catch (e) {
    if (version === loadVersion && !disposed && !background) failure(e, e.message === 'date' ? '结束日期不能早于开始日期' : '消息加载失败');
  } finally { if (version === loadVersion) busy.value = false; }
}
function changeFolder(value) {
  folder.value = value; page.value = 1; selected.value = []; closeDetail();
  data.value = { items: [], total: 0 }; filters.value.read = ''; load();
}
function closeDetail() { ++detailVersion; detail.value = null; }
async function open(item) {
  const version = ++detailVersion;
  if (sentFolder.value) { detail.value = { ...item, outgoing: true }; return; }
  try {
    const result = await api.get(`/notifications/${encodeURIComponent(item.id)}`);
    if (version !== detailVersion || disposed) return;
    detail.value = { ...result.data, outgoing: false };
    if (folder.value !== 'trash') {
      const read = await api.post(`/notifications/${encodeURIComponent(item.id)}/read`);
      if (version === detailVersion && !disposed) detail.value = { ...detail.value, ...read.data };
      await load(true);
    }
  } catch (e) { if (version === detailVersion) failure(e, '消息读取失败'); }
}
async function action(name, ids) {
  if (saving.value) return;
  saving.value = true; error.value = '';
  try {
    await api.post('/notifications/batch-actions', { action: name, ids: [...ids] });
    closeDetail(); await load();
  } catch (e) { failure(e, '操作失败'); }
  finally { saving.value = false; }
}
async function readAll() {
  if (saving.value || !['inbox', 'unread', 'urgent'].includes(folder.value) || !data.value.unread) return;
  const cutoff = data.value.server_time;
  if (!cutoff) { error.value = '请刷新消息后重试'; return; }
  saving.value = true; error.value = '';
  try {
    await api.post('/notifications/read-all', { cutoff });
    notice.value = '消息已标记为已读';
    await load();
  } catch (e) { failure(e, '标记全部已读失败'); }
  finally { saving.value = false; }
}
async function loadContext() {
  try {
    const result = await api.get('/notifications/sender-context');
    context.value = { ...context.value, ...result.data }; contextReady.value = true;
    if (context.value.settings) settings.value = { ...settings.value, ...context.value.settings };
  } catch (e) { failure(e, '发送权限信息加载失败'); }
}
async function loadPreferences() {
  try {
    const result = await api.get('/notifications/preferences');
    preferences.value = { email_enabled: !!result.data.email_enabled }; preferencesReady.value = true;
  } catch (e) { failure(e, '邮件偏好加载失败'); }
}
async function openSettings() {
  settingsOpen.value = true; notice.value = ''; error.value = '';
  await loadPreferences();
  if (!contextReady.value) await loadContext();
  if (context.value.platform_admin) {
    settingsReady.value = false;
    try {
      const result = await api.get('/notifications/settings');
      settings.value = { ...settings.value, ...result.data }; settingsReady.value = true;
    } catch (e) { failure(e, '消息设置加载失败'); }
    await loadHealth();
  }
}
async function loadHealth() {
  if (!context.value.platform_admin || healthBusy.value) return;
  healthBusy.value = true;
  try { deliveryHealth.value = { counts: [], failures: [], expired_urgent: [], ...(await api.get('/notifications/delivery-health')).data }; }
  catch (e) { failure(e, '投递状态加载失败'); }
  finally { healthBusy.value = false; }
}
async function retryDelivery(item) {
  if (saving.value) return;
  saving.value = true; error.value = '';
  try {
    await api.post(`/notifications/deliveries/${encodeURIComponent(item.id)}/retry`);
    notice.value = '失败任务已重新排队'; await loadHealth();
  } catch (e) { failure(e, '任务重试失败'); }
  finally { saving.value = false; }
}
async function resolveExpired(item) {
  if (saving.value || !context.value.platform_admin) return;
  const reason = resolutionReasons.value[item.id]?.trim();
  if (!reason) { error.value = '请填写运营处理说明'; return; }
  saving.value = true; error.value = '';
  try {
    await api.post(`/notifications/expired-urgent/${encodeURIComponent(item.id)}/resolve`, { reason });
    delete resolutionReasons.value[item.id];
    notice.value = '运营处理已记录'; await loadHealth();
  } catch (e) { failure(e, '运营处理记录失败'); }
  finally { saving.value = false; }
}
async function saveConfig(platform) {
  if (saving.value) return;
  saving.value = true; error.value = '';
  try {
    const payload = platform ? { ...settings.value, retention_days: Number(settings.value.retention_days), attachment_max_mb: Number(settings.value.attachment_max_mb) } : { email_enabled: preferences.value.email_enabled };
    if (platform && (!Number.isInteger(payload.retention_days) || payload.retention_days < 1 || payload.retention_days > 3650 || !Number.isInteger(payload.attachment_max_mb) || payload.attachment_max_mb < 1 || payload.attachment_max_mb > 100)) throw new Error('validation');
    await api.put(platform ? '/notifications/settings' : '/notifications/preferences', payload);
    notice.value = '设置已保存';
  } catch (e) { failure(e, e.message === 'validation' ? '请输入有效的保留天数和附件大小' : '设置保存失败'); }
  finally { saving.value = false; }
}
function compose() {
  if (!canSend.value) return;
  composer.value = { title: '', content: '', severity: 'normal', category: 'announcement', tenant_id: context.value.platform_admin ? '' : context.value.enterprises[0]?.id || '', recipient_mode: '', recipient_ids: [], attachments: [], event_key: globalThis.crypto?.randomUUID?.() || `manual-${Date.now()}-${Math.random().toString(36).slice(2)}` };
  notice.value = ''; error.value = '';
  if (!contextReady.value) loadContext();
}
function editDraft(item) {
  if (!canSend.value) return;
  compose();
  composer.value = { ...composer.value, id: item.id, title: item.title, content: item.content, severity: item.severity, category: item.category, tenant_id: item.tenant_id || '', recipient_ids: [...(item.recipient_ids || [])], attachments: [...(item.attachments || [])] };
  closeDetail();
}
function changeEnterprise() {
  if (composer.value.recipient_mode) { selectRecipients(); return; }
  const allowed = new Set(availableUsers.value.map(user => user.id));
  composer.value.recipient_ids = composer.value.recipient_ids.filter(id => allowed.has(id));
}
function selectRecipients() {
  const form = composer.value;
  if (!form || !context.value.platform_admin || !form.recipient_mode) return;
  const users = form.recipient_mode === 'all' ? availableUsers.value : availableUsers.value.filter(user => `role:${user.platform_role}` === form.recipient_mode);
  form.recipient_ids = [...new Set(users.map(user => user.id))];
}
async function upload(event) {
  const files = Array.from(event.target.files || []);
  event.target.value = '';
  const draft = composer.value;
  if (!draft || uploading.value) return;
  uploading.value = true; error.value = '';
  try {
    if (draft.attachments.length + files.length > 5) throw new Error('count');
    for (const file of files) {
      if (file.size > Number(settings.value.attachment_max_mb) * 1024 * 1024) throw new Error('size');
      const body = new FormData(); body.append('upload', file);
      const result = await api.post('/notifications/attachments', body);
      if (composer.value === draft) draft.attachments.push(result.data);
    }
  } catch (e) { failure(e, e.message === 'size' ? `附件不能超过 ${settings.value.attachment_max_mb} MB` : e.message === 'count' ? '最多添加 5 个附件' : '附件上传失败'); }
  finally { uploading.value = false; }
}
async function send(draft) {
  if (saving.value || uploading.value || !composer.value) return;
  const form = composer.value;
  if (form.attachments.length > 5) { error.value = '最多添加 5 个附件'; return; }
  if (!form.title.trim() || !form.content.trim() || (!draft && !form.recipient_ids.length)) {
    error.value = draft ? '请填写标题和内容' : '请填写标题、内容并选择收件人'; return;
  }
  if (!context.value.platform_admin && !form.tenant_id) { error.value = '请选择所属企业'; return; }
  if (form.recipient_ids.length > 500) { error.value = '最多选择 500 位收件人'; return; }
  saving.value = true; error.value = '';
  try {
    const payload = { title: form.title.trim(), content: form.content, severity: form.severity, category: form.category, tenant_id: form.tenant_id, recipient_ids: [...form.recipient_ids], attachment_ids: form.attachments.map(item => item.id), draft: form.id ? true : draft, event_key: form.event_key };
    if (form.id) {
      await api.put(`/notifications/sent/${encodeURIComponent(form.id)}`, payload);
      if (!draft) await api.post(`/notifications/sent/${encodeURIComponent(form.id)}/publish`);
    } else await api.post('/notifications/send', payload);
    composer.value = null; notice.value = draft ? '草稿已保存' : '消息已发送'; changeFolder(draft ? 'drafts' : 'sent');
  } catch (e) { failure(e, '消息保存失败'); }
  finally { saving.value = false; }
}
async function sentAction() {
  if (saving.value || !confirmation.value) return;
  const { item, action: command, reason } = confirmation.value;
  if (command === 'recall' && !reason?.trim()) { error.value = '请填写撤回原因'; return; }
  saving.value = true; error.value = '';
  try {
    await api.post(`/notifications/sent/${encodeURIComponent(item.id)}/${command}`, command === 'recall' ? { reason: reason.trim() } : undefined);
    confirmation.value = null; closeDetail(); notice.value = command === 'publish' ? '草稿已发送' : '消息已撤回'; await load();
  } catch (e) { failure(e, '操作失败'); }
  finally { saving.value = false; }
}
function closePreview() {
  if (preview.value?.url) URL.revokeObjectURL(preview.value.url);
  preview.value = null;
}
async function attachment(item, view = false) {
  if (downloading.value) return;
  downloading.value = true;
  try {
    const result = await api.get(`/notifications/attachments/${encodeURIComponent(item.id)}/download`, { responseType: 'blob' });
    if (disposed) return;
    let blob = result.data;
    const bytes = new Uint8Array(await blob.slice(0, 16).arrayBuffer());
    const signature = String.fromCharCode(...bytes);
    const type = bytes[0] === 137 && signature.slice(1, 4) === 'PNG' ? 'image/png'
      : bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255 ? 'image/jpeg'
      : /^GIF8[79]a/.test(signature) ? 'image/gif'
      : signature.startsWith('RIFF') && signature.slice(8, 12) === 'WEBP' ? 'image/webp'
      : signature.startsWith('%PDF-') ? 'application/pdf'
      : /\.(txt|log|csv)$/i.test(item.name) ? 'text/plain' : '';
    if (type) blob = new Blob([blob], { type });
    const kind = type.startsWith('image/') ? 'image' : type === 'application/pdf' ? 'pdf' : type === 'text/plain' ? 'text' : '';
    if (view) {
      closePreview();
      const text = kind === 'text' ? await blob.slice(0, 1024 * 1024).text() : '';
      if (!disposed) preview.value = { item, kind, url: kind && kind !== 'text' ? URL.createObjectURL(blob) : '', text };
    } else {
      const url = URL.createObjectURL(blob); const link = document.createElement('a');
      link.href = url; link.download = item.name || '附件'; document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  } catch (e) { failure(e, '附件读取失败'); }
  finally { downloading.value = false; }
}
async function navigate(item) {
  if (!props.allowedViews) await loadAccess();
  if (disposed) return;
  const view = businessView(item);
  if (!view) return;
  emit('navigate', view, { target_type: item.target_type, target_id: item.target_id });
  closeDetail();
}
const { connected } = useNotificationStream(() => load(true), { pollMs, onAuthExpired: () => { error.value = '登录已过期，请重新登录'; } });
onMounted(() => {
  loadContext();
  if (!props.allowedViews) loadAccess();
  watch(() => props.initialId, id => {
    if (!id || disposed || detail.value?.id === id) return;
    folder.value = 'inbox'; page.value = 1; selected.value = []; filters.value.read = '';
    load();
    open({ id });
  }, { immediate: true });
  if (!props.initialId) load();
});
onUnmounted(() => { disposed = true; ++loadVersion; ++detailVersion; closePreview(); });
</script>

<template>
  <div class="message-center">
    <div class="page-heading"><div><div class="eyebrow">消息通知</div><h1>消息中心</h1></div><div class="action-list"><span class="muted">{{ connected ? '实时更新' : '定时更新' }}</span><button class="icon-btn" title="消息设置" aria-label="消息设置" @click="openSettings"><Settings :size="18" /></button><button v-if="['inbox', 'unread', 'urgent'].includes(folder) && data.unread > 0" class="secondary-btn" :disabled="saving || busy || !data.server_time" @click="readAll"><CheckCircle2 :size="15" />全部已读</button><button class="secondary-btn" :disabled="busy" @click="load()"><RefreshCw :size="15" />刷新</button><button v-if="canSend" class="primary-btn" @click="compose"><Send :size="15" />写消息</button></div></div>
    <div class="tabs message-tabs"><button v-for="[key, title] in folders" :key="key" :class="{ active: folder === key }" @click="changeFolder(key)">{{ title }}</button></div>
    <form class="message-filter" @submit.prevent="page = 1; load()">
      <label>搜索<input v-model="filters.q" placeholder="搜索标题或内容" /></label>
      <label>分类<select v-model="filters.category"><option value="">全部分类</option><option v-for="[key, name] in categories" :key="key" :value="key">{{ name }}</option></select></label>
      <label>等级<select v-model="filters.severity"><option value="">全部等级</option><option value="normal">普通</option><option value="important">重要</option><option value="urgent">紧急</option></select></label>
      <label v-if="!sentFolder">阅读状态<select v-model="filters.read" :disabled="folder === 'unread'"><option value="">全部状态</option><option value="unread">未读</option><option value="read">已读</option></select></label>
      <label>开始日期<input v-model="filters.start" type="date" :max="filters.end || undefined" /></label><label>结束日期<input v-model="filters.end" type="date" :min="filters.start || undefined" /></label>
      <button class="secondary-btn" :disabled="busy"><Search :size="15" />检索</button><span class="muted">共 {{ data.total }} 条<span v-if="!sentFolder"> · 未读 {{ data.unread || 0 }} 条</span></span>
    </form>
    <p v-if="error" role="alert" class="error-text">{{ error }}</p><p v-if="notice" role="status" class="muted">{{ notice }}</p>
    <div v-if="selected.length && !sentFolder" class="action-list message-batch"><button v-if="folder !== 'trash'" class="text-btn" :disabled="saving" @click="action('read', selected)">标记已读</button><button v-if="folder !== 'trash'" class="text-btn" :disabled="saving" @click="action('archive', selected)">归档</button><button v-if="folder !== 'trash'" class="text-btn danger-text" :disabled="saving" @click="action('delete', selected)">删除</button><button v-else class="text-btn" :disabled="saving" @click="action('restore', selected)">恢复</button></div>
    <div class="table-wrap" :aria-busy="busy"><table class="data-table"><thead><tr><th v-if="!sentFolder"></th><th>等级 / 分类</th><th>消息</th><th>状态</th><th>发送时间</th><th>操作</th></tr></thead><tbody><tr v-for="item in data.items" :key="item.id"><td v-if="!sentFolder"><input v-model="selected" type="checkbox" :value="item.id" :aria-label="`选择消息：${item.title}`" /></td><td>{{ severity(item.severity) }}<small>{{ categoryName(item.category) }}</small></td><td class="message-title"><button class="product-link" @click="open(item)">{{ item.title }}</button><small class="message-excerpt">{{ item.content }}</small></td><td>{{ sentFolder ? (isDraft(item) ? '草稿' : isRecalled(item) ? '已撤回' : '已发送') : item.status === 'unread' ? '未读' : '已读' }}<small v-if="!sentFolder && item.severity === 'urgent'">{{ item.acknowledged_at ? '已确认' : '待确认' }}</small><small v-if="item.email_status">邮件：{{ emailState(item.email_status) }}</small></td><td>{{ date(item.sent_at || item.created_at) }}</td><td><template v-if="sentFolder"><button v-if="canSend && isDraft(item)" class="text-btn" :disabled="saving" @click="editDraft(item)">编辑</button><button v-if="canSend && isDraft(item)" class="text-btn" :disabled="saving" @click="confirmation = { item, action: 'publish' }">发送</button><button v-else-if="!isDraft(item) && !isRecalled(item)" class="text-btn danger-text" :disabled="saving" @click="confirmation = { item, action: 'recall', reason: '' }">撤回</button></template><button v-else-if="folder === 'trash'" class="text-btn" :disabled="saving" @click="action('restore', [item.id])">恢复</button><button v-else class="text-btn danger-text" :disabled="saving" @click="action('delete', [item.id])">删除</button></td></tr></tbody></table><div v-if="!data.items.length" class="empty-state"><Bell :size="22" />{{ busy ? '加载中' : '暂无消息' }}</div></div>
    <div class="audit-pagination"><button class="secondary-btn" :disabled="page === 1 || busy" @click="page--; load()">上一页</button><span class="muted">{{ page }} / {{ Math.max(1, Math.ceil(data.total / 20)) }}</span><button class="secondary-btn" :disabled="page * 20 >= data.total || busy" @click="page++; load()">下一页</button></div>

    <div v-if="detail" class="modal-scrim" @click.self="closeDetail"><section class="modal-card message-detail" role="dialog" aria-modal="true" aria-label="消息详情"><div class="drawer-head"><div><span class="eyebrow">{{ severity(detail.severity) }} · {{ categoryName(detail.category) }}</span><h2>{{ detail.title }}</h2></div><button class="icon-btn" title="关闭" aria-label="关闭" @click="closeDetail"><X :size="18" /></button></div><p class="message-body">{{ detail.content }}</p><div class="state-grid"><div><small>发送时间</small><strong>{{ date(detail.sent_at || detail.created_at) }}</strong></div><div><small>邮件状态</small><strong>{{ emailState(detail.email_status) }}</strong></div><template v-if="!detail.outgoing"><div><small>站内送达</small><strong>{{ date(detail.delivered_at) }}</strong></div><div><small>首次阅读</small><strong>{{ date(detail.first_read_at) }}</strong></div><div><small>最后阅读</small><strong>{{ date(detail.last_read_at) }}</strong></div><div><small>确认时间</small><strong>{{ date(detail.acknowledged_at) }}</strong></div></template><div v-if="detail.recalled_at"><small>撤回时间</small><strong>{{ date(detail.recalled_at) }}</strong></div></div>
      <ul v-if="detail.attachments?.length" class="attachments"><li v-for="item in detail.attachments" :key="item.id"><Paperclip :size="15" /><span>{{ item.name }}</span><button class="icon-btn" title="查看附件" aria-label="查看附件" :disabled="downloading" @click="attachment(item, true)"><Eye :size="16" /></button><button class="icon-btn" title="下载附件" aria-label="下载附件" :disabled="downloading" @click="attachment(item)"><Download :size="16" /></button></li></ul>
      <ul v-if="detail.delivery?.length" class="attachments"><li v-for="(item, index) in detail.delivery" :key="index"><span>{{ channelName(item.channel) }} · {{ emailState(item.status) }}</span><strong>{{ item.count }} 条</strong></li></ul><div v-if="detail.outgoing" class="muted">收件人 {{ detail.recipients || detail.recipient_ids?.length || 0 }} 位</div><div class="modal-actions"><button v-if="canSend && detail.outgoing && isDraft(detail)" class="secondary-btn" @click="editDraft(detail)"><Save :size="15" />编辑草稿</button><button v-if="businessView(detail) && detail.target_id" class="secondary-btn" @click="navigate(detail)"><ExternalLink :size="15" />查看业务</button><button v-if="!detail.outgoing && detail.severity === 'urgent' && !detail.acknowledged_at && folder !== 'trash'" class="primary-btn" :disabled="saving" @click="action('acknowledge', [detail.id])">确认收到</button><button v-if="!detail.outgoing && folder !== 'trash'" class="secondary-btn" :disabled="saving" @click="action('archive', [detail.id])">归档</button><button class="secondary-btn" @click="closeDetail">关闭</button></div><p v-if="error" class="error-text" role="alert">{{ error }}</p></section></div>

    <div v-if="composer" class="modal-scrim" @click.self="!saving && !uploading && (composer = null)"><section class="modal-card message-detail" role="dialog" aria-modal="true" aria-label="写消息"><div class="drawer-head"><h2>{{ composer.id ? '编辑草稿' : '写消息' }}</h2><button class="icon-btn" title="关闭" aria-label="关闭" :disabled="saving || uploading" @click="composer = null"><X :size="18" /></button></div><fieldset :disabled="saving"><label>标题<input v-model="composer.title" maxlength="220" /></label><div class="form-grid"><label>等级<select v-model="composer.severity"><option value="normal">普通</option><option value="important">重要</option><option value="urgent">紧急</option></select></label><label>分类<select v-model="composer.category"><option v-for="[key, name] in categories" :key="key" :value="key">{{ name }}</option></select></label></div><label>所属企业<select v-model="composer.tenant_id" :disabled="!contextReady" @change="changeEnterprise"><option v-if="context.platform_admin" value="">不指定企业</option><option v-else value="" disabled>请选择企业</option><option v-for="item in context.enterprises" :key="item.id" :value="item.id">{{ item.name }}</option></select></label><label v-if="context.platform_admin">快速选择收件人<select v-model="composer.recipient_mode" @change="selectRecipients"><option value="">手动选择</option><option value="all">全部可用收件人</option><option v-for="role in platformRoles" :key="role" :value="`role:${role}`">{{ platformRoleName(role) }}</option></select></label><label>收件人（已选 {{ composer.recipient_ids.length }} 位）<select v-model="composer.recipient_ids" @change="composer.recipient_mode = ''" multiple class="recipient-select" :disabled="!contextReady"><option v-for="item in availableUsers" :key="item.id" :value="item.id">{{ userName(item) }}{{ item.name && item.name !== item.id && item.email ? ` (${item.email})` : '' }}</option></select></label><label>内容<textarea v-model="composer.content" rows="6" maxlength="20000" /></label><label class="upload-label"><Paperclip :size="16" />{{ uploading ? '上传中' : '添加附件（最多 5 个）' }}<input type="file" multiple :disabled="uploading || !contextReady" @change="upload" /></label><ul class="attachments"><li v-for="(item, index) in composer.attachments" :key="item.id"><Paperclip :size="15" /><span>{{ item.name }}</span><button class="icon-btn" title="移除附件" aria-label="移除附件" :disabled="uploading" @click="composer.attachments.splice(index, 1)"><X :size="16" /></button></li></ul></fieldset><p v-if="error" role="alert" class="error-text">{{ error }}</p><div class="modal-actions"><button class="secondary-btn" :disabled="saving || uploading || !contextReady" @click="send(true)"><Save :size="15" />保存草稿</button><button class="primary-btn" :disabled="saving || uploading || !contextReady" @click="send(false)"><Send :size="15" />发送</button></div></section></div>

    <div v-if="settingsOpen" class="modal-scrim" @click.self="!saving && (settingsOpen = false)"><section class="modal-card message-detail" role="dialog" aria-modal="true" aria-label="消息设置"><div class="drawer-head"><h2>消息设置</h2><button class="icon-btn" title="关闭" aria-label="关闭" :disabled="saving" @click="settingsOpen = false"><X :size="18" /></button></div><form @submit.prevent="saveConfig(false)"><h3>个人邮件偏好</h3><label class="checkbox-label"><input v-model="preferences.email_enabled" type="checkbox" :disabled="!preferencesReady || saving" />接收邮件通知</label><button class="secondary-btn" :disabled="!preferencesReady || saving"><Save :size="15" />保存偏好</button></form><form v-if="context.platform_admin" class="platform-settings" @submit.prevent="saveConfig(true)"><h3>平台消息设置</h3><fieldset :disabled="!settingsReady || saving"><div class="form-grid"><label>保留天数<input v-model.number="settings.retention_days" type="number" min="1" max="3650" step="1" required /></label><label>附件上限（MB）<input v-model.number="settings.attachment_max_mb" type="number" min="1" max="100" step="1" required /></label></div><label class="checkbox-label"><input v-model="settings.email_enabled" type="checkbox" />启用平台邮件通知</label><button class="primary-btn"><Save :size="15" />保存平台设置</button></fieldset></form><section v-if="context.platform_admin" class="platform-settings"><div class="drawer-head"><h3>通知投递</h3><button class="icon-btn" title="刷新投递状态" aria-label="刷新投递状态" :disabled="healthBusy" @click="loadHealth"><RefreshCw :size="16" /></button></div><ul class="attachments"><li v-for="(item, index) in deliveryHealth.counts" :key="index"><span>{{ channelName(item.channel) }} · {{ emailState(item.status) }}</span><strong>{{ item.count }} 条</strong></li></ul><div class="table-wrap"><table v-if="deliveryHealth.failures.length" class="data-table"><thead><tr><th>渠道</th><th>尝试次数</th><th>状态</th><th>操作</th></tr></thead><tbody><tr v-for="item in deliveryHealth.failures" :key="item.id"><td>{{ channelName(item.channel) }}</td><td>{{ item.attempts }}</td><td>{{ deliveryFailure(item.result) }}</td><td><button class="text-btn" :disabled="saving || healthBusy" @click="retryDelivery(item)"><RefreshCw :size="14" />重试</button></td></tr></tbody></table><p v-else class="muted">{{ healthBusy ? '加载中' : '暂无失败任务' }}</p></div><h3>过期紧急消息待办</h3><ul v-if="deliveryHealth.expired_urgent.length" class="expired-urgent-list"><li v-for="item in deliveryHealth.expired_urgent" :key="item.id"><div class="drawer-head"><strong>{{ item.recipient || '收件人' }}</strong><span class="muted">过期于 {{ date(item.expired_at) }}</span></div><label>运营处理说明<textarea v-model="resolutionReasons[item.id]" rows="2" maxlength="500" :disabled="saving" /></label><button class="secondary-btn" :disabled="saving || healthBusy || !resolutionReasons[item.id]?.trim()" @click="resolveExpired(item)"><CheckCircle2 :size="15" />记录运营处理</button></li></ul><p v-else class="muted">{{ healthBusy ? '加载中' : '暂无过期紧急消息待办' }}</p></section><p v-if="error" role="alert" class="error-text">{{ error }}</p><p v-if="notice" role="status" class="muted">{{ notice }}</p></section></div>

    <div v-if="confirmation" class="modal-scrim confirmation-scrim" @click.self="!saving && (confirmation = null)"><section class="modal-card" role="dialog" aria-modal="true" aria-label="确认操作"><h2>{{ confirmation.action === 'publish' ? '发送草稿' : '撤回消息' }}</h2><p class="message-body">{{ confirmation.item.title }}</p><p v-if="confirmation.action === 'recall'" class="muted">已被邮件服务接受的邮件无法撤回</p><label v-if="confirmation.action === 'recall'">撤回原因<textarea v-model="confirmation.reason" rows="3" maxlength="500" :disabled="saving" required /></label><p v-if="error" role="alert" class="error-text">{{ error }}</p><div class="modal-actions"><button class="secondary-btn" :disabled="saving" @click="confirmation = null">取消</button><button class="primary-btn" :disabled="saving" @click="sentAction">{{ saving ? '处理中' : '确认' }}</button></div></section></div>
    <div v-if="preview" class="modal-scrim preview-scrim" @click.self="closePreview"><section class="modal-card message-detail" role="dialog" aria-modal="true" aria-label="附件预览"><div class="drawer-head"><h2>{{ preview.item.name }}</h2><button class="icon-btn" title="关闭" aria-label="关闭" @click="closePreview"><X :size="18" /></button></div><img v-if="preview.kind === 'image'" class="attachment-preview" :src="preview.url" :alt="preview.item.name" /><iframe v-else-if="preview.kind === 'pdf'" class="attachment-preview pdf-preview" :src="preview.url" title="附件预览" sandbox></iframe><pre v-else-if="preview.kind === 'text'" class="message-body">{{ preview.text }}</pre><p v-else class="muted">此附件需下载查看</p><div class="modal-actions"><button class="secondary-btn" :disabled="downloading" @click="attachment(preview.item)"><Download :size="15" />下载</button></div></section></div>
  </div>
</template>

<style scoped>
.expired-urgent-list{list-style:none;padding:0;margin:0}.expired-urgent-list li{border-bottom:1px solid #e5e6eb;padding:14px 0}.expired-urgent-list strong{font-size:12px;overflow-wrap:anywhere}.expired-urgent-list .drawer-head{flex-wrap:wrap;gap:8px}
.message-center{min-width:0}.message-filter{display:flex;align-items:flex-end;flex-wrap:wrap;gap:12px;margin:18px 0}.message-filter label{display:flex;flex-direction:column;gap:6px;font-size:11px;color:#4e5969}.message-filter input,.message-filter select{min-width:0;border:1px solid #d9dce1;border-radius:7px;padding:9px 11px;font-size:12px;background:#fff}.message-filter>span{align-self:center}.message-tabs{display:flex;flex-wrap:wrap;gap:4px}.message-batch{margin-bottom:12px}.message-detail{width:min(700px,calc(100% - 32px));max-height:90vh;overflow:auto;border-radius:8px}.message-body{white-space:pre-wrap;font-size:13px;line-height:1.7;overflow-wrap:anywhere}.message-detail h2,.modal-card h2{font-size:18px;overflow-wrap:anywhere}.message-detail h3{font-size:14px;margin-top:22px}.audit-pagination{display:flex;justify-content:flex-end;align-items:center;gap:12px;margin-top:18px}.message-title{max-width:380px;overflow-wrap:anywhere}.message-title button{white-space:normal;text-align:left}.message-excerpt{display:block;max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.message-center fieldset{border:0;padding:0;margin:0;min-width:0}.message-center .modal-actions{display:flex;justify-content:flex-end;flex-wrap:wrap;gap:10px;margin-top:20px}.attachments{list-style:none;padding:0;margin:14px 0}.attachments li{display:flex;align-items:center;gap:8px;border-bottom:1px solid #e5e6eb;padding:7px 0;font-size:12px}.attachments li>span{flex:1;min-width:0;overflow-wrap:anywhere}.message-center .checkbox-label{display:flex;flex-direction:row;align-items:center;gap:8px}.checkbox-label input{width:16px;height:16px;flex:0 0 auto}.recipient-select{min-height:110px}.platform-settings{border-top:1px solid #e5e6eb;margin-top:24px;padding-top:4px}.message-center .upload-label{display:flex;flex-direction:row;align-items:center;flex-wrap:wrap}.upload-label input{max-width:100%;font-size:12px}.attachment-preview{display:block;width:100%;max-height:60vh;object-fit:contain;margin-top:18px}.pdf-preview{height:55vh;border:0}.preview-scrim,.confirmation-scrim{z-index:60}.message-center button:disabled{opacity:.5;cursor:not-allowed}.page-heading>.action-list{align-items:center}.table-wrap{overflow-x:auto}@media(max-width:600px){.page-heading{align-items:flex-start;flex-wrap:wrap}.message-filter label{flex:1 1 130px;min-width:0}.message-filter input,.message-filter select{width:100%}.message-detail{padding:18px}.message-center .form-grid{grid-template-columns:1fr;gap:0}.message-tabs button{white-space:normal}.message-excerpt{max-width:200px}}
</style>
