import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { compile, computed, createSSRApp, h, nextTick, onMounted, onServerPrefetch, onUnmounted, ref, watch } from "vue";
import { renderToString } from "@vue/server-renderer";
import { ExternalLink, Users, RefreshCw, X, LogIn, AppWindow } from "lucide-vue-next";
import { canViewApplications, canManageApplication, createApplicationsSession } from "../src/consolePermissions.js";
import { safeApplicationUrl } from "../src/routes.js";

const source = readFileSync(new URL("../src/ApplicationsCenter.vue", import.meta.url), "utf8");
const script = source.split("<script setup>")[1].split("</script>")[0].replace(/^import .*;$/gm, "");
const template = source.split("<template>")[1].split("</template>")[0];
const user = { id: "isolated-member", platform_role: "", verified_status: "verified", activation_status: "active",
  enterprise_memberships: ["e1", "e2"].map(enterprise_id => ({ enterprise_id, role: "member", status: "active" })) };
const application = (enterprise_id = "e1", changes = {}) => ({ subscription_id: `s-${enterprise_id}`, product_id: "isolated-app",
  enterprise_id, product_name: "Isolated application", enterprise_name: enterprise_id, provider_name: "Fixture provider",
  version_code: "v1", expires_at: "2026-10-15T00:00:00Z", logo_url: "", available: true,
  blocked_reason: "", is_enterprise_admin: false, ...changes });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

async function renderApplication(item) {
  const calls = []; let session;
  const api = { get: async (path, config) => { calls.push({ method: "GET", path, params: config.params }); return { data: { items: [item] } }; },
    post: async () => { calls.push({ method: "POST" }); throw new Error("No fixture writes permitted"); } };
  const component = {
    props: ["user", "enterprise", "token", "api"],
    components: { ExternalLink, Users, RefreshCw, X, LogIn, AppWindow },
    setup(props) {
      const context = vm.createContext({ computed, nextTick, onMounted, onUnmounted, ref, watch, URL,
        canViewApplications, canManageApplication, createApplicationsSession, safeApplicationUrl,
        defineProps: () => props, localStorage: { getItem: () => "isolated-token" },
        window: { location: { origin: "https://fixture.invalid", assign: () => { throw new Error("No fixture navigation"); } } } });
      vm.runInContext(script + "\nthis.exposed = { state, allowed, session, logo, fmtDate, canManageApplication, openMembers, closeMembers, changeGrant, trapFocus, loginAgain, modalElement };", context);
      session = context.exposed.session;
      // SSR does not run onMounted; load through the real session before rendering.
      onServerPrefetch(() => session.load());
      return context.exposed;
    },
    render: compile(template),
  };
  const html = await renderToString(createSSRApp({ render: () => h(component,
    { user, enterprise: { id: item.enterprise_id }, token: "isolated-token", api }) }));
  return { html, calls, session };
}

test("real ApplicationsCenter SSR shows unavailable reasons and disables ordinary-member access", async () => {
  for (const reason of ["订阅已到期", "授权已撤销", "应用已停用", ""]) {
    const item = application("e1", { available: false, blocked_reason: reason });
    const result = await renderApplication(item);
    assert.ok(result.html.includes(reason || "暂不可访问"));
    assert.match(result.html, /class="application-blocked"/);
    assert.match(result.html, /<button[^>]*class="primary-btn"[^>]*disabled/);
    assert.doesNotMatch(result.html, /成员授权/);
    assert.deepEqual(result.calls[0], { method: "GET", path: "/applications", params: { enterprise_id: "e1" } });
    await result.session.launch(item.subscription_id);
    assert.equal(result.calls.some(call => call.method === "POST"), false);
    result.session.dispose();
  }
});

test("real ApplicationsCenter SSR escapes reason text and leaves available access enabled", async () => {
  const blocked = await renderApplication(application("e1", { available: false, blocked_reason: "<script>fixture-only</script>" }));
  assert.ok(blocked.html.includes("&lt;script&gt;fixture-only&lt;/script&gt;"));
  assert.doesNotMatch(blocked.html, /<script>/);
  blocked.session.dispose();
  const available = await renderApplication(application());
  assert.match(available.html, /class="primary-btn"/);
  assert.doesNotMatch(available.html, /<button[^>]*class="primary-btn"[^>]*disabled/);
  assert.doesNotMatch(available.html, /class="application-blocked"|成员授权/);
  available.session.dispose();
});

test("two ordinary memberships switch current enterprise and discard stale list success/error", async () => {
  for (const fail of [false, true]) {
    const context = { user: structuredClone(user), enterprise: { id: "e1" }, token: "isolated-token" };
    const old = deferred(), current = deferred(), calls = []; let state;
    const api = { get: (path, config) => { calls.push({ path, params: config.params }); return config.params.enterprise_id === "e1" ? old.promise : current.promise; } };
    const session = createApplicationsSession({ api, getContext: () => context, onChange: value => { state = value; }, onNavigate: () => assert.fail("No fixture navigation") });
    const first = session.load();
    context.enterprise = { id: "e2" }; session.reset();
    const second = session.load();
    if (fail) old.reject({ response: { status: 403 } }); else old.resolve({ data: { items: [application("e1")] } });
    await first;
    assert.equal(state.loading, true); assert.deepEqual(state.items, []); assert.equal(state.error, "");
    current.resolve({ data: { items: [application("e2")] } }); await second;
    assert.deepEqual(calls, [{ path: "/applications", params: { enterprise_id: "e1" } }, { path: "/applications", params: { enterprise_id: "e2" } }]);
    assert.deepEqual(state.items.map(item => item.enterprise_id), ["e2"]);
    assert.equal(state.loading, false); assert.equal(state.error, "");
    assert.equal(canManageApplication(context.user, state.items[0]), false);
    assert.ok(context.user.enterprise_memberships.every(member => member.role === "member"));
    session.dispose();
  }
});
