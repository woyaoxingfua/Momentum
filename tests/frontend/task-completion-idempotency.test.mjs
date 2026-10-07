import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { requestJson } from "../../src/momentum_agent/static/js/api.js";
import { createTaskCompletionAction } from "../../src/momentum_agent/static/js/task-completion-action.mjs";
import { completionResultMessage } from "../../src/momentum_agent/static/js/tasks.js";
import { renderTodayReview } from "../../src/momentum_agent/static/js/advice-review.mjs";

const UUID_A = "11111111-1111-4111-8111-111111111111";
const UUID_B = "22222222-2222-4222-8222-222222222222";
const UUID_C = "33333333-3333-4333-8333-333333333333";

class MemoryStorage {
  values = new Map();
  get length() { return this.values.size; }
  key(index) { return [...this.values.keys()][index] ?? null; }
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.listeners = new Map();
    this.attributes = {};
    this.textContent = "";
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this.classList = { add() {}, remove() {}, contains() { return false; } };
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  setAttribute(name, value) { this.attributes[name] = value; }
  async click() { if (!this.disabled && !this.hidden) return this.listeners.get("click")?.(); }
}

function findAll(element, className) {
  const found = [];
  if ((element.className || "").split(" ").includes(className)) found.push(element);
  for (const child of element.children || []) found.push(...findAll(child, className));
  return found;
}

function jsonResponse(payload, status = 200) {
  return { status, ok: status >= 200 && status < 300, json: async () => payload };
}

function completionStorageKey(userId, taskId) {
  return `momentum_task_completion_v1:${encodeURIComponent(userId)}:${encodeURIComponent(String(taskId))}`;
}

const review = {
  localDate: "2026-10-04",
  completed_count: 1,
  today_focus_actual_seconds: 30,
  today_focus_session_count: 1,
  completed_events: [],
};

test("ordinary task list and today-close share one synchronous lock and the same persisted key", async () => {
  const storage = new MemoryStorage();
  const calls = [];
  let finish;
  const gate = new Promise((resolve) => { finish = resolve; });
  const action = createTaskCompletionAction({
    storage,
    getUserId: () => "user-a",
    createId: () => UUID_A,
    requestJson: async (url, options) => {
      const saved = JSON.parse(storage.getItem(completionStorageKey("user-a", 51)));
      assert.equal(saved.status, "sending", "the intent is persisted synchronously before the request starts");
      calls.push({ url, options, saved });
      await gate;
      return { message: "任务完成" };
    },
  });

  const fromTaskList = action(51);
  const fromTodayClose = action(51);
  assert.equal(calls.length, 1, "both entry points share a single in-flight controller");
  const secondResult = await fromTodayClose;
  assert.equal(secondResult.status, "pending");
  assert.equal(secondResult.intent.idempotency_key, UUID_A);
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.body, undefined, "the done endpoint sends no body");
  assert.equal(calls[0].options.headers["Idempotency-Key"], UUID_A);
  finish();
  assert.equal((await fromTaskList).status, "success");
  assert.equal(storage.getItem(completionStorageKey("user-a", 51)), null);

  const [tasksSource, appSource, workerSource] = await Promise.all([
    readFile(new URL("../../src/momentum_agent/static/js/tasks.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/momentum_agent/static/js/app.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/momentum_agent/static/sw.js", import.meta.url), "utf8"),
  ]);
  assert.match(tasksSource, /completeTask\.retry\s*=\s*\(taskId/);
  assert.match(appSource, /completeTask\s+as\s+completeTaskAction/);
  assert.match(appSource, /onCompleteTask:\s*completeTaskAction/);
  assert.ok(workerSource.includes("/js/task-completion-action.mjs"), "the shared module is available to the offline app shell");
});

