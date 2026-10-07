import assert from "node:assert/strict";
import test from "node:test";

import {
  ALLOWED_ATTACHMENT_TYPES,
  ATTACHMENT_NOTICE,
  MAX_ATTACHMENT_BYTES,
  addAttachmentFile,
  attachmentErrorMessage,
  attachmentUserKey,
  countTaskAttachments,
  deleteAttachmentRecord,
  deleteTaskAttachments,
  fileExtension,
  formatBytes,
  listTaskAttachments,
  makeAttachmentKey,
  summarizeAttachments,
  taskIdentity,
  validateAttachmentFile,
} from "../../src/momentum_agent/static/js/attachments.mjs";

// ---- 最小 IndexedDB 假实现（只覆盖 attachments.mjs 用到的 API 面）----
class FakeRequest {
  constructor() { this.result = undefined; this.error = null; this.onsuccess = null; this.onerror = null; }
}

class FakeStore {
  constructor(keyPath) { this.keyPath = keyPath; this.data = new Map(); this.indexes = new Map(); }
  createIndex(name, keyPath) { this.indexes.set(name, { keyPath }); }
  put(record) {
    const request = new FakeRequest();
    queueMicrotask(() => { this.data.set(record[this.keyPath], record); request.result = record[this.keyPath]; request.onsuccess?.(); });
    return request;
  }
  delete(key) {
    const request = new FakeRequest();
    queueMicrotask(() => { this.data.delete(key); request.onsuccess?.(); });
    return request;
  }
  index(name) {
    const store = this;
    const meta = this.indexes.get(name) || { keyPath: name };
    const fields = Array.isArray(meta.keyPath) ? meta.keyPath : [meta.keyPath];
    const keyOf = (record) => fields.map((field) => String(record[field])).join("\u0000");
    const target = (key) => (Array.isArray(key) ? key.map(String).join("\u0000") : String(key));
    return {
      getAll(key) {
        const request = new FakeRequest();
        const rows = [...store.data.values()].filter((row) => keyOf(row) === target(key));
        queueMicrotask(() => { request.result = rows; request.onsuccess?.(); });
        return request;
      },
      openKeyCursor(key) {
        const request = new FakeRequest();
        const keys = [...store.data.entries()].filter(([, row]) => keyOf(row) === target(key)).map(([k]) => k);
        let index = 0;
        const step = () => {
          if (index >= keys.length) { request.result = null; queueMicrotask(() => request.onsuccess?.()); return; }
          const current = keys[index];
          index += 1;
          request.result = { delete: () => store.data.delete(current), continue: () => step() };
          queueMicrotask(() => request.onsuccess?.());
        };
        queueMicrotask(step);
        return request;
      },
    };
  }
}

class FakeDB {
  constructor() { this.stores = new Map(); this.objectStoreNames = { contains: (name) => this.stores.has(name) }; }
  createObjectStore(name, options) { const store = new FakeStore(options.keyPath); this.stores.set(name, store); return store; }
  transaction(name) {
    const store = this.stores.get(name);
    const tx = { error: null, oncomplete: null, onerror: null, onabort: null, objectStore: () => store };
    setTimeout(() => tx.oncomplete?.(), 0);
    return tx;
  }
  close() {}
}

function fakeIndexedDB() {
  const db = new FakeDB();
  return { open: () => { const request = new FakeRequest(); queueMicrotask(() => { request.result = db; request.onupgradeneeded?.(); request.onsuccess?.(); }); return request; } };
}

function memoryStorage(user) {
  const values = new Map([["momentum_user", user]]);
  return { getItem: (key) => (values.has(key) ? values.get(key) : null), setItem: (key, value) => values.set(key, String(value)), removeItem: (key) => values.delete(key) };
}

function pngFile(name = "shot.png", size = 1024) { return { name, type: "image/png", size }; }

