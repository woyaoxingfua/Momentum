import assert from "node:assert/strict";
import test from "node:test";

import { bindAttachmentDialog, ATTACHMENT_NOTICE } from "../../src/momentum_agent/static/js/attachments.mjs";

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.listeners = new Map();
    this.textContent = "";
    this.className = "";
    this.dataset = {};
    this._value = "";
    this.files = null;
    this.open = false;
    this.alt = "";
    this.src = "";
    this.type = "";
    this.innerHTMLWrites = 0;
    this.classList = { toggle: () => {} };
  }
  get value() { return this._value; }
  set value(next) {
    this._value = next;
    // 真实浏览器里把 value 设为 "" 会清空 FileList —— 假实现必须一致，
    // 否则「先清空再读文件」这种顺序错误在单元测试里永远发现不了。
    if (next === "") this.files = null;
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(name, fn) { this.listeners.set(name, fn); }
  showModal() { this.open = true; }
  close() { this.open = false; this.listeners.get("close")?.(); }
  set innerHTML(value) { this.innerHTMLWrites += 1; this._html = value; }
  get innerHTML() { return this._html || ""; }
}

class FakeDocument {
  constructor(ids) { this.elements = new Map(ids.map((id) => [id, new FakeElement("div")])); }
  getElementById(id) { return this.elements.get(id) || null; }
  createElement(tag) { return new FakeElement(tag); }
  querySelectorAll() { return []; }
}

class FakeRequest {
  constructor() { this.result = undefined; this.error = null; this.onsuccess = null; this.onerror = null; }
}
class FakeStore {
  constructor(keyPath) { this.keyPath = keyPath; this.data = new Map(); this.indexes = new Map(); }
  createIndex(name, keyPath) { this.indexes.set(name, { keyPath }); }
  put(record) { const r = new FakeRequest(); queueMicrotask(() => { this.data.set(record[this.keyPath], record); r.onsuccess?.(); }); return r; }
  delete(key) { const r = new FakeRequest(); queueMicrotask(() => { this.data.delete(key); r.onsuccess?.(); }); return r; }
  index(name) {
    const store = this;
    const fields = this.indexes.get(name).keyPath;
    const keyOf = (row) => fields.map((f) => String(row[f])).join("\u0000");
    const target = (key) => (Array.isArray(key) ? key.map(String).join("\u0000") : String(key));
    return {
      getAll(key) { const r = new FakeRequest(); const rows = [...store.data.values()].filter((row) => keyOf(row) === target(key)); queueMicrotask(() => { r.result = rows; r.onsuccess?.(); }); return r; },
      openKeyCursor(key) {
        const r = new FakeRequest();
        const keys = [...store.data.entries()].filter(([, row]) => keyOf(row) === target(key)).map(([k]) => k);
        let i = 0;
        const step = () => {
          if (i >= keys.length) { r.result = null; queueMicrotask(() => r.onsuccess?.()); return; }
          const current = keys[i]; i += 1;
          r.result = { delete: () => store.data.delete(current), continue: () => step() };
          queueMicrotask(() => r.onsuccess?.());
        };
        queueMicrotask(step);
        return r;
      },
    };
  }
}
class FakeDB {
  constructor() { this.stores = new Map(); this.objectStoreNames = { contains: (n) => this.stores.has(n) }; }
  createObjectStore(name, options) { const s = new FakeStore(options.keyPath); this.stores.set(name, s); return s; }
  transaction(name) { const s = this.stores.get(name); const tx = { error: null, oncomplete: null, objectStore: () => s }; setTimeout(() => tx.oncomplete?.(), 0); return tx; }
  close() {}
}
function fakeIndexedDB() { const db = new FakeDB(); return { open: () => { const r = new FakeRequest(); queueMicrotask(() => { r.result = db; r.onupgradeneeded?.(); r.onsuccess?.(); }); return r; } }; }
function memoryStorage(user) { const v = new Map([["momentum_user", user]]); return { getItem: (k) => (v.has(k) ? v.get(k) : null), setItem: (k, x) => v.set(k, String(x)), removeItem: (k) => v.delete(k) }; }
function pngFile(name = "shot.png", size = 1024) { return { name, type: "image/png", size }; }

const IDS = ["attachmentDialog", "attachmentTitle", "attachmentList", "attachmentFile", "attachmentStatus", "attachmentNotice", "attachmentCloseButton"];

async function settle(times = 8) {
  for (let i = 0; i < times; i += 1) await new Promise((resolve) => setTimeout(resolve, 1));
}

