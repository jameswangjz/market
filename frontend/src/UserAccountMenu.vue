<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { ChevronDown, UserRound } from "lucide-vue-next";
defineProps({ name: String, roleLabel: String });
const emit = defineEmits(["profile"]);
const expanded = ref(false), root = ref(null), trigger = ref(null), menu = ref(null);
function outside(event) {
  if (!root.value?.contains(event.target)) expanded.value = false;
}
function leave() {
  if (!root.value?.contains(document.activeElement)) expanded.value = false;
}
function blur(event) {
  if (!root.value?.contains(event.relatedTarget)) expanded.value = false;
}
function close(event) {
  if (!expanded.value) return;
  event.preventDefault(); event.stopPropagation();
  expanded.value = false; trigger.value?.focus();
}
async function focusMenu() {
  expanded.value = true;
  await nextTick();
  menu.value?.querySelector('[role="menuitem"]')?.focus();
}
function move(event) {
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const items = [...menu.value.querySelectorAll('[role="menuitem"]')];
  const index = items.indexOf(document.activeElement);
  const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
    : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
  items[next]?.focus();
}
onMounted(() => document.addEventListener("pointerdown", outside));
onBeforeUnmount(() => document.removeEventListener("pointerdown", outside));
</script>

<template>
  <div ref="root" class="account-menu" @pointerenter="$event.pointerType === 'mouse' && (expanded = true)" @pointerleave="leave" @focusout="blur" @keydown.esc="close">
    <button ref="trigger" type="button" class="user-chip account-trigger" aria-label="用户菜单" aria-haspopup="menu"
      :aria-expanded="expanded" aria-controls="account-menu-panel" @click="expanded = !expanded" @keydown.down.prevent="focusMenu">
      <span class="avatar">{{ name?.slice(0, 1) }}</span>
      <span class="user-chip-copy"><strong>{{ name }}</strong><small>{{ roleLabel }}</small></span>
      <ChevronDown :size="14" aria-hidden="true" />
    </button>
    <div v-show="expanded" class="account-menu-popup">
      <div id="account-menu-panel" ref="menu" class="account-menu-panel" role="menu" aria-label="账户操作" @keydown="move" @click="expanded = false">
        <button type="button" role="menuitem" class="account-menu-item" @click="emit('profile')"><UserRound :size="16" />个人信息</button>
        <slot />
      </div>
    </div>
  </div>
</template>

<style>
.account-menu-item { display:flex; align-items:center; gap:9px; width:100%; padding:9px 12px; border:0; border-radius:4px; background:transparent; color:#4e5969; font-size:13px; line-height:20px; text-align:left; }
.account-menu-item:hover,.account-menu-item:focus-visible { background:#fff0e6; color:#c64c00; }
</style>
<style scoped>
.account-menu { position:relative; min-width:0; }
.account-trigger { border:0; padding:4px; background:transparent; border-radius:6px; font:inherit; color:inherit; text-align:left; max-width:100%; }
.account-trigger > svg { flex-shrink:0; color:#86909c; }
.account-menu-popup { position:absolute; top:100%; right:0; width:184px; max-width:calc(100vw - 24px); padding-top:6px; z-index:45; }
.account-menu-panel { padding:4px; background:#fff; border:1px solid #e5e6eb; border-radius:6px; box-shadow:0 6px 20px #1d21291a; }
</style>
