<script setup>
import { ref } from "vue";
import AfterSalesModalShell from "./AfterSalesModalShell.vue";
import { newAfterSalesRequestKey } from "./afterSales.js";
const props = defineProps({ api: Object, orderId: String, params: Object });
const emit = defineEmits(["close", "created"]);
const description = ref(""), type = ref("质量异议"), priority = ref("normal"), busy = ref(false), error = ref("");
let operation;
async function submit() {
  if (busy.value || !description.value.trim() || !type.value.trim()) return;
  const body = { order_id: props.orderId, type: type.value.trim(), description: description.value.trim(), priority: priority.value };
  const fingerprint = JSON.stringify(body);
  if (operation?.fingerprint !== fingerprint) {
    try { operation = { fingerprint, key: newAfterSalesRequestKey() }; }
    catch { error.value = "浏览器无法安全生成请求标识，请更换浏览器后重试"; return; }
  }
  busy.value = true; error.value = "";
  try { const response = await props.api.post("/after-sales", { ...body, idempotency_key: operation.key }, { params: props.params }); emit("created", response.data); }
  catch (failure) { error.value = failure.response?.status === 409 ? "订单或工单已变化，请刷新订单与工单列表后核对；输入已保留。" : failure.response?.data?.detail || "创建失败，输入已保留，可重试"; }
  finally { busy.value = false; }
}
</script>
<template>
  <AfterSalesModalShell title="发起售后" :busy="busy" @close="emit('close')"><p>订单：{{ orderId }}</p><p v-if="error" class="error-text" role="alert">{{ error }}</p>
    <form @submit.prevent="submit"><fieldset :disabled="busy"><label>类别<select v-model="type"><option>质量异议</option><option>交付问题</option><option>服务问题</option><option>其他</option></select></label><label>优先级<select v-model="priority"><option value="normal">普通</option><option value="important">重要</option><option value="urgent">紧急</option></select></label><label>售后说明（必填）<textarea v-model="description" rows="5" required maxlength="10000" /></label><button class="primary-btn" :disabled="!description.trim()" type="submit">{{ busy ? '提交中' : '提交售后申请' }}</button></fieldset></form>
  </AfterSalesModalShell>
</template>
