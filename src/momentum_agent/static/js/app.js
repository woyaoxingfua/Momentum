import { requestJson, logout } from "./api.js";

if (!localStorage.getItem("momentum_token")) {
  window.location.href = "/login.html";
}
import { initTasks, saveEdit, loadTasks, setTaskStatusFilter } from "./tasks.js";
import { initChat, setAfterChat, sendChat } from "./chat.js";
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
  configCapacity: document.querySelector("#configCapacity"),
  configWorkStart: document.querySelector("#configWorkStart"),
  configWorkEnd: document.querySelector("#configWorkEnd"),
  configHeartbeatEnabled: document.querySelector("#configHeartbeatEnabled"),
  configHeartbeatStart: document.querySelector("#configHeartbeatStart"),
  configHeartbeatEnd: document.querySelector("#configHeartbeatEnd"),
  configHeartbeatInterval: document.querySelector("#configHeartbeatInterval"),
  configSaveButton: document.querySelector("#configSaveButton"),
  weatherTemp: document.querySelector("#weatherTemp"),
  weatherDesc: document.querySelector("#weatherDesc"),
  refreshWeather: document.querySelector("#refreshWeather"),
  weatherCity: document.querySelector("#weatherCity"),
  themeToggle: document.querySelector("#themeToggle"),
};

async function loadProvider() {
  const payload = await requestJson("/api/provider");
  els.providerStatus.textContent = payload.provider;
}

async function refreshAll() {
  await Promise.all([loadTasks(), loadAdvice(), loadProvider()]);
}

let searchTimer = null;

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
    showToast("✅ 导入成功！");
  } catch (err) {
    showToast(`❌ 导入失败：${err.message}`);
  }
}

async function addTask() {
  const text = els.taskInput.value.trim();
  if (!text) return;
  els.addTaskButton.disabled = true;
  els.addTaskButton.textContent = "⏳ 添加中...";
  try {
    await requestJson("/api/tasks", { method: "POST", body: JSON.stringify({ text }) });
    els.taskInput.value = "";
    await refreshAll();
    showToast("✅ 任务添加成功！");
  } catch (err) {
    showToast(`❌ 添加失败：${err.message}`);
  } finally {
    els.addTaskButton.disabled = false;
    els.addTaskButton.textContent = "✨ 新增任务";
  }
}

async function planTask() {
  const text = els.taskInput.value.trim();
  if (!text) return;
  els.planTaskButton.disabled = true;
  els.planTaskButton.textContent = "⏳ 拆分中...";
  try {
    await requestJson("/api/plan", { method: "POST", body: JSON.stringify({ text }) });
    els.taskInput.value = "";
    await refreshAll();
    showToast("✅ 计划创建成功！");
  } catch (err) {
    showToast(`❌ 计划失败：${err.message}`);
  } finally {
    els.planTaskButton.disabled = false;
    els.planTaskButton.textContent = "📋 拆成计划";
  }
}

async function loadWeather() {
  try {
    const userCity = localStorage.getItem("momentum_city") || "北京";
    const response = await fetch(`/api/weather?city=${encodeURIComponent(userCity)}`);
    if (!response.ok) {
      throw new Error("天气加载失败");
    }
    const weather = await response.json();
    els.weatherTemp.textContent = `${weather.emoji} ${weather.temperature}°C`;
    els.weatherDesc.textContent = `${weather.condition_cn} | 湿度 ${weather.humidity}% | ${weather.advice}`;
    els.weatherCity.value = userCity;
  } catch (err) {
    els.weatherTemp.textContent = "❓ --°C";
    els.weatherDesc.textContent = "天气加载失败，请检查网络";
  }
}

function saveWeatherCity() {
  const city = els.weatherCity.value.trim();
  if (city) {
    localStorage.setItem("momentum_city", city);
    loadWeather();
    showToast(`📍 已切换到 ${city} 的天气`);
  }
}

function toggleTheme() {
  const currentTheme = document.documentElement.getAttribute("data-theme");
  const newTheme = currentTheme === "dark" ? "light" : "dark";
  
  document.documentElement.setAttribute("data-theme", newTheme);
  localStorage.setItem("momentum_theme", newTheme);
  
  els.themeToggle.textContent = newTheme === "dark" ? "☀️" : "🌙";
  showToast(newTheme === "dark" ? "🌙 已切换到深色模式" : "☀️ 已切换到浅色模式");
}

function loadTheme() {
  const savedTheme = localStorage.getItem("momentum_theme") || "light";
  document.documentElement.setAttribute("data-theme", savedTheme);
  els.themeToggle.textContent = savedTheme === "dark" ? "☀️" : "🌙";
}

function showConfetti() {
  const colors = ["#ff6b6b", "#4ecdc4", "#45b7d1", "#96ceb4", "#ffeaa7", "#dfe6e9", "#a29bfe"];
  const emojis = ["🎉", "✨", "🌟", "💫", "⭐", "🎊", "👏"];
  
  for (let i = 0; i < 20; i++) {
    const confetti = document.createElement("div");
    confetti.className = "confetti";
    confetti.textContent = emojis[Math.floor(Math.random() * emojis.length)];
    confetti.style.left = `${Math.random() * 100}vw`;
    confetti.style.fontSize = `${Math.random() * 20 + 20}px`;
    confetti.style.animationDelay = `${Math.random() * 0.5}s`;
    document.body.appendChild(confetti);
    
    setTimeout(() => confetti.remove(), 3000);
  }
}

function showToast(message) {
  const existing = document.querySelector(".toast");
  if (existing) existing.remove();
  
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.textContent = message;
  document.body.appendChild(toast);
  
  setTimeout(() => {
    toast.style.opacity = "0";
    setTimeout(() => toast.remove(), 300);
  }, 3000);
}

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
  { configCapacity: els.configCapacity, configWorkStart: els.configWorkStart, configWorkEnd: els.configWorkEnd },
  els.adviceText,
);

els.addTaskButton.addEventListener("click", addTask);
els.planTaskButton.addEventListener("click", planTask);
els.adviseButton.addEventListener("click", loadAdvice);
els.reviewButton.addEventListener("click", loadReview);
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
els.refreshWeather.addEventListener("click", loadWeather);
els.weatherCity.addEventListener("change", saveWeatherCity);
els.themeToggle.addEventListener("click", toggleTheme);

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
loadTheme();
loadWeather();

window.showConfetti = showConfetti;

setInterval(loadWeather, 300000);

initHeartbeat(els);
loadHeartbeatConfig();
startHeartbeatChecks();
