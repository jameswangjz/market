import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { nextTick, ref, watch } from "vue";
import { canAccessConsoleView } from "../src/consolePermissions.js";
import { allowedBusinessViews } from "../src/consoleBusinessView.js";
import { deliveryForProductType, registrationProductTypes } from "../src/productRegistration.js";
import { pageCount, pageSizeValue } from "../src/pagination.js";

const consoleSource = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
const storeSource = readFileSync(new URL("../src/Storefront.vue", import.meta.url), "utf8");

test("development access excludes every non-system-admin identity", () => {
  for (const platform_role of ["super_admin", "platform_operator", "finance_settlement", "security_compliance", "product_manager", "quality_reviewer", "delivery_monitor", ""]) {
    const user = { platform_role, verified_status: "verified", enterprise_memberships: [{ enterprise_id: "e", role: "super_admin", status: "active" }] };
    assert.equal(canAccessConsoleView(user, { id: "e" }, "development"), platform_role === "super_admin", platform_role);
  }
  assert.match(consoleSource, /if \(\(view === "development" \|\| view === "overview"\) && canAccessConsoleView\(user.value, enterprise.value, "development"\)\)/);
  assert.match(consoleSource, /v-if="canAccessConsoleView\(user, enterprise, 'development'\)" class="panel progress-panel"/);
});

test("provider admin navigation excludes applications and buyer navigation retains them", () => {
  for (const role of ["super_admin", "enterprise_admin"]) {
    const user = { enterprise_memberships: [{ enterprise_id: "e", role, status: "active" }] };
    assert.equal(allowedBusinessViews(user, { id: "e" }, "provider").includes("applications"), false);
    assert.equal(allowedBusinessViews(user, { id: "e" }, "buyer").includes("applications"), true);
  }
});

test("storefront requests product_type and categories without delivery filtering", async () => {
  for (const search of ["?q=hello&product_type=api&parent_id=parent&category_id=child&delivery_method=file&page=3", "?type=report&category_id=orphan&delivery_method=file"]) {
    const calls = [], paths = [];
    const context = { URLSearchParams, AbortController, registrationProductTypes, pageCount, pageSizeValue, props: { search, productId: "" },
      api: { get: async (path, options) => { calls.push({ path, options }); return { data: path === "/categories" ? { items: [] } : { items: [], total: 0, page: 1, page_size: 12 } }; } },
      navigate: path => paths.push(path), controller: undefined };
    for (const key of ["busy", "error", "notFound", "product", "selectedVersion", "selectedEnterprise", "subscriptionMonths", "items", "query", "type", "parentId", "categoryId", "page", "categories", "total", "pageSize"]) context[key] = ref(null);
    vm.createContext(context);
    vm.runInContext(storeSource.slice(storeSource.indexOf("function applyFilters("), storeSource.indexOf("watch(() => [props.productId")), context);
    await context.load();
    const params = calls.find(call => call.path === "/products").options.params;
    assert.equal("delivery_method" in params, false);
    assert.equal("type" in params, false);
    assert.equal("params" in calls.find(call => call.path === "/categories").options, false);
    assert.equal(params.product_type, search.includes("product_type=api") ? "api" : "");
    assert.equal(params.category_id, search.includes("parent_id=") ? "child" : "");
    context.applyFilters();
    const next = new URL(paths[0], "https://market.invalid");
    assert.equal(next.searchParams.has("delivery_method"), false);
    assert.equal(next.searchParams.has("page"), false);
    assert.equal(next.searchParams.get("product_type"), params.product_type || null);
  }
});

test("new registration choices omit legacy types and delivery cannot be selected", () => {
  assert.deepEqual(Object.keys(registrationProductTypes), ["dataset", "model", "api", "saas", "training", "consulting", "custom"]);
  assert.match(consoleSource, /交付方式<select v-model="productForm.delivery_method" disabled aria-readonly="true">/);
  assert.match(consoleSource, /v-for="\(name, key\) in registrationProductTypes"/);
  const scrim = consoleSource.slice(consoleSource.indexOf('v-if="showProductForm"'), consoleSource.indexOf('class="modal-card product-modal"'));
  assert.doesNotMatch(scrim, /@click|@mouseleave/);
  assert.match(scrim, /@keydown.esc.stop.prevent/);
  for (const source of [consoleSource, storeSource]) {
    for (const select of source.matchAll(/<select\b[^>]*>[\s\S]*?<\/select>/g)) {
      assert.doesNotMatch(select[0], /value="(?:report|application)"|in typeLabels|:value="productForm.product_type"/);
    }
  }
  assert.match(consoleSource, /<input v-if="!registrationProductTypes\[productForm.product_type\]"[^>]*readonly/);
});

