/* ── Focus Timer ────────────────────────────────────────────── */

import { requestJson } from "./api.js";
import { formatFocusStartError, startFocusSession } from "./focus-api.mjs";
import { completeTask } from "./tasks.js";
import { FocusClock, remainingFocusSeconds } from "./focus-clock.mjs";
import { FOCUS_SNAPSHOT_VERSION, FocusFinishCoordinator, FocusRecoveryStore, focusRecoveryMode, getCurrentFocusUserId } from "./focus-recovery.mjs";
import { FocusTabCoordinator } from "./focus-tabs.mjs";
import { setFocusEntryAvailability } from "./advice-review.mjs";

const FOCUS_MAX_SAMPLE_GAP_MS = 1500;

let _focusTimerState = {
  taskId: null,
  taskTitle: "",
  durationMinutes: 25,
  remainingSeconds: 0,
  intervalId: null,
  paused: false,
  phase: "idle", // idle | running | break | recovery | mirror
  breakSeconds: 0,
  elapsedSeconds: 0,
  sessionId: null,
  startedAt: null,
  completionPending: false,
  sessionSaved: false,
  sessionResult: null,
  finishPayload: null,
};

const _focusClock = new FocusClock();
let _focusBreakIntervalId = null;
let _focusFinishRequest = null;
let _focusRecoveryStore = null;
let _focusFinishCoordinator = null;
let _focusTabCoordinator = null;
let _focusOwnerHeartbeatIntervalId = null;
let _focusLastPersistedSeconds = -1;
let _focusStartPending = false;
let _focusStartLockHeld = false;
let _focusMirror = false;

function syncFocusEntryAvailability() {
  const active = _focusStartPending || _focusTimerState.phase !== "idle";
  setFocusEntryAvailability(active);
  const startButton = document.getElementById("focusStartBtn");
  if (startButton) startButton.disabled = active;
  const estimatedStartButton = document.getElementById("focusEstimatedStartBtn");
  if (estimatedStartButton) {
    estimatedStartButton.disabled = active || estimatedStartButton.dataset?.estimateValid !== "true";
  }
}

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
  setTimeout(() => toast.remove(), 3000);
}

export function focusInit() {
  const durBtns = document.querySelectorAll(".focus-dur-btn");
  durBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      durBtns.forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      _focusTimerState.durationMinutes = parseInt(btn.dataset.min, 10);
      updateFocusCountdownDisplay();
    });
  });

  document.getElementById("focusStartBtn").addEventListener("click", () => { void focusStart(); });
  document.getElementById("focusEstimatedStartBtn").addEventListener("click", () => { void focusStartSelectedEstimate(); });
  document.getElementById("focusTaskSelect").addEventListener("change", updateEstimatedFocusStartAvailability);
  document.getElementById("focusPauseBtn").addEventListener("click", () => { void focusTogglePause(); });
  document.getElementById("focusStopBtn").addEventListener("click", () => { void focusStop(); });
  document.getElementById("focusSkipBreakBtn").addEventListener("click", focusSkipBreak);
  document.getElementById("focusDoneBtn").addEventListener("click", () => { void focusDone(); });
  document.getElementById("focusBreakStartBtn").addEventListener("click", () => { void focusBreakStartNextRound(); });
  document.getElementById("focusRecoveryContinue").addEventListener("click", focusContinueRecovery);
  document.getElementById("focusRecoveryInterrupt").addEventListener("click", () => { void focusInterruptRecovery(); });
  document.getElementById("focusRecoveryRetry").addEventListener("click", () => { void focusRetryPendingSave(); });

  if (typeof window !== "undefined") window.addEventListener("pagehide", persistFocusBeforePageHide);

  // Collapsible
  const header = document.querySelector(".focus-timer-header");
  if (header) {
    header.addEventListener("click", () => {
      const body = document.getElementById("focusTimerBody");
      if (body) body.classList.toggle("hidden");
    });
  }

  void loadFocusStats();
  void populateFocusTaskSelect();
  _focusRecoveryStore = new FocusRecoveryStore(getCurrentFocusUserId());
  _focusTabCoordinator = new FocusTabCoordinator(_focusRecoveryStore.userId);
  _focusFinishCoordinator = new FocusFinishCoordinator(_focusRecoveryStore, requestJson);
  if (typeof window !== "undefined") window.addEventListener("storage", handleFocusStorageChange);
  restoreFocusSnapshot();
  syncFocusEntryAvailability();
}

function restoreFocusSnapshot(snapshot = _focusRecoveryStore?.load()) {
  if (!snapshot) return;
  if (_focusTabCoordinator?.isOwnedByAnotherTab(snapshot.session_id) && !snapshot.finish_payload) {
    showMirroredFocusSnapshot(snapshot);
    return;
  }
  _focusMirror = false;
  _focusTimerState.taskId = snapshot.task_id;
  _focusTimerState.taskTitle = snapshot.task_title || `任务 ${snapshot.task_id}`;
  _focusTimerState.durationMinutes = snapshot.planned_minutes;
  _focusTimerState.elapsedSeconds = snapshot.finish_payload?.actual_seconds ?? snapshot.elapsed_seconds;
  _focusTimerState.remainingSeconds = remainingFocusSeconds(snapshot.planned_minutes * 60, _focusTimerState.elapsedSeconds);
  _focusTimerState.sessionId = snapshot.session_id;
  _focusTimerState.startedAt = snapshot.started_at;
  _focusTimerState.finishPayload = snapshot.finish_payload || null;
  _focusTimerState.paused = true;
  _focusTimerState.phase = "recovery";
  _focusClock.reset();
  _focusLastPersistedSeconds = _focusTimerState.elapsedSeconds;

  const matchingDuration = document.querySelector(`.focus-dur-btn[data-min="${snapshot.planned_minutes}"]`);
  if (matchingDuration) {
    document.querySelectorAll(".focus-dur-btn").forEach((button) => button.classList.remove("active"));
    matchingDuration.classList.add("active");
  }
  document.getElementById("focusIdle").classList.add("hidden");
  document.getElementById("focusRunning").classList.add("hidden");
  document.getElementById("focusBreak").classList.add("hidden");
  document.getElementById("focusRecovery").classList.remove("hidden");
  const message = document.getElementById("focusRecoveryMessage");
  const actions = document.getElementById("focusRecoveryActions");
  const retry = document.getElementById("focusRecoveryRetry");
  if (focusRecoveryMode(snapshot) === "retry-only") {
    message.textContent = `“${_focusTimerState.taskTitle}”的记录尚未确认保存（已确认 ${_focusTimerState.finishPayload.actual_seconds} 秒）。只能按原会话和原载荷重试。`;
    actions.classList.add("hidden");
    retry.classList.remove("hidden");
  } else {
    message.textContent = `检测到“${_focusTimerState.taskTitle}”专注记录：已确认累计 ${_focusTimerState.elapsedSeconds} 秒。刷新或重开前未确认的时间不计入。请选择如何处理。`;
    actions.classList.remove("hidden");
    retry.classList.add("hidden");
    _focusRecoveryStore.save({ ...snapshot, state: "paused" });
  }
}

