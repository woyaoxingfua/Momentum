import { requestJson, escapeHtml, formatDue, priorityText, recurrenceText, toDatetimeLocal, fromDatetimeLocal } from "./api.js";
import { loadReview, postponeTask } from "./advice.js";
import { createTaskCompletionReviewEntry } from "./completion-review-entry.mjs";
import { createTaskCompletionAction } from "./task-completion-action.mjs";
import { cleanupAttachmentsForTask, openAttachmentDialog, refreshAttachmentBadges } from "./attachments.mjs";
import { filterUnestimatedDailyWorkloadTasks, getDailyWorkloadEstimate, isValidLocalDateKey, localDateKey, positiveEstimateMinutes, taskDueLocalDate } from "./daily-workload.mjs";

const els = {};
const taskCompletion = createTaskCompletionAction({ requestJson });
let currentStatus = "todo";
let appEls = null;
let currentTasks = [];
let completionReviewEntry = null;
let editingTask = null;
let onTaskUpdated = null;
const pendingTaskCompletions = new Set();
const completionFeedback = new Map();
const QUICK_ESTIMATE_MINUTES = Object.freeze([15, 25, 45, 60]);
const quickEstimateSelections = new Map();
const quickEstimateFeedback = new Map();
const pendingQuickEstimateSaves = new Set();
const pendingQuickEstimateChecks = new Set();
const DAILY_WORKLOAD_REFRESH_KEY = "momentum_daily_workload_refresh";
let sortMode = "default"; // default | score | short
let serverSortMode = "default";
let unsubscribePostponeState = null;
let unsubscribeCompletionState = null;
let dueDateFilterActive = false;
let dueOnFilterDate = null;
let unestimatedDueByTodayActive = false;
let currentIsSearchResult = false;
const OPEN_TASK_STATUSES = new Set(["todo", "doing"]);

export function initTasks(elements, dialogElements, appElements, taskUpdatedHandler = null) {
  unsubscribePostponeState?.();
  unsubscribeCompletionState?.();
  Object.assign(els, elements, dialogElements);
  if (!els.dueDateFilterButton) {
    els.dueDateFilterButton = globalThis.document?.getElementById?.("dueDateFilterButton") || null;
  }
  appEls = appElements;
  els.dueOnFilterChip ||= globalThis.document?.getElementById?.("dueOnFilterChip") || null;
  els.dueOnFilterLabel ||= globalThis.document?.getElementById?.("dueOnFilterLabel") || null;
  els.clearDueOnFilterButton ||= globalThis.document?.getElementById?.("clearDueOnFilterButton") || null;
  els.unestimatedDueFilterChip ||= globalThis.document?.getElementById?.("unestimatedDueFilterChip") || null;
  els.clearUnestimatedDueFilterButton ||= globalThis.document?.getElementById?.("clearUnestimatedDueFilterButton") || null;
  els.quickEstimateStatus ||= globalThis.document?.getElementById?.("quickEstimateStatus") || null;
  dueOnFilterDate = parseDueOnDate(globalThis.location?.search || "");
  unestimatedDueByTodayActive = parseUnestimatedDueByToday(globalThis.location?.search || "");
  if (dueOnFilterDate && unestimatedDueByTodayActive) {
    unestimatedDueByTodayActive = false;
    updateUnestimatedDueByTodayQuery(false);
  }
  if (dueOnFilterDate || unestimatedDueByTodayActive) dueDateFilterActive = false;
  onTaskUpdated = taskUpdatedHandler;
  unsubscribePostponeState = postponeTask.subscribe?.(syncPostponeState) || null;
  unsubscribeCompletionState = taskCompletion.subscribe(syncTaskCompletionState);
  appEls.postponeRetryButton?.addEventListener("click", retryPendingPostpone);
  els.dueDateFilterButton?.addEventListener("click", toggleDueDateFilter);
  els.clearDueOnFilterButton?.addEventListener("click", clearDueOnFilter);
  els.clearUnestimatedDueFilterButton?.addEventListener("click", clearUnestimatedDueFilter);
  syncDateFilterControls();
}

export function setSortMode(mode) {
  if (!new Set(["default", "score", "short"]).has(mode)) return;
  if (mode === "short" && !OPEN_TASK_STATUSES.has(currentStatus)) return;
  sortMode = mode;
  if (mode !== "short") serverSortMode = mode;
}

export function getSortMode() {
  return sortMode;
}

export function setTaskStatusFilter(status) {
  currentStatus = status;
  if (!OPEN_TASK_STATUSES.has(status)) {
    if (sortMode === "short") sortMode = serverSortMode;
    dueDateFilterActive = false;
    clearDueOnFilter({ render: false });
    clearUnestimatedDueFilter({ render: false });
  }
  syncDateFilterControls();
}

export function getTaskStatusFilter() {
  return currentStatus;
}

const STATUS_LABELS = { todo: "待办", doing: "进行中", done: "已完成", dropped: "已放弃" };

export function parseDueOnDate(search = globalThis.location?.search || "") {
  const query = new URLSearchParams(String(search).replace(/^\?/, ""));
  const value = query.get("due_on");
  return isValidLocalDateKey(value) ? value : null;
}

export function parseUnestimatedDueByToday(search = globalThis.location?.search || "") {
  const query = new URLSearchParams(String(search).replace(/^\?/, ""));
  return query.get("unestimated_due_by_today") === "1";
}

function updateQueryParameter(name, value) {
  const location = globalThis.location;
  const history = globalThis.history;
  if (!location?.href || typeof history?.replaceState !== "function") return;

  const url = new URL(location.href);
  if (value !== null && value !== undefined && value !== "") url.searchParams.set(name, value);
  else url.searchParams.delete(name);
  history.replaceState(history.state ?? null, "", `${url.pathname}${url.search}${url.hash}`);
}

function updateDueOnQuery(value) {
  updateQueryParameter("due_on", isValidLocalDateKey(value) ? value : null);
}

function updateUnestimatedDueByTodayQuery(active) {
  updateQueryParameter("unestimated_due_by_today", active ? "1" : null);
}

