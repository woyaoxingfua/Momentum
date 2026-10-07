import assert from "node:assert/strict";
import test from "node:test";

import {
  FREQUENT_COMPLETED_WINDOW_DAYS,
  createFrequentCompletedEntry,
  formatFrequentSummary,
  formatLastCompletedAt,
  loadFrequentCompleted,
  normalizeFrequentItems,
} from "../../src/momentum_agent/static/js/frequent.mjs";

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.listeners = new Map();
    this.textContent = "";
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this.attributes = {};
    this.innerHTMLWrites = 0;
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(name, fn) { this.listeners.set(name, fn); }
  setAttribute(name, value) { this.attributes[name] = value; }
  async click() { return this.listeners.get("click")?.(); }
  set innerHTML(value) { this.innerHTMLWrites += 1; }
  get innerHTML() { return ""; }
}

class FakeDocument { createElement(tagName) { return new FakeElement(tagName); } }

function setup({ payload, failCreate = false, failLoad = false } = {}) {
  const calls = [];
  const requestJson = async (url, options) => {
    calls.push({ url, options });
    if (url.startsWith("/api/tasks/frequent")) {
      if (failLoad) throw new Error("load boom");
      return payload === undefined ? { tasks: [] } : payload;
    }
    if (failCreate) throw new Error("create boom");
    return { message: "已创建任务 #9：写周报", task: { id: 9 } };
  };
  const created = [];
  const entry = createFrequentCompletedEntry({
    requestJson,
    onCreated: async (result) => { created.push(result); },
    documentRef: new FakeDocument(),
  });
  return { entry, calls, created };
}

async function settle(times = 6) {
  for (let i = 0; i < times; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

const ITEM = { key: "写周报", title: "写周报", completion_count: 3, last_completed_at: "2026-10-01T09:12:33+00:00" };

test("the entry performs no network request and stays collapsed before the user acts", () => {
  const { entry, calls } = setup();
  assert.equal(calls.length, 0);
  assert.equal(entry.list.hidden, true);
  assert.equal(entry.status.hidden, true);
});

test("expanding loads the 180-day window and renders titles as text only", async () => {
  const hostile = { key: "x", title: "<img src=x onerror=alert(1)>", completion_count: "bad", last_completed_at: "2026-10-01T09:12:33+00:00" };
  const { entry, calls } = setup({ payload: { tasks: [hostile] } });
  await entry.expand();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/tasks/frequent?days=" + FREQUENT_COMPLETED_WINDOW_DAYS);
  assert.equal(entry.list.hidden, false);
  const row = entry.list.children[0];
  assert.equal(row.children[0].textContent, "<img src=x onerror=alert(1)>");
  assert.equal(entry.list.innerHTMLWrites, 0);
  assert.equal(row.children[1].textContent, "完成 0 次 · 最近 10-01");
});

test("an empty history explains itself and shows no rows", async () => {
  const { entry } = setup({ payload: { tasks: [] } });
  await entry.expand();
  assert.equal(entry.list.hidden, true);
  assert.match(entry.status.textContent, /还没有/);
});

test("a load failure is surfaced instead of rendering an empty success", async () => {
  const { entry } = setup({ failLoad: true });
  await entry.expand();
  assert.match(entry.status.textContent, /读取失败/);
  assert.equal(entry.list.children.length, 0);
});

test("double clicking 再来一个 posts exactly once", async () => {
  const { entry, calls, created } = setup({ payload: { tasks: [ITEM] } });
  await entry.expand();
  const again = entry.list.children[0].children[2];
  assert.equal(again.textContent, "再来一个");
  await Promise.all([again.click(), again.click()]);
  await settle();
  const posts = calls.filter((call) => call.url === "/api/frequent/recreate");
  assert.equal(posts.length, 1);
  assert.equal(posts[0].options.method, "POST");
  assert.deepEqual(JSON.parse(posts[0].options.body), { key: "写周报" });
  assert.equal(created.length, 1);
  assert.match(entry.status.textContent, /已创建任务/);
  assert.equal(again.disabled, false);
});

test("a failed recreate reports the error and creates nothing", async () => {
  const { entry, calls, created } = setup({ payload: { tasks: [ITEM] }, failCreate: true });
  await entry.expand();
  const again = entry.list.children[0].children[2];
  await again.click();
  await settle();
  assert.equal(calls.filter((call) => call.url === "/api/frequent/recreate").length, 1);
  assert.equal(created.length, 0);
  assert.match(entry.status.textContent, /创建失败/);
});

test("collapsing hides the rows without another request", async () => {
  const { entry, calls } = setup({ payload: { tasks: [ITEM] } });
  await entry.expand();
  await entry.expand();
  assert.equal(entry.list.hidden, true);
  assert.equal(calls.length, 1, "collapsing must not refetch");
});

test("custom window days are encoded on the request", async () => {
  const calls = [];
  const requestJson = async (url) => { calls.push(url); return { tasks: [] }; };
  await loadFrequentCompleted(requestJson, { days: 30 });
  assert.equal(calls[0], "/api/tasks/frequent?days=30");
});

test("normalizeFrequentItems drops rows without a key or title", () => {
  const items = normalizeFrequentItems({ tasks: [
    { key: "a", title: "A", completion_count: 2, last_completed_at: "2026-10-01T00:00:00+00:00" },
    { key: "", title: "B" },
    { key: "c", title: "" },
    null,
    { key: "d", title: "D", completion_count: -5 },
  ] });
  assert.deepEqual(items.map((item) => item.key), ["a", "d"]);
  assert.equal(items[1].completion_count, 0);
  assert.deepEqual(normalizeFrequentItems(null), []);
});

test("format helpers render stable, label-carrying strings", () => {
  const now = new Date("2026-10-07T12:00:00+08:00");
  assert.equal(formatLastCompletedAt("2026-10-07T09:00:00+08:00", now), "今天");
  assert.equal(formatLastCompletedAt("2026-10-01T09:00:00+08:00", now), "10-01");
  assert.equal(formatLastCompletedAt("nonsense", now), "");
  assert.equal(formatLastCompletedAt("", now), "");
  assert.equal(formatFrequentSummary({ completion_count: 3, last_completed_at: "" }), "完成 3 次");
  assert.equal(formatFrequentSummary(null), "完成 0 次");
});

test("the module never builds HTML from server data", async () => {
  const source = await (await import("node:fs/promises")).readFile(new URL("../../src/momentum_agent/static/js/frequent.mjs", import.meta.url), "utf8");
  assert.equal(/innerHTML/.test(source), false, "server-provided titles must never reach innerHTML");
  assert.equal(/toLowerCase\(|normalize\(/.test(source), false, "grouping stays server-authoritative; no client-side title matching");
});
