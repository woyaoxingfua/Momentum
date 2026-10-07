import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import { filterUnestimatedDailyWorkloadTasks, getDailyWorkloadEstimate, taskDueLocalDate } from "../../src/momentum_agent/static/js/daily-workload.mjs";

const statsPath = new URL("../../src/momentum_agent/static/js/stats.js", import.meta.url);
const htmlPath = new URL("../../src/momentum_agent/static/stats.html", import.meta.url);
const source = await readFile(statsPath, "utf8");
const html = await readFile(htmlPath, "utf8");
const harnessSource = source
  .replace(/^import .*;\s*$/gm, "")
  .replace(/^export /gm, "")
  .replace(/^init\(\);\s*$/m, "")
  + "\nglobalThis.__statsTest = { parseDailyCapacity, summarizeDailyWorkload, loadDailyWorkload, refreshDailyWorkload, bindDailyWorkloadRefresh };\n";
const context = vm.createContext({ console });
context.getDailyWorkloadEstimate = getDailyWorkloadEstimate;
context.taskDueLocalDate = taskDueLocalDate;
new vm.Script(harnessSource, { filename: "stats.js" }).runInContext(context);
const stats = context.__statsTest;

function createElements(ids) {
  const createElement = () => ({
    textContent: "initial",
    children: [],
    setAttribute() {},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = [...children]; },
  });
  const elements = new Map(ids.map((id) => [id, createElement()]));
  context.document = { getElementById: (id) => elements.get(id), createElement };
  return elements;
}

function localDate(year, monthIndex, day, hour = 12) {
  return new Date(year, monthIndex, day, hour, 0, 0);
}

function workloadFixtures() {
  return {
    todo: [
      { id: 1, status: "todo", due_at: "2026-10-05T23:59:00", estimated_minutes: 25 },
      { id: 2, status: "todo", due_at: "2026-10-06T00:00:00", estimated_minutes: 35 },
      { id: 3, status: "todo", due_at: "2026-10-07T00:00:00", estimated_minutes: 100 },
      { id: 4, status: "todo", due_at: null, estimated_minutes: 100 },
      { id: 5, status: "todo", due_at: "not-a-date", estimated_minutes: 100 },
      { id: 6, status: "done", due_at: "2026-10-05T12:00:00", estimated_minutes: 90 },
      { id: 7, status: "dropped", due_at: "2026-10-05T12:00:00", estimated_minutes: 90 },
      { id: 8, status: "todo", due_at: "2026-10-06T12:00:00", estimated_minutes: 0 },
      { id: 9, status: "todo", due_at: "2026-10-06T12:00:00", estimated_minutes: -5 },
    ],
    doing: [
      { id: 10, status: "doing", due_at: "2026-10-04T12:00:00", estimated_minutes: "40" },
      { id: 11, status: "doing", due_at: "2026-10-06T12:00:00", estimated_minutes: null },
      { id: 12, status: "doing", due_at: "2026-10-08T12:00:00", estimated_minutes: 500 },
      { id: 13, status: "done", due_at: "2026-10-05T12:00:00", estimated_minutes: 70 },
      { id: 14, status: "dropped", due_at: "2026-10-05T12:00:00", estimated_minutes: 70 },
    ],
  };
}

test("daily capacity reads /api/config text and preserves the established 45-minute fallback and explicit zero", () => {
  assert.equal(stats.parseDailyCapacity("provider = openai\ndaily_capacity_minutes = 75\n"), 75);
  assert.equal(stats.parseDailyCapacity("provider = openai\n"), 45);
  assert.equal(stats.parseDailyCapacity("没有配置项"), 45);
  assert.equal(stats.parseDailyCapacity("daily_capacity_minutes = \n"), 45);
  assert.equal(stats.parseDailyCapacity("daily_capacity_minutes = 0\n"), 0);
});