export function filterOpenTasksDueByLocalToday(tasks, now = new Date()) {
  const today = localDateKey(now);
  if (!today || !Array.isArray(tasks)) return [];
  return tasks.filter((task) => getDailyWorkloadEstimate(task, today) !== null);
}

export function filterOpenTasksDueOnLocalDate(tasks, dateKey) {
  if (!isValidLocalDateKey(dateKey) || !Array.isArray(tasks)) return [];
  return tasks.filter((task) => OPEN_TASK_STATUSES.has(task?.status) && taskDueLocalDate(task?.due_at) === dateKey);
}

export function getDueDateFilterActive() {
  return dueDateFilterActive;
}

export function getUnestimatedDueByTodayFilterActive() {
  return unestimatedDueByTodayActive;
}

function syncDateFilterControls() {
  const chip = els.dueOnFilterChip;
  if (chip) chip.hidden = !dueOnFilterDate;
  if (els.dueOnFilterLabel) els.dueOnFilterLabel.textContent = dueOnFilterDate || "";
  if (els.unestimatedDueFilterChip) els.unestimatedDueFilterChip.hidden = !unestimatedDueByTodayActive;

  const button = els.dueDateFilterButton;
  if (!button) return;
  const available = OPEN_TASK_STATUSES.has(currentStatus);
  const anotherFilterIsActive = Boolean(dueOnFilterDate || unestimatedDueByTodayActive);
  button.hidden = !available;
  button.disabled = !available || anotherFilterIsActive;
  button.title = dueOnFilterDate
    ? "指定日期筛选优先；与逾期/今天到期及未估时筛选互斥，清除指定日期后可使用"
    : unestimatedDueByTodayActive
      ? "未估时筛选包含本地逾期和今天到期的开放任务；与其他到期筛选互斥，清除后可使用"
    : "只显示待办或进行中且已到期的任务";
  button.setAttribute?.("aria-pressed", String(dueDateFilterActive && available));
  button.classList?.toggle("primary", dueDateFilterActive && available && !anotherFilterIsActive);
  button.classList?.toggle("ghost", !dueDateFilterActive || !available || anotherFilterIsActive);
}

function clearDueOnFilter({ render = true } = {}) {
  if (!dueOnFilterDate) return;
  dueOnFilterDate = null;
  updateDueOnQuery(null);
  syncDateFilterControls();
  if (render) renderTasks(currentTasks, currentIsSearchResult);
}

function clearUnestimatedDueFilter({ render = true } = {}) {
  if (!unestimatedDueByTodayActive) return;
  unestimatedDueByTodayActive = false;
  updateUnestimatedDueByTodayQuery(false);
  setQuickEstimateGlobalStatus("");
  syncDateFilterControls();
  if (render) renderTasks(currentTasks, currentIsSearchResult);
}

function toggleDueDateFilter() {
  if (!OPEN_TASK_STATUSES.has(currentStatus) || dueOnFilterDate || unestimatedDueByTodayActive) {
    dueDateFilterActive = false;
    syncDateFilterControls();
    return;
  }
  dueDateFilterActive = !dueDateFilterActive;
  syncDateFilterControls();
  renderTasks(currentTasks, currentIsSearchResult);
}

export function renderTasks(tasks, isSearchResult = false, now = new Date()) {
  currentTasks = Array.isArray(tasks) ? tasks : [];
  currentIsSearchResult = Boolean(isSearchResult);
  const exactDateFilterIsActive = Boolean(dueOnFilterDate && OPEN_TASK_STATUSES.has(currentStatus));
  const unestimatedFilterIsActive = unestimatedDueByTodayActive && OPEN_TASK_STATUSES.has(currentStatus) && !exactDateFilterIsActive;
  const overdueFilterIsActive = dueDateFilterActive && OPEN_TASK_STATUSES.has(currentStatus) && !exactDateFilterIsActive && !unestimatedFilterIsActive;
  const visibleTasks = exactDateFilterIsActive
    ? filterOpenTasksDueOnLocalDate(currentTasks, dueOnFilterDate)
    : unestimatedFilterIsActive ? filterUnestimatedDailyWorkloadTasks(currentTasks, now)
      : overdueFilterIsActive ? filterOpenTasksDueByLocalToday(currentTasks, now) : currentTasks;
  if (isSearchResult) {
    els.taskCount.textContent = `${visibleTasks.length} 个搜索结果`;
  } else {
    const label = STATUS_LABELS[currentStatus] || "待办";
    els.taskCount.textContent = `${visibleTasks.length} 个${label}`;
  }
  const listedTaskIds = new Set(currentTasks.map((task) => String(task.id)));
  const pendingOutsideList = taskCompletion.getPendingIntents()
    .filter((intent) => !listedTaskIds.has(String(intent.task_id)))
    .map(renderPendingCompletionCard)
    .join("");
  const emptyMessage = isSearchResult
    ? exactDateFilterIsActive ? "没有找到同时符合搜索与指定日期筛选的开放任务。"
      : unestimatedFilterIsActive ? "没有找到同时符合搜索与本地逾期+今天未估时筛选的开放任务。"
        : overdueFilterIsActive ? "没有找到同时符合搜索与到期筛选的开放任务。" : "没有找到匹配的任务。"
    : exactDateFilterIsActive ? `没有 ${dueOnFilterDate} 到期的开放任务。`
      : unestimatedFilterIsActive ? "没有本地逾期或今天到期的未估时开放任务。"
        : overdueFilterIsActive ? "没有逾期或今天到期的开放任务。" : `没有${STATUS_LABELS[currentStatus] || "待办"}任务。`;
  const taskMarkup = visibleTasks.length === 0
    ? `<div class="empty">${emptyMessage}</div>`
    : orderTasks(visibleTasks).map((task, i) => renderTaskCard(task, i, unestimatedFilterIsActive, now)).join("");
  els.tasks.innerHTML = `${pendingOutsideList}${taskMarkup}`;

  bindTaskButtons();
  void refreshAttachmentBadges(visibleTasks);
}

