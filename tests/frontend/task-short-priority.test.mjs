import assert from "node:assert/strict";
import test from "node:test";

import {
  getSortMode,
  initTasks,
  orderTasksByEstimate,
  renderCurrentTasks,
  renderTasks,
  setSortMode,
  setTaskStatusFilter,
} from "../../src/momentum_agent/static/js/tasks.js";

class FakeElement {
  constructor() {
    this.textContent = "";
    this.innerHTML = "";
    this.hidden = false;
    this.disabled = false;
    this.listeners = new Map();
    this.attributes = {};
    this.classes = new Set(["ghost"]);
    this.classList = {
      toggle: (name, force) => {
        const enabled = force === undefined ? !this.classes.has(name) : Boolean(force);
        if (enabled) this.classes.add(name);
        else this.classes.delete(name);
        return enabled;
      },
    };
  }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  setAttribute(name, value) { this.attributes[name] = value; }
  async click() { if (!this.disabled && !this.hidden) await this.listeners.get("click")?.(); }
}

function installLocation(search) {
  const location = {
    href: `https://momentum.example/${search}#task-list`,
    get search() { return new URL(this.href).search; },
  };
  globalThis.location = location;
  globalThis.history = {
    state: null,
    replaceState(_state, _title, path) { location.href = new URL(path, location.href).href; },
  };
}

function makeElements() {
  return {
    tasks: new FakeElement(),
    taskCount: new FakeElement(),
    dueDateFilterButton: new FakeElement(),
    dueOnFilterChip: new FakeElement(),
    dueOnFilterLabel: new FakeElement(),
    clearDueOnFilterButton: new FakeElement(),
    unestimatedDueFilterChip: new FakeElement(),
    clearUnestimatedDueFilterButton: new FakeElement(),
  };
}

function renderedIds(markup) {
  return [...markup.matchAll(/data-toggle="(\d+)"/g)].map((match) => Number(match[1]));
}

test("short-priority helper sorts positive estimates, pushes missing estimates back, and stably preserves groups and ties", () => {
  const tasks = [
    { id: 21, title: "group child", status: "todo", parent_task_id: 20, estimated_minutes: 5 },
    { id: 40, title: "unestimated first", status: "todo", estimated_minutes: null },
    { id: 20, title: "group parent", status: "todo", estimated_minutes: 20 },
    { id: 10, title: "short tie first", status: "todo", estimated_minutes: 5 },
    { id: 11, title: "short tie second", status: "todo", estimated_minutes: "5" },
    { id: 23, title: "long tie", status: "todo", estimated_minutes: 20 },
    { id: 30, title: "long", status: "todo", estimated_minutes: 40 },
    { id: 41, title: "zero estimate", status: "todo", estimated_minutes: 0 },
    { id: 42, title: "invalid estimate", status: "todo", estimated_minutes: "invalid" },
  ];
  const originalIds = tasks.map(({ id }) => id);

  assert.deepEqual(orderTasksByEstimate(tasks).map(({ id }) => id), [10, 11, 20, 21, 23, 30, 40, 41, 42]);
  assert.deepEqual(tasks.map(({ id }) => id), originalIds, "sorting does not mutate the loaded server array");
  assert.deepEqual(orderTasksByEstimate(null), []);
});

test("short-priority mode locally reorders only the current exact-date search intersection and exits on closed statuses", () => {
  const originalDocument = globalThis.document;
  const originalLocation = globalThis.location;
  const originalHistory = globalThis.history;
  const originalTimezone = process.env.TZ;
  process.env.TZ = "Asia/Hong_Kong";
  globalThis.document = { querySelectorAll: () => [] };
  installLocation("?due_on=2026-10-05");

  try {
    const elements = makeElements();
    setSortMode("score");
    setTaskStatusFilter("todo");
    initTasks(elements, {}, {});
    const searchResults = [
      { id: 201, title: "group parent", status: "todo", due_at: "2026-10-05", estimated_minutes: 30 },
      { id: 202, title: "group child", status: "todo", parent_task_id: 201, due_at: "2026-10-05", estimated_minutes: 5 },
      { id: 203, title: "long task", status: "todo", due_at: "2026-10-05", estimated_minutes: 60 },
      { id: 204, title: "short task", status: "todo", due_at: "2026-10-05", estimated_minutes: 5 },
      { id: 205, title: "short tie", status: "todo", due_at: "2026-10-05", estimated_minutes: 5 },
      { id: 206, title: "outside date", status: "todo", due_at: "2026-10-06", estimated_minutes: 1 },
      { id: 207, title: "closed search result", status: "done", due_at: "2026-10-05", estimated_minutes: 1 },
    ];
    renderTasks(searchResults, true, new Date(2026, 9, 5, 12));
    setSortMode("short");
    renderCurrentTasks(new Date(2026, 9, 5, 12));

    assert.equal(elements.taskCount.textContent, "5 个搜索结果");
    assert.deepEqual(renderedIds(elements.tasks.innerHTML), [204, 205, 201, 202, 203]);
    assert.doesNotMatch(elements.tasks.innerHTML, /outside date|closed search result/);
    assert.equal(getSortMode(), "short");

    setTaskStatusFilter("done");
    assert.equal(getSortMode(), "score", "switching to done exits local mode and restores the prior server-side sort");
    setSortMode("short");
    assert.equal(getSortMode(), "score", "short priority cannot be re-enabled on done tasks");
  } finally {
    globalThis.document = originalDocument;
    if (originalTimezone === undefined) delete process.env.TZ;
    else process.env.TZ = originalTimezone;
    if (originalLocation === undefined) delete globalThis.location;
    else globalThis.location = originalLocation;
    if (originalHistory === undefined) delete globalThis.history;
    else globalThis.history = originalHistory;
    setSortMode("default");
    setTaskStatusFilter("todo");
  }
});

