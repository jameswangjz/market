import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { computed, ref, watch } from "vue";
import { bindRegistrationVersions, deliveryForProductType, emptyVersionIntegration, isValidOidcRedirectUri, loadRegistrationIntegrations, productDeliveryMapping, registrationPayload, saveRegistrationAttachments, splitOidcRedirectUris, stableRouteKey } from "../src/productRegistration.js";

test("product types map to their canonical delivery and route keys use stable IDs", () => {
  assert.deepEqual(productDeliveryMapping, { dataset: "file", model: "file", report: "file", application: "file", api: "api", saas: "tenant_access", training: "training", consulting: "consulting", custom: "custom" });
  assert.equal(deliveryForProductType("saas"), "tenant_access");
  assert.equal(stableRouteKey("product id", "version/id"), "p-product-id-v-version-id");
});

test("registration JSON omits file objects and integration-only fields", () => {
  const payload = registrationPayload({ logoFile: {}, fileUpload: {}, client_secret: "must-not-send", review_logs: [], versions: [{ version_code: "v1", fileUpload: {}, integration: { client_secret: "must-not-send" } }] });
  assert.equal("logoFile" in payload, false);
  assert.equal("fileUpload" in payload.versions[0], false);
  assert.equal("integration" in payload.versions[0], false);
  assert.equal("client_secret" in payload, false);
  assert.equal("review_logs" in payload, false);
});

test("registration serializes compatibility and callback arrays without UI or server fields", () => {
  const form = {
    product_type: "saas", delivery_method: "tenant_access",
    oidc_redirect_uris: [" https://app.example/callback ", "", "https://app.example/other?client=market"],
    oidcRedirectUrisText: "must-not-send", delivery_fields_locked: true,
    versions: [
      { version_code: "v1", compatibility_mode: "compatible", compatibility_notes: "", is_sold: true },
      { version_code: "v2", compatibility_mode: "breaking", compatibility_notes: "New response schema", internal: true },
    ],
  };
  const payload = JSON.parse(JSON.stringify(registrationPayload(form)));
  assert.deepEqual(payload.oidc_redirect_uris, ["https://app.example/callback", "https://app.example/other?client=market"]);
  assert.deepEqual(payload.versions, [
    { version_code: "v1", compatibility_mode: "compatible", compatibility_notes: "" },
    { version_code: "v2", compatibility_mode: "breaking", compatibility_notes: "New response schema" },
  ]);
  assert.equal("oidcRedirectUrisText" in payload, false);
  assert.equal("delivery_fields_locked" in payload, false);
  assert.equal(form.oidc_redirect_uris[0], " https://app.example/callback ");
});

test("registration supplies callback and compatibility defaults for older forms", () => {
  const payload = registrationPayload({ versions: [{ version_code: "v1" }] });
  assert.deepEqual(payload.oidc_redirect_uris, []);
  assert.equal(payload.versions[0].compatibility_mode, "compatible");
  assert.equal(payload.versions[0].compatibility_notes, "");
});

test("OIDC callbacks accept HTTPS by default", () => {
  for (const uri of [
    "https://app.example/callback", "https://app.example/", "https://app.example:8443/callback?state=abc",
    "https://[::1]/callback", "HTTPS://app.example/callback",
  ]) assert.equal(isValidOidcRedirectUri(uri), true, uri);
});

test("OIDC callbacks allow HTTP only when runtime policy explicitly enables it", () => {
  for (const uri of ["http://localhost:8200/callback", "http://example.com/callback", "http://market-mock-saas:8200/callback"]) {
    assert.equal(isValidOidcRedirectUri(uri), false, uri);
    assert.equal(isValidOidcRedirectUri(uri, { allowHttp: true }), true, uri);
  }
  assert.equal(isValidOidcRedirectUri("http://example.com/callback", { allowHttp: false }), false);
});