function showMirroredFocusSnapshot(snapshot) {
  if (!snapshot || snapshot.finish_payload) return;
  _focusMirror = true;
  _focusTimerState.taskId = snapshot.task_id;
  _focusTimerState.taskTitle = snapshot.task_title || `任务 ${snapshot.task_id}`;
  _focusTimerState.durationMinutes = snapshot.planned_minutes;
  _focusTimerState.elapsedSeconds = snapshot.elapsed_seconds;
  _focusTimerState.remainingSeconds = remainingFocusSeconds(snapshot.planned_minutes * 60, snapshot.elapsed_seconds);
  _focusTimerState.sessionId = snapshot.session_id;
  _focusTimerState.startedAt = snapshot.started_at;
  _focusTimerState.paused = true;
  _focusTimerState.phase = "mirror";
  _focusClock.reset();

  document.getElementById("focusIdle").classList.add("hidden");
  document.getElementById("focusRecovery").classList.add("hidden");
  document.getElementById("focusBreak").classList.add("hidden");
  document.getElementById("focusRunning").classList.remove("hidden");
  document.getElementById("focusCurrentTask").textContent = `${_focusTimerState.taskTitle}（另一标签页正在专注）`;
  document.getElementById("focusPauseBtn").disabled = true;
  document.getElementById("focusStopBtn").disabled = true;
  document.getElementById("focusStopBtn").textContent = "另一标签页控制中";
  updateFocusCountdownDisplay();
  syncFocusEntryAvailability();
}

function handleFocusStorageChange(event) {
  if (!_focusRecoveryStore || !_focusTabCoordinator) return;
  const relevantPrefixes = [
    _focusRecoveryStore.key,
    _focusRecoveryStore.legacyKey,
    _focusTabCoordinator.ownerKey,
    "momentum_focus_settled_v1:",
  ].filter(Boolean);
  if (!relevantPrefixes.some((prefix) => event.key === prefix || event.key?.startsWith(prefix))) return;

  const snapshot = _focusRecoveryStore.load();
  if (snapshot) {
    if (_focusTabCoordinator.hasLiveOwner(snapshot.session_id)) {
      showMirroredFocusSnapshot(snapshot);
    } else if (_focusMirror) {
      _focusMirror = false;
      _focusTimerState.phase = "idle";
      resetFocusSession();
      restoreFocusSnapshot(snapshot);
      syncFocusEntryAvailability();
    }
    return;
  }

  if (!_focusMirror || !_focusTimerState.sessionId) return;
  const settlement = _focusRecoveryStore.getSettlement(_focusTimerState.sessionId);
  if (!settlement) return;
  _focusMirror = false;
  _focusTimerState.phase = "idle";
  document.getElementById("focusRunning").classList.add("hidden");
  document.getElementById("focusRecovery").classList.add("hidden");
  document.getElementById("focusBreak").classList.add("hidden");
  document.getElementById("focusIdle").classList.remove("hidden");
  resetFocusSession();
  syncFocusEntryAvailability();
  _showToast(settlement.finish_payload.outcome === "completed" ? "另一标签页已完成专注" : "另一标签页已结束专注");
  void loadFocusStats();
  void populateFocusTaskSelect();
}

function focusElapsedSeconds() {
  const plannedSeconds = _focusTimerState.durationMinutes * 60;
  if (_focusTimerState.phase === "running" || _focusTimerState.phase === "break") {
    return Math.min(plannedSeconds, _focusClock.elapsedSeconds());
  }
  return Math.min(plannedSeconds, Math.max(0, _focusTimerState.elapsedSeconds));
}

function persistFocusSnapshot({ state = _focusTimerState.paused ? "paused" : "running", elapsedSeconds = focusElapsedSeconds(), finishPayload = _focusTimerState.finishPayload } = {}) {
  if (!_focusRecoveryStore?.userId || !_focusTimerState.sessionId) return false;
  if (state === "running" && !_focusTabCoordinator?.refreshOwner(_focusTimerState.sessionId)) return false;
  _focusTimerState.elapsedSeconds = elapsedSeconds;
  const snapshot = {
    version: FOCUS_SNAPSHOT_VERSION,
    user_id: _focusRecoveryStore.userId,
    session_id: _focusTimerState.sessionId,
    task_id: _focusTimerState.taskId,
    planned_minutes: _focusTimerState.durationMinutes,
    elapsed_seconds: elapsedSeconds,
    state,
    started_at: _focusTimerState.startedAt,
    task_title: _focusTimerState.taskTitle,
  };
  if (finishPayload) snapshot.finish_payload = { ...finishPayload };
  const saved = _focusRecoveryStore.save(snapshot);
  if (saved) _focusLastPersistedSeconds = elapsedSeconds;
  return saved;
}

function startFocusOwnerHeartbeat() {
  clearInterval(_focusOwnerHeartbeatIntervalId);
  _focusOwnerHeartbeatIntervalId = setInterval(() => {
    _focusTabCoordinator?.refreshOwner(_focusTimerState.sessionId);
  }, 2000);
}

function releaseFocusTabOwner(sessionId = _focusTimerState.sessionId) {
  clearInterval(_focusOwnerHeartbeatIntervalId);
  _focusOwnerHeartbeatIntervalId = null;
  _focusTabCoordinator?.releaseOwner(sessionId);
}

