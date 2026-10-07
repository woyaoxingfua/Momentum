import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

import { getEstimationAccuracyDisplay } from "../../src/momentum_agent/static/js/estimation-accuracy.mjs";

const statsHtmlUrl = new URL("../../src/momentum_agent/static/stats.html", import.meta.url);
const statsJsUrl = new URL("../../src/momentum_agent/static/js/stats.js", import.meta.url);

test("estimation accuracy with no eligible tasks still shows a zero sample count", () => {
  const display = getEstimationAccuracyDisplay(0, 0);
  assert.deepEqual(display, {
    value: "--",
    detail: "样本任务数：0 · 样本较少，仅供参考 · 按 task_id 聚合，不按单次完成周期拆分；重开后的专注时段仍计入该任务。 · 依据近30天专注记录，最多纳入最近完成的100个任务。 · 使用任务当前估时；完成后改估时会重算历史准确度。",
  });
  assert.match(display.detail, /依据近30天专注记录，最多纳入最近完成的100个任务。/);
  assert.match(display.detail, /使用任务当前估时；完成后改估时会重算历史准确度。/);
});

test("one eligible task shows its metric and the low-sample caution", () => {
  const display = getEstimationAccuracyDisplay(0.6, 1);
  assert.deepEqual(display, {
    value: "60%",
    detail: "样本任务数：1 · 样本较少，仅供参考 · 按 task_id 聚合，不按单次完成周期拆分；重开后的专注时段仍计入该任务。 · 依据近30天专注记录，最多纳入最近完成的100个任务。 · 使用任务当前估时；完成后改估时会重算历史准确度。",
  });
  assert.match(display.detail, /依据近30天专注记录，最多纳入最近完成的100个任务。/);
  assert.match(display.detail, /使用任务当前估时；完成后改估时会重算历史准确度。/);
});

test("three eligible tasks show their metric and sample count without the low-sample caution", () => {
  const display = getEstimationAccuracyDisplay(0.83, 3);
  assert.deepEqual(display, {
    value: "83%",
    detail: "样本任务数：3 · 1 = 完美预估 · 按 task_id 聚合，不按单次完成周期拆分；重开后的专注时段仍计入该任务。 · 依据近30天专注记录，最多纳入最近完成的100个任务。 · 使用任务当前估时；完成后改估时会重算历史准确度。",
  });
  assert.match(display.detail, /依据近30天专注记录，最多纳入最近完成的100个任务。/);
  assert.match(display.detail, /使用任务当前估时；完成后改估时会重算历史准确度。/);
});

test("Insights accuracy card uses the explicit label and the profile display helper", async () => {
  const [html, js] = await Promise.all([
    readFile(statsHtmlUrl, "utf8"),
    readFile(statsJsUrl, "utf8"),
  ]);
  assert.match(html, /已完成任务实际专注与估算的接近度/);
  assert.match(html, /id="estimationDetail"/);
  assert.match(js, /getEstimationAccuracyDisplay\(p\.estimation_accuracy, p\.estimated_focus_tasks\)/);
});