test("OIDC callbacks reject insecure hosts, parser normalization, fragments and userinfo", () => {
  for (const uri of [
    "http://example.com/callback", "http://localhost.example/callback", "http://market-mock-saas.evil/callback",
    "http://127.0.0.2/callback", "http://127.1/callback", "http://2130706433/callback",
    "http://0x7f000001/callback", "http://127.000.000.001/callback", "http://[::1]/callback",
    "http://localhost./callback", "http://local%68ost/callback",
    "https://app.example/callback#fragment", "https://app.example/callback#",
    "https://user:password@app.example/callback", "https://@app.example/callback",
    "http://localhost@evil.example/callback", "https://app.example/call back",
    "https://app.example/call\nback", "https://app.example\\callback",
    "https:/app.example/callback", "https:///app.example/callback", "https://",
    "/callback", "//app.example/callback", "ftp://app.example/callback", "javascript:alert(1)",
    "https://app.example:99999/callback", "", null, 123, {},
    "https://app.example", "https://app.example?callback=true", "https://app.example:0/callback",
    "https://app.example/callback\u0000", "https://*.example/callback", "https://app.example/*",
    "https://app.example/" + "a".repeat(2000),
  ]) assert.equal(isValidOidcRedirectUri(uri), false, String(uri));
});

test("registration rejects invalid callback values before persistence", () => {
  for (const redirects of ["https://app.example/callback", [123], [null], ["http://evil.example/callback"], ["http://localhost/callback"], ["https://app.example/#"]]) {
    assert.throws(() => registrationPayload({ oidc_redirect_uris: redirects, versions: [] }), /OIDC/);
  }
  assert.deepEqual(registrationPayload({ oidc_redirect_uris: ["http://app.example/callback"], versions: [] }, { allowHttp: true }).oidc_redirect_uris, ["http://app.example/callback"]);
  assert.deepEqual(splitOidcRedirectUris(" https://app.example/one \r\n\nhttps://app.example/two\n"), ["https://app.example/one", "https://app.example/two"]);
});

test("new and reset forms have independent defaults and multiline editing preserves a trailing newline", () => {
  const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
  const start = source.indexOf("const emptyProductVersion =");
  const end = source.indexOf("let progressTimer;", start);
  const readinessStart = source.indexOf("const productReadiness =", start);
  const formWatchStart = source.indexOf("watch(() => productForm.value.product_type", readinessStart);
  const formSource = source.slice(start, readinessStart) + source.slice(formWatchStart, end);
  const state = { computed, ref, watch, deliveryForProductType, applicationAddressPolicy: ref({ allow_http: false }), emptyVersionIntegration, splitOidcRedirectUris, isValidOidcRedirectUri };
  vm.createContext(state);
  vm.runInContext(formSource + "\nthis.form = productForm; this.redirectText = oidcRedirectUrisText; this.reset = emptyProductForm; this.emptyVersion = emptyProductVersion; this.validateRedirects = updateOidcRedirectUris;", state);
  assert.equal(state.form.value.oidc_redirect_uris.length, 0);
  assert.equal(state.form.value.versions[0].compatibility_mode, "compatible");
  assert.equal(state.emptyVersion().compatibility_notes, "");
  state.redirectText.value = "https://app.example/one\n";
  assert.equal(state.redirectText.value, "https://app.example/one\n");
  state.redirectText.value += "https://app.example/two";
  assert.deepEqual(Array.from(registrationPayload(state.form.value).oidc_redirect_uris), ["https://app.example/one", "https://app.example/two"]);
  let validity;
  const input = { value: "http://evil.example/callback", setCustomValidity: (message) => { validity = message; } };
  state.validateRedirects({ target: input });
  assert.match(validity, /HTTPS/);
  state.applicationAddressPolicy.value.allow_http = true;
  input.value = " https://app.example/one \n\nhttp://market-mock-saas:8200/callback";
  state.validateRedirects({ target: input });
  assert.equal(validity, "");
  state.form.value.oidc_redirect_uris = ["https://persisted.example/callback"];
  assert.equal(state.redirectText.value, "https://persisted.example/callback");
  state.form.value = state.reset();
  assert.equal(state.redirectText.value, "");
  assert.equal(state.form.value.versions[0].compatibility_mode, "compatible");
  assert.equal(state.form.value.versions[0].compatibility_notes, "");
});

