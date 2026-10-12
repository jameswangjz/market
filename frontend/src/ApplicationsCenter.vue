<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { ExternalLink, Users, RefreshCw, X, LogIn, AppWindow } from "lucide-vue-next";
import { canViewApplications, canManageApplication, createApplicationsSession } from "./consolePermissions.js";
import { safeApplicationUrl } from "./routes.js";

const props = defineProps({ user: { type: Object, required: true }, enterprise: { type: Object, default: null },
  token: { type: String, required: true }, api: { type: Object, required: true } });
const state = ref({ items: [], loading: false, error: "", launching: "", modal: null, members: [], memberLoading: false, memberError: "", saving: "" });
const allowed = computed(() => canViewApplications(props.user));
const context = () => {
  const storedToken = localStorage.getItem("market_token") || "";
  return { user: props.token === storedToken ? props.user : null, enterprise: props.enterprise, token: props.token, storedToken };
};
const session = createApplicationsSession({ api: props.api, getContext: context, onChange: value => { state.value = value; },
  onNavigate: target => window.location.assign(target) });
const modalElement = ref(null);
let triggerElement = null;
function logo(url) {
  try {
    const value = new URL(url, window.location.origin);
    return safeApplicationUrl(value.href);
  } catch { return ""; }
}
function fmtDate(value) {
  return Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "-";
}
async function openMembers(id) {
  triggerElement = document.activeElement;
  const request = session.openMembers(id);
  await nextTick(); modalElement.value?.focus();
  await request;
}
function closeMembers() { session.closeMembers(); triggerElement?.focus(); triggerElement = null; }
function changeGrant(member, event) {
  const granted = event.target.checked;
  event.target.checked = member.granted;
  session.grant(member.user_id, granted);
}
function trapFocus(event) {
  if (event.key !== "Tab") return;
  const elements = [...modalElement.value.querySelectorAll('button:not(:disabled), input:not(:disabled), [tabindex="0"]')];
  if (!elements.length) { event.preventDefault(); return; }
  const first = elements[0], last = elements.at(-1);
  if (event.shiftKey && (document.activeElement === first || document.activeElement === modalElement.value)) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}
function loginAgain() { localStorage.removeItem("market_token"); window.location.assign("/console?view=applications"); }
watch(() => JSON.stringify(context()), () => { session.reset(); if (allowed.value && props.token === localStorage.getItem("market_token")) session.load(); }, { flush: "sync" });
onMounted(() => session.load());
onUnmounted(() => session.dispose());
</script>

<template>
  <section v-if="allowed" class="applications-center" aria-labelledby="applications-title">
    <header class="page-heading"><h1 id="applications-title">我的应用</h1><button class="secondary-btn" :disabled="state.loading" @click="session.load"><RefreshCw :size="16" />刷新</button></header>
    <div v-if="state.error" class="applications-error" role="alert"><span>{{ state.error }}</span><button v-if="state.error.includes('重新登录')" class="text-btn" @click="loginAgain"><LogIn :size="15" />重新登录</button><button v-else class="text-btn" @click="session.load"><RefreshCw :size="15" />重试</button></div>
    <p v-if="state.loading" class="muted" role="status">正在加载应用</p>
    <p v-else-if="!state.error && !state.items.length" class="applications-empty">暂无获授权的应用</p>
    <div v-else class="applications-list">
      <article v-for="item in state.items" :key="item.subscription_id" class="application-row">
        <div class="application-logo"><img v-if="logo(item.logo_url) && item.logo_url" :src="logo(item.logo_url)" alt="" referrerpolicy="no-referrer" @error="$event.target.style.display = 'none'" /><AppWindow v-else :size="24" /></div>
        <div class="application-description"><h2>{{ item.product_name }}</h2><p>{{ item.enterprise_name }} · {{ item.provider_name }}</p><dl><div><dt>版本</dt><dd>{{ item.version_code || '-' }}</dd></div><div><dt>有效期至</dt><dd>{{ fmtDate(item.expires_at) }}</dd></div></dl><p v-if="!item.available" class="application-blocked">{{ item.blocked_reason || '暂不可访问' }}</p></div>
        <div class="application-actions"><button v-if="canManageApplication(user, item)" class="secondary-btn" @click="openMembers(item.subscription_id)"><Users :size="16" />成员授权</button><button class="primary-btn" :disabled="!item.available || !!state.launching" @click="session.launch(item.subscription_id)"><ExternalLink :size="16" />{{ state.launching === item.subscription_id ? '正在打开' : '访问应用' }}</button></div>
      </article>
    </div>
    <div v-if="state.modal && canManageApplication(user, state.modal)" class="modal-scrim" @click.self="closeMembers">
      <section ref="modalElement" class="modal-card application-members-modal" role="dialog" aria-modal="true" aria-labelledby="application-members-title" tabindex="-1" @keydown.esc="closeMembers" @keydown="trapFocus">
        <header class="drawer-head"><div><h2 id="application-members-title">成员授权</h2><p>{{ state.modal.product_name }} · {{ state.modal.enterprise_name }}</p></div><button class="icon-btn" title="关闭成员授权" aria-label="关闭成员授权" @click="closeMembers"><X :size="18" /></button></header>
        <div v-if="state.memberError" class="applications-error" role="alert"><span>{{ state.memberError }}</span><button v-if="state.memberError.includes('重新登录')" class="text-btn" @click="loginAgain"><LogIn :size="15" />重新登录</button><button v-else class="text-btn" :disabled="!!state.saving" @click="session.loadMembers"><RefreshCw :size="15" />重试</button></div>
        <p v-if="state.memberLoading" class="muted" role="status">正在加载成员</p>
        <p v-else-if="!state.members.length && !state.memberError" class="muted">暂无可授权成员</p>
        <div v-else class="application-members-list"><label v-for="member in state.members" :key="member.user_id" class="application-member"><div><strong>{{ member.name || member.login_name }}</strong><span>{{ member.login_name || member.email }}</span><small>{{ ['super_admin', 'enterprise_admin'].includes(member.role) ? '企业管理员' : '企业成员' }}</small></div><span v-if="['super_admin', 'enterprise_admin'].includes(member.role)" class="muted">管理员可访问</span><input v-else type="checkbox" :aria-label="`授权 ${member.name || member.login_name} 访问应用`" :checked="member.granted" :disabled="!!state.saving || state.memberLoading" @change="changeGrant(member, $event)" /></label></div>
        <footer class="modal-actions"><button class="secondary-btn" :disabled="state.memberLoading || !!state.saving" @click="session.loadMembers"><RefreshCw :size="16" />刷新成员</button><button class="secondary-btn" @click="closeMembers"><X :size="16" />关闭</button></footer>
      </section>
    </div>
  </section>
  <p v-else class="error-text" role="alert">当前账号不可查看应用</p>
