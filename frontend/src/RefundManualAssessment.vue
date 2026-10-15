<script setup>
import { computed, ref } from "vue";
import { ClipboardCheck } from "lucide-vue-next";
import { isRefundApprover, refundManualValues } from "./refunds.js";
const props = defineProps({ user: Object, enabled: Boolean, busy: Boolean });
const emit = defineEmits(["assess"]);
const amount = ref(""), usedCalls = ref(""), basis = ref(""), refundableLimit = ref(""), evidenceRefs = ref(""), error = ref("");
const permitted = computed(() => isRefundApprover(props.user));
function submit() {
  if (!permitted.value || !props.enabled || props.busy) return;
  try {
    const values = { amount: amount.value, usedCalls: usedCalls.value, basis: basis.value, refundableLimit: refundableLimit.value, evidenceRefs: evidenceRefs.value };
    refundManualValues(values); error.value = ""; emit("assess", values);
  } catch (failure) { error.value = failure.message; }
}
</script>
<template>
  <section v-if="permitted" class="refund-manual-assessment refund-form"><h4>历史缺证人工核定</h4><p>用量未知（unknown）；自动试算禁用。人工核定须明确金额、已扣减用量和依据。</p><p v-if="!enabled" class="muted">人工核定接口尚未就绪，当前不可提交。</p><p v-if="error" role="alert" class="error-text">{{ error }}</p>
    <form @submit.prevent="submit"><fieldset class="form-grid" :disabled="!enabled || busy"><label>核定退款金额<input v-model="amount" inputmode="decimal" required /></label><label>核定可退上限<input v-model="refundableLimit" inputmode="decimal" required /></label><label>核定已扣减用量<input v-model="usedCalls" inputmode="numeric" required /></label><label class="wide">核定依据（必填）<textarea v-model="basis" rows="3" required maxlength="10000" /></label><label class="wide">证据引用（每行一条，必填）<textarea v-model="evidenceRefs" rows="3" required /></label><div class="wide"><button class="secondary-btn" type="submit" :disabled="!amount.trim() || !refundableLimit.trim() || !usedCalls.trim() || !basis.trim() || !evidenceRefs.trim()"><ClipboardCheck :size="15" />提交人工核定</button></div></fieldset></form>
  </section>
</template>
<style scoped>.refund-manual-assessment fieldset{border:0;padding:0;margin:12px 0;min-width:0}.refund-manual-assessment p{overflow-wrap:anywhere}</style>
<style scoped src="./refundForms.css"></style>
