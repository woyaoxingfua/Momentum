import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { createTaskPostponeAction } from "../../src/momentum_agent/static/js/postpone-action.mjs";
import { renderTodayReview } from "../../src/momentum_agent/static/js/advice-review.mjs";
import { initTasks, renderTasks, savePostpone } from "../../src/momentum_agent/static/js/tasks.js";
import { postponeTask } from "../../src/momentum_agent/static/js/advice.js";

const UUID = "44444444-4444-4444-8444-444444444444";
const task = { id: 41, status: "todo", title: "共享控制器 mock task", due_at: "2026-10-04T12:00:00Z" };
const updatedTask = { ...task, due_at: "2026-10-05T12:00:00Z" };

class MemoryStorage {
  values = new Map();
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
    this.classList = { add() {}, remove() {} };
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  setAttribute(name, value) { this.attributes[name] = value; }
  async click() { if (!this.disabled && !this.hidden) return this.listeners.get("click")?.(); }
}

async function withFakeDocument(run) {
  const original = globalThis.document;
  globalThis.document = { createElement: (tag) => new FakeElement(tag), querySelectorAll: () => [] };
  try { return await run(); }
  finally { globalThis.document = original; }
}

function findAll(element, className) {
  const found = [];
  if ((element.className || "").split(" ").includes(className)) found.push(element);
  for (const child of element.children || []) found.push(...findAll(child, className));
  return found;
}

async function withLegacyDialog(taskUnderTest, fetchMock, run) {
  const originalDocument = globalThis.document;
  const originalStorage = globalThis.localStorage;
  const originalFetch = globalThis.fetch;
  const storage = new MemoryStorage();
  storage.setItem("momentum_token", "node-mock-token");
  storage.setItem("momentum_user", "node-user-a");
  const postponeButton = new FakeElement("button");
  postponeButton.dataset = { postpone: String(taskUnderTest.id) };
  const retryButton = new FakeElement("button");
  const appElements = {
    postponeTaskId: { value: "" },
    postponeCurrentDue: { textContent: "" },
    postponeFeedback: { textContent: "" },
    postponeSubmitButton: { disabled: false },
    postponeRetryButton: retryButton,
    postponeDialog: { open: false, showModal() { this.open = true; }, close() { this.open = false; } },
  };
  globalThis.document = {
    createElement: (tag) => new FakeElement(tag),
    querySelectorAll: (selector) => selector === "[data-postpone]" || selector === `[data-postpone="${taskUnderTest.id}"]` ? [postponeButton] : [],
  };
  globalThis.localStorage = storage;
  globalThis.fetch = fetchMock;
  try {
    initTasks({ tasks: { innerHTML: "" }, taskCount: { textContent: "" } }, {}, appElements);
    renderTasks([taskUnderTest]);
    await postponeButton.click();
    return await run({ appElements, postponeButton, storage, retryButton });
  } finally {
    globalThis.document = originalDocument;
    globalThis.localStorage = originalStorage;
    globalThis.fetch = originalFetch;
  }
}

async function source(name) {
  return readFile(new URL(`../../src/momentum_agent/static/js/${name}`, import.meta.url), "utf8");
}

function response(payload, status = 200) {
  return { status, ok: status >= 200 && status < 300, json: async () => payload };
}

