<script setup>
const props = defineProps({ modelValue: String, tabs: { type: Array, required: true }, label: { type: String, required: true } });
const emit = defineEmits(['update:modelValue']);
function move(event, index) {
  const enabled = props.tabs.filter(tab => !tab.disabled);
  const current = enabled.findIndex(tab => tab.key === props.tabs[index].key);
  let next;
  if (event.key === 'ArrowRight') next = (current + 1) % enabled.length;
  else if (event.key === 'ArrowLeft') next = (current - 1 + enabled.length) % enabled.length;
  else if (event.key === 'Home') next = 0;
  else if (event.key === 'End') next = enabled.length - 1;
  else return;
  event.preventDefault();
  const tab = enabled[next];
  if (!tab) return;
  emit('update:modelValue', tab.key);
  const buttons = [...event.currentTarget.parentElement.querySelectorAll('[role="tab"]')];
  buttons[props.tabs.findIndex(item => item.key === tab.key)]?.focus();
}
</script>
<template>
  <div class="console-tabs" role="tablist" :aria-label="label">
    <button v-for="(tab, index) in tabs" :key="tab.key" type="button" role="tab" :aria-selected="modelValue === tab.key" :tabindex="modelValue === tab.key ? 0 : -1" :disabled="tab.disabled" :class="{ active: modelValue === tab.key }" @click="emit('update:modelValue', tab.key)" @keydown="move($event, index)">
      <component :is="tab.icon" v-if="tab.icon" :size="16" aria-hidden="true" />
      <span>{{ tab.label }}</span><small v-if="tab.count != null">{{ tab.count }}</small>
    </button>
  </div>
</template>
<style scoped>
.console-tabs{display:flex;flex-wrap:wrap;gap:4px 18px;margin:0 0 18px;border-bottom:1px solid #e5e6eb;min-width:0;letter-spacing:0}.console-tabs button{display:flex;align-items:center;justify-content:center;gap:7px;min-height:44px;max-width:100%;padding:10px 2px;border:0;border-bottom:2px solid transparent;background:transparent;color:#4e5969;font-size:13px;line-height:20px;cursor:pointer;overflow-wrap:anywhere}.console-tabs button.active{border-bottom-color:#ff6a00;color:#c64c00;font-weight:600}.console-tabs button:focus-visible{outline:2px solid #165dff;outline-offset:2px}.console-tabs button:disabled{opacity:.5;cursor:default}.console-tabs svg{flex-shrink:0}.console-tabs small{font-size:12px;color:#6b7785;font-variant-numeric:tabular-nums}@media(max-width:600px){.console-tabs{gap:0 12px}.console-tabs button{font-size:12px;flex:0 1 auto}}
</style>
