import { requestJson } from "/js/api.js";
import { getEstimationAccuracyDisplay } from "/js/estimation-accuracy.mjs";
import { getDailyWorkloadEstimate, taskDueLocalDate } from "./daily-workload.mjs";

const DAILY_WORKLOAD_REFRESH_KEY = "momentum_daily_workload_refresh";

const COLORS = {
  accent: "#f59e0b",
  accentAlpha: "rgba(245, 158, 11, 0.25)",
  success: "#22c55e",
  successAlpha: "rgba(34, 197, 94, 0.25)",
  danger: "#ef4444",
  dangerAlpha: "rgba(239, 68, 68, 0.25)",
  text: "#f5f0e8",
  text2: "#a8a19a",
  text3: "#6e6860",
  grid: "#2e2c2a",
};

const CHART_DEFAULTS = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: {
      labels: { color: COLORS.text2, font: { family: "JetBrains Mono, monospace", size: 11 } },
    },
    tooltip: {
      backgroundColor: "#1f1e1c",
      titleColor: COLORS.text,
      bodyColor: COLORS.text2,
      borderColor: COLORS.grid,
      borderWidth: 1,
      padding: 10,
      titleFont: { family: "Noto Serif SC, serif", size: 13 },
      bodyFont: { family: "JetBrains Mono, monospace", size: 12 },
    },
  },
  scales: {
    x: {
      grid: { color: COLORS.grid },
      ticks: { color: COLORS.text3, font: { family: "JetBrains Mono, monospace", size: 10 } },
    },
    y: {
      grid: { color: COLORS.grid },
      ticks: { color: COLORS.text3, font: { family: "JetBrains Mono, monospace", size: 10 } },
    },
  },
};

