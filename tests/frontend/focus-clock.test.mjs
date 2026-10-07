import assert from "node:assert/strict";
import test from "node:test";

import { FocusClock, remainingFocusSeconds } from "../../src/momentum_agent/static/js/focus-clock.mjs";

test("FocusClock accumulates running segments and excludes pauses", () => {
  let now = 0;
  const clock = new FocusClock(() => now);

  clock.start();
  now = 12_400;
  assert.equal(clock.elapsedSeconds(), 12);

  clock.pause();
  now += 60_000;
  assert.equal(clock.elapsedSeconds(), 12);

  clock.resume();
  now += 3_700;
  assert.equal(clock.elapsedSeconds(), 16);

  clock.pause();
  now += 25_000;
  assert.equal(clock.elapsedSeconds(), 16);
});

test("FocusClock uses monotonic elapsed time across wall-clock changes", () => {
  let monotonic = 4_000;
  const clock = new FocusClock(() => monotonic);
  clock.start();
  monotonic += 1_999;
  assert.equal(clock.elapsedSeconds(), 1);
  assert.equal(clock.elapsedMilliseconds(), 1_999);
});

test("restored focus starts a fresh segment and discards long unobserved gaps", () => {
  let now = 10_000;
  const clock = new FocusClock(() => now);
  clock.start(31, 1_500);
  now += 900;
  assert.equal(clock.elapsedSeconds(), 31);
  clock.pause();
  now += 3_600_000;
  assert.equal(clock.elapsedSeconds(), 31, "paused time is never included");
  clock.resume();
  now += 4_000;
  assert.equal(clock.elapsedSeconds(), 31, "an unobserved scheduler gap is dropped rather than caught up");
  now += 1_000;
  assert.equal(clock.elapsedSeconds(), 32, "a new live segment resumes accumulating normally");
});

test("start → pause → resume → stop records only active seconds and stopped outcome", () => {
  let now = 0;
  const clock = new FocusClock(() => now);

  clock.start();
  now = 2_600;
  clock.pause();
  now += 60_000;
  clock.resume();
  now += 3_800;

  const result = clock.finish(25 * 60);
  assert.deepEqual(result, { actual_seconds: 6, outcome: "stopped" });

  now += 10_000;
  assert.equal(clock.elapsedSeconds(), 6, "stopped sessions must no longer accrue time");
});

test("finish after reaching the planned active duration records completed", () => {
  let now = 0;
  const clock = new FocusClock(() => now);
  clock.start();
  now = 3_000;
  clock.pause();
  now += 20_000;
  clock.resume();
  now += 2_000;

  assert.deepEqual(clock.finish(5), { actual_seconds: 5, outcome: "completed" });
});

test("remaining focus time is floored and never negative", () => {
  assert.equal(remainingFocusSeconds(1_500, 12), 1_488);
  assert.equal(remainingFocusSeconds(1_500, 1_600), 0);
});
