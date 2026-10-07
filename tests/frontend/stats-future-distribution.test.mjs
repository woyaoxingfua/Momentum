import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import { getDailyWorkloadEstimate, taskDueLocalDate } from "../../src/momentum_agent/static/js/daily-workload.mjs";

const statsPath = new URL("../../src/momentum_agent/static/js/stats.js", import.meta.url);
const htmlPath = new URL("../../src/momentum_agent/static/stats.html", import.meta.url);
const source = await readFile(statsPath, "utf8");
const html = await readFile(htmlPath, "utf8");
const harnessSource = source
  .replace(/^import .*;\s*$/gm, "")
  .replace(/^export /gm, "")
  .replace(/^init\(\);\s*$/m, "")
  + "\nglobalThis.__statsTest = { summarizeFutureDueDistribution, loadDailyWorkload, refreshDailyWorkload };\n";
const context = vm.createContext({ console });
context.getDailyWorkloadEstimate = getDailyWorkloadEstimate;
context.taskDueLocalDate = taskDueLocalDate;
new vm.Script(harnessSource, { filename: "stats.js" }).runInContext(context);
const stats = context.__statsTest;

function localDate(year, monthIndex, day, hour = 12) {
  return new Date(year, monthIndex, day, hour, 0, 0);
}

function localDateKey(date, offsetDays = 0) {
  const offset = new Date(date.getFullYear(), date.getMonth(), date.getDate() + offsetDays, 12);
  return `${offset.getFullYear()}-${String(offset.getMonth() + 1).padStart(2, "0")}-${String(offset.getDate()).padStart(2, "0")}`;
}

function makeFixtures(now) {
  const due = (offsetDays) => localDateKey(now, offsetDays);
  return {
    todo: [
      { status: "todo", due_at: due(-1), estimated_minutes: 80 },
      { status: "todo", due_at: due(0), estimated_minutes: 40 },
      { status: "todo", due_at: due(1), estimated_minutes: 25 },
      { status: "todo", due_at: due(1), estimated_minutes: "15" },
      { status: "todo", due_at: due(1), estimated_minutes: 0 },
      { status: "todo", due_at: due(1), estimated_minutes: -2 },
      { status: "todo", due_at: due(1), estimated_minutes: undefined },
      { status: "todo", due_at: due(2), estimated_minutes: null },
      { status: "todo", due_at: due(7), estimated_minutes: 9 },
      { status: "todo", due_at: due(7), estimated_minutes: 0 },
      { status: "todo", due_at: due(8), estimated_minutes: 999 },
      { status: "todo", due_at: null, estimated_minutes: 500 },
      { status: "todo", due_at: "not-a-date", estimated_minutes: 500 },
      { status: "done", due_at: due(1), estimated_minutes: 700 },
      { status: "dropped", due_at: due(1), estimated_minutes: 800 },
    ],
    doing: [
      { status: "doing", due_at: due(1), estimated_minutes: 5 },
      { status: "doing", due_at: due(2), estimated_minutes: 20 },
      { status: "doing", due_at: due(2), estimated_minutes: "" },
      { status: "done", due_at: due(1), estimated_minutes: 900 },
    ],
  };
}

test("future distribution covers the next seven local calendar days and excludes today, overdue, invalid and closed tasks", () => {
  const now = localDate(2026, 9, 6, 23);
  const { todo, doing } = makeFixtures(now);
  const days = stats.summarizeFutureDueDistribution(todo, doing, now);

  assert.equal(days.length, 7);
  assert.equal(days[0].dateKey, "2026-10-07");
  assert.equal(days[6].dateKey, "2026-10-13");
  assert.equal(days[0].estimatedMinutes, 45);
  assert.equal(days[0].unestimatedTaskCount, 3);
  assert.equal(days[0].eligibleTaskCount, 6);
  assert.equal(days[1].estimatedMinutes, 20);
  assert.equal(days[1].unestimatedTaskCount, 2);
  assert.equal(days[1].eligibleTaskCount, 3);
  assert.equal(days[6].estimatedMinutes, 9);
  assert.equal(days[6].unestimatedTaskCount, 1);
  assert.equal(days[6].eligibleTaskCount, 2);
  for (const day of days.slice(2, 6)) {
    assert.equal(day.estimatedMinutes, 0);
    assert.equal(day.unestimatedTaskCount, 0);
    assert.equal(day.eligibleTaskCount, 0);
  }
});

test("daily workload and future distribution reuse only the existing config and open-task API requests", async () => {
  const now = localDate(2026, 9, 6);
  const { todo, doing } = makeFixtures(now);
  const calls = [];
  const summary = await stats.loadDailyWorkload(async (url) => {
    calls.push(url);
    if (url === "/api/config") return { config: "daily_capacity_minutes = 50" };
    if (url === "/api/tasks?status=todo") return { tasks: todo };
    if (url === "/api/tasks?status=doing") return { tasks: doing };
    throw new Error(`Unexpected URL: ${url}`);
  }, now);

  assert.deepEqual(calls, ["/api/config", "/api/tasks?status=todo", "/api/tasks?status=doing"]);
  assert.equal(summary.futureDueDistribution.length, 7);
  assert.equal(summary.futureDueDistribution[0].estimatedMinutes, 45);
  assert.equal(summary.futureDueDistribution[0].unestimatedTaskCount, 3);
});

test("future distribution renders clickable due_on links for all seven days and distinguishes them from scheduling and actual focus", async () => {
  const now = localDate(2026, 9, 6);
  const { todo, doing } = makeFixtures(now);
  const makeElement = () => ({
    textContent: "",
    children: [],
    setAttribute() {},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = [...children]; },
  });
  const elements = new Map([
    "dailyLoadMinutes", "dailyLoadDifference", "dailyLoadRatio", "dailyUnestimatedCount", "dailyLoadStatus",
    "futureDueDays", "futureDueStatus",
  ].map((id) => [id, makeElement()]));
  context.document = { getElementById: (id) => elements.get(id), createElement: makeElement };

  await stats.refreshDailyWorkload(async (url) => {
    if (url === "/api/config") return { config: "daily_capacity_minutes = 50" };
    return { tasks: url.endsWith("todo") ? todo : doing };
  }, now);

  const dayRows = elements.get("futureDueDays").children;
  assert.equal(dayRows.length, 7);
  const firstDayLink = dayRows[0].children[0];
  assert.equal(firstDayLink.href, "/?due_on=2026-10-07");
  assert.equal(firstDayLink.children[0].textContent, "10-07 周三");
  assert.equal(firstDayLink.children[1].textContent, "正估时合计 45 分钟 · 未估时 3 个");
  assert.equal(dayRows[6].children[0].href, "/?due_on=2026-10-13");
  assert.equal(elements.get("futureDueStatus").textContent, "已统计未来 7 个完整自然日（不含今日）");
  assert.match(html, /未来 7 天截止日估时分布/);
  assert.match(html, /按浏览器本地截止日期汇总 todo \/ doing/);
  assert.match(html, /不是排程，也不代表实际专注/);
});