test("transport uncertainty survives reload; refresh does not send and explicit retry reuses exact key, method, URL, and body", async () => {
  const storage = new MemoryStorage();
  storage.setItem("momentum_token", "not-to-be-persisted");
  storage.setItem("momentum_user", "user-a");
  const originalStorage = globalThis.localStorage;
  const originalWindow = globalThis.window;
  const originalFetch = globalThis.fetch;
  globalThis.localStorage = storage;
  globalThis.window = { location: { href: "" } };
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options: { ...options, headers: { ...options.headers } } });
    const saved = JSON.parse(storage.getItem(completionStorageKey("user-a", 62)));
    assert.equal(saved.status, "sending");
    if (calls.length === 1) throw new TypeError("connection interrupted after POST");
    return jsonResponse({ message: "原始完成成功消息" });
  };
  try {
    const firstPage = createTaskCompletionAction({ requestJson, storage, getUserId: () => storage.getItem("momentum_user"), createId: () => UUID_A });
    const firstResult = await firstPage(62);
    assert.equal(firstResult.status, "uncertain");
    assert.equal(firstPage.getPendingIntent(62).status, "uncertain");
    assert.equal(calls.length, 1);

    const refreshedPage = createTaskCompletionAction({ requestJson, storage, getUserId: () => storage.getItem("momentum_user"), createId: () => UUID_B });
    assert.equal(refreshedPage.getPendingIntent(62).idempotency_key, UUID_A);
    assert.equal(calls.length, 1, "loading and reading pending intent never resends it");
    assert.equal((await refreshedPage(62)).status, "pending", "a normal completion click cannot silently replace or retry a pending intent");
    assert.equal(calls.length, 1);

    const retry = await refreshedPage.retry(62);
    assert.equal(retry.status, "success");
    assert.deepEqual(retry.response, { message: "原始完成成功消息" });
    assert.equal(calls.length, 2);
    for (const call of calls) {
      assert.equal(call.url, "/api/tasks/62/done");
      assert.equal(call.options.method, "POST");
      assert.equal(call.options.body, undefined);
      assert.equal(call.options.headers["Idempotency-Key"], UUID_A);
      assert.equal(call.options.headers.Authorization, "Bearer not-to-be-persisted");
    }
    assert.equal(storage.getItem(completionStorageKey("user-a", 62)), null);
    const serializedPending = [...storage.values.entries()].filter(([key]) => key.startsWith("momentum_task_completion_v1:"));
    assert.equal(JSON.stringify(serializedPending).includes("not-to-be-persisted"), false);
  } finally {
    globalThis.localStorage = originalStorage;
    globalThis.window = originalWindow;
    globalThis.fetch = originalFetch;
  }
});

test("a definite 200 clears pending; a later completion intent (after reopen) receives a fresh UUIDv4", async () => {
  const storage = new MemoryStorage();
  const generated = [UUID_A, UUID_B];
  const sent = [];
  const action = createTaskCompletionAction({
    storage,
    getUserId: () => "user-a",
    createId: () => generated.shift(),
    requestJson: async (_url, options) => {
      sent.push(options.headers["Idempotency-Key"]);
      return { message: "服务器原响应" };
    },
  });
  assert.equal((await action(77)).status, "success");
  assert.equal(storage.getItem(completionStorageKey("user-a", 77)), null);
  await action(77); // Represents an explicit later completion after the task is reopened.
  assert.deepEqual(sent, [UUID_A, UUID_B]);
  assert.notEqual(sent[0], sent[1]);
  assert.match(sent[1], /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
});

test("pending completion records are isolated by authenticated user", async () => {
  const storage = new MemoryStorage();
  let currentUser = "user-a";
  const generated = [UUID_A, UUID_B];
  const action = createTaskCompletionAction({
    storage,
    getUserId: () => currentUser,
    createId: () => generated.shift(),
    requestJson: async () => {
      if (currentUser === "user-a") throw new TypeError("response lost");
      return { message: "user B result" };
    },
  });

  assert.equal((await action(88)).status, "uncertain");
  assert.equal(action.getPendingIntent(88).idempotency_key, UUID_A);
  currentUser = "user-b";
  assert.equal(action.getPendingIntent(88), null);
  assert.equal((await action(88)).status, "success");
  currentUser = "user-a";
  assert.equal(action.getPendingIntent(88).idempotency_key, UUID_A);
  assert.equal(storage.getItem(completionStorageKey("user-b", 88)), null);
});

test("today-close renders the shared pending intent as unconfirmed and sends only after explicit retry", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = { createElement: (tag) => new FakeElement(tag) };
  try {
    let pending = { task_id: "95", idempotency_key: UUID_A, status: "uncertain" };
    let normalRequests = 0;
    let retryRequests = 0;
    const onCompleteTask = async () => { normalRequests += 1; };
    onCompleteTask.getPendingIntent = () => pending;
    onCompleteTask.retry = async (taskId) => {
      retryRequests += 1;
      assert.equal(String(taskId), "95");
      assert.equal(pending.idempotency_key, UUID_A);
      pending = null;
      return { status: "success", response: { message: "原始服务器消息" } };
    };
    const mount = new FakeElement("section");
    renderTodayReview(mount, review, {
      unfinishedTasks: [{ id: 95, status: "todo", title: "待确认完成" }],
      onCompleteTask,
    });

    assert.equal(normalRequests, 0, "render/refresh never sends a completion request");
    const primary = findAll(mount, "today-review-complete")[0];
    const retry = findAll(mount, "today-review-completion-retry")[0];
    assert.equal(primary.textContent, "完成未确认");
    assert.equal(primary.disabled, true);
    assert.equal(retry.hidden, false);
    assert.equal(retry.disabled, false);
    assert.match(findAll(mount, "today-review-completion-feedback")[0].textContent, /尚未确认.*显式/);

    await primary.click();
    assert.equal(normalRequests, 0);
    await retry.click();
    assert.equal(retryRequests, 1);
    assert.equal(normalRequests, 0);
    assert.equal(retry.hidden, true);
  } finally {
    globalThis.document = originalDocument;
  }
});

