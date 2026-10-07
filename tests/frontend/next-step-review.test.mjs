import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

import {
  findBoundSuggestionTask,
  formatActualFocusDuration,
  getTodayReviewParams,
  getTodayReviewUrl,
  loadAdvicePayload,
  loadTodayReviewPayload,
  renderAdvicePayload,
  renderSuggestion,
  renderTodayReview,
  saveSuggestionEstimate,
} from "../../src/momentum_agent/static/js/advice-review.mjs";
import { formatFocusStartError, startFocusSession } from "../../src/momentum_agent/static/js/focus-api.mjs";
import { createTaskCompletionAction } from "../../src/momentum_agent/static/js/task-completion-action.mjs";
import { createTaskPostponeAction } from "../../src/momentum_agent/static/js/postpone-action.mjs";

const fixtures = JSON.parse(await readFile(new URL("./fixtures/next-step-review.json", import.meta.url), "utf8"));

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
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  setAttribute(name, value) { this.attributes[name] = value; }
  async click() { return this.listeners.get("click")?.(); }
}

class MemoryStorage {
  values = new Map();
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

async function withFakeDocument(run) {
  const original = globalThis.document;
  globalThis.document = { createElement: (tag) => new FakeElement(tag) };
  try { return await run(); }
  finally { globalThis.document = original; }
}

function textContent(element) {
  return [element.textContent, ...element.children.map(textContent)].filter(Boolean).join(" ");
}

function findByClass(element, className) {
  if (element.className.split(" ").includes(className)) return element;
  for (const child of element.children) {
    const found = findByClass(child, className);
    if (found) return found;
  }
  return null;
}

test("advice API uses the existing GET endpoint and keeps advice plus one suggestion", async () => {
  const calls = [];
  const payload = await loadAdvicePayload(async (...args) => {
    calls.push(args);
    return fixtures.advice;
  });
  assert.deepEqual(calls, [["/api/advice"]]);
  assert.equal(payload.advice, fixtures.advice.advice);
  assert.deepEqual(payload.suggestion, fixtures.advice.suggestion);
});

test("doing-vs-todo advice remains bound to the selected suggestion and preserves its start action", async () => {
  const payload = fixtures.doingVsTodo;

  await withFakeDocument(async () => {
    const explanation = new FakeElement("p");
    const mount = new FakeElement("div");
    let startedSuggestion;
    const binding = renderAdvicePayload(payload, explanation, mount, async (value) => { startedSuggestion = value; });
    assert.strictEqual(binding.suggestion, payload.suggestion);
    assert.equal(binding.task_id, payload.suggestion.task_id);
    assert.equal(binding.title, payload.suggestion.title);
    assert.ok(binding.text.includes(binding.title));
    assert.equal(explanation.textContent, binding.text);
    assert.ok(!explanation.textContent.includes(payload.todo.title), "the separately named todo must not leak into the doing explanation");
    assert.match(textContent(mount), new RegExp(`任务 #${binding.task_id}`));
    assert.ok(textContent(mount).includes(binding.title));
    await findByClass(mount, "suggestion-start").click();
    assert.strictEqual(startedSuggestion, payload.suggestion, "the original selected task still reaches the existing start action");

    const calls = [];
    await startFocusSession(async (...args) => {
      calls.push(args);
      return { session_id: "doing-vs-todo-session", started_at: "2026-10-03T10:00:00Z" };
    }, payload.suggestion.task_id, 25);
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], "/api/focus/start");
    assert.equal(calls[0][1].method, "POST");
    assert.equal(JSON.parse(calls[0][1].body).task_id, payload.suggestion.task_id);
  });
});

test("today review query always sends browser IANA zone and the browser-local calendar date", async () => {
  const now = new Date(2026, 9, 3, 23, 58, 0);
  assert.deepEqual(getTodayReviewParams({ now, timeZone: "Asia/Tokyo" }), {
    timeZone: "Asia/Tokyo",
    localDate: "2026-10-03",
  });
  assert.equal(
    getTodayReviewUrl({ now, timeZone: "Asia/Tokyo" }),
    "/api/review?timeZone=Asia%2FTokyo&localDate=2026-10-03",
  );

  const calls = [];
  const payload = await loadTodayReviewPayload(async (...args) => {
    calls.push(args);
    return fixtures.review;
  }, { now, timeZone: "America/Los_Angeles" });
  assert.deepEqual(calls, [["/api/review?timeZone=America%2FLos_Angeles&localDate=2026-10-03"]]);
  assert.equal(payload.localDate, "2026-10-03");
});

