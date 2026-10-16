<script setup>
import { RefreshCw, Eye } from "lucide-vue-next";
import { refundStatus, refundMoney, refundSource } from "./refunds.js";
defineProps({ items: { type: Array, default: () => [] }, busy: Boolean, error: String });
const emit = defineEmits(["refresh", "select"]);
const date = value => value ? new Date(value).toLocaleString() : "—";
</script>
<template>
  <section class="refund-list"><div class="panel-heading"><h3>退款申请</h3><button type="button" class="icon-btn" title="刷新退款列表" aria-label="刷新退款列表" :disabled="busy" @click="emit('refresh')"><RefreshCw :size="16" /></button></div>
    <p v-if="busy" role="status">读取中</p><p v-if="error" role="alert" class="error-text">{{ error }}</p>
    <div class="table-wrap"><table class="data-table compact"><thead><tr><th>退款单 / 原订单</th><th>产品</th><th>提供方</th><th>购买方</th><th>申请金额</th><th>状态</th><th>来源</th><th>申请时间</th><th>详情</th></tr></thead><tbody>
      <tr v-for="item in items" :key="item.id"><td><button class="text-btn task-code" :disabled="busy" @click="emit('select', item.id)">{{ item.refund_no || item.id }}</button><small>{{ item.order_no || item.order_id || '—' }}</small></td><td>{{ item.product_name || '—' }}</td><td>{{ item.provider_name || '—' }}</td><td>{{ item.buyer_name || '—' }}</td><td>{{ refundMoney(item.amount) }}</td><td>{{ refundStatus(item.status) }}</td><td>{{ refundSource(item.source) }}</td><td>{{ date(item.created_at) }}</td><td><button class="icon-btn" title="查看退款详情" aria-label="查看退款详情" :disabled="busy" @click="emit('select', item.id)"><Eye :size="16" /></button></td></tr>
      <tr v-if="!busy && !items.length"><td colspan="9">暂无可见退款申请。</td></tr>
    </tbody></table></div>
  </section>
</template>
