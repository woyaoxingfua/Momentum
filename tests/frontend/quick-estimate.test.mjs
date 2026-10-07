import assert from "node:assert/strict";
import test from "node:test";

import { initTasks, renderTasks, setTaskStatusFilter } from "../../src/momentum_agent/static/js/tasks.js";
import { localDateKey } from "../../src/momentum_agent/static/js/daily-workload.mjs";

class MemoryStorage {
  values = new Map();
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

function datasetName(attribute) {
  return attribute.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
}

class FakeButton {
  constructor(attributes) {
    this.dataset = {};
    this.listeners = new Map();
    this.attributes = {};
    this.disabled = /(?:^|\s)disabled(?:\s|=|$)/.test(attributes);
    this.classes = new Set((/\bclass="([^"]*)"/.exec(attributes)?.[1] || "").split(/\s+/).filter(Boolean));
    for (const [, name, value] of attributes.matchAll(/\b(data-[a-z-]+)="([^"]*)"/g)) {
      this.dataset[datasetName(name.slice(5))] = value;
    }
    for (const [, name, value] of attributes.matchAll(/\b([a-z-]+)="([^"]*)"/g)) this.attributes[name] = value;
    this.classList = { contains: (name) => this.classes.has(name) };
  }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  async click() {
    if (this.disabled) return;
    return this.listeners.get("click")?.({ preventDefault() {}, stopPropagation() {} });
  }
}

class FakeTaskList {
  constructor() {
    this.markup = "";
    this.buttons = [];
  }
  set innerHTML(markup) {
    this.markup = String(markup);
    this.buttons = [...this.markup.matchAll(/<button\b([^>]*)>/g)].map((match) => new FakeButton(match[1]));
  }
  get innerHTML() { return this.markup; }
  querySelectorAll(selector) {
    const attribute = /\[data-([a-z-]+)\]/.exec(selector)?.[1];
    if (!attribute) return [];
    const key = datasetName(attribute);
    return this.buttons.filter((button) => Object.hasOwn(button.dataset, key));
  }
  getButton(attribute, value = undefined) {
    const key = datasetName(attribute);
    return this.buttons.find((button) => Object.hasOwn(button.dataset, key) && (value === undefined || button.dataset[key] === String(value))) || null;
  }
}

class FakeElement {
  constructor() {
    this.value = "";
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.listeners = new Map();
    this.attributes = {};
    this.classes = new Set();
    this.classList = { toggle: (name, force) => force ? this.classes.add(name) : this.classes.delete(name) };
  }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  setAttribute(name, value) { this.attributes[name] = value; }
  async click() {
    if (this.disabled || this.hidden) return;
    return this.listeners.get("click")?.({ preventDefault() {} });
  }
}

function makeTask(id, overrides = {}) {
  return {
    id,
    title: `任务 ${id}`,
    status: "todo",
    due_at: localDateKey(new Date()),
    priority: "high",
    estimated_minutes: null,
    notes: "保留 notes",
    tags: ["保留 tag"],
    ...overrides,
  };
}

