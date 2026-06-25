import { requestJson } from "./api.js";
import { initTasks, saveEdit, saveSubtask, savePostpone, bindPostponeOptions, loadTasks, setTaskStatusFilter, renderTasks } from "./tasks.js";
import { initChat, setAfterChat, sendChat, sendToAgent } from "./chat.js";
import { initAdvice, loadAdvice, loadReview, loadAdviceWithAI, loadReviewWithAI } from "./advice.js";
import { initConfig, loadConfig, saveConfig, setOnConfigSaved } from "./config.js";
import { initHeartbeat, loadHeartbeatConfig, startHeartbeatChecks } from "./heartbeat.js";

let onAfterChat = null;

// ── Auth guard ────────────────────────────────────────────────
const _hasToken = !!localStorage.getItem("momentum_token");
if (!_hasToken) {
  window.location.href = "/login.html";
}

// ── State ────────────────────────────────────────────────────
let isProviderConfigured = false;
let uploadedImages = [];

// ── Elements ─────────────────────────────────────────────────
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

const els = {
  // composer
  taskInput:      $("#taskInput"),
  addTaskButton:  $("#addTaskButton"),
  planTaskButton: $("#planTaskButton"),
  adviseButton:   $("#adviseButton"),
  reviewButton:   $("#reviewButton"),
  refreshButton:  $("#refreshButton"),
  searchInput:     $("#searchInput"),
  exportButton:   $("#exportButton"),
  importFile:      $("#importFile"),
  tasks:           $("#tasks"),
  taskCount:       $("#taskCount"),
  adviceText:      $("#adviceText"),
  // heartbeat
  heartbeatSection: $("#heartbeatSection"),
  heartbeatText:    $("#heartbeatText"),
  heartbeatDismiss: $("#heartbeatDismiss"),
  // chat
  chatForm:   $("#chatForm"),
  chatInput:  $("#chatInput"),
  chatLog:    $("#chatLog"),
  // dialogs
  editDialog:          $("#editDialog"),
  editTaskId:          $("#editTaskId"),
  editTitle:           $("#editTitle"),
  editDue:             $("#editDue"),
  editPriority:        $("#editPriority"),
  editEstimate:        $("#editEstimate"),
  editTags:            $("#editTags"),
  editNotes:           $("#editNotes"),
  editCancelButton:    $("#editCancelButton"),
  addSubtaskDialog:    $("#addSubtaskDialog"),
  addSubtaskParentId:  $("#addSubtaskParentId"),
  addSubtaskTitle:     $("#addSubtaskTitle"),
  addSubtaskDue:       $("#addSubtaskDue"),
  addSubtaskPriority:  $("#addSubtaskPriority"),
  addSubtaskEstimate:  $("#addSubtaskEstimate"),
  addSubtaskCancelButton: $("#addSubtaskCancelButton"),
  postponeDialog:      $("#postponeDialog"),
  postponeTaskId:      $("#postponeTaskId"),
  postponeDays:        $("#postponeDays"),
  postponeCancelButton:$("#postponeCancelButton"),
  // config
  configApiKey:            $("#configApiKey"),
  configApiBase:           $("#configApiBase"),
  configModel:             $("#configModel"),
  configVisionEnabled:     $("#configVisionEnabled"),
  configCapacity:          $("#configCapacity"),
  configWorkStart:         $("#configWorkStart"),
  configWorkEnd:           $("#configWorkEnd"),
  configHeartbeatEnabled:  $("#configHeartbeatEnabled"),
  configHeartbeatStart:    $("#configHeartbeatStart"),
  configHeartbeatEnd:      $("#configHeartbeatEnd"),
  configHeartbeatInterval: $("#configHeartbeatInterval"),
  configSaveButton:        $("#configSaveButton"),
  providerStatus:          $("#providerStatus"),
};

// ── Provider ──────────────────────────────────────────────────
export async function loadProvider() {
  const payload = await requestJson("/api/provider");
  els.providerStatus.textContent = payload.provider;
  isProviderConfigured = payload.configured === true;
}

// ── Refresh ───────────────────────────────────────────────────
async function refreshAll() {
  await Promise.all([loadTasks(), loadAdvice(), loadProvider()]);
}

// ── Search ────────────────────────────────────────────────────
let searchTimer = null;

function onSearchInput() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    const q = els.searchInput.value.trim();
    if (!q) { await loadTasks(); return; }
    const payload = await requestJson(`/api/tasks?q=${encodeURIComponent(q)}`);
    renderTasks(payload.tasks, true);
  }, 200);
}

// ── Export / Import ────────────────────────────────────────────
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
    await requestJson("/api/import", { method: "POST", body: JSON.stringify({ data }) });
    await refreshAll();
  } catch (err) {
    alert(`导入失败：${err.message}`);
  }
}