function persistFocusBeforePageHide() {
  if (_focusTimerState.phase !== "running") return;
  _focusTimerState.elapsedSeconds = focusElapsedSeconds();
  _focusTimerState.remainingSeconds = remainingFocusSeconds(_focusTimerState.durationMinutes * 60, _focusTimerState.elapsedSeconds);
  persistFocusSnapshot({
    state: _focusTimerState.paused ? "paused" : "running",
    elapsedSeconds: _focusTimerState.elapsedSeconds,
  });
  releaseFocusTabOwner(_focusTimerState.sessionId);
}

function focusContinueRecovery() {
  if (_focusTimerState.phase !== "recovery" || _focusTimerState.finishPayload) return;
  const elapsedSeconds = _focusTimerState.elapsedSeconds;
  if (!persistFocusSnapshot({ state: "running", elapsedSeconds, finishPayload: null })) {
    _showToast("无法保存恢复状态，专注尚未继续");
    return;
  }
  startFocusOwnerHeartbeat();
  _focusClock.start(elapsedSeconds, FOCUS_MAX_SAMPLE_GAP_MS);
  _focusTimerState.phase = "running";
  _focusTimerState.paused = false;
  _focusTimerState.completionPending = false;
  syncFocusEntryAvailability();
  _focusTimerState.remainingSeconds = remainingFocusSeconds(_focusTimerState.durationMinutes * 60, elapsedSeconds);
  document.getElementById("focusRecovery").classList.add("hidden");
  document.getElementById("focusRunning").classList.remove("hidden");
  document.getElementById("focusCurrentTask").textContent = _focusTimerState.taskTitle;
  document.getElementById("focusPauseBtn").textContent = "暂停";
  document.getElementById("focusPauseBtn").disabled = false;
  document.getElementById("focusStopBtn").textContent = "结束并保存";
  document.getElementById("focusStopBtn").disabled = false;
  updateFocusCountdownDisplay();
  clearInterval(_focusTimerState.intervalId);
  _focusTimerState.intervalId = setInterval(focusTick, 250);
}

async function focusInterruptRecovery() {
  if (_focusTimerState.phase !== "recovery" || _focusTimerState.finishPayload) return;
  const buttons = document.querySelectorAll("#focusRecoveryActions button");
  buttons.forEach((button) => { button.disabled = true; });
  let result;
  try {
    result = await saveFocusSession();
  } catch (error) {
    showPendingFocusRetry(error);
    return;
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
  }
  handleStoppedFocusSuccess(result);
}

async function focusRetryPendingSave() {
  if (_focusTimerState.phase !== "recovery" || !_focusTimerState.finishPayload) return;
  const retry = document.getElementById("focusRecoveryRetry");
  retry.disabled = true;
  let result;
  try {
    result = await saveFocusSession();
  } catch (error) {
    showPendingFocusRetry(error);
    return;
  } finally {
    retry.disabled = false;
  }
  handleStoppedFocusSuccess(result);
}

function showPendingFocusRetry(error) {
  if (!_focusTimerState.finishPayload && _focusFinishCoordinator?.lastPayload) {
    _focusTimerState.finishPayload = { ..._focusFinishCoordinator.lastPayload };
  }
  const actions = document.getElementById("focusRecoveryActions");
  const retry = document.getElementById("focusRecoveryRetry");
  if (_focusTimerState.finishPayload) {
    actions.classList.add("hidden");
    retry.classList.remove("hidden");
    document.getElementById("focusRecoveryMessage").textContent = `记录尚未确认保存；再次尝试将使用相同会话 ID 与相同结束载荷。${error?.message || ""}`;
  } else {
    actions.classList.remove("hidden");
    retry.classList.add("hidden");
    document.getElementById("focusRecoveryMessage").textContent = `本地无法固定待保存载荷，尚未发送请求。请再次选择操作重试。${error?.message || ""}`;
  }
  _showToast(`记录未保存：${error?.message || "请重试"}`);
}

function handleStoppedFocusSuccess(result) {
  releaseFocusTabOwner(_focusTimerState.sessionId);
  if (result?.outcome === "completed") {
    enterFocusBreak();
    if (result?.snapshotSettled === false) {
      _showToast("专注已记录，但本地结算标记未能保存；检查本地存储后可能需要重试");
    }
    return;
  }
  const elapsed = result?.actual_seconds ?? _focusTimerState.elapsedSeconds;
  _focusTimerState.phase = "idle";
  syncFocusEntryAvailability();
  document.getElementById("focusRecovery").classList.add("hidden");
  document.getElementById("focusRunning").classList.add("hidden");
  document.getElementById("focusIdle").classList.remove("hidden");
  document.getElementById("focusStopBtn").textContent = "结束并保存";
  resetFocusSession();
  void loadFocusStats();
  void populateFocusTaskSelect();
  _showToast(result?.snapshotSettled !== false
    ? `已记录实际专注 ${elapsed} 秒（历史结果标记为 stopped）`
    : `已记录实际专注 ${elapsed} 秒，但本地结算标记未能保存；检查本地存储后可能需要重试`);
}