export function renderCurrentTasks(now = new Date()) {
  return renderTasks(currentTasks, currentIsSearchResult, now);
}

function renderTaskCard(task, index, showQuickEstimate = false, now = new Date()) {
  const isDone = task.status === "done";
  const isDropped = task.status === "dropped";
  const checkboxClass = isDone ? "task-checkbox checked" : "task-checkbox";
  const completionIntent = taskCompletion.getPendingIntent(task.id);

  const statusBadge = task.status !== "todo"
    ? `<span class="badge status-${task.status}">${STATUS_LABELS[task.status] || task.status}</span>`
    : "";

  const tagsHtml = task.tags && task.tags.length > 0
    ? task.tags.map(tag => `<span class="badge tag">${escapeHtml(tag)}</span>`).join("")
    : "";

  const dueText = formatDue(task.due_at);
  const dueClass = dueText.includes("逾期") ? "badge overdue" : dueText.includes("今天") ? "badge status-doing" : "badge low";

  const estimate = positiveEstimateMinutes(task.estimated_minutes);
  const estimateText = estimate > 0 ? `${estimate}m` : "";

  return `
    <article class="task ${task.parent_task_id ? "subtask" : "parent-task"} ${isDone ? "task-done" : ""} ${isDropped ? "task-dropped" : ""}" style="animation-delay: ${index * 30}ms">
      <button class="${checkboxClass}" data-toggle="${task.id}" aria-label="切换完成状态" ${completionIntent ? "disabled" : ""}></button>
      <div class="task-body">
        <div class="task-title">${escapeHtml(task.title)}</div>
        <div class="task-meta">
          ${statusBadge}
          <span class="badge ${task.priority}">${priorityText(task.priority)}</span>
          ${dueText ? `<span class="${dueClass}">${dueText}</span>` : ""}
          ${estimateText ? `<span class="badge low">${estimateText}</span>` : ""}
          ${task.parent_task_id ? `<span class="badge low">子任务</span>` : ""}
          ${task.recurrence ? `<span class="badge recurrence">${recurrenceText(task.recurrence)}</span>` : ""}
          ${tagsHtml}
        </div>
      </div>
      <div class="task-actions">
        ${actionButtons(task)}
        ${showQuickEstimate ? renderQuickEstimateControls(task, now) : ""}
      </div>
    </article>`;
}

function renderQuickEstimateControls(task, now) {
  if (!OPEN_TASK_STATUSES.has(task?.status) || getDailyWorkloadEstimate(task, localDateKey(now)) !== 0) return "";

  const taskId = String(task.id);
  const selectedMinutes = quickEstimateSelections.get(taskId);
  const feedback = quickEstimateFeedback.get(taskId);
  const saving = pendingQuickEstimateSaves.has(taskId);
  const checking = pendingQuickEstimateChecks.has(taskId);
  const uncertain = feedback?.kind === "uncertain";
  const choices = QUICK_ESTIMATE_MINUTES.map((minutes) => {
    const selected = selectedMinutes === minutes;
    return `<button type="button" data-estimate-select="${escapeHtml(taskId)}" data-estimate-minutes="${minutes}" class="${selected ? "primary" : "ghost"}" aria-pressed="${selected}" aria-label="选择 ${minutes} 分钟" ${saving || checking || uncertain ? "disabled" : ""}>${minutes} 分钟</button>`;
  }).join("");
  const feedbackMarkup = feedback?.message
    ? `<span class="task-quick-estimate-feedback" role="${feedback.kind === "error" || uncertain ? "alert" : "status"}">${escapeHtml(feedback.message)}</span>`
    : "";
  const action = uncertain
    ? `<button type="button" data-estimate-check="${escapeHtml(taskId)}" ${checking || saving ? "disabled" : ""}>${checking ? "正在 GET 核对…" : "刷新核对"}</button>`
    : `<button type="button" data-estimate-save="${escapeHtml(taskId)}" class="primary" ${!QUICK_ESTIMATE_MINUTES.includes(selectedMinutes) || saving || checking ? "disabled" : ""}>${saving ? "保存中…" : feedback?.kind === "reconciled" && feedback.minutes === selectedMinutes ? `显式重试 ${selectedMinutes} 分钟` : "显式保存"}</button>`;

  return `<div class="task-quick-estimate" aria-label="快捷估时"><div class="task-quick-estimate-choices" role="group" aria-label="选择估时分钟数">${choices}</div>${action}${feedbackMarkup}</div>`;
}

function actionButtons(task) {
  const buttons = [];
  const completionIntent = taskCompletion.getPendingIntent(task.id);
  buttons.push(`<button data-edit="${task.id}" title="编辑">编辑</button>`);
  buttons.push(`<button data-attachments="${task.id}" title="本地附件（仅此浏览器，不上传）">附件</button>`);

  if (!task.parent_task_id) {
    buttons.push(`<button data-add-subtask="${task.id}" title="添加子任务">子任务</button>`);
  }

  if (completionIntent) {
    buttons.push(`<span class="task-completion-pending" role="status">完成未确认</span>`);
    if (completionIntent.status !== "sending" && completionIntent.status !== "corrupt") {
      buttons.push(`<button data-done-retry="${task.id}" title="使用原 UUID 重试完成">使用原请求重试</button>`);
    }
    const pendingMessage = completionIntent.status === "corrupt"
      ? "本地完成请求记录无法验证；为避免生成新 UUID 重复完成，已停止发送。"
      : completionIntent.status === "sending"
        ? "完成请求处理中，尚未确认；页面加载不会自动重发。"
        : "完成结果尚未确认；请显式使用原请求重试，页面加载不会自动重发。";
    buttons.push(`<span class="task-completion-feedback" role="status">${escapeHtml(pendingMessage)}</span>`);
  } else if (task.status === "todo") {
    buttons.push(`<button data-start="${task.id}" title="开始做" class="primary">开始</button>`);
    buttons.push(`<button data-focus-start="${task.id}" title="以任务预估时长开始专注">开始专注</button>`);
    buttons.push(`<button data-done="${task.id}" title="完成">完成</button>`);
    buttons.push(postponeButton(task));
    buttons.push(`<button data-drop="${task.id}" title="放弃" class="danger">放弃</button>`);
  } else if (task.status === "doing") {
    buttons.push(`<button data-focus-start="${task.id}" title="以任务预估时长开始专注">开始专注</button>`);
    buttons.push(`<button data-done="${task.id}" class="primary">完成</button>`);
    buttons.push(postponeButton(task));
    buttons.push(`<button data-drop="${task.id}" title="放弃" class="danger">放弃</button>`);
  } else {
    buttons.push(`<button data-reopen="${task.id}" title="重新打开">重开</button>`);
  }

  const priorFeedback = getCompletionFeedback(task.id);
  if (!completionIntent && priorFeedback) {
    buttons.push(`<span class="task-completion-feedback" role="${priorFeedback.kind === "error" ? "alert" : "status"}">${escapeHtml(priorFeedback.message)}</span>`);
  }

  return buttons.join("");
}

