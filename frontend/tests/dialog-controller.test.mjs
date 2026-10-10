import test from "node:test";
import assert from "node:assert/strict";
import { createDialogController } from "../src/dialogController.js";
import { readFileSync } from "node:fs";
import vm from "node:vm";

test("required rejection reason rejects empty or whitespace without closing", async () => {
  const state = { value: null };
  const dialogs = createDialogController(state);
  const result = dialogs.prompt("Product", "", { required: true });
  assert.equal(dialogs.submit("   "), false);
  assert.equal(state.value.kind, "prompt");
  assert.equal(dialogs.submit("  reason  "), true);
  assert.equal(await result, "reason");
  assert.equal(state.value, null);
});

test("cancelled prompt returns null and confirmation returns false", async () => {
  const state = { value: null };
  const dialogs = createDialogController(state);
  const prompt = dialogs.prompt("Enter", "default");
  dialogs.cancel(); assert.equal(await prompt, null);
  const confirm = dialogs.confirm("Delete?");
  dialogs.cancel(); assert.equal(await confirm, false);
});

test("queued prompts retain their own content and resolve once", async () => {
  const state = { value: null };
  const dialogs = createDialogController(state);
  const first = dialogs.prompt("first", "one");
  const second = dialogs.confirm("second");
  assert.equal(state.value.message, "first");
  dialogs.submit("answer");
  assert.equal(state.value.message, "second");
  dialogs.submit();
  assert.equal(await first, "answer");
  assert.equal(await second, true);
  assert.equal(dialogs.submit("duplicate"), false);
});

test("empty optional input remains valid and disposal cancels all pending work", async () => {
  const state = { value: null };
  const dialogs = createDialogController(state);
  const optional = dialogs.prompt("optional");
  assert.equal(dialogs.submit(""), true);
  assert.equal(await optional, "");
  const first = dialogs.prompt("first");
  const second = dialogs.confirm("second");
  dialogs.dispose();
  assert.equal(await first, null);
  assert.equal(await second, false);
  assert.equal(state.value, null);
  assert.equal(await dialogs.prompt("after disposal"), null);
});

function reviewHarness(promptDialog, post) {
  const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
  const review = source.slice(source.indexOf("async function reviewProductFromDetail("), source.indexOf("async function saveProductEdit("));
  const state = {
    productReviewSubmitting: { value: false }, productForm: { value: { id: "product-1", name: "sample", status: "security_review" } },
    promptDialog, api: { post }, canReviewProduct: () => true,
    showProductForm: { value: true }, productReviewMode: { value: true },
    notify: () => {}, loadViewData: async () => {},
  };
  vm.createContext(state); vm.runInContext(review, state); return state;
}
test("cancel product review sends no request and releases submission guard", async () => {
  const state = reviewHarness(async () => null, async () => { throw new Error("unexpected request"); });
  await state.reviewProductFromDetail("reject");
  assert.equal(state.productReviewSubmitting.value, false);
  assert.equal(state.showProductForm.value, true);
});
test("duplicate review clicks open one dialog and submit once to correct stage", async () => {
  let finish, prompts = 0, calls = [];
  const pending = new Promise((resolve) => { finish = resolve; });
  const state = reviewHarness(async () => { prompts++; return pending; }, async (...args) => { calls.push(args); });
  const first = state.reviewProductFromDetail("reject");
  await state.reviewProductFromDetail("approve");
  assert.equal(prompts, 1);
  finish("reason"); await first;
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/products/product-1/security-review");
  assert.equal(calls[0][1].comment, "reason");
  assert.equal(state.productReviewSubmitting.value, false);
});
test("review API failure keeps details available and releases submission guard", async () => {
  const state = reviewHarness(async () => "approved", async () => { throw new Error("failure"); });
  await state.reviewProductFromDetail("approve");
  assert.equal(state.showProductForm.value, true);
  assert.equal(state.productReviewSubmitting.value, false);
});