function setup(user = "alice") {
  const documentRef = new FakeDocument(IDS);
  const indexedDBRef = fakeIndexedDB();
  const storage = memoryStorage(user);
  const revoked = [];
  let objectUrlSeq = 0;
  const urlApi = { createObjectURL: () => "blob:local-" + (objectUrlSeq += 1), revokeObjectURL: (url) => revoked.push(url) };
  const opened = [];
  const handle = bindAttachmentDialog({ documentRef, storage, indexedDBRef, urlApi, openWindow: (url) => opened.push(url) });
  return { documentRef, indexedDBRef, storage, urlApi, revoked, opened, handle, els: documentRef.elements };
}

test("opening the dialog shows the local-only notice and an empty list", async () => {
  const { els, handle } = setup();
  await handle.open({ id: 1, created_at: "t1", title: "写周报" });
  assert.equal(els.get("attachmentNotice").textContent, ATTACHMENT_NOTICE);
  assert.match(els.get("attachmentTitle").textContent, /写周报/);
  assert.equal(els.get("attachmentList").children.length, 1);
  assert.match(els.get("attachmentList").children[0].textContent, /还没有本地附件/);
  assert.equal(els.get("attachmentDialog").open, true);
});

test("adding a file stores it locally, renders the name as text and never as HTML", async () => {
  const { els, handle } = setup();
  await handle.open({ id: 1, created_at: "t1", title: "T" });
  const input = els.get("attachmentFile");
  input.files = [pngFile("<img src=x onerror=alert(1)>.png")];
  await input.listeners.get("change")();
  await settle();
  const rows = els.get("attachmentList").children;
  assert.equal(rows.length, 1);
  const meta = rows[0].children.find((child) => child.className === "attachment-meta");
  const name = meta.children.find((child) => child.className === "attachment-name");
  assert.equal(name.textContent, "<img src=x onerror=alert(1)>.png");
  assert.equal(name.innerHTMLWrites, 0, "the file name must be assigned as text, never as HTML");
  assert.equal(els.get("attachmentList").innerHTMLWrites, 0, "the list must be built with DOM APIs");
  assert.match(els.get("attachmentStatus").textContent, /未上传/);
  assert.equal(input.value, "", "the file input must be cleared so the same file can be re-added");
});

test("an oversized or unsupported file is rejected with an explicit message", async () => {
  const { els, handle } = setup();
  await handle.open({ id: 2, created_at: "t2" });
  const input = els.get("attachmentFile");
  input.files = [{ name: "big.png", type: "image/png", size: 9 * 1024 * 1024 }];
  await input.listeners.get("change")();
  await settle();
  assert.match(els.get("attachmentStatus").textContent, /8 MiB/);
  assert.equal(els.get("attachmentList").children[0].textContent.includes("还没有本地附件"), true);
});

test("deleting removes the record and refreshes the list", async () => {
  const { els, handle } = setup();
  await handle.open({ id: 3, created_at: "t3" });
  const input = els.get("attachmentFile");
  input.files = [pngFile("a.png")];
  await input.listeners.get("change")();
  await settle();
  const row = els.get("attachmentList").children[0];
  const actions = row.children.find((child) => child.className === "attachment-actions");
  const removeButton = actions.children.find((child) => child.textContent === "删除");
  await removeButton.listeners.get("click")();
  await settle();
  assert.equal(els.get("attachmentList").children[0].textContent.includes("还没有本地附件"), true);
});

test("another account never sees the first account attachments", async () => {
  const alice = setup("alice");
  await alice.handle.open({ id: 4, created_at: "t4" });
  const input = alice.els.get("attachmentFile");
  input.files = [pngFile("secret.png")];
  await input.listeners.get("change")();
  await settle();
  assert.equal(alice.els.get("attachmentList").children.length, 1);

  const bob = setup("bob");
  await bob.handle.open({ id: 4, created_at: "t4" });
  assert.equal(bob.els.get("attachmentList").children.length, 1);
  assert.match(bob.els.get("attachmentList").children[0].textContent, /还没有本地附件/);
});

test("closing releases every object URL it created", async () => {
  const { els, handle, revoked, urlApi } = setup();
  await handle.open({ id: 5, created_at: "t5" });
  const input = els.get("attachmentFile");
  input.files = [pngFile("a.png")];
  await input.listeners.get("change")();
  await settle();
  assert.equal(revoked.length, 0, "URLs stay alive while the dialog is open");
  handle.close();
  await settle(2);
  assert.equal(revoked.length >= 1, true);
});

test("a task without a creation stamp cannot bind attachments", async () => {
  const { els, handle } = setup();
  await handle.open({ id: 6 });
  assert.match(els.get("attachmentStatus").textContent, /缺少任务创建标识/);
});
