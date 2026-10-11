import test from "node:test";
import assert from "node:assert/strict";
import { bindRegistrationVersions, emptyVersionIntegration, loadRegistrationIntegrations, registrationPayload, saveRegistrationAttachments } from "../src/productRegistration.js";

test("registration JSON omits file objects and integration-only fields", () => {
  const payload = registrationPayload({ logoFile: {}, fileUpload: {}, client_secret: "must-not-send", review_logs: [], versions: [{ version_code: "v1", fileUpload: {}, integration: { client_secret: "must-not-send" } }] });
  assert.equal("logoFile" in payload, false);
  assert.equal("fileUpload" in payload.versions[0], false);
  assert.equal("integration" in payload.versions[0], false);
  assert.equal("client_secret" in payload, false);
  assert.equal("review_logs" in payload, false);
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
