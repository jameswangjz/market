import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { allowedBusinessViews, businessModes, businessViewParams, consoleEnterpriseSelection } from "../src/consoleBusinessView.js";

const enterprise = { id: "tenant-a" };
const admin = { enterprise_memberships: [{ enterprise_id: "tenant-a", role: "enterprise_admin", status: "active" }] };

test("active enterprise admins can switch business lens without business_roles metadata", () => {
  assert.deepEqual(businessModes(admin, enterprise), ["provider", "buyer"]);
  assert.ok(allowedBusinessViews(admin, enterprise, "buyer").includes("audit"));
  assert.ok(allowedBusinessViews(admin, enterprise, "buyer").includes("applications"));
  assert.ok(!allowedBusinessViews(admin, enterprise, "buyer").includes("products"));
  assert.ok(!allowedBusinessViews(admin, enterprise, "buyer").includes("overview"));
  assert.ok(!allowedBusinessViews(admin, enterprise, "provider").includes("overview"));
});

test("only selected enterprise superadmins get provider overview; ordinary membership cannot toggle privileges", () => {
  const owner = { enterprise_memberships: [{ enterprise_id: "tenant-a", role: "super_admin", status: "active" }] };
  const member = { enterprise_memberships: [{ enterprise_id: "tenant-a", role: "member", status: "active" }] };
  assert.ok(allowedBusinessViews(owner, enterprise, "provider").includes("overview"));
  assert.ok(!allowedBusinessViews(owner, enterprise, "buyer").includes("overview"));
  assert.deepEqual(businessModes(member, enterprise), []);
  assert.deepEqual(allowedBusinessViews(member, enterprise, "provider"), ["applications", "users", "messages"]);
});

test("business query context is explicit and platform roles ignore enterprise mode", () => {
  assert.deepEqual(businessViewParams(admin, enterprise, "provider"), { enterprise_id: "tenant-a", business_view: "provider" });
  assert.deepEqual(businessViewParams({ platform_role: "platform_operator" }, enterprise, "buyer"), {});
});

test("checkout enterprise selection uses only active memberships of the signed-in user", () => {
  const user = { enterprise_memberships: [
    { enterprise_id: "a", enterprise_name: "A", role: "member", status: "active" },
    { enterprise_id: "b", enterprise_name: "B", role: "enterprise_admin", status: "active" },
    { enterprise_id: "disabled", role: "super_admin", status: "disabled" },
  ] };
  const selection = consoleEnterpriseSelection(user, "b");
  assert.deepEqual(selection.enterprise, { id: "b", name: "B" });
  assert.equal(selection.membership.role, "enterprise_admin");
  assert.deepEqual(businessViewParams(user, selection.enterprise, "buyer"), { enterprise_id: "b", business_view: "buyer" });
  for (const id of ["", "outside", "disabled"]) assert.equal(consoleEnterpriseSelection(user, id), null);
  assert.equal(consoleEnterpriseSelection({ ...user, platform_role: "platform_operator" }, "b"), null);
  assert.equal(consoleEnterpriseSelection({ enterprise_memberships: [] }, "b"), null);
});

test("checkout redirect initializes buyer orders before loading for either active enterprise", async () => {
  const storefront = readFileSync(new URL("../src/Storefront.vue", import.meta.url), "utf8");
  const consoleSource = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
  for (const purchaser of ["a", "b"]) {
    let redirect = "";
    const checkout = vm.createContext({
      URLSearchParams, canSubscribe: { value: true },
      purchase: { submit: async () => ({ id: "new-order", buyer_enterprise_id: purchaser }) },
      navigate: url => { redirect = url; },
    });
    vm.runInContext(storefront.slice(storefront.indexOf("async function subscribe()"),
      storefront.indexOf("function applyFilters(")), checkout);
    await checkout.subscribe();
    const url = new URL(redirect, "https://market.invalid");
    assert.equal(url.searchParams.get("enterprise_id"), purchaser);
    assert.equal(url.searchParams.get("view"), "orders");
    const signedInUser = { id: "user", verified_status: "verified", platform_role: "",
      enterprise_memberships: ["a", "b"].map(id => ({ enterprise_id: id,
        enterprise_name: id.toUpperCase(), role: "enterprise_admin", status: "active" })) };
    const state = {
      URLSearchParams, window: { location: { search: url.search } },
      token: { value: "token" }, user: { value: null }, enterprise: { value: null },
      businessView: { value: "provider" }, activeView: { value: "overview" },
      showPersonalVerification: { value: false }, consoleEnterpriseSelection, businessModes,
      api: { defaults: { headers: { common: {} } }, get: async path => {
        assert.equal(path, "/auth/me");
        return { data: { user: signedInUser, enterprise: { id: "a", name: "A" }, role: "enterprise_admin" } };
      } },
      loadApplicationAddressPolicy: async () => {}, loadBuyerEnterprises: async () => {},
      loadNotificationSummary: async () => {},
      logout: () => assert.fail("session initialization unexpectedly failed"),
    };
    state.visibleNav = { get value() {
      return allowedBusinessViews(state.user.value, state.enterprise.value, state.businessView.value)
        .map(key => ({ key }));
    } };
    let loads = 0;
    state.refreshData = async () => {
      loads++;
      assert.equal(state.enterprise.value.id, purchaser);
      assert.equal(state.activeView.value, "orders");
      assert.equal(state.businessView.value, "buyer");
      assert.deepEqual(businessViewParams(state.user.value, state.enterprise.value, state.businessView.value),
        { enterprise_id: purchaser, business_view: "buyer" });
    };
    const session = vm.createContext(state);
    vm.runInContext(consoleSource.slice(consoleSource.indexOf("async function loadSession()"),
      consoleSource.indexOf("async function selectEnterprise(")), session);
    await session.loadSession();
    assert.equal(loads, 1);
  }
});