async function populateFocusTaskSelect() {
  const select = document.getElementById("focusTaskSelect");
  if (!select) return;
  const selectedId = normalizeFocusTaskId(select.value);
  const searchInput = document.getElementById("focusTaskSearch");
  const loadError = document.getElementById("focusTaskLoadError");
  if (searchInput) searchInput.oninput = null;
  select.disabled = true;
  select.innerHTML = '<option value="">正在加载可专注任务...</option>';
  select.value = "";
  if (loadError) {
    loadError.hidden = true;
    loadError.textContent = "";
  }

  try {
    const [todoPayload, doingPayload] = await Promise.all([
      requestJson("/api/tasks?status=todo"),
      requestJson("/api/tasks?status=doing"),
    ]);
    if (!Array.isArray(todoPayload?.tasks) || !Array.isArray(doingPayload?.tasks)) {
      throw new Error("任务列表响应格式无效");
    }

    const seenIds = new Set();
    const tasks = [
      ...todoPayload.tasks.filter((task) => task?.status === "todo"),
      ...doingPayload.tasks.filter((task) => task?.status === "doing"),
    ].filter((task) => {
      const id = normalizeFocusTaskId(task.id);
      if (id === null || seenIds.has(id)) return false;
      seenIds.add(id);
      return true;
    });

    const renderTasks = (preferredId = normalizeFocusTaskId(select.value)) => {
      const query = String(searchInput?.value ?? "").trim().toLowerCase();
      const visibleTasks = query
        ? tasks.filter((task) => String(task.title ?? "").toLowerCase().includes(query))
        : tasks;
      const placeholder = tasks.length === 0
        ? "暂无可专注任务"
        : visibleTasks.length === 0
          ? "暂无匹配任务"
          : "选择任务...";

      select.innerHTML = `<option value="">${placeholder}</option>`;
      visibleTasks.forEach((task) => {
        const opt = document.createElement("option");
        const taskId = normalizeFocusTaskId(task.id);
        const taskTitle = String(task.title ?? "");
        const statusLabel = task.status === "todo" ? "待办" : "进行中";
        const hasEstimate = Number.isSafeInteger(task.estimated_minutes) && task.estimated_minutes >= 1 && task.estimated_minutes <= 120;
        const estimateLabel = hasEstimate ? `${task.estimated_minutes} 分钟` : "未估时";
        opt.value = taskId;
        opt.dataset.taskTitle = taskTitle;
        opt.textContent = `${taskTitle}（${statusLabel} · #${taskId} · ${estimateLabel}）`;
        if (hasEstimate) {
          opt.dataset.estimatedMinutes = String(task.estimated_minutes);
        }
        select.appendChild(opt);
      });
      const selectedIndex = preferredId === null
        ? -1
        : Array.from(select.options).findIndex((option) => normalizeFocusTaskId(option.value) === preferredId);
      if (selectedIndex > 0) {
        select.selectedIndex = selectedIndex;
        select.value = select.options[selectedIndex].value;
      } else {
        select.selectedIndex = 0;
        select.value = "";
      }
      select.disabled = visibleTasks.length === 0;
    };

    if (searchInput) searchInput.oninput = () => renderTasks();
    renderTasks(selectedId);
  } catch {
    select.innerHTML = '<option value="">任务加载失败，请刷新重试</option>';
    select.value = "";
    select.selectedIndex = 0;
    select.disabled = true;
    if (loadError) {
      loadError.textContent = "无法加载可专注任务，请检查网络连接后刷新重试。";
      loadError.hidden = false;
    }
  } finally {
    updateEstimatedFocusStartAvailability();
  }
}

function normalizeFocusTaskId(value) {
  if (typeof value === "string" ? !/^\d+$/.test(value) : typeof value !== "number") return null;
  const id = Number(value);
  return Number.isSafeInteger(id) && id > 0 ? String(id) : null;
}

function formatFocusMinutes(value) {
  const parsed = Number(value);
  const minutes = Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
  return Number.isInteger(minutes) ? String(minutes) : minutes.toFixed(1);
}

function appendFocusStat(stats, labelText, valueText, className = "") {
  const item = document.createElement("span");
  if (className) item.className = className;
  const label = document.createElement("span");
  label.textContent = labelText;
  const value = document.createElement("strong");
  value.textContent = valueText;
  item.appendChild(label);
  item.appendChild(value);
  stats.appendChild(item);
}

function renderFocusStatsSummary(data, stats) {
  stats.replaceChildren();
  appendFocusStat(stats, "今日", `${formatFocusMinutes(data.total_minutes_today ?? 0)}m`, "focus-stats-today");
  appendFocusStat(stats, "近7天", `${formatFocusMinutes(data.total_minutes_week ?? 0)}m`);
  const sessionCount = Number(data.total_sessions_week);
  appendFocusStat(stats, "近7天实际次数", Number.isFinite(sessionCount) && sessionCount >= 0 ? String(sessionCount) : "0");

  const attributionNote = document.createElement("small");
  attributionNote.className = "focus-stats-note";
  attributionNote.textContent = "Focus今日时长按开始时的本地日期归属；每日复盘按结束时的本地日期归属。";
  stats.appendChild(attributionNote);

  const legacyCount = Number(data.legacy_sessions_week);
  if (Number.isFinite(legacyCount) && legacyCount > 0) {
    const note = document.createElement("small");
    note.className = "focus-stats-note";
    note.textContent = `另有 ${legacyCount} 条旧记录仅含计划时长，未计入实际统计。`;
    stats.appendChild(note);
  }
}

function focusSessionTimestamp(session) {
  const value = session.ended_at || session.started_at;
  const timestamp = typeof value === "string" ? Date.parse(value) : Number.NaN;
  return Number.isFinite(timestamp) ? timestamp : Number.NEGATIVE_INFINITY;
}

function formatFocusSessionTime(session) {
  const timestamp = focusSessionTimestamp(session);
  return Number.isFinite(timestamp) ? new Date(timestamp).toLocaleString() : "时间未知";
}

function formatFocusSessionDuration(session) {
  if (session.is_actual === true) {
    const rawSeconds = session.actual_seconds;
    const seconds = rawSeconds === null || rawSeconds === undefined || rawSeconds === ""
      ? Number.NaN
      : Number(rawSeconds);
    return Number.isFinite(seconds) && seconds >= 0
      ? `实际时长 ${seconds} 秒`
      : "实际时长未知";
  }

  const rawPlannedMinutes = session.planned_minutes;
  const plannedMinutes = rawPlannedMinutes === null || rawPlannedMinutes === undefined || rawPlannedMinutes === ""
    ? Number.NaN
    : Number(rawPlannedMinutes);
  return Number.isFinite(plannedMinutes) && plannedMinutes >= 0
    ? `实际时长未知 · 仅计划时长 ${formatFocusMinutes(plannedMinutes)} 分钟`
    : "实际时长未知 · 仅计划时长";
}

function focusSessionOutcomeLabel(outcome) {
  if (outcome === "completed") return "已完成";
  if (outcome === "stopped") return "已停止";
  return "状态未知";
}