test("definite 400/404/409 responses clear only the completed intent and present both 400 contract errors", async () => {
  const cases = [
    { status: 400, code: "idempotency_key_required" },
    { status: 400, code: "idempotency_key_invalid" },
    { status: 404, code: "not_found" },
    { status: 409, code: "idempotency_conflict" },
  ];
  for (const item of cases) {
    const storage = new MemoryStorage();
    const usedKeys = [];
    const action = createTaskCompletionAction({
      storage,
      getUserId: () => "user-a",
      createId: () => usedKeys.length ? UUID_B : UUID_A,
      requestJson: async (_url, options) => {
        usedKeys.push(options.headers["Idempotency-Key"]);
        throw Object.assign(new Error(item.code), { status: item.status, payload: { error: item.code } });
      },
    });
    const outcome = await action(item.status);
    assert.equal(outcome.status, "error");
    assert.equal(action.getPendingIntent(item.status), null, `${item.status} is a definite response`);
    const message = completionResultMessage(outcome);
    assert.match(message, new RegExp(String(item.status)));
    if (item.status === 400) assert.match(message, /Idempotency-Key/);
    if (item.status === 404) assert.match(message, /任务不存在/);
    if (item.status === 409) assert.match(message, /UUID.*不同/);
    assert.equal((await action(item.status)).status, "error", "only a later explicit call starts another intent");
    assert.deepEqual(usedKeys, [UUID_A, UUID_B]);
  }
});

test("400, 404, and 409 render distinct definite errors and do not masquerade as success", async () => {
  const cases = [
    { status: 400, code: "idempotency_key_required", expected: /400.*Idempotency-Key/ },
    { status: 400, code: "idempotency_key_invalid", expected: /400.*UUIDv4/ },
    { status: 404, code: "task_not_found", expected: /404.*任务不存在/ },
    { status: 409, code: "idempotency_conflict", expected: /409.*UUID.*不同/ },
  ];
  const originalDocument = globalThis.document;
  globalThis.document = { createElement: (tag) => new FakeElement(tag) };
  try {
    for (const item of cases) {
      const message = completionResultMessage({
        status: "error",
        error: Object.assign(new Error(item.code), { status: item.status, payload: { error: item.code } }),
      });
      assert.match(message, item.expected);
      assert.doesNotMatch(message, /完成成功|已完成/);

      const feedbackValue = { message, kind: "error" };
      const onCompleteTask = async () => ({ status: "error", error: Object.assign(new Error(item.code), { status: item.status }) });
      onCompleteTask.getPendingIntent = () => null;
      onCompleteTask.getCompletionFeedback = () => feedbackValue;
      const mount = new FakeElement("section");
      renderTodayReview(mount, review, {
        unfinishedTasks: [{ id: 90 + item.status, status: "todo", title: `错误 ${item.status}` }],
        onCompleteTask,
      });
      await findAll(mount, "today-review-complete")[0].click();
      const feedback = findAll(mount, "today-review-completion-feedback")[0];
      assert.equal(feedback.textContent, message);
      assert.equal(feedback.attributes.role, "alert");
    }
  } finally {
    globalThis.document = originalDocument;
  }
});
