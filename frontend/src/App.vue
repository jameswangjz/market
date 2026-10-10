<script setup>
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref } from "vue";
import Storefront from "./Storefront.vue";
import { navigate, resolveRoute } from "./routes.js";

const ConsoleApp = defineAsyncComponent(() => import("./ConsoleApp.vue"));
const location = ref(new URL(window.location.href));
const route = computed(() => resolveRoute(location.value.pathname));
function syncRoute() {
  location.value = new URL(window.location.href);
  window.scrollTo(0, 0);
}
onMounted(() => window.addEventListener("popstate", syncRoute));
onUnmounted(() => window.removeEventListener("popstate", syncRoute));
</script>

<template>
  <ConsoleApp v-if="route.name === 'console'" />
  <Storefront v-else-if="['storefront', 'product'].includes(route.name)" :product-id="route.id || ''" :search="location.search" />
  <main v-else class="store-not-found">
    <h1>页面不存在</h1>
    <a href="/" class="primary-btn" @click.prevent="navigate('/')">返回商城</a>
  </main>
</template>
