const els = {
  taskInput: document.querySelector("#taskInput"),
  addTaskButton: document.querySelector("#addTaskButton"),
  planTaskButton: document.querySelector("#planTaskButton"),
  adviseButton: document.querySelector("#adviseButton"),
  reviewButton: document.querySelector("#reviewButton"),
  refreshButton: document.querySelector("#refreshButton"),
  tasks: document.querySelector("#tasks"),
  taskCount: document.querySelector("#taskCount"),
  adviceText: document.querySelector("#adviceText"),
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
  editNotes: document.querySelector("#editNotes"),
  editCancelButton: document.querySelector("#editCancelButton"),
  configPanel: document.querySelector("#configPanel"),
  configCapacity: document.querySelector("#configCapacity"),
  configWorkStart: document.querySelector("#configWorkStart"),
  configWorkEnd: document.querySelector("#configWorkEnd"),
  configSaveButton: document.querySelector("#configSaveButton"),
};

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "请求失败");
  }
  return payload;
}

function formatDue(value) {
  if (!value) return "无截止";
  const date = new Date(value);
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function toDatetimeLocal(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toISOString().slice(0, 16);
}

function renderTasks(tasks) {
  els.taskCount.textContent = `${tasks.length} 个待办`;
  if (tasks.length === 0) {
    els.tasks.innerHTML = `<div class="empty">暂时没有待办。写下一件想推进的事。</div>`;
    return;
  }

  const ordered = orderTasks(tasks);
  els.tasks.innerHTML = ordered
    .map(
      (task) => `
        <article class="task ${task.parent_task_id ? "subtask" : ""}">
          <div>
            <div class="task-title">${escapeHtml(task.title)}</div>
            <div class="task-meta">
              <span class="badge ${task.priority}">${priorityText(task.priority)}</span>
              <span>${formatDue(task.due_at)}</span>
              <span>${task.estimated_minutes ? `${task.estimated_minutes} 分钟` : "未估时"}</span>
              ${task.parent_task_id ? `<span>子任务</span>` : ""}
              ${task.recurrence ? `<span class="badge recurrence">${recurrenceText(task.recurrence)}</span>` : ""}
            </div>
          </div>
          <div class="task-actions">
            <button data-done="${task.id}" title="完成">完成</button>
            <button data-edit="${task.id}" title="编辑">编辑</button>
            <button data-postpone="${task.id}" title="推迟3天">推迟</button>
            <button data-drop="${task.id}" title="放弃">放弃</button>
          </div>
        </article>
      `,
    )
    .join("");

  bindTaskButtons();
}

function bindTaskButtons() {
  document.querySelectorAll("[data-done]").forEach((button) => {
    button.addEventListener("click", async () => {
      await requestJson(`/api/tasks/${button.dataset.done}/done`, { method: "POST" });
      await refreshAll();
    });
  });

  document.querySelectorAll("[data-edit]").forEach((button) => {
    button.addEventListener("click", () => openEditDialog(button.dataset.edit));
  });

  document.querySelectorAll("[data-postpone]").forEach((button) => {
    button.addEventListener("click", async () => {
      await requestJson(`/api/tasks/${button.dataset.postpone}/postpone`, {
        method: "POST",
        body: JSON.stringify({ days: 3 }),
      });
      await refreshAll();
    });
  });

  document.querySelectorAll("[data-drop]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!confirm("确定要放弃这个任务吗？")) return;
      await requestJson(`/api/tasks/${button.dataset.drop}/drop`, { method: "POST" });
      await refreshAll();
    });
  });
}

async function openEditDialog(taskId) {
  const payload = await requestJson(`/api/tasks?status=todo`);
  const task = payload.tasks.find((t) => t.id === Number(taskId));
  if (!task) return;

  els.editTaskId.value = task.id;
  els.editTitle.value = task.title;
  els.editDue.value = toDatetimeLocal(task.due_at);
  els.editPriority.value = task.priority;
  els.editEstimate.value = task.estimated_minutes || "";
  els.editNotes.value = task.notes || "";
  els.editDialog.showModal();
}

async function saveEdit(event) {
  event.preventDefault();
  const taskId = els.editTaskId.value;
  const body = {
    title: els.editTitle.value.trim(),
    due_at: els.editDue.value || null,
    priority: els.editPriority.value,
    estimated_minutes: els.editEstimate.value ? Number(els.editEstimate.value) : null,
    notes: els.editNotes.value.trim() || null,
  };
  await requestJson(`/api/tasks/${taskId}`, { method: "PUT", body: JSON.stringify(body) });
  els.editDialog.close();
  await refreshAll();
}

