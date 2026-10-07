import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const focusSource = await readFile(
  new URL("../../src/momentum_agent/static/js/focus.js", import.meta.url),
  "utf8",
);

function extractFocusCompletion(dependencies) {
  const start = focusSource.indexOf("async function focusDone()");
  assert.ok(start >= 0, "focusDone is present");
  const declaration = focusSource.slice(start);
  return new Function("dependencies", `
    const {
      _focusBreakIntervalId, _focusClock,
      _focusTimerState, clearInterval, completeTask, document, loadFocusStats,
      populateFocusTaskSelect, syncFocusEntryAvailability,
    } = dependencies;
    let _focusLastPersistedSeconds = dependencies._focusLastPersistedSeconds;
    let _focusFinishRequest = null;
    ${declaration}
    return { focusDone, retryFocusTaskCompletion };
  `)(dependencies);
}

class FakeElement {
  constructor(id, registry) {
    this.id = id;
    this.registry = registry;
    this.hidden = false;
    this.disabled = false;
    this.textContent = "";
    this.style = { cssText: "" };
    this.attributes = {};
    this.children = [];
    this.listeners = new Map();
    const classes = new Set();
    this.classList = {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      contains: (name) => classes.has(name),
    };
  }

  setAttribute(name, value) {
    this.attributes[name] = value;
  }

  addEventListener(name, listener) {
    this.listeners.set(name, listener);
  }

  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
    if (child.id) this.registry.set(child.id, child);
    return child;
  }

  remove() {
    if (this.id && this.registry.get(this.id) === this) this.registry.delete(this.id);
    if (this.parentElement) {
      this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
      this.parentElement = null;
    }
  }

  async click() {
    if (this.disabled || this.hidden) return;
    return this.listeners.get("click")?.({ target: this });
  }
}

function createFocusHarness({ result, pendingIntent = null, retryResult = { status: "success" } }) {
  const elements = new Map();
  const makeElement = (id) => {
    const element = new FakeElement(id, elements);
    elements.set(id, element);
    return element;
  };
  const document = {
    getElementById: (id) => elements.get(id) || null,
    createElement: () => new FakeElement("", elements),
  };
  const focusBreak = makeElement("focusBreak");
  const focusRecovery = makeElement("focusRecovery");
  const focusIdle = makeElement("focusIdle");
  const focusDoneBtn = makeElement("focusDoneBtn");
  const focusPauseBtn = makeElement("focusPauseBtn");
  const focusStopBtn = makeElement("focusStopBtn");
  focusRecovery.classList.add("hidden");
  focusIdle.classList.add("hidden");

  const state = {
    taskId: 41,
    taskTitle: "测试任务",
    phase: "break",
    remainingSeconds: 30,
    elapsedSeconds: 120,
    paused: false,
    sessionId: "focus-session-not-an-idempotency-key",
    startedAt: "2026-10-06T00:00:00Z",
    completionPending: false,
    sessionSaved: true,
    sessionResult: { saved: true },
    finishPayload: { actual_seconds: 120 },
  };
  const normalCalls = [];
  const retryCalls = [];
  const clock = { resets: 0, reset() { this.resets += 1; } };
  const completeTask = async (taskId) => {
    normalCalls.push(taskId);
    return result;
  };
  completeTask.getPendingIntent = () => pendingIntent;
  completeTask.retry = async (taskId) => {
    retryCalls.push({ taskId, intent: pendingIntent });
    pendingIntent = null;
    return retryResult;
  };
  let availabilitySyncs = 0;
  let statsLoads = 0;
  let taskSelectLoads = 0;
  const focus = extractFocusCompletion({
    _focusBreakIntervalId: 17,
    _focusClock: clock,
    _focusLastPersistedSeconds: 5,
    _focusTimerState: state,
    clearInterval: (id) => assert.equal(id, 17),
    completeTask,
    document,
    loadFocusStats: () => { statsLoads += 1; },
    populateFocusTaskSelect: () => { taskSelectLoads += 1; },
    syncFocusEntryAvailability: () => { availabilitySyncs += 1; },
  });

  return {
    ...focus,
    completeTask,
    document,
    elements,
    focusBreak,
    focusDoneBtn,
    focusIdle,
    focusPauseBtn,
    focusRecovery,
    focusStopBtn,
    normalCalls,
    retryCalls,
    state,
    clock,
    get counters() { return { availabilitySyncs, statsLoads, taskSelectLoads }; },
  };
}