test("multiple version uploads bind returned IDs by code and retain only failed files for retry", async () => {
  const versions = ["v1", "v2"].map((version_code) => ({ version_code, fileUpload: new Blob([version_code]) }));
  bindRegistrationVersions(versions, [{ id: "two", version_code: "v2" }, { id: "one", version_code: "v1" }]);
  const calls = [];
  let fail = true;
  const api = { post: async (url, body) => {
    calls.push([body.get("version_id"), body.get("version")]);
    if (body.get("version_id") === "two" && fail) throw new Error("upload failed");
    return { data: { original_name: "data.zip" } };
  } };
  const form = { delivery_method: "file", versions };
  await assert.rejects(saveRegistrationAttachments(api, form, "product"));
  assert.equal(versions[0].fileUpload, null);
  assert.ok(versions[1].fileUpload);
  fail = false;
  await saveRegistrationAttachments(api, form, "product");
  assert.deepEqual(calls, [["one", "v1"], ["two", "v2"], ["two", "v2"]]);
});

test("integration loading and saving whitelist config without carrying credentials or health results", async () => {
  const versions = [{ id: "one", version_code: "v1", daily_quota: 0, monthly_quota: 12000, rate_limit_per_minute: 60 }];
  await loadRegistrationIntegrations({ get: async () => ({ data: { route: { ...emptyVersionIntegration(), upstream_url: "https://provider.example/api", route_key: "route-one", upstream_client_secret: "must-not-load", healthy: true } } }) }, "product", versions);
  assert.equal("upstream_client_secret" in versions[0].integration, false);
  const calls = [];
  await saveRegistrationAttachments({ put: async (url, body) => { calls.push({ url, body }); } }, { delivery_method: "api", versions }, "product");
  assert.equal(calls[0].url, "/products/product/versions/one/integration");
  assert.equal(calls[0].body.version, "v1");
  assert.equal(calls[0].body.daily_quota, 0);
  assert.equal(calls[0].body.monthly_quota, 12000);
  assert.equal(calls[0].body.route_key, "route-one");
  assert.equal("healthy" in calls[0].body, false);
});

test("integration save generates a missing route key from persisted IDs", async () => {
  const versions = [{ id: "v-1", version_code: "v1", integration: emptyVersionIntegration() }];
  versions[0].integration.upstream_url = "https://business.example/v1";
  let request;
  await saveRegistrationAttachments({ put: async (url, body) => { request = { url, body }; } }, { delivery_method: "api", versions }, "p-1");
  assert.equal(request.body.route_key, "p-p-1-v-v-1");
});

for (const delivery_method of ["api", "model_api"]) {
  const apiForm = () => ({
    delivery_method,
    integration_api_url: " https://management.example/control ",
    upstream_url: "https://legacy-business.example",
    versions: ["v1", "v2"].map(version_code => ({ version_code, integration: { ...emptyVersionIntegration(), upstream_url: `https://business.example/${version_code}` } })),
  });

  test(`${delivery_method} projects management independently and writes business URLs only per version`, async () => {
    const form = apiForm();
    const payload = registrationPayload(form);
    assert.equal(payload.integration_api_url, "https://management.example/control");
    assert.equal("upstream_url" in payload, false);
    assert.equal(JSON.stringify(payload).includes("business.example"), false);
    assert.equal(form.upstream_url, "https://legacy-business.example");
    bindRegistrationVersions(form.versions, [{ id: "one", version_code: "v1" }, { id: "two", version_code: "v2" }]);
    const calls = [];
    await saveRegistrationAttachments({ put: async (url, body) => calls.push({ url, body }) }, form, "product");
    assert.deepEqual(calls.map(call => call.body.upstream_url), ["https://business.example/v1", "https://business.example/v2"]);
    assert.ok(calls.every(call => !("integration_api_url" in call.body)));
  });

  test(`${delivery_method} requires management and every editable version upstream before draft persistence`, async () => {
    for (const value of [undefined, "", " ", "/control", "ftp://management.example", "https://", "https://example/" + "a".repeat(500)]) {
      const form = apiForm();
      form.integration_api_url = value;
      assert.throws(() => registrationPayload(form), /API服务管理接口地址/);
    }
    const form = apiForm();
    form.versions[1].integration.upstream_url = "";
    assert.throws(() => registrationPayload(form), /版本v2上游URL/);
    await assert.rejects(saveRegistrationAttachments({ put: async () => assert.fail("No partial writes") }, form, "product"), /版本v2上游URL/);
    form.versions[1].is_sold = true;
    assert.doesNotThrow(() => registrationPayload(form));
    delete form.versions[0].integration;
    assert.throws(() => registrationPayload(form), /版本v1上游URL/);
  });
}

