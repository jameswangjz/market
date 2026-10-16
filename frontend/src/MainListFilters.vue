<script setup>
import { Search, RotateCcw } from "lucide-vue-next";
defineProps({ state: Object, fields: Array });
defineEmits(["search", "reset"]);
</script>
<template>
  <form class="main-list-filters" @submit.prevent="$emit('search')">
    <label v-for="field in fields" :key="field.key">{{ field.label }}
      <select v-if="field.options" v-model="state.filters[field.key]">
        <option v-if="!field.options.some(option => option.value === 'all')" value="">全部</option><option v-for="option in field.options" :key="option.value" :value="option.value">{{ option.label }}</option>
      </select>
      <input v-else v-model="state.filters[field.key]" :type="field.type || 'text'" maxlength="200" />
    </label>
    <button class="primary-btn" :disabled="state.busy"><Search :size="15" />检索</button>
    <button type="button" class="secondary-btn" :disabled="state.busy" @click="$emit('reset')"><RotateCcw :size="15" />重置</button>
    <p v-if="state.error" role="alert">{{ state.error }}</p>
  </form>
</template>
<style scoped>
.main-list-filters{display:flex;align-items:end;flex-wrap:wrap;gap:12px;margin:12px 0 18px}.main-list-filters.main-list-filters label{display:flex;flex-direction:column;gap:5px;width:170px;max-width:100%;margin:0;font-size:12px;line-height:18px}.main-list-filters.main-list-filters input,.main-list-filters.main-list-filters select{box-sizing:border-box;width:100%;height:34px;min-width:0;padding:6px 8px;border:1px solid #d9dde3;border-radius:4px;background:white;font-size:12px;line-height:20px}.main-list-filters p{flex-basis:100%;margin:0;color:#bd2929}@media(max-width:600px){.main-list-filters.main-list-filters label{width:calc(50% - 6px)}}
</style>
