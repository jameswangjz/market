<script setup>
import { computed, onMounted, reactive, ref } from 'vue';
import { RefreshCw } from 'lucide-vue-next';

const props = defineProps({ api: { type: Object, required: true } });
const saved = ref(null);
const form = reactive({ provider: 'minio', endpoint: '', secure: true, region: '', bucket: '', ca_pem: '', auth_mode: 'ak_sk_v4', access_key: '', secret_key: '' });
const busy = ref('');
const error = ref('');
const message = ref('');
const canKeepCredentials = computed(() => saved.value?.credentials_configured &&
  form.provider === saved.value.provider && form.endpoint.trim().toLowerCase() === saved.value.endpoint.toLowerCase() && form.secure === saved.value.secure);

function reset(data) {
  saved.value = data;
  Object.assign(form, {
    provider: data.provider, endpoint: data.endpoint, secure: data.secure,
    region: data.region, bucket: data.bucket, ca_pem: data.ca_pem || '',
    auth_mode: 'ak_sk_v4', access_key: '', secret_key: '',
  });
}

function errorMessage(err) {
  const detail = err.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map(item => item.msg || JSON.stringify(item)).join('，');
  return detail ? JSON.stringify(detail) : '请求失败，请稍后重试';
}

async function load() {
  busy.value = 'load';
  error.value = '';
  message.value = '';
  try {
    const { data } = await props.api.get('/admin/settings/s3-storage');
    reset(data);
  } catch (err) {
    error.value = errorMessage(err);
  } finally {
    busy.value = '';
  }
}

function payload() {
  const endpoint = form.endpoint.trim();
  if (!endpoint || /[\s/\\?#@]/.test(endpoint) || endpoint.includes('://')) throw new Error('Endpoint 请填写主机名或主机名加端口，不含协议和路径');
  if (!form.bucket.trim()) throw new Error('请填写 Bucket');
  if (form.provider === 'h3c' && !form.region.trim()) throw new Error('H3C 必须填写 Region，并使用已创建的 Bucket');
  if (Boolean(form.access_key) !== Boolean(form.secret_key)) throw new Error('Access Key 和 Secret Key 必须同时填写');
  if (!canKeepCredentials.value && (!form.access_key || !form.secret_key)) throw new Error('当前连接需要填写 Access Key 和 Secret Key');
  const result = { provider: form.provider, endpoint, secure: form.secure, region: form.region.trim(), bucket: form.bucket.trim(), ca_pem: form.secure ? form.ca_pem.trim() : '', auth_mode: 'ak_sk_v4' };
  if (form.access_key && form.secret_key) Object.assign(result, { access_key: form.access_key, secret_key: form.secret_key });
  return result;
}

async function submit(action) {
  error.value = '';
  message.value = '';
  let body;
  try { body = payload(); } catch (err) { error.value = err.message; return; }
  busy.value = action;
  try {
    if (action === 'test') {
      const { data } = await props.api.post('/admin/settings/s3-storage/test', body);
      message.value = data.message || '连接测试成功';
    } else {
      const { data } = await props.api.patch('/admin/settings/s3-storage', body);
      reset(data);
      message.value = 'S3 存储已保存并启用';
    }
  } catch (err) {
    error.value = errorMessage(err);
  } finally {
    busy.value = '';
  }
}

onMounted(load);
</script>

<template>
  <div class="page-heading">
    <div><div class="eyebrow">平台管理 · S3存储</div><h1>S3存储</h1><p>切换现有 Bucket 前，请先迁移数据并完成最终校验。</p></div>
    <button class="secondary-btn" type="button" :disabled="!!busy" @click="load"><RefreshCw :size="16" />刷新</button>
  </div>
  <section class="panel s3-settings">
    <div class="panel-heading"><h3>存储连接配置</h3><span v-if="saved" class="muted">配置来源 {{ saved.source === 'database' ? '数据库' : '环境变量' }}</span></div>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    <p v-if="message" role="status">{{ message }}</p>
    <p v-if="busy === 'load'" class="muted" role="status">正在读取存储配置…</p>
    <form v-if="saved" @submit.prevent="submit('save')">
      <fieldset :disabled="!!busy">
        <div class="form-grid">
          <label>存储提供商<select v-model="form.provider"><option value="minio">MinIO</option><option value="h3c">新华三 H3C S3</option></select></label>
          <label>存储访问地址<input v-model="form.endpoint" required placeholder="s3.example.com:9000" /></label>
          <label>Region<input v-model="form.region" :required="form.provider === 'h3c'" placeholder="存储服务所在区域" /></label>
          <label>{{ form.provider === "h3c" ? "华三预设私有桶" : "存储桶" }}<input v-model="form.bucket" required placeholder="存储桶名称" /></label>
        </div>
        <label class="checkbox-line"><input v-model="form.secure" type="checkbox" />使用 HTTPS</label>
        <p v-if="form.provider === 'h3c'" class="muted">H3C 需使用预先创建的 Bucket，并明确填写 Region。</p>
        <label>CA 证书包（可选 PEM）<textarea v-model="form.ca_pem" :disabled="!form.secure" rows="5" placeholder="自签名或私有 CA 证书包" spellcheck="false" /></label>
        <p class="muted">认证方式 AK/SK S3 Signature V4</p>
        <div class="form-grid">
          <label>Access Key<input v-model="form.access_key" type="password" autocomplete="new-password" :required="!canKeepCredentials || !!form.secret_key" /></label>
          <label>Secret Key<input v-model="form.secret_key" type="password" autocomplete="new-password" :required="!canKeepCredentials || !!form.access_key" /></label>
        </div>
        <p class="muted">{{ canKeepCredentials ? '已配置凭据，AK/SK 同时留空可保留，同时填写可更新。' : '请填写完整 AK/SK，变更提供商、Endpoint 或协议后须重新填写。' }}</p>
        <div class="s3-actions">
          <button class="secondary-btn" type="button" @click="submit('test')">{{ busy === 'test' ? '正在测试…' : '测试连接' }}</button>
          <button class="primary-btn" type="submit">{{ busy === 'save' ? '正在保存…' : '保存并启用' }}</button>
        </div>
      </fieldset>
    </form>
  </section>
</template>

<style scoped>
.s3-settings fieldset { border: 0; margin: 0; padding: 0; min-width: 0; }
.s3-settings label:not(.checkbox-line) { display: flex; flex-direction: column; gap: 7px; margin-bottom: 15px; font-size: 12px; font-weight: 600; color: #4e5969; }
.s3-settings input:not([type="checkbox"]), .s3-settings select, .s3-settings textarea { width: 100%; min-width: 0; border: 1px solid #d9dce1; border-radius: 7px; padding: 10px 11px; background: #fff; color: #1d2129; font: inherit; }
.s3-settings textarea { resize: vertical; }
.s3-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }
</style>