function renderPendingCompletionCard(intent) {
  const taskId = escapeHtml(intent.task_id);
  const isSending = intent.status === "sending";
  const isCorrupt = intent.status === "corrupt";
  const message = isCorrupt
    ? "本地完成请求记录无法验证；为避免生成新 UUID 重复完成，已停止发送。"
    : isSending
      ? "完成请求处理中，尚未确认；页面加载不会自动重发。"
      : "完成结果尚未确认；请显式使用原请求重试，页面加载不会自动重发。";
  return `<article class="task task-completion-pending-card" aria-live="polite"><div class="task-body"><div class="task-title">任务 #${taskId}</div><div class="task-meta"><span class="badge status-doing">完成未确认</span></div></div><div class="task-actions"><span class="task-completion-feedback" role="status">${message}</span>${!isSending && !isCorrupt ? `<button data-done-retry="${taskId}">使用原请求重试</button>` : ""}</div></article>`;
}

function completionFeedbackKey(taskId) {
  const userId = taskCompletion.getCurrentUserId();
  return userId == null ? null : `${userId}\u0000${String(taskId)}`;
}

function setCompletionFeedback(taskId, value) {
  const key = completionFeedbackKey(taskId);
  if (key == null) return;
  if (value) completionFeedback.set(key, value);
  else completionFeedback.delete(key);
}

function getCompletionFeedback(taskId) {
  const key = completionFeedbackKey(taskId);
  return key == null ? null : completionFeedback.get(key) || null;
}

export function completionResultMessage(result) {
  const error = result?.error;
  const code = error?.payload?.error;
  const status = Number(error?.status);
  if (status === 400 && code === "idempotency_key_required") return "完成请求被拒绝（400）：服务端未收到 Idempotency-Key。";
  if (status === 400 && code === "idempotency_key_invalid") return "完成请求被拒绝（400）：Idempotency-Key 不是有效 UUIDv4。";
  if (status === 404) return "完成请求失败（404）：任务不存在或当前不可用。";
  if (status === 409 && code === "idempotency_conflict") return "完成请求冲突（409）：此 UUID 已用于不同的请求；任务未按本次请求确认。";
  if (result?.status === "uncertain" || result?.status === "pending") {
    const detail = error?.message ? `（${error.message}）` : "";
    return `完成结果尚未确认${detail}；页面加载不会自动重发，请显式使用原请求重试。`;
  }
  if (result?.status === "blocked") return error?.message || "完成请求状态无法验证，已停止发送。";
  if (Number.isInteger(status)) return `完成请求失败（HTTP ${status}）：${error?.payload?.error || error?.message || "请稍后检查任务状态"}`;
  return error?.message || "完成请求未能发送；请确认状态后再试。";
}

function syncTaskCompletionState(event) {
  if (!event || String(event.user_id) !== String(taskCompletion.getCurrentUserId())) return;
  const taskId = String(event.task_id);
  if (event.type === "intent") {
    if (event.error) setCompletionFeedback(taskId, { message: completionResultMessage({ status: "uncertain", error: event.error }), kind: "pending" });
    else if (event.intent?.status === "sending") setCompletionFeedback(taskId, null);
  } else if (event.outcome === "error") {
    setCompletionFeedback(taskId, { message: completionResultMessage({ status: "error", error: event.error }), kind: "error" });
  } else {
    setCompletionFeedback(taskId, null);
  }
  renderTasks(currentTasks, currentIsSearchResult);
}

function postponeButton(task) {
  const intent = postponeTask.getPendingIntent?.(task.id);
  const label = !intent ? "推迟" : intent.status === "sending" ? "顺延处理中" : "顺延未决";
  const accessibleLabel = `${label}：${task.title}`;
  return `<button data-postpone="${task.id}" title="${label}" aria-label="${escapeHtml(accessibleLabel)}">${label}</button>`;
}

function syncPostponeState(event) {
  if (!event || String(event.user_id) !== String(postponeTask.getCurrentUserId?.())) return;
  const taskId = String(event.task_id);
  const pending = event.type === "intent" ? event.intent : null;
  document.querySelectorAll(`[data-postpone="${taskId}"]`).forEach((button) => {
    const label = !pending ? "推迟" : pending.status === "sending" ? "顺延处理中" : "顺延未决";
    button.textContent = label;
    button.title = label;
    button.setAttribute?.("aria-label", `${label}：${currentTasks.find((task) => String(task.id) === taskId)?.title || `任务 ${taskId}`}`);
  });
  if (appEls?.postponeDialog?.open && String(appEls.postponeTaskId?.value) === taskId) {
    if (pending) {
      showPostponeIntent(pending);
    } else if (event.outcome === "success") {
      const task = event.task || currentTasks.find((item) => String(item.id) === taskId);
      if (task?.due_at) appEls.postponeCurrentDue.textContent = `当前截止日：${formatDue(task.due_at)}；已顺延 1 天。`;
      appEls.postponeFeedback.textContent = "顺延已确认；两个入口现已同步。";
      appEls.postponeSubmitButton.disabled = false;
      if (appEls.postponeRetryButton) appEls.postponeRetryButton.hidden = true;
    } else {
      appEls.postponeSubmitButton.disabled = false;
      if (appEls.postponeRetryButton) appEls.postponeRetryButton.hidden = true;
      if (event.error?.message) appEls.postponeFeedback.textContent = event.error.message;
    }
  }
}