test("loadDailyWorkload requests config plus both open statuses and applies local date and status filters", async () => {
  const calls = [];
  const fixtures = workloadFixtures();
  const summary = await stats.loadDailyWorkload(async (url) => {
    calls.push(url);
    if (url === "/api/config") return { config: "daily_capacity_minutes = 100\n" };
    if (url === "/api/tasks?status=todo") return { tasks: fixtures.todo };
    if (url === "/api/tasks?status=doing") return { tasks: fixtures.doing };
    throw new Error(`Unexpected URL: ${url}`);
  }, localDate(2026, 9, 6));

  assert.deepEqual(calls, ["/api/config", "/api/tasks?status=todo", "/api/tasks?status=doing"]);
  const { futureDueDistribution, ...dailySummary } = summary;
  assert.deepEqual(dailySummary, {
    capacityMinutes: 100,
    estimatedMinutes: 100,
    eligibleTaskCount: 6,
    unestimatedTaskCount: 3,
    overCapacityMinutes: 0,
    remainingMinutes: 0,
    capacityPercent: 100,
  });
  assert.equal(futureDueDistribution.length, 7);
  assert.equal(futureDueDistribution[0].dateKey, "2026-10-07");
  assert.equal(futureDueDistribution[0].estimatedMinutes, 100);
  assert.equal(futureDueDistribution[1].dateKey, "2026-10-08");
  assert.equal(futureDueDistribution[1].estimatedMinutes, 500);
  assert.ok(futureDueDistribution.slice(2).every((day) => day.estimatedMinutes === 0 && day.unestimatedTaskCount === 0));
});

test("daily workload reports remaining, exact-capacity, and over-capacity boundaries", () => {
  const { todo, doing } = workloadFixtures();
  const today = localDate(2026, 9, 6);
  const atCapacity = stats.summarizeDailyWorkload(todo, doing, 100, today);
  const belowCapacity = stats.summarizeDailyWorkload(todo, doing, 120, today);
  const overCapacity = stats.summarizeDailyWorkload(todo, doing, 80, today);

  assert.equal(atCapacity.remainingMinutes, 0);
  assert.equal(atCapacity.overCapacityMinutes, 0);
  assert.equal(belowCapacity.remainingMinutes, 20);
  assert.equal(belowCapacity.overCapacityMinutes, 0);
  assert.equal(belowCapacity.capacityPercent, 83.33333333333334);
  assert.equal(overCapacity.remainingMinutes, 0);
  assert.equal(overCapacity.overCapacityMinutes, 20);
  assert.equal(overCapacity.capacityPercent, 125);
});

