/* ── Focus Timer ────────────────────────────────────────────── */

import { requestJson } from "./api.js";
import { loadTasks } from "./tasks.js";

let _focusTimerState = {
  taskId: null,
  taskTitle: "",
  durationMinutes: 25,
  remainingSeconds: 0,
  intervalId: null,
  paused: false,
  pausedAt: null,
  phase: "idle", // idle | running | break
  breakSeconds: 0,
};

let _focusBreakIntervalId = null;

// 简易提示，避免引入额外依赖
function _showToast(msg) {
  const existing = document.getElementById("_focusToast");
  if (existing) existing.remove();
  const toast = document.createElement("div");
  toast.id = "_focusToast";
  toast.style.cssText = `
    position: fixed; bottom: 80px; left: 50%; transform: translateX(-50%);
    background: var(--surface); color: var(--text);
    border: 1px solid var(--accent); padding: 8px 16px;
    font-size: 13px; z-index: 9999; pointer-events: none;
    box-shadow: var(--shadow);
  `;
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 2000);
}

export function focusInit() {
  const durBtns = document.querySelectorAll(".focus-dur-btn");
  durBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      durBtns.forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      _focusTimerState.durationMinutes = parseInt(btn.dataset.min);
      updateFocusCountdownDisplay();
    });
  });

  document.getElementById("focusStartBtn").addEventListener("click", focusStart);
  document.getElementById("focusPauseBtn").addEventListener("click", focusTogglePause);
  document.getElementById("focusStopBtn").addEventListener("click", focusStop);
  document.getElementById("focusSkipBreakBtn").addEventListener("click", focusSkipBreak);
  document.getElementById("focusDoneBtn").addEventListener("click", focusDone);

  // Collapsible
  const header = document.querySelector(".focus-timer-header");
  if (header) {
    header.addEventListener("click", () => {
      const body = document.getElementById("focusTimerBody");
      if (body) body.classList.toggle("hidden");
    });
  }

  loadFocusStats();
  populateFocusTaskSelect();
}

async function populateFocusTaskSelect() {
  const select = document.getElementById("focusTaskSelect");
  if (!select) return;
  select.innerHTML = '<option value="">选择任务...</option>';
  try {
    const tasks = await loadTasks();
    tasks
      .filter((t) => t.status === "todo" || t.status === "doing")
      .forEach((t) => {
        const opt = document.createElement("option");
        opt.value = t.id;
        opt.textContent = t.title;
        select.appendChild(opt);
      });
  } catch {
    // 静默失败，用户可能还没登录
  }
}

async function loadFocusStats() {
  const stats = document.getElementById("focusStats");
  if (!stats) return;
  try {
    const data = await requestJson("/api/focus/stats");
    const todayMins = data.total_minutes_today || 0;
    const weekMins = data.total_minutes_week || 0;
    const sessions = data.total_sessions_week || 0;
    stats.innerHTML = `
      <span>今日 <strong style="color:var(--accent)">${todayMins}m</strong></span>
      <span>本周 <strong>${weekMins}m</strong></span>
      <span>次数 <strong>${sessions}</strong></span>
    `;
  } catch {
    stats.innerHTML = "";
  }
}