function renderFocusSessionHistory(sessions) {
  const list = document.getElementById("focusSessionHistoryList");
  const message = document.getElementById("focusSessionHistoryMessage");
  if (!list || !message) return;

  list.replaceChildren();
  const recent = (Array.isArray(sessions) ? sessions : [])
    .filter((session) => session && typeof session === "object")
    .map((session, index) => ({ session, index, timestamp: focusSessionTimestamp(session) }))
    .sort((left, right) => {
      if (left.timestamp === right.timestamp) return left.index - right.index;
      return left.timestamp > right.timestamp ? -1 : 1;
    })
    .slice(0, 3);

  if (recent.length === 0) {
    message.dataset.state = "empty";
    message.textContent = "暂无专注记录";
    return;
  }

  message.dataset.state = "ready";
  message.textContent = "";
  for (const { session } of recent) {
    const item = document.createElement("li");
    item.className = "focus-session-history-item";
    const heading = document.createElement("div");
    heading.className = "focus-session-history-heading";
    const title = document.createElement("span");
    title.className = "focus-session-history-task";
    const taskId = typeof session.task_id === "string" || typeof session.task_id === "number"
      ? String(session.task_id)
      : "未知";
    title.textContent = `任务 #${taskId}`;
    const outcome = document.createElement("span");
    outcome.className = "focus-session-history-outcome";
    outcome.textContent = focusSessionOutcomeLabel(session.outcome);
    heading.appendChild(title);
    heading.appendChild(outcome);

    const details = document.createElement("div");
    details.className = "focus-session-history-details";
    const time = document.createElement("time");
    time.className = "focus-session-history-time";
    time.textContent = formatFocusSessionTime(session);
    const duration = document.createElement("span");
    duration.className = "focus-session-history-duration";
    duration.textContent = formatFocusSessionDuration(session);
    details.appendChild(time);
    details.appendChild(duration);

    item.appendChild(heading);
    item.appendChild(details);
    list.appendChild(item);
  }
}