test("legacy task dialog and today-close are wired to the same exported persistent controller", async () => {
  const [advice, tasks] = await Promise.all([source("advice.js"), source("tasks.js")]);
  const html = await readFile(new URL("../../src/momentum_agent/static/index.html", import.meta.url), "utf8");
  assert.match(advice, /export\s*\{\s*postponeTask\s*\}/);
  assert.match(tasks, /import\s*\{\s*loadReview\s*,\s*postponeTask\s*\}\s*from\s*["']\.\/advice\.js["']/);
  assert.match(tasks, /await\s+postponeTask\(task\)/);
  assert.match(tasks, /postponeTask\.retry\(task\)/);
  assert.doesNotMatch(tasks, /requestJson\([`\"]\/api\/tasks\/\$\{taskId\}\/postpone/);
  assert.match(html, /id="postponeSubmitButton"[^>]*>顺延 1 天/);
  assert.match(html, /id="postponeRetryButton"[^>]*>使用原请求再试/);
  assert.doesNotMatch(html, /postponeDays|postpone-option|data-days=/);
});

test("legacy and today-close activations while one intent is in flight share its UUID and issue one POST", async () => {
  await withFakeDocument(async () => {
    const storage = new MemoryStorage();
    let releasePost;
    const postGate = new Promise((resolve) => { releasePost = resolve; });
    const posts = [];
    const action = createTaskPostponeAction({
      requestJson: async (url, options = {}) => {
        if (url.endsWith("/postpone")) {
          posts.push({ url, options });
          await postGate;
          return { message: "已顺延" };
        }
        if (url.endsWith("/with-subtasks")) return { task: updatedTask, subtasks: [] };
        throw new Error(`unexpected request ${url}`);
      },
      storage,
      getUserId: () => "node-user-a",
      createId: () => UUID,
      refreshTaskList: async () => {},
    });

    const legacyPromise = action(task);
    const mount = new FakeElement("section");
    renderTodayReview(mount, {
      localDate: "2026-10-04",
      completed_count: 2,
      today_focus_actual_seconds: 900,
      today_focus_session_count: 1,
      completed_events: [],
    }, { unfinishedTasks: [task], onPostpone: action });
    const todayButton = findAll(mount, "today-review-postpone")[0];
    await todayButton.click();

    assert.equal(posts.length, 1);
    assert.equal(posts[0].options.headers["Idempotency-Key"], UUID);
    assert.equal(posts[0].options.body, '{"days":1}');
    assert.equal(todayButton.disabled, true);
    assert.match(findAll(mount, "today-review-postpone-feedback")[0].textContent, /正在处理/);

    releasePost();
    const legacyResult = await legacyPromise;
    assert.equal(legacyResult.due_at, updatedTask.due_at);
    assert.equal(posts.length, 1);
    assert.equal(action.getPendingIntent(task.id), null);
  });
});

test("old task dialog surfaces a definite eligibility 409 and clears it without automatic repeat", async () => {
  const requests = [];
  await withLegacyDialog(task, async (url, options) => {
    requests.push({ url, options });
    return response({ error: "该任务当前无法顺延截止日。" }, 409);
  }, async ({ appElements }) => {
    await savePostpone({ preventDefault() {} });
    assert.equal(appElements.postponeFeedback.textContent, "该任务当前无法顺延截止日。");
    assert.equal(appElements.postponeSubmitButton.disabled, false);
    assert.equal(appElements.postponeRetryButton.hidden, true);
    assert.equal(appElements.postponeDialog.open, true);
    assert.equal(requests.length, 1);
    assert.equal(requests[0].options.method, "POST");
    assert.equal(requests[0].options.headers.Authorization, "Bearer node-mock-token");
    assert.match(requests[0].options.headers["Idempotency-Key"], /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
    assert.equal(requests[0].options.body, '{"days":1}');
  });
});

test("old-entry lost response survives reload and today-close explicitly retries the exact saved key and body", async () => {
  const requests = [];
  let posts = 0;
  await withLegacyDialog(task, async (url, options) => {
    requests.push({ url, options });
    if (url.endsWith("/postpone")) {
      posts += 1;
      if (posts === 1) throw new TypeError("response lost after POST");
      return response({ message: "original stored result replayed" });
    }
    if (url.endsWith("/with-subtasks")) return response({ task: updatedTask, subtasks: [] });
    throw new Error(`unexpected request ${url}`);
  }, async ({ appElements, storage }) => {
    await savePostpone({ preventDefault() {} });
    assert.match(appElements.postponeFeedback.textContent, /结果不确定/);
    assert.match(appElements.postponeFeedback.textContent, /不能证明本次请求或 updated event 已结算/);
    assert.match(appElements.postponeCurrentDue.textContent, /顺延结果仍不确定/);
    assert.equal(appElements.postponeSubmitButton.disabled, true);
    assert.equal(appElements.postponeRetryButton.hidden, false);
    assert.equal(posts, 1);

    const beforeReloadIntent = [...storage.values.entries()].find(([key]) => key.startsWith("momentum_task_postpone_v1:"));
    assert.ok(beforeReloadIntent);
    const saved = JSON.parse(beforeReloadIntent[1]);
    assert.equal(saved.status, "uncertain");
    assert.equal(saved.body, '{"days":1}');

    // A newly rendered today-close entry reads the persisted state; render itself sends no POST.
    const mount = new FakeElement("section");
    renderTodayReview(mount, {
      localDate: "2026-10-04",
      completed_count: 2,
      today_focus_actual_seconds: 900,
      today_focus_session_count: 1,
      completed_events: [],
    }, { unfinishedTasks: [task], onPostpone: postponeTask });
    const primary = findAll(mount, "today-review-postpone")[0];
    const retry = findAll(mount, "today-review-postpone-retry")[0];
    assert.equal(primary.disabled, true);
    assert.equal(retry.hidden, false);
    assert.match(retry.attributes["aria-label"], /使用原请求再试/);
    assert.equal(posts, 1, "reload/render must never automatically resend");

    await retry.click();
    assert.equal(posts, 2);
    assert.deepEqual(requests.filter(({ url }) => url.endsWith("/postpone")).map(({ options }) => [options.headers["Idempotency-Key"], options.body]), [
      [saved.idempotency_key, '{"days":1}'],
      [saved.idempotency_key, '{"days":1}'],
    ]);
    assert.equal(storage.getItem(beforeReloadIntent[0]), null);
    assert.equal(findAll(mount, "today-review-open-task-due")[0].textContent.includes("2026"), true);
  });
});

test("today-close eligibility errors remain visible and do not refresh or change review metrics", async () => {
  await withFakeDocument(async () => {
    const beforeMetrics = ["2 条", "15 分钟", "1 次"];
    const mount = new FakeElement("section");
    renderTodayReview(mount, {
      localDate: "2026-10-04",
      completed_count: 2,
      today_focus_actual_seconds: 900,
      today_focus_session_count: 1,
      completed_events: [],
    }, {
      unfinishedTasks: [{ ...task, id: 52 }],
      onPostpone: async () => { throw Object.assign(new Error("该任务当前无法顺延截止日。"), { status: 409 }); },
    });
    const metricsBefore = findAll(mount, "today-review-metric-value").map((node) => node.textContent);
    assert.deepEqual(metricsBefore, beforeMetrics);
    const button = findAll(mount, "today-review-postpone")[0];
    await button.click();
    const feedback = findAll(mount, "today-review-postpone-feedback")[0];
    assert.equal(feedback.hidden, false);
    assert.equal(feedback.textContent, "该任务当前无法顺延截止日。");
    assert.equal(feedback.attributes.role, "alert");
    assert.equal(button.disabled, false);
    assert.deepEqual(findAll(mount, "today-review-metric-value").map((node) => node.textContent), metricsBefore);
  });
});

test("HTML hidden semantics override site-wide button display styling", async () => {
  const css = await readFile(new URL("../../src/momentum_agent/static/app.css", import.meta.url), "utf8");
  assert.match(css, /\[hidden\]\s*\{\s*display:\s*none\s*!important\s*;/);
});
