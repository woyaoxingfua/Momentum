import { requestJson } from "/js/api.js";

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

async function init() {
  if (!localStorage.getItem("momentum_token")) {
    window.location.href = "/login.html";
    return;
  }

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
  document.getElementById("estimationAccuracy").textContent = p.estimation_accuracy ? `${(p.estimation_accuracy * 100).toFixed(0)}%` : "--";
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
