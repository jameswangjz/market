import { computed, ref } from "vue";

export function taskSubtasks(task) {
  return Array.isArray(task?.subtasks) ? task.subtasks : [];
}

export function useDevelopmentSubtasks(development, visibleTasks) {
  const expandedTaskCodes = ref(new Set());
  const expandableTasks = computed(() => visibleTasks.value.filter(task => taskSubtasks(task).length));
  const hasVisibleSubtasks = computed(() => expandableTasks.value.length > 0);
  const allSubtasksExpanded = computed(() => hasVisibleSubtasks.value && expandableTasks.value.every(task => expandedTaskCodes.value.has(task.code)));
  const subtaskTotal = computed(() => {
    const count = development.value.subtask_count;
    return Number.isSafeInteger(count) && count >= 0 ? count :
      (development.value.items || []).reduce((total, task) => total + taskSubtasks(task).length, 0);
  });
  function toggleTaskSubtasks(task) {
    if (!taskSubtasks(task).length) return;
    const expanded = new Set(expandedTaskCodes.value);
    if (expanded.has(task.code)) expanded.delete(task.code);
    else expanded.add(task.code);
    expandedTaskCodes.value = expanded;
  }
  function toggleAllSubtasks() {
    const expanded = new Set(expandedTaskCodes.value);
    const collapse = allSubtasksExpanded.value;
    for (const task of expandableTasks.value) {
      if (collapse) expanded.delete(task.code);
      else expanded.add(task.code);
    }
    expandedTaskCodes.value = expanded;
  }
  return { expandedTaskCodes, hasVisibleSubtasks, allSubtasksExpanded, subtaskTotal, toggleTaskSubtasks, toggleAllSubtasks };
}
