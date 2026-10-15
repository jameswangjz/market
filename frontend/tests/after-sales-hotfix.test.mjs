import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { compile, createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createAfterSalesCloseGuard, createAfterSalesSession, evidenceAuthor, ticketStatus, afterSalesActions, timelineActor, timelineFrom } from "../src/afterSales.js";

test("clean modal closes immediately, dirty modal requires explicit custom discard", async () => {
  let dirty = false, closed = 0, asked = 0, consent = false;
  const close = createAfterSalesCloseGuard({ isBusy: () => false, isDirty: () => dirty,
    confirmDiscard: async () => { asked++; return consent; }, onClose: () => closed++ });
  await close(); assert.equal(closed, 1); assert.equal(asked, 0);
  dirty = true; await close(); assert.equal(closed, 1); assert.equal(asked, 1);
  consent = true; await close(); assert.equal(closed, 2);
});

test("busy modal cannot close and repeated close requests open only one discard dialog", async () => {
  let busy = true, asked = 0, closed = 0, finish;
  const close = createAfterSalesCloseGuard({ isBusy: () => busy, isDirty: () => true,
    confirmDiscard: () => { asked++; return new Promise(resolve => { finish = resolve; }); }, onClose: () => closed++ });
  await close(); assert.equal(asked, 0);
  busy = false; const pending = close(); await close(); assert.equal(asked, 1);
  finish(false); await pending; assert.equal(closed, 0);
});

async function conflictRace(dispose) {
  const state = { ticket: { id: "old", version: 1 }, busy: false, loading: false, error: "" };
  const waits = [];
  const api = { post: async () => { throw { response: { status: 409 } }; },
    get: (url) => url.includes("/old") ? new Promise(resolve => waits.push(() => resolve({ data: url.endsWith("/old") ? { id: "old", version: 2 } : { items: [] } })))
      : Promise.resolve({ data: url.endsWith("/new") ? { id: "new", version: 1 } : { items: [] } }) };
  const session = createAfterSalesSession(api, state, {}, () => "isolated-race-key");
  const pending = session.mutate("actions", { action: "withdraw", reason: "isolated" });
  for (let i = 0; i < 10 && waits.length < 4; i++) await Promise.resolve();
  assert.equal(waits.length, 4);
  if (dispose) { session.dispose(); state.ticket = { id: "new" }; }
  else await session.load("new");
  state.error = "new-context-error";
  waits.forEach(finish => finish()); await pending;
  assert.equal(state.ticket.id, "new"); assert.equal(state.error, "new-context-error");
}
test("409 refresh completing after session disposal cannot overwrite new identity error", () => conflictRace(true));
test("409 refresh completing after a newer generation cannot overwrite newer ticket error", () => conflictRace(false));

test("evidence author prioritizes name then email then stable identity, never only role", () => {
  assert.equal(evidenceAuthor({ author_name: "作者甲", author_email: "qa@example.test", author_id: "user-id" }), "作者甲");
  assert.equal(evidenceAuthor({ author_email: "qa@example.test", author_id: "user-id" }), "qa@example.test");
  assert.equal(evidenceAuthor({ author_id: "user-id", author_role: "enterprise_admin" }), "user-id");
  assert.equal(evidenceAuthor({ author_role: "enterprise_admin" }), "历史作者未记录");
});

test("real detail template displays escaped named comment and attachment authors", async () => {
  const source = readFileSync(new URL("../src/AfterSalesDetail.vue", import.meta.url), "utf8");
  const { descriptor } = parse(source);
  const output = await renderToString(createSSRApp({ components: {
    AfterSalesModalShell: { template: "<section><slot /></section>" }, Download: { render: () => null }, RefreshCw: { render: () => null },
  }, setup: () => ({ state: { ticket: { ticket_no: "AS-ISOLATED", status: "completed", order_no: "ORD-ISOLATED" }, busy: false, loading: false, error: "", timeline: [],
    comments: [{ id: "comment", author_name: "评论作者<安全>", author_role: "enterprise_admin", content: "内容", attachments: [] }],
    attachments: [{ id: "attachment", name: "owned.txt", author_name: "附件作者<安全>", author_role: "enterprise_admin", available: false }] },
    reason: "", comment: "", selectedAttachments: [], allowed: [], collaborate: false, terminal: true,
    action: "", upload: () => {}, selectableAttachment: () => false, date: () => "—", session: {}, emit: () => {},
    evidenceAuthor, ticketStatus, afterSalesActions, timelineActor, timelineFrom }), render: compile(descriptor.template.content) }));
  assert.match(output, /评论作者&lt;安全&gt;/); assert.match(output, /上传人：附件作者&lt;安全&gt;/);
});

test("dirty bindings cover create fields, action reason, comment and attachment selection; shell reuses ActionDialog", () => {
  const create = readFileSync(new URL("../src/AfterSalesCreate.vue", import.meta.url), "utf8");
  const detail = readFileSync(new URL("../src/AfterSalesDetail.vue", import.meta.url), "utf8");
  const shell = readFileSync(new URL("../src/AfterSalesModalShell.vue", import.meta.url), "utf8");
  assert.match(create, /:dirty="!!description \|\| type !== '质量异议' \|\| priority !== 'normal'"/);
  assert.match(detail, /:dirty="!!reason \|\| !!comment \|\| !!selectedAttachments.length"/);
  assert.match(shell, /<ActionDialog :dialog="discardDialog"/); assert.match(shell, /@click="requestClose"/);
  assert.doesNotMatch(shell, /(?:window|globalThis)\.confirm|(?<![.\w])confirm\s*\(/);
});

test("changed components compile with real binding metadata", () => {
  for (const name of ["AfterSalesModalShell", "AfterSalesCreate", "AfterSalesDetail"]) {
    const source = readFileSync(new URL(`../src/${name}.vue`, import.meta.url), "utf8");
    const { descriptor, errors } = parse(source); assert.deepEqual(errors, []);
    const script = compileScript(descriptor, { id: name });
    assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: `${name}.vue`, id: name,
      compilerOptions: { bindingMetadata: script.bindings } }).errors, []);
  }
});
