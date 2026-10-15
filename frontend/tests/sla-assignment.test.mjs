import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { ref } from "vue";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";

const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
const assignment = source.slice(source.indexOf("async function assignServiceLevel()"), source.indexOf("function departmentNames("));
function harness(role = "super_admin") {
  const calls = [], notices = [], refreshes = [];
  const state = vm.createContext({
    user: ref({ platform_role: role }), serviceAssignmentSaving: ref(false),
    serviceAssignmentForm: ref({ enterprise_id: "enterprise", service_level_id: "enterprise-level", user_id: "", expires_at: null, reason: "  企业升级支持权益  " }),
    api: { post: async (...args) => { calls.push(args); } },
    notify: text => notices.push(text), loadViewData: async view => refreshes.push(view),
  });
  vm.runInContext(assignment, state);
  return { state, calls, notices, refreshes };
}

test("SLA assignment validates enterprise, level and nonblank bounded reason before requesting", async () => {
  for (const changes of [{ enterprise_id: "" }, { service_level_id: "" }, { reason: "" }, { reason: " \n\t " }, { reason: "x".repeat(2001) }]) {
    const h = harness(); Object.assign(h.state.serviceAssignmentForm.value, changes);
    await h.state.assignServiceLevel();
    assert.equal(h.calls.length, 0); assert.equal(h.notices.length, 1);
    assert.equal(h.state.serviceAssignmentSaving.value, false);
  }
});

test("both platform managers send trimmed reason and preserve existing assignment fields", async () => {
  for (const role of ["super_admin", "platform_operator"]) {
    const h = harness(role); await h.state.assignServiceLevel();
    assert.equal(h.calls[0][0], "/sla/service-level-assignments");
    assert.deepEqual({ ...h.calls[0][1] }, { enterprise_id: "enterprise", service_level_id: "enterprise-level", user_id: "", expires_at: null, reason: "企业升级支持权益" });
    assert.deepEqual(h.refreshes, ["sla"]);
    assert.equal(h.state.serviceAssignmentSaving.value, false);
  }
});

test("non-manager platform and enterprise identities cannot assign SLA", async () => {
  for (const role of ["finance_settlement", "delivery_monitor", "security_compliance", "quality_reviewer", "product_manager", ""]) {
    const h = harness(role); await h.state.assignServiceLevel();
    assert.equal(h.calls.length, 0);
  }
});

test("duplicate SLA submissions are blocked and a failed request keeps all entered fields", async () => {
  const h = harness(); let reject; let requests = 0;
  const before = JSON.stringify(h.state.serviceAssignmentForm.value);
  h.state.api.post = () => { requests++; return new Promise((_resolve, fail) => { reject = fail; }); };
  const first = h.state.assignServiceLevel();
  assert.equal(h.state.serviceAssignmentSaving.value, true);
  await h.state.assignServiceLevel(); assert.equal(requests, 1);
  reject({ response: { data: { detail: "模拟失败" } } }); await first;
  assert.equal(h.state.serviceAssignmentSaving.value, false);
  assert.equal(JSON.stringify(h.state.serviceAssignmentForm.value), before);
  assert.deepEqual(h.notices, ["模拟失败"]); assert.deepEqual(h.refreshes, []);
  h.state.api.post = async () => { requests++; };
  await h.state.assignServiceLevel(); assert.equal(requests, 2);
});

test("existing SLA form includes required reason input and the merged SFC compiles", () => {
  assert.match(source, /v-model="serviceAssignmentForm.reason"[^>]*maxlength="2000"[^>]*required/);
  assert.match(source, /:disabled="serviceAssignmentSaving \|\| !serviceAssignmentForm.reason.trim\(\)"/);
  const { descriptor, errors } = parse(source, { filename: "ConsoleApp.vue" });
  assert.deepEqual(errors, []);
  compileScript(descriptor, { id: "sla-assignment" });
  assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: "ConsoleApp.vue", id: "sla-assignment" }).errors, []);
});
