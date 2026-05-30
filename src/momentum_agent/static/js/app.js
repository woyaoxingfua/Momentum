import { requestJson, logout } from "./api.js";

if (!localStorage.getItem("momentum_token")) {
  window.location.href = "/login.html";
}
import { initTasks, saveEdit, loadTasks, setTaskStatusFilter } from "./tasks.js";
import { initChat, setAfterChat, sendChat, sendToAgent } from "./chat.js";
import { initAdvice, loadAdvice, loadReview } from "./advice.js";
import { initConfig, loadConfig, saveConfig } from "./config.js";
import { initHeartbeat, loadHeartbeatConfig, startHeartbeatChecks } from "./heartbeat.js";

const els = {
  taskInput: document.querySelector("#taskInput"),
  addTaskButton: document.querySelector("#addTaskButton"),
  planTaskButton: document.querySelector("#planTaskButton"),
  adviseButton: document.querySelector("#adviseButton"),
  reviewButton: document.querySelector("#reviewButton"),
  refreshButton: document.querySelector("#refreshButton"),
  searchInput: document.querySelector("#searchInput"),
  exportButton: document.querySelector("#exportButton"),
  importFile: document.querySelector("#importFile"),
  tasks: document.querySelector("#tasks"),
  taskCount: document.querySelector("#taskCount"),
  adviceText: document.querySelector("#adviceText"),
  heartbeatSection: document.querySelector("#heartbeatSection"),
  heartbeatText: document.querySelector("#heartbeatText"),
  heartbeatDismiss: document.querySelector("#heartbeatDismiss"),
  chatForm: document.querySelector("#chatForm"),
  chatInput: document.querySelector("#chatInput"),
  chatLog: document.querySelector("#chatLog"),
  providerStatus: document.querySelector("#providerStatus"),
  editDialog: document.querySelector("#editDialog"),
  editTaskId: document.querySelector("#editTaskId"),
  editTitle: document.querySelector("#editTitle"),
  editDue: document.querySelector("#editDue"),
  editPriority: document.querySelector("#editPriority"),
  editEstimate: document.querySelector("#editEstimate"),
  editTags: document.querySelector("#editTags"),
  editNotes: document.querySelector("#editNotes"),
  editCancelButton: document.querySelector("#editCancelButton"),
  configApiKey: document.querySelector("#configApiKey"),
  configApiBase: document.querySelector("#configApiBase"),
  configModel: document.querySelector("#configModel"),
  configCapacity: document.querySelector("#configCapacity"),
  configWorkStart: document.querySelector("#configWorkStart"),
  configWorkEnd: document.querySelector("#configWorkEnd"),
  configHeartbeatEnabled: document.querySelector("#configHeartbeatEnabled"),
  configHeartbeatStart: document.querySelector("#configHeartbeatStart"),
  configHeartbeatEnd: document.querySelector("#configHeartbeatEnd"),
  configHeartbeatInterval: document.querySelector("#configHeartbeatInterval"),
  configSaveButton: document.querySelector("#configSaveButton"),
};

// ── provider ────────────────────────────────────────────────────────

async function loadProvider() {
  const payload = await requestJson("/api/provider");
  els.providerStatus.textContent = payload.provider;
  isProviderConfigured = payload.configured === true;
}

// ── refresh ─────────────────────────────────────────────────────────

async function refreshAll() {
  await Promise.all([loadTasks(), loadAdvice(), loadProvider()]);
}

// ── search ──────────────────────────────────────────────────────────

let searchTimer = null;
let isProviderConfigured = false;

function onSearchInput() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    const q = els.searchInput.value.trim();
    if (!q) { await loadTasks(); return; }
    const payload = await requestJson(`/api/tasks?q=${encodeURIComponent(q)}`);
    const { renderTasks } = await import("./tasks.js");
    renderTasks(payload.tasks);
  }, 200);
}

// ── export / import ─────────────────────────────────────────────────

async function exportData() {
  const payload = await requestJson("/api/export");
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "momentum-export.json";
  a.click();
  URL.revokeObjectURL(url);
}

