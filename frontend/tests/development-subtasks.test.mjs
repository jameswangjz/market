import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { computed, createSSRApp, ref } from "vue";
import { renderToString } from "@vue/server-renderer";
import { ChevronDown, ChevronRight, ChevronsDown, ChevronsUp } from "lucide-vue-next";
import { taskSubtasks, useDevelopmentSubtasks } from "../src/developmentSubtasks.js";

const task = (code, count = 2) => ({ code, title: `任务 ${code}`, area: "前端", owner: "frontend", priority: "P1", status: "done", progress: 100,
  subtasks: Array.from({ length: count }, (_, index) => ({ id: `${code}-S${String(index + 1).padStart(2, "0")}`, title: `${code} 完整子步骤 ${index + 1}`, status: "untracked" })) });
const source = readFileSync(new URL("../src/ConsoleApp.vue", import.meta.url), "utf8");
const start = source.indexOf('<div class="panel task-panel">');
const end = source.indexOf('</div></template', start) + '</div>'.length;
const template = source.slice(start, end);
function harness(items, total) {
  const development = ref({ items, total: items.length, completion_rate: 100, ...(total === undefined ? {} : { subtask_count: total }) });
  const filter = ref("all");
  const filteredTasks = computed(() => development.value.items.filter(item => filter.value === "all" || item.code === filter.value));
  return { development, filteredTasks, filter, ...useDevelopmentSubtasks(development, filteredTasks) };
}
async function render(state) {
  return renderToString(createSSRApp({ template, components: { ChevronDown, ChevronRight, ChevronsDown, ChevronsUp },
    setup: () => ({ ...state, taskSubtasks, fmtDate: () => "", taskStatusLabels: { done: "已完成", review: "待验收" }, updateTask: () => {} }) }));
}

test("subtask totals prefer the API count, with an array-based legacy fallback", () => {
  const state = harness([task("A"), task("B", 3)]);
  assert.equal(state.subtaskTotal.value, 5);
  state.development.value.subtask_count = 124;
  assert.equal(state.subtaskTotal.value, 124);
  state.filter.value = "A";
  assert.equal(state.subtaskTotal.value, 124);
  state.development.value.subtask_count = 0;
  assert.equal(state.subtaskTotal.value, 0);
  state.development.value.subtask_count = -1;
  assert.equal(state.subtaskTotal.value, 5);
});

test("individual and all toggles act on visible tasks and retain expansion through polling", () => {
  const state = harness([task("A"), task("B"), { code: "old" }]);
  state.toggleTaskSubtasks(state.development.value.items[0]);
  assert.deepEqual([...state.expandedTaskCodes.value], ["A"]);
  assert.equal(state.allSubtasksExpanded.value, false);
  state.toggleAllSubtasks();
  assert.equal(state.allSubtasksExpanded.value, true);
  assert.deepEqual([...state.expandedTaskCodes.value], ["A", "B"]);
  state.filter.value = "B"; state.toggleAllSubtasks();
  assert.deepEqual([...state.expandedTaskCodes.value], ["A"]);
  state.development.value.items = [task("A", 3), task("B")];
  state.filter.value = "A";
  assert.equal(state.allSubtasksExpanded.value, true);
  assert.equal(taskSubtasks(state.filteredTasks.value[0]).length, 3);
  state.toggleTaskSubtasks(state.filteredTasks.value[0]);
  assert.equal(state.expandedTaskCodes.value.size, 0);
});

test("legacy tasks without subtasks render safely and cannot expand", async () => {
  const state = harness([{ ...task("old"), subtasks: undefined }, { ...task("null"), subtasks: null }]);
  assert.deepEqual(taskSubtasks(null), []);
  assert.equal(state.subtaskTotal.value, 0);
  assert.equal(state.hasVisibleSubtasks.value, false);
  state.toggleTaskSubtasks(state.development.value.items[0]); state.toggleAllSubtasks();
  assert.equal(state.expandedTaskCodes.value.size, 0);
  const html = await render(state);
  assert.match(html, /0 个子步骤/);
  assert.doesNotMatch(html, /class="task-subtask-row"/);
  assert.match(html, /aria-label="展开全部子步骤"[^>]*disabled/);
});

test("32 tasks expose all 124 complete steps only when expanded, independent of parent completion", async () => {
  const items = Array.from({ length: 32 }, (_, index) => task(`TRD-R3-${String(index + 1).padStart(3, "0")}`, index < 28 ? 4 : 3));
  const longTitle = "完整标题 ".repeat(40) + "<script>不可执行</script>";
  items[0].subtasks[0].title = longTitle;
  const state = harness(items, 124);
  let html = await render(state);
  assert.match(html, /32 项 · 124 个子步骤/);
  assert.match(html, /4 个子步骤/);
  assert.match(html, /3 个子步骤/);
  assert.doesNotMatch(html, /class="task-subtask-row"/);
  state.toggleAllSubtasks(); html = await render(state);
  assert.equal((html.match(/class="task-subtask-row"/g) || []).length, 32);
  assert.equal((html.match(/class="task-subtask-status">未单独标记/g) || []).length, 124);
  for (const item of items) for (const step of item.subtasks) assert.ok(html.includes(step.id), step.id);
  assert.ok(html.includes(longTitle.replaceAll("<", "&lt;").replaceAll(">", "&gt;")));
  assert.match(html, /aria-label="折叠全部子步骤"/);
  assert.match(html, /aria-expanded="true" aria-controls="task-subtasks-TRD-R3-001"/);
  state.toggleAllSubtasks();
  assert.doesNotMatch(await render(state), /class="task-subtask-row"/);
});