function createHarness({ initialTasks, fetchRequest }) {
  const old = {
    document: globalThis.document,
    location: globalThis.location,
    history: globalThis.history,
    localStorage: globalThis.localStorage,
    fetch: globalThis.fetch,
    timezone: process.env.TZ,
  };
  process.env.TZ = "Asia/Hong_Kong";
  const storage = new MemoryStorage();
  storage.setItem("momentum_token", "node-mock-token");
  globalThis.localStorage = storage;
  const taskList = new FakeTaskList();
  const taskCount = new FakeElement();
  const quickEstimateStatus = new FakeElement();
  const clearUnestimatedDueFilterButton = new FakeElement();
  const elementsById = new Map([
    ["quickEstimateStatus", quickEstimateStatus],
    ["clearUnestimatedDueFilterButton", clearUnestimatedDueFilterButton],
  ]);
  globalThis.document = {
    getElementById: (id) => elementsById.get(id) || null,
    querySelectorAll: (selector) => taskList.querySelectorAll(selector),
  };
  const location = {
    href: "https://momentum.example/?unestimated_due_by_today=1#task-list",
    get search() { return new URL(this.href).search; },
  };
  globalThis.location = location;
  globalThis.history = {
    state: null,
    replaceState(_state, _title, path) { location.href = new URL(path, location.href).href; },
  };
  globalThis.fetch = fetchRequest;
  initTasks({ tasks: taskList, taskCount, quickEstimateStatus, clearUnestimatedDueFilterButton, unestimatedDueFilterChip: new FakeElement() }, {}, { postponeRetryButton: null });
  setTaskStatusFilter("todo");
  renderTasks(initialTasks, false, new Date());

  return {
    taskList,
    taskCount,
    quickEstimateStatus,
    clearUnestimatedDueFilterButton,
    storage,
    restore() {
      globalThis.document = old.document;
      globalThis.location = old.location;
      globalThis.history = old.history;
      globalThis.localStorage = old.localStorage;
      globalThis.fetch = old.fetch;
      if (old.timezone === undefined) delete process.env.TZ;
      else process.env.TZ = old.timezone;
    },
  };
}

function jsonResponse(status, payload) {
  return { status, ok: status >= 200 && status < 300, json: async () => payload };
}

function installTaskApi({ task, requests, putHandler = null }) {
  return async (url, options = {}) => {
    const method = options.method || "GET";
    requests.push({ url, method, options });
    if (method === "PUT") {
      if (putHandler) return putHandler({ url, options, task });
      Object.assign(task, JSON.parse(options.body));
      return jsonResponse(200, { task });
    }
    if (method === "GET") return jsonResponse(200, { tasks: [task] });
    throw new Error(`Unexpected request: ${method} ${url}`);
  };
}

test("quick estimate controls render only for rows in the shared unestimated-due filter and require an explicit selected save", async () => {
  const today = localDateKey(new Date());
  const target = makeTask(31);
  const otherOpen = makeTask(32, { estimated_minutes: 20 });
  const future = makeTask(33, { due_at: "2999-12-31" });
  const done = makeTask(34, { status: "done" });
  const requests = [];
  const harness = createHarness({
    initialTasks: [target, otherOpen, future, done],
    fetchRequest: installTaskApi({ task: target, requests }),
  });

  try {
    assert.match(harness.taskList.innerHTML, /data-estimate-select="31" data-estimate-minutes="15"/);
    assert.match(harness.taskList.innerHTML, /data-estimate-minutes="25"/);
    assert.match(harness.taskList.innerHTML, /data-estimate-minutes="45"/);
    assert.match(harness.taskList.innerHTML, /data-estimate-minutes="60"/);
    assert.doesNotMatch(harness.taskList.innerHTML, /data-estimate-select="32"/);
    assert.doesNotMatch(harness.taskList.innerHTML, /data-estimate-select="33"/);
    assert.doesNotMatch(harness.taskList.innerHTML, /data-estimate-select="34"/);

    const initiallyDisabledSave = harness.taskList.getButton("estimate-save", "31");
    assert.equal(initiallyDisabledSave.disabled, true, "no selection means no write is possible");
    await initiallyDisabledSave.click();
    assert.equal(requests.length, 0);

    await harness.clearUnestimatedDueFilterButton.click();
    assert.doesNotMatch(harness.taskList.innerHTML, /data-estimate-select=/, "controls disappear when the filter is cleared");
  } finally {
    harness.restore();
  }
});