async function importData(file) {
  try {
    const text = await file.text();
    const data = JSON.parse(text);
    await requestJson("/api/import", {
      method: "POST",
      body: JSON.stringify({ data }),
    });
    file.target.value = "";
    await refreshAll();
  } catch (err) {
    alert(`导入失败：${err.message}`);
  }
}

// ── tasks ───────────────────────────────────────────────────────────

async function addTask() {
  const text = els.taskInput.value.trim();
  if (!text) return;
  els.addTaskButton.disabled = true;
  try {
    await requestJson("/api/tasks", { method: "POST", body: JSON.stringify({ text }) });
    els.taskInput.value = "";
    await refreshAll();
  } finally {
    els.addTaskButton.disabled = false;
  }
}

async function planTask() {
  const text = els.taskInput.value.trim();
  if (!text) return;
  
  if (isProviderConfigured) {
    els.planTaskButton.disabled = true;
    try {
      const message = `请帮我将以下任务拆分成 3-5 个具体、可执行的子任务，并创建到我的任务列表中。

任务：${text}

拆解原则：
1. 每个子任务控制在 10-60 分钟内可完成
2. 子任务之间要有先后逻辑：先收集信息，再动手做，最后检查
3. 子任务标题要具体，包含动作动词（梳理/写出/查找/对比/整理/提交/发送/确认）
4. 总时间不要超过父任务的合理范围（通常 60-180 分钟）

请帮我创建这些任务。`;
      await sendToAgent(message);
      els.taskInput.value = "";
      await refreshAll();
    } finally {
      els.planTaskButton.disabled = false;
    }
  } else {
    els.planTaskButton.disabled = true;
    try {
      await requestJson("/api/plan", { method: "POST", body: JSON.stringify({ text }) });
      els.taskInput.value = "";
      await refreshAll();
    } finally {
      els.planTaskButton.disabled = false;
    }
  }
}

// ── init ────────────────────────────────────────────────────────────

initTasks(
  { tasks: els.tasks, taskCount: els.taskCount },
  { editDialog: els.editDialog, editTaskId: els.editTaskId, editTitle: els.editTitle,
    editDue: els.editDue, editPriority: els.editPriority, editEstimate: els.editEstimate,
    editNotes: els.editNotes },
);
initChat(els.chatLog, els.chatInput);
setAfterChat(refreshAll);
initAdvice(els.adviceText);
initConfig(
  { 
    configApiKey: els.configApiKey, 
    configApiBase: els.configApiBase,
    configModel: els.configModel,
    configCapacity: els.configCapacity, 
    configWorkStart: els.configWorkStart, 
    configWorkEnd: els.configWorkEnd 
  },
  els.adviceText,
);

els.addTaskButton.addEventListener("click", addTask);
els.planTaskButton.addEventListener("click", planTask);
els.adviseButton.addEventListener("click", async () => {
  if (isProviderConfigured) {
    const tasks = await loadTasks();
    await loadAdviceWithAI(tasks);
  } else {
    await loadAdvice();
  }
});
els.reviewButton.addEventListener("click", async () => {
  if (isProviderConfigured) {
    const tasks = await loadTasks();
    await loadReviewWithAI(tasks);
  } else {
    await loadReview();
  }
});
els.refreshButton.addEventListener("click", refreshAll);
els.chatForm.addEventListener("submit", sendChat);
els.taskInput.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") addTask();
});
els.editDialog.querySelector("form").addEventListener("submit", saveEdit);
els.editCancelButton.addEventListener("click", () => els.editDialog.close());
els.configSaveButton.addEventListener("click", saveConfig);
els.searchInput.addEventListener("input", onSearchInput);
els.exportButton.addEventListener("click", exportData);
els.importFile.addEventListener("change", (e) => { if (e.target.files[0]) importData(e); });

document.querySelectorAll(".status-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".status-tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    setTaskStatusFilter(tab.dataset.status);
    loadTasks();
  });
});

const user = localStorage.getItem("momentum_user") || "default";
document.querySelector("#currentUser").textContent = user;
document.querySelector("#logoutButton").addEventListener("click", logout);

refreshAll().catch((error) => {
  els.adviceText.textContent = error.message;
});
loadConfig();

// 初始化心跳
initHeartbeat(els);
loadHeartbeatConfig();
startHeartbeatChecks();