function localDateKey(date) {
  if (!date || typeof date.getTime !== "function" || !Number.isFinite(date.getTime())) return null;
  const year = String(date.getFullYear()).padStart(4, "0");
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function parseDailyCapacity(configText) {
  if (typeof configText !== "string" || !configText || configText.startsWith("没有配置项")) return 45;

  let configuredValue = null;
  for (const line of configText.split("\n")) {
    if (!line.includes("=")) continue;
    const [key, ...rest] = line.replaceAll("  ", "").split("=");
    if (key.trim() === "daily_capacity_minutes") configuredValue = rest.join("=").trim();
  }
  if (configuredValue === null || configuredValue === "" || !/^\d+$/.test(configuredValue)) return 45;

  const capacity = Number(configuredValue);
  return Number.isSafeInteger(capacity) ? capacity : 45;
}

export function summarizeDailyWorkload(todoTasks, doingTasks, capacityMinutes, now = new Date()) {
  const today = localDateKey(now);
  const capacity = Number.isSafeInteger(capacityMinutes) && capacityMinutes >= 0 ? capacityMinutes : 45;
  let estimatedMinutes = 0;
  let unestimatedTaskCount = 0;
  let eligibleTaskCount = 0;

  for (const task of [...(Array.isArray(todoTasks) ? todoTasks : []), ...(Array.isArray(doingTasks) ? doingTasks : [])]) {
    const estimate = getDailyWorkloadEstimate(task, today);
    if (estimate === null) continue;

    eligibleTaskCount += 1;
    if (estimate > 0) estimatedMinutes += estimate;
    else unestimatedTaskCount += 1;
  }

  const overCapacityMinutes = Math.max(0, estimatedMinutes - capacity);
  const remainingMinutes = Math.max(0, capacity - estimatedMinutes);
  return {
    capacityMinutes: capacity,
    estimatedMinutes,
    eligibleTaskCount,
    unestimatedTaskCount,
    overCapacityMinutes,
    remainingMinutes,
    capacityPercent: capacity > 0 ? (estimatedMinutes / capacity) * 100 : null,
  };
}

export function summarizeFutureDueDistribution(todoTasks, doingTasks, now = new Date()) {
  const weekdays = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
  const days = Array.from({ length: 7 }, (_, index) => {
    const date = new Date(now.getFullYear(), now.getMonth(), now.getDate() + index + 1, 12);
    return {
      dateKey: localDateKey(date),
      label: `${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${weekdays[date.getDay()]}`,
      estimatedMinutes: 0,
      unestimatedTaskCount: 0,
      eligibleTaskCount: 0,
    };
  });
  const daysByDate = new Map(days.map((day) => [day.dateKey, day]));

  for (const task of [...(Array.isArray(todoTasks) ? todoTasks : []), ...(Array.isArray(doingTasks) ? doingTasks : [])]) {
    if (!task || (task.status !== "todo" && task.status !== "doing")) continue;
    const day = daysByDate.get(taskDueLocalDate(task.due_at));
    if (!day) continue;

    day.eligibleTaskCount += 1;
    const estimate = Number(task.estimated_minutes);
    if (Number.isFinite(estimate) && estimate > 0) day.estimatedMinutes += estimate;
    else day.unestimatedTaskCount += 1;
  }

  return days;
}

export async function loadDailyWorkload(request = requestJson, now = new Date()) {
  const [configPayload, todoPayload, doingPayload] = await Promise.all([
    request("/api/config"),
    request("/api/tasks?status=todo"),
    request("/api/tasks?status=doing"),
  ]);
  if (!todoPayload || !Array.isArray(todoPayload.tasks)) throw new Error("待办任务列表格式无效");
  if (!doingPayload || !Array.isArray(doingPayload.tasks)) throw new Error("进行中任务列表格式无效");

  const capacity = parseDailyCapacity(configPayload?.config);
  return {
    ...summarizeDailyWorkload(todoPayload.tasks, doingPayload.tasks, capacity, now),
    futureDueDistribution: summarizeFutureDueDistribution(todoPayload.tasks, doingPayload.tasks, now),
  };
}

function renderDailyWorkload(summary) {
  document.getElementById("dailyLoadMinutes").textContent = `${summary.estimatedMinutes} / ${summary.capacityMinutes} 分钟`;
  const difference = summary.overCapacityMinutes > 0
    ? `超出 ${summary.overCapacityMinutes} 分钟`
    : summary.remainingMinutes > 0
      ? `余量 ${summary.remainingMinutes} 分钟`
      : "刚好达到容量";
  document.getElementById("dailyLoadDifference").textContent = difference;
  document.getElementById("dailyLoadRatio").textContent = summary.capacityPercent === null
    ? "容量占比未计算（容量为 0）"
    : `容量占比 ${summary.capacityPercent.toFixed(0)}%`;
  const unestimatedLink = document.getElementById("dailyUnestimatedLink");
  if (unestimatedLink) unestimatedLink.textContent = `${summary.unestimatedTaskCount} 个`;
  else document.getElementById("dailyUnestimatedCount").textContent = `未估时任务：${summary.unestimatedTaskCount} 个`;
  document.getElementById("dailyLoadStatus").textContent = `纳入 ${summary.eligibleTaskCount} 个本地今日及逾期的开放任务`;
  renderFutureDueDistribution(summary.futureDueDistribution);
}

function renderFutureDueDistribution(days) {
  const list = document.getElementById("futureDueDays");
  list.replaceChildren();
  for (const day of days) {
    const item = document.createElement("div");
    item.className = "future-due-day";
    item.setAttribute("role", "listitem");

    const link = document.createElement("a");
    link.className = "future-due-link";
    link.href = `/?due_on=${encodeURIComponent(day.dateKey)}`;
    link.setAttribute("aria-label", `${day.label}：查看当天开放任务`);

    const date = document.createElement("span");
    date.className = "future-due-date";
    date.textContent = day.label;

    const totals = document.createElement("span");
    totals.className = "future-due-totals";
    totals.textContent = `正估时合计 ${day.estimatedMinutes} 分钟 · 未估时 ${day.unestimatedTaskCount} 个`;

    link.append(date, totals);
    item.append(link);
    list.append(item);
  }
  document.getElementById("futureDueStatus").textContent = "已统计未来 7 个完整自然日（不含今日）";
}

function renderDailyWorkloadError(error) {
  document.getElementById("dailyLoadMinutes").textContent = "--";
  document.getElementById("dailyLoadDifference").textContent = "--";
  document.getElementById("dailyLoadRatio").textContent = "容量占比：--";
  const unestimatedLink = document.getElementById("dailyUnestimatedLink");
  if (unestimatedLink) unestimatedLink.textContent = "--";
  else document.getElementById("dailyUnestimatedCount").textContent = "未估时任务：--";
  document.getElementById("dailyLoadStatus").textContent = `今日负载加载失败：${error?.message || "请求失败"}`;
  document.getElementById("futureDueDays").replaceChildren();
  document.getElementById("futureDueStatus").textContent = `未来截止日分布加载失败：${error?.message || "请求失败"}`;
}

export async function refreshDailyWorkload(request = requestJson, now = new Date()) {
  const dailyLoadLink = document.getElementById("dailyLoadLink");
  if (dailyLoadLink) {
    const today = localDateKey(now);
    dailyLoadLink.href = `/?due_on=${encodeURIComponent(today)}`;
  }
  const unestimatedLink = document.getElementById("dailyUnestimatedLink");
  if (unestimatedLink) unestimatedLink.href = "/?unestimated_due_by_today=1";

  try {
    const summary = await loadDailyWorkload(request, now);
    renderDailyWorkload(summary);
    return summary;
  } catch (error) {
    renderDailyWorkloadError(error);
    return null;
  }
}

export function bindDailyWorkloadRefresh(target = globalThis.window, request = requestJson) {
  if (!target || typeof target.addEventListener !== "function") return () => {};
  const onStorage = (event) => {
    if (event?.key === DAILY_WORKLOAD_REFRESH_KEY) return refreshDailyWorkload(request);
    return undefined;
  };
  target.addEventListener("storage", onStorage);
  return () => target.removeEventListener?.("storage", onStorage);
}

async function init() {
  if (!localStorage.getItem("momentum_token")) {
    window.location.href = "/login.html";
    return;
  }

  bindDailyWorkloadRefresh();
  void refreshDailyWorkload();

  try {
    const data = await requestJson("/api/stats");
    renderStats(data);
    renderCharts(data);
    loadInsights();
  } catch (e) {
    console.error(e);
    document.getElementById("insightsList").innerHTML = `<p class="muted">加载失败：${e.message}</p>`;
  }
}

function renderStats(data) {
  const p = data.profile;
  document.getElementById("completionRate").textContent = `${(p.completion_rate * 100).toFixed(0)}%`;
  document.getElementById("completionDetail").textContent = `${p.total_completed} / ${p.total_created}`;
  document.getElementById("avgHours").textContent = p.avg_completion_hours ? p.avg_completion_hours.toFixed(1) : "--";
  const trackedTasks = p.focus_tracked_tasks || 0;
  document.getElementById("avgFocusMinutes").textContent = trackedTasks
    ? p.avg_actual_focus_minutes.toFixed(1)
    : "--";
  document.getElementById("focusTrackedDetail").textContent = trackedTasks
    ? `基于 ${trackedTasks} 个有实际记录的完成任务（分钟 / 任务）`
    : "尚无实际专注记录；旧计划时长不会当作实际值";
  const estimationDisplay = getEstimationAccuracyDisplay(p.estimation_accuracy, p.estimated_focus_tasks);
  document.getElementById("estimationAccuracy").textContent = estimationDisplay.value;
  document.getElementById("estimationDetail").textContent = estimationDisplay.detail;
  document.getElementById("peakHour").textContent = p.peak_completion_hour !== null ? `${p.peak_completion_hour}:00` : "--";
}

function renderCharts(data) {
  Chart.defaults.color = COLORS.text2;
  Chart.defaults.font.family = "JetBrains Mono, monospace";

  // 每日趋势
  new Chart(document.getElementById("dailyChart"), {
    type: "line",
    data: {
      labels: data.daily.labels,
      datasets: [
        {
          label: "创建",
          data: data.daily.created,
          borderColor: COLORS.text3,
          backgroundColor: "transparent",
          borderWidth: 1.5,
          tension: 0.3,
          pointRadius: 0,
        },
        {
          label: "完成",
          data: data.daily.done,
          borderColor: COLORS.accent,
          backgroundColor: COLORS.accentAlpha,
          borderWidth: 2,
          fill: true,
          tension: 0.3,
          pointRadius: 2,
          pointBackgroundColor: COLORS.accent,
        },
      ],
    },
    options: CHART_DEFAULTS,
  });

  // 优先级分布
  new Chart(document.getElementById("priorityChart"), {
    type: "doughnut",
    data: {
      labels: ["高", "中", "低"],
      datasets: [{
        data: [data.priority.high, data.priority.medium, data.priority.low],
        backgroundColor: [COLORS.danger, COLORS.accent, COLORS.text3],
        borderWidth: 0,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: "bottom", labels: { color: COLORS.text2, font: { size: 11 } } },
      },
    },
  });

  // 每周模式
  const weekDays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
  const weeklyData = weekDays.map((d) => data.weekly[d] || 0);
  new Chart(document.getElementById("weeklyChart"), {
    type: "bar",
    data: {
      labels: weekDays,
      datasets: [{
        label: "完成任务数",
        data: weeklyData,
        backgroundColor: COLORS.successAlpha,
        borderColor: COLORS.success,
        borderWidth: 1,
      }],
    },
    options: {
      ...CHART_DEFAULTS,
      plugins: { legend: { display: false } },
    },
  });

  // 专注趋势
  new Chart(document.getElementById("focusChart"), {
    type: "bar",
    data: {
      labels: data.focus.labels,
      datasets: [{
        label: "专注分钟",
        data: data.focus.minutes,
        backgroundColor: COLORS.accentAlpha,
        borderColor: COLORS.accent,
        borderWidth: 1,
      }],
    },
    options: {
      ...CHART_DEFAULTS,
      plugins: { legend: { display: false } },
    },
  });

  // 完成时段分布
  const hours = Array.from({ length: 24 }, (_, i) => `${i}:00`);
  const hourlyData = hours.map((_, i) => data.hourly[String(i)] || 0);
  new Chart(document.getElementById("hourlyChart"), {
    type: "bar",
    data: {
      labels: hours,
      datasets: [{
        label: "完成任务数",
        data: hourlyData,
        backgroundColor: COLORS.accent,
        borderRadius: 0,
      }],
    },
    options: {
      ...CHART_DEFAULTS,
      plugins: { legend: { display: false } },
    },
  });
}

async function loadInsights() {
  try {
    const res = await requestJson("/api/advice");
    const list = document.getElementById("insightsList");
    if (res.insights && res.insights.length > 0) {
      list.innerHTML = res.insights
        .filter((i) => i.category !== "achievement" || i.priority >= 2)
        .slice(0, 6)
        .map((i) => `<div class="insight-item">${i.icon} <strong>${i.title}</strong>：${i.detail}</div>`)
        .join("");
    } else if (res.summary) {
      list.innerHTML = `<div class="insight-item">${res.summary}</div>`;
    } else {
      list.innerHTML = `<p class="muted">暂无足够数据生成洞察。继续使用 Momentum，我会逐渐了解你的工作模式。</p>`;
    }
  } catch {
    document.getElementById("insightsList").innerHTML = `<p class="muted">洞察加载失败</p>`;
  }
}

init();
