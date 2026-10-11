import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { oidcRequestId, resolveRoute, safeReturnTarget, safeApplicationUrl, consoleLoginUrl } from "../src/routes.js";

const source = await readFile(new URL("../src/OIDCAuthorization.vue", import.meta.url), "utf8");
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1].replace('"./routes.js"', JSON.stringify(new URL("../src/routes.js", import.meta.url).href));
const { sanitizeOIDCRequest, createOIDCSession } = await import(`data:text/javascript;base64,${Buffer.from(script).toString("base64")}`);
const id = "a".repeat(64), origin = "https://market.example";
globalThis.window = { location: { origin } };
const item = { request_id: id, product_name: "Application", enterprise_name: "Buyer", enterprise_id: "e1", scopes: ["openid", "email"], expires_at: 1791720000 };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
function harness() {
  const context = { requestId: id, token: "private-session" }, calls = [], redirects = [];
  let state;
  const api = { get: async (path, config) => { calls.push(["GET", path, config]); return { data: path === "/auth/me" ? { user: { name: "Buyer" } } : item }; },
    post: async (...args) => { calls.push(["POST", ...args]); return { data: { redirect_url: "https://rp.example/callback?code=single-use&state=opaque" } }; } };
  const session = createOIDCSession({ api, getContext: () => context, onChange: value => { state = value; }, onNavigate: value => redirects.push(value) });
  return { context, api, calls, redirects, session, state: () => state };
}

test("SSO route consumes only one 64hex request ID and refuses OAuth query/duplicates", () => {
  assert.equal(resolveRoute("/console/sso").name, "sso");
  assert.equal(oidcRequestId(`?request=${id}`), id);
  for (const query of [`?request=${id}&request=${id}`, `?request=${id}&client_id=rp`, "?request=short", `?request=${"A".repeat(64)}`, "?redirect_uri=https://evil.example"]) {
    assert.equal(oidcRequestId(query), "");
    assert.equal(safeReturnTarget(`/console/sso${query}`, origin), "/console");
  }
  assert.equal(safeReturnTarget(`/console/sso?request=${id}#fragment`, origin), "/console");
  assert.equal(safeReturnTarget(`https://evil.example/console/sso?request=${id}`, origin), "/console");
  assert.equal(new URL(consoleLoginUrl(`/console/sso?request=${id}`), origin).searchParams.get("returnTo"), `/console/sso?request=${id}`);
});

test("external navigation rejects unsafe protocols, credentials, whitespace and relative input", () => {
  for (const url of ["javascript:alert(1)", "data:text/html,x", "/callback", "//evil.example", "https://user:secret@evil.example/", "https://rp.example/\\x", "https://rp.example/\nx", ""]) assert.throws(() => safeApplicationUrl(url));
  assert.equal(safeApplicationUrl("https://rp.example/callback?code=x&state=y"), "https://rp.example/callback?code=x&state=y");
  assert.equal(safeApplicationUrl("http://localhost:8001/login"), "http://localhost:8001/login");
});

test("consent sanitization whitelists public fields and does not misjudge VM clock skew", () => {
  assert.deepEqual(sanitizeOIDCRequest({ ...item, secret: "hidden", token: "hidden", total_cost: 123 }, id), item);
  for (const changes of [{ request_id: "b".repeat(64) }, { scopes: ["resource.invoke"] }, { scopes: "openid" }, { expires_at: "invalid" }, { expires_at: String(item.expires_at) }, { expires_at: 0 }, { expires_at: 1.5 }]) assert.throws(() => sanitizeOIDCRequest({ ...item, ...changes }, id));
});

