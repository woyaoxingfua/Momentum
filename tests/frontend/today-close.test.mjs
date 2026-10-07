import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { loadOpenTaskSummary, renderSuggestion, renderTodayReview, setFocusEntryAvailability } from "../../src/momentum_agent/static/js/advice-review.mjs";

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
    this.classList = {
      values: new Set(),
      add: (value) => this.classList.values.add(value),
      remove: (value) => this.classList.values.delete(value),
      contains: (value) => this.classList.values.has(value),
    };
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  setAttribute(name, value) { this.attributes[name] = value; }
  async click() { return this.listeners.get("click")?.(); }
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

function findAll(element, className) {
  const found = [];
  if (element.className.split(" ").includes(className)) found.push(element);
  for (const child of element.children) found.push(...findAll(child, className));
  return found;
}

function reviewFixture() {
  return {
    localDate: "2026-10-04",
    completed_count: 2,
    today_focus_actual_seconds: 900,
    today_focus_session_count: 1,
    completed_events: [],
  };
}

test("open-task API loader reads only todo/doing and excludes closed or mismatched statuses", async () => {
  const calls = [];
  const tasks = await loadOpenTaskSummary(async (url) => {
    calls.push(url);
    if (url.endsWith("todo")) return { tasks: [
      { id: 1, status: "todo", title: "有截止日", due_at: "2026-10-05T10:00:00Z" },
      { id: 3, status: "done", title: "已完成" },
    ] };
    return { tasks: [
      { id: 2, status: "doing", title: "无截止日", due_at: null },
      { id: 4, status: "abandoned", title: "已放弃" },
      { id: 1, status: "todo", title: "重复项目" },
    ] };
  });
  assert.deepEqual(calls, ["/api/tasks?status=todo", "/api/tasks?status=doing"]);
  assert.deepEqual(tasks.map(({ id }) => id), [1, 2]);
});

test("today close renders an independent todo/doing summary, explicit no-due reason, and a clearly worded existing-due action", async () => {
  await withFakeDocument(async () => {
    const mount = new FakeElement("section");
    renderTodayReview(mount, reviewFixture(), { unfinishedTasks: [
      { id: 7, status: "todo", title: "按现有截止日顺延", due_at: "2026-10-05T10:00:00Z" },
      { id: 8, status: "doing", title: "没有截止日", due_at: null },
      { id: 9, status: "done", title: "不应出现在未完成摘要" },
      { id: 10, status: "abandoned", title: "也不应出现" },
    ], onPostpone: async () => {} });

    const summaryTitle = findAll(mount, "today-review-section-title").find((node) => node.textContent === "尚未完成");
    assert.ok(summaryTitle, "the open-task summary has an independent heading");
    const text = textContent(mount);
    assert.match(text, /不按今天到期筛选/);
    assert.doesNotMatch(text, /不应出现在未完成摘要|也不应出现/);
    assert.match(text, /未设置截止日，无法顺延/);
    const buttons = findAll(mount, "today-review-postpone");
    assert.equal(buttons.length, 1);
    assert.equal(buttons[0].textContent, "顺延现有截止日 1 天");
    assert.equal(buttons[0].disabled, false);
  });
});

test("no-due todo and doing tasks expose the shared edit-deadline action without altering task identity or status", async () => {
  await withFakeDocument(async () => {
    const mount = new FakeElement("section");
    const todo = { id: 31, status: "todo", title: "待办无截止", due_at: null };
    const doing = { id: 32, status: "doing", title: "进行中无截止", due_at: null };
    const edited = [];
    renderTodayReview(mount, reviewFixture(), {
      unfinishedTasks: [todo, doing],
      onEditTask: async (task) => { edited.push(task); },
    });

    const buttons = findAll(mount, "today-review-set-due");
    assert.equal(buttons.length, 2);
    assert.deepEqual(buttons.map((button) => button.textContent), ["设置截止时间", "设置截止时间"]);
    assert.match(buttons[0].attributes["aria-label"], /待办无截止/);
    assert.match(buttons[1].attributes["aria-label"], /进行中无截止/);
    await buttons[0].click();
    await buttons[1].click();
    assert.deepEqual(edited, [todo, doing], "the existing task objects reach the shared editor unchanged");
    assert.deepEqual(edited.map(({ id, title, status, due_at }) => ({ id, title, status, due_at })), [
      { id: 31, title: "待办无截止", status: "todo", due_at: null },
      { id: 32, title: "进行中无截止", status: "doing", due_at: null },
    ]);
    assert.equal(findAll(mount, "today-review-postpone").length, 0);
  });
});

