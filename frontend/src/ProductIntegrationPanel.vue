<script setup>
import { reactive, watch, onUnmounted } from 'vue';
import { RefreshCw, ShieldCheck, Download, FileText } from 'lucide-vue-next';
import { createIntegrationController, integrationStatus } from './productIntegration.js';

const props = defineProps({ product: { type: Object, required: true }, api: { type: Object, required: true }, provider: Boolean });
const emit = defineEmits(['changed', 'credentials']);
const state = reactive({ rows: [], busy: '', loading: false });
const controller = createIntegrationController({ state, api: props.api, product: () => props.product, changed: () => emit('changed') });
watch(() => [props.product.id, props.product.status, props.product.versions?.map(v => v.id).join(',')], controller.load, { immediate: true });
onUnmounted(controller.dispose);
</script>

<template>
  <section class="product-integration-panel">
    <div class="integration-heading">
      <h3>版本接入状态</h3>
      <div class="integration-actions">
        <a href="/api-gateway-integration.md" download="API应用提供方接入统一网关接口与规范.md" class="text-btn"><FileText :size="14" />提供方规范</a>
        <button v-if="provider && ['pending_integration', 'published'].includes(product.status)" type="button" class="text-btn" @click="emit('credentials')"><Download :size="14" />下载接入凭据</button>
        <button type="button" class="icon-btn" title="刷新接入状态" aria-label="刷新接入状态" :disabled="state.loading || !!state.busy" @click="controller.load"><RefreshCw :size="15" /></button>
      </div>
    </div>
    <p v-if="state.loading" class="muted" role="status">加载中</p>
    <p v-else-if="!state.rows.length" class="muted">暂无已保存版本</p>
    <div v-for="row in state.rows" :key="row.version.id" class="integration-row">
      <div class="integration-row-heading">
        <strong>{{ row.version.version_code }}</strong>
        <span :class="['status-pill', row.item?.status === 'verified' && row.item?.healthy ? 'status-done' : 'status-review']">{{ integrationStatus(row.item) }}</span>
        <button v-if="row.item && ['pending_integration', 'published'].includes(product.status)" type="button" class="secondary-btn" :disabled="state.loading || !!state.busy" @click="controller.verify(row)"><ShieldCheck :size="14" />{{ state.busy === row.version.id ? '验证中' : '验证接入' }}</button>
      </div>
      <dl v-if="row.route">
        <div><dt>路由标识</dt><dd>{{ row.route.route_key }}</dd></div>
        <div><dt>健康检查</dt><dd>{{ row.route.health_method }} {{ row.route.health_path }}<span v-if="row.item?.health_status_code"> · HTTP {{ row.item.health_status_code }}</span></dd></div>
        <div><dt>最近验证</dt><dd>{{ row.item?.verified_at ? new Date(row.item.verified_at).toLocaleString() : '-' }}</dd></div>
        <div><dt>验证结果</dt><dd>{{ row.item?.status === 'failed' ? `验证失败 (${row.item.error_code || 'integration_failed'})` : row.route.health_message || '-' }}</dd></div>
      </dl>
      <p v-if="row.error" class="danger-text" role="alert">{{ row.error }}</p>
    </div>
  </section>
</template>

<style scoped>
.product-integration-panel{border-top:1px solid #e5e6eb;margin-top:20px;padding-top:16px;min-width:0}.integration-heading,.integration-actions,.integration-row-heading{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.integration-heading{justify-content:space-between}.integration-heading h3{font-size:14px;margin:0}.integration-actions .text-btn{display:inline-flex;align-items:center;gap:5px;font-size:12px}.integration-row{padding:14px 0;border-bottom:1px solid #e5e6eb}.integration-row-heading strong{font-size:13px}.integration-row-heading button{margin-left:auto;font-size:12px}.integration-row dl{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 18px;margin:12px 0 0;font-size:12px}.integration-row dt{color:#727782;margin-bottom:4px}.integration-row dd{margin:0;overflow-wrap:anywhere}.integration-row p{font-size:12px;margin-bottom:0}@media(max-width:600px){.integration-row dl{grid-template-columns:1fr}.integration-actions{gap:8px}}
</style>