test("todo and doing suggestions show the right status and retain the original task ID on direct start", async () => {
  await withFakeDocument(async () => {
    for (const [suggestion, expectedStatus] of [
      [fixtures.advice.suggestion, "待办"],
      [fixtures.suggestionDoing, "进行中"],
    ]) {
      const mount = new FakeElement("div");
      let startedSuggestion;
      renderSuggestion(suggestion, mount, async (value) => { startedSuggestion = value; });
      assert.equal(mount.hidden, false);
      assert.match(textContent(mount), new RegExp(expectedStatus));
      assert.match(textContent(mount), new RegExp(`任务 #${suggestion.task_id}`));

      const button = findByClass(mount, "suggestion-start");
      assert.ok(button);
      assert.equal(button.textContent, "开始专注");
      await button.click();
      assert.strictEqual(startedSuggestion, suggestion, "the exact suggestion object is passed through without selecting or cloning a task");

      let request;
      await startFocusSession(async (...args) => {
        request = args;
        return { session_id: "session-start", started_at: "2026-10-03T10:00:00Z" };
      }, suggestion.task_id, 25);
      assert.equal(request[0], "/api/focus/start");
      assert.equal(request[1].method, "POST");
      const body = JSON.parse(request[1].body);
      assert.equal(body.task_id, suggestion.task_id);
      assert.equal(typeof body.task_id, typeof suggestion.task_id);
      assert.equal(body.duration_minutes, 25);
    }
  });
});

test("todo and doing advice cards save only their bound task estimate and start with the refreshed suggestion", async () => {
  await withFakeDocument(async () => {
    for (const [source, expectedOtherTaskId, updatedMinutes] of [
      [fixtures.advice.suggestion, 527, 47],
      [fixtures.doingVsTodo.suggestion, 419, 38],
    ]) {
      const suggestion = { ...source };
      const mount = new FakeElement("div");
      const calls = [];
      let startedSuggestion;
      renderSuggestion(
        suggestion,
        mount,
        async (value) => { startedSuggestion = value; },
        (boundSuggestion, minutes) => saveSuggestionEstimate(async (...args) => {
          calls.push(args);
          return { message: "任务已更新" };
        }, boundSuggestion, minutes),
      );

      const estimateInput = findByClass(mount, "suggestion-estimate-input");
      const saveButton = findByClass(mount, "suggestion-estimate-save");
      assert.ok(estimateInput && saveButton, `${suggestion.status} suggestion supports estimate editing`);
      estimateInput.value = String(updatedMinutes);
      await saveButton.click();

      assert.equal(calls.length, 1);
      assert.equal(calls[0][0], `/api/tasks/${suggestion.task_id}`);
      assert.equal(calls[0][0].includes(String(expectedOtherTaskId)), false, "a separate todo/doing task is never targeted");
      assert.equal(calls[0][1].method, "PUT");
      assert.deepEqual(JSON.parse(calls[0][1].body), { estimated_minutes: updatedMinutes });
      assert.equal(suggestion.estimated_minutes, updatedMinutes);
      assert.equal(findByClass(mount, "suggestion-estimate").textContent, `预估 ${updatedMinutes} 分钟`);
      assert.match(findByClass(mount, "suggestion-estimate-feedback").textContent, /已保存/);

      await findByClass(mount, "suggestion-start").click();
      assert.strictEqual(startedSuggestion, suggestion);
      assert.equal(startedSuggestion.task_id, source.task_id);
      assert.equal(startedSuggestion.estimated_minutes, updatedMinutes, "direct start receives the newly saved estimate");
    }

    const completedSuggestion = { ...fixtures.advice.suggestion, status: "done" };
    const completedMount = new FakeElement("div");
    renderSuggestion(completedSuggestion, completedMount, async () => {}, async () => {});
    assert.equal(findByClass(completedMount, "suggestion-estimate-input"), null, "closed suggestions cannot edit the estimate");
  });
});

