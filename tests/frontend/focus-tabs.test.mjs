import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { FocusClock } from "../../src/momentum_agent/static/js/focus-clock.mjs";
import { FOCUS_SNAPSHOT_VERSION, FocusFinishCoordinator, FocusRecoveryStore } from "../../src/momentum_agent/static/js/focus-recovery.mjs";
import { formatFocusStartError, startFocusSession } from "../../src/momentum_agent/static/js/focus-api.mjs";

import {
  FOCUS_TAB_OWNER_LEASE_MS,
  FocusTabCoordinator,
  focusStartLockName,
  focusTabOwnerKey,
} from "../../src/momentum_agent/static/js/focus-tabs.mjs";

const focusControllerSource = await readFile(
  new URL("../../src/momentum_agent/static/js/focus.js", import.meta.url),
  "utf8",
);

class MemoryStorage {
  values = new Map();
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

class SerialLockManager {
  tails = new Map();
  async request(name, options, callback) {
    assert.equal(options.mode, "exclusive");
    const previous = this.tails.get(name) || Promise.resolve();
    const current = previous.catch(() => {}).then(() => callback({ name, mode: options.mode }));
    this.tails.set(name, current.catch(() => {}));
    return current;
  }
}

function extractFocusSource(startMarker, endMarker) {
  const start = focusControllerSource.indexOf(startMarker);
  assert.ok(start >= 0, `focus.js contains ${startMarker}`);
  const end = focusControllerSource.indexOf(endMarker, start);
  assert.ok(end > start, `focus.js contains ${endMarker} after ${startMarker}`);
  return focusControllerSource.slice(start, end);
}

class FocusStatsElement {
  constructor(tagName = "div") {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.className = "";
    this._textContent = "";
  }

  get textContent() {
    return this._textContent + this.children.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    this._textContent = String(value ?? "");
    this.children = [];
  }

  appendChild(child) {
    this._textContent = "";
    this.children.push(child);
    return child;
  }

  replaceChildren(...children) {
    this._textContent = "";
    this.children = [];
    for (const child of children) this.appendChild(child);
  }