test("card count and task-list filter share scope and estimate semantics across date boundaries", () => {
  const originalTimezone = process.env.TZ;
  process.env.TZ = "Asia/Hong_Kong";
  try {
    const today = localDate(2026, 9, 5);
    const tasks = [
      { id: 1, status: "todo", due_at: "2026-10-04", estimated_minutes: null },
      { id: 2, status: "doing", due_at: "2026-10-05T15:59:59Z", estimated_minutes: 0 },
      { id: 3, status: "todo", due_at: "2026-10-05T15:59:59Z", estimated_minutes: "invalid" },
      { id: 4, status: "todo", due_at: "2026-10-05T16:00:00Z", estimated_minutes: undefined },
      { id: 5, status: "todo", due_at: "2026-10-06", estimated_minutes: null },
      { id: 6, status: "todo", due_at: null, estimated_minutes: null },
      { id: 7, status: "todo", due_at: "2026-02-30", estimated_minutes: null },
      { id: 8, status: "done", due_at: "2026-10-04", estimated_minutes: null },
      { id: 9, status: "dropped", due_at: "2026-10-04", estimated_minutes: 0 },
      { id: 10, status: "doing", due_at: "2026-10-04", estimated_minutes: 20 },
      { id: 11, status: "todo", due_at: "2026-10-05", estimated_minutes: "25" },
      { id: 12, status: "doing", due_at: "2026-10-05", estimated_minutes: -1 },
      { id: 13, status: "doing", due_at: "2026-10-05", estimated_minutes: Number.NaN },
      { id: 14, status: "todo", due_at: "2026-10-05", estimated_minutes: Number.POSITIVE_INFINITY },
    ];
    const todo = tasks.filter((task) => task.status === "todo");
    const doing = tasks.filter((task) => task.status === "doing");
    const listed = filterUnestimatedDailyWorkloadTasks(tasks, today);
    const summary = stats.summarizeDailyWorkload(todo, doing, 45, today);

    assert.deepEqual(listed.map(({ id }) => id), [1, 2, 3, 12, 13, 14]);
    assert.equal(summary.unestimatedTaskCount, listed.length);
    assert.equal(summary.eligibleTaskCount, 8);
    assert.equal(summary.estimatedMinutes, 45);
    assert.equal(getDailyWorkloadEstimate(tasks[1], "2026-10-05"), 0, "zero is unestimated");
    assert.equal(getDailyWorkloadEstimate(tasks[0], "2026-10-05"), 0, "null follows Number(null) and is unestimated");
    assert.equal(getDailyWorkloadEstimate(tasks[10], "2026-10-05"), 25, "numeric strings remain valid positive estimates");
    assert.equal(getDailyWorkloadEstimate(tasks[4], "2026-10-05"), null, "future deadlines are outside today's workload");
  } finally {
    if (originalTimezone === undefined) delete process.env.TZ;
    else process.env.TZ = originalTimezone;
  }
});

test("zero capacity stays zero, avoids a division result, and still counts unestimated eligible tasks", () => {
  const { todo, doing } = workloadFixtures();
  const summary = stats.summarizeDailyWorkload(todo, doing, 0, localDate(2026, 9, 6));
  assert.equal(summary.capacityMinutes, 0);
  assert.equal(summary.estimatedMinutes, 100);
  assert.equal(summary.capacityPercent, null);
  assert.equal(summary.overCapacityMinutes, 100);
  assert.equal(summary.unestimatedTaskCount, 3);
});

test("the card renders a separate clickable unestimated count for the matching local overdue-plus-today list", async () => {
  const ids = ["dailyLoadLink", "dailyLoadMinutes", "dailyLoadDifference", "dailyLoadRatio", "dailyUnestimatedCount", "dailyUnestimatedLink", "dailyLoadStatus", "futureDueDays", "futureDueStatus"];
  const elements = createElements(ids);
  const tasks = [{ status: "todo", due_at: "2026-10-06T12:00:00", estimated_minutes: 20 }];

  await stats.refreshDailyWorkload(async (url) => {
    if (url === "/api/config") return { config: "daily_capacity_minutes = 0" };
    return { tasks: url.endsWith("todo") ? tasks : [{ status: "doing", due_at: "2026-10-06T12:00:00" }] };
  }, localDate(2026, 9, 6));

  assert.equal(elements.get("dailyLoadMinutes").textContent, "20 / 0 分钟");
  assert.equal(elements.get("dailyLoadDifference").textContent, "超出 20 分钟");
  assert.equal(elements.get("dailyLoadRatio").textContent, "容量占比未计算（容量为 0）");
  assert.equal(elements.get("dailyUnestimatedLink").textContent, "1 个");
  assert.equal(elements.get("dailyLoadLink").href, "/?due_on=2026-10-06");
  assert.equal(elements.get("dailyUnestimatedLink").href, "/?unestimated_due_by_today=1");
  assert.equal(elements.get("futureDueDays").children.length, 7);
});