test("failed estimate save reports failure and leaves the suggestion estimate unchanged", async () => {
  await withFakeDocument(async () => {
    const suggestion = { ...fixtures.advice.suggestion };
    const previousMinutes = suggestion.estimated_minutes;
    const mount = new FakeElement("div");
    const calls = [];
    renderSuggestion(suggestion, mount, async () => {}, (boundSuggestion, minutes) => saveSuggestionEstimate(async (...args) => {
      calls.push(args);
      throw new Error("网络断开");
    }, boundSuggestion, minutes));

    findByClass(mount, "suggestion-estimate-input").value = "62";
    await findByClass(mount, "suggestion-estimate-save").click();

    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], `/api/tasks/${suggestion.task_id}`);
    assert.deepEqual(JSON.parse(calls[0][1].body), { estimated_minutes: 62 });
    assert.equal(suggestion.estimated_minutes, previousMinutes);
    assert.equal(findByClass(mount, "suggestion-estimate").textContent, `预估 ${previousMinutes} 分钟`);
    const feedback = findByClass(mount, "suggestion-estimate-feedback");
    assert.equal(feedback.attributes.role, "alert");
    assert.match(feedback.textContent, /保存失败，未保存：网络断开/);
  });
});

test("review displays event-derived daily count, actual focus seconds, estimates only as references, and midnight note", async () => {
  await withFakeDocument(async () => {
    const mount = new FakeElement("section");
    const review = { ...fixtures.review, completed_count: 7 };
    renderTodayReview(mount, review);
    const text = textContent(mount);
    assert.match(text, /完成事件 7 条/);
    assert.match(text, /实际专注 1 小时 2 分钟/);
    assert.doesNotMatch(text, /实际专注 180 分钟/);
    assert.match(text, /预估参考 180 分钟/);
    assert.match(text, /跨午夜/);
    assert.equal(formatActualFocusDuration(review.today_focus_actual_seconds), "1 小时 2 分钟");
    assert.equal(formatActualFocusDuration(0), "0 秒");
  });
});

test("advice and review loading are read-only and never create, reschedule, or update tasks", async () => {
  const calls = [];
  await loadAdvicePayload(async (...args) => { calls.push(args); return fixtures.advice; });
  await loadTodayReviewPayload(async (...args) => { calls.push(args); return fixtures.review; }, {
    now: new Date(2026, 9, 3),
    timeZone: "Asia/Tokyo",
  });
  assert.equal(calls.length, 2);
  assert.ok(calls.every(([url, options]) => options === undefined && !url.startsWith("/api/tasks") && !url.startsWith("/api/plan")));
  assert.equal(calls[0][0], "/api/advice");
  assert.match(calls[1][0], /^\/api\/review\?timeZone=/);
});

test("closed-task start 409 is surfaced unchanged; no status mutation request is attempted", async () => {
  const suggestion = fixtures.advice.suggestion;
  const error = Object.assign(new Error(fixtures.startConflict.error), { status: 409, payload: fixtures.startConflict });
  const calls = [];
  await assert.rejects(
    startFocusSession(async (...args) => { calls.push(args); throw error; }, suggestion.task_id, 25),
    (received) => received === error && received.status === 409,
  );
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/api/focus/start");
  assert.equal(JSON.parse(calls[0][1].body).task_id, suggestion.task_id);
  assert.match(formatFocusStartError(error), /409.*该任务已关闭.*未开始专注/);
});