// ---- 校验规则 ----
test("accepts common images and PDF, rejects everything else", () => {
  assert.equal(validateAttachmentFile(pngFile()).ok, true);
  assert.equal(validateAttachmentFile({ name: "b.pdf", type: "application/pdf", size: 2048 }).ok, true);
  for (const bad of [
    { name: "x.svg", type: "image/svg+xml", size: 10 },
    { name: "x.html", type: "text/html", size: 10 },
    { name: "x.exe", type: "application/octet-stream", size: 10 },
    { name: "x.zip", type: "application/zip", size: 10 },
    { name: "x.js", type: "text/javascript", size: 10 },
    { name: "x.txt", type: "text/plain", size: 10 },
    { name: "x.png", type: "image/png", size: 0 },
    { name: "x.png", type: "image/png", size: MAX_ATTACHMENT_BYTES + 1 },
  ]) {
    assert.equal(validateAttachmentFile(bad).ok, false, JSON.stringify(bad));
  }
  assert.equal(validateAttachmentFile(null).ok, false);
  assert.equal(ALLOWED_ATTACHMENT_TYPES.has("application/pdf"), true);
});

test("rejects blocked extensions even when the MIME type claims to be an image", () => {
  assert.equal(validateAttachmentFile({ name: "payload.svg", type: "image/png", size: 100 }).ok, false);
  assert.equal(validateAttachmentFile({ name: "payload.HTML", type: "image/png", size: 100 }).ok, false);
  assert.equal(fileExtension("a.b.PDF"), "pdf");
  assert.equal(fileExtension("noext"), "");
});

// ---- 身份绑定 ----
test("task identity requires both the task id and its creation stamp", () => {
  assert.deepEqual(taskIdentity({ id: 7, created_at: "2026-10-07T00:00:00+00:00" }), { taskId: "7", taskCreatedAt: "2026-10-07T00:00:00+00:00" });
  assert.throws(() => taskIdentity({ created_at: "x" }), /缺少任务标识/);
  assert.throws(() => taskIdentity({ id: 7 }), /缺少任务创建标识/);
});

test("user key is sanitized and differs per account", () => {
  assert.equal(attachmentUserKey(memoryStorage("alice")), "alice");
  assert.equal(attachmentUserKey(memoryStorage("a/b c")), "a_b_c");
  assert.equal(attachmentUserKey(memoryStorage(null)), "anonymous");
  assert.notEqual(attachmentUserKey(memoryStorage("alice")), attachmentUserKey(memoryStorage("bob")));
  assert.match(makeAttachmentKey("u", 1, "t", "f"), /^u\|1\|t\|f$/);
});

// ---- 存储往返 + 隔离 ----
test("stores, lists and deletes a task attachment", async () => {
  const indexedDBRef = fakeIndexedDB();
  const storage = memoryStorage("alice");
  const identity = { taskId: "1", taskCreatedAt: "t1" };
  const added = await addAttachmentFile(pngFile(), identity, { indexedDBRef, storage, now: () => 1000 });
  assert.equal(added.ok, true);
  const rows = await listTaskAttachments({ user: "alice", ...identity }, { indexedDBRef });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].name, "shot.png");
  assert.equal(rows[0].taskId, "1");
  assert.equal(rows[0].user, "alice");
  await deleteAttachmentRecord(rows[0].key, { indexedDBRef });
  assert.equal((await listTaskAttachments({ user: "alice", ...identity }, { indexedDBRef })).length, 0);
});

test("attachments are isolated by account and by task creation identity", async () => {
  const indexedDBRef = fakeIndexedDB();
  const alice = memoryStorage("alice");
  const bob = memoryStorage("bob");
  await addAttachmentFile(pngFile("a.png"), { taskId: "1", taskCreatedAt: "t1" }, { indexedDBRef, storage: alice });
  await addAttachmentFile(pngFile("b.png"), { taskId: "1", taskCreatedAt: "t1" }, { indexedDBRef, storage: bob });
  const aliceRows = await listTaskAttachments({ user: "alice", taskId: "1", taskCreatedAt: "t1" }, { indexedDBRef });
  assert.equal(aliceRows.length, 1);
  assert.equal(aliceRows[0].name, "a.png");
  const reopened = await listTaskAttachments({ user: "alice", taskId: "1", taskCreatedAt: "t2" }, { indexedDBRef });
  assert.equal(reopened.length, 0, "a new task that reuses id 1 must not inherit the old attachments");
});