test("version loading keeps distinct business upstreams and never supplies a management fallback", async () => {
  const form = { delivery_method: "api", integration_api_url: "", upstream_url: "https://legacy.example", versions: [{ id: "one", version_code: "v1", is_sold: true }, { id: "two", version_code: "v2" }] };
  await loadRegistrationIntegrations({ get: async url => ({ data: { route: url.includes("/one/") ? { upstream_url: "https://business.example/v1" } : null } }) }, "product", form.versions);
  assert.equal(form.versions[0].integration.upstream_url, "https://business.example/v1");
  assert.equal(form.versions[0].is_sold, true);
  assert.equal(form.versions[1].integration.upstream_url, "");
  assert.equal(form.integration_api_url, "");
  assert.throws(() => registrationPayload(form), /API服务管理接口地址/);
});

test("new and edit draft handlers reject missing API management before making requests", async () => {
  const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
  for (const [name, next] of [["createProduct", "closeProductEditor"], ["saveProductEdit", "updateVersionMonthlyQuota"]]) {
    const start = source.indexOf(`async function ${name}()`);
    const end = source.indexOf(`${next === "closeProductEditor" ? "async " : ""}function ${next}(`, start);
    const notices = [];
    const state = {
      productSaving: ref(false), selectedProductId: ref(name === "createProduct" ? "" : "existing"), productReadOnlyMode: ref(false),
      productForm: ref({ delivery_method: "api", versions: [] }), applicationAddressPolicy: ref({ allow_http: false }),
      registrationPayload, notify: message => notices.push(message),
      api: { post: () => assert.fail("Invalid draft must not POST"), put: () => assert.fail("Invalid draft must not PUT") },
      TypeError,
    };
    vm.createContext(state);
    vm.runInContext(source.slice(start, end) + `\nthis.save = ${name};`, state);
    await state.save();
    assert.match(notices[0], /API服务管理接口地址/);
    assert.equal(state.productSaving.value, false);
  }
});

test("API form separates management from required version upstream and preserves SaaS labels and sold locks", () => {
  const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
  assert.doesNotMatch(source, /v-model="productForm.upstream_url"|提供方后端访问URL/);
  assert.match(source, /API服务管理接口地址<input v-model="productForm.integration_api_url" :disabled="productForm.delivery_fields_locked"[^>]*required/);
  assert.match(source, /租户\/权限开通API URL<input v-model="productForm.integration_api_url"/);
  assert.match(source, /应用访问URL<input v-model="productForm.application_url"/);
  assert.match(source, /版本上游URL<input v-model="version.integration.upstream_url"[^>]*required/);
  assert.match(source, /:disabled="productReadOnlyMode \|\| version.is_sold === true"/);
});

test("missing saved version identity stops attachment writes", () => {
  assert.throws(() => bindRegistrationVersions([{ version_code: "v2" }], [{ id: "one", version_code: "v1" }]));
});

test("sold release configuration is not resaved and sold file uploads are rejected", async () => {
  const api = { put: async () => assert.fail('Sold config must not be saved'), post: async () => assert.fail('Sold file must not be uploaded') };
  const versions = [{ id: 'sold', is_sold: true, integration: emptyVersionIntegration() }];
  await saveRegistrationAttachments(api, { delivery_method: 'api', versions }, 'product');
  versions[0].fileUpload = new Blob(['archive']);
  await assert.rejects(saveRegistrationAttachments(api, { delivery_method: 'file', versions }, 'product'), /已售版本/);
});
