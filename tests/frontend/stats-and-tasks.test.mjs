import assert from "node:assert/strict";
import test from "node:test";

// stats.js / tasks.js 会在模块初始化时做一次异步刷新（依赖 localStorage 与 fetch）。
// Node 里没有这两个全局对象，直接静态 import 会在测试结束后抛 unhandledRejection
// 并被 node --test 判为整个文件失败。所以先打桩，再动态 import。
globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
};
globalThis.fetch = () => new Promise(() => {});  // 永不 resolve：不产生未处理的拒绝
globalThis.window = {
  addEventListener: () => {},
  removeEventListener: () => {},
  setInterval: () => 0,
  clearInterval: () => {},
  setTimeout: () => 0,
  clearTimeout: () => {},
  matchMedia: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
  location: { search: "", href: "http://localhost/app", pathname: "/app", hash: "" },
  history: { replaceState: () => {}, state: null },
  localStorage: globalThis.localStorage,
};
globalThis.document = {
  addEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  documentElement: {
    classList: { add: () => {}, remove: () => {}, toggle: () => {} },
    setAttribute: () => {},
    style: {},
  },
  createElement: () => ({
    classList: { add: () => {}, remove: () => {} },
    append: () => {},
    style: {},
    addEventListener: () => {},
    setAttribute: () => {},
  }),
};

const {
  parseDailyCapacity,
  summarizeDailyWorkload,
  summarizeFutureDueDistribution,
} = await import("../../src/momentum_agent/static/js/stats.js");
const {
  completionResultMessage,
  getSortMode,
  orderTasksByEstimate,
  parseDueOnDate,
  parseUnestimatedDueByToday,
  setSortMode,
} = await import("../../src/momentum_agent/static/js/tasks.js");

const NOW = new Date(2026, 5, 7, 12, 0, 0);  // 本地时间 2026-06-07 12:00

function task(overrides = {}) {
  return {
    id: overrides.id ?? 1,
    title: overrides.title ?? "任务",
    status: overrides.status ?? "todo",
    priority: overrides.priority ?? "medium",
    due_at: overrides.due_at ?? null,
    estimated_minutes: overrides.estimated_minutes ?? null,
    parent_task_id: overrides.parent_task_id ?? null,
    tags: overrides.tags ?? [],
  };
}

test("parseDailyCapacity 解析配置行，异常时回落到默认值", () => {
  assert.equal(parseDailyCapacity("daily_capacity_minutes=240"), 240);
  assert.equal(parseDailyCapacity("theme=paper" + "\n" + "daily_capacity_minutes=120"), 120);
  assert.equal(parseDailyCapacity("daily_capacity_minutes=abc"), 45);
  assert.equal(parseDailyCapacity("daily_capacity_minutes=-5"), 45);
  assert.equal(parseDailyCapacity("没有配置项"), 45);
  assert.equal(parseDailyCapacity(""), 45);
  assert.equal(parseDailyCapacity(null), 45);
});

function localIso(year, month, day, hour = 12) {
  return new Date(year, month - 1, day, hour, 0, 0).toISOString();
}

test("summarizeDailyWorkload 汇总当天可安排的工作量", () => {
  const todo = [task({ id: 1, due_at: localIso(2026, 6, 7), estimated_minutes: 60 })];
  const summary = summarizeDailyWorkload(todo, [], 45, NOW);
  assert.equal(summary.capacityMinutes, 45);
  assert.equal(summary.estimatedMinutes, 60);
  assert.equal(summary.eligibleTaskCount, 1);
  assert.equal(summary.overCapacityMinutes, Math.max(0, summary.estimatedMinutes - 45));
  assert.equal(summary.remainingMinutes, Math.max(0, 45 - summary.estimatedMinutes));
  assert.ok(summary.capacityPercent > 100, String(summary.capacityPercent));
});

test("summarizeDailyWorkload 把没估算的任务单独计数", () => {
  const todo = [task({ id: 1, due_at: localIso(2026, 6, 7), estimated_minutes: null })];
  const summary = summarizeDailyWorkload(todo, [], 45, NOW);
  assert.equal(summary.estimatedMinutes, 0);
  assert.equal(summary.unestimatedTaskCount, 1);
  assert.equal(summary.overCapacityMinutes, 0);
});

