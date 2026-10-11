import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { computed, ref } from "vue";
import { bindRegistrationVersions, emptyVersionIntegration, isValidOidcRedirectUri, loadRegistrationIntegrations, registrationPayload, saveRegistrationAttachments, splitOidcRedirectUris } from "../src/productRegistration.js";

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

test("OIDC callbacks accept HTTPS and only exact HTTP development hosts", () => {
  for (const uri of [
    "https://app.example/callback", "https://app.example/", "https://app.example:8443/callback?state=abc",
    "https://[::1]/callback", "HTTPS://app.example/callback", "http://localhost:8200/callback",
    "http://127.0.0.1/callback", "http://market-mock-saas:8200/callback", "http://LOCALHOST/callback",
  ]) assert.equal(isValidOidcRedirectUri(uri), true, uri);
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
  for (const redirects of ["https://app.example/callback", [123], [null], ["http://evil.example/callback"], ["https://app.example/#"]]) {
    assert.throws(() => registrationPayload({ oidc_redirect_uris: redirects, versions: [] }), /OIDC/);
  }
  assert.deepEqual(splitOidcRedirectUris(" https://app.example/one \r\n\nhttps://app.example/two\n"), ["https://app.example/one", "https://app.example/two"]);
});

test("new and reset forms have independent defaults and multiline editing preserves a trailing newline", () => {
  const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
  const start = source.indexOf("const emptyProductVersion =");
  const end = source.indexOf("let progressTimer;", start);
  const state = { computed, ref, emptyVersionIntegration, splitOidcRedirectUris, isValidOidcRedirectUri };
  vm.createContext(state);
  vm.runInContext(source.slice(start, end) + "\nthis.form = productForm; this.redirectText = oidcRedirectUrisText; this.reset = emptyProductForm; this.emptyVersion = emptyProductVersion; this.validateRedirects = updateOidcRedirectUris;", state);
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
  assert.equal("healthy" in calls[0].body, false);
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
