import test from "node:test";
import assert from "node:assert/strict";
import { resolveRoute, safeReturnTarget, consoleLoginUrl } from "../src/routes.js";
import { canAccessConsoleView } from "../src/consolePermissions.js";
import { createOrder, explicitOrderPayload } from "../src/orderClient.js";

const origin = "https://market.example";
globalThis.window = { location: { origin } };

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
    for (const view of ["users", "products", "messages"]) assert.equal(canAccessConsoleView(user, null, view), true);
    for (const view of ["gateway", "sla", "audit", "settlements", "settings", "development"]) assert.equal(canAccessConsoleView(user, null, view), false);
  }
  assert.equal(canAccessConsoleView(null, null, "products"), false);
});

test("sensitive menus follow platform roles", () => {
  const user = (platform_role) => ({ verified_status: "verified", platform_role });
  assert.equal(canAccessConsoleView(user("finance_settlement"), null, "settlements"), true);
  assert.equal(canAccessConsoleView(user("finance_settlement"), null, "gateway"), false);
  assert.equal(canAccessConsoleView(user("delivery_monitor"), null, "delivery"), true);
  assert.equal(canAccessConsoleView(user("business_reviewer"), null, "audit"), false);
  assert.equal(canAccessConsoleView(user("super_admin"), null, "settings"), true);
  assert.equal(canAccessConsoleView({ ...user(""), enterprise_role: "enterprise_admin" }, { id: "e-1" }, "delivery"), true);
  assert.equal(canAccessConsoleView({ ...user(""), enterprise_role: "super_admin" }, { id: "e-1" }, "delivery"), true);
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