test("any config or task API failure is visible and never renders a successful zero-load result", async () => {
    const ids = ["dailyLoadLink", "dailyLoadMinutes", "dailyLoadDifference", "dailyLoadRatio", "dailyUnestimatedCount", "dailyUnestimatedLink", "dailyLoadStatus", "futureDueDays", "futureDueStatus"];
  const urls = ["/api/config", "/api/tasks?status=todo", "/api/tasks?status=doing"];

  for (const failingUrl of urls) {
    const elements = createElements(ids);
    const calls = [];
    const result = await stats.refreshDailyWorkload(async (url) => {
      calls.push(url);
      if (url === failingUrl) throw new Error(`${url} 不可用`);
      if (url === "/api/config") return { config: "daily_capacity_minutes = 45" };
      return { tasks: [] };
    }, localDate(2026, 9, 6));

    assert.equal(result, null, `${failingUrl} failure must not return a summary`);
    assert.deepEqual(calls, urls);
    assert.equal(elements.get("dailyLoadMinutes").textContent, "--");
    assert.equal(elements.get("dailyLoadDifference").textContent, "--");
    assert.equal(elements.get("dailyUnestimatedLink").textContent, "--");
    assert.equal(elements.get("dailyLoadLink").href, "/?due_on=2026-10-06");
    assert.equal(elements.get("dailyLoadStatus").textContent, `今日负载加载失败：${failingUrl} 不可用`);
    assert.equal(elements.get("futureDueStatus").textContent, `未来截止日分布加载失败：${failingUrl} 不可用`);
    assert.deepEqual(elements.get("futureDueDays").children, []);
    assert.doesNotMatch(elements.get("dailyLoadMinutes").textContent, /^0 \/ 45/);
  }
});

test("the visible card labels planned estimates as non-actual focus time", () => {
  assert.match(html, /今日计划负载/);
  assert.match(html, /已估计划分钟 \/ 容量分钟/);
  assert.match(html, /不代表实际专注时长/);
  assert.match(html, /id="dailyLoadLink"[^>]*href="\/"/);
  assert.match(html, /查看今天到期的待办与进行中任务（不含逾期）/);
  assert.match(html, /id="dailyUnestimatedLink"[^>]*href="\/\?unestimated_due_by_today=1"/);
  assert.match(html, /未估时任务（本地逾期\+今天）/);
});

test("an estimate-update storage notification refreshes Stats workload counts in another tab", async () => {
  const ids = ["dailyLoadLink", "dailyLoadMinutes", "dailyLoadDifference", "dailyLoadRatio", "dailyUnestimatedCount", "dailyUnestimatedLink", "dailyLoadStatus", "futureDueDays", "futureDueStatus"];
  const elements = createElements(ids);
  const listeners = new Map();
  const target = {
    addEventListener: (name, listener) => listeners.set(name, listener),
    removeEventListener: (name) => listeners.delete(name),
  };
  const requests = [];
  const stop = stats.bindDailyWorkloadRefresh(target, async (url) => {
    requests.push(url);
    if (url === "/api/config") return { config: "daily_capacity_minutes = 90" };
    if (url === "/api/tasks?status=todo") return { tasks: [{ id: 1, status: "todo", due_at: "2026-10-06", estimated_minutes: 25 }] };
    if (url === "/api/tasks?status=doing") return { tasks: [{ id: 2, status: "doing", due_at: "2026-10-06", estimated_minutes: null }] };
    throw new Error(`Unexpected URL: ${url}`);
  });

  assert.equal(listeners.has("storage"), true);
  assert.equal(await listeners.get("storage")({ key: "unrelated_key" }), undefined);
  const summary = await listeners.get("storage")({ key: "momentum_daily_workload_refresh" });
  assert.deepEqual(requests, ["/api/config", "/api/tasks?status=todo", "/api/tasks?status=doing"]);
  assert.equal(summary.estimatedMinutes, 25);
  assert.equal(summary.unestimatedTaskCount, 1);
  assert.equal(elements.get("dailyUnestimatedLink").textContent, "1 个");
  assert.equal(elements.get("dailyLoadMinutes").textContent, "25 / 90 分钟");
  stop();
  assert.equal(listeners.has("storage"), false);
});
