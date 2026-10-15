<script setup>
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { Save, RefreshCw } from "lucide-vue-next";
const props = defineProps({ api: Object, enterprise: Object, user: Object });
const levels = ref([]), assignments = ref([]), levelId = ref(""), reason = ref("");
const loading = ref(false), saving = ref(false), error = ref(""), message = ref("");
const permitted = computed(() => ["super_admin", "platform_operator"].includes(props.user?.platform_role) && props.enterprise?.verification_status === "verified");
let epoch = 0;
const date = value => value ? new Date(value).toLocaleString() : "—";
async function load() {
  if (!permitted.value) return;
  const current = ++epoch, id = props.enterprise.id;
  loading.value = true; error.value = "";
  try {
    const [configuration, history] = await Promise.all([props.api.get("/sla/service-levels"), props.api.get("/sla/service-level-assignments")]);
    if (current !== epoch) return;
    levels.value = configuration.data.items.filter(item => item.status === "active" && ["standard", "enterprise", "strategic"].includes(item.code));
    assignments.value = history.data.items.filter(item => item.enterprise_id === id);
  } catch (failure) { if (current === epoch) error.value = failure.response?.data?.detail || "企业SLA读取失败"; }
  finally { if (current === epoch) loading.value = false; }
}
async function save() {
  if (!permitted.value || saving.value || loading.value) return;
  if (!levels.value.some(item => item.id === levelId.value) || !reason.value.trim() || reason.value.trim().length > 2000) { error.value = "请选择已启用等级并填写调整原因（最多2000字符）"; return; }
  const current = epoch;
  saving.value = true; error.value = ""; message.value = "";
  try {
    const response = await props.api.post("/sla/service-level-assignments", { enterprise_id: props.enterprise.id, service_level_id: levelId.value, reason: reason.value.trim() });
    if (current !== epoch) return;
    message.value = `调整已生效：${date(response.data.item.effective_at)}；已有工单与订单冻结承诺不变。`;
    reason.value = ""; levelId.value = "";
    await load();
  } catch (failure) { if (current === epoch) error.value = failure.response?.data?.detail || "调整失败，输入已保留"; }
  finally { saving.value = false; }
}
watch(() => [props.enterprise?.id, props.user?.id, permitted.value].join(":"), () => {
  epoch++; levels.value = []; assignments.value = []; reason.value = ""; levelId.value = ""; message.value = ""; error.value = ""; load();
}, { immediate: true });
onBeforeUnmount(() => epoch++);
</script>
<template>
  <section v-if="permitted" class="sla-enterprise-adjustment">
    <div class="panel-heading"><h3>企业SLA等级调整</h3><button class="icon-btn" title="刷新企业SLA记录" aria-label="刷新企业SLA记录" :disabled="loading || saving" @click="load"><RefreshCw :size="16" /></button></div>
    <p class="muted">仅影响新工单及后续冻结承诺，不追溯变更已有工单或订单。</p>
    <p v-if="loading" role="status">读取中</p><p v-if="error" class="error-text" role="alert">{{ error }}</p><p v-if="message" role="status">{{ message }}</p>
    <form @submit.prevent="save"><fieldset :disabled="loading || saving"><label>服务等级<select v-model="levelId" required><option value="">请选择等级</option><option v-for="level in levels" :key="level.id" :value="level.id">{{ level.name }}</option></select></label><label>调整原因（必填）<textarea v-model="reason" rows="3" required maxlength="2000" /></label><button class="secondary-btn" type="submit" :disabled="!levelId || !reason.trim()"><Save :size="15" />{{ saving ? '保存中' : '调整等级' }}</button></fieldset></form>
    <div class="table-wrap"><table class="data-table compact"><thead><tr><th>等级</th><th>生效时间</th><th>到期时间</th><th>操作人</th></tr></thead><tbody><tr v-for="item in assignments" :key="item.id"><td>{{ item.service_level?.name || '—' }}</td><td>{{ date(item.effective_at) }}</td><td>{{ date(item.expires_at) }}</td><td>{{ item.created_by || '—' }}</td></tr><tr v-if="!assignments.length"><td colspan="4">暂无等级记录；未指定时使用标准级。</td></tr></tbody></table></div>
  </section>
</template>
<style scoped>
.sla-enterprise-adjustment{width:100%;min-width:0;grid-column:1/-1}.sla-enterprise-adjustment fieldset{border:0;padding:0;margin:12px 0;min-width:0}.sla-enterprise-adjustment textarea{width:100%;box-sizing:border-box}.sla-enterprise-adjustment p{overflow-wrap:anywhere}
</style>
