<script setup>
import { ref, onMounted } from 'vue';
import axios from 'axios';
import { Save, RefreshCw } from 'lucide-vue-next';

const values = ref({ retention_days: 180, attachment_max_mb: 20, email_enabled: true, retry_count: 3, retry_interval_seconds: 10, poll_interval_seconds: 30, email_subject_template: '${title}', email_body_template: '${content}\n\n${platform_url}' });
const busy = ref(false);
const ready = ref(false);
const error = ref('');
const notice = ref('');
const api = axios.create({ baseURL: '/api' });
api.interceptors.request.use(config => { config.headers.Authorization = `Bearer ${localStorage.getItem('market_token')}`; return config; });
async function load() {
  busy.value = true;
  try { values.value = (await api.get('/notifications/settings')).data; ready.value = true; error.value = ''; }
  catch (e) { error.value = e.response?.data?.detail || '消息配置加载失败'; }
  finally { busy.value = false; }
}
async function save() {
  busy.value = true; notice.value = '';
  try { values.value = (await api.put('/notifications/settings', values.value)).data; notice.value = '消息配置已保存'; error.value = ''; }
  catch (e) { error.value = typeof e.response?.data?.detail === 'string' ? e.response.data.detail : '配置无效或保存失败'; }
  finally { busy.value = false; }
}
onMounted(load);
</script>

<template>
  <div class="page-heading"><div><div class="eyebrow">平台管理</div><h1>消息中心设置</h1></div><button class="secondary-btn" :disabled="busy" @click="load"><RefreshCw :size="15" />刷新</button></div>
  <form class="message-settings-form" @submit.prevent="save">
    <fieldset :disabled="busy || !ready">
      <h2>消息与附件</h2>
      <div class="form-grid"><label>新消息保留天数<input v-model.number="values.retention_days" type="number" min="1" max="3650" required /></label><label>附件上限（MB）<input v-model.number="values.attachment_max_mb" type="number" min="1" max="100" required /></label></div>
      <h2>发送与提醒</h2>
      <div class="form-grid"><label>失败重试次数<input v-model.number="values.retry_count" type="number" min="0" max="10" required /></label><label>重试间隔（秒）<input v-model.number="values.retry_interval_seconds" type="number" min="1" max="3600" required /></label><label>断线轮询间隔（秒）<input v-model.number="values.poll_interval_seconds" type="number" min="5" max="300" required /></label></div>
      <label class="checkbox-line"><input v-model="values.email_enabled" type="checkbox" />启用邮件通知</label>
      <h2>邮件模板</h2>
      <label>邮件主题<input v-model="values.email_subject_template" maxlength="300" required /></label><label>邮件正文<textarea v-model="values.email_body_template" rows="7" maxlength="10000" required /></label>
      <button class="primary-btn"><Save :size="15" />保存配置</button>
    </fieldset>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
  </form>
</template>

<style scoped>
.message-settings-form{max-width:760px}.message-settings-form fieldset{border:0;margin:0;padding:0;min-width:0}.message-settings-form h2{font-size:14px;margin:22px 0 16px;border-bottom:1px solid #e5e6eb;padding-bottom:10px}.checkbox-label{display:flex;flex-direction:row;align-items:center;gap:8px}.checkbox-label input{width:16px;height:16px}.message-settings-form button{margin-top:16px}.message-settings-form input,.message-settings-form textarea{font-size:12px}
</style>
