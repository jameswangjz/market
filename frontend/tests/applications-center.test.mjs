import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { canViewApplications, canManageApplication, canAccessConsoleView, sanitizeApplications,
  sanitizeApplicationMembers, createApplicationsSession } from "../src/consolePermissions.js";

const user = { id: "u1", verified_status: "verified", activation_status: "active", platform_role: "", enterprise_memberships: [
  { enterprise_id: "e1", role: "member", status: "active" }, { enterprise_id: "e2", role: "enterprise_admin", status: "active" }] };
const app = { subscription_id: "s1", enterprise_id: "e1", product_id: "p1", enterprise_name: "Buyer", product_name: "App", provider_name: "Provider", version_code: "v1", expires_at: "2026-10-12T00:00:00Z", available: true, blocked_reason: "", is_enterprise_admin: false, logo_url: "" };
const managed = { ...app, subscription_id: "s2", enterprise_id: "e2", is_enterprise_admin: true };
const member = { user_id: "u2", name: "Member", login_name: "member", email: "member@example.invalid", role: "member", granted: false };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
function harness(enterpriseId = "") {
  const context = { user: structuredClone(user), enterprise: { id: enterpriseId }, token: "private-token" }, calls = [], redirects = [];
  let state;
  const api = { get: async (...args) => { calls.push(["GET", ...args]); const selected = args[1]?.params?.enterprise_id; return { data: { items: args[0] === "/applications" ? [app, managed].filter(item => !selected || item.enterprise_id === selected) : [member] } }; },
    post: async (...args) => { calls.push(["POST", ...args]); return { data: args[0].endsWith("/launch") ? { launch_url: "https://rp.example/login?enterprise_id=e1" } : { granted: args[1].granted } }; } };
  const session = createApplicationsSession({ api, getContext: () => context, onChange: value => { state = value; }, onNavigate: value => redirects.push(value) });
  return { context, calls, redirects, api, session, state: () => state };
}

test("verified ordinary members see applications; platform and inactive users do not", () => {
  assert.equal(canViewApplications(user), true); assert.equal(canAccessConsoleView(user, { id: "e1" }, "applications"), true);
  for (const changes of [{ platform_role: "super_admin" }, { platform_role: "finance_settlement" }, { verified_status: "pending" }, { is_active: false }, { activation_status: "pending_activation" }, { enterprise_memberships: [] }, { enterprise_memberships: [{ enterprise_id: "e1", role: "super_admin", status: "disabled" }] }]) assert.equal(canViewApplications({ ...user, ...changes }), false);
});

test("management requires real membership for item's enterprise and backend admin marker", () => {
  assert.equal(canManageApplication(user, app), false); assert.equal(canManageApplication(user, managed), true);
  assert.equal(canManageApplication({ ...user, enterprise_role: "super_admin" }, { ...app, is_enterprise_admin: true }), false);
  assert.equal(canManageApplication(user, { ...managed, is_enterprise_admin: false }), false);
  assert.equal(canManageApplication({ ...user, platform_role: "super_admin" }, managed), false);
});

test("list/member sanitizers strip cost/secret fields and reject cross-enterprise or ambiguous data", () => {
  assert.deepEqual(sanitizeApplications({ items: [{ ...app, client_secret: "private", cost: 10 }] }, user), [app]);
  assert.deepEqual(sanitizeApplicationMembers({ items: [{ ...member, password_hash: "private" }] }), [member]);
  for (const items of [[app, app], [{ ...app, enterprise_id: "other" }], [{ ...app, available: "true" }], [{ ...app, subscription_id: "../other" }]]) assert.throws(() => sanitizeApplications({ items }, user));
  assert.throws(() => sanitizeApplicationMembers({ items: [member, member] }));
});

test("launch is backend-only authenticated POST, blocked items and duplicate launch cannot run", async () => {
  const h = harness(); await h.session.load(); await h.session.launch("s1");
  assert.equal(h.calls.at(-1)[1], "/applications/s1/launch"); assert.equal(h.calls.at(-1)[2], undefined);
  assert.equal(h.calls.at(-1)[3].headers.Authorization, "Bearer private-token"); assert.equal(h.redirects.length, 1);
  const pending = deferred(); let posts = 0; h.api.post = () => { posts++; return pending.promise; };
  const first = h.session.launch("s1"); await h.session.launch("s2"); assert.equal(posts, 1);
  pending.resolve({ data: { launch_url: "javascript:alert(1)" } }); await first; assert.equal(h.redirects.length, 1); assert.ok(h.state().error);
  h.api.get = async () => ({ data: { items: [{ ...app, available: false }] } }); await h.session.load(); await h.session.launch("s1"); assert.equal(posts, 1);
});