test("the rest-panel Done button uses the shared completeTask controller for the focus task", async () => {
  assert.match(focusSource, /import \{ completeTask \} from "\.\/tasks\.js";/);
  assert.doesNotMatch(focusSource, /import \{[^}]*\bloadTasks\b[^}]*\} from "\.\/tasks\.js";/);
  assert.match(focusSource, /document\.getElementById\("focusDoneBtn"\)\.addEventListener\("click", \(\) => \{ void focusDone\(\); \}\);/);

  const focus = createFocusHarness({ result: { status: "success", response: { message: "已完成" } } });
  await focus.focusDone();
  assert.deepEqual(focus.normalCalls, [41]);
});

test("success closes and resets the focus rest panel only after task completion succeeds", async () => {
  const focus = createFocusHarness({ result: { status: "success", response: { message: "已完成" } } });
  await focus.focusDone();

  assert.equal(focus.focusBreak.classList.contains("hidden"), true);
  assert.equal(focus.focusRecovery.classList.contains("hidden"), true);
  assert.equal(focus.focusIdle.classList.contains("hidden"), false);
  assert.equal(focus.state.phase, "idle");
  assert.equal(focus.state.taskId, null, "resetFocusSession clears the focus task only after success");
  assert.equal(focus.clock.resets, 1);
  assert.equal(focus.focusDoneBtn.disabled, false);
  assert.equal(focus.counters.statsLoads, 1);
  assert.equal(focus.counters.taskSelectLoads, 1);
  assert.equal(focus.normalCalls.length, 1);
  assert.equal(focus.retryCalls.length, 0);
});

test("uncertain and pending results preserve visible feedback without an automatic retry", async (t) => {
  const cases = [
    {
      name: "uncertain with retryable intent",
      result: { status: "uncertain", error: new Error("request timed out") },
      pendingIntent: { task_id: "41", idempotency_key: "test-only-key", status: "uncertain" },
      retryVisible: true,
    },
    {
      name: "bare 401/timeout error remains on the uncertain result path",
      result: { status: "uncertain", error: new Error("401 Unauthorized: request timed out") },
      pendingIntent: { task_id: "41", idempotency_key: "test-only-key", status: "uncertain" },
      retryVisible: true,
    },
    {
      name: "pending while original request is sending",
      result: { status: "pending" },
      pendingIntent: { task_id: "41", idempotency_key: "test-only-key", status: "sending" },
      retryVisible: false,
    },
    {
      name: "pending with a matching uncertain intent offers explicit retry",
      result: { status: "pending" },
      pendingIntent: { task_id: "41", idempotency_key: "test-only-key", status: "uncertain" },
      retryVisible: true,
    },
    {
      name: "uncertain intent for another task does not expose retry",
      result: { status: "uncertain" },
      pendingIntent: { task_id: "42", idempotency_key: "test-only-other-key", status: "uncertain" },
      retryVisible: false,
    },
  ];

  for (const item of cases) {
    await t.test(item.name, async () => {
      const focus = createFocusHarness(item);
      await focus.focusDone();
      const status = focus.document.getElementById("focusTaskCompletionStatus");
      const retryButton = focus.document.getElementById("focusTaskCompletionRetry");

      assert.ok(status, "completion feedback is rendered in the visible rest panel");
      assert.equal(status.attributes.role, "status");
      assert.equal(status.attributes["aria-live"], "polite");
      assert.match(status.textContent, /^任务完成待确认：/);
      assert.ok(!status.textContent.includes("test-only-key"), "idempotency keys are never rendered");
      assert.equal(focus.focusBreak.classList.contains("hidden"), false);
      assert.equal(focus.focusIdle.classList.contains("hidden"), true);
      assert.equal(focus.state.phase, "break");
      assert.equal(focus.state.taskId, 41);
      assert.equal(focus.clock.resets, 0);
      assert.equal(focus.focusDoneBtn.disabled, true, "ordinary Done cannot create a replacement request");
      assert.equal(retryButton.hidden, !item.retryVisible);
      assert.equal(retryButton.disabled, !item.retryVisible);
      assert.deepEqual(focus.normalCalls, [41]);
      assert.deepEqual(focus.retryCalls, [], "failure handling never retries automatically");
      assert.equal(focus.counters.statsLoads, 0);
      assert.equal(focus.counters.taskSelectLoads, 0);
    });
  }
});