test("summarizeDailyWorkload 容忍非数组输入与非法容量", () => {
  const summary = summarizeDailyWorkload(null, undefined, -10, NOW);
  assert.equal(summary.estimatedMinutes, 0);
  assert.ok(summary.capacityMinutes >= 0, String(summary.capacityMinutes));
});

test("summarizeFutureDueDistribution 覆盖未来七天并把任务放进对应的一天", () => {
  const todo = [task({ id: 1, due_at: localIso(2026, 6, 8), estimated_minutes: 30 })];
  const days = summarizeFutureDueDistribution(todo, [], NOW);
  assert.equal(days.length, 7);
  for (const day of days) {
    assert.ok(typeof day.dateKey === "string" && day.dateKey);
    assert.ok(typeof day.label === "string" && day.label);
    assert.ok(Number.isInteger(day.estimatedMinutes));
    assert.ok(Number.isInteger(day.eligibleTaskCount));
  }
  const tomorrow = days[0];
  assert.equal(tomorrow.eligibleTaskCount, 1);
  assert.equal(tomorrow.estimatedMinutes, 30);
  assert.ok(days.slice(1).every((day) => day.eligibleTaskCount === 0), "其它天不应被误计");
});

test("summarizeFutureDueDistribution 忽略没有截止日与已完成的任务", () => {
  const todo = [task({ id: 1, due_at: null, estimated_minutes: 30 })];
  const done = [task({ id: 2, status: "done", due_at: localIso(2026, 6, 8), estimated_minutes: 30 })];
  const days = summarizeFutureDueDistribution(todo, done, NOW);
  assert.ok(days.every((day) => day.eligibleTaskCount === 0));
});

test("parseDueOnDate 只接受合法的本地日期键", () => {
  assert.equal(parseDueOnDate("?due_on=2026-06-07"), "2026-06-07");
  assert.equal(parseDueOnDate("due_on=2026-06-07"), "2026-06-07");
  assert.equal(parseDueOnDate("?due_on=2026-13-40"), null);
  assert.equal(parseDueOnDate("?due_on=today"), null);
  assert.equal(parseDueOnDate(""), null);
});

test("parseUnestimatedDueByToday 只在值为 1 时为真", () => {
  assert.equal(parseUnestimatedDueByToday("?unestimated_due_by_today=1"), true);
  assert.equal(parseUnestimatedDueByToday("?unestimated_due_by_today=0"), false);
  assert.equal(parseUnestimatedDueByToday(""), false);
});

test("orderTasksByEstimate 保留全部任务且父任务排在子任务之前", () => {
  const parent = task({ id: 1, title: "父", estimated_minutes: 60 });
  const child = task({ id: 2, title: "子", parent_task_id: 1, estimated_minutes: 15 });
  const lonely = task({ id: 3, title: "独立", estimated_minutes: 5 });
  const ordered = orderTasksByEstimate([child, parent, lonely]);
  assert.equal(ordered.length, 3);
  const ids = ordered.map((item) => item.id);
  assert.ok(ids.indexOf(1) < ids.indexOf(2), JSON.stringify(ids));
  assert.deepEqual([...ids].sort(), [1, 2, 3]);
  assert.deepEqual(orderTasksByEstimate(null), []);
});

test("getSortMode/setSortMode 往返一致且忽略非法模式", () => {
  const original = getSortMode();
  setSortMode("score");
  assert.equal(getSortMode(), "score");
  setSortMode("不存在的模式");
  assert.equal(getSortMode(), "score", "非法模式必须被忽略而不是写进去");
  setSortMode(original);
  assert.equal(getSortMode(), original);
});

test("completionResultMessage 覆盖各类失败与不确定状态", () => {
  assert.match(completionResultMessage({ error: { status: 404 } }), /404/);
  assert.match(
    completionResultMessage({ error: { status: 400, payload: { error: "idempotency_key_required" } } }),
    /Idempotency-Key/,
  );
  assert.match(
    completionResultMessage({ error: { status: 409, payload: { error: "idempotency_conflict" } } }),
    /409/,
  );
  assert.match(completionResultMessage({ status: "uncertain" }), /尚未确认/);
  assert.match(completionResultMessage({ status: "blocked" }), /停止发送|无法验证/);
  assert.match(completionResultMessage({ error: { message: "网络中断" } }), /网络中断/);
});
