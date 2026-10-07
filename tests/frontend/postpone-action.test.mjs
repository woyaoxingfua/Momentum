import assert from "node:assert/strict";
import test from "node:test";
import {
  createTaskPostponeAction,
  postponeIntentStorageKey,
} from "../../src/momentum_agent/static/js/postpone-action.mjs";

const originalTask = { id: 41, status: "todo", title: "隔离 mock task", due_at: "2026-10-04T12:00:00Z", updated_at: "2026-10-01T12:00:00Z" };
const updatedTask = { ...originalTask, due_at: "2026-10-05T12:00:00Z", updated_at: "2026-10-04T12:00:01Z" };
const UUID_A = "11111111-1111-4111-8111-111111111111";
const UUID_B = "22222222-2222-4222-8222-222222222222";
const UUID_C = "33333333-3333-4333-8333-333333333333";

class MemoryStorage {
  values = new Map();
  writes = [];
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) {
    this.writes.push({ type: "set", key, value: String(value) });
    this.values.set(key, String(value));
  }
  removeItem(key) {
    this.writes.push({ type: "remove", key });
    this.values.delete(key);
  }
  keys() { return [...this.values.keys()]; }
}

function createError(status, message, payload = { error: message }) {
  return Object.assign(new Error(message), { status, payload });
}

function createAction({ storage = new MemoryStorage(), user = "user-a", ids = [UUID_A], ...options } = {}) {
  let currentUser = user;
  let nextId = 0;
  const action = createTaskPostponeAction({
    requestJson: options.requestJson,
    refreshTaskList: options.refreshTaskList,
    onTaskUpdated: options.onTaskUpdated,
    onTaskObserved: options.onTaskObserved,
    storage,
    getUserId: () => currentUser,
    createId: () => ids[nextId++ % ids.length],
    now: () => new Date("2026-10-04T12:00:00.000Z"),
  });
  return { action, storage, setUser(value) { currentUser = value; }, getUser() { return currentUser; } };
}

function savedIntent(storage, userId = "user-a", taskId = originalTask.id) {
  const key = postponeIntentStorageKey(userId, taskId);
  const raw = storage.getItem(key);
  return raw === null ? null : JSON.parse(raw);
}