test("short-priority mode keeps the unestimated-due filter and search-result boundary intact", () => {
  const originalDocument = globalThis.document;
  const originalLocation = globalThis.location;
  const originalHistory = globalThis.history;
  const originalTimezone = process.env.TZ;
  process.env.TZ = "Asia/Hong_Kong";
  globalThis.document = { querySelectorAll: () => [] };
  installLocation("?unestimated_due_by_today=1");

  try {
    const elements = makeElements();
    setSortMode("score");
    setTaskStatusFilter("todo");
    initTasks(elements, {}, {});
    const searchResults = [
      { id: 301, title: "overdue invalid estimate", status: "todo", due_at: "2026-10-04", estimated_minutes: "invalid" },
      { id: 302, title: "today zero estimate", status: "todo", due_at: "2026-10-05", estimated_minutes: 0 },
      { id: 303, title: "today estimated", status: "todo", due_at: "2026-10-05", estimated_minutes: 25 },
      { id: 304, title: "future unestimated", status: "todo", due_at: "2026-10-06", estimated_minutes: null },
      { id: 305, title: "done unestimated", status: "done", due_at: "2026-10-05", estimated_minutes: null },
    ];
    renderTasks(searchResults, true, new Date(2026, 9, 5, 12));
    setSortMode("short");
    renderCurrentTasks(new Date(2026, 9, 5, 12));

    assert.equal(elements.taskCount.textContent, "2 个搜索结果");
    assert.deepEqual(renderedIds(elements.tasks.innerHTML), [301, 302], "equal unestimated ranks retain the search result order");
    assert.doesNotMatch(elements.tasks.innerHTML, /today estimated|future unestimated|done unestimated/);
    assert.equal(getSortMode(), "short");

    setTaskStatusFilter("dropped");
    assert.equal(getSortMode(), "score", "switching to dropped exits local mode consistently");
    setSortMode("short");
    assert.equal(getSortMode(), "score", "short priority cannot be re-enabled on dropped tasks");
  } finally {
    globalThis.document = originalDocument;
    if (originalTimezone === undefined) delete process.env.TZ;
    else process.env.TZ = originalTimezone;
    if (originalLocation === undefined) delete globalThis.location;
    else globalThis.location = originalLocation;
    if (originalHistory === undefined) delete globalThis.history;
    else globalThis.history = originalHistory;
    setSortMode("default");
    setTaskStatusFilter("todo");
  }
});

test("short-priority mode sorts only the current overdue-or-today search intersection", async () => {
  const originalDocument = globalThis.document;
  const originalLocation = globalThis.location;
  const originalHistory = globalThis.history;
  const originalTimezone = process.env.TZ;
  process.env.TZ = "Asia/Hong_Kong";
  globalThis.document = { querySelectorAll: () => [] };
  installLocation("");

  try {
    const elements = makeElements();
    setSortMode("score");
    setTaskStatusFilter("todo");
    initTasks(elements, {}, {});
    await elements.dueDateFilterButton.click();
    const searchResults = [
      { id: 401, title: "today long", status: "todo", due_at: "2026-10-05", estimated_minutes: 40 },
      { id: 402, title: "overdue shorter", status: "doing", due_at: "2026-10-04", estimated_minutes: 10 },
      { id: 403, title: "today shortest", status: "todo", due_at: "2026-10-05", estimated_minutes: 5 },
      { id: 404, title: "today unestimated", status: "todo", due_at: "2026-10-05", estimated_minutes: 0 },
      { id: 405, title: "future task", status: "todo", due_at: "2026-10-06", estimated_minutes: 1 },
      { id: 406, title: "closed task", status: "done", due_at: "2026-10-04", estimated_minutes: 1 },
    ];
    const now = new Date(2026, 9, 5, 12);
    renderTasks(searchResults, true, now);
    setSortMode("short");
    renderCurrentTasks(now);

    assert.equal(elements.taskCount.textContent, "4 个搜索结果");
    assert.deepEqual(renderedIds(elements.tasks.innerHTML), [403, 402, 401, 404]);
    assert.doesNotMatch(elements.tasks.innerHTML, /future task|closed task/);
  } finally {
    globalThis.document = originalDocument;
    if (originalTimezone === undefined) delete process.env.TZ;
    else process.env.TZ = originalTimezone;
    if (originalLocation === undefined) delete globalThis.location;
    else globalThis.location = originalLocation;
    if (originalHistory === undefined) delete globalThis.history;
    else globalThis.history = originalHistory;
    setSortMode("default");
    setTaskStatusFilter("todo");
  }
});