test("one-field PUT for the row ID is followed by GET, and a successful estimate removes the row", async () => {
  const target = makeTask(41);
  const originalDue = target.due_at;
  const requests = [];
  const harness = createHarness({
    initialTasks: [target],
    fetchRequest: installTaskApi({ task: target, requests }),
  });
  try {
    const selectedButton = harness.taskList.buttons.find((button) => button.dataset.estimateSelect === "41" && button.dataset.estimateMinutes === "25");
    await selectedButton.click();
    assert.equal(harness.taskList.buttons.find((button) => button.dataset.estimateSelect === "41" && button.dataset.estimateMinutes === "25").getAttribute("aria-pressed"), "true");
    await harness.taskList.getButton("estimate-save", "41").click();

    const puts = requests.filter((request) => request.method === "PUT");
    assert.equal(puts.length, 1);
    assert.equal(puts[0].url, "/api/tasks/41", "the row ID is used for the PUT path");
    assert.deepEqual(JSON.parse(puts[0].options.body), { estimated_minutes: 25 });
    assert.deepEqual(Object.keys(JSON.parse(puts[0].options.body)), ["estimated_minutes"], "no title, due date, priority, notes, tags, or other fields are submitted");
    assert.deepEqual(requests.map(({ method }) => method), ["PUT", "GET"]);
    assert.equal(target.title, "任务 41");
    assert.equal(target.due_at, originalDue);
    assert.equal(target.priority, "high");
    assert.equal(target.notes, "保留 notes");
    assert.deepEqual(target.tags, ["保留 tag"]);
    assert.equal(target.estimated_minutes, 25);
    assert.doesNotMatch(harness.taskList.innerHTML, /data-toggle="41"/, "GET refresh removes the now-estimated row from the unestimated result");
    assert.match(harness.quickEstimateStatus.textContent, /已保存 25 分钟.*已移出未估时结果/);
    assert.ok(harness.storage.getItem("momentum_daily_workload_refresh"), "a refresh marker notifies an open Stats tab");
  } finally {
    harness.restore();
  }
});

test("a definite 4xx keeps the task and its estimate unchanged, shows an error, and permits another explicit save", async () => {
  const target = makeTask(51);
  const requests = [];
  let putCount = 0;
  const fetchRequest = installTaskApi({
    task: target,
    requests,
    putHandler: async ({ options }) => {
      putCount += 1;
      if (putCount === 1) return jsonResponse(400, { error: "invalid estimate" });
      Object.assign(target, JSON.parse(options.body));
      return jsonResponse(200, { task: target });
    },
  });
  const harness = createHarness({ initialTasks: [target], fetchRequest });

  try {
    const sixty = harness.taskList.buttons.find((button) => button.dataset.estimateSelect === "51" && button.dataset.estimateMinutes === "60");
    await sixty?.click();
    await harness.taskList.getButton("estimate-save", "51").click();

    assert.equal(target.estimated_minutes, null);
    assert.match(harness.taskList.innerHTML, /保存失败（HTTP 400）/);
    assert.match(harness.taskList.innerHTML, /data-toggle="51"/, "4xx retains the task in the list");
    assert.equal(requests.filter((request) => request.method === "GET").length, 0, "a definite 4xx does not trigger a reconciliation GET");
    assert.equal(harness.taskList.getButton("estimate-save", "51").disabled, false);

    const fortyFive = harness.taskList.buttons.find((button) => button.dataset.estimateSelect === "51" && button.dataset.estimateMinutes === "45");
    await fortyFive?.click();
    await harness.taskList.getButton("estimate-save", "51").click();
    assert.equal(target.estimated_minutes, 45);
    assert.equal(requests.filter((request) => request.method === "PUT").length, 2);
    assert.deepEqual(requests.filter((request) => request.method === "PUT").map(({ options }) => JSON.parse(options.body)), [
      { estimated_minutes: 60 },
      { estimated_minutes: 45 },
    ]);
  } finally {
    harness.restore();
  }
});