function updateFocusCountdownDisplay() {
  const el = document.getElementById("focusCountdown");
  if (!el) return;
  const mins = Math.floor(_focusTimerState.remainingSeconds / 60);
  const secs = _focusTimerState.remainingSeconds % 60;
  el.textContent = `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;

  const progress = document.getElementById("focusProgressFill");
  if (progress) {
    const total = _focusTimerState.durationMinutes * 60;
    progress.style.width = `${((total - _focusTimerState.remainingSeconds) / total) * 100}%`;
  }
}

async function focusStart() {
  const select = document.getElementById("focusTaskSelect");
  const taskId = select ? parseInt(select.value) : null;
  const title = taskId
    ? (select.options[select.selectedIndex]?.text || "")
    : "";

  if (!taskId) {
    _showToast("请先选择一个任务");
    return;
  }

  const duration = _focusTimerState.durationMinutes;

  // 通知后端记录
  try {
    await requestJson("/api/focus/start", {
      method: "POST",
      body: JSON.stringify({ task_id: taskId, duration_minutes: duration }),
    });
  } catch {
    // 静默失败，不阻塞计时器
  }

  _focusTimerState.taskId = taskId;
  _focusTimerState.taskTitle = title;
  _focusTimerState.remainingSeconds = duration * 60;
  _focusTimerState.phase = "running";
  _focusTimerState.paused = false;

  document.getElementById("focusIdle").classList.add("hidden");
  document.getElementById("focusBreak").classList.add("hidden");
  document.getElementById("focusRunning").classList.remove("hidden");
  document.getElementById("focusCurrentTask").textContent = title;
  document.getElementById("focusPauseBtn").textContent = "暂停";

  updateFocusCountdownDisplay();
  _focusTimerState.intervalId = setInterval(focusTick, 1000);
}

function focusTick() {
  if (_focusTimerState.paused) return;
  _focusTimerState.remainingSeconds--;
  if (_focusTimerState.remainingSeconds <= 0) {
    clearInterval(_focusTimerState.intervalId);
    focusOnComplete();
    return;
  }
  updateFocusCountdownDisplay();
}

function focusOnComplete() {
  _focusTimerState.phase = "break";
  _focusTimerState.breakSeconds = 5 * 60; // 5分钟休息

  document.getElementById("focusRunning").classList.add("hidden");
  document.getElementById("focusBreak").classList.remove("hidden");

  // 浏览器通知
  if (Notification.permission === "granted") {
    new Notification("专注完成！", { body: "该休息一下了 ☕" });
  } else if (Notification.permission !== "denied") {
    Notification.requestPermission();
  }

  // 尝试播放提示音
  try {
    const ctx = new AudioContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.frequency.value = 880;
    gain.gain.value = 0.15;
    osc.start();
    osc.stop(ctx.currentTime + 0.3);
  } catch (_) {}

  _focusBreakIntervalId = setInterval(focusBreakTick, 1000);
  updateFocusBreakDisplay();
}

function focusBreakTick() {
  _focusTimerState.breakSeconds--;
  if (_focusTimerState.breakSeconds <= 0) {
    clearInterval(_focusBreakIntervalId);
  }
  updateFocusBreakDisplay();
}

function updateFocusBreakDisplay() {
  const el = document.getElementById("focusBreakCountdown");
  if (!el) return;
  const mins = Math.floor(_focusTimerState.breakSeconds / 60);
  const secs = _focusTimerState.breakSeconds % 60;
  el.textContent = `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function focusTogglePause() {
  _focusTimerState.paused = !_focusTimerState.paused;
  const btn = document.getElementById("focusPauseBtn");
  btn.textContent = _focusTimerState.paused ? "继续" : "暂停";
}

function focusStop() {
  clearInterval(_focusTimerState.intervalId);
  _focusTimerState.phase = "idle";
  document.getElementById("focusRunning").classList.add("hidden");
  document.getElementById("focusIdle").classList.remove("hidden");
  loadFocusStats();
}

function focusSkipBreak() {
  clearInterval(_focusBreakIntervalId);
  _focusTimerState.phase = "idle";
  document.getElementById("focusBreak").classList.add("hidden");
  document.getElementById("focusIdle").classList.remove("hidden");
  loadFocusStats();
}

async function focusDone() {
  clearInterval(_focusBreakIntervalId);

  // 自动完成关联任务
  if (_focusTimerState.taskId) {
    try {
      await requestJson(`/api/tasks/${_focusTimerState.taskId}/done`, { method: "POST" });
      await loadTasks();
    } catch (_) {}
  }

  _focusTimerState.phase = "idle";
  document.getElementById("focusBreak").classList.add("hidden");
  document.getElementById("focusIdle").classList.remove("hidden");
  loadFocusStats();
  populateFocusTaskSelect();
}
