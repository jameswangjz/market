<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
import axios from "axios";
import { ArrowLeft, ArrowRight, Building2, Database, PackageCheck, RefreshCw, Search, ShoppingCart } from "lucide-vue-next";
import { consoleLoginUrl, navigate } from "./routes.js";
import "./storefront.css";

const props = defineProps({ productId: { type: String, default: "" }, search: { type: String, default: "" } });
// This client never inherits the console's bearer token or private product API.
const api = axios.create({ baseURL: "/api/storefront", timeout: 15000 });
const items = ref([]);
const product = ref(null);
const busy = ref(false);
const error = ref("");
const notFound = ref(false);
const total = ref(0);
const page = ref(1);
const pageSize = ref(12);
const query = ref("");
const type = ref("");
const selectedVersion = ref("");
const types = { dataset: "数据集", model: "模型", api: "API 服务", application: "数据应用", saas: "SaaS 应用", report: "数据报告", training: "培训", consulting: "咨询", custom: "定制开发" };
const deliveryLabels = { file: "文件交付", api: "API 交付", model_api: "模型 API 交付", tenant_access: "租户访问授权", saas: "SaaS 应用", online: "线上交付", offline: "线下交付", manual: "人工交付", training: "培训服务", consulting: "咨询服务", custom: "定制开发" };
const pages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)));
const loginUrl = computed(() => consoleLoginUrl(`/products/${encodeURIComponent(props.productId)}`));
const version = computed(() => product.value?.versions?.find(item => item.id === selectedVersion.value));
let controller;