test("a statusless disconnect never auto-retries; only explicit GET reconciliation enables an explicit same-value retry", async () => {
  const target = makeTask(61);
  const requests = [];
  let putCount = 0;
  const fetchRequest = installTaskApi({
    task: target,
    requests,
    putHandler: async ({ options }) => {
      putCount += 1;
      if (putCount === 1) throw new TypeError("socket disconnected");
      Object.assign(target, JSON.parse(options.body));
      return jsonResponse(200, { task: target });
    },
  });
  const harness = createHarness({ initialTasks: [target], fetchRequest });

  try {
    await harness.taskList.getButton("estimate-select", "61").click();
    const twentyFive = harness.taskList.buttons.find((button) => button.dataset.estimateSelect === "61" && button.dataset.estimateMinutes === "25");
    await twentyFive.click();
    await harness.taskList.getButton("estimate-save", "61").click();

    assert.equal(requests.filter((request) => request.method === "PUT").length, 1);
    assert.equal(requests.filter((request) => request.method === "GET").length, 0, "unknown PUT outcome is not automatically refreshed");
    assert.match(harness.taskList.innerHTML, /结果未确认/);
    assert.match(harness.taskList.innerHTML, /不会自动重发/);
    assert.ok(harness.taskList.getButton("estimate-check", "61"));
    assert.equal(harness.taskList.getButton("estimate-save", "61"), null, "save/retry is unavailable until the user requests GET reconciliation");

    await harness.taskList.getButton("estimate-check", "61").click();
    assert.equal(requests.filter((request) => request.method === "GET").length, 1);
    assert.equal(requests.filter((request) => request.method === "PUT").length, 1, "GET reconciliation never resends the PUT");
    assert.match(harness.taskList.innerHTML, /GET 已核对：任务当前仍未估时/);
    assert.match(harness.taskList.getButton("estimate-save", "61").textContent || harness.taskList.innerHTML, /显式重试 25 分钟/);

    await harness.taskList.getButton("estimate-save", "61").click();
    const puts = requests.filter((request) => request.method === "PUT");
    assert.equal(puts.length, 2, "the second PUT occurs only after the explicit retry click");
    assert.deepEqual(puts.map(({ options }) => JSON.parse(options.body)), [
      { estimated_minutes: 25 },
      { estimated_minutes: 25 },
    ], "the user-confirmed retry uses the same selected value and minimal payload");
    assert.equal(requests.filter((request) => request.method === "GET").length, 2);
    assert.equal(target.estimated_minutes, 25);
    assert.doesNotMatch(harness.taskList.innerHTML, /data-toggle="61"/);
  } finally {
    harness.restore();
  }
});

test("an in-flight quick estimate locks repeated clicks until the request and refresh finish", async () => {
  const target = makeTask(71);
  const requests = [];
  let releasePut;
  const putResponse = new Promise((resolve) => { releasePut = resolve; });
  const fetchRequest = installTaskApi({
    task: target,
    requests,
    putHandler: async () => putResponse,
  });
  const harness = createHarness({ initialTasks: [target], fetchRequest });

  try {
    await harness.taskList.getButton("estimate-select", "71").click();
    const sixty = harness.taskList.buttons.find((button) => button.dataset.estimateSelect === "71" && button.dataset.estimateMinutes === "60");
    await sixty.click();
    const staleSaveButton = harness.taskList.getButton("estimate-save", "71");
    const firstClick = staleSaveButton.click();
    await new Promise((resolve) => setImmediate(resolve));
    await staleSaveButton.click();
    assert.equal(requests.filter((request) => request.method === "PUT").length, 1);
    assert.equal(harness.taskList.getButton("estimate-save", "71").disabled, true);

    Object.assign(target, { estimated_minutes: 60 });
    releasePut(jsonResponse(200, { task: target }));
    await firstClick;
    assert.equal(requests.filter((request) => request.method === "PUT").length, 1);
    assert.equal(requests.filter((request) => request.method === "GET").length, 1);
  } finally {
    harness.restore();
  }
});
