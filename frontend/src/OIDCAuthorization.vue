<script>
import { oidcRequestId, safeApplicationUrl } from "./routes.js";

export function sanitizeOIDCRequest(data, id) {
  if (data?.request_id !== id || typeof data.product_name !== "string" || typeof data.enterprise_name !== "string" ||
      typeof data.enterprise_id !== "string" || !data.enterprise_id || !Array.isArray(data.scopes) ||
      !data.scopes.includes("openid") || !data.scopes.every(scope => ["openid", "profile", "email"].includes(scope)) ||
      !Number.isSafeInteger(data.expires_at) || data.expires_at <= 0 ||
      !Number.isFinite(new Date(data.expires_at * 1000).getTime())) throw new Error("授权请求无效");
  return { request_id: id, product_name: data.product_name, enterprise_name: data.enterprise_name,
    enterprise_id: data.enterprise_id, scopes: [...new Set(data.scopes)], expires_at: data.expires_at };
}

export function createOIDCSession({ api, getContext, onChange, onNavigate }) {
  let epoch = 0, disposed = false;
  let state = { item: null, actor: "", loading: false, deciding: "", error: "", status: 0 };
  const key = () => JSON.stringify(getContext());
  const emit = () => onChange({ ...state, item: state.item ? { ...state.item, scopes: [...state.item.scopes] } : null });
  const config = () => ({ headers: { Authorization: `Bearer ${getContext().token}` }, withCredentials: true });
  const current = (ticket, context) => !disposed && ticket === epoch && context === key();
  function reset() { epoch++; state = { item: null, actor: "", loading: false, deciding: "", error: "", status: 0 }; emit(); }
  function fail(error) {
    state.item = null;
    state.status = error?.response?.status || 0;
    state.error = state.status === 401 ? "请重新登录后继续授权" : state.status === 403 ? "当前账号或浏览器无权确认此请求" :
      [404, 409, 410].includes(state.status) ? "授权请求已过期或已处理，请从应用重新发起" : "授权请求暂时无法处理，请重试";
  }
  async function load() {
    reset();
    const { token, requestId } = getContext();
    if (disposed || !token || !/^[a-f0-9]{64}$/.test(requestId)) return;
    const ticket = epoch, context = key();
    state.loading = true; emit();
    try {
      const actor = await api.get("/auth/me", config());
      if (!current(ticket, context)) return;
      state.actor = typeof actor.data?.user?.name === "string" ? actor.data.user.name : "";
      const { data } = await api.get(`/oidc/browser/${requestId}`, config());
      if (current(ticket, context)) state.item = sanitizeOIDCRequest(data, requestId);
    } catch (error) { if (current(ticket, context)) fail(error); }
    finally { if (current(ticket, context)) { state.loading = false; emit(); } }
  }
  async function decide(decision) {
    if (disposed || !state.item || state.loading || state.deciding || !["approve", "reject"].includes(decision)) return;
    const ticket = epoch, context = key(), requestId = state.item.request_id;
    state.deciding = decision; state.error = ""; emit();
    try {
      const { data } = await api.post(`/oidc/browser/${requestId}/${decision}`, undefined, config());
      if (current(ticket, context)) { const target = safeApplicationUrl(data?.redirect_url); state.item = null; onNavigate(target); }
    } catch (error) { if (current(ticket, context)) fail(error); }
    finally { if (current(ticket, context)) { state.deciding = ""; emit(); } }
  }
  return { load, decide, reset, dispose() { reset(); disposed = true; } };
}
</script>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import axios from "axios";
import { Check, X, RefreshCw, LogIn, ArrowLeft, ShieldCheck } from "lucide-vue-next";
import { navigate, consoleLoginUrl } from "./routes.js";

const props = defineProps({ search: { type: String, default: "" } });
const requestId = computed(() => oidcRequestId(props.search));
const token = ref(localStorage.getItem("market_token") || "");
const state = ref({ item: null, actor: "", loading: false, deciding: "", error: "", status: 0 });
const api = axios.create({ baseURL: "/api" });
const session = createOIDCSession({ api, getContext: () => ({ requestId: requestId.value, token: localStorage.getItem("market_token") || "" }),
  onChange: value => { state.value = value; }, onNavigate: value => window.location.assign(value) });
