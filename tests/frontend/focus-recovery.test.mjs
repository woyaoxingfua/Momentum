import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

import { FocusFinishCoordinator, FocusRecoveryStore, FOCUS_SNAPSHOT_VERSION, focusRecoveryMode, focusSettledKey, focusSnapshotKey } from "../../src/momentum_agent/static/js/focus-recovery.mjs";

class MemoryStorage {
  values = new Map();
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

function snapshot(userId = "user-a", overrides = {}) {
  return {
    version: FOCUS_SNAPSHOT_VERSION,
    user_id: userId,
    session_id: "session-123",
    task_id: 7,
    planned_minutes: 25,
    elapsed_seconds: 12,
    state: "running",
    started_at: "2026-10-03T15:00:00Z",
    task_title: "写测试",
    ...overrides,
  };
}

test("focus snapshots are isolated by authenticated user ID", () => {
  const storage = new MemoryStorage();
  const alice = new FocusRecoveryStore("alice", storage);
  const bob = new FocusRecoveryStore("bob", storage);
  assert.notEqual(focusSnapshotKey("alice"), focusSnapshotKey("bob"));
  assert.equal(alice.save(snapshot("alice")), true);
  assert.equal(bob.load(), null);
  assert.equal(alice.load().session_id, "session-123");

  storage.setItem(bob.key, JSON.stringify(snapshot("alice")));
  assert.equal(bob.load(), null, "a mismatched embedded user ID is rejected");
  assert.equal(storage.getItem(bob.key), null, "the foreign/malformed entry is discarded");
  assert.equal(alice.load().user_id, "alice", "reading as another user never deletes or returns Alice's session");
});

test("corrupt and unknown-version snapshots are ignored without breaking loading", () => {
  const storage = new MemoryStorage();
  const store = new FocusRecoveryStore("user-a", storage);
  storage.setItem(store.key, "{not valid JSON");
  assert.doesNotThrow(() => assert.equal(store.load(), null));
  assert.equal(storage.getItem(store.key), null);

  storage.setItem(store.key, JSON.stringify(snapshot("user-a", { version: 99 })));
  assert.doesNotThrow(() => assert.equal(store.load(), null));
  assert.equal(storage.getItem(store.key), null);
});

test("a reloaded session requires a choice; an already-fixed finish offers retry only", () => {
  const restored = snapshot();
  assert.equal(focusRecoveryMode(restored), "choose");
  const pending = {
    ...restored,
    state: "paused",
    finish_payload: {
      task_id: restored.task_id,
      session_id: restored.session_id,
      started_at: restored.started_at,
      planned_minutes: restored.planned_minutes,
      actual_seconds: restored.elapsed_seconds,
      outcome: "stopped",
      ended_at: "2026-10-03T15:00:12.000Z",
    },
  };
  assert.equal(focusRecoveryMode(pending), "retry-only");
  assert.equal(focusRecoveryMode(null), "none");
});

test("finish payload is persisted before send and an ambiguous failure retries identical bytes", async () => {
  const storage = new MemoryStorage();
  const store = new FocusRecoveryStore("user-a", storage);
  const runningSnapshot = snapshot();
  assert.equal(store.save(runningSnapshot), true);
  const bodies = [];
  const ledger = new Map();
  let attempts = 0;
  let clockCalls = 0;
  const now = () => {
    clockCalls += 1;
    return new Date(clockCalls === 1 ? "2026-10-03T15:00:12.000Z" : "2026-10-03T15:05:00.000Z");
  };
  const simulatedRequest = async (url, options) => {
    assert.equal(url, "/api/focus/finish");
    const saved = store.load();
    assert.ok(saved?.finish_payload, "fixed finish payload must be stored before the API request");
    const body = options.body;
    assert.deepEqual(JSON.parse(body), saved.finish_payload);
    bodies.push(body);
    attempts += 1;
    const payload = JSON.parse(body);
    if (ledger.has(payload.session_id)) {
      assert.equal(ledger.get(payload.session_id), body, "the backend idempotency key sees identical payload bytes");
    } else {
      ledger.set(payload.session_id, body);
    }
    if (attempts === 1) throw new Error("simulated response loss after server acceptance");
    return { session_id: payload.session_id, actual_seconds: payload.actual_seconds, outcome: payload.outcome };
  };

  const firstPage = new FocusFinishCoordinator(store, simulatedRequest, now);
  await assert.rejects(firstPage.submit(runningSnapshot, 12), /response loss/);
  const persistedAfterFailure = store.load();
  assert.deepEqual(persistedAfterFailure.finish_payload, {
    task_id: 7,
    session_id: "session-123",
    started_at: "2026-10-03T15:00:00Z",
    planned_minutes: 25,
    actual_seconds: 12,
    outcome: "stopped",
    ended_at: "2026-10-03T15:00:12.000Z",
  });
  assert.equal(focusRecoveryMode(persistedAfterFailure), "retry-only");

  const afterRefresh = new FocusFinishCoordinator(store, simulatedRequest, now);
  const result = await afterRefresh.submit(persistedAfterFailure, 900);
  assert.deepEqual(result, { actual_seconds: 12, outcome: "stopped", response: {
    session_id: "session-123", actual_seconds: 12, outcome: "stopped",
  }, snapshotSettled: true });
  assert.equal(bodies.length, 2);
  assert.equal(bodies[0], bodies[1], "retry must send the exact same serialized payload");
  assert.equal(JSON.parse(bodies[0]).ended_at, "2026-10-03T15:00:12.000Z");
  assert.equal(clockCalls, 1, "retry after refresh must not capture a new ended_at");
  assert.equal(ledger.size, 1, "idempotent server accounting remains one session");
  assert.equal(store.load(), null, "success clears the saved recovery snapshot");

  await afterRefresh.submit(persistedAfterFailure, 1234);
  assert.equal(attempts, 2, "a successful local session cannot be submitted again");
  assert.equal(ledger.size, 1);
});

test("same-owner retry success settles its storage key across reload and ignores stale SW writes", async () => {
  const storage = new MemoryStorage();
  const store = new FocusRecoveryStore("user-a", storage);
  const base = snapshot("user-a", { state: "paused", elapsed_seconds: 18 });
  const pending = {
    ...base,
    finish_payload: {
      task_id: base.task_id,
      session_id: base.session_id,
      started_at: base.started_at,
      planned_minutes: base.planned_minutes,
      actual_seconds: 18,
      outcome: "stopped",
      ended_at: "2026-10-03T15:00:18.000Z",
    },
  };
  assert.equal(store.save(pending), true);
  const originalOwnerKey = store.key;
  const bodies = [];
  const request = async (_url, options) => {
    bodies.push(options.body);
    if (bodies.length === 1) throw new Error("response lost after acceptance");
    const payload = JSON.parse(options.body);
    return { session_id: payload.session_id, actual_seconds: payload.actual_seconds, outcome: payload.outcome };
  };

  await assert.rejects(new FocusFinishCoordinator(store, request).submit(pending, 18), /response lost/);
  assert.equal(storage.getItem(focusSettledKey("user-a", pending.session_id)), null,
    "an unknown network outcome must not be persisted as settled");
  const retryPageStore = new FocusRecoveryStore("user-a", storage);
  assert.equal(retryPageStore.key, originalOwnerKey, "same owner reload resolves the original storage key");
  const retrySnapshot = retryPageStore.load();
  assert.equal(retrySnapshot.finish_payload.actual_seconds, 18);
  const retryResult = await new FocusFinishCoordinator(retryPageStore, request).submit(retrySnapshot, 999);
  assert.equal(retryResult.snapshotSettled, true, "only a successful response writes the durable settlement marker");
  assert.ok(storage.getItem(focusSettledKey("user-a", pending.session_id)),
    "the successful retry leaves a cross-reload per-session marker");
  assert.deepEqual(retryPageStore.getSettlement(pending.session_id)?.finish_payload, pending.finish_payload,
    "a peer tab can read the validated finish outcome from the durable marker");
  assert.equal(bodies[0], bodies[1], "the success retry preserves the exact original finish payload");
  assert.equal(storage.getItem(originalOwnerKey), null, "the exact owner snapshot key is cleared after settlement");

  // An older cached page/SW can still write its v1 key, but the durable per-session marker fences it.
  storage.setItem(store.legacyKey, JSON.stringify({ ...pending, version: 1 }));
  const reloadedStore = new FocusRecoveryStore("user-a", storage);
  const restored = reloadedStore.load();
  assert.equal(restored, null, "reload must not restore the settled 18-second recovery card");
  if (restored) await new FocusFinishCoordinator(reloadedStore, request).submit(restored, 18);
  assert.equal(bodies.length, 2, "reload does not send finish again after confirmed success");
  assert.equal(storage.getItem(store.legacyKey), null, "the stale same-session legacy write is discarded");

  const focusJs = await readFile(new URL("../../src/momentum_agent/static/js/focus.js", import.meta.url), "utf8");
  const successStart = focusJs.indexOf("function handleStoppedFocusSuccess");
  const successEnd = focusJs.indexOf("async function populateFocusTaskSelect", successStart);
  assert.ok(successStart >= 0 && successEnd > successStart);
  const successHandler = focusJs.slice(successStart, successEnd);
  assert.doesNotMatch(successHandler, /clearFocusRecoverySnapshot/,
    "UI success handling must not perform an unscoped second deletion");
  assert.match(successHandler, /result\?\.snapshotSettled === false/,
    "the UI reports when the durable settlement marker could not be stored");

  const sw = await readFile(new URL("../../src/momentum_agent/static/sw.js", import.meta.url), "utf8");
  assert.match(sw, /const CACHE_NAME = "momentum-v7";/,
    "reload must install the recovery code that fences legacy service-worker writes");
});

test("settling one finish never clears a newer session for the same owner", async () => {
  const storage = new MemoryStorage();
  const store = new FocusRecoveryStore("user-a", storage);
  const base = snapshot("user-a", { state: "paused" });
  const pending = {
    ...base,
    finish_payload: {
      task_id: base.task_id,
      session_id: base.session_id,
      started_at: base.started_at,
      planned_minutes: base.planned_minutes,
      actual_seconds: 18,
      outcome: "stopped",
      ended_at: "2026-10-03T15:00:18.000Z",
    },
  };
  const nextSession = snapshot("user-a", { session_id: "session-next", elapsed_seconds: 3 });
  const request = async () => {
    assert.equal(store.save(nextSession), true, "simulate another session taking the same owner slot before the response arrives");
    return { session_id: pending.session_id, actual_seconds: 18, outcome: "stopped" };
  };

  const result = await new FocusFinishCoordinator(store, request).submit(pending, 18);
  assert.equal(result.snapshotSettled, true);
  assert.equal(new FocusRecoveryStore("user-a", storage).load().session_id, "session-next");
});

test("recovery controls are present and initialized only through explicit actions", async () => {
  const html = await readFile(new URL("../../src/momentum_agent/static/index.html", import.meta.url), "utf8");
  const focusJs = await readFile(new URL("../../src/momentum_agent/static/js/focus.js", import.meta.url), "utf8");
  assert.match(html, /id="focusRecoveryContinue"[^>]*>按已保存时长继续/);
  assert.match(html, /id="focusRecoveryInterrupt"[^>]*>以已确认时长标记中断/);
  assert.match(focusJs, /_focusTimerState\.phase = "recovery"/);
  assert.match(focusJs, /function focusContinueRecovery\(\)/);
  assert.doesNotMatch(focusJs, /visibilitychange/, "switching tabs must not auto-pause focus");
  assert.match(focusJs, /window\.addEventListener\("pagehide", persistFocusBeforePageHide\)/);
  const pageHideHandler = focusJs.slice(
    focusJs.indexOf("function persistFocusBeforePageHide"),
    focusJs.indexOf("function focusContinueRecovery"),
  );
  assert.match(pageHideHandler, /state: _focusTimerState\.paused \? "paused" : "running"/);
  assert.doesNotMatch(pageHideHandler, /_focusClock\.pause\(/, "pagehide only checkpoints; reload presents recovery choices");
  const focusTick = focusJs.slice(focusJs.indexOf("function focusTick"), focusJs.indexOf("async function saveFocusSession"));
  assert.match(focusTick, /persistFocusSnapshot\(\{ state: "running", elapsedSeconds \}\)/);
});