// ── Image upload ─────────────────────────────────────────────
function clearImages() {
  uploadedImages = [];
  const preview = $("#imagePreview");
  if (preview) { preview.innerHTML = ""; preview.style.display = "none"; }
}

function handleImageUpload(event) {
  const files = event.target.files;
  if (!files || files.length === 0) return;

  const preview = $("#imagePreview");
  preview.style.display = "flex";

  Array.from(files).forEach(file => {
    if (!file.type.startsWith("image/")) return;
    const reader = new FileReader();
    reader.onload = (e) => {
      const base64 = e.target.result.split(",")[1];
      uploadedImages.push(base64);

      const item = document.createElement("div");
      item.className = "image-preview-item";
      item.innerHTML = `
        <img src="${e.target.result}" alt="Preview" />
        <button class="remove-btn" title="移除">×</button>`;

      item.querySelector(".remove-btn").onclick = () => {
        const index = uploadedImages.indexOf(base64);
        if (index > -1) { uploadedImages.splice(index, 1); item.remove(); }
        if (uploadedImages.length === 0) preview.style.display = "none";
      };
      preview.appendChild(item);
    };
    reader.readAsDataURL(file);
  });
  event.target.value = "";
}

// ── Tasks ─────────────────────────────────────────────────────
async function addTask() {
  const text = els.taskInput.value.trim();
  if (!text && uploadedImages.length === 0) return;
  els.addTaskButton.disabled = true;
  try {
    const body = { text: text || "从图片中提取的任务" };
    if (uploadedImages.length > 0) body.images = uploadedImages;
    await requestJson("/api/tasks", { method: "POST", body: JSON.stringify(body) });
    els.taskInput.value = "";
    clearImages();
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
2. 子任务之间要有先后逻辑
3. 子任务标题要具体，包含动作动词
4. 总时间不要超过合理范围

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

// ── Mobile nav & panels ────────────────────────────────────────
const PANEL_MAP = {
  chat:     "mobileChatPanel",
  insights: "mobileInsightsPanel",
  settings: "mobileSettingsPanel",
};

function closeAllPanels() {
  document.querySelectorAll(".mobile-panel").forEach(p => p.classList.remove("open"));
  document.querySelectorAll(".mobile-nav-item").forEach(b => b.classList.remove("active"));
  const defaultBtn = document.querySelector(".mobile-nav-item[data-panel='tasks']");
  if (defaultBtn) defaultBtn.classList.add("active");
}

function openPanel(name) {
  const panelId = PANEL_MAP[name];
  if (!panelId) return;
  document.querySelectorAll(".mobile-panel").forEach(p => p.classList.remove("open"));
  document.querySelectorAll(".mobile-nav-item").forEach(b => b.classList.remove("active"));
  const btn = document.querySelector(`.mobile-nav-item[data-panel="${name}"]`);
  if (btn) btn.classList.add("active");
  const panel = document.getElementById(panelId);
  if (panel) {
    panel.classList.add("open");
    if (name === "insights") loadMobileInsights();
    if (name === "settings") syncMobileSettings();
  }
}

function loadMobileInsights() {
  const body = document.getElementById("mobileInsightsBody");
  if (!body) return;
  body.innerHTML = '<p class="muted">加载中...</p>';
  const token = localStorage.getItem("momentum_token");
  Promise.all([
    fetch("/api/advice", { headers: { Authorization: `Bearer ${token}` } }).then(r => r.json()),
    fetch("/api/review", { headers: { Authorization: `Bearer ${token}` } }).then(r => r.json()),
  ]).then(([advice, review]) => {
    body.innerHTML =
      `<div class="insight"><span class="insight-label">建议</span><p>${advice.advice || "暂无建议"}</p></div>`
      + (review.review ? `<div class="insight" style="margin-top:12px;"><span class="insight-label">复盘</span><p style="white-space:pre-line;">${review.review}</p></div>` : "");
  }).catch(() => { body.innerHTML = '<p class="muted">加载失败</p>'; });
}

function syncMobileSettings() {
  // Sync desktop config values to mobile config form
  const pairs = [
    ["mobileConfigApiKey", "configApiKey"],
    ["mobileConfigApiBase", "configApiBase"],
    ["mobileConfigModel", "configModel"],
    ["mobileConfigCapacity", "configCapacity"],
    ["mobileConfigWorkStart", "configWorkStart"],
    ["mobileConfigWorkEnd", "configWorkEnd"],
    ["mobileConfigHeartbeatStart", "configHeartbeatStart"],
    ["mobileConfigHeartbeatEnd", "configHeartbeatEnd"],
    ["mobileConfigHeartbeatInterval", "configHeartbeatInterval"],
  ];
  pairs.forEach(([mob, desk]) => {
    const src = document.getElementById(desk);
    const dst = document.getElementById(mob);
    if (src && dst) dst.value = src.value;
  });
  const cb1 = document.getElementById("configVisionEnabled");
  const cb2 = document.getElementById("mobileConfigVisionEnabled");
  if (cb1 && cb2) cb2.checked = cb1.checked;
  const cb3 = document.getElementById("configHeartbeatEnabled");
  const cb4 = document.getElementById("mobileConfigHeartbeatEnabled");
  if (cb3 && cb4) cb4.checked = cb3.checked;
}

function syncMobileChat() {
  const mobileLog = document.getElementById("mobileChatLog");
  const desktopLog = els.chatLog;
  if (mobileLog && desktopLog) {
    mobileLog.innerHTML = desktopLog.innerHTML;
    mobileLog.scrollTop = mobileLog.scrollHeight;
  }
}

function initMobileNav() {
  // Nav item clicks
  document.querySelectorAll(".mobile-nav-item").forEach(btn => {
    btn.addEventListener("click", () => {
      const panel = btn.dataset.panel;
      if (panel === "tasks") { closeAllPanels(); return; }
      openPanel(panel);
    }, { passive: true });
  });

  // Close buttons — one handler per panel
  const closeBtnMap = {
    mobileChatPanel:      "mobileChatPanelClose",
    mobileInsightsPanel:  "mobileInsightsPanelClose",
    mobileSettingsPanel:  "mobileSettingsPanelClose",
  };
  Object.entries(closeBtnMap).forEach(([panelId, btnId]) => {
    const btn = document.getElementById(btnId);
    if (btn) {
      btn.addEventListener("click", closeAllPanels, { passive: true });
    }
  });

  // Mobile chat form
  const mobileChatForm = document.getElementById("mobileChatForm");
  const mobileChatInput = document.getElementById("mobileChatInput");
  if (mobileChatForm && mobileChatInput) {
    mobileChatForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const msg = mobileChatInput.value.trim();
      if (!msg) return;
      mobileChatInput.value = "";

      // Append user message to both logs
      const desktopMsg = document.createElement("div");
      desktopMsg.className = "message user";
      desktopMsg.textContent = msg;
      els.chatLog.appendChild(desktopMsg);
      els.chatLog.scrollTop = els.chatLog.scrollHeight;
      syncMobileChat();

      // Agent response placeholder
      const agentMsg = document.createElement("div");
      agentMsg.className = "message agent";
      agentMsg.textContent = "…";
      els.chatLog.appendChild(agentMsg);
      els.chatLog.scrollTop = els.chatLog.scrollHeight;
      syncMobileChat();

      try {
        const token = localStorage.getItem("momentum_token");
        const resp = await fetch("/api/chat/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
          body: JSON.stringify({ message: msg }),
        });
        if (resp.status === 401) {
          localStorage.removeItem("momentum_token");
          window.location.href = "/login.html";
          return;
        }
        if (!resp.ok || !resp.body) throw new Error("请求失败");
        const reader = resp.body.getReader();
        const dec = new TextDecoder();
        let buf = "", full = "";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          const lines = buf.split("\n");
          buf = lines.pop() || "";
          for (const line of lines) {
            if (line.startsWith("data: ")) {
              try {
                const p = JSON.parse(line.slice(6));
                if (p.chunk) full += p.chunk;
                if (p.error) full = p.error;
              } catch {}
            }
          }
          agentMsg.textContent = full || "…";
          els.chatLog.scrollTop = els.chatLog.scrollHeight;
          syncMobileChat();
        }
      } catch (err) {
        agentMsg.textContent = `连接失败：${err.message}`;
      }
      if (onAfterChat) await onAfterChat();
    });
  }

  // Mobile settings save — sync back to desktop then save
  const mobileSaveBtn = document.getElementById("mobileConfigSaveButton");
  if (mobileSaveBtn) {
    mobileSaveBtn.addEventListener("click", async () => {
      // Copy mobile values back to desktop inputs
      const pairs = [
        ["configApiKey", "mobileConfigApiKey"],
        ["configApiBase", "mobileConfigApiBase"],
        ["configModel", "mobileConfigModel"],
        ["configCapacity", "mobileConfigCapacity"],
        ["configWorkStart", "mobileConfigWorkStart"],
        ["configWorkEnd", "mobileConfigWorkEnd"],
        ["configHeartbeatStart", "mobileConfigHeartbeatStart"],
        ["configHeartbeatEnd", "mobileConfigHeartbeatEnd"],
        ["configHeartbeatInterval", "mobileConfigHeartbeatInterval"],
      ];
      pairs.forEach(([desk, mob]) => {
        const src = document.getElementById(mob);
        const dst = document.getElementById(desk);
        if (src && dst) dst.value = src.value;
      });
      const cb1 = document.getElementById("mobileConfigVisionEnabled");
      const cb2 = document.getElementById("configVisionEnabled");
      if (cb1 && cb2) cb2.checked = cb1.checked;
      const cb3 = document.getElementById("mobileConfigHeartbeatEnabled");
      const cb4 = document.getElementById("configHeartbeatEnabled");
      if (cb3 && cb4) cb4.checked = cb3.checked;

      // Call saveConfig
      const { saveConfig } = await import("./config.js");
      await saveConfig();

      mobileSaveBtn.textContent = "✓ 已保存";
      mobileSaveBtn.disabled = true;
      setTimeout(() => {
        mobileSaveBtn.textContent = "保存设置";
        mobileSaveBtn.disabled = false;
        closeAllPanels();
      }, 1500);
    });
  }
}

