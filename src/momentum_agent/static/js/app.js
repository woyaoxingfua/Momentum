import { requestJson } from "./api.js";
import { initTasks, saveEdit, saveSubtask, savePostpone, bindPostponeOptions, loadTasks, setTaskStatusFilter, renderTasks, setSortMode, getSortMode } from "./tasks.js";
import { initChat, setAfterChat, sendChat, sendToAgent } from "./chat.js";
import { initAdvice, loadAdvice, loadReview, loadAdviceWithAI, loadReviewWithAI } from "./advice.js";
import { initConfig, loadConfig, saveConfig, setOnConfigSaved } from "./config.js";
import { initHeartbeat, loadHeartbeatConfig, startHeartbeatChecks } from "./heartbeat.js";
import { initNotifications } from "./notifications.js";

// ── Theme ─────────────────────────────────────────────────────
function initTheme() {
  const saved = localStorage.getItem("momentum_theme");
  const theme = saved || (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  applyTheme(theme);
}
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("momentum_theme", theme);
  const moon = document.getElementById("themeIconMoon");
  const sun = document.getElementById("themeIconSun");
  if (moon && sun) {
    moon.style.display = theme === "dark" ? "none" : "block";
    sun.style.display = theme === "dark" ? "block" : "none";
  }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "light" ? "#f5f3ef" : "#121110");
}
function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme") || "dark";
  applyTheme(current === "dark" ? "light" : "dark");
}
initTheme();

// ── Background image ──────────────────────────────────────────
function initBackground() {
  const url = localStorage.getItem("momentum_bg_url");
  const opacity = localStorage.getItem("momentum_bg_opacity") || "15";
  applyBackground(url, opacity);
}
function applyBackground(url, opacity) {
  if (url) {
    document.documentElement.style.setProperty("--bg-image", `url("${url}")`);
    document.documentElement.style.setProperty("--bg-opacity", String(parseInt(opacity) / 100));
  } else {
    document.documentElement.style.setProperty("--bg-image", "none");
    document.documentElement.style.setProperty("--bg-opacity", "0");
  }
}
initBackground();

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
  sortButton:     $("#sortButton"),
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
  configProvider:          $("#configProvider"),
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
    ["mobileConfigProvider", "configProvider"],
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
  updateMobileProviderUI();
}

function updateMobileProviderUI() {
  const provider = document.getElementById("mobileConfigProvider");
  if (!provider) return;
  const ollama = provider.value === "ollama";
  const apiKey = document.getElementById("mobileConfigApiKey");
  const base = document.getElementById("mobileConfigApiBase");
  const model = document.getElementById("mobileConfigModel");
  const refreshRow = document.getElementById("mobileConfigModelRefreshRow");

  if (apiKey) apiKey.placeholder = ollama ? "ollama（本地无需 key）" : "sk-...";
  if (base) base.placeholder = ollama ? "http://localhost:11434" : "https://api.openai.com/v1";
  if (model) model.placeholder = ollama ? "如：llama3.2、qwen2.5" : "如：gpt-4o、deepseek-chat";
  if (refreshRow) refreshRow.style.display = ollama ? "flex" : "none";
}

function bindMobileProviderUI() {
  const provider = document.getElementById("mobileConfigProvider");
  if (!provider) return;
  provider.addEventListener("change", () => {
    updateMobileProviderUI();
    const modelSelect = document.getElementById("mobileConfigModelSelect");
    if (modelSelect) modelSelect.style.display = "none";
  });
  const refreshBtn = document.getElementById("mobileConfigRefreshModels");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", async () => {
      const { refreshOllamaModels } = await import("./config.js");
      await refreshOllamaModels("mobileConfig");
    });
  }
  const modelSelect = document.getElementById("mobileConfigModelSelect");
  if (modelSelect) {
    modelSelect.addEventListener("change", () => {
      const model = document.getElementById("mobileConfigModel");
      if (model) model.value = modelSelect.value;
    });
  }
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
        ["configProvider", "mobileConfigProvider"],
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

  bindMobileProviderUI();
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