</template>

<style scoped>
.applications-center { min-width: 0; }
.page-heading h1 { margin: 0; font-size: 24px; }
.applications-list { border-top: 1px solid #e5e6eb; }
.application-row { display: flex; align-items: flex-start; gap: 18px; padding: 24px 0; border-bottom: 1px solid #e5e6eb; }
.application-logo { width: 48px; height: 48px; flex: 0 0 48px; display: grid; place-items: center; background: #e9f6f0; color: #0f8b5f; border-radius: 8px; overflow: hidden; }
.application-logo img { width: 100%; height: 100%; object-fit: contain; }
.application-description { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.application-description h2 { margin: 0 0 8px; font-size: 16px; }
.application-description p { margin: 0 0 12px; font-size: 12px; color: #86909c; }
.application-description dl { display: flex; flex-wrap: wrap; gap: 12px 28px; margin: 0; font-size: 12px; }
.application-description dl > div { display: flex; flex-wrap: wrap; gap: 8px; min-width: 0; }
.application-description dt { color: #86909c; } .application-description dd { margin: 0; }
.application-description .application-blocked { color: #b45309; margin-top: 12px; }
.application-actions { display: flex; flex-wrap: wrap; gap: 8px; flex-shrink: 0; }
.applications-error { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; color: #c0392b; padding: 12px 0; font-size: 13px; overflow-wrap: anywhere; }
.applications-empty { padding: 60px 0; text-align: center; color: #86909c; }
.application-members-modal { width: min(620px, calc(100vw - 32px)); max-height: calc(100dvh - 32px); overflow-y: auto; border-radius: 8px; }
.application-members-modal .drawer-head > div { min-width: 0; overflow-wrap: anywhere; }
.application-members-modal .drawer-head .icon-btn { flex-shrink: 0; }
.application-member { display: flex; flex-direction: row; align-items: center; gap: 16px; padding: 12px 0; border-bottom: 1px solid #e5e6eb; }
.application-member > div { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.application-member strong, .application-member span, .application-member small { display: block; font-size: 13px; }
.application-member small, .application-member div span { color: #86909c; margin-top: 4px; font-size: 12px; }
.application-member input { width: 18px; height: 18px; flex: 0 0 18px; padding: 0; margin: 0; accent-color: #ff6a00; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
button:disabled { opacity: .55; cursor: default; }
@media (max-width: 760px) { .application-row { flex-wrap: wrap; gap: 12px; } .application-description { flex-basis: calc(100% - 60px); } .application-actions { width: 100%; padding-left: 60px; } }
@media (max-width: 480px) { .application-actions { padding-left: 0; } .application-members-modal { padding: 20px; } }
</style>