test("todo and doing cards expose the shared completion action and synchronously ignore repeated activation", async () => {
  await withFakeDocument(async () => {
    const mount = new FakeElement("section");
    const calls = [];
    const finishCompletions = [];
    renderTodayReview(mount, reviewFixture(), {
      unfinishedTasks: [
        { id: 31, status: "todo", title: "待完成待办", due_at: "2026-10-05T10:00:00Z" },
        { id: 32, status: "doing", title: "待完成进行中", due_at: null },
        { id: 33, status: "done", title: "不应显示完成动作", due_at: null },
        { id: 34, status: "abandoned", title: "也不应显示", due_at: null },
      ],
      onCompleteTask: async (taskId, button) => {
        calls.push({ taskId, button });
        await new Promise((resolve) => finishCompletions.push(resolve));
      },
      onPostpone: async () => {},
      onEditTask: async () => {},
    });

    const buttons = findAll(mount, "today-review-complete");
    assert.equal(buttons.length, 2);
    assert.deepEqual(buttons.map((button) => button.textContent), ["完成", "完成"]);
    assert.match(buttons[0].attributes["aria-label"], /待完成待办/);
    assert.match(buttons[1].attributes["aria-label"], /待完成进行中/);
    assert.doesNotMatch(textContent(mount), /不应显示完成动作|也不应显示/);

    const first = buttons[0].click();
    assert.equal(buttons[0].disabled, true);
    assert.equal(buttons[0].attributes["aria-busy"], "true");
    assert.equal(buttons[0].classList.contains("is-pending"), true);
    const doing = buttons[1].click();
    assert.equal(buttons[1].disabled, true);
    assert.equal(calls.length, 2);
    assert.deepEqual(calls.map((call) => call.taskId), [31, 32]);
    const repeated = buttons[0].click();
    assert.equal(calls.length, 2);
    assert.equal(calls[0].taskId, 31);
    assert.equal(calls[0].button, buttons[0]);

    finishCompletions.forEach((finish) => finish());
    await Promise.all([first, doing, repeated]);
    assert.equal(buttons[0].disabled, false);
    assert.equal(buttons[0].attributes["aria-busy"], "false");
    assert.equal(buttons[0].classList.contains("is-pending"), false);
  });
});

test("today-close starts focus with the original task, prevents repeat activation, and retains completion, postpone, and deadline actions", async () => {
  setFocusEntryAvailability(false);
  try {
    await withFakeDocument(async () => {
      const mount = new FakeElement("section");
      const todo = { id: 41, status: "todo", title: "原始待办 B", due_at: "2026-10-06T12:00:00Z" };
      const doing = { id: 42, status: "doing", title: "无截止进行中", due_at: null };
      const calls = [];
      let finishStart;
      const gate = new Promise((resolve) => { finishStart = resolve; });
      renderTodayReview(mount, reviewFixture(), {
        unfinishedTasks: [todo, doing],
        onCompleteTask: async () => {},
        onPostpone: async () => {},
        onEditTask: async () => {},
        onStartTask: async (task) => {
          calls.push(task);
          setFocusEntryAvailability(true);
          await gate;
        },
      });

      const starts = findAll(mount, "today-review-start-focus");
      assert.equal(starts.length, 2);
      assert.deepEqual(starts.map((button) => button.textContent), ["开始专注", "开始专注"]);
      assert.match(starts[0].attributes["aria-label"], /原始待办 B/);
      assert.equal(findAll(mount, "today-review-complete").length, 2);
      assert.equal(findAll(mount, "today-review-postpone").length, 1);
      assert.equal(findAll(mount, "today-review-set-due").length, 1);

      const first = starts[0].click();
      assert.equal(starts[0].disabled, true);
      assert.equal(starts[0].attributes["aria-busy"], "true");
      assert.equal(calls.length, 1);
      assert.strictEqual(calls[0], todo, "the exact open-task object reaches the shared focus controller");
      assert.equal(calls[0].id, 41);
      assert.equal(starts[1].disabled, true, "the controller's pending state disables every focus card");

      await Promise.all([starts[0].click(), starts[1].click()]);
      assert.equal(calls.length, 1, "repeated clicks do not dispatch another start");
      finishStart();
      await first;
      assert.equal(starts[0].disabled, true, "the active session keeps all focus entries disabled");
      setFocusEntryAvailability(false);
      assert.equal(starts[0].disabled, false);
      assert.equal(starts[1].disabled, false);
    });
  } finally {
    setFocusEntryAvailability(false);
  }
});

