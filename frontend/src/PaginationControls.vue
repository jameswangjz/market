<script setup>
import { computed, ref, watch } from "vue";
import { ArrowLeft, ArrowRight, CornerDownLeft } from "lucide-vue-next";
import { pageCount, pageSizes, validPage } from "./pagination.js";
const props = defineProps({ page: { type: Number, default: 1 }, pageSize: { type: Number, default: 20 }, total: { type: Number, default: 0 }, busy: Boolean, local: Boolean, label: { type: String, default: "分页" } });
const emit = defineEmits(["page", "size"]);
const pages = computed(() => pageCount(props.total, props.pageSize));
const canJump = computed(() => !props.busy && props.total > 0);
const jump = ref(String(props.page));
const error = ref("");
watch(() => [props.page, props.pageSize, props.total], () => { jump.value = String(props.page); error.value = ""; });
function go(value) {
  if (!canJump.value) return;
  const next = validPage(value, pages.value);
  if (next === null) { error.value = `请输入 1 至 ${pages.value} 的整数页码`; return; }
  error.value = "";
  if (next !== props.page) emit("page", next);
}
function resize(event) {
  const size = Number(event.target.value);
  if (!props.busy && pageSizes.includes(size) && size !== props.pageSize) emit("size", size);
}
</script>
<template>
  <nav class="pagination-controls" :aria-label="label" :aria-busy="busy">
    <span class="pagination-total">{{ local ? '本地分页 · ' : '' }}共 {{ total }} 条</span>
    <label>每页<select :value="pageSize" :disabled="busy" aria-label="每页条数" @change="resize"><option v-if="!pageSizes.includes(pageSize)" :value="pageSize">{{ pageSize }}</option><option v-for="size in pageSizes" :key="size" :value="size">{{ size }}</option></select></label>
    <div class="pagination-navigation"><button type="button" class="icon-btn" title="上一页" aria-label="上一页" :disabled="!canJump || page <= 1" @click="go(page - 1)"><ArrowLeft :size="16" /></button><span class="pagination-position">{{ page }} / {{ pages }}</span><button type="button" class="icon-btn" title="下一页" aria-label="下一页" :disabled="!canJump || page >= pages" @click="go(page + 1)"><ArrowRight :size="16" /></button></div>
    <label>跳至<input v-model="jump" type="text" inputmode="numeric" aria-label="跳转页码" :aria-invalid="!!error" :disabled="!canJump" @input="error = ''" @keydown.enter.prevent="go(jump)" /></label>
    <button type="button" class="icon-btn" title="跳转到指定页" aria-label="跳转到指定页" :disabled="!canJump" @click="go(jump)"><CornerDownLeft :size="16" /></button>
    <span class="pagination-error" role="alert">{{ error }}</span>
  </nav>
</template>
<style scoped>
.pagination-controls{display:flex;align-items:center;justify-content:flex-end;flex-wrap:wrap;gap:6px 10px;min-height:68px;margin-top:14px;font-size:12px;line-height:1.5;color:#4e5969;letter-spacing:0}.pagination-controls label{display:flex;flex-direction:row;align-items:center;gap:6px;margin:0;font-size:12px}.pagination-controls select,.pagination-controls input{box-sizing:border-box;height:30px;min-width:0;margin:0;padding:4px 6px;border:1px solid #e5e6eb;border-radius:5px;background:#fff;color:#4e5969;font-size:12px;line-height:20px}.pagination-controls select{width:66px}.pagination-controls input{width:58px}.pagination-navigation{display:flex;align-items:center;gap:4px}.pagination-controls .icon-btn{width:30px;height:30px;flex:0 0 30px}.pagination-position{min-width:70px;text-align:center;font-variant-numeric:tabular-nums}.pagination-total{font-variant-numeric:tabular-nums}.pagination-error{flex-basis:100%;min-height:18px;color:#c9302c;text-align:right}.pagination-controls :disabled{opacity:.5;cursor:default}@media(max-width:600px){.pagination-controls{justify-content:flex-start;gap:6px 8px}.pagination-error{text-align:left}}
</style>
