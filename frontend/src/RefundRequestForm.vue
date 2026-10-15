<script setup>
import { computed, ref, watch } from "vue";
import { Send, Calculator } from "lucide-vue-next";
import { refundAmount, refundReason, refundMoney, refundPlanning } from "./refunds.js";
const props = defineProps({ busy: Boolean, quoteBusy: Boolean, error: String, enabled: Boolean, orderNo: String, preview: Object });
const emit = defineEmits(["submit", "quote", "amount-change"]);
const amount = ref(""), reason = ref(""), ticketId = ref(""), paymentId = ref(""), validation = ref("");
const previewState = computed(() => refundPlanning(props.preview));
watch(amount, () => emit("amount-change"));
function quote() { try { validation.value = ""; emit("quote", refundAmount(amount.value)); } catch (failure) { validation.value = failure.message; } }
function submit() {
  if (props.busy || !props.enabled) return;
  try { validation.value = ""; emit("submit", { amount: refundAmount(amount.value), reason: refundReason(reason.value), ticketId: ticketId.value.trim(), paymentId: paymentId.value.trim() }); }
  catch (failure) { validation.value = failure.message; }
}
</script>
<template>
  <section class="refund-request-form refund-form">
    <h3>申请普通退款</h3><p>原订单：{{ orderNo || '—' }}</p>
    <div class="refund-preview"><strong>{{ previewState.label }}</strong><p>{{ previewState.reason }}</p>
      <template v-if="preview?.status === 'available'">
        <p>服务端可退余额：{{ refundMoney(preview.item?.refundable_balance_before) }}</p>
        <template v-if="preview.item?.basis === 'time'">
          <p>时间口径：按原订阅时段计算（非调用次数）</p>
          <p v-if="preview.item.starts_at || preview.item.original_ends_at">原时段：{{ preview.item.starts_at || '—' }} 至 {{ preview.item.original_ends_at || '—' }}</p>
          <p v-if="preview.item.effective_ends_at">试算后预计截止：{{ preview.item.effective_ends_at }}</p>
        </template>
        <p v-else-if="preview.item?.basis === 'calls'">实际扣减次数：{{ preview.item.charged_calls ?? '未知' }}</p>
        <p v-else>计量口径未知，不推断用量。</p>
        <p>试算截至：{{ preview.item?.as_of || '—' }}</p>
      </template>
      <button class="secondary-btn" type="button" :disabled="busy || quoteBusy || !enabled || !amount.trim()" @click="quote"><Calculator :size="15" />{{ quoteBusy ? '试算中' : '服务端试算' }}</button>
    </div>
    <p v-if="!enabled" class="muted">申请入口尚未就绪或当前身份无申请权限。</p>
    <p v-if="error || validation" role="alert" class="error-text">{{ error || validation }}</p>
    <form @submit.prevent="submit"><fieldset class="form-grid" :disabled="busy || !enabled">
      <label>申请金额<input v-model="amount" type="text" inputmode="decimal" required maxlength="15" /></label>
      <label>关联工单 ID（可选）<input v-model="ticketId" maxlength="36" /></label>
      <label>支付单 ID（多笔支付时指定）<input v-model="paymentId" maxlength="36" /></label>
      <label class="wide">申请理由（必填）<textarea v-model="reason" rows="4" required maxlength="10000" /></label>
      <div class="wide"><button class="primary-btn" type="submit" :disabled="!amount.trim() || !reason.trim()"><Send :size="15" />{{ busy ? '提交中' : '提交申请' }}</button></div>
    </fieldset></form>
    <p class="muted">申请不代表退款完成，也不自动关闭关联售后工单。</p>
  </section>
</template>
<style scoped>
.refund-request-form{min-width:0}.refund-request-form fieldset{border:0;margin:12px 0;padding:0}.refund-preview{padding:12px 0;border-bottom:1px solid #e5e6eb}.refund-request-form p{overflow-wrap:anywhere}
</style>
<style scoped src="./refundForms.css"></style>