test("definite 4xx errors show the original error and never offer a retry", async () => {
  const error = Object.assign(new Error("任务已被拒绝：状态不允许完成"), { status: 409 });
  const focus = createFocusHarness({
    result: { status: "error", error },
    pendingIntent: { task_id: "41", idempotency_key: "test-only-stale-key", status: "uncertain" },
  });

  await focus.focusDone();
  const status = focus.document.getElementById("focusTaskCompletionStatus");
  const retryButton = focus.document.getElementById("focusTaskCompletionRetry");
  await retryButton.click();

  assert.equal(status.textContent, error.message, "the original 4xx error remains visible verbatim");
  assert.equal(status.attributes.role, "alert");
  assert.doesNotMatch(status.textContent, /待确认/);
  assert.equal(retryButton.hidden, true, "a deterministic client rejection cannot be retried");
  assert.equal(focus.focusDoneBtn.disabled, true, "Done cannot create a fresh completion request after rejection");
  assert.equal(focus.focusBreak.classList.contains("hidden"), false);
  assert.equal(focus.focusIdle.classList.contains("hidden"), true);
  assert.equal(focus.state.phase, "break");
  assert.equal(focus.state.taskId, 41);
  assert.equal(focus.clock.resets, 0);
  assert.deepEqual(focus.normalCalls, [41]);
  assert.deepEqual(focus.retryCalls, []);
  assert.equal(focus.counters.statsLoads, 0);
  assert.equal(focus.counters.taskSelectLoads, 0);
});

test("non-pending errors without an HTTP status remain visible as errors, not awaiting confirmation", async () => {
  const error = new Error("local completion state could not be saved");
  const focus = createFocusHarness({ result: { status: "error", error }, pendingIntent: null });

  await focus.focusDone();
  const status = focus.document.getElementById("focusTaskCompletionStatus");

  assert.equal(status.textContent, error.message);
  assert.equal(status.attributes.role, "alert");
  assert.doesNotMatch(status.textContent, /待确认/);
  assert.equal(focus.document.getElementById("focusTaskCompletionRetry").hidden, true);
  assert.equal(focus.focusBreak.classList.contains("hidden"), false);
  assert.equal(focus.state.taskId, 41);
  assert.equal(focus.clock.resets, 0);
  assert.deepEqual(focus.retryCalls, []);
});

test("explicit retry uses the same pending completion intent and success clears/closes the panel", async () => {
  const originalIntent = { task_id: "41", idempotency_key: "test-only-same-key", status: "uncertain" };
  const focus = createFocusHarness({
    result: { status: "uncertain" },
    pendingIntent: originalIntent,
    retryResult: { status: "success", response: { message: "已完成" } },
  });
  await focus.focusDone();
  const retryButton = focus.document.getElementById("focusTaskCompletionRetry");
  assert.equal(retryButton.hidden, false);

  await retryButton.click();

  assert.deepEqual(focus.normalCalls, [41]);
  assert.deepEqual(focus.retryCalls, [{ taskId: 41, intent: originalIntent }]);
  assert.equal(focus.state.taskId, null);
  assert.equal(focus.state.phase, "idle");
  assert.equal(focus.focusBreak.classList.contains("hidden"), true);
  assert.equal(focus.focusIdle.classList.contains("hidden"), false);
  assert.equal(focus.document.getElementById("focusTaskCompletionStatus"), null);
  assert.equal(focus.document.getElementById("focusTaskCompletionRetry"), null);
  assert.equal(focus.clock.resets, 1);
  assert.equal(focus.counters.statsLoads, 1);
  assert.equal(focus.counters.taskSelectLoads, 1);
});

test("retry is gated by a matching pending intent and delegates to the shared retry controller", async () => {
  const focus = createFocusHarness({ result: { status: "uncertain" }, pendingIntent: null });
  await focus.focusDone();
  await focus.retryFocusTaskCompletion();

  assert.deepEqual(focus.retryCalls, []);
  assert.match(focusSource, /completeTask\.getPendingIntent\(taskId\)/);
  assert.match(focusSource, /completeTask\.retry\(taskId\)/);
});
