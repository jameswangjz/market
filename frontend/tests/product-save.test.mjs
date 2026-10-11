import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { registrationPayload, bindRegistrationVersions, saveRegistrationAttachments } from "../src/productRegistration.js";

const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
const create = source.slice(source.indexOf("async function createProduct()"), source.indexOf("function openNewProduct()"));
const edit = source.slice(source.indexOf("async function saveProductEdit()"), source.indexOf("function addProductVersion()"));
function harness(post, put = async () => ({ data: draft })) {
  const state = {
    productSaving: { value: false }, selectedProductId: { value: "" },
    productDetailMode: { value: false }, productReadOnlyMode: { value: false },
    productForm: { value: { name: "sample", delivery_method: "file", versions: [{ version_code: "v1" }] } },
    productDirectories: { value: [] }, showProductForm: { value: true },
    api: { post, put }, messages: [],
    emptyProductForm: () => ({}), loadViewData: async () => {},
    FormData: class { append() {} },
    registrationPayload, bindRegistrationVersions, saveRegistrationAttachments,
  };
  state.notify = (message) => state.messages.push(message);
  vm.createContext(state);
  vm.runInContext(create + edit, state);
  return state;
}
const draft = { id: "draft-1", versions: [{ id: "version-1", version_code: "v1" }] };

test("overlapping submit events issue one product creation", async () => {
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  let creates = 0;
  const s = harness(async () => { creates++; await pending; return { data: draft }; });
  const first = s.createProduct();
  await s.createProduct();
  assert.equal(creates, 1);
  assert.equal(s.productSaving.value, true);
  release(); await first;
  assert.equal(s.productSaving.value, false);
});

test("failed attachment retains draft and retry edits instead of creating again", async () => {
  const calls = [];
  let uploads = 0;
  const s = harness(async (url) => {
    calls.push(url);
    if (url === "/products") return { data: draft };
    if (++uploads === 1) throw new Error("upload failed");
    return { data: { id: "file-1" } };
  }, async (url) => { calls.push(url); return { data: draft }; });
  s.productForm.value.logoFile = {};
  s.productForm.value.versions[0].fileUpload = new Blob(["archive"]);
  await s.createProduct();
  assert.equal(s.selectedProductId.value, "draft-1");
  assert.equal(s.productDetailMode.value, true);
  assert.equal(s.productForm.value.versions[0].id, "version-1");
  assert.equal(s.showProductForm.value, true);
  assert.equal(s.productSaving.value, false);
  await s.saveProductEdit();
  assert.equal(calls.filter((url) => url === "/products").length, 1);
  assert.ok(calls.includes("/products/draft-1"));
  assert.equal(s.productForm.value.logoFile, null);
  assert.equal(s.productForm.value.versions[0].fileUpload, null);
});

test("successful logo is not uploaded again when dataset upload fails", async () => {
  let logoUploads = 0;
  let fileUploads = 0;
  const s = harness(async (url) => {
    if (url === "/products") return { data: draft };
    if (++logoUploads === 1) return { data: { id: "logo-1" } };
    if (++fileUploads === 1) throw new Error("dataset failed");
    return { data: { id: "dataset-1" } };
  });
  s.productForm.value.logoFile = {};
  s.productForm.value.versions[0].fileUpload = new Blob(["archive"]);
  await s.createProduct();
  assert.equal(s.productForm.value.logoFile, null);
  assert.equal(s.productForm.value.logo_file_id, "logo-1");
  await s.saveProductEdit();
  assert.equal(logoUploads, 3); // One logo and two dataset attempts.
  assert.equal(s.productForm.value.versions[0].fileUpload, null);
});

test("failed product creation releases lock without pretending draft exists", async () => {
  const s = harness(async () => { throw new Error("create failed"); });
  await s.createProduct();
  assert.equal(s.productSaving.value, false);
  assert.equal(s.selectedProductId.value, "");
  assert.equal(s.productDetailMode.value, false);
});

test("overlapping edits issue one update and block create during edit", async () => {
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  let updates = 0;
  const s = harness(async () => { throw new Error("unexpected creation"); }, async () => { updates++; await pending; return { data: draft }; });
  s.selectedProductId.value = "draft-1";
  const first = s.saveProductEdit();
  await s.saveProductEdit();
  await s.createProduct();
  assert.equal(updates, 1);
  release(); await first;
  assert.equal(s.productSaving.value, false);
});

test("sold version rejection prevents all attachment uploads", async () => {
  let uploads = 0;
  const s = harness(async () => { uploads++; }, async () => { throw new Error("sold version immutable"); });
  s.selectedProductId.value = "draft-1";
  s.productForm.value.logoFile = {};
  s.productForm.value.versions[0].fileUpload = new Blob(["archive"]);
  await s.saveProductEdit();
  assert.equal(uploads, 0);
  assert.equal(s.showProductForm.value, true);
  assert.ok(s.productForm.value.versions[0].fileUpload);
});
