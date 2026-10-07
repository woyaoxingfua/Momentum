const STATUS_LABELS = { todo: "待办", doing: "进行中" };
const postponeViewUnsubscribers = new WeakMap();
const focusEntryButtonsByContainer = new WeakMap();
const focusEntryButtons = new Set();
let focusEntryActive = false;

function clearFocusEntryButtons(container) {
  const buttons = focusEntryButtonsByContainer.get(container);
  if (!buttons) return;
  buttons.forEach((button) => focusEntryButtons.delete(button));
  focusEntryButtonsByContainer.delete(container);
}

function trackFocusEntryButton(container, button, enabled) {
  let buttons = focusEntryButtonsByContainer.get(container);
  if (!buttons) {
    buttons = new Set();
    focusEntryButtonsByContainer.set(container, buttons);
  }
  button.focusEntryEnabled = enabled;
  button.disabled = focusEntryActive || !enabled;
  buttons.add(button);
  focusEntryButtons.add(button);
}

export function setFocusEntryAvailability(active) {
  focusEntryActive = Boolean(active);
  focusEntryButtons.forEach((button) => {
    button.disabled = focusEntryActive || !button.focusEntryEnabled;
  });
}

function localDateString(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function getTodayReviewParams({ now = new Date(), timeZone } = {}) {
  const browserTimeZone = timeZone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  return { timeZone: browserTimeZone, localDate: localDateString(now) };
}

export function getTodayReviewUrl(options = {}) {
  const { timeZone, localDate } = getTodayReviewParams(options);
  const query = new URLSearchParams({ timeZone, localDate });
  return `/api/review?${query.toString()}`;
}

export function loadAdvicePayload(requestJson) {
  return requestJson("/api/advice");
}

export function saveSuggestionEstimate(requestJson, suggestion, estimatedMinutes) {
  if (!suggestion || suggestion.task_id == null || !["todo", "doing"].includes(suggestion.status)) {
    throw new Error("只有待办或进行中的建议任务可以修改预计时长");
  }
  if (!Number.isSafeInteger(estimatedMinutes) || estimatedMinutes < 1 || estimatedMinutes > 120) {
    throw new Error("预计时长必须是 1 至 120 分钟的整数");
  }
  return requestJson(`/api/tasks/${encodeURIComponent(suggestion.task_id)}`, {
    method: "PUT",
    body: JSON.stringify({ estimated_minutes: estimatedMinutes }),
  });
}

export function bindAdviceToSuggestion(payload) {
  const suggestion = payload?.suggestion || null;
  const advice = typeof payload?.advice === "string" ? payload.advice.trim() : "";
  if (!suggestion || suggestion.task_id == null) {
    return { suggestion, task_id: null, title: "", text: advice || "暂无建议" };
  }

  const title = String(suggestion.title || "").trim() || `任务 #${suggestion.task_id}`;
  const text = advice.includes(title)
    ? advice
    : `建议先从「${title}」开始，完成一个具体的小步骤。`;
  return { suggestion, task_id: suggestion.task_id, title, text };
}

export function loadTodayReviewPayload(requestJson, options = {}) {
  return requestJson(getTodayReviewUrl(options));
}

export async function loadOpenTaskSummary(requestJson) {
  const payloads = await Promise.all([
    requestJson("/api/tasks?status=todo"),
    requestJson("/api/tasks?status=doing"),
  ]);
  const seen = new Set();
  return payloads.flatMap((payload) => Array.isArray(payload?.tasks) ? payload.tasks : [])
    .filter((task) => {
      if (!task || !["todo", "doing"].includes(task.status) || task.id == null) return false;
      const id = String(task.id);
      if (seen.has(id)) return false;
      seen.add(id);
      return true;
    });
}

export function findBoundSuggestionTask(suggestion, tasks) {
  if (!suggestion || suggestion.task_id == null || !["todo", "doing"].includes(suggestion.status) || !Array.isArray(tasks)) return null;
  return tasks.find((task) => task && task.id != null
    && String(task.id) === String(suggestion.task_id)
    && ["todo", "doing"].includes(task.status)) || null;
}

export function formatActualFocusDuration(seconds) {
  const value = Number(seconds);
  const total = Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  if (hours) return `${hours} 小时 ${minutes} 分钟`;
  if (minutes) return remainder ? `${minutes} 分 ${remainder} 秒` : `${minutes} 分钟`;
  return `${remainder} 秒`;
}

function makeElement(tagName, className, text) {
  const element = document.createElement(tagName);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}

export function renderSuggestion(suggestion, container, onStartSuggestion, onSaveEstimate, onCompleteTask, onRefreshSuggestion, currentTask, onPostponeTask, onRefreshAfterPostpone) {
  if (!container) return;
  clearFocusEntryButtons(container);
  container.replaceChildren();
  if (!suggestion || typeof suggestion !== "object") {
    container.hidden = true;
    return;
  }

  const card = makeElement("div", "suggestion-card");
  const content = makeElement("div", "suggestion-content");
  const meta = makeElement("div", "suggestion-meta");
  const id = makeElement("span", "suggestion-task-id", `任务 #${suggestion.task_id}`);
  const status = makeElement(
    "span",
    `suggestion-status suggestion-status-${STATUS_LABELS[suggestion.status] ? suggestion.status : "other"}`,
    STATUS_LABELS[suggestion.status] || `状态：${suggestion.status || "未知"}`,
  );
  meta.append(id, status);
  content.append(meta, makeElement("h3", "suggestion-title", suggestion.title || "未命名任务"));
  const estimate = Number(suggestion.estimated_minutes);
  const estimateDisplay = makeElement("p", "suggestion-estimate");
  estimateDisplay.hidden = !Number.isFinite(estimate) || estimate <= 0;
  if (!estimateDisplay.hidden) estimateDisplay.textContent = `预估 ${estimate} 分钟`;
  content.append(estimateDisplay);

  let estimateInput = null;
  let saveEstimateButton = null;
  let estimateFeedback = null;
  const canEditEstimate = suggestion.task_id != null
    && ["todo", "doing"].includes(suggestion.status)
    && typeof onSaveEstimate === "function";
  if (canEditEstimate) {
    const edit = makeElement("div", "suggestion-estimate-edit");
    const label = makeElement("label", "suggestion-estimate-label", "调整预计时长（分钟）");
    estimateInput = makeElement("input", "suggestion-estimate-input");
    estimateInput.type = "number";
    estimateInput.min = "1";
    estimateInput.max = "120";
    estimateInput.step = "1";
    estimateInput.value = !estimateDisplay.hidden ? String(estimate) : "";
    label.append(estimateInput);
    saveEstimateButton = makeElement("button", "suggestion-estimate-save", "保存");
    saveEstimateButton.type = "button";
    estimateFeedback = makeElement("p", "suggestion-estimate-feedback");
    estimateFeedback.hidden = true;
    estimateFeedback.setAttribute("role", "status");
    estimateFeedback.setAttribute("aria-live", "polite");
    edit.append(label, saveEstimateButton);
    content.append(edit, estimateFeedback);
  }

  const actions = makeElement("div", "suggestion-actions");
  const button = makeElement("button", "primary suggestion-start", "开始专注");
  button.type = "button";
  trackFocusEntryButton(container, button, typeof onStartSuggestion === "function");
  button.addEventListener("click", async () => {
    if (typeof onStartSuggestion !== "function" || button.disabled) return;
    button.disabled = true;
    try {
      await onStartSuggestion(suggestion);
    } finally {
      button.disabled = focusEntryActive || !button.focusEntryEnabled;
    }
  });
  actions.append(button);
  if (canEditEstimate) {
    saveEstimateButton.addEventListener("click", async () => {
      if (saveEstimateButton.disabled) return;
      const requestedMinutes = Number(estimateInput.value);
      if (!Number.isSafeInteger(requestedMinutes) || requestedMinutes < 1 || requestedMinutes > 120) {
        estimateFeedback.textContent = "请输入 1 至 120 分钟的整数。";
        estimateFeedback.setAttribute("role", "alert");
        estimateFeedback.hidden = false;
        return;
      }

      saveEstimateButton.disabled = true;
      button.disabled = true;
      estimateFeedback.textContent = "正在保存…";
      estimateFeedback.setAttribute("role", "status");
      estimateFeedback.hidden = false;
      try {
        await onSaveEstimate(suggestion, requestedMinutes);
        suggestion.estimated_minutes = requestedMinutes;
        estimateDisplay.textContent = `预估 ${requestedMinutes} 分钟`;
        estimateDisplay.hidden = false;
        estimateInput.value = String(requestedMinutes);
        estimateFeedback.textContent = `已保存：预估 ${requestedMinutes} 分钟`;
      } catch (error) {
        estimateFeedback.textContent = `保存失败，未保存：${error?.message || "请稍后重试"}`;
        estimateFeedback.setAttribute("role", "alert");
      } finally {
        saveEstimateButton.disabled = false;
        button.disabled = focusEntryActive || !button.focusEntryEnabled;
      }
    });
  }

  const canComplete = suggestion.task_id != null
    && ["todo", "doing"].includes(suggestion.status)
    && typeof onCompleteTask === "function";
  if (canComplete) {
    const completionButton = makeElement("button", "primary suggestion-complete", "完成此任务");
    completionButton.type = "button";
    const retryButton = makeElement("button", "suggestion-completion-retry", "使用原请求重试");
    retryButton.type = "button";
    retryButton.hidden = true;
    const completionFeedback = makeElement("p", "suggestion-completion-feedback");
    completionFeedback.hidden = true;
    completionFeedback.setAttribute("role", "status");
    completionFeedback.setAttribute("aria-live", "polite");
    content.append(completionFeedback);
    actions.append(completionButton, retryButton);

    const getPendingIntent = () => {
      let intent = null;
      try { intent = onCompleteTask.getPendingIntent?.(suggestion.task_id) || null; }
      catch { return null; }
      return intent && String(intent.task_id) === String(suggestion.task_id) ? intent : null;
    };
    const setFeedback = (message, kind = "status") => {
      completionFeedback.textContent = message;
      completionFeedback.hidden = !message;
      completionFeedback.setAttribute("role", kind === "error" ? "alert" : "status");
    };
    const showPendingIntent = (intent) => {
      const status = intent?.status;
      const corrupt = status === "corrupt";
      const sending = status === "sending";
      completionButton.disabled = Boolean(intent);
      completionButton.textContent = intent ? "完成未确认" : "完成此任务";
      retryButton.hidden = !intent || sending || corrupt || typeof onCompleteTask.retry !== "function";
      retryButton.disabled = sending || corrupt || typeof onCompleteTask.retry !== "function";
      if (!intent) return;
      const message = corrupt
        ? "本地完成请求记录无法验证；为避免生成新 UUID，已停止发送。"
        : sending
          ? "完成请求处理中，尚未确认；页面加载不会自动重发。"
          : "完成结果尚未确认；请显式使用原请求重试。";
      setFeedback(message, corrupt ? "error" : "status");
    };
    const showError = (error) => {
      const status = Number(error?.status);
      const prefix = Number.isInteger(status) ? `完成失败（HTTP ${status}）` : "完成失败";
      const detail = error?.message || error?.payload?.error || "请稍后重试";
      setFeedback(`${prefix}：${detail}。任务未标记为完成。`, "error");
    };
    const markConfirmed = () => {
      status.textContent = "已完成";
      status.className = "suggestion-status suggestion-status-done";
      completionButton.hidden = true;
      retryButton.hidden = true;
      retryButton.disabled = true;
      button.focusEntryEnabled = false;
      button.disabled = true;
      if (estimateInput) estimateInput.disabled = true;
      if (saveEstimateButton) saveEstimateButton.disabled = true;
      setFeedback("任务已完成，正在刷新任务状态、今日复盘和建议…");
    };
    let completionBusy = false;
    const runCompletion = async (isRetry) => {
      if (completionBusy) return;
      const currentIntent = getPendingIntent();
      if (isRetry) {
        if (!currentIntent || currentIntent.status !== "uncertain" || typeof onCompleteTask.retry !== "function") {
          showPendingIntent(currentIntent);
          return;
        }
      } else if (currentIntent) {
        showPendingIntent(currentIntent);
        return;
      }

      completionBusy = true;
      completionButton.disabled = true;
      retryButton.disabled = true;
      setFeedback(isRetry ? "正在使用原请求重试…" : "正在完成此任务…");
      let result;
      try {
        result = isRetry
          ? await onCompleteTask.retry(suggestion.task_id, retryButton)
          : await onCompleteTask(suggestion.task_id, completionButton);
      } catch (error) {
        result = { status: "error", error };
      } finally {
        completionBusy = false;
      }

      if (result?.status === "success") {
        markConfirmed();
        if (typeof onRefreshSuggestion !== "function") {
          setFeedback("任务已完成；建议尚未刷新，请刷新页面查看最新建议。");
          return;
        }
        try {
          const refreshed = await onRefreshSuggestion();
          if (refreshed == null || refreshed === false) {
            setFeedback("任务已完成；建议刷新失败，请稍后刷新查看最新建议。", "error");
          }
        } catch (error) {
          setFeedback(`任务已完成；建议刷新失败：${error?.message || "请稍后刷新"}`, "error");
        }
        return;
      }

      const intent = getPendingIntent();
      if (result?.status === "uncertain" || result?.status === "pending" || intent) {
        const viewIntent = intent || result?.intent || null;
        if (viewIntent && String(viewIntent.task_id) === String(suggestion.task_id)) {
          showPendingIntent(viewIntent);
          if (viewIntent.status === "uncertain" && !intent) {
            retryButton.hidden = true;
            retryButton.disabled = true;
            setFeedback("完成结果尚未确认，但本地未找到可验证的原请求；为避免重复完成，未发起新请求。", "error");
          }
        } else {
          completionButton.disabled = true;
          retryButton.hidden = true;
          setFeedback("完成结果尚未确认；为避免生成新请求，当前操作已锁定。", "error");
        }
        if (result?.status === "error" && result.error) showError(result.error);
        return;
      }

      completionButton.disabled = false;
      retryButton.hidden = true;
      retryButton.disabled = true;
      showError(result?.error || new Error("完成请求未确认"));
    };

    completionButton.addEventListener("click", () => runCompletion(false));
    retryButton.addEventListener("click", () => runCompletion(true));
    showPendingIntent(getPendingIntent());
  }

  const hasCurrentDueDate = currentTask?.due_at != null && String(currentTask.due_at).trim() !== "";
  const canPostpone = suggestion.task_id != null
    && ["todo", "doing"].includes(suggestion.status)
    && currentTask?.id != null
    && String(currentTask.id) === String(suggestion.task_id)
    && ["todo", "doing"].includes(currentTask.status)
    && hasCurrentDueDate
    && typeof onPostponeTask === "function";
  if (canPostpone) {
    const postponeButton = makeElement("button", "suggestion-postpone", "一天后再处理");
    postponeButton.type = "button";
    postponeButton.setAttribute("aria-label", `一天后再处理：${suggestion.title || `任务 #${suggestion.task_id}`}`);
    const retryButton = makeElement("button", "suggestion-postpone-retry", "使用原请求重试");
    retryButton.type = "button";
    retryButton.hidden = true;
    const feedback = makeElement("p", "suggestion-postpone-feedback suggestion-completion-feedback", "");
    feedback.hidden = true;
    feedback.setAttribute("role", "status");
    feedback.setAttribute("aria-live", "polite");
    actions.append(postponeButton, retryButton, feedback);

    const setFeedback = (message, kind = "status") => {
      feedback.textContent = message;
      feedback.hidden = !message;
      feedback.setAttribute("role", kind === "error" ? "alert" : "status");
    };
    const getPendingIntent = () => {
      try {
        const intent = onPostponeTask.getPendingIntent?.(suggestion.task_id) || null;
        return intent && String(intent.task_id) === String(suggestion.task_id) ? intent : null;
      } catch { return null; }
    };
    const showPendingIntent = (intent) => {
      if (!intent) {
        postponeButton.disabled = false;
        postponeButton.textContent = "一天后再处理";
        retryButton.hidden = true;
        retryButton.disabled = true;
        return;
      }
      const sending = intent.status === "sending";
      postponeButton.disabled = true;
      postponeButton.textContent = sending ? "顺延处理中" : "顺延结果未确认";
      retryButton.hidden = intent.status !== "uncertain" || typeof onPostponeTask.retry !== "function";
      retryButton.disabled = retryButton.hidden;
      setFeedback(intent.last_error_message || (sending
        ? "顺延请求正在处理；页面不会自动重发。"
        : "顺延结果尚未确认；只能显式使用原请求重试。"));
    };

    let postponing = false;
    const performPostpone = async (isRetry) => {
      if (postponing) return;
      const intent = getPendingIntent();
      if (isRetry) {
        if (!intent || intent.status !== "uncertain" || typeof onPostponeTask.retry !== "function") {
          showPendingIntent(intent);
          return;
        }
      } else if (intent) {
        showPendingIntent(intent);
        return;
      }

      postponing = true;
      postponeButton.disabled = true;
      retryButton.disabled = true;
      setFeedback(isRetry ? "正在使用原请求重试…" : "正在顺延一天…");
      try {
        const result = isRetry
          ? await onPostponeTask.retry(currentTask)
          : await onPostponeTask(currentTask);
        if (result?.postponePending) {
          const pendingIntent = result.intent || getPendingIntent();
          if (pendingIntent) showPendingIntent(pendingIntent);
          else {
            postponeButton.disabled = true;
            setFeedback("已有顺延请求处理中；为避免重复请求，未再次发送。", "error");
          }
          return;
        }
        if (result?.postponeConfirmed !== true) {
          const pendingIntent = getPendingIntent();
          if (pendingIntent) showPendingIntent(pendingIntent);
          else {
            postponeButton.disabled = true;
            setFeedback("顺延结果尚未确认；为避免重复顺延，入口已锁定。", "error");
          }
          return;
        }

        if (result.due_at) currentTask.due_at = result.due_at;
        postponeButton.disabled = true;
        postponeButton.textContent = "已顺延一天";
        retryButton.hidden = true;
        retryButton.disabled = true;
        setFeedback(result.postponeRefreshWarning || "已顺延一天，正在刷新任务、今日状态和建议。", result.postponeRefreshWarning ? "error" : "status");
        if (typeof onRefreshAfterPostpone === "function") {
          try {
            const refreshed = await onRefreshAfterPostpone();
            if (refreshed == null || refreshed === false) {
              setFeedback("已顺延一天，但今日状态或建议刷新失败；请稍后刷新查看。", "error");
            }
          } catch (error) {
            setFeedback(`已顺延一天，但刷新失败：${error?.message || "请稍后刷新查看"}`, "error");
          }
        }
      } catch (error) {
        const pendingIntent = error?.pendingIntent || getPendingIntent();
        if (pendingIntent) {
          showPendingIntent(pendingIntent);
          if (error?.message) setFeedback(error.message, pendingIntent.status === "uncertain" ? "status" : "error");
        } else {
          postponeButton.disabled = false;
          postponeButton.textContent = "一天后再处理";
          retryButton.hidden = true;
          retryButton.disabled = true;
          const statusCode = Number(error?.status);
          const prefix = Number.isInteger(statusCode) ? `顺延失败（HTTP ${statusCode}）` : "顺延失败";
          setFeedback(`${prefix}，未修改：${error?.message || "请稍后重试"}`, "error");
        }
      } finally {
        postponing = false;
      }
    };

    postponeButton.addEventListener("click", () => performPostpone(false));
    retryButton.addEventListener("click", () => performPostpone(true));
    showPendingIntent(getPendingIntent());
  }

  card.append(content, actions);
  container.append(card);
  container.hidden = false;
}

export function renderAdvicePayload(payload, adviceContainer, suggestionContainer, onStartSuggestion, onSaveEstimate, onCompleteTask, onRefreshSuggestion, currentTask, onPostponeTask, onRefreshAfterPostpone) {
  const boundAdvice = bindAdviceToSuggestion(payload);
  if (adviceContainer) adviceContainer.textContent = boundAdvice.text;
  renderSuggestion(boundAdvice.suggestion, suggestionContainer, onStartSuggestion, onSaveEstimate, onCompleteTask, onRefreshSuggestion, currentTask, onPostponeTask, onRefreshAfterPostpone);
  return boundAdvice;
}

export function renderTodayReviewMessage(container, message, kind = "loading") {
  if (!container) return;
  clearFocusEntryButtons(container);
  container.replaceChildren();
  container.hidden = false;
  if (typeof container.setAttribute === "function") {
    container.setAttribute("aria-busy", kind === "loading" ? "true" : "false");
  }
  container.append(makeElement("p", `today-review-message ${kind}`, message));
}

function formatCompletionTime(value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN", {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  });
}

export function renderTodayReview(container, review, { unfinishedTasks, pendingCompletions, completionNotice, onPostpone, onEditTask, onCompleteTask, onStartTask } = {}) {
  if (!container) return;
  postponeViewUnsubscribers.get(container)?.();
  postponeViewUnsubscribers.delete(container);
  clearFocusEntryButtons(container);
  container.replaceChildren();
  container.hidden = false;
  if (typeof container.setAttribute === "function") container.setAttribute("aria-busy", "false");

  const header = makeElement("div", "today-review-header");
  header.append(
    makeElement("span", "insight-label", "当天复盘"),
    makeElement("h2", "today-review-date", review.localDate || "今天"),
  );
  container.append(header);

  const completedCount = Number.isFinite(Number(review.completed_count))
    ? Math.max(0, Math.floor(Number(review.completed_count)))
    : 0;
  const actualSeconds = Number.isFinite(Number(review.today_focus_actual_seconds))
    ? Math.max(0, Math.floor(Number(review.today_focus_actual_seconds)))
    : 0;
  const sessionCount = Number.isFinite(Number(review.today_focus_session_count))
    ? Math.max(0, Math.floor(Number(review.today_focus_session_count)))
    : 0;
  const metrics = makeElement("div", "today-review-metrics");
  for (const [label, value] of [
    ["完成事件", `${completedCount} 条`],
    ["实际专注", formatActualFocusDuration(actualSeconds)],
    ["专注场次", `${sessionCount} 次`],
  ]) {
    const metric = makeElement("div", "today-review-metric");
    metric.append(makeElement("span", "today-review-metric-label", label));
    metric.append(makeElement("strong", "today-review-metric-value", value));
    metrics.append(metric);
  }
  container.append(metrics);

  const sectionTitle = makeElement("h3", "today-review-section-title", "完成记录");
  container.append(sectionTitle);
  container.append(makeElement(
    "p",
    "today-review-note",
    "同一任务重开后再次完成，会新增一条完成记录并另计。",
  ));
  const events = Array.isArray(review.completed_events) ? review.completed_events : [];
  if (completedCount === 0) {
    container.append(makeElement("p", "today-review-empty", "今天还没有任务完成。"));
  } else if (events.length === 0) {
    container.append(makeElement("p", "today-review-empty", "完成事件详情暂不可用。"));
  } else {
    const list = makeElement("ul", "today-review-events");
    for (const event of events) {
      const item = makeElement("li", "today-review-event");
      const title = event.title || `任务 #${event.task_id}`;
      item.append(makeElement("span", "today-review-event-title", title));
      const details = [];
      const time = formatCompletionTime(event.completed_at);
      if (time) details.push(time);
      const estimate = Number(event.estimated_minutes_reference);
      if (Number.isFinite(estimate) && estimate > 0) details.push(`预估参考 ${estimate} 分钟`);
      if (details.length) item.append(makeElement("span", "today-review-event-reference", details.join(" · ")));
      list.append(item);
    }
    container.append(list);
  }

  if (Array.isArray(unfinishedTasks)) {
    renderUnfinishedTaskSummary(container, unfinishedTasks, onPostpone, onEditTask, onCompleteTask, onStartTask);
  }
  if (Array.isArray(pendingCompletions)) {
    const openIds = new Set((unfinishedTasks || []).map((task) => String(task?.id)));
    renderPendingCompletionSummary(
      container,
      pendingCompletions.filter((intent) => !openIds.has(String(intent?.task_id))),
      onCompleteTask,
    );
  }
  if (completionNotice?.message) {
    const notice = makeElement("p", "today-review-completion-feedback", completionNotice.message);
    notice.setAttribute("role", completionNotice.kind === "error" ? "alert" : "status");
    notice.setAttribute("aria-live", "polite");
    container.append(notice);
  }

  if (review.focus_attribution_note) {
    container.append(makeElement("p", "today-review-note", review.focus_attribution_note));
  }
}

function formatExistingDueDate(value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
}

function completionIntentMessage(intent) {
  if (intent?.status === "corrupt") return "本地完成请求记录无法验证；为避免生成新 UUID 重复完成，已停止发送。";
  if (intent?.status === "sending") return "完成请求处理中，尚未确认；页面加载不会自动重发。";
  return "完成结果尚未确认；请显式使用原请求重试，页面加载不会自动重发。";
}

function renderPendingCompletionSummary(container, intents, onCompleteTask) {
  if (!intents.length) return;
  container.append(makeElement("h3", "today-review-section-title", "尚未确认的完成请求"));
  container.append(makeElement("p", "today-review-open-summary-note", "刷新或重新打开页面不会自动重发；只能显式重试原请求。"));
  const list = makeElement("ul", "today-review-open-tasks today-review-pending-completions");
  for (const intent of intents) {
    const item = makeElement("li", "today-review-open-task today-review-pending-completion");
    const content = makeElement("div", "today-review-open-task-content");
    content.append(makeElement("strong", "today-review-open-task-title", `任务 #${intent.task_id}`));
    content.append(makeElement("span", "today-review-open-task-status", "完成未确认"));
    const actions = makeElement("div", "today-review-open-task-actions");
    const retry = makeElement("button", "today-review-completion-retry", "使用原请求重试");
    retry.type = "button";
    retry.disabled = intent.status === "sending" || intent.status === "corrupt" || typeof onCompleteTask?.retry !== "function";
    retry.setAttribute("aria-label", `使用原请求重试完成：任务 #${intent.task_id}`);
    const feedback = makeElement("p", "today-review-completion-feedback", completionIntentMessage(intent));
    feedback.setAttribute("role", "status");
    feedback.setAttribute("aria-live", "polite");
    if (typeof onCompleteTask?.retry === "function") {
      retry.addEventListener("click", async () => {
        if (retry.disabled) return;
        retry.disabled = true;
        try { await onCompleteTask.retry(intent.task_id, retry); }
        finally {
          const current = onCompleteTask.getPendingIntent?.(intent.task_id);
          if (current) {
            feedback.textContent = completionIntentMessage(current);
            retry.disabled = current.status === "sending" || current.status === "corrupt";
          } else {
            retry.hidden = true;
          }
        }
      });
    }
    actions.append(retry, feedback);
    item.append(content, actions);
    list.append(item);
  }
  container.append(list);
}

function renderUnfinishedTaskSummary(container, tasks, onPostpone, onEditTask, onCompleteTask, onStartTask) {
  const activeTasks = tasks.filter((task) => task && ["todo", "doing"].includes(task.status));
  container.append(makeElement("h3", "today-review-section-title", "尚未完成"));
  container.append(makeElement(
    "p",
    "today-review-open-summary-note",
    "包含待办与进行中的任务；不按今天到期筛选。",
  ));

  if (activeTasks.length === 0) {
    container.append(makeElement("p", "today-review-empty", "目前没有待办或进行中的任务。"));
    return;
  }

  const list = makeElement("ul", "today-review-open-tasks");
  const controlsByTaskId = new Map();
  for (const task of activeTasks) {
    const item = makeElement("li", "today-review-open-task");
    const content = makeElement("div", "today-review-open-task-content");
    const title = task.title || `任务 #${task.id}`;
    content.append(makeElement("strong", "today-review-open-task-title", title));
    content.append(makeElement("span", "today-review-open-task-status", STATUS_LABELS[task.status]));

    const actions = makeElement("div", "today-review-open-task-actions");
    let initialCompletionIntent = null;
    try { initialCompletionIntent = onCompleteTask?.getPendingIntent?.(task.id) || null; } catch { /* A broken local store never starts a completion write. */ }
    const completeButton = makeElement("button", "today-review-complete", initialCompletionIntent ? "完成未确认" : "完成");
    completeButton.type = "button";
    completeButton.disabled = typeof onCompleteTask !== "function" || Boolean(initialCompletionIntent);
    completeButton.setAttribute("aria-label", `${initialCompletionIntent ? "完成未确认" : "完成"}：${title}`);
    const completionRetryButton = makeElement("button", "today-review-completion-retry", "使用原请求重试");
    completionRetryButton.type = "button";
    completionRetryButton.hidden = !initialCompletionIntent || initialCompletionIntent.status === "corrupt";
    completionRetryButton.disabled = !initialCompletionIntent || initialCompletionIntent.status === "sending" || typeof onCompleteTask?.retry !== "function";
    completionRetryButton.setAttribute("aria-label", `使用原请求重试完成：${title}`);
    const completionFeedback = makeElement("p", "today-review-completion-feedback", "");
    completionFeedback.hidden = true;
    completionFeedback.setAttribute("role", "status");
    completionFeedback.setAttribute("aria-live", "polite");
    function refreshCompletionState(fallbackMessage = "") {
      const intent = onCompleteTask?.getPendingIntent?.(task.id) || null;
      const prior = onCompleteTask?.getCompletionFeedback?.(task.id) || null;
      if (intent) {
        completeButton.textContent = "完成未确认";
        completeButton.disabled = true;
        completeButton.setAttribute("aria-label", `完成未确认：${title}`);
        completionRetryButton.hidden = intent.status === "corrupt";
        completionRetryButton.disabled = intent.status === "sending" || intent.status === "corrupt" || typeof onCompleteTask?.retry !== "function";
        completionFeedback.textContent = prior?.message || completionIntentMessage(intent);
        completionFeedback.setAttribute("role", prior?.kind === "error" ? "alert" : "status");
        completionFeedback.hidden = false;
      } else {
        completeButton.textContent = "完成";
        completeButton.disabled = typeof onCompleteTask !== "function";
        completeButton.setAttribute("aria-label", `完成：${title}`);
        completionRetryButton.hidden = true;
        completionRetryButton.disabled = true;
        completionFeedback.textContent = prior?.message || fallbackMessage;
        completionFeedback.setAttribute("role", prior?.kind === "error" ? "alert" : "status");
        completionFeedback.hidden = !completionFeedback.textContent;
      }
    }
    refreshCompletionState();
    if (typeof onCompleteTask === "function") {
      let completing = false;
      const performCompletion = async (isRetry) => {
        const target = isRetry ? completionRetryButton : completeButton;
        if (completing || target.disabled) return;
        completing = true;
        completeButton.disabled = true;
        completionRetryButton.disabled = true;
        completeButton.classList?.add("is-pending");
        completeButton.setAttribute("aria-busy", "true");
        let result;
        try {
          result = isRetry
            ? await onCompleteTask.retry?.(task.id, completeButton)
            : await onCompleteTask(task.id, completeButton);
        } catch (error) {
          result = { status: "error", error };
        } finally {
          completing = false;
          completeButton.classList?.remove("is-pending");
          completeButton.setAttribute("aria-busy", "false");
          const failureMessage = result?.status === "error"
            ? (result.error?.message || "完成请求失败。")
            : result?.status === "uncertain" || result?.status === "pending"
              ? "完成结果尚未确认；请显式使用原请求重试，页面加载不会自动重发。"
              : result?.status === "success"
                ? (result.response?.message || "任务已完成。")
                : "";
          refreshCompletionState(failureMessage);
        }
      };
      completeButton.addEventListener("click", () => performCompletion(false));
      completionRetryButton.addEventListener("click", () => performCompletion(true));
    }
    actions.append(completeButton, completionRetryButton, completionFeedback);

    const startFocusButton = makeElement("button", "today-review-start-focus primary", "开始专注");
    startFocusButton.type = "button";
    startFocusButton.setAttribute("aria-label", `开始专注：${title}`);
    trackFocusEntryButton(container, startFocusButton, typeof onStartTask === "function");
    if (typeof onStartTask === "function") {
      let starting = false;
      startFocusButton.addEventListener("click", async () => {
        if (starting || startFocusButton.disabled || focusEntryActive) return;
        starting = true;
        startFocusButton.disabled = true;
        startFocusButton.classList?.add("is-pending");
        startFocusButton.setAttribute("aria-busy", "true");
        try {
          await onStartTask(task);
        } finally {
          starting = false;
          startFocusButton.classList?.remove("is-pending");
          startFocusButton.setAttribute("aria-busy", "false");
          startFocusButton.disabled = focusEntryActive || !startFocusButton.focusEntryEnabled;
        }
      });
    }
    actions.append(startFocusButton);

    if (task.due_at) {
      const dueLabel = makeElement("span", "today-review-open-task-due", `现有截止日：${formatExistingDueDate(task.due_at)}`);
      content.append(dueLabel);
      const button = makeElement("button", "today-review-postpone", "顺延现有截止日 1 天");
      button.type = "button";
      button.disabled = typeof onPostpone !== "function";
      button.setAttribute("aria-label", `顺延现有截止日 1 天：${title}`);
      const feedback = makeElement("p", "today-review-postpone-feedback", "");
      feedback.hidden = true;
      feedback.setAttribute("role", "alert");
      feedback.setAttribute("aria-live", "polite");
      const retryButton = makeElement("button", "today-review-postpone-retry", "使用原请求再试");
      retryButton.type = "button";
      retryButton.hidden = true;
      retryButton.disabled = typeof onPostpone?.retry !== "function";
      retryButton.setAttribute("aria-label", `使用原请求再试：${title}`);

      const controls = { button, retryButton, feedback, dueLabel, task, title };
      controlsByTaskId.set(String(task.id), controls);
      function showPending(intent) {
        if (!intent) {
          button.disabled = typeof onPostpone !== "function";
          button.textContent = "顺延现有截止日 1 天";
          button.setAttribute("aria-label", `顺延现有截止日 1 天：${title}`);
          retryButton.hidden = true;
          retryButton.disabled = true;
          return;
        }
        const sending = intent.status === "sending";
        button.disabled = true;
        button.textContent = sending ? "顺延处理中" : "顺延未决";
        button.setAttribute("aria-label", `${sending ? "顺延处理中" : "顺延未决"}：${title}`);
        feedback.textContent = intent.last_error_message
          || (sending ? "该顺延请求正在处理；请勿启动第二个顺延意图。" : "该顺延请求尚未确认。请显式使用原请求再试。GET 截止日不能证明请求或 updated event 已结算。");
        feedback.hidden = false;
        retryButton.hidden = sending || typeof onPostpone?.retry !== "function";
        retryButton.disabled = sending || typeof onPostpone?.retry !== "function";
      }

      let initialIntent = null;
      try { initialIntent = onPostpone?.getPendingIntent?.(task.id) || null; } catch { /* A broken local store never starts a write. */ }
      showPending(initialIntent);
      if (typeof onPostpone === "function") {
        let pending = false;
        const perform = async (isRetry) => {
          if (pending || (isRetry ? retryButton.disabled : button.disabled)) return;
          pending = true;
          button.disabled = true;
          retryButton.disabled = true;
          button.classList?.add("is-pending");
          button.setAttribute("aria-busy", "true");
          try {
            const updatedTask = isRetry
              ? await onPostpone.retry(task)
              : await onPostpone(task);
            if (updatedTask?.postponePending) {
              const intent = updatedTask.intent || onPostpone.getPendingIntent?.(task.id);
              if (intent) showPending(intent);
              else {
                feedback.textContent = "已有顺延请求处理中；为避免重复请求，未再次发送。";
                feedback.hidden = false;
                button.disabled = true;
              }
            } else if (updatedTask?.postponeConfirmed || updatedTask?.due_at) {
              if (updatedTask.due_at) {
                task.due_at = updatedTask.due_at;
                dueLabel.textContent = `现有截止日：${formatExistingDueDate(updatedTask.due_at)}`;
              }
              button.textContent = "再顺延 1 天";
              button.setAttribute("aria-label", `再顺延 1 天：${title}`);
              button.disabled = false;
              retryButton.hidden = true;
              retryButton.disabled = true;
              feedback.textContent = updatedTask.postponeRefreshWarning || "已顺延 1 天。再次点击将创建独立的新顺延请求。";
              feedback.hidden = false;
            } else {
              feedback.textContent = "顺延结果尚未确认；为避免重复顺延，普通顺延入口已锁定。";
              feedback.hidden = false;
              button.disabled = true;
              retryButton.hidden = false;
              retryButton.disabled = typeof onPostpone.retry !== "function";
            }
          } catch (error) {
            if (error?.reconciledTask?.due_at) {
              task.due_at = error.reconciledTask.due_at;
              dueLabel.textContent = `现有截止日：${formatExistingDueDate(error.reconciledTask.due_at)}`;
            }
            feedback.textContent = error?.message || "顺延失败，请稍后重试。";
            feedback.hidden = false;
            const intent = error?.pendingIntent || onPostpone.getPendingIntent?.(task.id);
            if (intent) showPending(intent);
            else {
              button.disabled = false;
              retryButton.hidden = true;
              retryButton.disabled = true;
            }
            // Keep the precise server/error explanation visible after applying stored state.
            feedback.textContent = error?.message || feedback.textContent;
            feedback.hidden = false;
          } finally {
            pending = false;
            button.classList?.remove("is-pending");
            button.setAttribute("aria-busy", "false");
            if (button.textContent === "顺延处理中") {
              const intent = onPostpone.getPendingIntent?.(task.id);
              showPending(intent);
            }
            if (!retryButton.hidden && !onPostpone.getPendingIntent?.(task.id)) retryButton.hidden = true;
          }
        };
        button.addEventListener("click", async () => {
          if (pending || button.disabled) return;
          await perform(false);
        });
        retryButton.addEventListener("click", async () => {
          if (pending || retryButton.disabled) return;
          await perform(true);
        });
      }
      actions.append(button, retryButton, feedback);
    } else {
      content.append(makeElement("span", "today-review-open-task-unavailable", "未设置截止日，无法顺延。"));
      const button = makeElement("button", "today-review-set-due", "设置截止时间");
      button.type = "button";
      button.disabled = typeof onEditTask !== "function";
      button.setAttribute("aria-label", `设置截止时间：${title}`);
      if (typeof onEditTask === "function") {
        button.addEventListener("click", () => {
          if (!button.disabled) onEditTask(task);
        });
      }
      actions.append(button);
    }

    item.append(content, actions);
    list.append(item);
  }
  container.append(list);

  if (typeof onPostpone?.subscribe === "function") {
    const unsubscribe = onPostpone.subscribe((event) => {
      const controls = controlsByTaskId.get(String(event.task_id));
      if (!controls) return;
      if (event.type === "intent") {
        const intent = event.intent;
        controls.button.disabled = true;
        controls.button.textContent = intent.status === "sending" ? "顺延处理中" : "顺延未决";
        controls.button.setAttribute("aria-label", `${intent.status === "sending" ? "顺延处理中" : "顺延未决"}：${controls.title}`);
        controls.feedback.textContent = intent.last_error_message || "该顺延请求尚未确认。请显式使用原请求再试；GET 截止日不能证明请求或 updated event 已结算。";
        controls.feedback.hidden = false;
        controls.retryButton.hidden = intent.status === "sending" || typeof onPostpone.retry !== "function";
        controls.retryButton.disabled = intent.status === "sending" || typeof onPostpone.retry !== "function";
      } else if (event.outcome === "success") {
        if (event.task?.due_at) {
          controls.task.due_at = event.task.due_at;
          controls.dueLabel.textContent = `现有截止日：${formatExistingDueDate(event.task.due_at)}`;
        }
        controls.button.textContent = "再顺延 1 天";
        controls.button.setAttribute("aria-label", `再顺延 1 天：${controls.title}`);
        controls.button.disabled = false;
        controls.retryButton.hidden = true;
        controls.retryButton.disabled = true;
        controls.feedback.textContent = "";
        controls.feedback.hidden = true;
      } else {
        controls.button.disabled = false;
        controls.button.textContent = "顺延现有截止日 1 天";
        controls.retryButton.hidden = true;
        controls.retryButton.disabled = true;
        if (event.error?.message) {
          controls.feedback.textContent = event.error.message;
          controls.feedback.hidden = false;
        }
      }
    });
    postponeViewUnsubscribers.set(container, unsubscribe);
  }
}