// ── Status tabs ──────────────────────────────────────────────
function initStatusTabs() {
  $$(".status-tab").forEach(tab => {
    tab.addEventListener("click", () => {
      $$(".status-tab").forEach(t => { t.classList.remove("active"); t.setAttribute("aria-selected", "false"); });
      tab.classList.add("active");
      tab.setAttribute("aria-selected", "true");
      setTaskStatusFilter(tab.dataset.status);
      loadTasks();
    });
  });
}

// ── Init ──────────────────────────────────────────────────────
function init() {
  // Tasks
  initTasks(
    { tasks: els.tasks, taskCount: els.taskCount },
    {
      editDialog: els.editDialog, editTaskId: els.editTaskId, editTitle: els.editTitle,
      editDue: els.editDue, editPriority: els.editPriority, editEstimate: els.editEstimate,
      editTags: els.editTags, editNotes: els.editNotes,
    },
    els,
  );

  // Chat
  initChat(els.chatLog, els.chatInput);
  onAfterChat = async () => {
    await refreshAll();
    syncMobileChat();
  };
  setAfterChat(onAfterChat);

  // Advice
  initAdvice(els.adviceText);

  // Config
  initConfig({
    configApiKey: els.configApiKey, configApiBase: els.configApiBase,
    configModel: els.configModel, configVisionEnabled: els.configVisionEnabled,
    configCapacity: els.configCapacity, configWorkStart: els.configWorkStart,
    configWorkEnd: els.configWorkEnd,
  }, els.adviceText);
  setOnConfigSaved(loadProvider);

  // Heartbeat
  initHeartbeat(els);
  loadHeartbeatConfig();
  startHeartbeatChecks();

  // Status tabs
  initStatusTabs();

  // ── Event bindings ──
  els.addTaskButton.addEventListener("click", addTask);
  $("#imageUpload")?.addEventListener("change", handleImageUpload);
  els.planTaskButton.addEventListener("click", planTask);
  els.adviseButton.addEventListener("click", async () => {
    const tasks = await loadTasks();
    if (isProviderConfigured) await loadAdviceWithAI(tasks);
    else await loadAdvice();
  });
  els.reviewButton.addEventListener("click", async () => {
    const tasks = await loadTasks();
    if (isProviderConfigured) await loadReviewWithAI(tasks);
    else await loadReview();
  });
  els.refreshButton.addEventListener("click", refreshAll);
  els.chatForm.addEventListener("submit", sendChat);
  els.searchInput.addEventListener("input", onSearchInput);
  els.exportButton.addEventListener("click", exportData);
  els.importFile?.addEventListener("change", e => {
    if (e.target.files[0]) {
      importData(e.target.files[0]);
      e.target.value = "";
    }
  });

  // Enter in task input = add task
  els.taskInput.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") addTask();
  });

  // Dialogs
  els.editDialog.querySelector("form").addEventListener("submit", saveEdit);
  els.editCancelButton.addEventListener("click", () => els.editDialog.close());
  els.addSubtaskDialog.querySelector("form").addEventListener("submit", saveSubtask);
  els.addSubtaskCancelButton.addEventListener("click", () => els.addSubtaskDialog.close());
  els.postponeDialog.querySelector("form").addEventListener("submit", savePostpone);
  els.postponeCancelButton.addEventListener("click", () => els.postponeDialog.close());
  bindPostponeOptions();
  els.configSaveButton.addEventListener("click", saveConfig);

  // ── Mobile nav ──
  initMobileNav();

  // User info
  const user = localStorage.getItem("momentum_user");
  if (user) $("#currentUser").textContent = user;
  $("#logoutButton")?.addEventListener("click", () => {
    import("./api.js").then(({ logout }) => logout());
  });
}

// Initial load — only when authenticated
if (_hasToken) {
  init();
  refreshAll().catch(err => { els.adviceText.textContent = err.message; });
  loadConfig();
}