test("first explicit click persists a complete UUIDv4 intent before one fixed POST; success clears it and next click creates a new intent", async () => {
  const storage = new MemoryStorage();
  const calls = [];
  let expectedKey = UUID_A;
  let dueAt = originalTask.due_at;
  const { action } = createAction({ storage, ids: [UUID_A, UUID_B], requestJson: async (url, options = {}) => {
    calls.push({ url, options });
    if (url.endsWith("/postpone")) {
      const stored = savedIntent(storage);
      assert.ok(stored, "the request must not be sent before local persistence");
      assert.equal(stored.status, "sending");
      assert.equal(stored.idempotency_key, expectedKey);
      assert.equal(stored.task_id, originalTask.id);
      assert.equal(stored.days, 1);
      assert.equal(stored.body, '{"days":1}');
      assert.match(stored.fingerprint, /\"method\":\"POST\"/);
      assert.ok(Number.isFinite(Date.parse(stored.created_at)));
      assert.equal(stored.version, 1);
      assert.equal(stored.user_id, "user-a");
      assert.deepEqual(Object.keys(options.headers), ["Idempotency-Key"]);
      assert.equal(options.headers["Idempotency-Key"], expectedKey);
      assert.equal(options.body, '{"days":1}');
      dueAt = new Date(Date.parse(dueAt) + 86400000).toISOString();
      return { message: "已顺延" };
    }
    if (url.endsWith("/with-subtasks")) return { task: { ...originalTask, due_at: dueAt }, subtasks: [] };
    throw new Error(`unexpected request ${url}`);
  } });

  const first = await action(originalTask);
  assert.equal(first.due_at, "2026-10-05T12:00:00.000Z");
  assert.equal(savedIntent(storage), null);
  assert.equal(calls.filter(({ url }) => url.endsWith("/postpone")).length, 1);
  assert.deepEqual(JSON.parse(calls[0].options.body), { days: 1 });
  assert.equal(calls[1].url, "/api/tasks/41/with-subtasks");
  assert.equal(storage.keys().length, 0, "settlement removes only this user's task intent");

  expectedKey = UUID_B;
  const second = await action(originalTask);
  assert.equal(second.due_at, "2026-10-06T12:00:00.000Z");
  const posts = calls.filter(({ url }) => url.endsWith("/postpone"));
  assert.equal(posts.length, 2);
  assert.equal(posts[0].options.headers["Idempotency-Key"], UUID_A);
  assert.equal(posts[1].options.headers["Idempotency-Key"], UUID_B);
  assert.notEqual(posts[0].options.headers["Idempotency-Key"], posts[1].options.headers["Idempotency-Key"]);
});

test("ambiguous failure is persisted; reload performs no POST; explicit retry reuses exact UUID and body", async () => {
  const storage = new MemoryStorage();
  const calls = [];
  let postCount = 0;
  let observed = null;
  const requestJson = async (url, options = {}) => {
    calls.push({ url, options });
    if (url.endsWith("/postpone")) {
      postCount += 1;
      if (postCount === 1) throw new TypeError("connection lost after send");
      return { message: "replayed original result" };
    }
    if (url.endsWith("/with-subtasks")) return { task: updatedTask, subtasks: [] };
    throw new Error(`unexpected request ${url}`);
  };
  const first = createAction({ storage, requestJson, onTaskObserved: async (task) => { observed = task; } });
  let firstError;
  try { await first.action(originalTask); } catch (error) { firstError = error; }
  assert.equal(firstError.postponeUncertain, true);
  assert.match(firstError.message, /不能证明本次请求或 updated event 已结算/);
  assert.equal(observed.due_at, updatedTask.due_at);
  assert.equal(postCount, 1);
  const intent = savedIntent(storage);
  assert.equal(intent.status, "uncertain");
  assert.equal(intent.idempotency_key, UUID_A);
  assert.equal(intent.body, '{"days":1}');

  const reloaded = createAction({ storage, requestJson, ids: [UUID_B] });
  assert.equal(reloaded.action.getPendingIntent(originalTask.id).status, "uncertain");
  const ordinaryClick = await reloaded.action(originalTask);
  assert.equal(ordinaryClick.postponePending, true);
  assert.equal(postCount, 1, "reload and ordinary click never automatically replay an uncertain POST");

  const success = await reloaded.action.retry(originalTask);
  assert.equal(success.due_at, updatedTask.due_at);
  assert.equal(savedIntent(storage), null);
  const posts = calls.filter(({ url }) => url.endsWith("/postpone"));
  assert.equal(posts.length, 2);
  assert.deepEqual(posts.map(({ options }) => [options.headers["Idempotency-Key"], options.body]), [
    [UUID_A, '{"days":1}'],
    [UUID_A, '{"days":1}'],
  ]);
});

test("concurrent entrypoint activation shares one in-flight intent and one POST", async () => {
  let releasePost;
  const gate = new Promise((resolve) => { releasePost = resolve; });
  const posts = [];
  const { action } = createAction({ requestJson: async (url, options = {}) => {
    if (url.endsWith("/postpone")) { posts.push(options); await gate; return { message: "ok" }; }
    if (url.endsWith("/with-subtasks")) return { task: updatedTask };
    throw new Error(`unexpected request ${url}`);
  } });
  const oldEntry = action(originalTask);
  const todayEntry = await action(originalTask);
  assert.equal(todayEntry.postponePending, true);
  assert.equal(todayEntry.intent.idempotency_key, UUID_A);
  assert.equal(posts.length, 1);
  releasePost();
  await oldEntry;
  assert.equal(posts.length, 1);
});

for (const [status, message] of [[404, "没有找到这个任务。"], [409, "该任务当前无法顺延截止日。"]]) {
  test(`${status} eligibility failure is surfaced and clears the completed intent without a follow-up GET`, async () => {
    let calls = 0;
    const { action, storage } = createAction({ requestJson: async () => {
      calls += 1;
      throw createError(status, message);
    } });
    await assert.rejects(action(originalTask), (error) => error.status === status && error.message === message);
    assert.equal(calls, 1);
    assert.equal(savedIntent(storage), null);
  });
}

test("409 idempotency_conflict is recognizable, retains pending UUID/body, and never rotates the key", async () => {
  const sent = [];
  const { action, storage } = createAction({ ids: [UUID_A, UUID_B], requestJson: async (url, options = {}) => {
    if (url.endsWith("/postpone")) {
      sent.push({ key: options.headers["Idempotency-Key"], body: options.body });
      throw createError(409, "idempotency_conflict", { error: "idempotency_conflict" });
    }
    throw new Error("conflict must not reconcile with GET");
  } });
  let failure;
  try { await action(originalTask); } catch (error) { failure = error; }
  assert.equal(failure.idempotencyConflict, true);
  assert.match(failure.message, /409 idempotency_conflict/);
  assert.equal(savedIntent(storage).status, "conflict");
  assert.equal(savedIntent(storage).idempotency_key, UUID_A);

  const pending = await action(originalTask);
  assert.equal(pending.postponePending, true);
  try { await action.retry(originalTask); } catch (error) { failure = error; }
  assert.equal(failure.idempotencyConflict, true);
  assert.equal(savedIntent(storage).idempotency_key, UUID_A);
  assert.deepEqual(sent, [
    { key: UUID_A, body: '{"days":1}' },
    { key: UUID_A, body: '{"days":1}' },
  ]);
});

test("HTTP 400 is explicit and retains the same key for an intentional retry", async () => {
  const sent = [];
  const { action, storage } = createAction({ ids: [UUID_A, UUID_B], requestJson: async (url, options = {}) => {
    if (url.endsWith("/postpone")) {
      sent.push([options.headers["Idempotency-Key"], options.body]);
      throw createError(400, "invalid_idempotency_key", { error: "invalid_idempotency_key" });
    }
    throw new Error("400 key errors do not trigger GET reconciliation");
  } });
  let failure;
  try { await action(originalTask); } catch (error) { failure = error; }
  assert.equal(failure.postponeKeyError, true);
  assert.match(failure.message, /HTTP 400/);
  assert.equal(savedIntent(storage).status, "key_error");
  try { await action.retry(originalTask); } catch (error) { failure = error; }
  assert.equal(failure.postponeKeyError, true);
  assert.equal(savedIntent(storage).idempotency_key, UUID_A);
  assert.deepEqual(sent, [[UUID_A, '{"days":1}'], [UUID_A, '{"days":1}']]);
});

for (const [label, makeError, expectedStatus] of [
  ["timeout", () => Object.assign(new Error("timeout"), { name: "AbortError" }), "uncertain"],
  ["5xx", () => createError(503, "temporary server failure"), "uncertain"],
  ["unparseable JSON", () => new SyntaxError("Unexpected token"), "uncertain"],
]) {
  test(`${label} preserves the original intent and offers no automatic replay`, async () => {
    let posts = 0;
    const { action, storage } = createAction({ requestJson: async (url) => {
      if (url.endsWith("/postpone")) { posts += 1; throw makeError(); }
      if (url.endsWith("/with-subtasks")) return { task: originalTask };
      throw new Error(`unexpected request ${url}`);
    } });
    let failure;
    try { await action(originalTask); } catch (error) { failure = error; }
    assert.equal(failure.postponeUncertain, true);
    const saved = savedIntent(storage);
    assert.equal(saved.status, expectedStatus);
    assert.equal(saved.idempotency_key, UUID_A);
    assert.equal(saved.body, '{"days":1}');
    const pending = await action(originalTask);
    assert.equal(pending.postponePending, true);
    assert.equal(posts, 1);
  });
}

test("per-user/per-task keys isolate state; switching users neither shows nor deletes another owner's intent", async () => {
  const storage = new MemoryStorage();
  const { action, setUser } = createAction({ storage, ids: [UUID_A, UUID_B], requestJson: async (url) => {
    if (url.endsWith("/postpone")) throw new TypeError("network lost");
    if (url.endsWith("/with-subtasks")) return { task: originalTask };
    throw new Error(`unexpected request ${url}`);
  } });
  try { await action(originalTask); } catch { /* Keep Alice's ambiguous intent. */ }
  const aliceKey = postponeIntentStorageKey("user-a", originalTask.id);
  const aliceRaw = storage.getItem(aliceKey);
  assert.ok(aliceRaw);

  setUser("user-b");
  assert.equal(action.getPendingIntent(originalTask.id), null);
  try { await action(originalTask); } catch { /* Bob's independent intent is also ambiguous. */ }
  const bobKey = postponeIntentStorageKey("user-b", originalTask.id);
  assert.notEqual(aliceKey, bobKey);
  assert.equal(JSON.parse(storage.getItem(bobKey)).idempotency_key, UUID_B);
  assert.equal(storage.getItem(aliceKey), aliceRaw, "another account's pending record is untouched");

  setUser("user-a");
  assert.equal(action.getPendingIntent(originalTask.id).idempotency_key, UUID_A);
  assert.equal(storage.getItem(aliceKey), aliceRaw);
});

test("missing authenticated user or failed persistence prevents any POST", async () => {
  let posts = 0;
  const storage = new MemoryStorage();
  const noUser = createAction({ user: null, storage, requestJson: async () => { posts += 1; } });
  await assert.rejects(noUser.action(originalTask), /无法确认当前认证用户/);

  const brokenStorage = {
    getItem() { return null; },
    setItem() { throw new Error("quota"); },
    removeItem() {},
  };
  const noPersistence = createAction({ storage: brokenStorage, requestJson: async () => { posts += 1; } });
  await assert.rejects(noPersistence.action(originalTask), /无法先持久化/);
  assert.equal(posts, 0);
});
