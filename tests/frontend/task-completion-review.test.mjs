import assert from "node:assert/strict";
import test from "node:test";

import {
  createTaskCompletionReviewEntry,
  formatCompletionReviewSummary,
} from "../../src/momentum_agent/static/js/completion-review-entry.mjs";

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.listeners = new Map();
    this.attributes = {};
    this.textContent = "";
    this.className = "";
    this.disabled = false;
  }
  append(...nodes) { this.children.push(...nodes); }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  setAttribute(name, value) { this.attributes[name] = value; }
  async click() { return this.listeners.get("click")?.(); }
}

const fakeDocument = { createElement: (tagName) => new FakeElement(tagName) };

function textContent(element) {
  return [element.textContent, ...element.children.map(textContent)].filter(Boolean).join(" ");
}

test("completion summary uses review actual seconds and never substitutes estimated minutes", () => {
  const summary = formatCompletionReviewSummary({
    completed_count: 2,
    today_focus_actual_seconds: 83,
    completed_events: [{ estimated_minutes_reference: 120 }],
  });
  assert.equal(summary, "今天已完成 2 项 · 实际专注 1 分 23 秒");
  assert.doesNotMatch(summary, /120 分钟/);
});

test("completion review entry is accessible, safely renders text, and reloads only on explicit click", async () => {
  let reviewLoads = 0;
  const entry = createTaskCompletionReviewEntry({
    documentRef: fakeDocument,
    onReview: async () => {
      reviewLoads += 1;
      return { completed_count: 3, today_focus_actual_seconds: 3725 };
    },
  });

  assert.equal(entry.element.attributes.role, "region");
  assert.equal(entry.element.attributes["aria-label"], "今日收尾入口");
  assert.equal(entry.button.textContent, "查看今日收尾");
  assert.equal(reviewLoads, 0, "rendering the CTA does not create or reload review data");
  entry.updateMessage("<b>服务器原始完成消息</b>");
  assert.equal(entry.element.children[2].textContent, "<b>服务器原始完成消息</b>");
  assert.equal(entry.element.children[2].hidden, false);
  assert.doesNotMatch(textContent(entry.element), /<img/);

  entry.update({ completed_count: 2, today_focus_actual_seconds: 83 });
  assert.equal(entry.element.children[1].textContent, "今天已完成 2 项 · 实际专注 1 分 23 秒");
  entry.update({ completed_count: "<img src=x onerror=alert(1)>", today_focus_actual_seconds: "bad" });
  assert.equal(entry.element.children[1].textContent, "今天已完成 0 项 · 实际专注 0 秒");
  assert.doesNotMatch(textContent(entry.element), /<img/);

  await entry.button.click();
  assert.equal(reviewLoads, 1);
  assert.equal(entry.element.children[1].textContent, "今天已完成 3 项 · 实际专注 1 小时 2 分钟");
});
