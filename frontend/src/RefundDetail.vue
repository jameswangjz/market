<script setup>
import { computed } from "vue";
import { Play, RotateCcw, RefreshCw, X } from "lucide-vue-next";
import RefundReviewForm from "./RefundReviewForm.vue";
import RefundManualAssessment from "./RefundManualAssessment.vue";
import { refundStatus, refundMoney, refundSource, refundPlanning, canAssessRefund, refundExecutionActions, refundExecutionProgress } from "./refunds.js";
const props = defineProps({ item: Object, user: Object, busy: Boolean, error: String, allowedActions: { type: Array, default: () => [] }, approvalAvailable: Boolean });
const emit = defineEmits(["review", "assess", "execute", "refresh", "close"]);
const progress = computed(() => refundExecutionProgress(props.item));
const date = value => value ? new Date(value).toLocaleString() : "—";
</script>
<template>
  <section class="refund-detail"><p v-if="error" role="alert" class="error-text">{{ error }}</p><p v-if="busy" role="status">读取或提交中</p>
    <template v-if="item"><div class="refund-execution-actions"><h3>{{ item.refund_no || '退款详情' }}</h3><button type="button" class="icon-btn" :disabled="busy" title="关闭退款详情" aria-label="关闭退款详情" @click="emit('close')"><X :size="16" /></button></div>
      <dl class="state-grid"><div><dt>原订单</dt><dd>{{ item.order_no || item.order_id || '—' }}</dd></div><div><dt>支付单</dt><dd>{{ item.payment_no || item.payment_id || '—' }}</dd></div><div><dt>金额</dt><dd>{{ refundMoney(item.amount) }}</dd></div><div><dt>状态 / 版本</dt><dd>{{ refundStatus(item.status) }} / {{ item.version ?? '—' }}</dd></div><div><dt>来源</dt><dd>{{ refundSource(item.source) }}</dd></div><div><dt>关联工单</dt><dd>{{ item.ticket_no || item.ticket_id || '—' }}</dd></div><div><dt>申请时间</dt><dd>{{ date(item.created_at) }}</dd></div><div><dt>完成时间</dt><dd>{{ date(item.completed_at) }}</dd></div></dl>
      <h4>申请理由</h4><p>{{ item.reason || '未记录' }}</p><h4>审核理由</h4><p>{{ item.review_reason || '未记录' }}</p>
      <h4>{{ refundPlanning(item.planning).label }}</h4><p>{{ refundPlanning(item.planning).reason }}</p>
      <p v-if="item.status === 'completed'" class="muted">{{ item.execution_mode === 'simulated' || progress?.simulated ? '模拟退款已完成，仅记录本地执行结果，不代表银行到账或结算完成。' : '退款处理已完成，实际到账情况以支付机构凭证为准。' }}</p>
      <p v-else-if="item.status === 'executing' && !progress" role="status">退款执行中，等待最新执行进展。</p>
      <p v-else-if="!progress && item.status !== 'executing' && item.execution_available !== true" class="muted">资金与资源执行尚不可用；不提供模拟成功或强制完成入口。</p>
      <section v-if="progress" class="refund-execution" aria-label="退款执行进展">
        <p v-if="progress.simulated" class="muted">模拟资金处理，仅记录本地执行结果，不代表银行到账或结算完成。</p>
        <div class="refund-execution-actions"><h4>执行进展</h4><button type="button" class="icon-btn" title="刷新执行进展" aria-label="刷新执行进展" :disabled="busy" @click="emit('refresh')"><RefreshCw :size="16" /></button></div><dl class="state-grid"><div><dt>执行状态</dt><dd>{{ progress.status }}</dd></div><div><dt>资源回收</dt><dd>{{ progress.resource }}</dd></div><div><dt>清算状态</dt><dd>{{ progress.ledger }}</dd></div></dl>
        <p v-if="progress.failure" class="error-text">{{ progress.failure }}</p>
        <ol class="refund-execution-legs"><li v-for="(leg, index) in progress.legs" :key="index"><strong>{{ leg.label }}</strong><span>{{ leg.amount }}</span><span>{{ leg.status }}</span><span v-if="leg.failure" class="error-text">{{ leg.failure }}</span></li></ol>
        <p v-if="!progress.legsAvailable" class="muted">尚未提供逐资金腿进展。</p>
        <p v-if="item.status === 'failed'" class="muted">{{ refundExecutionActions(item, user).includes('retry') ? '请核对已完成资金腿及失败阶段，确认后重试。' : '等待平台核对处理；当前不可发起重试。' }}</p>
      </section>
      <div class="refund-execution-actions"><button v-if="refundExecutionActions(item, user).includes('execute')" type="button" class="primary-btn" :disabled="busy" @click="emit('execute', 'execute')"><Play :size="15" />执行退款</button><button v-if="refundExecutionActions(item, user).includes('retry')" type="button" class="secondary-btn" :disabled="busy" @click="emit('execute', 'retry')"><RotateCcw :size="15" />重试执行</button></div>
      <dl v-if="item.planning?.preview" class="state-grid"><div><dt>服务端可退余额</dt><dd>{{ refundMoney(item.planning.preview.refundable_balance_before) }}</dd></div><div><dt>实际扣减次数</dt><dd>{{ item.planning.preview.charged_calls ?? '未知' }}</dd></div><div><dt>计量依据</dt><dd>{{ item.planning.preview.basis === 'calls' ? '实际扣减次数' : item.planning.preview.basis === 'time' ? '服务时长' : '未知' }}</dd></div><div><dt>试算截至</dt><dd>{{ date(item.planning.preview.as_of) }}</dd></div></dl>
      <RefundManualAssessment v-if="canAssessRefund(item, user)" :user="user" :enabled="canAssessRefund(item, user)" :busy="busy" @assess="emit('assess', $event)" />
      <p class="muted">退款单与售后工单分别处理。模拟资金记录不作为银行到账证明。</p>
      <RefundReviewForm v-if="item.status === 'requested'" :user="user" :busy="busy" :allowed-actions="allowedActions" :approval-available="approvalAvailable" @review="emit('review', $event)" />
    </template>
  </section>
</template>
<style scoped>.refund-detail{min-width:0}.refund-detail dd{margin:4px 0;overflow-wrap:anywhere}.refund-detail p{white-space:pre-wrap;overflow-wrap:anywhere}</style>
<style scoped src="./refundForms.css"></style>
