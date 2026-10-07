import { requestJson } from "./api.js";
import { sendToAgent } from "./chat.js";
import { createTaskPostponeAction } from "./postpone-action.mjs";
import {
  getTodayReviewUrl,
  findBoundSuggestionTask,
  loadAdvicePayload,
  loadOpenTaskSummary,
  loadTodayReviewPayload,
  renderAdvicePayload,
  renderSuggestion,
  renderTodayReview,
  renderTodayReviewMessage,
  saveSuggestionEstimate,
} from "./advice-review.mjs";

let adviceText;
let suggestionContainer;
let reviewContainer;
let startSuggestedFocus = async () => {};
let refreshTaskList = async () => {};
let editTask = async () => {};
let completeTaskAction = async () => {};
let unsubscribeCompletionState = null;
let lastReviewPayload = null;
let lastOpenTasks = [];
let completionNotice = null;
const postponeTask = createTaskPostponeAction({
  requestJson,
  refreshTaskList: () => refreshTaskList(),
  onTaskUpdated: syncCachedReviewTask,
  onTaskObserved: syncCachedReviewTask,
});

function syncCachedReviewTask(task) {
  if (!task || !lastReviewPayload) return;
  const taskId = String(task.id);
  const index = lastOpenTasks.findIndex((item) => String(item.id) === taskId);
  if (index >= 0) lastOpenTasks[index] = { ...lastOpenTasks[index], ...task };
  else if (["todo", "doing"].includes(task.status)) lastOpenTasks.push(task);
  renderCachedTodayReview();
}

function renderCachedTodayReview() {
  if (!lastReviewPayload) return;
  let pendingCompletions = [];
  try { pendingCompletions = completeTaskAction.getPendingIntents?.() || []; } catch { /* Storage errors never trigger a completion request. */ }
  renderTodayReview(reviewContainer, lastReviewPayload, {
    unfinishedTasks: lastOpenTasks,
    pendingCompletions,
    completionNotice,
    onPostpone: postponeTask,
    onEditTask: editTask,
    onCompleteTask: completeTaskAction,
    onStartTask: startSuggestedFocus,
  });
}

export function syncReviewTask(task) {
  syncCachedReviewTask(task);
}

export { postponeTask };

export { getTodayReviewUrl };

export function initAdvice({ adviceText: adviceElement, suggestion, review, onStartSuggestion, onRefreshTasks, onEditTask, onCompleteTask } = {}) {
  unsubscribeCompletionState?.();
  adviceText = adviceElement;
  suggestionContainer = suggestion;
  reviewContainer = review;
  if (typeof onStartSuggestion === "function") startSuggestedFocus = onStartSuggestion;
  if (typeof onRefreshTasks === "function") refreshTaskList = onRefreshTasks;
  if (typeof onEditTask === "function") editTask = onEditTask;
  if (typeof onCompleteTask === "function") {
    completeTaskAction = onCompleteTask;
    unsubscribeCompletionState = completeTaskAction.subscribe?.((event) => {
      if (!event || String(event.user_id) !== String(completeTaskAction.getCurrentUserId?.())) return;
      if (event.type === "intent" && event.error) {
        const feedback = completeTaskAction.getCompletionFeedback?.(event.task_id);
        completionNotice = feedback ? { ...feedback, message: `任务 #${event.task_id}：${feedback.message}` } : null;
      } else if (event.outcome === "error") {
        const feedback = completeTaskAction.getCompletionFeedback?.(event.task_id);
        completionNotice = feedback ? { ...feedback, message: `任务 #${event.task_id}：${feedback.message}` } : null;
      } else if (event.outcome === "success" || event.intent?.status === "sending") {
        completionNotice = null;
      }
      renderCachedTodayReview();
    }) || null;
  }
}

export async function loadAdvice() {
  if (adviceText) adviceText.textContent = "正在读取任务状态...";
  try {
    const [payload, openTasks] = await Promise.all([
      loadAdvicePayload(requestJson),
      loadOpenTaskSummary(requestJson).catch(() => null),
    ]);
    const currentTask = findBoundSuggestionTask(payload?.suggestion, openTasks);
    renderAdvicePayload(
      payload,
      adviceText,
      suggestionContainer,
      startSuggestedFocus,
      (suggestion, minutes) => saveSuggestionEstimate(requestJson, suggestion, minutes),
      completeTaskAction,
      () => loadAdvice(),
      currentTask,
      postponeTask,
      async () => {
        const [refreshedAdvice, refreshedReview] = await Promise.all([loadAdvice(), loadReview()]);
        return refreshedAdvice && refreshedReview ? refreshedAdvice : null;
      },
    );
    return payload;
  } catch (error) {
    if (adviceText) adviceText.textContent = `建议加载失败：${error.message || "请稍后重试"}`;
    renderSuggestion(null, suggestionContainer, startSuggestedFocus);
    return null;
  }
}

export async function loadAdviceWithAI(tasks) {
  const taskList = tasks.map(t => `• ${t.title} (${t.priority || 'medium'}优先级${t.due_at ? ', 截止' + new Date(t.due_at).toLocaleString('zh-CN') : ''})`).join('\n');

  const message = `请分析我的当前任务状态，给我一个今天的工作建议。

我的任务列表：
${taskList || '暂无任务'}

请考虑：
1. 任务的优先级和截止时间
2. 任务的预估时长
3. 当前时间和精力状态
4. 任务的依赖关系

请给出 1-2 个具体的下一步行动建议，帮助我今天高效工作。`;

  await sendToAgent(message);
}

export async function loadReview(options = {}) {
  lastReviewPayload = null;
  lastOpenTasks = [];
  renderTodayReviewMessage(reviewContainer, "正在加载今天的复盘…", "loading");
  try {
    const [payload, unfinishedTasks] = await Promise.all([
      loadTodayReviewPayload(requestJson, options),
      loadOpenTaskSummary(requestJson),
    ]);
    lastReviewPayload = payload;
    lastOpenTasks = unfinishedTasks;
    renderCachedTodayReview();
    return payload;
  } catch (error) {
    renderTodayReviewMessage(reviewContainer, `今天的复盘加载失败：${error.message || "请稍后重试"}`, "error");
    return null;
  }
}

export async function loadReviewWithAI(tasks) {
  const taskList = tasks.map(t => {
    const statusMap = { todo: '待办', doing: '进行中', done: '已完成', dropped: '已放弃' };
    return `• ${t.title} (${statusMap[t.status] || t.status}${t.due_at ? ', 截止' + new Date(t.due_at).toLocaleString('zh-CN') : ''})`;
  }).join('\n');

  const message = `请帮我复盘一下今天的工作状态。

任务列表：
${taskList || '暂无任务'}

请分析：
1. 已完成的任务和今天的成就
2. 进行中的任务
3. 过期/超期的任务及原因
4. 明天的工作重点
5. 改进建议

请给出一个简洁的复盘报告。`;

  await sendToAgent(message);
}