test("ordinary member cannot call management endpoints even by direct helper invocation", async () => {
  const h = harness(); await h.session.load(); const count = h.calls.length;
  await h.session.openMembers("s1"); await h.session.grant("u2", true); assert.equal(h.calls.length, count);
});

test("admin grants send only user_id/bool and reload authoritative member state", async () => {
  const h = harness(); await h.session.load(); await h.session.openMembers("s2"); await h.session.grant("u2", true);
  assert.deepEqual(h.calls.find(c => c[0] === "POST")[2], { user_id: "u2", granted: true });
  assert.equal(h.calls.at(-1)[1], "/applications/s2/members"); assert.equal(h.state().saving, "");
  assert.equal(JSON.stringify(h.state()).includes("private-token"), false);
});

test("stale list, member, launch and grant results cannot affect another context", async () => {
  for (const phase of ["list", "members", "launch", "grant"]) {
    const h = harness(), pending = deferred();
    if (phase !== "list") await h.session.load();
    if (phase === "grant") await h.session.openMembers("s2");
    if (["list", "members"].includes(phase)) h.api.get = () => pending.promise; else h.api.post = () => pending.promise;
    const request = phase === "list" ? h.session.load() : phase === "members" ? h.session.openMembers("s2") : phase === "launch" ? h.session.launch("s1") : h.session.grant("u2", true);
    h.context.token = "new-account"; h.session.reset(); pending.resolve({ data: { items: [app], launch_url: "https://rp.example/", granted: true } }); await request;
    assert.deepEqual(h.state().items, []); assert.deepEqual(h.state().members, []); assert.equal(h.state().modal, null); assert.equal(h.redirects.length, 0);
  }
});

test("closing modal discards an old member load without releasing a newer one", async () => {
  const h = harness(); await h.session.load(); const old = deferred(), newer = deferred();
  h.api.get = () => old.promise; const first = h.session.openMembers("s2"); h.session.closeMembers();
  h.api.get = () => newer.promise; const second = h.session.openMembers("s2"); old.resolve({ data: { items: [member] } }); await first;
  assert.equal(h.state().memberLoading, true); assert.deepEqual(h.state().members, []);
  newer.resolve({ data: { items: [member] } }); await second; assert.equal(h.state().memberLoading, false);
});

test("errors are generic, retries work, and disposed session cannot launch", async () => {
  for (const status of [401, 403, 409]) {
    const h = harness(); h.api.get = async () => { throw { response: { status, data: { detail: "private" } } }; };
    await h.session.load(); assert.ok(h.state().error); assert.equal(h.state().error.includes("private"), false);
    h.api.get = async () => ({ data: { items: [app] } }); await h.session.load(); assert.equal(h.state().error, "");
    h.session.dispose(); await h.session.launch("s1"); assert.equal(h.redirects.length, 0);
  }
});

test("application UI has no API credentials/download entry and uses checkbox grants", async () => {
  const source = await readFile(new URL("../src/ApplicationsCenter.vue", import.meta.url), "utf8");
  assert.match(source, /type="checkbox"/); assert.match(source, /canManageApplication/);
  assert.match(source, /event\.target\.checked = member\.granted/);
  assert.doesNotMatch(source, /api-credentials|delivery-access|client_secret|localStorage\.setItem|window\.open/);
});

test("membership downgrade and enterprise selection changes invalidate pending launches", async () => {
  for (const change of [h => { h.context.enterprise.id = "e2"; }, h => { h.context.user.enterprise_memberships[0].status = "disabled"; }, h => { h.context.user.platform_role = "super_admin"; }]) {
    const h = harness(), pending = deferred(); await h.session.load(); h.api.post = () => pending.promise;
    const request = h.session.launch("s1"); change(h); h.session.reset();
    pending.resolve({ data: { launch_url: "https://rp.example/login" } }); await request; assert.equal(h.redirects.length, 0);
  }
});

test("list refresh nonce keeps newest result after stale success or error", async () => {
  for (const fail of [false, true]) {
    const h = harness(), pending = deferred(); h.api.get = () => pending.promise; const first = h.session.load();
    h.api.get = async () => ({ data: { items: [managed] } }); await h.session.load();
    if (fail) pending.reject({ response: { status: 403 } }); else pending.resolve({ data: { items: [app] } });
    await first; assert.deepEqual(h.state().items, [managed]); assert.equal(h.state().error, "");
  }
});