function price(value) {
  if (value === null || value === undefined || value === "" || !Number.isFinite(Number(value))) return "价格待公布";
  return `¥${Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
function priceUnit(value) {
  return { month: "月", "/month": "月", order: "次", "/order": "次" }[value] || String(value || "").replace(/^\//, "");
}
function logoUrl(value) {
  if (!value) return "";
  try {
    const url = new URL(value, window.location.origin);
    if (url.username || url.password) return "";
    if (url.origin === window.location.origin && /^\/api\/storefront\/products\/[^/]+\/logo$/.test(url.pathname)) return url.href;
    if (url.origin !== window.location.origin && url.protocol === "https:") return url.href;
  } catch { /* Invalid logos use the product-type icon. */ }
  return "";
}
function hideBrokenLogo(event) { event.target.hidden = true; }
function productUrl(id) { return `/products/${encodeURIComponent(id)}`; }
function applyFilters(nextPage = 1) {
  const params = new URLSearchParams();
  if (query.value.trim()) params.set("q", query.value.trim());
  if (type.value) params.set("type", type.value);
  if (nextPage > 1) params.set("page", String(nextPage));
  navigate(`/${params.size ? `?${params}` : ""}`);
}
async function load() {
  controller?.abort();
  const request = new AbortController();
  controller = request;
  busy.value = true;
  error.value = "";
  notFound.value = false;
  product.value = null;
  items.value = [];
  const params = new URLSearchParams(props.search);
  query.value = params.get("q") || "";
  type.value = params.get("type") || "";
  const requestedPage = Number(params.get("page") || 1);
  page.value = Number.isSafeInteger(requestedPage) && requestedPage > 0 ? requestedPage : 1;
  try {
    if (props.productId) {
      const { data } = await api.get(`/products/${encodeURIComponent(props.productId)}`, { signal: request.signal });
      if (request.signal.aborted) return;
      if (!data?.name || !Array.isArray(data.versions)) throw new Error("invalid-contract");
      product.value = data;
      selectedVersion.value = data.versions[0]?.id || "";
    } else {
      const { data } = await api.get("/products", { params: { q: query.value, type: type.value, page: page.value, page_size: 12 }, signal: request.signal });
      if (request.signal.aborted) return;
      if (!Array.isArray(data?.items) || !Number.isFinite(data.total) || !Number.isInteger(data.page) || !Number.isInteger(data.page_size) || data.total < 0 || data.page < 1 || data.page_size < 1) throw new Error("invalid-contract");
      items.value = data.items.filter(item => item?.id && item.name);
      total.value = data.total;
      page.value = data.page;
      pageSize.value = data.page_size;
    }
  } catch (failure) {
    if (request.signal.aborted) return;
    notFound.value = Boolean(props.productId && failure.response?.status === 404);
    error.value = notFound.value ? "商品不存在或已下架" : failure.message === "invalid-contract" ? "商品数据暂时不可用，请稍后重试" : "公开商城服务暂时不可用，请稍后重试";
  } finally {
    if (!request.signal.aborted) busy.value = false;
  }
}
watch(() => [props.productId, props.search], load, { immediate: true });
onUnmounted(() => controller?.abort());
</script>

<template>
  <div class="store-shell">
    <header class="store-header">
      <a href="/" class="store-brand" @click.prevent="navigate('/')"><span class="brand-mark">M</span><strong>market <small>数据与服务商城</small></strong></a>
      <a href="/console" class="secondary-btn" @click.prevent="navigate('/console')"><Building2 :size="16" />工作台登录</a>
    </header>
    <main class="store-content">
      <template v-if="!productId">
        <div class="page-heading"><div><h1>数据与服务商城</h1><p>数据产品、API 服务与专业服务</p></div><span v-if="!busy && !error" class="muted">{{ total }} 个商品</span></div>
        <form class="store-filters" @submit.prevent="applyFilters()">
          <label class="store-search"><Search :size="18" /><input v-model="query" type="search" aria-label="搜索商品" placeholder="搜索商品名称" /></label>
          <select v-model="type" aria-label="商品类型" @change="applyFilters()"><option value="">全部类型</option><option v-for="(name, key) in types" :key="key" :value="key">{{ name }}</option></select>
          <button type="submit" class="primary-btn"><Search :size="16" />搜索</button>
        </form>
      </template>
      <a v-else href="/" class="store-back text-btn" @click.prevent="navigate('/')"><ArrowLeft :size="16" />全部商品</a>

      <div v-if="busy" class="store-state" role="status"><RefreshCw :size="24" class="store-spinning" /><p>正在加载商品</p></div>
      <div v-else-if="error" class="store-state" role="alert"><PackageCheck :size="32" /><h2>{{ error }}</h2><button v-if="!notFound" class="secondary-btn" @click="load"><RefreshCw :size="16" />重试</button></div>
      <template v-else-if="productId && product">
        <section class="store-product-heading">
          <div class="store-logo store-logo-large"><Database :size="40" /><img v-if="logoUrl(product.logo_url)" :key="product.logo_url" :src="logoUrl(product.logo_url)" :alt="product.name" @error="hideBrokenLogo" /></div>
          <div><span class="catalog-tag">{{ types[product.product_type] || product.product_type }}</span><h1>{{ product.name }}</h1><p><Building2 :size="16" />{{ product.provider_name || '提供方待公布' }}</p></div>
        </section>
        <div class="store-detail-grid">
          <div class="store-description"><section><h2>商品介绍</h2><p>{{ product.description || '暂无介绍' }}</p></section><section><h2>交付方式</h2><p>{{ deliveryLabels[product.delivery_method] || product.delivery_method || '待公布' }}</p></section></div>
          <section class="store-version-tool">
            <h2>商品版本</h2>
            <label v-if="product.versions.length">版本<select v-model="selectedVersion"><option v-for="item in product.versions" :key="item.id" :value="item.id">{{ item.version_code }}</option></select></label>
            <template v-if="version"><p class="store-version-description">{{ version.description || '暂无版本介绍' }}</p><div class="store-price">{{ price(version.price) }}<small v-if="product.price_unit"> / {{ priceUnit(product.price_unit) }}</small></div></template>
            <p v-else class="muted">暂无可售版本</p>
            <a :href="loginUrl" class="primary-btn full-btn" @click.prevent="navigate(loginUrl)"><ShoppingCart :size="16" />前往工作台</a>
          </section>
        </div>
      </template>
      <template v-else-if="!productId">
        <div v-if="items.length" class="store-grid">
          <article v-for="item in items" :key="item.id" class="store-card">
            <a :href="productUrl(item.id)" class="store-card-title" @click.prevent="navigate(productUrl(item.id))"><span class="store-logo"><Database :size="24" /><img v-if="logoUrl(item.logo_url)" :src="logoUrl(item.logo_url)" :alt="item.name" @error="hideBrokenLogo" /></span><h2>{{ item.name }}</h2></a>
            <p class="store-provider">{{ item.provider_name || '提供方待公布' }}</p>
            <p class="store-card-description">{{ item.description || '暂无介绍' }}</p>
            <div class="store-tags"><span class="catalog-tag">{{ types[item.product_type] || item.product_type }}</span><span>{{ deliveryLabels[item.delivery_method] || item.delivery_method }}</span></div>
            <div class="store-card-bottom"><strong>{{ price(item.minimum_price) }}<small v-if="item.price_unit"> / {{ priceUnit(item.price_unit) }}</small></strong><a :href="productUrl(item.id)" class="icon-btn" title="查看商品详情" aria-label="查看商品详情" @click.prevent="navigate(productUrl(item.id))"><ArrowRight :size="18" /></a></div>
          </article>
        </div>
        <div v-else class="store-state"><Database :size="32" /><h2>{{ query || type ? '没有找到匹配的商品' : '暂无在售商品' }}</h2></div>
        <nav v-if="total > 0" class="store-pagination" aria-label="商品分页"><button class="icon-btn" title="上一页" aria-label="上一页" :disabled="page <= 1" @click="applyFilters(page - 1)"><ArrowLeft :size="18" /></button><span>{{ page }} / {{ pages }}</span><button class="icon-btn" title="下一页" aria-label="下一页" :disabled="page >= pages" @click="applyFilters(page + 1)"><ArrowRight :size="18" /></button></nav>
      </template>
    </main>
  </div>
</template>