  set innerHTML(_value) {
    throw new Error("focus statistics must render with textContent, not innerHTML");
  }
}

function createFocusStatsFixture(requestJson) {
  const elements = new Map([
    ["focusStats", new FocusStatsElement()],
    ["focusSessionHistoryList", new FocusStatsElement("ul")],
    ["focusSessionHistoryMessage", new FocusStatsElement("p")],
  ]);
  const document = {
    getElementById: (id) => elements.get(id) || null,
    createElement: (tagName) => new FocusStatsElement(tagName),
  };
  const declarations = extractFocusSource(
    "function formatFocusMinutes(value)",
    "\nfunction updateFocusCountdownDisplay()",
  );
  const focus = new Function("dependencies", `
    const { document, requestJson } = dependencies;
    ${declarations}
    return { loadFocusStats };
  `)({ document, requestJson });
  return { elements, focus };
}

function createFocusTaskSelectController(dependencies) {
  const declarations = extractFocusSource(
    "async function populateFocusTaskSelect()",
    "\nfunction formatFocusMinutes(",
  );
  return new Function("dependencies", `
    const { document, requestJson, updateEstimatedFocusStartAvailability } = dependencies;
    ${declarations}
    return { populateFocusTaskSelect };
  `)(dependencies);
}

function createFocusTaskSelectFixture(requestJson, { selectedValue = "", query = "" } = {}) {
  const select = {
    value: selectedValue,
    selectedIndex: selectedValue ? 1 : 0,
    disabled: false,
    options: [],
    set innerHTML(markup) {
      const textContent = markup.replace(/^<option value="">|<\/option>$/g, "");
      this.options = [{ value: "", textContent, dataset: {} }];
      this.value = "";
      this.selectedIndex = 0;
    },
    appendChild(option) { this.options.push(option); },
  };
  select.innerHTML = '<option value="">选择任务...</option>';
  select.value = selectedValue;
  const searchInput = { value: query, oninput: null };
  const loadError = { hidden: true, textContent: "" };
  const elements = new Map([
    ["focusTaskSelect", select],
    ["focusTaskSearch", searchInput],
    ["focusTaskLoadError", loadError],
  ]);
  let availabilityUpdates = 0;
  const focus = createFocusTaskSelectController({
    document: {
      getElementById: (id) => elements.get(id) || null,
      createElement: () => ({ value: "", textContent: "", dataset: {} }),
    },
    requestJson,
    updateEstimatedFocusStartAvailability: () => { availabilityUpdates += 1; },
  });
  return { availabilityUpdates: () => availabilityUpdates, focus, loadError, searchInput, select };
}

function createFocusStartController(dependencies) {
  const declarations = [
    extractFocusSource("function syncFocusEntryAvailability()", "\n// 简易提示"),
    extractFocusSource("function _showToast(msg)", "\nexport function focusInit()"),
    extractFocusSource("function persistFocusSnapshot(", "\nfunction startFocusOwnerHeartbeat()"),
    extractFocusSource("function startFocusOwnerHeartbeat()", "\nfunction releaseFocusTabOwner("),
    extractFocusSource("function releaseFocusTabOwner(", "\nfunction persistFocusBeforePageHide()"),
    extractFocusSource("function updateFocusCountdownDisplay()", "\nexport function startSuggestedFocus("),
    extractFocusSource("export function startSuggestedFocus(suggestion)", "\nasync function focusStart(").replace(/^export /, ""),
    extractFocusSource("async function focusStart(taskOverride = null)", "\nfunction focusTick()"),
    extractFocusSource("function updateFocusBreakDisplay()", "\nasync function focusBreakStartNextRound()"),
    extractFocusSource("async function focusBreakStartNextRound()", "\nasync function focusTogglePause()"),
  ].join("\n\n");

  return new Function("dependencies", `
    const {
      FOCUS_MAX_SAMPLE_GAP_MS, FOCUS_SNAPSHOT_VERSION, FocusFinishCoordinator,
      _focusClock, _focusRecoveryStore, _focusTabCoordinator, _focusTimerState,
      _focusBreakIntervalId: initialBreakIntervalId,
      clearInterval, document, focusTick, formatFocusStartError, requestJson,
      setFocusEntryAvailability, setInterval, setTimeout, startFocusSession,
    } = dependencies;
    let _focusFinishCoordinator = null;
    let _focusFinishRequest = null;
    let _focusLastPersistedSeconds = -1;
    let _focusOwnerHeartbeatIntervalId = null;
    let _focusStartLockHeld = false;
    let _focusStartPending = false;
    let _focusBreakIntervalId = initialBreakIntervalId;
    ${declarations}
    return {
      focusStart,
      startSuggestedFocus,
      focusStartSelectedEstimate,
      updateEstimatedFocusStartAvailability,
      focusBreakStartNextRound,
      get pending() { return _focusStartPending; },
      get state() { return _focusTimerState; },
    };
  `)(dependencies);
}

function createFocusStartRetryFixture(requestJson, { initialState = {}, breakIntervalId = 777 } = {}) {
  const storage = new MemoryStorage();
  const recoveryStore = new FocusRecoveryStore("alice", storage);
  const persistedSnapshots = [];
  const saveSnapshot = recoveryStore.save.bind(recoveryStore);
  recoveryStore.save = (snapshot) => {
    persistedSnapshots.push(snapshot);
    return saveSnapshot(snapshot);
  };
  let activeLockCallbacks = 0;
  let lockCallbackReleases = 0;
  const lockManager = {
    async request(name, options, callback) {
      assert.equal(name, focusStartLockName("alice"));
      assert.equal(options.mode, "exclusive");
      activeLockCallbacks += 1;
      try {
        return await callback({ name, mode: options.mode });
      } finally {
        activeLockCallbacks -= 1;
        lockCallbackReleases += 1;
      }
    },
  };
  const coordinator = new FocusTabCoordinator("alice", {
    storage,
    lockManager,
    tabId: "tab-retry",
  });
  const elements = new Map();
  const makeElement = (id) => {
    const classes = new Set();
    const element = {
      id,
      hidden: false,
      disabled: false,
      textContent: "",
      style: {},
      classList: {
        add: (...names) => names.forEach((name) => classes.add(name)),
        remove: (...names) => names.forEach((name) => classes.delete(name)),
        contains: (name) => classes.has(name),
      },
    };
    elements.set(id, element);
    return element;
  };
  const document = {
    getElementById: (id) => elements.get(id) || null,
    createElement: () => makeElement(""),
    body: { appendChild() {} },
  };
  const taskSelect = makeElement("focusTaskSelect");
  Object.assign(taskSelect, {
    value: "41",
    selectedIndex: 0,
    options: [{
      text: "测试任务（待办 · #41 · 未估时）",
      textContent: "测试任务（待办 · #41 · 未估时）",
      dataset: { taskTitle: "测试任务" },
    }],
  });
  for (const id of [
    "focusStartBtn", "focusStartError", "focusIdle", "focusRecovery", "focusBreak",
    "focusBreakTask", "focusBreakCountdown", "focusBreakStartError", "focusBreakStartBtn", "focusSkipBreakBtn", "focusDoneBtn",
    "focusRunning", "focusCurrentTask", "focusPauseBtn", "focusStopBtn", "focusCountdown",
    "focusProgressFill",
  ]) makeElement(id);
  elements.get("focusStartError").hidden = true;
  elements.get("focusBreakStartError").hidden = true;

  let nextTimerId = 0;
  const timers = { intervals: [], timeouts: [], cleared: [] };
  const setInterval = (callback, delay) => {
    const id = ++nextTimerId;
    timers.intervals.push({ id, callback, delay });
    return id;
  };
  const clearInterval = (id) => timers.cleared.push(id);
  const setTimeout = (callback, delay) => {
    const id = ++nextTimerId;
    timers.timeouts.push({ id, callback, delay });
    return id;
  };
  const focus = createFocusStartController({
    FOCUS_MAX_SAMPLE_GAP_MS: 1500,
    FOCUS_SNAPSHOT_VERSION,
    FocusFinishCoordinator,
    _focusClock: new FocusClock(),
    _focusRecoveryStore: recoveryStore,
    _focusTabCoordinator: coordinator,
    _focusTimerState: {
      taskId: null,
      taskTitle: "",
      durationMinutes: 25,
      remainingSeconds: 0,
      intervalId: null,
      paused: false,
      phase: "idle",
      breakSeconds: 0,
      elapsedSeconds: 0,
      sessionId: null,
      startedAt: null,
      completionPending: false,
      sessionSaved: false,
      sessionResult: null,
      finishPayload: null,
      ...initialState,
    },
    _focusBreakIntervalId: breakIntervalId,
    clearInterval,
    document,
    focusTick: () => {},
    formatFocusStartError,
    requestJson,
    setFocusEntryAvailability: () => {},
    setInterval,
    setTimeout,
    startFocusSession,
  });

  return {
    activeLockCallbacks: () => activeLockCallbacks,
    coordinator,
    elements,
    focus,
    lockCallbackReleases: () => lockCallbackReleases,
    persistedSnapshots,
    recoveryStore,
    storage,
    timers,
  };
}

test("focus start locks and owner keys are isolated by authenticated user", () => {
  assert.notEqual(focusStartLockName("alice"), focusStartLockName("bob"));
  assert.notEqual(focusTabOwnerKey("alice"), focusTabOwnerKey("bob"));
  assert.equal(focusStartLockName(""), null);
  assert.equal(focusTabOwnerKey(" "), null);
});

test("owner lease identifies a live peer tab and expires without deleting snapshots", () => {
  const storage = new MemoryStorage();
  let now = 1000;
  const owner = new FocusTabCoordinator("alice", { storage, now: () => now, tabId: "tab-a" });
  const peer = new FocusTabCoordinator("alice", { storage, now: () => now, tabId: "tab-b" });
  const otherUser = new FocusTabCoordinator("bob", { storage, now: () => now, tabId: "tab-c" });

  assert.equal(owner.refreshOwner("session_123", { force: true }), true);
  assert.equal(owner.hasLiveOwner("session_123"), true);
  assert.equal(owner.isOwnedByAnotherTab("session_123"), false);
  assert.equal(peer.isOwnedByAnotherTab("session_123"), true);
  assert.equal(otherUser.hasLiveOwner("session_123"), false);
  assert.equal(peer.releaseOwner("session_123"), false, "a peer cannot clear the active owner's lease");

  now += FOCUS_TAB_OWNER_LEASE_MS + 1;
  assert.equal(peer.hasLiveOwner("session_123"), false);
  assert.equal(peer.isOwnedByAnotherTab("session_123"), false);
  assert.ok(storage.getItem(focusTabOwnerKey("alice")), "expiry does not delete the durable recovery snapshot/owner record");
  assert.equal(owner.releaseOwner("session_123"), true);
});

test("same-user start callbacks are serialized before the active snapshot is inspected", async () => {
  const locks = new SerialLockManager();
  const first = new FocusTabCoordinator("alice", { storage: new MemoryStorage(), lockManager: locks, tabId: "tab-a" });
  const second = new FocusTabCoordinator("alice", { storage: new MemoryStorage(), lockManager: locks, tabId: "tab-b" });
  let active = 0;
  let maximumActive = 0;
  const run = (coordinator) => coordinator.withStartLock(async () => {
    active += 1;
    maximumActive = Math.max(maximumActive, active);
    await new Promise((resolve) => setTimeout(resolve, 5));
    active -= 1;
  });

  await Promise.all([run(first), run(second)]);
  assert.equal(maximumActive, 1);
});

test("focusStart synchronously ignores a duplicate call while its first start request is pending", async () => {
  const storage = new MemoryStorage();
  const recoveryStore = new FocusRecoveryStore("alice", storage);
  const persistedSnapshots = [];
  const saveSnapshot = recoveryStore.save.bind(recoveryStore);
  recoveryStore.save = (snapshot) => {
    persistedSnapshots.push(snapshot);
    return saveSnapshot(snapshot);
  };
  const coordinator = new FocusTabCoordinator("alice", {
    storage,
    lockManager: new SerialLockManager(),
    tabId: "tab-a",
  });
  const elements = new Map();
  const makeElement = (id) => {
    const classes = new Set();
    const element = {
      id,
      hidden: false,
      disabled: false,
      textContent: "",
      style: {},
      classList: {
        add: (...names) => names.forEach((name) => classes.add(name)),
        remove: (...names) => names.forEach((name) => classes.delete(name)),
        contains: (name) => classes.has(name),
      },
    };
    elements.set(id, element);
    return element;
  };
  const document = {
    getElementById: (id) => elements.get(id) || null,
    createElement: () => makeElement(""),
    body: { appendChild() {} },
  };
  const taskSelect = makeElement("focusTaskSelect");
  Object.assign(taskSelect, {
    value: "41",
    selectedIndex: 0,
    options: [{ text: "测试任务" }],
  });
  for (const id of [
    "focusStartBtn", "focusStartError", "focusIdle", "focusRecovery", "focusBreak",
    "focusRunning", "focusCurrentTask", "focusPauseBtn", "focusStopBtn", "focusCountdown",
    "focusProgressFill",
  ]) makeElement(id);

  const requests = [];
  let resolveStartRequest;
  let markRequestStarted;
  const requestStarted = new Promise((resolve) => { markRequestStarted = resolve; });
  const requestJson = (path, options) => {
    requests.push({ path, options });
    const pending = new Promise((resolve) => { resolveStartRequest = resolve; });
    markRequestStarted();
    return pending;
  };
  let nextTimerId = 0;
  const timers = { intervals: [], timeouts: [], cleared: [] };
  const setInterval = (callback, delay) => {
    const id = ++nextTimerId;
    timers.intervals.push({ id, callback, delay });
    return id;
  };
  const clearInterval = (id) => timers.cleared.push(id);
  const setTimeout = (callback, delay) => {
    const id = ++nextTimerId;
    timers.timeouts.push({ id, callback, delay });
    return id;
  };
  const focus = createFocusStartController({
    FOCUS_MAX_SAMPLE_GAP_MS: 1500,
    FOCUS_SNAPSHOT_VERSION,
    FocusFinishCoordinator,
    _focusClock: new FocusClock(),
    _focusRecoveryStore: recoveryStore,
    _focusTabCoordinator: coordinator,
    _focusTimerState: {
      taskId: null,
      taskTitle: "",
      durationMinutes: 25,
      remainingSeconds: 0,
      intervalId: null,
      paused: false,
      phase: "idle",
      breakSeconds: 0,
      elapsedSeconds: 0,
      sessionId: null,
      startedAt: null,
      completionPending: false,
      sessionSaved: false,
      sessionResult: null,
      finishPayload: null,
    },
    clearInterval,
    document,
    focusTick: () => {},
    formatFocusStartError,
    requestJson,
    setFocusEntryAvailability: () => {},
    setInterval,
    setTimeout,
    startFocusSession,
  });

  const firstTask = { id: 41, title: "待办任务", status: "todo", estimated_minutes: 25 };
  const firstStart = focus.startSuggestedFocus(firstTask);
  await requestStarted;
  assert.equal(focus.pending, true, "the first call sets the pending guard before awaiting the response");
  assert.equal(focus.state.sessionId, null, "the session is not initialized before the start response arrives");
  assert.equal(persistedSnapshots.length, 0);

  const secondStart = focus.startSuggestedFocus({ id: 42, title: "另一待办", status: "todo", estimated_minutes: 12 });
  await secondStart;
  assert.equal(requests.length, 1, "the same-page second call returns before issuing another request");
  assert.equal(focus.state.taskId, null, "a pending start cannot be replaced by another task");

  resolveStartRequest({
    session_id: "focus-session-1",
    started_at: "2026-10-06T10:00:00Z",
  });
  await firstStart;
  await focus.startSuggestedFocus({ id: 43, title: "进行中任务", status: "doing", estimated_minutes: 40 });

  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, "/api/focus/start");
  assert.equal(requests[0].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[0].options.body), { task_id: 41, duration_minutes: 25 });
  assert.equal(focus.pending, false);
  assert.equal(focus.state.phase, "running");
  assert.equal(focus.state.taskId, 41, "an active timer rejects a second task focus start");
  assert.equal(focus.state.sessionId, "focus-session-1");
  assert.equal(persistedSnapshots.length, 1, "exactly one running session snapshot is initialized");
  assert.equal(persistedSnapshots[0].session_id, "focus-session-1");
  assert.equal(recoveryStore.load().session_id, "focus-session-1");
  assert.deepEqual(timers.intervals.map(({ delay }) => delay).sort((a, b) => a - b), [250, 2000]);
  assert.deepEqual(timers.timeouts, []);
});