function orderTasks(tasks) {
  const children = new Map();
  tasks.forEach((task) => {
    if (!task.parent_task_id) return;
    const group = children.get(task.parent_task_id) || [];
    group.push(task);
    children.set(task.parent_task_id, group);
  });

  const ordered = [];
  tasks
    .filter((task) => !task.parent_task_id)
    .forEach((task) => {
      ordered.push(task);
      ordered.push(...(children.get(task.id) || []));
    });

  tasks
    .filter((task) => task.parent_task_id && !tasks.some((parent) => parent.id === task.parent_task_id))
    .forEach((task) => ordered.push(task));

  return ordered;
}

function priorityText(priority) {
  return { high: "高", medium: "中", low: "低" }[priority] || "中";
}

function recurrenceText(value) {
  return { daily: "每天", weekly: "每周", monthly: "每月" }[value] || "";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function loadTasks() {
  const payload = await requestJson("/api/tasks?status=todo");
  renderTasks(payload.tasks);
}

async function loadAdvice() {
  const payload = await requestJson("/api/advice");
  els.adviceText.textContent = payload.advice;
}

async function loadProvider() {
  const payload = await requestJson("/api/provider");
  els.providerStatus.textContent = payload.provider;
}

async function loadReview() {
  const payload = await requestJson("/api/review");
  els.adviceText.textContent = payload.review;
}

async function loadConfig() {
  const payload = await requestJson("/api/config");
  const text = payload.config;
  if (text && text !== "没有配置项。用 momentum-agent config set <key> <value> 来设置偏好。") {
    // Parse "当前配置：\n  key = value" format back
    const lines = text.split("\n").filter((l) => l.includes("="));
    lines.forEach((line) => {
      const [key, ...rest] = line.replace("  ", "").split("=");
      const value = rest.join("=").trim();
      if (key.trim() === "daily_capacity_minutes") els.configCapacity.value = value;
      if (key.trim() === "working_hours_start") els.configWorkStart.value = value;
      if (key.trim() === "working_hours_end") els.configWorkEnd.value = value;
    });
  }
}

async function saveConfig() {
  const entries = [
    ["daily_capacity_minutes", els.configCapacity.value || "45"],
    ["working_hours_start", els.configWorkStart.value || "09:00"],
    ["working_hours_end", els.configWorkEnd.value || "18:00"],
  ];
  for (const [key, value] of entries) {
    await requestJson("/api/config", {
      method: "POST",
      body: JSON.stringify({ key, value }),
    });
  }
  await loadAdvice();
}

async function refreshAll() {
  await Promise.all([loadTasks(), loadAdvice(), loadProvider()]);
}

async function addTask() {
  const text = els.taskInput.value.trim();
  if (!text) return;
  els.addTaskButton.disabled = true;
  try {
    await requestJson("/api/tasks", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    els.taskInput.value = "";
    await refreshAll();
  } finally {
    els.addTaskButton.disabled = false;
  }
}

async function planTask() {
  const text = els.taskInput.value.trim();
  if (!text) return;
  els.planTaskButton.disabled = true;
  try {
    await requestJson("/api/plan", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    els.taskInput.value = "";
    await refreshAll();
  } finally {
    els.planTaskButton.disabled = false;
  }
}

function addMessage(role, text) {
  const item = document.createElement("div");
  item.className = `message ${role}`;
  item.textContent = text;
  els.chatLog.appendChild(item);
  els.chatLog.scrollTop = els.chatLog.scrollHeight;
}

async function sendChat(event) {
  event.preventDefault();
  const message = els.chatInput.value.trim();
  if (!message) return;
  els.chatInput.value = "";
  addMessage("user", message);
  const payload = await requestJson("/api/chat", {
    method: "POST",
    body: JSON.stringify({ message }),
  });
  addMessage("agent", payload.message);
  await refreshAll();
}

els.addTaskButton.addEventListener("click", addTask);
els.planTaskButton.addEventListener("click", planTask);
els.adviseButton.addEventListener("click", loadAdvice);
els.reviewButton.addEventListener("click", loadReview);
els.refreshButton.addEventListener("click", refreshAll);
els.chatForm.addEventListener("submit", sendChat);
els.taskInput.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    addTask();
  }
});
els.editDialog.querySelector("form").addEventListener("submit", saveEdit);
els.editCancelButton.addEventListener("click", () => els.editDialog.close());
els.configSaveButton.addEventListener("click", saveConfig);

refreshAll().catch((error) => {
  els.adviceText.textContent = error.message;
});
loadConfig();
