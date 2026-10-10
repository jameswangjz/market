<script setup>
import { nextTick, onBeforeUnmount, ref, watch } from "vue";
import { CircleAlert, X } from "lucide-vue-next";
const props = defineProps({ dialog: Object });
const emit = defineEmits(["submit", "cancel"]);
const panel = ref(null);
const input = ref(null);
const value = ref("");
const error = ref("");
let previousFocus;
let previousOverflow;
watch(() => props.dialog, async (dialog, old) => {
  if (dialog) {
    if (!old) {
      previousFocus = document.activeElement;
      previousOverflow = document.body.style.overflow;
      document.body.style.overflow = "hidden";
    }
    value.value = dialog.value || "";
    error.value = "";
    await nextTick();
    (input.value || panel.value)?.focus();
  } else {
    document.body.style.overflow = previousOverflow ?? "";
    previousFocus?.focus?.();
  }
});
function submit() {
  if (props.dialog.required && !value.value.trim()) {
    error.value = props.dialog.requiredMessage || "请填写内容后再提交";
    input.value?.focus();
    return;
  }
  emit("submit", value.value);
}
function keydown(event) {
  if (event.key === "Escape") { event.preventDefault(); emit("cancel"); }
  if (event.key !== "Tab") return;
  const controls = [...panel.value.querySelectorAll('button:not(:disabled), textarea, input, [tabindex="0"]')];
  const first = controls[0], last = controls.at(-1);
  if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.value)) {
    event.preventDefault(); last?.focus();
  } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === panel.value)) {
    event.preventDefault(); first?.focus();
  }
}
onBeforeUnmount(() => {
  if (props.dialog) document.body.style.overflow = previousOverflow ?? "";
});
</script>

<template>
  <Teleport to="body">
    <div v-if="dialog" class="modal-scrim action-dialog-scrim" @click.self="emit('cancel')">
      <form ref="panel" class="modal-card action-dialog" role="dialog" aria-modal="true"
        aria-labelledby="action-dialog-title" aria-describedby="action-dialog-description" tabindex="-1"
        @submit.prevent="submit" @keydown="keydown">
        <div class="drawer-head">
          <div><span class="eyebrow">MARKET</span><h2 id="action-dialog-title">{{ dialog.title }}</h2></div>
          <button type="button" class="icon-btn" title="关闭" aria-label="关闭" @click="emit('cancel')"><X :size="18" /></button>
        </div>
        <p id="action-dialog-description" class="action-dialog-description">{{ dialog.message }}</p>
        <label v-if="dialog.kind === 'prompt'">
          {{ dialog.label || "填写内容" }}<span v-if="dialog.required" class="action-dialog-required">必填</span>
          <textarea v-if="dialog.multiline" ref="input" v-model="value" rows="4" :aria-invalid="!!error" aria-describedby="action-dialog-error" @input="error = ''" />
          <input v-else ref="input" v-model="value" :aria-invalid="!!error" aria-describedby="action-dialog-error" @input="error = ''" />
        </label>
        <p v-if="error" id="action-dialog-error" class="error-text" role="alert"><CircleAlert :size="14" /> {{ error }}</p>
        <div class="modal-actions">
          <button type="button" class="secondary-btn" @click="emit('cancel')">取消</button>
          <button type="submit" :class="['primary-btn', { 'action-dialog-danger': dialog.danger }]">{{ dialog.confirmText }}</button>
        </div>
      </form>
    </div>
  </Teleport>
</template>

<style>
.action-dialog-scrim{z-index:100}
.modal-card.action-dialog{width:min(480px,calc(100% - 32px));max-height:calc(100dvh - 32px);border-radius:8px;padding:24px}
.action-dialog-description{font-size:13px;line-height:1.7;color:#4e5969;overflow-wrap:anywhere;white-space:pre-wrap}
.action-dialog textarea{resize:vertical;min-height:104px;max-height:240px}
.action-dialog-required{font-size:11px;color:#c9302c;font-weight:400}
.action-dialog .error-text{display:flex;align-items:center;gap:6px}
.action-dialog .action-dialog-danger{background:#c9302c;box-shadow:none}
.action-dialog .action-dialog-danger:hover{background:#a72724}
@media(max-width:480px){.modal-card.action-dialog{padding:18px}.action-dialog .modal-actions{flex-wrap:wrap}}
</style>