test("today-close focus entries render disabled during an existing active session and send nothing", async () => {
  setFocusEntryAvailability(true);
  try {
    await withFakeDocument(async () => {
      const mount = new FakeElement("section");
      let requests = 0;
      renderTodayReview(mount, reviewFixture(), {
        unfinishedTasks: [
          { id: 51, status: "todo", title: "已有会话时的待办", due_at: null },
          { id: 52, status: "doing", title: "已有会话时的进行中", due_at: "2026-10-08T12:00:00Z" },
        ],
        onStartTask: async () => { requests += 1; },
      });
      const starts = findAll(mount, "today-review-start-focus");
      assert.equal(starts.length, 2);
      assert.ok(starts.every((button) => button.disabled));
      await Promise.all(starts.map((button) => button.click()));
      assert.equal(requests, 0);
      setFocusEntryAvailability(false);
      assert.ok(starts.every((button) => !button.disabled));
    });
  } finally {
    setFocusEntryAvailability(false);
  }
});

test("next-step suggestion remains disabled when its shared focus controller starts a session", async () => {
  setFocusEntryAvailability(false);
  try {
    await withFakeDocument(async () => {
      const mount = new FakeElement("div");
      const suggestion = { task_id: 61, status: "todo", title: "建议任务" };
      renderSuggestion(suggestion, mount, async (task) => {
        assert.strictEqual(task, suggestion);
        setFocusEntryAvailability(true);
      });
      const button = findAll(mount, "suggestion-start")[0];
      await button.click();
      assert.equal(button.disabled, true, "renderSuggestion finally must preserve active controller state");
      setFocusEntryAvailability(false);
      assert.equal(button.disabled, false, "the entry is available again after focus becomes idle");
    });
  } finally {
    setFocusEntryAvailability(false);
  }
});

test("postpone interaction locks synchronously and repeated activation makes one mocked request", async () => {
  await withFakeDocument(async () => {
    const mount = new FakeElement("section");
    let requestCount = 0;
    let finishRequest;
    const gate = new Promise((resolve) => { finishRequest = resolve; });
    renderTodayReview(mount, reviewFixture(), {
      unfinishedTasks: [{ id: 11, status: "doing", title: "正在处理", due_at: "2026-10-06T12:00:00Z" }],
      onPostpone: async (task) => {
        requestCount += 1;
        assert.equal(task.id, 11);
        await gate;
        return { ...task, due_at: "2026-10-07T12:00:00Z" };
      },
    });
    const button = findAll(mount, "today-review-postpone")[0];
    const first = button.click();
    assert.equal(button.disabled, true);
    assert.equal(button.attributes["aria-busy"], "true");
    assert.equal(button.classList.contains("is-pending"), true);
    const second = button.click();
    assert.equal(requestCount, 1);
    finishRequest();
    await Promise.all([first, second]);
    assert.equal(button.disabled, false, "a later explicit click may create a distinct one-day intent");
    assert.equal(button.textContent, "再顺延 1 天");
    assert.equal(button.attributes["aria-busy"], "false");
    assert.equal(button.classList.contains("is-pending"), false);
  });
});

