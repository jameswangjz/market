<script setup>
import { Download, RotateCcw, Search } from 'lucide-vue-next';
defineProps({ filters: { type: Object, required: true }, busy: Boolean, exporting: Boolean, canExport: Boolean });
const emit = defineEmits(['search', 'reset', 'export', 'update']);
function update(key, event) { emit('update', key, event.target.value); }
</script>
<template>
  <form class="audit-search" @submit.prevent="emit('search')">
    <div class="audit-search-grid">
      <label>关键词<input :value="filters.q" placeholder="动作、对象或说明" @input="update('q', $event)" /></label>
      <label>操作人<input :value="filters.actor" placeholder="用户名/邮箱" @input="update('actor', $event)" /></label>
      <label>订单号<input :value="filters.order_id" placeholder="ORD…" @input="update('order_id', $event)" /></label>
      <label>清算批次<input :value="filters.batch_no" placeholder="批次号" @input="update('batch_no', $event)" /></label>
      <label>规则版本<input :value="filters.rule_version" placeholder="如 v1" @input="update('rule_version', $event)" /></label>
      <label>风险级别<select :value="filters.risk_level" @change="update('risk_level', $event)"><option value="">全部</option><option value="normal">正常</option><option value="warning">预警</option><option value="high">高风险</option></select></label>
      <label>开始时间<input :value="filters.start" type="datetime-local" :max="filters.end || undefined" @input="update('start', $event)" /></label>
      <label>结束时间<input :value="filters.end" type="datetime-local" :min="filters.start || undefined" @input="update('end', $event)" /></label>
    </div>
    <div class="audit-search-actions">
      <button type="submit" class="primary-btn" :disabled="busy"><Search :size="15" />检索</button>
      <button type="button" class="secondary-btn" :disabled="busy" @click="emit('reset')"><RotateCcw :size="15" />清空条件</button>
      <button v-if="canExport" type="button" class="secondary-btn" :disabled="exporting || busy" @click="emit('export')"><Download :size="15" />{{ exporting ? '导出中' : '导出 CSV' }}</button>
      <span class="muted">共 {{ filters.total }} 条，当前第 {{ filters.page }} / {{ filters.pages || 1 }} 页</span>
    </div>
  </form>
</template>
<style scoped>
.audit-search{container-type:inline-size;min-width:0}.audit-search-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.audit-search-grid label{display:flex;flex-direction:column;gap:6px;min-width:0;font-size:12px;font-weight:600;color:#4e5969}.audit-search-grid input,.audit-search-grid select{box-sizing:border-box;display:block;width:100%;min-width:0;max-width:100%;height:34px;padding:7px 8px;border:1px solid #d9dce1;border-radius:4px;background:#fff;color:#1d2129;font:inherit;font-weight:400}.audit-search-actions{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:16px 0 22px}.audit-search-actions button{min-height:34px}.audit-search-actions .muted{font-size:12px;overflow-wrap:anywhere}@container(max-width:800px){.audit-search-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@container(max-width:420px){.audit-search-grid{grid-template-columns:minmax(0,1fr)}}
</style>