function loginAgain() {
  session.reset();
  localStorage.removeItem("market_token");
  token.value = "";
  navigate(consoleLoginUrl(`/console/sso?request=${requestId.value}`));
}
function synchronize() {
  const latest = localStorage.getItem("market_token") || "";
  if (latest !== token.value) { token.value = latest; reload(); }
}
function reload() {
  session.reset();
  if (!requestId.value) return;
  if (!token.value) navigate(consoleLoginUrl(`/console/sso?request=${requestId.value}`));
  else session.load();
}
const scopeLabel = { openid: "确认平台用户身份", profile: "读取姓名", email: "读取邮箱与验证状态" };
let timer;
watch(() => props.search, reload);
onMounted(() => { reload(); window.addEventListener("storage", synchronize); timer = window.setInterval(synchronize, 500); });
onUnmounted(() => { session.dispose(); window.removeEventListener("storage", synchronize); window.clearInterval(timer); });
</script>

<template>
  <main class="oidc-page">
    <header class="oidc-header"><strong>market</strong><button class="text-btn" @click="navigate('/console')"><ArrowLeft :size="16" />返回工作台</button></header>
    <section class="oidc-consent" aria-labelledby="oidc-title">
      <ShieldCheck class="oidc-mark" :size="30" /><h1 id="oidc-title">应用访问授权</h1>
      <p v-if="!requestId" class="error-text" role="alert">授权请求无效，请从应用重新发起</p>
      <p v-else-if="state.loading" role="status">正在核对授权请求</p>
      <p v-if="state.error" class="error-text" role="alert">{{ state.error }}</p>
      <template v-if="state.item">
        <dl><div><dt>应用</dt><dd>{{ state.item.product_name }}</dd></div><div><dt>企业</dt><dd>{{ state.item.enterprise_name }}</dd></div><div><dt>当前账号</dt><dd>{{ state.actor || '已登录用户' }}</dd></div><div><dt>有效期至</dt><dd>{{ new Date(state.item.expires_at * 1000).toLocaleString('zh-CN', { hour12: false }) }}</dd></div></dl>
        <h2>请求授权范围</h2><ul><li v-for="scope in state.item.scopes" :key="scope">{{ scopeLabel[scope] }}</li></ul>
        <footer><button class="secondary-btn" :disabled="!!state.deciding" @click="session.decide('reject')"><X :size="16" />{{ state.deciding === 'reject' ? '正在拒绝' : '拒绝' }}</button><button class="primary-btn" :disabled="!!state.deciding" @click="session.decide('approve')"><Check :size="16" />{{ state.deciding === 'approve' ? '正在确认' : '同意授权' }}</button></footer>
      </template>
      <footer v-else-if="requestId && !state.loading">
        <button v-if="state.status !== 401" class="secondary-btn" @click="session.load()"><RefreshCw :size="16" />重试</button>
        <button class="primary-btn" @click="loginAgain"><LogIn :size="16" />重新登录</button>
      </footer>
    </section>
  </main>
</template>

<style scoped>
.oidc-page { min-height: 100dvh; background: #f7f8fa; padding: 24px; }
.oidc-header { max-width: 760px; margin: 0 auto; display: flex; justify-content: space-between; align-items: center; gap: 16px; }
.oidc-header strong { font-size: 22px; }
.oidc-consent { max-width: 560px; margin: 48px auto; background: #fff; border: 1px solid #e5e6eb; border-radius: 8px; padding: 28px; overflow-wrap: anywhere; }
.oidc-mark { color: #0f8b5f; }
h1 { font-size: 24px; margin: 16px 0 24px; }
h2 { font-size: 14px; margin: 24px 0 12px; }
dl { margin: 0; } dl > div { display: grid; grid-template-columns: 90px minmax(0, 1fr); gap: 12px; margin: 12px 0; }
dt { color: #86909c; font-size: 13px; } dd { margin: 0; font-size: 14px; }
ul { padding-left: 20px; font-size: 14px; line-height: 1.8; }
footer { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 12px; margin-top: 28px; }
button:disabled { opacity: .55; cursor: default; }
@media (max-width: 480px) { .oidc-page { padding: 16px; } .oidc-consent { padding: 20px; margin: 28px auto; } dl > div { grid-template-columns: minmax(0, 1fr); gap: 4px; } }
</style>