async function loadFocusStats() {
  const stats = document.getElementById("focusStats");
  if (!stats) return;
  const historyMessage = document.getElementById("focusSessionHistoryMessage");
  if (historyMessage) {
    historyMessage.dataset.state = "loading";
    historyMessage.textContent = "正在加载最近专注…";
  }
  try {
    const data = await requestJson("/api/focus/stats");
    renderFocusStatsSummary(data, stats);
    renderFocusSessionHistory(data.sessions);
  } catch {
    stats.replaceChildren();
    const historyList = document.getElementById("focusSessionHistoryList");
    if (historyList) historyList.replaceChildren();
    if (historyMessage) {
      historyMessage.dataset.state = "error";
      historyMessage.textContent = "无法加载专注记录，请检查网络连接后重试。";
    }
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

function inspectSelectedEstimatedFocusTask() {
  const select = document.getElementById("focusTaskSelect");
  const option = select?.options?.[select.selectedIndex];
  if (!option || !option.value) {
    return { task: null, message: "请先选择一个任务。" };
  }

  const taskId = Number(option.value);
  if (!Number.isSafeInteger(taskId) || taskId <= 0) {
    return { task: null, message: "无法读取所选任务，请刷新后重试。" };
  }

  const rawEstimate = option.dataset?.estimatedMinutes;
  const estimate = rawEstimate === undefined || rawEstimate === "" ? NaN : Number(rawEstimate);
  if (!Number.isSafeInteger(estimate) || estimate < 1 || estimate > 120) {
    return {
      task: null,
      message: "此任务缺少有效估时；按任务估时开始需要 1–120 分钟的安全整数，请先为任务设置估时。",
    };
  }

  return {
    task: { id: taskId, title: String(option.dataset?.taskTitle ?? ""), estimated_minutes: estimate },
    message: `将按此任务的估时（${estimate} 分钟）开始。`,
  };
}

function updateEstimatedFocusStartAvailability() {
  const button = document.getElementById("focusEstimatedStartBtn");
  const help = document.getElementById("focusEstimatedStartHelp");
  const { task, message } = inspectSelectedEstimatedFocusTask();
  if (help) help.textContent = message;
  if (button) {
    button.dataset.estimateValid = task ? "true" : "false";
    button.disabled = _focusStartPending || _focusTimerState.phase !== "idle" || !task;
  }
}

export function startSuggestedFocus(suggestion) {
  if (!suggestion || typeof suggestion !== "object") return Promise.resolve();
  return focusStart(suggestion);
}

function focusStartSelectedEstimate() {
  const { task } = inspectSelectedEstimatedFocusTask();
  if (!task) return Promise.resolve();
  return startSuggestedFocus(task);
}

async function focusStart(taskOverride = null) {
  if (_focusStartPending || _focusTimerState.phase !== "idle") return;
  if (!_focusRecoveryStore?.userId) {
    _showToast("无法确认当前账号，暂不能安全保存专注恢复状态");
    return;
  }
  _focusStartPending = true;
  syncFocusEntryAvailability();
  try {
    await _focusTabCoordinator.withStartLock(async () => {
      const existing = _focusRecoveryStore.load();
      if (existing) {
        if (_focusTabCoordinator.hasLiveOwner(existing.session_id) && !existing.finish_payload) {
          showMirroredFocusSnapshot(existing);
        } else {
          restoreFocusSnapshot(existing);
        }
        return;
      }
      _focusStartLockHeld = true;
      try {
        await focusStartWithinLock(taskOverride);
      } finally {
        _focusStartLockHeld = false;
      }
    });
  } catch (error) {
    const startError = document.getElementById("focusStartError");
    if (startError) {
      startError.textContent = formatFocusStartError(error);
      startError.hidden = false;
    }
    _showToast(`专注未开始：${error.message || "无法连接服务"}`);
  } finally {
    _focusStartLockHeld = false;
    _focusStartPending = false;
    syncFocusEntryAvailability();
  }
}

async function focusStartWithinLock(taskOverride = null) {
  if ((_focusStartPending && !_focusStartLockHeld) || _focusTimerState.phase !== "idle") return;
  if (!_focusRecoveryStore?.userId) {
    _showToast("无法确认当前账号，暂不能安全保存专注恢复状态");
    return;
  }
  const select = document.getElementById("focusTaskSelect");
  const isSuggestedTask = taskOverride !== null;
  const taskId = isSuggestedTask ? (taskOverride.task_id ?? taskOverride.id) : (select ? Number.parseInt(select.value, 10) : NaN);
  const title = isSuggestedTask
    ? String(taskOverride.title || "")
    : (Number.isSafeInteger(taskId) && taskId > 0 ? String(select.options[select.selectedIndex]?.dataset?.taskTitle ?? "") : "");
  const startError = document.getElementById("focusStartError");
  if (startError) {
    startError.hidden = true;
    startError.textContent = "";
  }

  if (!Number.isSafeInteger(taskId) || taskId <= 0) {
    _showToast("请先选择一个任务");
    return;
  }

  const suggestedDuration = Number(taskOverride?.estimated_minutes);
  const duration = isSuggestedTask && Number.isSafeInteger(suggestedDuration) && suggestedDuration >= 1 && suggestedDuration <= 120
    ? suggestedDuration
    : _focusTimerState.durationMinutes;
  const startBtn = document.getElementById("focusStartBtn");
  _focusStartPending = true;
  syncFocusEntryAvailability();
  if (startBtn) startBtn.disabled = true;

  let session;
  try {
    session = await startFocusSession(requestJson, taskId, duration);
    if (!session.session_id || !session.started_at) throw new Error("无法初始化专注记录");
  } catch (error) {
    const message = error?.status == null
      ? "无法确认开始；再次点击将从新时间重新开始"
      : formatFocusStartError(error);
    if (startError) {
      startError.textContent = message;
      startError.hidden = false;
    }
    _showToast(message);
    _focusStartPending = false;
    syncFocusEntryAvailability();
    return;
  } finally {
    if (startBtn) startBtn.disabled = _focusStartPending || _focusTimerState.phase !== "idle";
  }

  _focusTimerState.durationMinutes = duration;
  _focusTimerState.taskId = taskId;
  _focusTimerState.taskTitle = title;
  _focusTimerState.sessionId = session.session_id;
  _focusTimerState.startedAt = session.started_at;
  _focusTimerState.remainingSeconds = duration * 60;
  _focusTimerState.elapsedSeconds = 0;
  _focusTimerState.phase = "running";
  _focusTimerState.paused = false;
  _focusTimerState.completionPending = false;
  _focusStartPending = false;
  syncFocusEntryAvailability();
  _focusTimerState.sessionSaved = false;
  _focusTimerState.finishPayload = null;
  _focusFinishRequest = null;
  _focusFinishCoordinator = new FocusFinishCoordinator(_focusRecoveryStore, requestJson);
  _focusClock.start(0, FOCUS_MAX_SAMPLE_GAP_MS);

  document.getElementById("focusIdle").classList.add("hidden");
  document.getElementById("focusRecovery").classList.add("hidden");
  document.getElementById("focusBreak").classList.add("hidden");
  document.getElementById("focusRunning").classList.remove("hidden");
  document.getElementById("focusCurrentTask").textContent = title;
  document.getElementById("focusPauseBtn").textContent = "暂停";
  document.getElementById("focusPauseBtn").disabled = false;
  document.getElementById("focusStopBtn").textContent = "结束并保存";
  document.getElementById("focusStopBtn").disabled = false;

  updateFocusCountdownDisplay();
  if (!persistFocusSnapshot({ state: "running", elapsedSeconds: 0, finishPayload: null })) {
    releaseFocusTabOwner(session.session_id);
    clearInterval(_focusTimerState.intervalId);
    _focusClock.reset();
    _focusTimerState.phase = "idle";
    syncFocusEntryAvailability();
    document.getElementById("focusRunning").classList.add("hidden");
    document.getElementById("focusIdle").classList.remove("hidden");
    resetFocusSession();
    const startError = document.getElementById("focusStartError");
    if (startError) {
      startError.textContent = "本地恢复快照无法保存，专注未开始";
      startError.hidden = false;
    }
    _showToast("本地恢复快照无法保存，专注未开始");
    return;
  }
  startFocusOwnerHeartbeat();
  clearInterval(_focusTimerState.intervalId);
  _focusTimerState.intervalId = setInterval(focusTick, 250);
}

function focusTick() {
  if (_focusTimerState.phase !== "running") return;
  _focusTabCoordinator?.refreshOwner(_focusTimerState.sessionId);
  if (_focusTimerState.paused) return;
  const plannedSeconds = _focusTimerState.durationMinutes * 60;
  const elapsedSeconds = Math.min(plannedSeconds, _focusClock.elapsedSeconds());
  _focusTimerState.elapsedSeconds = elapsedSeconds;
  _focusTimerState.remainingSeconds = remainingFocusSeconds(plannedSeconds, elapsedSeconds);
  updateFocusCountdownDisplay();
  if (!persistFocusSnapshot({ state: "running", elapsedSeconds })) {
    _focusTimerState.elapsedSeconds = Math.max(0, _focusLastPersistedSeconds);
    _focusTimerState.remainingSeconds = remainingFocusSeconds(plannedSeconds, _focusTimerState.elapsedSeconds);
    _focusClock.reset();
    _focusClock.start(_focusTimerState.elapsedSeconds, FOCUS_MAX_SAMPLE_GAP_MS);
    _focusClock.pause();
    _focusTimerState.paused = true;
    document.getElementById("focusPauseBtn").textContent = "继续";
    updateFocusCountdownDisplay();
    _showToast("本地恢复快照无法更新，专注计时已暂停");
    return;
  }
  if (_focusTimerState.remainingSeconds <= 0) {
    clearInterval(_focusTimerState.intervalId);
    void focusOnComplete();
  }
}

async function saveFocusSession() {
  if (_focusTimerState.sessionSaved) return _focusTimerState.sessionResult;
  if (_focusFinishRequest) return _focusFinishRequest;

  const elapsedSeconds = _focusTimerState.finishPayload?.actual_seconds ?? focusElapsedSeconds();
  const draft = {
    version: FOCUS_SNAPSHOT_VERSION,
    user_id: _focusRecoveryStore?.userId,
    session_id: _focusTimerState.sessionId,
    task_id: _focusTimerState.taskId,
    planned_minutes: _focusTimerState.durationMinutes,
    elapsed_seconds: elapsedSeconds,
    state: "paused",
    started_at: _focusTimerState.startedAt,
    task_title: _focusTimerState.taskTitle,
  };
  if (_focusTimerState.finishPayload) draft.finish_payload = { ..._focusTimerState.finishPayload };
  _focusTimerState.paused = true;
  _focusClock.pause();
  const submission = _focusFinishCoordinator.submit(draft, elapsedSeconds);
  if (_focusFinishCoordinator.lastPayload) {
    _focusTimerState.finishPayload = { ..._focusFinishCoordinator.lastPayload };
    _focusTimerState.elapsedSeconds = _focusTimerState.finishPayload.actual_seconds;
    _focusTimerState.remainingSeconds = remainingFocusSeconds(
      _focusTimerState.durationMinutes * 60,
      _focusTimerState.finishPayload.actual_seconds,
    );
  }
  _focusFinishRequest = submission.then((result) => {
    _focusTimerState.sessionSaved = true;
    _focusTimerState.sessionResult = {
      actual_seconds: result.actual_seconds,
      outcome: result.outcome,
      snapshotSettled: result.snapshotSettled,
    };
    return _focusTimerState.sessionResult;
  }).finally(() => {
    _focusFinishRequest = null;
  });
  return _focusFinishRequest;
}

async function focusOnComplete() {
  if (_focusTimerState.phase !== "running" || _focusTimerState.completionPending) return;
  _focusTimerState.completionPending = true;
  _focusTimerState.paused = true;
  _focusClock.pause();
  _focusTimerState.elapsedSeconds = Math.min(_focusTimerState.durationMinutes * 60, _focusClock.elapsedSeconds());
  _focusTimerState.remainingSeconds = 0;
  updateFocusCountdownDisplay();
  document.getElementById("focusPauseBtn").disabled = true;
  const stopBtn = document.getElementById("focusStopBtn");
  stopBtn.disabled = true;

  try {
    await saveFocusSession();
  } catch (error) {
    stopBtn.disabled = false;
    stopBtn.textContent = "重试保存";
    _showToast(`时长已到，但记录未保存：${error.message || "请重试"}`);
    return;
  }

  enterFocusBreak();
}

function enterFocusBreak() {
  releaseFocusTabOwner(_focusTimerState.sessionId);
  clearInterval(_focusTimerState.intervalId);
  _focusTimerState.phase = "break";
  syncFocusEntryAvailability();
  _focusTimerState.completionPending = false;
  _focusTimerState.breakSeconds = 5 * 60;

  document.getElementById("focusRunning").classList.add("hidden");
  document.getElementById("focusRecovery").classList.add("hidden");
  document.getElementById("focusBreak").classList.remove("hidden");
  document.getElementById("focusBreakTask").textContent = _focusTimerState.taskTitle;
  document.getElementById("focusBreakStartBtn").textContent = `再专注一轮（${formatFocusMinutes(_focusTimerState.durationMinutes)}分钟）`;
  const startError = document.getElementById("focusStartError");
  if (startError) {
    startError.hidden = true;
    startError.textContent = "";
  }
  const breakStartError = document.getElementById("focusBreakStartError");
  if (breakStartError) {
    breakStartError.hidden = true;
    breakStartError.textContent = "";
  }

  // 浏览器通知
  if (typeof Notification !== "undefined") {
    if (Notification.permission === "granted") {
      new Notification("专注完成！", { body: "该休息一下了 ☕" });
    } else if (Notification.permission !== "denied") {
      Notification.requestPermission();
    }
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
    void ctx.close();
  } catch (_) {}

  _focusBreakIntervalId = setInterval(focusBreakTick, 1000);
  updateFocusBreakDisplay();
  void loadFocusStats();
}

function focusBreakTick() {
  _focusTimerState.breakSeconds--;
  if (_focusTimerState.breakSeconds <= 0) {
    _focusTimerState.breakSeconds = 0;
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

async function focusBreakStartNextRound() {
  if (_focusTimerState.phase !== "break" || _focusStartPending || _focusStartLockHeld) return;

  const previousState = { ..._focusTimerState };
  const task = {
    task_id: _focusTimerState.taskId,
    title: _focusTimerState.taskTitle,
    estimated_minutes: _focusTimerState.durationMinutes,
  };
  const actionButtons = [
    document.getElementById("focusBreakStartBtn"),
    document.getElementById("focusSkipBreakBtn"),
    document.getElementById("focusDoneBtn"),
  ].filter(Boolean);
  const startError = document.getElementById("focusStartError");
  const breakStartError = document.getElementById("focusBreakStartError");

  clearInterval(_focusBreakIntervalId);
  _focusBreakIntervalId = null;
  _focusTimerState.breakSeconds = 0;
  updateFocusBreakDisplay();
  if (breakStartError) {
    breakStartError.hidden = true;
    breakStartError.textContent = "";
  }
  if (startError) {
    startError.hidden = true;
    startError.textContent = "";
  }
  actionButtons.forEach((button) => { button.disabled = true; });
  _focusTimerState.phase = "idle";
  syncFocusEntryAvailability();

  try {
    await startSuggestedFocus(task);
  } catch (error) {
    if (startError) {
      startError.textContent = formatFocusStartError(error);
      startError.hidden = false;
    }
  }

  if (_focusTimerState.phase === "idle") {
    Object.assign(_focusTimerState, previousState, { phase: "break", breakSeconds: 0 });
    document.getElementById("focusRunning").classList.add("hidden");
    document.getElementById("focusIdle").classList.add("hidden");
    document.getElementById("focusRecovery").classList.add("hidden");
    document.getElementById("focusBreak").classList.remove("hidden");
    syncFocusEntryAvailability();
    const message = startError && !startError.hidden
      ? startError.textContent
      : "无法确认当前账号，暂不能安全保存专注恢复状态";
    if (breakStartError) {
      breakStartError.textContent = message;
      breakStartError.hidden = false;
    }
  }
  if (_focusTimerState.phase === "break") {
    actionButtons.forEach((button) => { button.disabled = false; });
  }
}

async function focusTogglePause() {
  if (_focusTimerState.phase !== "running" || _focusTimerState.completionPending) return;
  if (_focusTimerState.paused) {
    if (!persistFocusSnapshot({ state: "running", elapsedSeconds: _focusTimerState.elapsedSeconds })) {
      _showToast("无法保存运行状态，专注仍保持暂停");
      return;
    }
    _focusClock.resume();
    _focusTimerState.paused = false;
  } else {
    _focusClock.pause();
    _focusTimerState.elapsedSeconds = focusElapsedSeconds();
    _focusTimerState.paused = true;
    _focusTimerState.remainingSeconds = remainingFocusSeconds(_focusTimerState.durationMinutes * 60, _focusTimerState.elapsedSeconds);
    if (!persistFocusSnapshot({ state: "paused", elapsedSeconds: _focusTimerState.elapsedSeconds })) {
      _showToast("已暂停，但本地恢复快照未能更新");
    }
  }
  const btn = document.getElementById("focusPauseBtn");
  btn.textContent = _focusTimerState.paused ? "继续" : "暂停";
  updateFocusCountdownDisplay();
}

async function focusStop() {
  if (_focusTimerState.phase !== "running") return;
  clearInterval(_focusTimerState.intervalId);
  _focusTimerState.paused = true;
  _focusClock.pause();
  const stopBtn = document.getElementById("focusStopBtn");
  stopBtn.disabled = true;
  let result;

  try {
    result = await saveFocusSession();
  } catch (error) {
    stopBtn.disabled = false;
    stopBtn.textContent = "重试保存";
    document.getElementById("focusPauseBtn").disabled = true;
    _showToast(`记录未保存：${error.message || "请重试"}`);
    return;
  }

  handleStoppedFocusSuccess(result);
}

function focusSkipBreak() {
  clearInterval(_focusBreakIntervalId);
  _focusTimerState.phase = "idle";
  syncFocusEntryAvailability();
  document.getElementById("focusBreak").classList.add("hidden");
  document.getElementById("focusRecovery").classList.add("hidden");
  document.getElementById("focusIdle").classList.remove("hidden");
  resetFocusSession();
  void loadFocusStats();
  void populateFocusTaskSelect();
}

async function focusDone() {
  clearInterval(_focusBreakIntervalId);

  const taskId = _focusTimerState.taskId;
  if (taskId) {
    let result;
    try {
      result = await completeTask(taskId);
    } catch (error) {
      result = { status: "error", error };
    }
    if (result?.status !== "success") {
      showFocusTaskCompletionFeedback(taskId, result);
      return;
    }
  }

  closeFocusBreak();
}

async function retryFocusTaskCompletion() {
  const taskId = _focusTimerState.taskId;
  if (!taskId || typeof completeTask.getPendingIntent !== "function" || typeof completeTask.retry !== "function") return;

  let intent;
  try {
    intent = completeTask.getPendingIntent(taskId);
  } catch {
    return;
  }
  if (!intent || String(intent.task_id) !== String(taskId) || intent.status !== "uncertain" || !intent.idempotency_key) return;

  const retryButton = document.getElementById("focusTaskCompletionRetry");
  if (retryButton) retryButton.disabled = true;

  let result;
  try {
    result = await completeTask.retry(taskId);
  } catch (error) {
    result = { status: "error", error };
  }
  if (result?.status === "success") {
    closeFocusBreak();
    return;
  }
  showFocusTaskCompletionFeedback(taskId, result);
}

function showFocusTaskCompletionFeedback(taskId, result) {
  const panel = document.getElementById("focusBreak");
  let status = document.getElementById("focusTaskCompletionStatus");
  if (!status) {
    status = document.createElement("p");
    status.id = "focusTaskCompletionStatus";
    status.className = "focus-task-completion-status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    status.style.cssText = "width: 100%; max-width: 100%; margin: 0; text-align: center; font-size: 12px; line-height: 1.5; color: var(--text-2);";
    panel.appendChild(status);
  }

  let intent = null;
  try {
    intent = completeTask.getPendingIntent?.(taskId) || null;
  } catch {
    // Feedback remains visible even if local intent storage cannot be read.
  }
  const hasMatchingIntent = intent && String(intent.task_id) === String(taskId);
  const awaitingConfirmation = result?.status === "uncertain" || result?.status === "pending";
  const canRetry = Boolean(
    awaitingConfirmation
    && hasMatchingIntent
    && intent.status === "uncertain"
    && intent.idempotency_key
    && typeof completeTask.retry === "function"
  );
  if (awaitingConfirmation) {
    const detail = result.status === "pending" ? "原请求仍在处理中或等待核实" : "请求结果尚不确定";
    const nextStep = canRetry
      ? "可显式使用原请求重试。"
      : hasMatchingIntent
        ? "原请求仍在处理中或不可重试。"
        : "当前没有可重试的待确认请求。";
    status.setAttribute("role", "status");
    status.textContent = `任务完成待确认：${detail}。${nextStep}休息面板保持打开。`;
  } else {
    const error = result?.error;
    const errorText = typeof error?.message === "string" && error.message
      ? error.message
      : typeof error === "string" && error
        ? error
        : "完成请求失败。";
    status.setAttribute("role", "alert");
    status.textContent = errorText;
  }

  let retryButton = document.getElementById("focusTaskCompletionRetry");
  if (!retryButton) {
    retryButton = document.createElement("button");
    retryButton.id = "focusTaskCompletionRetry";
    retryButton.type = "button";
    retryButton.className = "primary focus-task-completion-retry";
    retryButton.textContent = "使用原请求重试";
    retryButton.setAttribute("aria-label", "使用原请求重试此任务的完成操作");
    retryButton.addEventListener("click", retryFocusTaskCompletion);
    panel.appendChild(retryButton);
  }
  retryButton.hidden = !canRetry;
  retryButton.disabled = !canRetry;
  const doneButton = document.getElementById("focusDoneBtn");
  if (doneButton) doneButton.disabled = true;
}

function closeFocusBreak() {
  _focusTimerState.phase = "idle";
  syncFocusEntryAvailability();
  document.getElementById("focusBreak").classList.add("hidden");
  document.getElementById("focusRecovery").classList.add("hidden");
  document.getElementById("focusIdle").classList.remove("hidden");
  resetFocusSession();
  void loadFocusStats();
  void populateFocusTaskSelect();
}

function resetFocusSession() {
  _focusClock.reset();
  _focusTimerState.taskId = null;
  _focusTimerState.taskTitle = "";
  _focusTimerState.remainingSeconds = 0;
  _focusTimerState.elapsedSeconds = 0;
  _focusTimerState.paused = false;
  _focusTimerState.sessionId = null;
  _focusTimerState.startedAt = null;
  _focusTimerState.completionPending = false;
  _focusTimerState.sessionSaved = false;
  _focusTimerState.sessionResult = null;
  _focusTimerState.finishPayload = null;
  _focusFinishRequest = null;
  _focusLastPersistedSeconds = -1;
  document.getElementById("focusTaskCompletionStatus")?.remove();
  document.getElementById("focusTaskCompletionRetry")?.remove();
  document.getElementById("focusDoneBtn").disabled = false;
  document.getElementById("focusPauseBtn").textContent = "暂停";
  document.getElementById("focusPauseBtn").disabled = false;
  document.getElementById("focusStopBtn").disabled = false;
}
