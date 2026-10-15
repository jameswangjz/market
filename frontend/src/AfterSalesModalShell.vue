<script setup>
import { nextTick, onMounted, onBeforeUnmount, ref } from "vue";
import { X } from "lucide-vue-next";
defineProps({ title: String, busy: Boolean });
const emit = defineEmits(["close"]);
const panel = ref(null);
let previousFocus, overflow;
onMounted(async () => { previousFocus = document.activeElement; overflow = document.body.style.overflow; document.body.style.overflow = "hidden"; await nextTick(); panel.value?.focus(); });
onBeforeUnmount(() => { document.body.style.overflow = overflow; previousFocus?.focus?.(); });
function trap(event) {
  if (event.key !== "Tab") return;
  const elements = [...panel.value.querySelectorAll('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex="0"]')];
  const first = elements[0], last = elements.at(-1);
  if (event.shiftKey && [panel.value, first].includes(document.activeElement)) { event.preventDefault(); last?.focus(); }
  else if (!event.shiftKey && [panel.value, last].includes(document.activeElement)) { event.preventDefault(); first?.focus(); }
}
</script>
<template>
  <Teleport to="body"><div class="modal-scrim after-sales-scrim">
    <section ref="panel" class="modal-card after-sales-modal" role="dialog" aria-modal="true" aria-labelledby="after-sales-title" tabindex="-1" @keydown="trap">
      <div class="drawer-head"><h2 id="after-sales-title">{{ title }}</h2><button type="button" class="icon-btn" :disabled="busy" title="关闭工单窗口" aria-label="关闭工单窗口" @click="emit('close')"><X :size="18" /></button></div>
      <slot />
    </section>
  </div></Teleport>
</template>
<style>
.after-sales-scrim{z-index:95}.modal-card.after-sales-modal{width:min(880px,calc(100% - 24px));max-height:calc(100dvh - 24px);overflow:auto;border-radius:8px;min-width:0}.after-sales-modal p,.after-sales-modal dd,.after-sales-modal small{overflow-wrap:anywhere;white-space:pre-wrap}.after-sales-modal textarea{width:100%;box-sizing:border-box;resize:vertical}.after-sales-modal fieldset{border:0;padding:0;margin:12px 0;min-width:0}.after-sales-modal .after-sales-actions{display:flex;flex-wrap:wrap;gap:8px}.after-sales-modal .after-sales-record{border-bottom:1px solid #dce2e8;padding:12px 0}.after-sales-modal .after-sales-meta{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.after-sales-modal dd{margin:4px 0}.after-sales-modal .error-text{color:#b42318}@media(max-width:600px){.after-sales-modal .after-sales-meta{grid-template-columns:minmax(0,1fr)}}
</style>
