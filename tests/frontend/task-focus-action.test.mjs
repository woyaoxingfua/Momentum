import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { initTasks, renderTasks, setTaskStatusFilter } from "../../src/momentum_agent/static/js/tasks.js";

const tasksSource = await readFile(
  new URL("../../src/momentum_agent/static/js/tasks.js", import.meta.url),
  "utf8",
);

class FakeElement {
  constructor() {
    this.innerHTML = "";
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.listeners = new Map();
    this.classList = { toggle() {} };
  }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  setAttribute() {}
}

test("todo and doing cards expose a distinct focus action while todo start remains a status action", () => {
  const originalDocument = globalThis.document;
  const taskList = new FakeElement();
  const taskCount = new FakeElement();
  globalThis.document = {
    getElementById: () => null,
    querySelectorAll: () => [],
  };

  try {
    initTasks({ tasks: taskList, taskCount }, {}, {});

    setTaskStatusFilter("todo");
    renderTasks([{ id: 731, title: "待办目标", status: "todo", priority: "medium", estimated_minutes: 26 }]);
    assert.match(taskList.innerHTML, /data-start="731"[^>]*>开始<\/button>/);
    assert.match(taskList.innerHTML, /data-focus-start="731"[^>]*>开始专注<\/button>/);

    setTaskStatusFilter("doing");
    renderTasks([{ id: 842, title: "进行中目标", status: "doing", priority: "medium", estimated_minutes: 41 }]);
    assert.match(taskList.innerHTML, /data-focus-start="842"[^>]*>开始专注<\/button>/);
    assert.doesNotMatch(taskList.innerHTML, /data-start="842"/);

    setTaskStatusFilter("done");
    renderTasks([{ id: 953, title: "已完成目标", status: "done", priority: "medium", estimated_minutes: 19 }]);
    assert.doesNotMatch(taskList.innerHTML, /data-focus-start=/);
  } finally {
    globalThis.document = originalDocument;
  }
});

test("row focus handler resolves the current open task object and delegates to the shared focus controller", () => {
  const focusBindingStart = tasksSource.indexOf('document.querySelectorAll("[data-focus-start]")');
  assert.notEqual(focusBindingStart, -1);
  const focusBindingEnd = tasksSource.indexOf('document.querySelectorAll("[data-done]")', focusBindingStart);
  assert.ok(focusBindingEnd > focusBindingStart);
  const binding = tasksSource.slice(focusBindingStart, focusBindingEnd);

  assert.match(binding, /currentTasks\.find\(\(item\) => String\(item\.id\) === String\(btn\.dataset\.focusStart\)\)/);
  assert.match(binding, /OPEN_TASK_STATUSES\.has\(task\.status\)/);
  assert.match(binding, /await startSuggestedFocus\(task\)/);
  assert.doesNotMatch(binding, /\/api\/tasks\/.*\/start/);

  const taskStartBindingStart = tasksSource.indexOf('document.querySelectorAll("[data-start]")');
  const taskStartBindingEnd = tasksSource.indexOf('document.querySelectorAll("[data-focus-start]")', taskStartBindingStart);
  const taskStartBinding = tasksSource.slice(taskStartBindingStart, taskStartBindingEnd);
  assert.match(taskStartBinding, /\/api\/tasks\/\$\{btn\.dataset\.start\}\/start/);
  assert.match(taskStartBinding, /await loadTasks\(\)/);
});
