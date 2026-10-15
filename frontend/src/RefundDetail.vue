<script setup>
import RefundReviewForm from "./RefundReviewForm.vue";
import RefundManualAssessment from "./RefundManualAssessment.vue";
import { refundStatus, refundMoney, refundSource, refundPlanning, canAssessRefund } from "./refunds.js";
defineProps({ item: Object, user: Object, busy: Boolean, error: String, allowedActions: { type: Array, default: () => [] }, approvalAvailable: Boolean });
const emit = defineEmits(["review", "assess"]);
const date = value => value ? new Date(value).toLocaleString() : "—";
</script>
<template>
  <section class="refund-detail"><p v-if="error" role="alert" class="error-text">{{ error }}</p><p v-if="busy" role="status">读取或提交中</p>
    <template v-if="item"><h3>{{ item.refund_no || '退款详情' }}</h3>
      <dl class="state-grid"><div><dt>原订单</dt><dd>{{ item.order_no || item.order_id || '—' }}</dd></div><div><dt>支付单</dt><dd>{{ item.payment_no || item.payment_id || '—' }}</dd></div><div><dt>金额</dt><dd>{{ refundMoney(item.amount) }}</dd></div><div><dt>状态 / 版本</dt><dd>{{ refundStatus(item.status) }} / {{ item.version ?? '—' }}</dd></div><div><dt>来源</dt><dd>{{ refundSource(item.source) }}</dd></div><div><dt>关联工单</dt><dd>{{ item.ticket_no || item.ticket_id || '—' }}</dd></div><div><dt>申请时间</dt><dd>{{ date(item.created_at) }}</dd></div><div><dt>完成时间</dt><dd>{{ date(item.completed_at) }}</dd></div></dl>
      <h4>申请理由</h4><p>{{ item.reason || '未记录' }}</p><h4>审核理由</h4><p>{{ item.review_reason || '未记录' }}</p>
      <h4>{{ refundPlanning(item.planning).label }}</h4><p>{{ refundPlanning(item.planning).reason }}</p><p v-if="item.execution_available !== true" class="muted">资金与资源执行尚不可用；不提供模拟成功或强制完成入口。</p>
      <dl v-if="item.planning?.preview" class="state-grid"><div><dt>服务端可退余额</dt><dd>{{ refundMoney(item.planning.preview.refundable_balance_before) }}</dd></div><div><dt>实际扣减次数</dt><dd>{{ item.planning.preview.charged_calls ?? '未知' }}</dd></div><div><dt>计量依据</dt><dd>{{ item.planning.preview.basis === 'calls' ? '实际扣减次数' : item.planning.preview.basis === 'time' ? '服务时长' : '未知' }}</dd></div><div><dt>试算截至</dt><dd>{{ date(item.planning.preview.as_of) }}</dd></div></dl>
      <RefundManualAssessment v-if="canAssessRefund(item, user)" :user="user" :enabled="canAssessRefund(item, user)" :busy="busy" @assess="emit('assess', $event)" />
      <p class="muted">退款单与售后工单分别处理。模拟资金记录不作为银行到账证明。</p>
      <RefundReviewForm :user="user" :busy="busy" :allowed-actions="allowedActions" :approval-available="approvalAvailable" @review="emit('review', $event)" />
    </template>
  </section>
</template>
<style scoped>.refund-detail{min-width:0}.refund-detail dd{margin:4px 0;overflow-wrap:anywhere}.refund-detail p{white-space:pre-wrap;overflow-wrap:anywhere}</style>