test("focusStart releases its lock after a definite 409 and allows an explicit retry", async () => {
  const storage = new MemoryStorage();
  const recoveryStore = new FocusRecoveryStore("alice", storage);
  const persistedSnapshots = [];
  const saveSnapshot = recoveryStore.save.bind(recoveryStore);
  recoveryStore.save = (snapshot) => {
    persistedSnapshots.push(snapshot);
    return saveSnapshot(snapshot);
  };
  let activeLockCallbacks = 0;
  let lockCallbackReleases = 0;
  const lockManager = {
    async request(name, options, callback) {
      assert.equal(name, focusStartLockName("alice"));
      assert.equal(options.mode, "exclusive");
      activeLockCallbacks += 1;
      try {
        return await callback({ name, mode: options.mode });
      } finally {
        activeLockCallbacks -= 1;
        lockCallbackReleases += 1;
      }
    },
  };
  const coordinator = new FocusTabCoordinator("alice", {
    storage,
    lockManager,
    tabId: "tab-retry",
  });
  const elements = new Map();
  const makeElement = (id) => {
    const classes = new Set();
    const element = {
      id,
      hidden: false,
      disabled: false,
      textContent: "",
      style: {},
      classList: {
        add: (...names) => names.forEach((name) => classes.add(name)),
        remove: (...names) => names.forEach((name) => classes.delete(name)),
        contains: (name) => classes.has(name),
      },
    };
    elements.set(id, element);
    return element;
  };
  const document = {
    getElementById: (id) => elements.get(id) || null,
    createElement: () => makeElement(""),
    body: { appendChild() {} },
  };
  const taskSelect = makeElement("focusTaskSelect");
  Object.assign(taskSelect, {
    value: "41",
    selectedIndex: 0,
    options: [{ text: "测试任务" }],
  });
  for (const id of [
    "focusStartBtn", "focusStartError", "focusIdle", "focusRecovery", "focusBreak",
    "focusRunning", "focusCurrentTask", "focusPauseBtn", "focusStopBtn", "focusCountdown",
    "focusProgressFill",
  ]) makeElement(id);

  const requests = [];
  const requestJson = (path, options) => {
    requests.push({ path, options });
    if (requests.length === 1) {
      const error = new Error("task is closed");
      error.status = 409;
      return Promise.reject(error);
    }
    return Promise.resolve({
      session_id: "focus-session-2",
      started_at: "2026-10-06T10:05:00Z",
    });
  };
  let nextTimerId = 0;
  const timers = { intervals: [], timeouts: [], cleared: [] };
  const setInterval = (callback, delay) => {
    const id = ++nextTimerId;
    timers.intervals.push({ id, callback, delay });
    return id;
  };
  const clearInterval = (id) => timers.cleared.push(id);
  const setTimeout = (callback, delay) => {
    const id = ++nextTimerId;
    timers.timeouts.push({ id, callback, delay });
    return id;
  };
  const focus = createFocusStartController({
    FOCUS_MAX_SAMPLE_GAP_MS: 1500,
    FOCUS_SNAPSHOT_VERSION,
    FocusFinishCoordinator,
    _focusClock: new FocusClock(),
    _focusRecoveryStore: recoveryStore,
    _focusTabCoordinator: coordinator,
    _focusTimerState: {
      taskId: null,
      taskTitle: "",
      durationMinutes: 25,
      remainingSeconds: 0,
      intervalId: null,
      paused: false,
      phase: "idle",
      breakSeconds: 0,
      elapsedSeconds: 0,
      sessionId: null,
      startedAt: null,
      completionPending: false,
      sessionSaved: false,
      sessionResult: null,
      finishPayload: null,
    },
    clearInterval,
    document,
    focusTick: () => {},
    formatFocusStartError,
    requestJson,
    setFocusEntryAvailability: () => {},
    setInterval,
    setTimeout,
    startFocusSession,
  });

  const rejectedTask = { id: 41, title: "被拒绝的待办", status: "todo", estimated_minutes: 25 };
  await focus.startSuggestedFocus(rejectedTask);

  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, "/api/focus/start");
  assert.equal(requests[0].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[0].options.body), { task_id: 41, duration_minutes: 25 });
  assert.equal(elements.get("focusStartError").hidden, false, "the definite 409 is visible in the error UI");
  assert.match(elements.get("focusStartError").textContent, /409/);
  assert.equal(focus.pending, false);
  assert.equal(focus.state.phase, "idle");
  assert.equal(elements.get("focusStartBtn").disabled, false, "the start button is re-enabled");
  assert.equal(focus.state.sessionId, null);
  assert.equal(persistedSnapshots.length, 0, "a rejected start saves no recovery snapshot");
  assert.equal(recoveryStore.load(), null);
  assert.equal(storage.getItem(focusTabOwnerKey("alice")), null, "a rejected start leaves no owner lease");
  assert.equal(activeLockCallbacks, 0);
  assert.equal(lockCallbackReleases, 1, "the first lock callback has been released");

  await focus.startSuggestedFocus(rejectedTask);

  assert.equal(requests.length, 2, "only the user's explicit second call issues a second POST");
  assert.equal(requests[1].path, "/api/focus/start");
  assert.equal(requests[1].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[1].options.body), { task_id: 41, duration_minutes: 25 });
  assert.equal(focus.pending, false);
  assert.equal(focus.state.phase, "running");
  assert.equal(focus.state.sessionId, "focus-session-2");
  assert.equal(focus.state.startedAt, "2026-10-06T10:05:00Z");
  assert.deepEqual(persistedSnapshots.map(({ session_id }) => session_id), ["focus-session-2"]);
  assert.equal(recoveryStore.load().session_id, "focus-session-2");
  assert.equal(coordinator.readOwner("focus-session-2").session_id, "focus-session-2");
  assert.equal(JSON.parse(storage.getItem(focusTabOwnerKey("alice"))).session_id, "focus-session-2");
  assert.equal(activeLockCallbacks, 0);
  assert.equal(lockCallbackReleases, 2, "the retry's lock callback is also released");
  assert.deepEqual(timers.intervals.map(({ delay }) => delay).sort((a, b) => a - b), [250, 2000]);
  assert.deepEqual(timers.timeouts.map(({ delay }) => delay), [3000], "the 409 error toast is scheduled for dismissal");
});