test("deleting a task removes exactly that task attachments", async () => {
  const indexedDBRef = fakeIndexedDB();
  const storage = memoryStorage("alice");
  await addAttachmentFile(pngFile("keep.png"), { taskId: "9", taskCreatedAt: "t9" }, { indexedDBRef, storage });
  await addAttachmentFile(pngFile("drop1.png"), { taskId: "5", taskCreatedAt: "t5" }, { indexedDBRef, storage });
  await addAttachmentFile(pngFile("drop2.png"), { taskId: "5", taskCreatedAt: "t5" }, { indexedDBRef, storage });
  const removed = await deleteTaskAttachments({ user: "alice", taskId: "5", taskCreatedAt: "t5" }, { indexedDBRef });
  assert.equal(removed, 2);
  assert.equal((await listTaskAttachments({ user: "alice", taskId: "5", taskCreatedAt: "t5" }, { indexedDBRef })).length, 0);
  assert.equal((await listTaskAttachments({ user: "alice", taskId: "9", taskCreatedAt: "t9" }, { indexedDBRef })).length, 1);
});

test("counts attachments per task and skips tasks without identity", async () => {
  const indexedDBRef = fakeIndexedDB();
  const storage = memoryStorage("alice");
  await addAttachmentFile(pngFile(), { taskId: "3", taskCreatedAt: "t3" }, { indexedDBRef, storage });
  const counts = await countTaskAttachments([{ id: 3, created_at: "t3" }, { id: 4 }], { indexedDBRef, storage });
  assert.equal(counts.get("3"), 1);
  assert.equal(counts.has("4"), false);
});

// ---- 失败语义 ----
test("quota failures are reported explicitly and never as silent loss", async () => {
  const quota = Object.assign(new Error("boom"), { name: "QuotaExceededError" });
  assert.match(attachmentErrorMessage(quota, "附件保存"), /存储空间不足/);
  assert.match(attachmentErrorMessage(quota, "附件保存"), /已有附件没有改动/);
  assert.match(attachmentErrorMessage(new Error("磁盘错误"), "附件保存"), /附件保存失败：磁盘错误/);
  const failing = { open: () => { const request = new FakeRequest(); queueMicrotask(() => { request.error = quota; request.onerror?.(); }); return request; } };
  const result = await addAttachmentFile(pngFile(), { taskId: "1", taskCreatedAt: "t" }, { indexedDBRef: failing, storage: memoryStorage("alice") });
  assert.equal(result.ok, false);
  assert.match(result.message, /存储空间不足/);
});

test("rejects files before touching storage", async () => {
  let opened = 0;
  const spy = { open: () => { opened += 1; return new FakeRequest(); } };
  const result = await addAttachmentFile({ name: "a.svg", type: "image/svg+xml", size: 10 }, { taskId: "1", taskCreatedAt: "t" }, { indexedDBRef: spy, storage: memoryStorage("alice") });
  assert.equal(result.ok, false);
  assert.equal(opened, 0);
});

test("summary and byte formatting", () => {
  assert.deepEqual(summarizeAttachments([{ size: 10 }, { size: 32 }]), { count: 2, totalBytes: 42 });
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(2048), "2.0 KiB");
  assert.equal(formatBytes(3 * 1024 * 1024), "3.0 MiB");
});

test("local-only notice states every required limitation", () => {
  assert.match(ATTACHMENT_NOTICE, /当前浏览器/);
  assert.match(ATTACHMENT_NOTICE, /清理网站数据/);
  assert.match(ATTACHMENT_NOTICE, /不共享/);
  assert.match(ATTACHMENT_NOTICE, /备份/);
  assert.match(ATTACHMENT_NOTICE, /未加密/);
});

test("the module never talks to the network", async () => {
  const source = await (await import("node:fs/promises")).readFile(new URL("../../src/momentum_agent/static/js/attachments.mjs", import.meta.url), "utf8");
  assert.equal(/fetch\(|XMLHttpRequest|navigator\.sendBeacon/.test(source), false, "attachments must stay local-only");
  assert.equal(/console\./.test(source), false, "file content must not be logged");
});
