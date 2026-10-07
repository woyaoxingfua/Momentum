import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  getUnestimatedDueByTodayFilterActive,
  filterOpenTasksDueByLocalToday,
  filterOpenTasksDueOnLocalDate,
  getDueDateFilterActive,
  initTasks,
  parseDueOnDate,
  parseUnestimatedDueByToday,
  renderTasks,
  setTaskStatusFilter,
} from "../../src/momentum_agent/static/js/tasks.js";

const indexHtml = await readFile(new URL("../../src/momentum_agent/static/index.html", import.meta.url), "utf8");

class FakeElement {
  constructor() {
    this.value = "";
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
  async click() {
    if (!this.disabled && !this.hidden) await this.listeners.get("click")?.();
  }
}

test("task page includes a visible-on-activation date chip, clear button, and mutually exclusive overdue control", () => {
  assert.match(indexHtml, /id="dueOnFilterChip" class="due-on-filter-chip" aria-live="polite" hidden/);
  assert.match(indexHtml, /id="clearDueOnFilterButton"[^>]*>清除<\/button>/);
  assert.match(indexHtml, /id="dueDateFilterButton"/);
  assert.match(indexHtml, /id="unestimatedDueFilterChip"[^>]*hidden/);
  assert.match(indexHtml, /id="clearUnestimatedDueFilterButton"[^>]*aria-label="清除未估时筛选"/);
});

test("due_on URL parsing accepts real calendar dates and ignores malformed or impossible dates", () => {
  assert.equal(parseDueOnDate("?due_on=2024-02-29"), "2024-02-29");
  assert.equal(parseDueOnDate("due_on=2026-10-07&keep=1"), "2026-10-07");
  for (const value of ["2026-2-07", "2026-02-30", "2025-02-29", "2026-10-07T00:00:00Z", "not-a-date"]) {
    assert.equal(parseDueOnDate(`?due_on=${encodeURIComponent(value)}`), null, value);
  }
  assert.equal(parseDueOnDate("?keep=1"), null);
});

test("date-only due dates and timezone timestamps match the exact requested local calendar day", () => {
  const originalTimezone = process.env.TZ;
  process.env.TZ = "Asia/Hong_Kong";
  try {
    const tasks = [
      { id: 1, status: "todo", due_at: "2026-10-05" },
      { id: 2, status: "doing", due_at: "2026-10-04T16:00:00Z" },
      { id: 3, status: "todo", due_at: "2026-10-05T15:59:59Z" },
      { id: 4, status: "todo", due_at: "2026-10-05T16:00:00Z" },
      { id: 5, status: "todo", due_at: "2026-10-04" },
      { id: 6, status: "done", due_at: "2026-10-05" },
      { id: 7, status: "todo", due_at: "2026-02-30" },
    ];
    assert.deepEqual(filterOpenTasksDueOnLocalDate(tasks, "2026-10-05").map(({ id }) => id), [1, 2, 3]);
    assert.deepEqual(filterOpenTasksDueOnLocalDate(tasks, "2026-02-30"), []);
  } finally {
    if (originalTimezone === undefined) delete process.env.TZ;
    else process.env.TZ = originalTimezone;
  }
});

function installLocation(search) {
  const location = {
    href: `https://momentum.example/${search}#task-list`,
    get search() { return new URL(this.href).search; },
  };
  const history = {
    state: null,
    replaceState(_state, _title, path) { location.href = new URL(path, location.href).href; },
  };
  globalThis.location = location;
  globalThis.history = history;
  return { location, history };
}

function makeDateFilterElements() {
  const elements = {
    tasks: new FakeElement(),
    taskCount: new FakeElement(),
    dueDateFilterButton: new FakeElement(),
    dueOnFilterChip: new FakeElement(),
    dueOnFilterLabel: new FakeElement(),
    clearDueOnFilterButton: new FakeElement(),
    unestimatedDueFilterChip: new FakeElement(),
    clearUnestimatedDueFilterButton: new FakeElement(),
  };
  return elements;
}

test("an impossible due_on URL leaves the task list unfiltered and the date chip hidden", () => {
  const originalDocument = globalThis.document;
  const originalLocation = globalThis.location;
  const originalHistory = globalThis.history;
  installLocation("?due_on=2026-02-30");
  globalThis.document = { querySelectorAll: () => [] };
  try {
    const elements = makeDateFilterElements();
    setTaskStatusFilter("todo");
    initTasks(elements, {}, {});
    renderTasks([
      { id: 11, title: "first task", status: "todo", due_at: "2026-02-28" },
      { id: 12, title: "second task", status: "todo", due_at: "2026-03-01" },
    ]);
    assert.equal(elements.dueOnFilterChip.hidden, true);
    assert.equal(elements.dueDateFilterButton.disabled, false);
    assert.match(elements.tasks.innerHTML, /first task/);
    assert.match(elements.tasks.innerHTML, /second task/);
  } finally {
    globalThis.document = originalDocument;
    if (originalLocation === undefined) delete globalThis.location;
    else globalThis.location = originalLocation;
    if (originalHistory === undefined) delete globalThis.history;
    else globalThis.history = originalHistory;
  }
});

test("a valid URL restores after initialization, survives todo/doing tabs, and is removed on done/dropped", () => {
  const originalDocument = globalThis.document;
  const originalLocation = globalThis.location;
  const originalHistory = globalThis.history;
  const originalTimezone = process.env.TZ;
  process.env.TZ = "Asia/Hong_Kong";
  installLocation("?keep=1&due_on=2026-10-05");
  globalThis.document = { querySelectorAll: () => [] };
  try {
    const elements = makeDateFilterElements();
    setTaskStatusFilter("todo");
    initTasks(elements, {}, {});
    const todoTasks = [
      { id: 21, title: "date-only match", status: "todo", due_at: "2026-10-05" },
      { id: 22, title: "timestamp match", status: "todo", due_at: "2026-10-04T16:00:00Z" },
      { id: 23, title: "different local day", status: "todo", due_at: "2026-10-06" },
    ];
    renderTasks(todoTasks, false, new Date("2026-10-05T12:00:00Z"));
    assert.equal(elements.dueOnFilterChip.hidden, false);
    assert.equal(elements.dueOnFilterLabel.textContent, "2026-10-05");
    assert.equal(elements.dueDateFilterButton.disabled, true, "exact-date and overdue/today filters are mutually exclusive");
    assert.match(elements.tasks.innerHTML, /date-only match/);
    assert.match(elements.tasks.innerHTML, /timestamp match/);
    assert.doesNotMatch(elements.tasks.innerHTML, /different local day/);

    setTaskStatusFilter("doing");
    assert.equal(parseDueOnDate(globalThis.location.search), "2026-10-05");
    assert.equal(elements.dueOnFilterChip.hidden, false);
    renderTasks([{ id: 31, title: "doing match", status: "doing", due_at: "2026-10-05T03:00:00Z" }]);
    assert.match(elements.tasks.innerHTML, /doing match/);

    setTaskStatusFilter("done");
    assert.equal(parseDueOnDate(globalThis.location.search), null);
    assert.equal(new URL(globalThis.location.href).searchParams.get("keep"), "1");
    assert.equal(elements.dueOnFilterChip.hidden, true);
    assert.equal(elements.dueDateFilterButton.hidden, true);
    setTaskStatusFilter("todo");
    assert.equal(elements.dueOnFilterChip.hidden, true);

    installLocation("?keep=1&due_on=2026-10-05");
    initTasks(elements, {}, {});
    assert.equal(elements.dueOnFilterChip.hidden, false);
    setTaskStatusFilter("dropped");
    assert.equal(parseDueOnDate(globalThis.location.search), null);
    assert.equal(elements.dueOnFilterChip.hidden, true);
  } finally {
    globalThis.document = originalDocument;
    if (originalTimezone === undefined) delete process.env.TZ;
    else process.env.TZ = originalTimezone;
    if (originalLocation === undefined) delete globalThis.location;
    else globalThis.location = originalLocation;
    if (originalHistory === undefined) delete globalThis.history;
    else globalThis.history = originalHistory;
  }
});

test("clearing the date chip removes only due_on and restores the unfiltered current list", async () => {
  const originalDocument = globalThis.document;
  const originalLocation = globalThis.location;
  const originalHistory = globalThis.history;
  installLocation("?keep=1&due_on=2026-10-05");
  globalThis.document = { querySelectorAll: () => [] };
  try {
    const elements = makeDateFilterElements();
    setTaskStatusFilter("todo");
    initTasks(elements, {}, {});
    const tasks = [
      { id: 41, title: "selected date", status: "todo", due_at: "2026-10-05" },
      { id: 42, title: "other date restored", status: "todo", due_at: "2026-10-06" },
    ];
    renderTasks(tasks);
    assert.doesNotMatch(elements.tasks.innerHTML, /other date restored/);
    await elements.clearDueOnFilterButton.click();
    assert.equal(parseDueOnDate(globalThis.location.search), null);
    assert.equal(new URL(globalThis.location.href).searchParams.get("keep"), "1");
    assert.equal(new URL(globalThis.location.href).hash, "#task-list");
    assert.equal(elements.dueOnFilterChip.hidden, true);
    assert.match(elements.tasks.innerHTML, /selected date/);
    assert.match(elements.tasks.innerHTML, /other date restored/);
  } finally {
    globalThis.document = originalDocument;
    if (originalLocation === undefined) delete globalThis.location;
    else globalThis.location = originalLocation;
    if (originalHistory === undefined) delete globalThis.history;
    else globalThis.history = originalHistory;
  }
});

test("local due filter uses local calendar days, intersects search results, and clears outside open statuses", async () => {
  const originalTimezone = process.env.TZ;
  const originalDocument = globalThis.document;
  process.env.TZ = "Asia/Hong_Kong";
  globalThis.document = { querySelectorAll: () => [] };

  try {
    const now = new Date(2026, 9, 5, 0, 30, 0);
    const tasks = [
      { id: 1, title: "today", status: "todo", due_at: "2026-10-05" },
      { id: 2, title: "overdue", status: "doing", due_at: "2026-10-04" },
      { id: 3, title: "tomorrow", status: "todo", due_at: "2026-10-06" },
      { id: 4, title: "no due", status: "todo", due_at: null },
      { id: 5, title: "invalid", status: "doing", due_at: "not-a-date" },
      { id: 6, title: "invalid calendar date", status: "todo", due_at: "2026-02-30" },
      { id: 7, title: "timestamp local today", status: "doing", due_at: "2026-10-04T17:00:00Z" },
      { id: 8, title: "timestamp local overdue", status: "todo", due_at: "2026-10-04T15:59:59Z" },
      { id: 9, title: "done", status: "done", due_at: "2026-10-01" },
      { id: 10, title: "dropped", status: "dropped", due_at: "2026-10-01" },
    ];

    assert.deepEqual(
      filterOpenTasksDueByLocalToday(tasks, now).map(({ id }) => id),
      [1, 2, 7, 8],
      "date-only values retain their calendar date, while zoned timestamps are grouped in Asia/Hong_Kong",
    );

    const taskList = new FakeElement();
    const taskCount = new FakeElement();
    const dueDateFilterButton = new FakeElement();
    initTasks({ tasks: taskList, taskCount, dueDateFilterButton }, {}, {});
    setTaskStatusFilter("todo");
    await dueDateFilterButton.click();
    assert.equal(getDueDateFilterActive(), true);
    assert.equal(dueDateFilterButton.attributes["aria-pressed"], "true");

    const matchingSearchResults = [tasks[0], tasks[2], tasks[4], tasks[6], tasks[8]];
    renderTasks(matchingSearchResults, true, now);
    assert.match(taskList.innerHTML, /today/);
    assert.match(taskList.innerHTML, /timestamp local today/);
    assert.deepEqual(
      [...taskList.innerHTML.matchAll(/data-toggle="(\d+)"/g)].map((match) => Number(match[1])),
      [1, 7],
    );
    assert.equal(taskCount.textContent, "2 个搜索结果");

    renderTasks([tasks[2], tasks[4]], true, now);
    assert.match(taskList.innerHTML, /没有找到同时符合搜索与到期筛选的开放任务/);
    assert.equal(taskCount.textContent, "0 个搜索结果");
    assert.deepEqual(filterOpenTasksDueByLocalToday([], now), []);

    setTaskStatusFilter("doing");
    assert.equal(getDueDateFilterActive(), true, "the local filter remains available across the two open-status tabs");
    setTaskStatusFilter("done");
    assert.equal(getDueDateFilterActive(), false);
    assert.equal(dueDateFilterButton.hidden, true);
    assert.equal(dueDateFilterButton.disabled, true);
    assert.equal(dueDateFilterButton.attributes["aria-pressed"], "false");
    setTaskStatusFilter("dropped");
    assert.equal(getDueDateFilterActive(), false);
    setTaskStatusFilter("todo");
    assert.equal(dueDateFilterButton.hidden, false);
    assert.equal(dueDateFilterButton.disabled, false);
  } finally {
    globalThis.document = originalDocument;
    if (originalTimezone === undefined) delete process.env.TZ;
    else process.env.TZ = originalTimezone;
  }
});

test("unestimated query filters open overdue-or-today search results and can be cleared without losing other query state", async () => {
  const originalDocument = globalThis.document;
  const originalLocation = globalThis.location;
  const originalHistory = globalThis.history;
  const originalTimezone = process.env.TZ;
  process.env.TZ = "Asia/Hong_Kong";
  installLocation("?keep=1&unestimated_due_by_today=1");
  globalThis.document = { querySelectorAll: () => [] };
  try {
    assert.equal(parseUnestimatedDueByToday("?unestimated_due_by_today=1"), true);
    for (const value of ["true", "0", "", "yes"]) {
      assert.equal(parseUnestimatedDueByToday(`?unestimated_due_by_today=${value}`), false);
    }

    const elements = makeDateFilterElements();
    setTaskStatusFilter("todo");
    initTasks(elements, {}, {});
    assert.equal(getUnestimatedDueByTodayFilterActive(), true);
    assert.equal(elements.unestimatedDueFilterChip.hidden, false);
    assert.equal(elements.dueDateFilterButton.disabled, true);
    assert.match(elements.dueDateFilterButton.title, /未估时筛选/);

    const now = new Date(2026, 9, 5, 0, 30);
    const searchResults = [
      { id: 51, title: "overdue null estimate", status: "todo", due_at: "2026-10-04", estimated_minutes: null },
      { id: 52, title: "today zero estimate", status: "todo", due_at: "2026-10-05", estimated_minutes: 0 },
      { id: 53, title: "today invalid estimate", status: "todo", due_at: "2026-10-05T15:59:59Z", estimated_minutes: "oops" },
      { id: 54, title: "today valid estimate", status: "todo", due_at: "2026-10-05", estimated_minutes: "25" },
      { id: 55, title: "future unestimated", status: "todo", due_at: "2026-10-06", estimated_minutes: null },
      { id: 56, title: "no deadline unestimated", status: "todo", due_at: null, estimated_minutes: null },
      { id: 57, title: "invalid deadline unestimated", status: "todo", due_at: "2026-02-30", estimated_minutes: null },
      { id: 58, title: "done overdue", status: "done", due_at: "2026-10-04", estimated_minutes: null },
      { id: 59, title: "dropped overdue", status: "dropped", due_at: "2026-10-04", estimated_minutes: 0 },
    ];
    renderTasks(searchResults, true, now);
    assert.deepEqual(
      [...elements.tasks.innerHTML.matchAll(/data-toggle="(\d+)"/g)].map((match) => Number(match[1])),
      [51, 52, 53],
    );
    assert.equal(elements.taskCount.textContent, "3 个搜索结果");
    assert.doesNotMatch(elements.tasks.innerHTML, /oopsm/);

    setTaskStatusFilter("doing");
    assert.equal(parseUnestimatedDueByToday(globalThis.location.search), true, "the filter survives the other open-status tab");
    renderTasks([
      { id: 60, title: "doing overdue unestimated", status: "doing", due_at: "2026-10-04", estimated_minutes: "NaN" },
      { id: 61, title: "doing today estimated", status: "doing", due_at: "2026-10-05", estimated_minutes: 5 },
    ], true, now);
    assert.match(elements.tasks.innerHTML, /doing overdue unestimated/);
    assert.doesNotMatch(elements.tasks.innerHTML, /doing today estimated/);

    await elements.clearUnestimatedDueFilterButton.click();
    assert.equal(getUnestimatedDueByTodayFilterActive(), false);
    assert.equal(parseUnestimatedDueByToday(globalThis.location.search), false);
    assert.equal(new URL(globalThis.location.href).searchParams.get("keep"), "1");
    assert.equal(new URL(globalThis.location.href).hash, "#task-list");
    assert.equal(elements.unestimatedDueFilterChip.hidden, true);
    assert.equal(elements.dueDateFilterButton.disabled, false);
    assert.match(elements.tasks.innerHTML, /doing today estimated/);

    installLocation("?keep=1&unestimated_due_by_today=1");
    initTasks(elements, {}, {});
    assert.equal(getUnestimatedDueByTodayFilterActive(), true);
    setTaskStatusFilter("done");
    assert.equal(getUnestimatedDueByTodayFilterActive(), false);
    assert.equal(parseUnestimatedDueByToday(globalThis.location.search), false);
    assert.equal(new URL(globalThis.location.href).searchParams.get("keep"), "1");
    assert.equal(elements.unestimatedDueFilterChip.hidden, true);
    assert.equal(elements.dueDateFilterButton.hidden, true);

    installLocation("?keep=1&unestimated_due_by_today=1");
    initTasks(elements, {}, {});
    setTaskStatusFilter("dropped");
    assert.equal(parseUnestimatedDueByToday(globalThis.location.search), false);
  } finally {
    globalThis.document = originalDocument;
    if (originalTimezone === undefined) delete process.env.TZ;
    else process.env.TZ = originalTimezone;
    if (originalLocation === undefined) delete globalThis.location;
    else globalThis.location = originalLocation;
  }
});

test("valid due_on has explicit priority over a simultaneous unestimated query", () => {
  const originalDocument = globalThis.document;
  const originalLocation = globalThis.location;
  const originalHistory = globalThis.history;
  installLocation("?keep=1&due_on=2026-10-05&unestimated_due_by_today=1");
  globalThis.document = { querySelectorAll: () => [] };
  try {
    const elements = makeDateFilterElements();
    setTaskStatusFilter("todo");
    initTasks(elements, {}, {});
    assert.equal(parseDueOnDate(globalThis.location.search), "2026-10-05");
    assert.equal(parseUnestimatedDueByToday(globalThis.location.search), false);
    assert.equal(getUnestimatedDueByTodayFilterActive(), false);
    assert.equal(elements.dueOnFilterChip.hidden, false);
    assert.equal(elements.unestimatedDueFilterChip.hidden, true);
    assert.equal(elements.dueDateFilterButton.disabled, true);
    assert.equal(new URL(globalThis.location.href).searchParams.get("keep"), "1");
  } finally {
    globalThis.document = originalDocument;
    if (originalLocation === undefined) delete globalThis.location;
    else globalThis.location = originalLocation;
    if (originalHistory === undefined) delete globalThis.history;
    else globalThis.history = originalHistory;
  }
});
