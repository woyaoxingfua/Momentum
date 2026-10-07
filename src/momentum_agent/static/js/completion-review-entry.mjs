import { formatActualFocusDuration } from "./advice-review.mjs";

function nonNegativeInteger(value) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? Math.floor(number) : 0;
}

export function formatCompletionReviewSummary(review) {
  if (!review || typeof review !== "object") {
    return "任务已完成。查看今天已完成的任务与实际专注投入。";
  }
  const completedCount = nonNegativeInteger(review.completed_count);
  const actualSeconds = nonNegativeInteger(review.today_focus_actual_seconds);
  return `今天已完成 ${completedCount} 项 · 实际专注 ${formatActualFocusDuration(actualSeconds)}`;
}

export function createTaskCompletionReviewEntry({ onReview, documentRef = globalThis.document } = {}) {
  if (!documentRef || typeof documentRef.createElement !== "function") return null;

  const element = documentRef.createElement("section");
  element.className = "insight task-completion-review-entry";
  element.setAttribute("role", "region");
  element.setAttribute("aria-label", "今日收尾入口");
  element.setAttribute("aria-live", "polite");

  const label = documentRef.createElement("span");
  label.className = "insight-label";
  label.textContent = "今日收尾";

  const summary = documentRef.createElement("p");
  summary.textContent = formatCompletionReviewSummary(null);

  const message = documentRef.createElement("p");
  message.className = "task-completion-success-message";
  message.setAttribute("role", "status");
  message.setAttribute("aria-live", "polite");
  message.hidden = true;

  const button = documentRef.createElement("button");
  button.type = "button";
  button.className = "primary";
  button.textContent = "查看今日收尾";
  button.addEventListener("click", async () => {
    if (typeof onReview !== "function") return;
    button.disabled = true;
    try {
      const review = await onReview();
      if (review !== undefined) summary.textContent = formatCompletionReviewSummary(review);
    } finally {
      button.disabled = false;
    }
  });

  element.append(label, summary, message, button);
  return {
    element,
    button,
    updateMessage(value) {
      message.textContent = value == null ? "" : String(value);
      message.hidden = message.textContent === "";
    },
    update(review) {
      summary.textContent = formatCompletionReviewSummary(review);
    },
  };
}