test("statusless start timeout stays idle and only an explicit click starts a fresh session", async () => {
  const requests = [];
  const { activeLockCallbacks, elements, focus, lockCallbackReleases, persistedSnapshots, recoveryStore, storage, timers } = createFocusStartRetryFixture((path, options) => {
    requests.push({ path, options });
    if (requests.length === 1) return Promise.reject(new Error("request timed out"));
    return Promise.resolve({
      session_id: "focus-session-after-timeout",
      started_at: "2026-10-06T10:10:00Z",
    });
  });

  const timeoutTask = { id: 41, title: "超时待办", status: "todo", estimated_minutes: 25 };
  await focus.startSuggestedFocus(timeoutTask);

  assert.equal(requests.length, 1, "a statusless timeout does not trigger an automatic second POST");
  assert.equal(focus.pending, false);
  assert.equal(focus.state.phase, "idle");
  assert.equal(elements.get("focusStartBtn").disabled, false, "the start button is restored after timeout");
  assert.equal(elements.get("focusStartError").hidden, false);
  assert.equal(elements.get("focusStartError").textContent, "无法确认开始；再次点击将从新时间重新开始");
  assert.equal(focus.state.sessionId, null);
  assert.equal(persistedSnapshots.length, 0);
  assert.equal(recoveryStore.load(), null);
  assert.equal(storage.getItem(focusTabOwnerKey("alice")), null, "the failed start leaves no owner lease");
  assert.equal(activeLockCallbacks(), 0);
  assert.equal(lockCallbackReleases(), 1, "the timeout releases its start lock");

  await focus.startSuggestedFocus(timeoutTask);

  assert.equal(requests.length, 2, "only the explicit click issues the new POST");
  assert.equal(requests[1].path, "/api/focus/start");
  assert.equal(requests[1].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[1].options.body), { task_id: 41, duration_minutes: 25 });
  assert.equal(focus.pending, false);
  assert.equal(focus.state.phase, "running");
  assert.equal(focus.state.sessionId, "focus-session-after-timeout");
  assert.equal(focus.state.startedAt, "2026-10-06T10:10:00Z");
  assert.deepEqual(persistedSnapshots.map(({ session_id }) => session_id), ["focus-session-after-timeout"]);
  assert.equal(recoveryStore.load().session_id, "focus-session-after-timeout");
  assert.equal(JSON.parse(storage.getItem(focusTabOwnerKey("alice"))).session_id, "focus-session-after-timeout");
  assert.equal(elements.get("focusStartError").hidden, true, "a successful explicit retry clears the error UI");
  assert.equal(activeLockCallbacks(), 0);
  assert.equal(lockCallbackReleases(), 2, "the explicit retry also releases its lock");
  assert.deepEqual(timers.timeouts.map(({ delay }) => delay), [3000]);
});