test("suggestion completion targets its bound task, preserves the UUID through explicit retry, then refreshes to the new suggestion", async () => {
  const UUID = "a4bc82b6-ae31-4e91-ae83-4dcc83d67c4d";
  const storage = new MemoryStorage();
  const calls = [];
  const action = createTaskCompletionAction({
    storage,
    getUserId: () => "user-a",
    createId: () => UUID,
    requestJson: async (url, options) => {
      calls.push({ url, options });
      if (calls.length === 1) throw new TypeError("response lost");
      return { message: "原请求完成成功" };
    },
  });

  await withFakeDocument(async () => {
    const explanation = new FakeElement("p");
    const mount = new FakeElement("div");
    let refreshCount = 0;
    const adviceRefreshCalls = [];
    const refreshSuggestion = async () => {
      refreshCount += 1;
      const payload = await loadAdvicePayload(async (...args) => {
        adviceRefreshCalls.push(args);
        return fixtures.doingVsTodo;
      });
      renderAdvicePayload(payload, explanation, mount, async () => {}, undefined, action, refreshSuggestion);
      return payload;
    };
    renderAdvicePayload(fixtures.advice, explanation, mount, async () => {}, undefined, action, refreshSuggestion);

    await findByClass(mount, "suggestion-complete").click();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, `/api/tasks/${fixtures.advice.suggestion.task_id}/done`);
    assert.equal(calls[0].options.method, "POST");
    assert.equal(calls[0].options.headers["Idempotency-Key"], UUID);
    assert.match(textContent(mount), /任务 #419/);
    assert.doesNotMatch(textContent(mount), /任务已完成/);
    assert.equal(refreshCount, 0, "an uncertain response does not refresh away the bound card");
    assert.equal(findByClass(mount, "suggestion-completion-retry").hidden, false);

    renderAdvicePayload(fixtures.advice, explanation, mount, async () => {}, undefined, action, refreshSuggestion);
    assert.equal(calls.length, 1, "rendering a pending intent never sends or retries automatically");
    assert.equal(findByClass(mount, "suggestion-complete").disabled, true);
    assert.equal(findByClass(mount, "suggestion-completion-retry").hidden, false);

    await findByClass(mount, "suggestion-completion-retry").click();
    assert.equal(calls.length, 2);
    assert.equal(calls[1].url, calls[0].url);
    assert.equal(calls[1].options.method, "POST");
    assert.equal(calls[1].options.headers["Idempotency-Key"], UUID, "retry reuses the exact UUIDv4 rather than generating a new key");
    assert.equal(refreshCount, 1);
    assert.deepEqual(adviceRefreshCalls, [["/api/advice"]]);
    assert.match(textContent(mount), /任务 #527/);
    assert.match(textContent(mount), /核对实验结果/);
    assert.doesNotMatch(textContent(mount), /任务 #419/);
  });
});

test("definite completion failure leaves the suggestion visible, shows the original error, and never refreshes as success", async () => {
  const storage = new MemoryStorage();
  let refreshCount = 0;
  const action = createTaskCompletionAction({
    storage,
    getUserId: () => "user-a",
    createId: () => "b7ae8189-6116-49db-9aa2-c56e6465cb09",
    requestJson: async () => {
      throw Object.assign(new Error("任务不存在或当前不可用"), { status: 404, payload: { error: "task_not_found" } });
    },
  });

  await withFakeDocument(async () => {
    const explanation = new FakeElement("p");
    const mount = new FakeElement("div");
    renderAdvicePayload(
      fixtures.advice,
      explanation,
      mount,
      async () => {},
      undefined,
      action,
      async () => { refreshCount += 1; return fixtures.doingVsTodo; },
    );

    await findByClass(mount, "suggestion-complete").click();
    const feedback = findByClass(mount, "suggestion-completion-feedback");
    assert.equal(feedback.attributes.role, "alert");
    assert.match(feedback.textContent, /HTTP 404.*任务不存在或当前不可用/);
    assert.match(textContent(mount), /任务 #419/);
    assert.doesNotMatch(textContent(mount), /任务已完成|已完成，正在刷新/);
    assert.equal(findByClass(mount, "suggestion-complete").hidden, false);
    assert.equal(findByClass(mount, "suggestion-completion-retry").hidden, true);
    assert.equal(refreshCount, 0);
  });
});

test("suggestion postpone is available only for its matching open task with a due date", async () => {
  const suggestion = { ...fixtures.advice.suggestion, status: "todo" };
  const task = { id: suggestion.task_id, status: "todo", due_at: "2026-10-09T16:00:00Z" };
  assert.strictEqual(findBoundSuggestionTask(suggestion, [
    { id: 527, status: "todo", due_at: "2026-10-08T16:00:00Z" },
    task,
  ]), task);
  assert.equal(findBoundSuggestionTask(suggestion, [{ ...task, id: 527 }]), null, "another task's due date cannot be borrowed");
  assert.equal(findBoundSuggestionTask(suggestion, null), null, "a failed open-task lookup cannot establish a due date");
  assert.equal(findBoundSuggestionTask({ ...suggestion, status: "done" }, [task]), null, "a closed suggestion is not postponable");

  await withFakeDocument(async () => {
    const mount = new FakeElement("div");
    const action = async () => {};
    renderAdvicePayload(fixtures.advice, new FakeElement("p"), mount, async () => {}, undefined, undefined, undefined, task, action);
    assert.ok(findByClass(mount, "suggestion-postpone"));
    assert.equal(findByClass(mount, "suggestion-postpone").textContent, "一天后再处理");

    const noDueMount = new FakeElement("div");
    renderAdvicePayload(fixtures.advice, new FakeElement("p"), noDueMount, async () => {}, undefined, undefined, undefined, { ...task, due_at: null }, action);
    assert.equal(findByClass(noDueMount, "suggestion-postpone"), null, "tasks without a due date hide postpone controls");
    assert.equal(findByClass(noDueMount, "suggestion-postpone-retry"), null);

    const unmatchedMount = new FakeElement("div");
    renderAdvicePayload(fixtures.advice, new FakeElement("p"), unmatchedMount, async () => {}, undefined, undefined, undefined, { ...task, id: 527 }, action);
    assert.equal(findByClass(unmatchedMount, "suggestion-postpone"), null, "a task with a different ID cannot enable the action");
  });
});

test("suggestion postpone uses its bound task, UUIDv4 key and days=1, then refreshes task, today state and advice", async () => {
  const UUID = "69b641f0-e915-4df0-8c54-973228073cb1";
  const suggestion = { ...fixtures.advice.suggestion, status: "todo" };
  const task = { id: suggestion.task_id, status: "todo", due_at: "2026-10-09T16:00:00Z" };
  const updatedTask = { ...task, due_at: "2026-10-10T16:00:00Z" };
  const calls = [];
  const updates = [];
  let taskListRefreshes = 0;
  let adviceAndTodayRefreshes = 0;
  const action = createTaskPostponeAction({
    storage: new MemoryStorage(),
    getUserId: () => "user-a",
    createId: () => UUID,
    requestJson: async (url, options) => {
      calls.push({ url, options });
      if (url.endsWith("/postpone")) return { message: "顺延已确认" };
      if (url.endsWith("/with-subtasks")) return { task: updatedTask };
      throw new Error(`unexpected request: ${url}`);
    },
    refreshTaskList: async () => { taskListRefreshes += 1; },
    onTaskUpdated: async (updated) => { updates.push(updated); },
  });

  await withFakeDocument(async () => {
    const mount = new FakeElement("div");
    renderAdvicePayload(
      { ...fixtures.advice, suggestion }, new FakeElement("p"), mount, async () => {}, undefined, undefined, undefined,
      findBoundSuggestionTask(suggestion, [task]), action,
      async () => { adviceAndTodayRefreshes += 1; return true; },
    );

    await findByClass(mount, "suggestion-postpone").click();

    assert.equal(calls.filter(({ url }) => url.endsWith("/postpone")).length, 1);
    const [post] = calls;
    assert.equal(post.url, `/api/tasks/${suggestion.task_id}/postpone`, "only the current suggestion.task_id is targeted");
    assert.equal(post.options.method, "POST");
    assert.match(post.options.headers["Idempotency-Key"], /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
    assert.equal(post.options.headers["Idempotency-Key"], UUID);
    assert.deepEqual(JSON.parse(post.options.body), { days: 1 });
    assert.equal(taskListRefreshes, 1);
    assert.deepEqual(updates, [updatedTask]);
    assert.equal(adviceAndTodayRefreshes, 1);
    assert.equal(action.getPendingIntent(suggestion.task_id), null);
    assert.match(findByClass(mount, "suggestion-postpone-feedback").textContent, /已顺延一天/);
  });
});

test("definite postpone failures preserve the suggestion UI and do not refresh as success", async () => {
  for (const failure of [
    Object.assign(new Error("任务不存在或当前不可用"), { status: 404, payload: { error: "task_not_found" } }),
    Object.assign(new Error("任务状态冲突"), { status: 409, payload: { error: "task_conflict" } }),
  ]) {
    const suggestion = { ...fixtures.advice.suggestion, status: "todo" };
    const task = { id: suggestion.task_id, status: "todo", due_at: "2026-10-09T16:00:00Z" };
    let refreshCount = 0;
    const action = createTaskPostponeAction({
      storage: new MemoryStorage(),
      getUserId: () => "user-a",
      createId: () => "ddf93675-91fd-4511-8c13-06a0e70f8b39",
      requestJson: async () => { throw failure; },
    });

    await withFakeDocument(async () => {
      const mount = new FakeElement("div");
      renderAdvicePayload(
        { ...fixtures.advice, suggestion }, new FakeElement("p"), mount, async () => {}, undefined, undefined, undefined,
        task, action, async () => { refreshCount += 1; return true; },
      );
      await findByClass(mount, "suggestion-postpone").click();

      const button = findByClass(mount, "suggestion-postpone");
      const feedback = findByClass(mount, "suggestion-postpone-feedback");
      assert.equal(button.disabled, false);
      assert.equal(button.textContent, "一天后再处理");
      assert.equal(findByClass(mount, "suggestion-status").textContent, "待办");
      assert.equal(feedback.attributes.role, "alert");
      assert.match(feedback.textContent, /未修改/);
      assert.equal(findByClass(mount, "suggestion-postpone-retry").hidden, true);
      assert.equal(refreshCount, 0);
      assert.equal(action.getPendingIntent(suggestion.task_id), null);
    });
  }
});

test("uncertain suggestion postpone requires explicit retry with the exact original key", async () => {
  const UUID = "381e20aa-fc25-429a-b29b-a5c0088d7620";
  const suggestion = { ...fixtures.advice.suggestion, status: "todo" };
  const task = { id: suggestion.task_id, status: "todo", due_at: "2026-10-09T16:00:00Z" };
  const updatedTask = { ...task, due_at: "2026-10-10T16:00:00Z" };
  const calls = [];
  let postCount = 0;
  let adviceAndTodayRefreshes = 0;
  const action = createTaskPostponeAction({
    storage: new MemoryStorage(),
    getUserId: () => "user-a",
    createId: () => UUID,
    requestJson: async (url, options) => {
      calls.push({ url, options });
      if (url.endsWith("/postpone")) {
        postCount += 1;
        if (postCount === 1) throw new TypeError("response lost");
        return { message: "原顺延请求已确认" };
      }
      if (url.endsWith("/with-subtasks")) return { task: updatedTask };
      throw new Error(`unexpected request: ${url}`);
    },
  });

  await withFakeDocument(async () => {
    const mount = new FakeElement("div");
    const render = () => renderAdvicePayload(
      { ...fixtures.advice, suggestion }, new FakeElement("p"), mount, async () => {}, undefined, undefined, undefined,
      task, action, async () => { adviceAndTodayRefreshes += 1; return true; },
    );
    render();

    await findByClass(mount, "suggestion-postpone").click();
    assert.equal(postCount, 1);
    assert.equal(action.getPendingIntent(suggestion.task_id).status, "uncertain");
    assert.equal(findByClass(mount, "suggestion-postpone").disabled, true);
    assert.equal(findByClass(mount, "suggestion-postpone-retry").hidden, false);
    assert.equal(adviceAndTodayRefreshes, 0, "an uncertain result does not refresh the bound advice away");

    render();
    assert.equal(postCount, 1, "rerendering a pending intent never automatically resends it");
    assert.equal(findByClass(mount, "suggestion-postpone").disabled, true);
    assert.equal(findByClass(mount, "suggestion-postpone-retry").hidden, false);

    await findByClass(mount, "suggestion-postpone-retry").click();
    const posts = calls.filter(({ url }) => url.endsWith("/postpone"));
    assert.equal(posts.length, 2);
    assert.deepEqual(posts.map(({ url }) => url), [
      `/api/tasks/${suggestion.task_id}/postpone`,
      `/api/tasks/${suggestion.task_id}/postpone`,
    ]);
    assert.deepEqual(posts.map(({ options }) => options.headers["Idempotency-Key"]), [UUID, UUID]);
    assert.deepEqual(posts.map(({ options }) => JSON.parse(options.body)), [{ days: 1 }, { days: 1 }]);
    assert.equal(adviceAndTodayRefreshes, 1, "refresh runs only after the retry is confirmed");
    assert.equal(action.getPendingIntent(suggestion.task_id), null);
  });
});