// ── Onboarding ────────────────────────────────────────────────
const ONBOARDING_STEPS = [
  {
    icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>',
    title: '欢迎使用 Momentum',
    desc: '你的 AI 任务管理助手。在这里，你可以轻松管理待办、让 AI 帮你拆分计划、追踪专注时间。',
  },
  {
    icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>',
    title: '智能任务管理',
    desc: '在输入框写下你想做的事，按 Ctrl+Enter 快速添加。支持图片识别、子任务拆分、任务依赖和智能排序。',
  },
  {
    icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>',
    title: 'AI Agent 助手',
    desc: '点击"拆成计划"让 AI 自动拆分任务，点击"今日建议"获取下一步建议，点击"复盘"总结一天进展。移动端点击底部 Agent 标签对话。',
  },
  {
    icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    title: '开始使用',
    desc: '在设置中配置你的 AI 提供商（OpenAI / DeepSeek / Ollama）。添加到主屏幕即可像原生 App 一样使用。准备好开始了吗？',
  },
];

let _onboardingStep = 0;

function showOnboarding() {
  const overlay = document.getElementById("onboardingOverlay");
  if (!overlay) return;
  overlay.style.display = "flex";
  _onboardingStep = 0;
  renderOnboardingStep();
}

function renderOnboardingStep() {
  const content = document.getElementById("onboardingContent");
  if (!content) return;
  const step = ONBOARDING_STEPS[_onboardingStep];
  content.innerHTML = `${step.icon}<h3>${step.title}</h3><p>${step.desc}</p>`;
  document.querySelectorAll(".onboarding-dot").forEach((d, i) => {
    d.classList.toggle("active", i === _onboardingStep);
  });
  const nextBtn = document.getElementById("onboardingNext");
  if (nextBtn) nextBtn.textContent = _onboardingStep === ONBOARDING_STEPS.length - 1 ? "完成" : "下一步";
}

function nextOnboarding() {
  if (_onboardingStep < ONBOARDING_STEPS.length - 1) {
    _onboardingStep++;
    renderOnboardingStep();
  } else {
    closeOnboarding();
  }
}

function closeOnboarding() {
  const overlay = document.getElementById("onboardingOverlay");
  if (overlay) overlay.style.display = "none";
  localStorage.setItem("momentum_onboarded", "1");
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
    configProvider: els.configProvider,
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

  // Sort toggle
  if (els.sortButton) {
    els.sortButton.addEventListener("click", () => {
      const current = getSortMode();
      const next = current === "default" ? "score" : "default";
      setSortMode(next);
      els.sortButton.textContent = next === "score" ? "默认排序" : "智能排序";
      els.sortButton.title = next === "score" ? "恢复默认时间排序" : "按 AI 推荐优先级排序";
      loadTasks();
    });
  }

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

  // Browser notifications
  initNotifications();

  // Focus timer
  import("./focus.js").then(({ focusInit }) => focusInit());

  // User info
  const user = localStorage.getItem("momentum_user");
  if (user) $("#currentUser").textContent = user;
  $("#logoutButton")?.addEventListener("click", () => {
    import("./api.js").then(({ logout }) => logout());
  });
  const themeToggle = document.getElementById("themeToggle");
  if (themeToggle) themeToggle.addEventListener("click", toggleTheme);
  const bgApply = document.getElementById("mobileConfigBgApply");
  const bgClear = document.getElementById("mobileConfigBgClear");
  const bgUrl = document.getElementById("mobileConfigBgUrl");
  const bgOpacity = document.getElementById("mobileConfigBgOpacity");
  if (bgApply) bgApply.addEventListener("click", () => {
    const url = bgUrl ? bgUrl.value.trim() : "";
    const opacity = bgOpacity ? bgOpacity.value : "15";
    localStorage.setItem("momentum_bg_url", url);
    localStorage.setItem("momentum_bg_opacity", opacity);
    applyBackground(url, opacity);
  });
  if (bgClear) bgClear.addEventListener("click", () => {
    localStorage.removeItem("momentum_bg_url");
    localStorage.removeItem("momentum_bg_opacity");
    applyBackground("", "0");
    if (bgUrl) bgUrl.value = "";
  });

  // Onboarding buttons
  const onboardingNext = document.getElementById("onboardingNext");
  const onboardingSkip = document.getElementById("onboardingSkip");
  if (onboardingNext) onboardingNext.addEventListener("click", nextOnboarding);
  if (onboardingSkip) onboardingSkip.addEventListener("click", closeOnboarding);
}

// Initial load — only when authenticated
if (_hasToken) {
  init();
  refreshAll().catch(err => { els.adviceText.textContent = err.message; });
  loadConfig();
  // 首次使用显示引导
  if (!localStorage.getItem("momentum_onboarded")) {
    showOnboarding();
  }
}
