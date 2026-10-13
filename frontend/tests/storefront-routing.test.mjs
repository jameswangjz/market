import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { resolveRoute, safeReturnTarget, consoleLoginUrl } from "../src/routes.js";
import { canAccessConsoleView, canManageProductRegistration } from "../src/consolePermissions.js";
import { createOrder, explicitOrderPayload } from "../src/orderClient.js";

const origin = "https://market.example";
globalThis.window = { location: { origin } };

test("subscription redirect uses only the server-returned purchaser enterprise", async () => {
  const source = readFileSync(new URL("../src/Storefront.vue", import.meta.url), "utf8");
  const start = source.indexOf("async function subscribe()");
  const end = source.indexOf("\n}", start) + 2;
  const paths = [];
  let order = { buyer_enterprise_id: "enterprise-B", unit_cost_snapshot: 99, secret: "private" };
  const context = vm.createContext({ URLSearchParams, canSubscribe: { value: true },
    selectedEnterprise: { value: "enterprise-A" }, purchase: { submit: async () => order },
    navigate: path => paths.push(path) });
  vm.runInContext(source.slice(start, end) + "\nthis.submit = subscribe;", context);
  await context.submit();
  const url = new URL(paths[0], origin);
  assert.equal(url.pathname, "/console");
  assert.deepEqual([...url.searchParams], [["view", "orders"], ["enterprise_id", "enterprise-B"]]);
  order = {};
  await context.submit();
  assert.equal(paths[1], "/console?view=orders");
  order = null;
  await context.submit();
  assert.equal(paths.length, 2);
});

test("public and console paths resolve independently, including direct detail refresh", () => {
  assert.deepEqual(resolveRoute("/"), { name: "storefront" });
  assert.deepEqual(resolveRoute("/console"), { name: "console" });
  assert.deepEqual(resolveRoute("/products/p-1"), { name: "product", id: "p-1" });
  for (const path of ["/missing", "/products/", "/products/%ZZ", "/products/a%2fb", "/products/a%5cb"]) {
    assert.equal(resolveRoute(path).name, "not-found");
  }
});

test("login return targets reject external, credentialed and malformed destinations", () => {
  for (const value of ["https://evil.example/", "//evil.example/", "\\evil.example", "javascript:alert(1)", "https://user@market.example/", "/api/files/secret", "/products/%2fsecret", "/\nevil.example", "https://market.example.evil/", ""]) {
    assert.equal(safeReturnTarget(value, origin), "/console", value);
  }
  assert.equal(safeReturnTarget(`${origin}/products/p-1?x=1`, origin), "/products/p-1?x=1");
  assert.equal(safeReturnTarget("/console?view=users&returnTo=https://evil.example", origin), "/console?view=users");
  const url = new URL(consoleLoginUrl("/products/p-1"), origin);
  assert.equal(url.pathname, "/console");
  assert.equal(url.searchParams.get("returnTo"), "/products/p-1");
  assert.equal(safeReturnTarget(url.pathname + url.search, origin, false), url.pathname + url.search);
});

test("verification and enterprise onboarding remain available without personal or enterprise verification", () => {
  for (const verified_status of ["unverified", "pending", "verified"]) {
    const user = { verified_status, platform_role: "" };
    for (const view of ["users", "messages"]) assert.equal(canAccessConsoleView(user, null, view), true);
    assert.equal(canAccessConsoleView(user, null, "products"), false);
    for (const view of ["gateway", "sla", "audit", "settlements", "settings", "development"]) assert.equal(canAccessConsoleView(user, null, view), false);
  }
  assert.equal(canAccessConsoleView(null, null, "products"), false);
});

test("enterprise products follow the selected active membership and registration role", () => {
  const user = { verified_status: "pending", platform_role: "", enterprise_memberships: [
    { enterprise_id: "a", role: "member", status: "active" },
    { enterprise_id: "b", role: "enterprise_admin", status: "active" },
    { enterprise_id: "disabled", role: "super_admin", status: "disabled" },
  ] };
  assert.equal(canAccessConsoleView(user, { id: "a" }, "products"), true);
  assert.equal(canAccessConsoleView(user, { id: "b" }, "products"), true);
  assert.equal(canAccessConsoleView(user, { id: "other" }, "products"), false);
  assert.equal(canManageProductRegistration(user, { id: "a" }), false);
  assert.equal(canManageProductRegistration(user, { id: "b" }), false);
  assert.equal(canManageProductRegistration({ ...user, verified_status: "verified" }, { id: "b" }), true);
  assert.equal(canManageProductRegistration(user, { id: "disabled" }), false);
});

test("enterprise overview is limited to its active super-admin membership", () => {
  const enterprise = { id: "selected" };
  const user = { verified_status: "verified", platform_role: "", enterprise_memberships: [
    { enterprise_id: "first", role: "super_admin", status: "active" },
    { enterprise_id: "selected", role: "enterprise_admin", status: "active" },
  ] };
  assert.equal(canAccessConsoleView(user, enterprise, "overview"), false);
  user.enterprise_memberships[1].role = "super_admin";
  assert.equal(canAccessConsoleView(user, enterprise, "overview"), true);
  assert.equal(canAccessConsoleView(user, { id: "first" }, "overview"), true);
});

test("sensitive menus follow platform roles", () => {
  const user = (platform_role) => ({ verified_status: "verified", platform_role });
  assert.equal(canAccessConsoleView(user("finance_settlement"), null, "settlements"), true);
  assert.equal(canAccessConsoleView(user("finance_settlement"), null, "gateway"), false);
  assert.equal(canAccessConsoleView(user("delivery_monitor"), null, "delivery"), true);
  assert.equal(canAccessConsoleView(user("business_reviewer"), null, "audit"), false);
  assert.equal(canAccessConsoleView(user("business_reviewer"), null, "orders"), false);
  assert.equal(canAccessConsoleView(user("quality_reviewer"), null, "products"), true);
  assert.equal(canAccessConsoleView(user("finance_settlement"), null, "products"), false);
  assert.equal(canAccessConsoleView(user("super_admin"), null, "settings"), true);
  const enterpriseUser = role => ({ ...user(""), enterprise_memberships: [{ enterprise_id: "e-1", role, status: "active" }] });
  assert.equal(canAccessConsoleView(enterpriseUser("enterprise_admin"), { id: "e-1" }, "delivery"), true);
  assert.equal(canAccessConsoleView(enterpriseUser("super_admin"), { id: "e-1" }, "delivery"), true);
  assert.equal(canAccessConsoleView({ ...user(""), enterprise_role: "super_admin" }, { id: "e-1" }, "delivery"), false);
  assert.equal(canAccessConsoleView({ ...user(""), enterprise_role: "member" }, { id: "e-1" }, "delivery"), false);
});

test("orders include the selected buyer enterprise and reject missing context before sending", async () => {
  const calls = [];
  const api = { post: async (...args) => { calls.push(args); } };
  assert.throws(() => createOrder(api, { product_id: "p-1" }), /请选择购买企业/);
  assert.throws(() => explicitOrderPayload({ buyer_enterprise_id: "" }, "active"), /请选择购买企业/);
  assert.throws(() => explicitOrderPayload({}, "default-first-enterprise"), /请选择购买企业/);
  assert.equal(calls.length, 0);
  await createOrder(api, { product_id: "p-1" }, "selected-enterprise");
  assert.deepEqual(calls[0], ["/orders", { product_id: "p-1", buyer_enterprise_id: "selected-enterprise" }]);
  assert.equal(explicitOrderPayload({ buyer_enterprise_id: "selected" }, "active").buyer_enterprise_id, "selected");
});