test("focus controller subscribes to cross-tab storage and routes starts through the shared lock", async () => {
  const source = await readFile(new URL("../../src/momentum_agent/static/js/focus.js", import.meta.url), "utf8");
  assert.match(source, /window\.addEventListener\("storage", handleFocusStorageChange\)/);
  assert.match(source, /_focusTabCoordinator\.withStartLock\(/);
  assert.match(source, /showMirroredFocusSnapshot\(snapshot\)/);
  assert.match(source, /getSettlement\(_focusTimerState\.sessionId\)/);
});

test("missing Web Locks fails closed before any start request can be made", async () => {
  const coordinator = new FocusTabCoordinator("alice", {
    storage: new MemoryStorage(),
    lockManager: null,
    tabId: "tab-a",
  });
  coordinator.lockManager = null;
  await assert.rejects(coordinator.withStartLock(async () => assert.fail("must not execute without a lock")), /不支持同源多标签启动互斥/);
});

test("todo/doing task objects preserve their own ID and estimated duration through startSuggestedFocus", async () => {
  const taskCases = [
    { id: 527, title: "待办短任务", status: "todo", estimated_minutes: 23 },
    { id: 528, title: "进行中任务", status: "doing", estimated_minutes: 38 },
    { task_id: 529, title: "建议任务回退", status: "doing", estimated_minutes: 17 },
  ];

  for (const task of taskCases) {
    const requests = [];
    const fixture = createFocusStartRetryFixture((path, options) => {
      requests.push({ path, options });
      return Promise.resolve({
        session_id: `focus-session-task-${task.id ?? task.task_id}`,
        started_at: "2026-10-06T10:20:00Z",
      });
    });

    await fixture.focus.startSuggestedFocus(task);

    const expectedTaskId = task.task_id ?? task.id;
    assert.equal(requests.length, 1);
    assert.equal(requests[0].path, "/api/focus/start");
    assert.equal(requests[0].options.method, "POST");
    assert.deepEqual(JSON.parse(requests[0].options.body), {
      task_id: expectedTaskId,
      duration_minutes: task.estimated_minutes,
    });
    assert.equal(fixture.focus.state.taskId, expectedTaskId);
    assert.equal(fixture.focus.state.durationMinutes, task.estimated_minutes);
    assert.equal(fixture.focus.state.remainingSeconds, task.estimated_minutes * 60);
    assert.equal(fixture.focus.state.phase, "running");
    assert.equal(fixture.elements.get("focusCurrentTask").textContent, task.title);
  }
});


test("the explicit selected-task estimate entry posts the selected task ID and exact estimate, including range edges", async () => {
  const cases = [
    { id: 527, title: "选中任务 A", estimated_minutes: 37 },
    { id: 528, title: "最短估时", estimated_minutes: 1 },
    { id: 529, title: "最长估时", estimated_minutes: 120 },
  ];

  for (const task of cases) {
    const requests = [];
    const fixture = createFocusStartRetryFixture((path, options) => {
      requests.push({ path, options });
      return Promise.resolve({
        session_id: `focus-session-estimate-${task.id}`,
        started_at: "2026-10-07T08:00:00Z",
      });
    });
    fixture.elements.set("focusEstimatedStartBtn", { disabled: true, dataset: {} });
    fixture.elements.set("focusEstimatedStartHelp", { textContent: "" });
    const select = fixture.elements.get("focusTaskSelect");
    select.value = String(task.id);
    select.selectedIndex = 0;
    select.options = [{
      value: String(task.id),
      textContent: `${task.title}（待办 · #${task.id} · ${task.estimated_minutes} 分钟）`,
      dataset: { taskTitle: task.title, estimatedMinutes: String(task.estimated_minutes) },
    }];
    fixture.focus.state.durationMinutes = 45;

    fixture.focus.updateEstimatedFocusStartAvailability();
    assert.equal(fixture.elements.get("focusEstimatedStartBtn").disabled, false);
    await fixture.focus.focusStartSelectedEstimate();

    assert.equal(requests.length, 1);
    assert.equal(requests[0].path, "/api/focus/start");
    assert.equal(requests[0].options.method, "POST");
    assert.deepEqual(JSON.parse(requests[0].options.body), {
      task_id: task.id,
      duration_minutes: task.estimated_minutes,
    });
    assert.equal(fixture.focus.state.taskId, task.id);
    assert.equal(fixture.focus.state.durationMinutes, task.estimated_minutes);
    assert.equal(fixture.focus.state.taskTitle, task.title);
    assert.equal(fixture.persistedSnapshots[0].task_title, task.title);
    assert.equal(fixture.recoveryStore.load().task_title, task.title);
  }
});

test("missing or invalid selected-task estimates disable the estimate entry and never issue a start request", async () => {
  const invalidEstimates = [
    { label: "missing", value: undefined },
    { label: "zero", value: 0 },
    { label: "negative", value: -1 },
    { label: "fractional", value: 1.5 },
    { label: "above maximum", value: 121 },
    { label: "unsafe integer", value: Number.MAX_SAFE_INTEGER + 1 },
  ];

  for (const { label, value } of invalidEstimates) {
    const requests = [];
    const fixture = createFocusStartRetryFixture((path, options) => {
      requests.push({ path, options });
      return Promise.resolve({ session_id: "should-not-start", started_at: "2026-10-07T08:00:00Z" });
    });
    fixture.elements.set("focusEstimatedStartBtn", { disabled: true, dataset: {} });
    fixture.elements.set("focusEstimatedStartHelp", { textContent: "" });
    const dataset = {};
    if (value !== undefined) dataset.estimatedMinutes = String(value);
    const select = fixture.elements.get("focusTaskSelect");
    select.value = "541";
    select.selectedIndex = 0;
    select.options = [{ value: "541", textContent: "无效估时任务", dataset }];

    fixture.focus.updateEstimatedFocusStartAvailability();
    assert.equal(fixture.elements.get("focusEstimatedStartBtn").disabled, true, `${label} estimate disables the explicit button`);
    assert.match(fixture.elements.get("focusEstimatedStartHelp").textContent, /1–120/);
    await fixture.focus.focusStartSelectedEstimate();

    assert.equal(requests.length, 0, `${label} estimate must not silently fall back to the manual duration`);
    assert.equal(fixture.focus.state.phase, "idle");
  }
});

test("ordinary 25/45/60 minute starts retain their manual-duration behavior", async () => {
  const html = await readFile(new URL("../../src/momentum_agent/static/index.html", import.meta.url), "utf8");
  assert.match(html, /id="focusEstimatedStartBtn"[^>]*>按任务估时开始/);
  assert.match(html, /id="focusEstimatedStartBtn"[^>]*disabled/);
  assert.match(html, /id="focusEstimatedStartHelp"[^>]*role="status"/);
  assert.match(focusControllerSource, /focusEstimatedStartBtn"\)\.addEventListener\("click", \(\) => \{ void focusStartSelectedEstimate\(\); \}\)/);
  assert.match(focusControllerSource, /focusTaskSelect"\)\.addEventListener\("change", updateEstimatedFocusStartAvailability\)/);
  assert.match(html, /data-min="25">25 分钟/);
  assert.match(html, /data-min="45">45 分钟/);
  assert.match(html, /data-min="60">60 分钟/);
  assert.match(focusControllerSource, /focusStartBtn"\)\.addEventListener\("click", \(\) => \{ void focusStart\(\); \}\)/);

  for (const duration of [25, 45, 60]) {
    const requests = [];
    const fixture = createFocusStartRetryFixture((path, options) => {
      requests.push({ path, options });
      return Promise.resolve({ session_id: `manual-${duration}`, started_at: "2026-10-07T08:00:00Z" });
    });
    fixture.focus.state.durationMinutes = duration;
    await fixture.focus.focusStart();

    assert.equal(requests.length, 1);
    assert.deepEqual(JSON.parse(requests[0].options.body), { task_id: 41, duration_minutes: duration });
    assert.equal(fixture.focus.state.taskTitle, "测试任务");
    assert.equal(fixture.persistedSnapshots[0].task_title, "测试任务");
    assert.equal(fixture.recoveryStore.load().task_title, "测试任务");
  }
});

test("rest break shows its task and does not start another session until its explicit button is clicked", async () => {
  const html = await readFile(new URL("../../src/momentum_agent/static/index.html", import.meta.url), "utf8");
  assert.match(html, /id="focusBreakTask"/);
  assert.match(html, /id="focusBreakStartBtn"/);
  assert.match(focusControllerSource, /getElementById\("focusBreakTask"\)\.textContent = _focusTimerState\.taskTitle/);
  assert.match(focusControllerSource, /focusBreakStartBtn"\)\.textContent = `再专注一轮（\$\{formatFocusMinutes\(_focusTimerState\.durationMinutes\)\}分钟）`/);
  assert.match(focusControllerSource, /focusBreakStartBtn"\)\.addEventListener\("click", \(\) => \{ void focusBreakStartNextRound\(\); \}\)/);

  const requests = [];
  const fixture = createFocusStartRetryFixture((path, options) => {
    requests.push({ path, options });
    return Promise.resolve({ session_id: "unused", started_at: "2026-10-07T08:00:00Z" });
  }, {
    initialState: {
      taskId: 41,
      taskTitle: "当前任务",
      durationMinutes: 37,
      phase: "break",
      breakSeconds: 120,
      sessionId: "old-focus-session",
    },
  });

  assert.equal(requests.length, 0, "an idle break does not POST a session automatically");
  assert.equal(fixture.focus.state.phase, "break");
});

test("one explicit break click posts the same task and estimate as a fresh session; a double click posts once", async () => {
  const requests = [];
  let resolveStartRequest;
  let markRequestStarted;
  const requestStarted = new Promise((resolve) => { markRequestStarted = resolve; });
  const fixture = createFocusStartRetryFixture((path, options) => {
    requests.push({ path, options });
    const pending = new Promise((resolve) => { resolveStartRequest = resolve; });
    markRequestStarted();
    return pending;
  }, {
    initialState: {
      taskId: 41,
      taskTitle: "当前任务",
      durationMinutes: 37,
      phase: "break",
      breakSeconds: 120,
      sessionId: "old-focus-session",
    },
  });

  const firstClick = fixture.focus.focusBreakStartNextRound();
  assert.deepEqual(fixture.timers.cleared, [777], "the break countdown is cleared only after the explicit click");
  assert.equal(fixture.elements.get("focusBreakCountdown").textContent, "00:00");
  await requestStarted;
  const secondClick = fixture.focus.focusBreakStartNextRound();
  await secondClick;

  assert.equal(requests.length, 1, "the pending gate suppresses a second click");
  assert.equal(requests[0].path, "/api/focus/start");
  assert.equal(requests[0].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[0].options.body), { task_id: 41, duration_minutes: 37 });

  resolveStartRequest({ session_id: "fresh-focus-session", started_at: "2026-10-07T08:05:00Z" });
  await firstClick;
  assert.equal(fixture.focus.state.phase, "running");
  assert.equal(fixture.focus.state.taskId, 41);
  assert.equal(fixture.focus.state.taskTitle, "当前任务");
  assert.equal(fixture.focus.state.durationMinutes, 37);
  assert.equal(fixture.focus.state.sessionId, "fresh-focus-session");
  assert.notEqual(fixture.focus.state.sessionId, "old-focus-session", "the new POST response, not the completed session id, owns the new round");
  assert.equal(fixture.persistedSnapshots.length, 1);
  assert.equal(fixture.persistedSnapshots[0].session_id, "fresh-focus-session");
});

test("a definite 4xx on a break restart stays in break, preserves the old session, and shows the existing error", async () => {
  const requests = [];
  const fixture = createFocusStartRetryFixture((path, options) => {
    requests.push({ path, options });
    const error = new Error("task is closed");
    error.status = 409;
    return Promise.reject(error);
  }, {
    initialState: {
      taskId: 41,
      taskTitle: "已完成任务",
      durationMinutes: 25,
      phase: "break",
      breakSeconds: 60,
      sessionId: "old-focus-session",
    },
  });

  await fixture.focus.focusBreakStartNextRound();

  assert.equal(requests.length, 1, "a rejected request is not retried automatically");
  assert.equal(fixture.focus.state.phase, "break");
  assert.equal(fixture.focus.state.sessionId, "old-focus-session");
  assert.equal(fixture.elements.get("focusBreakStartError").hidden, false);
  assert.equal(fixture.elements.get("focusBreakStartError").textContent, "启动失败（409）：该任务已关闭，未开始专注。 task is closed");
  assert.equal(fixture.elements.get("focusBreakStartBtn").disabled, false);
  assert.deepEqual(fixture.persistedSnapshots, []);
});

test("a statusless break-start timeout stays visible without automatic resend or a fake new session", async () => {
  const requests = [];
  const fixture = createFocusStartRetryFixture((path, options) => {
    requests.push({ path, options });
    return Promise.reject(new Error("request timed out"));
  }, {
    initialState: {
      taskId: 41,
      taskTitle: "当前任务",
      durationMinutes: 25,
      phase: "break",
      breakSeconds: 60,
      sessionId: "old-focus-session",
    },
  });

  await fixture.focus.focusBreakStartNextRound();

  assert.equal(requests.length, 1, "an unknown result never causes an automatic resend");
  assert.equal(fixture.focus.state.phase, "break");
  assert.equal(fixture.focus.state.sessionId, "old-focus-session");
  assert.equal(fixture.elements.get("focusBreakStartError").hidden, false);
  assert.equal(fixture.elements.get("focusBreakStartError").textContent, "无法确认开始；再次点击将从新时间重新开始");
  assert.equal(fixture.elements.get("focusBreakStartBtn").disabled, false);
  assert.deepEqual(fixture.persistedSnapshots, []);
});

test("focus selector concurrently loads todo then doing, filters statuses and invalid IDs, deduplicates, and preserves an open selection", async () => {
  const requestCalls = [];
  const resolvers = new Map();
  const fixture = createFocusTaskSelectFixture((url) => {
    requestCalls.push(url);
    return new Promise((resolve) => resolvers.set(url, resolve));
  }, { selectedValue: "12" });

  const pending = fixture.focus.populateFocusTaskSelect();
  assert.deepEqual(requestCalls, ["/api/tasks?status=todo", "/api/tasks?status=doing"]);
  assert.equal(requestCalls.length, 2, "both status requests start before either response resolves");
  assert.equal(fixture.select.disabled, true, "the selector stays disabled while its complete list is loading");
  const html = await readFile(new URL("../../src/momentum_agent/static/index.html", import.meta.url), "utf8");
  assert.match(html, /id="focusTaskLoadError"[^>]*role="alert"[^>]*aria-live="assertive"/);

  resolvers.get("/api/tasks?status=todo")({ tasks: [
    { id: "11", status: "todo", title: "待办甲", estimated_minutes: 25 },
    { id: 11, status: "todo", title: "同状态重复 ID" },
    { id: "12", status: "todo", title: "仍开放的选择", estimated_minutes: 45 },
    { id: 13, status: "doing", title: "状态与 todo 响应不符" },
    { id: "not-an-id", status: "todo", title: "非法字符串 ID" },
    { id: "1e3", status: "todo", title: "非十进制 ID" },
    { id: "0x10", status: "todo", title: "非十进制 ID" },
    { id: 0, status: "todo", title: "零 ID" },
    { id: -1, status: "todo", title: "负数 ID" },
    { id: 1.5, status: "todo", title: "小数 ID" },
    { id: 9007199254740992, status: "todo", title: "不安全整数 ID" },
  ] });
  resolvers.get("/api/tasks?status=doing")({ tasks: [
    { id: 12, status: "doing", title: "跨状态重复 ID" },
    { id: "14", status: "doing", title: "进行中乙", estimated_minutes: 60 },
    { id: 15, status: "todo", title: "状态与 doing 响应不符" },
    { id: null, status: "doing", title: "空 ID" },
  ] });
  assert.equal(fixture.select.options.length, 1, "a successful first response is not shown before the second succeeds");
  assert.equal(fixture.select.disabled, true);
  await pending;

  assert.deepEqual(fixture.select.options.slice(1).map(({ value, textContent }) => [value, textContent]), [
    ["11", "待办甲（待办 · #11 · 25 分钟）"],
    ["12", "仍开放的选择（待办 · #12 · 45 分钟）"],
    ["14", "进行中乙（进行中 · #14 · 60 分钟）"],
  ]);
  assert.deepEqual(fixture.select.options.slice(1).map(({ dataset }) => dataset.taskTitle), [
    "待办甲",
    "仍开放的选择",
    "进行中乙",
  ]);
  assert.equal(fixture.select.value, "12", "an existing selection is retained while its task remains open");
  assert.equal(fixture.select.selectedIndex, 2);
  assert.equal(fixture.select.disabled, false);
  assert.equal(fixture.select.options[1].dataset.estimatedMinutes, "25");
  assert.equal(fixture.select.options[2].dataset.estimatedMinutes, "45");
  assert.equal(fixture.select.options[3].dataset.estimatedMinutes, "60");
  assert.equal(fixture.loadError.hidden, true);
  assert.equal(fixture.availabilityUpdates(), 1);
});

test("focus selector clears a selection whose task is no longer open", async () => {
  const requestCalls = [];
  const fixture = createFocusTaskSelectFixture((url) => {
    requestCalls.push(url);
    return Promise.resolve({ tasks: [] });
  }, { selectedValue: "41" });

  await fixture.focus.populateFocusTaskSelect();

  assert.deepEqual(requestCalls, ["/api/tasks?status=todo", "/api/tasks?status=doing"]);
  assert.equal(requestCalls.length, 2);
  assert.equal(fixture.select.value, "");
  assert.equal(fixture.select.selectedIndex, 0);
  assert.equal(fixture.select.disabled, true);
  assert.equal(fixture.select.options.length, 1);
  assert.equal(fixture.select.options[0].textContent, "暂无可专注任务");
  assert.equal(fixture.loadError.hidden, true);
});

test("focus selector never exposes a partial list when either status request fails", async () => {
  for (const failedStatus of ["todo", "doing"]) {
    const requestCalls = [];
    const fixture = createFocusTaskSelectFixture((url) => {
      requestCalls.push(url);
      if (url === `/api/tasks?status=${failedStatus}`) return Promise.reject(new Error(`${failedStatus} 请求失败`));
      return Promise.resolve({ tasks: [{ id: 71, status: failedStatus === "todo" ? "doing" : "todo", title: "不应泄漏的半份任务" }] });
    }, { selectedValue: "71" });

    await fixture.focus.populateFocusTaskSelect();

    assert.deepEqual(requestCalls, ["/api/tasks?status=todo", "/api/tasks?status=doing"]);
    assert.equal(requestCalls.length, 2, `${failedStatus} failure still starts both GETs`);
    assert.equal(fixture.select.options.length, 1, "failed loading leaves only an empty placeholder");
    assert.equal(fixture.select.options[0].value, "");
    assert.equal(fixture.select.value, "");
    assert.equal(fixture.select.disabled, true);
    assert.equal(fixture.loadError.hidden, false);
    assert.match(fixture.loadError.textContent, /无法加载可专注任务.*刷新重试/);
    assert.equal(fixture.availabilityUpdates(), 1);
  }
});

test("focus task title search filters loaded todo and doing options locally and preserves only a matching selection", async () => {
  const requestCalls = [];
  const fixture = createFocusTaskSelectFixture((url) => {
    requestCalls.push(url);
    if (url.endsWith("status=todo")) {
      return Promise.resolve({ tasks: [
        { id: 11, status: "todo", title: "Draft project plan" },
        { id: 12, status: "todo", title: "Alpha checklist" },
      ] });
    }
    return Promise.resolve({ tasks: [
      { id: 14, status: "doing", title: "Prepare report" },
    ] });
  }, { selectedValue: "12" });

  await fixture.focus.populateFocusTaskSelect();
  assert.deepEqual(fixture.select.options.slice(1).map(({ value }) => value), ["11", "12", "14"]);
  assert.equal(fixture.select.value, "12");

  fixture.searchInput.value = "PHA";
  fixture.searchInput.oninput();
  assert.deepEqual(fixture.select.options.slice(1).map(({ value }) => value), ["12"], "uppercase substring search is case-insensitive");
  assert.equal(fixture.select.value, "12", "a selected task remains selected while it matches");

  fixture.searchInput.value = "JECT";
  fixture.searchInput.oninput();
  assert.deepEqual(fixture.select.options.slice(1).map(({ value }) => value), ["11"], "a partial title substring filters across the cached list");
  assert.equal(fixture.select.value, "", "a selection is cleared once it no longer matches");

  fixture.searchInput.value = "";
  fixture.searchInput.oninput();
  assert.deepEqual(fixture.select.options.slice(1).map(({ value }) => value), ["11", "12", "14"], "clearing the query restores all loaded todo and doing tasks");
  assert.equal(fixture.select.value, "", "clearing the query does not restore a previously cleared selection");
  assert.deepEqual(requestCalls, ["/api/tasks?status=todo", "/api/tasks?status=doing"], "typing and clearing search issue no additional GETs");

  const html = await readFile(new URL("../../src/momentum_agent/static/index.html", import.meta.url), "utf8");
  assert.match(html, /<label class="focus-task-search-label" for="focusTaskSearch">按标题搜索<\/label>/);
  assert.match(html, /id="focusTaskSearch" class="focus-task-search" type="search"[^>]*aria-controls="focusTaskSelect"/);
  assert.ok(html.indexOf("id=\"focusTaskSearch\"") < html.indexOf("id=\"focusTaskSelect\""), "the accessible search field appears above the task selector");
});

test("focus selector distinguishes same-title tasks by status and ID while searching only original titles", async () => {
  const fixture = createFocusTaskSelectFixture((url) => Promise.resolve({
    tasks: url.endsWith("status=todo")
      ? [{ id: 231, status: "todo", title: "整理项目计划", estimated_minutes: 30 }]
      : [{ id: 842, status: "doing", title: "整理项目计划" }],
  }));

  await fixture.focus.populateFocusTaskSelect();

  const options = fixture.select.options.slice(1);
  assert.equal(options.length, 2);
  assert.notEqual(options[0].textContent, options[1].textContent, "duplicate titles have distinguishable labels");
  assert.match(options[0].textContent, /待办 · #231 · 30 分钟/);
  assert.match(options[1].textContent, /进行中 · #842 · 未估时/);
  assert.deepEqual(options.map(({ dataset }) => dataset.taskTitle), ["整理项目计划", "整理项目计划"]);

  fixture.searchInput.value = "整理项目计划";
  fixture.searchInput.oninput();
  assert.deepEqual(fixture.select.options.slice(1).map(({ value }) => value), ["231", "842"]);
  fixture.searchInput.value = "进行中";
  fixture.searchInput.oninput();
  assert.equal(fixture.select.options.length, 1, "status decoration does not change title-only search semantics");
});


test("focus stats renders only the three most recent sessions safely from one stats response", async () => {
  const requestCalls = [];
  const response = {
    total_minutes_today: 12,
    total_minutes_week: 145.5,
    total_sessions_week: 7,
    legacy_sessions_week: 1,
    sessions: [
      {
        task_id: 25,
        started_at: "2026-01-10T08:00:00.000Z",
        ended_at: "2026-01-15T08:30:00.000Z",
        planned_minutes: 25,
        actual_seconds: 1800,
        is_actual: true,
        outcome: "stopped",
        session_id: "private-session-newest",
      },
      {
        task_id: 24,
        started_at: "2026-01-13T09:00:00.000Z",
        ended_at: null,
        planned_minutes: 30,
        actual_seconds: null,
        is_actual: false,
        outcome: "imported-legacy",
        session_id: "private-session-legacy",
      },
      {
        task_id: "<img src=x>",
        started_at: "2026-01-20T10:00:00.000Z",
        ended_at: "2026-01-12T10:05:00.000Z",
        planned_minutes: 25,
        actual_seconds: 0,
        is_actual: true,
        outcome: "completed",
        session_id: "private-session-zero",
      },
      {
        task_id: 99,
        started_at: "2026-01-11T08:00:00.000Z",
        ended_at: "2026-01-11T08:01:00.000Z",
        planned_minutes: 25,
        actual_seconds: 60,
        is_actual: true,
        outcome: "completed",
        session_id: "private-session-older",
      },
    ],
  };
  const fixture = createFocusStatsFixture(async (path) => {
    requestCalls.push(path);
    return response;
  });

  await fixture.focus.loadFocusStats();

  assert.deepEqual(requestCalls, ["/api/focus/stats"], "rendering makes one stats request and no task/title request");
  const list = fixture.elements.get("focusSessionHistoryList");
  assert.equal(list.children.length, 3, "the list is capped at three entries");
  const rows = list.children;
  assert.deepEqual(rows.map((row) => row.children[0].children[0].textContent), [
    "任务 #25",
    "任务 #24",
    "任务 #<img src=x>",
  ]);
  assert.deepEqual(rows.map((row) => row.children[0].children[1].textContent), ["已停止", "状态未知", "已完成"]);
  assert.equal(rows[1].children[1].children[1].textContent, "实际时长未知 · 仅计划时长 30 分钟");
  assert.equal(rows[2].children[1].children[1].textContent, "实际时长 0 秒", "zero seconds is a valid actual duration");
  assert.equal(
    rows[2].children[1].children[0].textContent,
    new Date("2026-01-12T10:05:00.000Z").toLocaleString(),
    "timestamps are localized in the browser's local timezone",
  );
  const renderedText = `${fixture.elements.get("focusStats").textContent}${list.textContent}`;
  assert.doesNotMatch(renderedText, /private-session-/i, "session identifiers are never rendered");
  assert.match(fixture.elements.get("focusStats").textContent, /12m/);
  assert.match(fixture.elements.get("focusStats").textContent, /近7天/);
  assert.doesNotMatch(fixture.elements.get("focusStats").textContent, /本周/);
  assert.match(fixture.elements.get("focusStats").textContent, /145\.5m/);
  const summary = fixture.elements.get("focusStats");
  assert.equal(summary.children[2].children[0].textContent, "近7天实际次数");
  assert.equal(summary.children[2].children[1].textContent, "7", "only the seven actual sessions count toward this metric");
  assert.ok(summary.children.some((child) => child.textContent === "Focus今日时长按开始时的本地日期归属；每日复盘按结束时的本地日期归属。"));
  assert.equal(summary.children.find((child) => child.textContent.startsWith("另有"))?.textContent, "另有 1 条旧记录仅含计划时长，未计入实际统计。");
  assert.equal(fixture.elements.get("focusSessionHistoryMessage").dataset.state, "ready");
});

test("focus stats distinguishes a successful empty history from a failed request", async () => {
  const emptyCalls = [];
  const emptyFixture = createFocusStatsFixture(async (path) => {
    emptyCalls.push(path);
    return { total_minutes_today: 0, total_minutes_week: 0, total_sessions_week: 0, legacy_sessions_week: 0, sessions: [] };
  });
  await emptyFixture.focus.loadFocusStats();
  const emptyMessage = emptyFixture.elements.get("focusSessionHistoryMessage");
  assert.deepEqual(emptyCalls, ["/api/focus/stats"]);
  assert.equal(emptyMessage.dataset.state, "empty");
  assert.equal(emptyMessage.textContent, "暂无专注记录");

  const failedCalls = [];
  const failedFixture = createFocusStatsFixture(async (path) => {
    failedCalls.push(path);
    throw new Error("request failure detail must not be injected into the page");
  });
  await failedFixture.focus.loadFocusStats();
  const failedMessage = failedFixture.elements.get("focusSessionHistoryMessage");
  assert.deepEqual(failedCalls, ["/api/focus/stats"]);
  assert.equal(failedMessage.dataset.state, "error");
  assert.equal(failedMessage.textContent, "无法加载专注记录，请检查网络连接后重试。");
  assert.notEqual(failedMessage.dataset.state, "empty", "request failure is not presented as an empty history");
  assert.equal(failedFixture.elements.get("focusSessionHistoryList").children.length, 0);
});

test("focus stats clears stale session rows when a refresh fails after a successful load", async () => {
  let requestCount = 0;
  const fixture = createFocusStatsFixture(async () => {
    requestCount += 1;
    if (requestCount === 1) {
      return {
        total_minutes_today: 1,
        total_minutes_week: 1,
        total_sessions_week: 1,
        legacy_sessions_week: 0,
        sessions: [{
          task_id: 7,
          started_at: "2026-10-07T08:00:00Z",
          ended_at: "2026-10-07T08:01:00Z",
          actual_seconds: 60,
          is_actual: true,
          outcome: "completed",
          session_id: "not-rendered-session-id",
        }],
      };
    }
    throw new Error("refresh failed");
  });

  await fixture.focus.loadFocusStats();
  const list = fixture.elements.get("focusSessionHistoryList");
  const message = fixture.elements.get("focusSessionHistoryMessage");
  assert.equal(list.children.length, 1);
  assert.equal(message.dataset.state, "ready");

  await fixture.focus.loadFocusStats();

  assert.equal(requestCount, 2);
  assert.equal(list.children.length, 0, "the previous successful rows do not remain visible after refresh failure");
  assert.equal(message.dataset.state, "error");
  assert.equal(message.textContent, "无法加载专注记录，请检查网络连接后重试。");
});
