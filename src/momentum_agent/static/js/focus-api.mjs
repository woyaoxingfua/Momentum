export function startFocusSession(requestJson, taskId, durationMinutes) {
  return requestJson("/api/focus/start", {
    method: "POST",
    body: JSON.stringify({ task_id: taskId, duration_minutes: durationMinutes }),
  });
}

export function formatFocusStartError(error) {
  if (error?.status === 409) {
    return `启动失败（409）：该任务已关闭，未开始专注。${error.message ? ` ${error.message}` : ""}`;
  }
  return `专注未开始：${error?.message || "无法连接服务"}`;
}