test("fallback development copy is absent and its remaining action is permission gated", () => {
  const fallback = consoleSource.slice(consoleSource.indexOf('<div class="empty-module">'), consoleSource.indexOf('<div v-if="false && activeView'));
  assert.doesNotMatch(fallback, /开发任务监控台会显示/);
  assert.match(fallback, /<button v-if="canAccessConsoleView\(user, enterprise, 'development'\)"[^>]*@click="selectView\('development'\)"/);
});

test("initial detail fetch and integration binding are clean; later user edits are dirty", async () => {
  let confirmations = 0;
  const context = { ref, watch, nextTick, deliveryForProductType,
    productForm: ref({ product_type: "dataset", delivery_method: "file", versions: [] }),
    productSaving: ref(false), productReviewSubmitting: ref(false), productReadOnlyMode: ref(false),
    showProductForm: ref(true), user: ref({}), enterprise: ref({}),
    canManageProductRegistration: () => true, canReviewProduct: () => false,
    emptyProductForm: () => ({}), emptyProductVersion: () => ({}), notify: () => {},
    confirmDialog: async () => { confirmations++; return false; },
    api: { get: async path => ({ data: path.endsWith("/files") ? { items: [] } : {
      id: "p", status: "draft", product_type: "api", delivery_method: "api", name: "Fetched", versions: [{ id: "v" }],
    } }) },
    loadRegistrationIntegrations: async (_api, _id, versions) => {
      await Promise.resolve();
      versions[0].integration = { route_key: "bound-route" };
    },
  };
  for (const key of ["selectedProductId", "productDetailMode", "productDetailTab", "productReviewMode", "productFiles", "productLogoPreview"]) context[key] = ref(null);
  vm.createContext(context);
  vm.runInContext(consoleSource.slice(consoleSource.indexOf("const productEditorDirty ="), consoleSource.indexOf("watch([showProductForm, productDetailMode")) +
    consoleSource.slice(consoleSource.indexOf("watch(() => productForm.value.product_type"), consoleSource.indexOf("const oidcRedirectUrisText =")) +
    consoleSource.slice(consoleSource.indexOf("async function closeProductEditor()"), consoleSource.indexOf("function openNewProduct()")) +
    consoleSource.slice(consoleSource.indexOf("async function openProductDetail("), consoleSource.indexOf("function isPlatformRole()")) +
    "\nthis.dirty = productEditorDirty;", context);
  context.productForm.value.name = "previous edit";
  assert.equal(context.dirty.value, true);
  await context.openProductDetail({ id: "p", status: "draft", product_type: "api" });
  assert.equal(context.productForm.value.name, "Fetched");
  assert.equal(context.productForm.value.versions[0].integration.route_key, "bound-route");
  assert.equal(context.dirty.value, false);
  await context.closeProductEditor();
  assert.equal(confirmations, 0);
  assert.equal(context.showProductForm.value, false);
  await context.openProductDetail({ id: "p", status: "draft", product_type: "api" });
  context.productForm.value.name = "user edit";
  await context.closeProductEditor();
  assert.equal(confirmations, 1);
  assert.equal(context.showProductForm.value, true);
  assert.equal(context.productForm.value.name, "user edit");
});

test("editor changes including file selection require confirmation; cancel retains the form", async () => {
  let resolve;
  let confirmations = 0;
  const context = { ref, watch, productForm: ref({ name: "", versions: [{ fileUpload: null }] }),
    showProductForm: ref(false), productReadOnlyMode: ref(false), productSaving: ref(false), productReviewSubmitting: ref(false),
    confirmDialog: () => { confirmations++; return new Promise(done => { resolve = done; }); } };
  vm.createContext(context);
  vm.runInContext(consoleSource.slice(consoleSource.indexOf("const productEditorDirty ="), consoleSource.indexOf("watch([showProductForm, productDetailMode")) +
    consoleSource.slice(consoleSource.indexOf("async function closeProductEditor()"), consoleSource.indexOf("function openNewProduct()")), context);
  context.showProductForm.value = true;
  await context.closeProductEditor();
  assert.equal(context.showProductForm.value, false);
  context.showProductForm.value = true;
  context.productForm.value.name = "unsaved";
  const closing = context.closeProductEditor();
  await context.closeProductEditor();
  assert.equal(confirmations, 1);
  resolve(false);
  await closing;
  assert.equal(context.showProductForm.value, true);
  assert.equal(context.productForm.value.name, "unsaved");
  context.productSaving.value = true;
  await context.closeProductEditor();
  assert.equal(confirmations, 1);
  context.productSaving.value = false;
  const discard = context.closeProductEditor();
  resolve(true);
  await discard;
  assert.equal(context.showProductForm.value, false);
  context.showProductForm.value = true;
  context.productForm.value.versions[0].fileUpload = new Blob(["file"]);
  const fileClose = context.closeProductEditor();
  assert.equal(confirmations, 3);
  resolve(false);
  await fileClose;
  assert.ok(context.productForm.value.versions[0].fileUpload);
});