test("successful mocked result updates only the target due label and allows a later explicit intent", async () => {
  await withFakeDocument(async () => {
    const mount = new FakeElement("section");
    renderTodayReview(mount, reviewFixture(), {
      unfinishedTasks: [
        { id: 13, status: "todo", title: "目标 task", due_at: "2026-10-04T12:00:00Z" },
        { id: 14, status: "doing", title: "保持不变", due_at: "2026-10-09T12:00:00Z" },
      ],
      onPostpone: async (task) => ({ ...task, due_at: "2026-10-05T12:00:00Z" }),
    });
    const beforeMetrics = findAll(mount, "today-review-metric-value").map((node) => node.textContent);
    const buttons = findAll(mount, "today-review-postpone");
    await buttons[0].click();
    assert.match(textContent(mount), /已顺延 1 天/);
    assert.equal(buttons[0].disabled, false);
    assert.equal(buttons[0].textContent, "再顺延 1 天");
    assert.equal(buttons[1].disabled, false);
    assert.deepEqual(findAll(mount, "today-review-metric-value").map((node) => node.textContent), beforeMetrics);
  });
});

test("without an injected backend handler, an existing-due button is rendered but remains safely disabled", async () => {
  await withFakeDocument(async () => {
    const mount = new FakeElement("section");
    renderTodayReview(mount, reviewFixture(), {
      unfinishedTasks: [{ id: 12, status: "todo", title: "待接入", due_at: "2026-10-08T12:00:00Z" }],
    });
    const button = findAll(mount, "today-review-postpone")[0];
    assert.ok(button);
    assert.equal(button.textContent, "顺延现有截止日 1 天");
    assert.equal(button.disabled, true);
    assert.equal(button.listeners.has("click"), false);
  });
});

test("explicit postpone errors are visible and leave completion/focus metrics unchanged", async () => {
  await withFakeDocument(async () => {
    const mount = new FakeElement("section");
    const stableReview = reviewFixture();
    renderTodayReview(mount, stableReview, {
      unfinishedTasks: [{ id: 21, status: "doing", title: "后端已拒绝", due_at: "2026-10-07T12:00:00Z" }],
      onPostpone: async () => { throw Object.assign(new Error("该任务当前无法顺延截止日。"), { status: 409 }); },
    });
    const beforeMetrics = findAll(mount, "today-review-metric-value").map((node) => node.textContent);
    const button = findAll(mount, "today-review-postpone")[0];
    await button.click();
    const feedback = findAll(mount, "today-review-postpone-feedback")[0];
    assert.equal(feedback.hidden, false);
    assert.equal(feedback.textContent, "该任务当前无法顺延截止日。");
    assert.equal(feedback.attributes.role, "alert");
    assert.equal(button.disabled, false);
    assert.deepEqual(findAll(mount, "today-review-metric-value").map((node) => node.textContent), beforeMetrics);
  });
});

test("uncertain mocked result updates the observed due date, locks ordinary action, and exposes an accessible explicit retry", async () => {
  await withFakeDocument(async () => {
    const mount = new FakeElement("section");
    let requestCount = 0;
    let pendingIntent = null;
    const onPostpone = async () => {
      requestCount += 1;
      pendingIntent = {
        status: "uncertain",
        last_error_message: "顺延请求结果不确定；GET 不能证明请求或 updated event 已结算。请使用原请求再试。",
      };
      throw Object.assign(new Error(pendingIntent.last_error_message), {
        postponeUncertain: true,
        pendingIntent,
        reconciledTask: { id: 22, due_at: "2026-10-08T12:00:00Z" },
      });
    };
    onPostpone.getPendingIntent = () => pendingIntent;
    onPostpone.retry = async () => {
      requestCount += 1;
      throw Object.assign(new Error("原请求重试仍不确定。"), { pendingIntent });
    };
    renderTodayReview(mount, reviewFixture(), {
      unfinishedTasks: [{ id: 22, status: "doing", title: "不确定结果", due_at: "2026-10-07T12:00:00Z" }],
      onPostpone,
    });
    const button = findAll(mount, "today-review-postpone")[0];
    await button.click();
    assert.equal(button.disabled, true);
    const retry = findAll(mount, "today-review-postpone-retry")[0];
    assert.equal(retry.hidden, false);
    assert.match(retry.attributes["aria-label"], /使用原请求再试/);
    assert.match(findAll(mount, "today-review-postpone-feedback")[0].textContent, /不能证明请求或 updated event 已结算/);
    assert.match(textContent(mount), /2026[/-]10[/-]08/);
    await button.click();
    assert.equal(requestCount, 1);
    await retry.click();
    assert.equal(requestCount, 2, "retry is a distinct explicit action");
    assert.equal(retry.hidden, false);
  });
});