test("consent loads via authenticated same-origin cookie and approves/rejects without a body", async () => {
  for (const decision of ["approve", "reject"]) {
    const h = harness(); await h.session.load();
    assert.equal(h.state().item.request_id, id);
    assert.equal(h.calls[1][1], `/oidc/browser/${id}`);
    assert.equal(h.calls[1][2].withCredentials, true);
    assert.equal(Object.keys(h.calls[1][2].headers).some(key => key.toLowerCase() === "origin"), false);
    assert.equal(h.calls[1][2].headers.Authorization, "Bearer private-session");
    await h.session.decide(decision);
    assert.equal(h.calls[2][1], `/oidc/browser/${id}/${decision}`);
    assert.equal(h.calls[2][2], undefined);
    assert.equal(h.redirects.length, 1);
    assert.equal(JSON.stringify(h.state()).includes("private-session"), false);
  }
});

test("refresh epoch and changed transaction suppress stale detail errors", async () => {
  const h = harness(), pending = deferred();
  const original = h.api.get;
  h.api.get = path => path === "/auth/me" ? original(path) : pending.promise;
  const first = h.session.load(); await Promise.resolve(); await Promise.resolve();
  h.context.requestId = "b".repeat(64);
  h.api.get = async path => ({ data: path === "/auth/me" ? { user: { name: "New account" } } : { ...item, request_id: h.context.requestId } });
  await h.session.load(); pending.reject({ response: { status: 410 } }); await first;
  assert.equal(h.state().item.request_id, h.context.requestId); assert.equal(h.state().error, "");
});

test("missing login or malformed request never calls an API", async () => {
  for (const changes of [{ token: "" }, { requestId: "invalid" }]) {
    const h = harness(); Object.assign(h.context, changes); await h.session.load(); assert.equal(h.calls.length, 0);
  }
});

test("401/403/409/410 failures clear consent and expose no raw server secrets", async () => {
  for (const status of [401, 403, 409, 410]) {
    const h = harness(); h.api.get = async () => { throw { response: { status, data: { detail: "private-secret" } } }; };
    await h.session.load(); assert.equal(h.state().item, null); assert.equal(h.state().status, status);
    assert.equal(h.state().error.includes("private-secret"), false);
    assert.match(h.state().error, status === 401 ? /重新登录/ : status === 403 ? /无权/ : /过期/);
  }
});

test("account/request changes discard stale details and redirects, including disposed sessions", async () => {
  for (const phase of ["load", "approve", "reject"]) {
    const h = harness(), pending = deferred();
    if (phase === "load") h.api.get = () => pending.promise;
    else { await h.session.load(); h.api.post = () => pending.promise; }
    const request = phase === "load" ? h.session.load() : h.session.decide(phase);
    h.context.token = "different-account"; h.session.reset();
    pending.resolve({ data: phase === "load" ? { user: { name: "Old user" } } : { redirect_url: "https://rp.example/callback" } });
    await request; assert.equal(h.state().item, null); assert.equal(h.state().actor, ""); assert.equal(h.redirects.length, 0);
  }
  const h = harness(); await h.session.load(); h.session.dispose(); await h.session.decide("approve"); assert.equal(h.redirects.length, 0);
});

test("duplicate decision is locked and unsafe backend URL never navigates", async () => {
  const h = harness(), pending = deferred(); await h.session.load(); let count = 0;
  h.api.post = () => { count++; return pending.promise; };
  const first = h.session.decide("approve"); await h.session.decide("reject"); assert.equal(count, 1);
  pending.resolve({ data: { redirect_url: "javascript:alert(1)" } }); await first;
  assert.equal(h.redirects.length, 0); assert.equal(h.state().item, null); assert.ok(h.state().error);
});

test("UI provides explicit consent, retry and login without copying OAuth secrets to storage", () => {
  assert.match(source, /session\.decide\('approve'\)/); assert.match(source, /session\.decide\('reject'\)/);
  assert.match(source, /重新登录/); assert.match(source, /localStorage\.getItem\("market_token"\)/);
  assert.doesNotMatch(source, /localStorage\.setItem|sessionStorage|searchParams\.get\("(?:token|code|client_secret)"\)/);
});
