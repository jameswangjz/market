<script setup>
import { computed, ref } from "vue";
import { Check, X } from "lucide-vue-next";
import { isRefundApprover, refundReason } from "./refunds.js";
const props = defineProps({ user: Object, busy: Boolean, allowedActions: { type: Array, default: () => [] }, approvalAvailable: Boolean });
const emit = defineEmits(["review"]);
const reason = ref(""), error = ref("");
const permitted = computed(() => isRefundApprover(props.user));
function submit(decision) {
  if (props.busy || !permitted.value || !props.allowedActions.includes(decision) || (decision === "approve" && !props.approvalAvailable)) return;
  try { error.value = ""; emit("review", { decision, reason: refundReason(reason.value) }); }
  catch (failure) { error.value = failure.message; }
}
</script>
<template>
  <section v-if="permitted" class="refund-review-form refund-form"><h3>退款审核</h3>
    <p v-if="!approvalAvailable" class="muted">可信试算及经济方案尚不可用，暂不能批准。</p>
    <p v-if="error" role="alert" class="error-text">{{ error }}</p>
    <fieldset :disabled="busy"><label>审核理由（必填）<textarea v-model="reason" rows="3" required maxlength="10000" /></label>
      <div class="refund-review-actions"><button v-if="allowedActions.includes('reject')" type="button" class="secondary-btn" :disabled="!reason.trim()" @click="submit('reject')"><X :size="15" />拒绝申请</button><button v-if="allowedActions.includes('approve')" type="button" class="primary-btn" :disabled="!approvalAvailable || !reason.trim()" @click="submit('approve')"><Check :size="15" />批准申请</button></div>
    </fieldset>
  </section>
</template>
<style scoped>.refund-review-form fieldset{border:0;padding:0;margin:12px 0}.refund-review-form textarea{width:100%;box-sizing:border-box}.refund-review-actions{display:flex;gap:8px;flex-wrap:wrap}</style>
<style scoped src="./refundForms.css"></style>