test("unfinished summary styles stack actions at 390px without horizontal overflow rules", async () => {
  const css = await readFile(new URL("../../src/momentum_agent/static/app.css", import.meta.url), "utf8");
  assert.match(css, /@media\s*\(max-width:\s*560px\)[\s\S]*?\.today-review-open-task\s*\{\s*flex-direction:\s*column;/);
  assert.match(css, /\.today-review-open-task-actions,[\s\S]*?\.today-review-postpone\s*\{\s*width:\s*100%;/);
  assert.match(css, /\.today-review-postpone-retry\[hidden\]\s*\{\s*display:\s*none\s*!important;/);
  assert.match(css, /\.today-review-open-task-actions\s*\{\s*display:\s*flex;\s*flex-direction:\s*column;\s*align-items:\s*stretch;/);
  assert.match(css, /\.today-review-open-task-actions\s*>\s*button\s*\{\s*width:\s*100%;\s*max-width:\s*100%;/);
  assert.match(css, /\.today-review-complete\s*\{\s*min-height:\s*36px;/);
  assert.match(css, /\.today-review-set-due\s*\{\s*min-height:\s*36px;/);
});

test("today review counts completion events and preserves repeated done transitions as distinct completion records", async () => {
  await withFakeDocument(async () => {
    const mount = new FakeElement("section");
    const repeatedCompletion = {
      localDate: "2026-10-04",
      completed_count: 2,
      today_focus_actual_seconds: 0,
      today_focus_session_count: 0,
      completed_events: [
        { task_id: 42, title: "同一标题的任务", completed_at: "2026-10-04T08:15:10Z" },
        { task_id: 42, title: "同一标题的任务", completed_at: "2026-10-04T08:15:12Z" },
      ],
    };

    renderTodayReview(mount, repeatedCompletion);
    const metricLabels = findAll(mount, "today-review-metric-label");
    const metricValues = findAll(mount, "today-review-metric-value");
    assert.equal(metricLabels[0].textContent, "完成事件");
    assert.equal(metricValues[0].textContent, "2 条");
    assert.equal(findAll(mount, "today-review-section-title")[0].textContent, "完成记录");
    assert.match(textContent(mount), /同一任务重开后再次完成，会新增一条完成记录并另计。/);

    const rows = findAll(mount, "today-review-event");
    assert.equal(rows.length, 2, "the same task ID is rendered once per completion event, not deduplicated");
    assert.deepEqual(rows.map((row) => findAll(row, "today-review-event-title")[0].textContent), [
      "同一标题的任务",
      "同一标题的任务",
    ]);
    const times = rows.map((row) => findAll(row, "today-review-event-reference")[0].textContent);
    assert.match(times[0], /\d{2}:\d{2}:\d{2}/);
    assert.match(times[1], /\d{2}:\d{2}:\d{2}/);
    assert.notEqual(times[0], times[1], "completion times remain distinguishable within the same minute");

    renderTodayReview(mount, {
      ...repeatedCompletion,
      completed_count: 1,
      completed_events: [repeatedCompletion.completed_events[0]],
    });
    assert.equal(findAll(mount, "today-review-metric-value")[0].textContent, "1 条");
    assert.equal(findAll(mount, "today-review-event").length, 1);
  });
});