function showPostponeIntent(intent) {
  appEls.postponeSubmitButton.disabled = true;
  appEls.postponeFeedback.textContent = intent.last_error_message
    || (intent.status === "sending" ? "顺延请求正在处理；普通顺延入口已锁定。" : "顺延请求尚未确认。请显式使用原请求再试；页面加载不会自动重发。");
  if (appEls.postponeRetryButton) {
    appEls.postponeRetryButton.hidden = intent.status === "sending";
    appEls.postponeRetryButton.disabled = intent.status === "sending";
  }
}

function bindTaskButtons() {
  document.querySelectorAll("[data-estimate-select]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const taskId = String(btn.dataset.estimateSelect);
      const minutes = Number(btn.dataset.estimateMinutes);
      if (pendingQuickEstimateSaves.has(taskId) || pendingQuickEstimateChecks.has(taskId) || quickEstimateFeedback.get(taskId)?.kind === "uncertain") return;
      if (!QUICK_ESTIMATE_MINUTES.includes(minutes)) return;
      quickEstimateSelections.set(taskId, minutes);
      const feedback = quickEstimateFeedback.get(taskId);
      if (feedback?.kind === "error") quickEstimateFeedback.delete(taskId);
      else if (feedback?.kind === "reconciled" && feedback.minutes !== minutes) {
        quickEstimateFeedback.set(taskId, { ...feedback, message: `GET 已核对仍未估时；尚未重试。当前选择 ${minutes} 分钟，点击显式保存后才会提交。` });
      }
      renderTasks(currentTasks, currentIsSearchResult);
    });
  });

  document.querySelectorAll("[data-estimate-save]").forEach((btn) => {
    btn.addEventListener("click", () => saveQuickEstimate(btn.dataset.estimateSave));
  });

  document.querySelectorAll("[data-estimate-check]").forEach((btn) => {
    btn.addEventListener("click", () => checkQuickEstimate(btn.dataset.estimateCheck));
  });

  document.querySelectorAll("[data-toggle]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const taskId = btn.dataset.toggle;
      const isChecked = btn.classList.contains("checked");
      if (isChecked) {
        await requestJson(`/api/tasks/${taskId}/reopen`, { method: "POST" });
        await loadTasks();
      } else {
        await completeTask(taskId, btn);
      }
    });
  });

  document.querySelectorAll("[data-start]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await requestJson(`/api/tasks/${btn.dataset.start}/start`, { method: "POST" });
      await loadTasks();
    });
  });

  document.querySelectorAll("[data-focus-start]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const task = currentTasks.find((item) => String(item.id) === String(btn.dataset.focusStart));
      if (!task || !OPEN_TASK_STATUSES.has(task.status)) return;
      const { startSuggestedFocus } = await import("./focus.js");
      await startSuggestedFocus(task);
    });
  });

  document.querySelectorAll("[data-done]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await completeTask(btn.dataset.done, btn);
    });
  });

  document.querySelectorAll("[data-done-retry]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await completeTask(btn.dataset.doneRetry, btn, true);
    });
  });

  document.querySelectorAll("[data-attachments]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const task = currentTasks.find((item) => String(item.id) === String(btn.dataset.attachments));
      if (task) void openAttachmentDialog(task);
    });
  });

  document.querySelectorAll("[data-edit]").forEach((btn) => {
    btn.addEventListener("click", () => openEditDialog(btn.dataset.edit));
  });

  document.querySelectorAll("[data-add-subtask]").forEach((btn) => {
    btn.addEventListener("click", () => openAddSubtaskDialog(btn.dataset.addSubtask));
  });

  document.querySelectorAll("[data-postpone]").forEach((btn) => {
    btn.addEventListener("click", () => openPostponeDialog(btn.dataset.postpone));
  });

  document.querySelectorAll("[data-drop]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("确定要放弃这个任务吗？")) return;
      const droppedTask = currentTasks.find((item) => String(item.id) === String(btn.dataset.drop));
      await requestJson(`/api/tasks/${btn.dataset.drop}/drop`, { method: "POST" });
      if (droppedTask) await cleanupAttachmentsForTask(droppedTask);
      await loadTasks();
    });
  });

  document.querySelectorAll("[data-reopen]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await requestJson(`/api/tasks/${btn.dataset.reopen}/reopen`, { method: "POST" });
      await loadTasks();
    });
  });
}

function setQuickEstimateGlobalStatus(message) {
  if (!els.quickEstimateStatus) return;
  els.quickEstimateStatus.textContent = message;
  els.quickEstimateStatus.hidden = !message;
}

function isQuickEstimateEligible(taskId) {
  const task = currentTasks.find((item) => String(item.id) === String(taskId));
  return Boolean(
    task
    && unestimatedDueByTodayActive
    && OPEN_TASK_STATUSES.has(task.status)
    && getDailyWorkloadEstimate(task, localDateKey(new Date())) === 0,
  );
}

function notifyDailyWorkloadStats() {
  try {
    globalThis.localStorage?.setItem(DAILY_WORKLOAD_REFRESH_KEY, `${Date.now()}:${Math.random()}`);
  } catch {
    // The task list refresh remains authoritative if storage events are unavailable.
  }
}