test("grant error preserves authoritative checkboxes and cannot release a newer modal submission", async () => {
  const h = harness(), old = deferred(), newer = deferred(); await h.session.load(); await h.session.openMembers("s2");
  h.api.post = () => old.promise; const first = h.session.grant("u2", true);
  h.session.closeMembers(); await h.session.openMembers("s2"); h.api.post = () => newer.promise; const second = h.session.grant("u2", true);
  old.reject({ response: { status: 403, data: { detail: "private" } } }); await first;
  assert.equal(h.state().saving, "u2"); assert.equal(h.state().memberError, ""); assert.equal(h.state().members[0].granted, false);
  newer.resolve({ data: { user_id: "another-user", granted: true } }); await second;
  assert.equal(h.state().saving, ""); assert.ok(h.state().memberError); assert.equal(h.state().members[0].granted, false);
});

test("enterprise admins cannot be changed as ordinary grants", async () => {
  const h = harness(); await h.session.load(); h.api.get = async () => ({ data: { items: [{ ...member, role: "enterprise_admin", granted: true }] } });
  await h.session.openMembers("s2"); const calls = h.calls.length; await h.session.grant("u2", false); assert.equal(h.calls.length, calls);
});

test("list sends selected enterprise_id and keeps legacy unfiltered requests without selection", async () => {
  for (const enterpriseId of ["e1", "e2", ""]) {
    const h = harness(enterpriseId);
    await h.session.load();
    const [method, path, config] = h.calls[0];
    assert.equal(method, "GET");
    assert.equal(path, "/applications");
    assert.deepEqual(config.params, enterpriseId ? { enterprise_id: enterpriseId } : undefined);
    assert.equal(config.headers.Authorization, "Bearer private-token");
    assert.equal(config.withCredentials, true);
    assert.deepEqual(h.state().items.map(item => item.enterprise_id), enterpriseId ? [enterpriseId] : ["e1", "e2"]);
  }
});

test("enterprise switch discards stale list success/error without clearing the new pending list", async () => {
  for (const fail of [false, true]) {
    const h = harness("e1"), old = deferred(), newer = deferred(), queries = [];
    h.api.get = (_path, config) => { queries.push(config.params); return config.params.enterprise_id === "e1" ? old.promise : newer.promise; };
    const first = h.session.load();
    h.context.enterprise = { id: "e2" };
    h.session.reset();
    const second = h.session.load();
    if (fail) old.reject({ response: { status: 403 } }); else old.resolve({ data: { items: [app] } });
    await first;
    assert.equal(h.state().loading, true);
    assert.deepEqual(h.state().items, []);
    assert.equal(h.state().error, "");
    newer.resolve({ data: { items: [managed] } });
    await second;
    assert.deepEqual(queries, [{ enterprise_id: "e1" }, { enterprise_id: "e2" }]);
    assert.deepEqual(h.state().items, [managed]);
    assert.equal(h.state().loading, false);
    assert.equal(h.state().error, "");
  }
});

test("selected list rejects another enterprise even when both memberships are active", async () => {
  const h = harness("e1");
  h.api.get = async () => ({ data: { items: [managed] } });
  await h.session.load();
  assert.deepEqual(h.state().items, []);
  assert.ok(h.state().error);
  await h.session.launch("s2");
  assert.deepEqual(h.redirects, []);
});

test("granted ordinary member lists and launches without order ownership or admin privileges", async () => {
  const h = harness("e1");
  assert.equal(h.context.user.enterprise_memberships[0].role, "member");
  assert.equal("order_id" in app, false);
  await h.session.load();
  assert.deepEqual(h.state().items, [app]);
  await h.session.launch("s1");
  assert.equal(h.redirects.length, 1);
  assert.equal(h.calls.at(-1)[1], "/applications/s1/launch");
  const count = h.calls.length;
  await h.session.openMembers("s1");
  await h.session.grant("u2", true);
  assert.equal(h.calls.length, count);
});

test("selected enterprise admin can grant; switching selection blocks cached launch and grants", async () => {
  const h = harness("e2");
  await h.session.load();
  await h.session.openMembers("s2");
  await h.session.grant("u2", true);
  assert.deepEqual(h.calls.find(call => call[1] === "/applications/s2/grants")[2], { user_id: "u2", granted: true });
  h.context.enterprise = { id: "e1" };
  const count = h.calls.length;
  await h.session.launch("s2");
  await h.session.loadMembers();
  await h.session.openMembers("s2");
  await h.session.grant("u2", false);
  assert.equal(h.calls.length, count);
  assert.deepEqual(h.redirects, []);
});

test("component watches the selected enterprise context synchronously", async () => {
  const source = await readFile(new URL("../src/ApplicationsCenter.vue", import.meta.url), "utf8");
  assert.match(source, /enterprise: props.enterprise/);
  assert.match(source, /watch\(\(\) => JSON.stringify\(context\(\)\), \(\) => \{ session.reset\(\);.*session.load\(\); \}, \{ flush: "sync" \}\)/);
});