function providerProducts(api) {
  const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
  const state = vm.createContext({
    api, visibleNav: { value: [{ key: "products" }] }, businessContextEpoch: 1,
    user: { value: { id: "provider-admin", platform_role: "", enterprise_role: "enterprise_admin" } },
    enterprise: { value: { id: "provider" } }, token: { value: "provider-token" }, businessView: { value: "provider" },
    products: { value: [] }, productDirectories: { value: [] }, settlementRules: { value: [] }, productForm: { value: { category_id: "" } },
  });
  vm.runInContext(source.slice(source.indexOf("async function loadViewData(view)"), source.indexOf("async function createSlaProfile()")), state);
  return state;
}

test("provider product view uses authenticated read-only categories and retains the real category DTO", async () => {
  const calls = [];
  const product = { id: "provider-product", name: "Provider SaaS", category_id: "category-child" };
  const categories = [{ id: "category-parent", name: "Software", children: [
    { id: "category-child", parent_id: "category-parent", name: "SaaS", delivery_method: "tenant_access", status: "active" },
  ] }];
  const state = providerProducts({ get: async path => {
    calls.push(path);
    if (path.startsWith("/admin/")) throw { response: { status: 403 } };
    if (path === "/products") return { data: { items: [product] } };
    if (path === "/product-categories") return { data: { items: categories } };
    if (path === "/settlement-rules") return { data: { items: [] } };
    assert.fail(`Unexpected request: ${path}`);
  } });
  await state.loadViewData("products");
  assert.deepEqual(calls, ["/products", "/product-categories", "/settlement-rules"]);
  assert.equal(state.products.value[0], product);
  assert.deepEqual(JSON.parse(JSON.stringify(state.productDirectories.value)), [
    { value: "category-child", label: "Software · SaaS", catalog_name: "Software/SaaS" },
  ]);
  assert.equal(state.productForm.value.category_id, "category-child");
  const settingsSource = readFileSync(new URL("../src/ProductCategoriesSettings.vue", import.meta.url), "utf8");
  assert.match(settingsSource, /api\.get\("\/admin\/product-categories"\)/);
});

test("optional directory/rule permission and transport failures do not discard provider products", async () => {
  for (const error of [{ response: { status: 403, data: { detail: "Forbidden" } } }, new Error("network unavailable")]) {
    const product = { id: "provider-product" };
    const state = providerProducts({ get: async path => {
      if (path === "/products") return { data: { items: [product] } };
      throw error;
    } });
    await state.loadViewData("products");
    assert.equal(state.products.value[0], product);
    assert.equal(state.productDirectories.value.length, 0);
    assert.equal(state.settlementRules.value.length, 0);
    assert.equal(state.productForm.value.category_id, "");
  }
  const required = providerProducts({ get: async path => {
    if (path === "/products") throw new Error("product permission denied");
    return { data: { items: [] } };
  } });
  await assert.rejects(required.loadViewData("products"), /product permission denied/);
});

test("provider directory load cannot overwrite another enterprise after optional request failure", async () => {
  let resolveProducts;
  const pending = new Promise(resolve => { resolveProducts = resolve; });
  const state = providerProducts({ get: async path => {
    if (path === "/products") return pending;
    throw { response: { status: 403 } };
  } });
  const loading = state.loadViewData("products");
  state.enterprise.value = { id: "new-provider" };
  state.products.value = [{ id: "new-product" }];
  state.productDirectories.value = [{ value: "new-category" }];
  resolveProducts({ data: { items: [{ id: "old-product" }] } });
  await loading;
  assert.equal(state.products.value[0].id, "new-product");
  assert.equal(state.productDirectories.value[0].value, "new-category");
});