async function saveQuickEstimate(taskId) {
  const id = String(taskId);
  if (pendingQuickEstimateSaves.has(id) || pendingQuickEstimateChecks.has(id)) return;
  if (quickEstimateFeedback.get(id)?.kind === "uncertain" || !isQuickEstimateEligible(id)) return;
  const minutes = quickEstimateSelections.get(id);
  if (!QUICK_ESTIMATE_MINUTES.includes(minutes)) return;

  pendingQuickEstimateSaves.add(id);
  quickEstimateFeedback.delete(id);
  setQuickEstimateGlobalStatus("");
  renderTasks(currentTasks, currentIsSearchResult);
  try {
    await requestJson(`/api/tasks/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify({ estimated_minutes: minutes }),
    });
  } catch (error) {
    const status = Number(error?.status);
    if (Number.isInteger(status) && status >= 400 && status < 500) {
      quickEstimateFeedback.set(id, {
        kind: "error",
        message: `保存失败（HTTP ${status}）：${error?.message || "请求被拒绝"}。任务仍保留在未估时列表，可重新选择并显式保存。`,
      });
    } else {
      quickEstimateFeedback.set(id, {
        kind: "uncertain",
        minutes,
        message: `结果未确认${error?.message ? `：${error.message}` : ""}。为避免重复写入，不会自动重发；请先 GET 刷新核对，再决定是否显式重试相同数值。`,
      });
    }
    pendingQuickEstimateSaves.delete(id);
    renderTasks(currentTasks, currentIsSearchResult);
    return;
  } finally {
    pendingQuickEstimateSaves.delete(id);
  }

  quickEstimateSelections.delete(id);
  quickEstimateFeedback.set(id, { kind: "success", message: `服务器已确认保存 ${minutes} 分钟；正在刷新列表。` });
  notifyDailyWorkloadStats();
  try {
    const refreshedTasks = await loadTasks();
    const stillUnestimated = filterUnestimatedDailyWorkloadTasks(refreshedTasks).some((task) => String(task.id) === id);
    if (stillUnestimated) {
      quickEstimateFeedback.set(id, {
        kind: "success",
        message: `服务器已确认保存 ${minutes} 分钟（HTTP 200），但 GET 刷新后任务仍符合未估时筛选；请核实当前数据，未自动重发。`,
      });
    } else {
      quickEstimateFeedback.delete(id);
      setQuickEstimateGlobalStatus(`已保存 ${minutes} 分钟；列表已刷新，该任务已移出未估时结果。`);
    }
    renderTasks(refreshedTasks, false);
  } catch (error) {
    quickEstimateFeedback.set(id, {
      kind: "success",
      message: `服务器已确认保存 ${minutes} 分钟（HTTP 200），但列表 GET 刷新失败${error?.message ? `：${error.message}` : ""}；请手动刷新核对。`,
    });
    renderTasks(currentTasks, currentIsSearchResult);
  }
}

async function checkQuickEstimate(taskId) {
  const id = String(taskId);
  const feedback = quickEstimateFeedback.get(id);
  if (pendingQuickEstimateChecks.has(id) || pendingQuickEstimateSaves.has(id) || feedback?.kind !== "uncertain") return;

  pendingQuickEstimateChecks.add(id);
  renderTasks(currentTasks, currentIsSearchResult);
  try {
    const refreshedTasks = await loadTasks();
    const stillUnestimated = filterUnestimatedDailyWorkloadTasks(refreshedTasks).some((task) => String(task.id) === id);
    if (stillUnestimated) {
      quickEstimateFeedback.set(id, {
        kind: "reconciled",
        minutes: feedback.minutes,
        message: `GET 已核对：任务当前仍未估时；未自动重发。保持 ${feedback.minutes} 分钟并点击“显式重试”，或重新选择后显式保存。`,
      });
    } else {
      quickEstimateSelections.delete(id);
      quickEstimateFeedback.delete(id);
      const taskStillInStatusList = refreshedTasks.some((task) => String(task.id) === id);
      setQuickEstimateGlobalStatus(taskStillInStatusList
        ? "GET 核对完成：任务当前不再符合未估时筛选，已移出结果；未重发。"
        : "GET 核对完成，但任务未出现在当前状态列表；未重发，请按刷新结果核实。");
    }
  } catch (error) {
    quickEstimateFeedback.set(id, {
      kind: "uncertain",
      minutes: feedback.minutes,
      message: `GET 刷新核对失败${error?.message ? `：${error.message}` : ""}；结果仍未确认，不会自动重发。请再次显式刷新核对。`,
    });
  } finally {
    pendingQuickEstimateChecks.delete(id);
    renderTasks(currentTasks, currentIsSearchResult);
  }
}

export async function completeTask(taskId, button, isRetry = false) {
  const id = String(taskId);
  const lockKey = completionFeedbackKey(id) || `unidentified\u0000${id}`;
  if (pendingTaskCompletions.has(lockKey)) return { status: "pending", intent: taskCompletion.getPendingIntent(id) };
  pendingTaskCompletions.add(lockKey);
  setCompletionFeedback(id, null);
  completionReviewEntry?.updateMessage("");
  if (button) button.disabled = true;
  try {
    const result = isRetry ? await taskCompletion.retry(id) : await taskCompletion(id);
    if (result.status !== "success") {
      const message = completionResultMessage(result);
      setCompletionFeedback(id, { message, kind: result.status === "error" || result.status === "blocked" ? "error" : "pending" });
      renderTasks(currentTasks, currentIsSearchResult);
      return result;
    }

    const entry = getCompletionReviewEntry();
    if (entry && els.tasks?.before && !entry.element.isConnected) {
      els.tasks.before(entry.element);
    }
    entry?.updateMessage(result.response?.message || "任务已完成。");
    entry?.update(null);
    const review = await loadReview();
    entry?.update(review);
    await loadTasks();
    return result;
  } catch (error) {
    const result = { status: "error", error };
    const message = completionResultMessage(result);
    setCompletionFeedback(id, { message, kind: "error" });
    renderTasks(currentTasks, currentIsSearchResult);
    return result;
  } finally {
    pendingTaskCompletions.delete(lockKey);
    if (button) button.disabled = Boolean(taskCompletion.getPendingIntent(id));
  }
}

completeTask.retry = (taskId, button) => completeTask(taskId, button, true);
completeTask.getPendingIntent = (taskId) => taskCompletion.getPendingIntent(taskId);
completeTask.getPendingIntents = () => taskCompletion.getPendingIntents();
completeTask.getCurrentUserId = () => taskCompletion.getCurrentUserId();
completeTask.subscribe = (listener) => taskCompletion.subscribe(listener);
completeTask.getCompletionFeedback = (taskId) => getCompletionFeedback(taskId);

function getCompletionReviewEntry() {
  if (!completionReviewEntry) {
    completionReviewEntry = createTaskCompletionReviewEntry({
      onReview: async () => {
        const review = await loadReview();
        const reviewPanel = document.getElementById("todayReview");
        if (reviewPanel && !reviewPanel.hidden) {
          reviewPanel.scrollIntoView?.({ behavior: "smooth", block: "start" });
        }
        return review;
      },
    });
  }
  return completionReviewEntry;
}

function openAddSubtaskDialog(parentTaskId) {
  appEls.addSubtaskParentId.value = parentTaskId;
  appEls.addSubtaskTitle.value = "";
  appEls.addSubtaskDue.value = "";
  appEls.addSubtaskPriority.value = "medium";
  appEls.addSubtaskEstimate.value = "";
  appEls.addSubtaskDialog.showModal();
}

export async function saveSubtask(event) {
  event.preventDefault();
  
  const parentTaskId = appEls.addSubtaskParentId.value;
  const title = appEls.addSubtaskTitle.value.trim();
  
  if (!title) {
    alert("请输入子任务标题");
    return;
  }
  
  const body = {
    title: title,
    due_at: appEls.addSubtaskDue.value || null,
    priority: appEls.addSubtaskPriority.value,
    estimated_minutes: appEls.addSubtaskEstimate.value ? Number(appEls.addSubtaskEstimate.value) : null,
  };
  
  await requestJson(`/api/tasks/${parentTaskId}/subtasks`, { method: "POST", body: JSON.stringify(body) });
  appEls.addSubtaskDialog.close();
  await loadTasks();
}

async function openEditDialog(taskId, knownTask = null) {
  let task = knownTask || currentTasks.find((t) => String(t.id) === String(taskId));
  if (!task) {
    const payload = await requestJson(`/api/tasks?status=${currentStatus}`);
    task = payload.tasks.find((t) => t.id === Number(taskId));
  }
  if (!task) return;

  editingTask = task;
  els.editTaskId.value = task.id;
  els.editTitle.value = task.title;
  els.editDue.value = toDatetimeLocal(task.due_at);
  els.editPriority.value = task.priority;
  els.editEstimate.value = task.estimated_minutes || "";
  els.editTags.value = task.tags ? task.tags.join(", ") : "";
  els.editNotes.value = task.notes || "";
  els.editDialog.showModal();
}

export function openTaskEditDialog(task) {
  if (!task || task.id == null) return;
  return openEditDialog(task.id, task);
}

export async function saveEdit(event) {
  event.preventDefault();

  const tagsStr = els.editTags.value.trim();
  let tags = null;
  if (tagsStr) {
    tags = tagsStr.split(",").map(t => t.trim()).filter(t => t);
  }

  const body = {
    title: els.editTitle.value.trim(),
    due_at: fromDatetimeLocal(els.editDue.value),
    priority: els.editPriority.value,
    estimated_minutes: els.editEstimate.value ? Number(els.editEstimate.value) : null,
    tags,
    notes: els.editNotes.value.trim() || null,
  };
  await requestJson(`/api/tasks/${els.editTaskId.value}`, { method: "PUT", body: JSON.stringify(body) });
  els.editDialog.close();
  if (editingTask) onTaskUpdated?.({ ...editingTask, ...body, id: editingTask.id });
  editingTask = null;
  await loadTasks();
}

export async function loadTasks() {
  const url = `/api/tasks?status=${currentStatus}&sort=${sortMode === "short" ? serverSortMode : sortMode}`;
  const payload = await requestJson(url);
  renderTasks(payload.tasks);
  return payload.tasks;
}

function orderTasks(tasks, mode = sortMode) {
  if (mode === "short") return orderTasksByEstimate(tasks);
  const children = new Map();
  tasks.forEach((task) => {
    if (!task.parent_task_id) return;
    const group = children.get(task.parent_task_id) || [];
    group.push(task);
    children.set(task.parent_task_id, group);
  });

  const ordered = [];
  tasks.filter((t) => !t.parent_task_id).forEach((task) => {
    ordered.push(task);
    ordered.push(...(children.get(task.id) || []));
  });

  tasks
    .filter((t) => t.parent_task_id && !tasks.some((p) => p.id === t.parent_task_id))
    .forEach((t) => ordered.push(t));

  return ordered;
}

export function orderTasksByEstimate(tasks) {
  if (!Array.isArray(tasks)) return [];

  const childrenByParent = new Map();
  const rootsById = new Map();
  const inputIndex = new Map(tasks.map((task, index) => [task, index]));
  for (const task of tasks) {
    if (!task?.parent_task_id) rootsById.set(String(task.id), task);
    else {
      const parentId = String(task.parent_task_id);
      const children = childrenByParent.get(parentId) || [];
      children.push(task);
      childrenByParent.set(parentId, children);
    }
  }

  const grouped = new Set();
  const groups = [];
  for (const task of tasks) {
    if (grouped.has(task)) continue;
    const parentId = task?.parent_task_id ? String(task.parent_task_id) : null;
    const root = parentId ? rootsById.get(parentId) : task;
    if (root && root !== task) continue;

    // A parent's estimate determines its intact group; orphaned children form a group at their first visible sibling.
    const members = root
      ? [root, ...(childrenByParent.get(String(root.id)) || [])]
      : parentId ? (childrenByParent.get(parentId) || [task]) : [task];
    const uniqueMembers = members.filter((member) => !grouped.has(member));
    uniqueMembers.forEach((member) => grouped.add(member));
    const estimateTask = root || uniqueMembers[0] || task;
    groups.push({
      members: uniqueMembers,
      estimate: positiveEstimateMinutes(estimateTask?.estimated_minutes),
      index: inputIndex.get(root || task) ?? inputIndex.get(task) ?? 0,
    });
  }

  groups.sort((left, right) => {
    if ((left.estimate > 0) !== (right.estimate > 0)) return left.estimate > 0 ? -1 : 1;
    return (left.estimate - right.estimate) || (left.index - right.index);
  });
  return groups.flatMap((group) => group.members);
}

function openPostponeDialog(taskId) {
  const task = currentTasks.find((item) => String(item.id) === String(taskId));
  appEls.postponeTaskId.value = taskId;
  const submitButton = appEls.postponeSubmitButton;
  const feedback = appEls.postponeFeedback;
  const retryButton = appEls.postponeRetryButton;
  const intent = postponeTask.getPendingIntent?.(taskId);
  const eligible = task && ["todo", "doing"].includes(task.status);
  const hasDueDate = Boolean(task?.due_at);
  appEls.postponeCurrentDue.textContent = hasDueDate
    ? `当前截止日：${formatDue(task.due_at)}；确认后顺延 1 天。`
    : "顺延现有截止日 1 天。";
  feedback.textContent = "";
  if (intent) {
    showPostponeIntent(intent);
  } else {
    if (retryButton) { retryButton.hidden = true; retryButton.disabled = true; }
    feedback.textContent = !eligible
      ? "仅待办或进行中的任务可顺延，请刷新任务列表后重试。"
      : !hasDueDate
        ? "未设置截止日，无法顺延。"
        : "";
    submitButton.disabled = !eligible || !hasDueDate;
  }
  appEls.postponeDialog.showModal();
}

let postponeDialogBusy = false;

export async function savePostpone(event) {
  event.preventDefault();
  if (postponeDialogBusy || appEls.postponeSubmitButton.disabled) return;

  const taskId = appEls.postponeTaskId.value;
  const task = currentTasks.find((item) => String(item.id) === String(taskId));
  const submitButton = appEls.postponeSubmitButton;
  const feedback = appEls.postponeFeedback;
  if (!task || !["todo", "doing"].includes(task.status) || !task.due_at) {
    feedback.textContent = !task || !["todo", "doing"].includes(task?.status)
      ? "仅待办或进行中的任务可顺延，请刷新任务列表后重试。"
      : "未设置截止日，无法顺延。";
    submitButton.disabled = true;
    return;
  }

  postponeDialogBusy = true;
  submitButton.disabled = true;
  feedback.textContent = "";
  try {
    const result = await postponeTask(task);
    if (result?.postponePending) {
      showPostponeIntent(result.intent || postponeTask.getPendingIntent?.(taskId) || { status: "uncertain" });
      return;
    }
    if (!result?.postponeConfirmed && !result?.due_at) {
      feedback.textContent = "顺延结果尚未确认；为避免重复顺延，普通顺延入口已锁定。";
      return;
    }
    if (result.due_at) {
      task.due_at = result.due_at;
      appEls.postponeCurrentDue.textContent = `当前截止日：${formatDue(task.due_at)}；已顺延 1 天。`;
    }
    if (result.postponeRefreshWarning) {
      feedback.textContent = result.postponeRefreshWarning;
    } else {
      feedback.textContent = "顺延已确认。";
    }
    if (appEls.postponeRetryButton) appEls.postponeRetryButton.hidden = true;
    appEls.postponeDialog.close();
  } catch (error) {
    if (error?.reconciledTask?.due_at) {
      task.due_at = error.reconciledTask.due_at;
      appEls.postponeCurrentDue.textContent = `当前截止日：${formatDue(task.due_at)}；顺延结果仍不确定。`;
    }
    feedback.textContent = error?.message || "顺延失败，请稍后重试。";
    const intent = error?.pendingIntent || postponeTask.getPendingIntent?.(taskId);
    if (intent) showPostponeIntent(intent);
    else {
      submitButton.disabled = false;
      if (appEls.postponeRetryButton) appEls.postponeRetryButton.hidden = true;
    }
    feedback.textContent = error?.message || feedback.textContent;
  } finally {
    postponeDialogBusy = false;
  }
}

async function retryPendingPostpone(event) {
  event?.preventDefault?.();
  if (postponeDialogBusy || !appEls.postponeRetryButton || appEls.postponeRetryButton.disabled) return;
  const taskId = appEls.postponeTaskId.value;
  const task = currentTasks.find((item) => String(item.id) === String(taskId));
  if (!task) return;
  postponeDialogBusy = true;
  appEls.postponeSubmitButton.disabled = true;
  appEls.postponeRetryButton.disabled = true;
  appEls.postponeFeedback.textContent = "正在使用原 UUID 与原请求重试…";
  try {
    const result = await postponeTask.retry(task);
    if (result?.postponePending) {
      showPostponeIntent(result.intent || postponeTask.getPendingIntent?.(taskId) || { status: "sending" });
      return;
    }
    if (result?.due_at) {
      task.due_at = result.due_at;
      appEls.postponeCurrentDue.textContent = `当前截止日：${formatDue(task.due_at)}；已顺延 1 天。`;
    }
    appEls.postponeFeedback.textContent = result?.postponeRefreshWarning || "顺延已确认。";
    if (appEls.postponeRetryButton) appEls.postponeRetryButton.hidden = true;
    appEls.postponeDialog.close();
  } catch (error) {
    if (error?.reconciledTask?.due_at) {
      task.due_at = error.reconciledTask.due_at;
      appEls.postponeCurrentDue.textContent = `当前截止日：${formatDue(task.due_at)}；顺延结果仍不确定。`;
    }
    appEls.postponeFeedback.textContent = error?.message || "原请求重试失败。";
    const intent = error?.pendingIntent || postponeTask.getPendingIntent?.(taskId);
    if (intent) showPostponeIntent(intent);
    else appEls.postponeSubmitButton.disabled = false;
    appEls.postponeFeedback.textContent = error?.message || appEls.postponeFeedback.textContent;
  } finally {
    postponeDialogBusy = false;
    const intent = postponeTask.getPendingIntent?.(taskId);
    if (intent && intent.status !== "sending" && appEls.postponeRetryButton) appEls.postponeRetryButton.disabled = false;
  }
}
