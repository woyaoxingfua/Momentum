import assert from "node:assert/strict";
import test from "node:test";
import { requestJson } from "../../src/momentum_agent/static/js/api.js";
import { createTaskCompletionAction } from "../../src/momentum_agent/static/js/task-completion-action.mjs";

const UUID_A = "11111111-1111-4111-8111-111111111111";
const UUID_B = "22222222-2222-4222-8222-222222222222";

class MemoryStorage {
  values = new Map();
  get length() { return this.values.size; }
  key(index) { return [...this.values.keys()][index] ?? null; }
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

function jsonResponse(payload) {
  return { status: 200, ok: true, json: async () => payload };
}

function completionStorageKey(userId, taskId) {
  return `momentum_task_completion_v1:${encodeURIComponent(userId)}:${encodeURIComponent(String(taskId))}`;
}

test("A→B→A keeps an uncertain completion private and explicitly retries only A's original intent", async () => {
  const storage = new MemoryStorage();
  storage.setItem("momentum_user", "user-a");
  storage.setItem("momentum_token", "token-a");
  const originalStorage = globalThis.localStorage;
  const originalWindow = globalThis.window;
  const originalFetch = globalThis.fetch;
  const userByToken = new Map([["token-a", "user-a"], ["token-b", "user-b"]]);
  const requests = [];
  const appliedCompletions = new Map();
  let loseFirstAResponse = true;
  globalThis.localStorage = storage;
  globalThis.window = { location: { href: "" } };
  globalThis.fetch = async (url, options) => {
    const userId = userByToken.get(options.headers.Authorization?.replace(/^Bearer /, ""));
    assert.ok(userId, "the request must carry the active user's auth token");
    const idempotencyKey = options.headers["Idempotency-Key"];
    requests.push({
      userId,
      url,
      method: options.method,
      body: options.body,
      idempotencyKey,
    });

    const intentKey = `${userId}:${url}:${idempotencyKey}`;
    if (!appliedCompletions.has(intentKey)) {
      appliedCompletions.set(intentKey, { userId, taskId: "96" });
    }
    if (userId === "user-a" && loseFirstAResponse) {
      loseFirstAResponse = false;
      throw new TypeError("response lost after server applied completion");
    }
    return jsonResponse({ message: `${userId} completion result` });
  };

  const generatedIds = [UUID_A, UUID_B];
  const createController = () => createTaskCompletionAction({
    requestJson,
    storage,
    getUserId: () => storage.getItem("momentum_user"),
    createId: () => generatedIds.shift(),
  });
  const keyA = completionStorageKey("user-a", 96);
  const keyB = completionStorageKey("user-b", 96);

  try {
    const firstA = createController();
    assert.equal((await firstA(96)).status, "uncertain");
    const originalARecord = storage.getItem(keyA);
    assert.deepEqual(JSON.parse(originalARecord), {
      task_id: "96",
      idempotency_key: UUID_A,
      status: "uncertain",
    });
    assert.deepEqual(requests[0], {
      userId: "user-a",
      url: "/api/tasks/96/done",
      method: "POST",
      body: undefined,
      idempotencyKey: UUID_A,
    });
    assert.equal(requests.length, 1);

    const refreshedA = createController();
    assert.equal(refreshedA.getPendingIntent(96).idempotency_key, UUID_A);
    assert.deepEqual(refreshedA.getPendingIntents(), [{
      task_id: "96",
      idempotency_key: UUID_A,
      status: "uncertain",
    }]);
    assert.equal(requests.length, 1, "creating a controller and reading A's pending state do not POST");

    storage.setItem("momentum_user", "user-b");
    storage.setItem("momentum_token", "token-b");
    const pageB = createController();
    assert.equal(pageB.getPendingIntent(96), null);
    assert.deepEqual(pageB.getPendingIntents(), []);
    assert.equal(storage.getItem(keyA), originalARecord, "B cannot alter A's pending record");
    assert.equal((await pageB.retry(96)).status, "error", "B cannot explicitly retry A's pending intent");
    assert.equal(requests.length, 1, "B's rejected retry sends no request");

    assert.equal((await pageB(96)).status, "success");
    assert.equal(requests.length, 2);
    assert.deepEqual(requests[1], {
      userId: "user-b",
      url: "/api/tasks/96/done",
      method: "POST",
      body: undefined,
      idempotencyKey: UUID_B,
    }, "B may create only its own intent, never reuse A's key or body");
    assert.equal(storage.getItem(keyB), null);
    assert.equal(storage.getItem(keyA), originalARecord);

    storage.setItem("momentum_user", "user-a");
    storage.setItem("momentum_token", "token-a");
    const returnedA = createController();
    assert.equal(returnedA.getPendingIntent(96).idempotency_key, UUID_A);
    assert.equal(requests.length, 2, "switching back and creating A's controller do not auto-POST");
    assert.equal((await returnedA(96)).status, "pending", "ordinary completion does not silently retry A's uncertain intent");
    assert.equal(requests.length, 2);

    assert.equal((await returnedA.retry(96)).status, "success");
    assert.equal(requests.length, 3);
    assert.deepEqual(requests[2], requests[0], "A's explicit retry reuses the exact user, URL, method, empty body, and UUID");
    assert.equal(storage.getItem(keyA), null);
    assert.equal(requests.filter(({ userId, idempotencyKey }) => userId === "user-b" && idempotencyKey === UUID_A).length, 0);
    assert.equal(appliedCompletions.size, 2, "A's two HTTP attempts represent one completion effect; B has a separate effect");
    assert.deepEqual(appliedCompletions.get(`user-a:/api/tasks/96/done:${UUID_A}`), { userId: "user-a", taskId: "96" });
    assert.deepEqual(appliedCompletions.get(`user-b:/api/tasks/96/done:${UUID_B}`), { userId: "user-b", taskId: "96" });
    assert.deepEqual(generatedIds, [], "only A's first intent and B's separate intent generate UUIDs");
  } finally {
    globalThis.localStorage = originalStorage;
    globalThis.window = originalWindow;
    globalThis.fetch = originalFetch;
  }
});
