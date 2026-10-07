import assert from "node:assert/strict";
import test from "node:test";

import { fromDatetimeLocal, toDatetimeLocal } from "../../src/momentum_agent/static/js/api.js";
import { initTasks, openTaskEditDialog, saveEdit } from "../../src/momentum_agent/static/js/tasks.js";

class MemoryStorage {
  values = new Map();
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

class FakeElement {
  constructor() {
    this.value = "";
    this.textContent = "";
    this.innerHTML = "";
    this.listeners = new Map();
    this.open = false;
  }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  showModal() { this.open = true; }
  close() { this.open = false; }
}

test("task editor renders and saves the selected Hong Kong wall time as UTC, then syncs the same task into today-close", async () => {
  const oldTz = process.env.TZ;
  const oldDocument = globalThis.document;
  const oldStorage = globalThis.localStorage;
  const oldFetch = globalThis.fetch;
  process.env.TZ = "Asia/Hong_Kong";
  const storage = new MemoryStorage();
  storage.setItem("momentum_token", "node-only-mock-token");
  storage.setItem("momentum_user", "node-only-mock-user");
  globalThis.localStorage = storage;
  globalThis.document = { querySelectorAll: () => [] };

  const target = { id: 31, title: "保持标题", status: "todo", due_at: null, priority: "medium", tags: [] };
  const requests = [];
  const updates = [];
  const fields = {
    editTaskId: new FakeElement(), editTitle: new FakeElement(), editDue: new FakeElement(),
    editPriority: new FakeElement(), editEstimate: new FakeElement(), editTags: new FakeElement(),
    editNotes: new FakeElement(), editDialog: new FakeElement(),
  };
  const taskList = new FakeElement();
  const taskCount = new FakeElement();
  const savedTask = { ...target, due_at: "2026-10-05T01:20:00+00:00" };
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    const payload = options.method === "PUT" ? { message: "updated" } : { tasks: [savedTask] };
    return { status: 200, ok: true, json: async () => payload };
  };

  try {
    initTasks({ tasks: taskList, taskCount }, fields, {
      postponeRetryButton: null,
    }, (task) => updates.push(task));
    await openTaskEditDialog(target);
    assert.equal(fields.editDialog.open, true);
    assert.equal(fields.editDue.value, "", "a task without a deadline opens with an empty datetime-local field");
    fields.editDue.value = "2026-10-05T09:20";

    await saveEdit({ preventDefault() {} });

    const put = requests.filter(({ options }) => options.method === "PUT");
    assert.equal(put.length, 1);
    assert.equal(put[0].url, "/api/tasks/31");
    const body = JSON.parse(put[0].options.body);
    assert.equal(body.due_at, "2026-10-05T01:20:00.000Z");
    assert.equal(body.title, "保持标题");
    assert.equal(Object.hasOwn(body, "status"), false, "the edit save never submits a status transition");
    assert.equal(fields.editDialog.open, false);
    assert.equal(updates.length, 1);
    assert.deepEqual(
      { id: updates[0].id, title: updates[0].title, status: updates[0].status, due_at: updates[0].due_at },
      { id: 31, title: "保持标题", status: "todo", due_at: "2026-10-05T01:20:00.000Z" },
    );
    assert.equal(toDatetimeLocal("2026-10-05T01:20:00Z"), "2026-10-05T09:20");
    assert.equal(fromDatetimeLocal(""), null);
  } finally {
    globalThis.document = oldDocument;
    globalThis.localStorage = oldStorage;
    globalThis.fetch = oldFetch;
    if (oldTz === undefined) delete process.env.TZ;
    else process.env.TZ = oldTz;
  }
});
