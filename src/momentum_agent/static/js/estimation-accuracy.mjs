export function getEstimationAccuracyDisplay(accuracy, sampleCount) {
  const count = Number.isSafeInteger(sampleCount) && sampleCount >= 0 ? sampleCount : 0;
  const value = count > 0 && Number.isFinite(accuracy)
    ? `${(accuracy * 100).toFixed(0)}%`
    : "--";
  const boundary = "按 task_id 聚合，不按单次完成周期拆分；重开后的专注时段仍计入该任务。";
  const scope = "依据近30天专注记录，最多纳入最近完成的100个任务。";
  const transparency = "使用任务当前估时；完成后改估时会重算历史准确度。";
  const detail = count < 3
    ? `样本任务数：${count} · 样本较少，仅供参考 · ${boundary} · ${scope} · ${transparency}`
    : `样本任务数：${count} · 1 = 完美预估 · ${boundary} · ${scope} · ${transparency}`;

  return { value, detail };
}
