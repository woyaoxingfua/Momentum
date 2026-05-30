import { requestJson, escapeHtml, formatDue, priorityText, recurrenceText, toDatetimeLocal } from "./api.js";

const els = {};
let currentStatus = "todo";
let appEls = null;

export function initTasks(elements, dialogElements, appElements) {
  Object.assign(els, elements, dialogElements);
  appEls = appElements;
}

export function setTaskStatusFilter(status) {
  currentStatus = status;
}

export function getTaskStatusFilter() {
  return currentStatus;
}

const STATUS_LABELS = { todo: "待办", doing: "进行中", done: "已完成", dropped: "已放弃" };

export function renderTasks(tasks, isSearchResult = false) {
  if (isSearchResult) {
    els.taskCount.textContent = `${tasks.length} 个搜索结果`;
  } else {
    const label = STATUS_LABELS[currentStatus] || "待办";
    els.taskCount.textContent = `${tasks.length} 个${label}`;
  }
  if (tasks.length === 0) {
    els.tasks.innerHTML = `<div class="empty">${isSearchResult ? "没有找到匹配的任务。" : `没有${STATUS_LABELS[currentStatus] || "待办"}任务。`}</div>`;
    return;
  }

  const ordered = orderTasks(tasks);
  els.tasks.innerHTML = ordered
    .map((task) => renderTaskCard(task))
    .join("");

  bindTaskButtons();
}

function renderTaskCard(task) {
  const statusBadge = task.status !== "todo"
    ? `<span class="badge status-badge status-${task.status}">${STATUS_LABELS[task.status] || task.status}</span>`
    : "";

  const tagsHtml = task.tags && task.tags.length > 0
    ? task.tags.map(tag => `<span class="badge tag">${escapeHtml(tag)}</span>`).join("")
    : "";

  return `
    <article class="task ${task.parent_task_id ? "subtask" : "parent-task"} ${task.status !== "todo" ? `task-${task.status}` : ""}">
      <div>
        <div class="task-title">${escapeHtml(task.title)}</div>
        <div class="task-meta">
          ${statusBadge}
          <span class="badge ${task.priority}">${priorityText(task.priority)}</span>
          <span>${formatDue(task.due_at)}</span>
          <span>${task.estimated_minutes ? `${task.estimated_minutes} 分钟` : "未估时"}</span>
          ${task.parent_task_id ? `<span class="badge">子任务</span>` : ""}
          ${task.recurrence ? `<span class="badge recurrence">${recurrenceText(task.recurrence)}</span>` : ""}
          ${tagsHtml}
        </div>
      </div>
      <div class="task-actions">
        ${actionButtons(task)}
      </div>
    </article>`;
}

function actionButtons(task) {
  const buttons = [];
  buttons.push(`<button data-edit="${task.id}" title="编辑">编辑</button>`);

  // 只有主任务才能添加子任务
  if (!task.parent_task_id) {
    buttons.push(`<button data-add-subtask="${task.id}" title="添加子任务">➕ 添加子任务</button>`);
  }

  if (task.status === "todo") {
    buttons.push(`<button data-start="${task.id}" title="开始做">开始</button>`);
    buttons.push(`<button data-done="${task.id}" title="完成">完成</button>`);
    buttons.push(`<button data-postpone="${task.id}" title="推迟">推迟</button>`);
    buttons.push(`<button data-drop="${task.id}" title="放弃">放弃</button>`);
  } else if (task.status === "doing") {
    buttons.push(`<button data-done="${task.id}" title="完成">完成</button>`);
    buttons.push(`<button data-drop="${task.id}" title="放弃">放弃</button>`);
  } else {
    buttons.push(`<button data-reopen="${task.id}" title="重新打开">重开</button>`);
  }

  return buttons.join("");
}

function bindTaskButtons() {
  document.querySelectorAll("[data-start]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await requestJson(`/api/tasks/${btn.dataset.start}/start`, { method: "POST" });
      await loadTasks();
    });
  });

  document.querySelectorAll("[data-done]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await requestJson(`/api/tasks/${btn.dataset.done}/done`, { method: "POST" });
      await loadTasks();
    });
  });

  document.querySelectorAll("[data-edit]").forEach((btn) => {
    btn.addEventListener("click", () => openEditDialog(btn.dataset.edit));
  });

  document.querySelectorAll("[data-add-subtask]").forEach((btn) => {
    btn.addEventListener("click", () => openAddSubtaskDialog(btn.dataset.addSubtask));
  });

  document.querySelectorAll("[data-postpone]").forEach((btn) => {
    btn.addEventListener("click", () => openPostponeDialog(btn.dataset.postpone));
  });

  document.querySelectorAll("[data-drop]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("确定要放弃这个任务吗？")) return;
      await requestJson(`/api/tasks/${btn.dataset.drop}/drop`, { method: "POST" });
      await loadTasks();
    });
  });

  document.querySelectorAll("[data-reopen]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await requestJson(`/api/tasks/${btn.dataset.reopen}/reopen`, { method: "POST" });
      await loadTasks();
    });
  });
}

function openAddSubtaskDialog(parentTaskId) {
  appEls.addSubtaskParentId.value = parentTaskId;
  appEls.addSubtaskTitle.value = "";
  appEls.addSubtaskDue.value = "";
  appEls.addSubtaskPriority.value = "medium";
  appEls.addSubtaskEstimate.value = "";
  appEls.addSubtaskDialog.showModal();
}

export async function saveSubtask(event) {
  event.preventDefault();
  
  const parentTaskId = appEls.addSubtaskParentId.value;
  const title = appEls.addSubtaskTitle.value.trim();
  
  if (!title) {
    alert("请输入子任务标题");
    return;
  }
  
  const body = {
    title: title,
    due_at: appEls.addSubtaskDue.value || null,
    priority: appEls.addSubtaskPriority.value,
    estimated_minutes: appEls.addSubtaskEstimate.value ? Number(appEls.addSubtaskEstimate.value) : null,
  };
  
  await requestJson(`/api/tasks/${parentTaskId}/subtasks`, { method: "POST", body: JSON.stringify(body) });
  appEls.addSubtaskDialog.close();
  await loadTasks();
}

async function openEditDialog(taskId) {
  const payload = await requestJson(`/api/tasks?status=${currentStatus}`);
  const task = payload.tasks.find((t) => t.id === Number(taskId));
  if (!task) return;

  els.editTaskId.value = task.id;
  els.editTitle.value = task.title;
  els.editDue.value = toDatetimeLocal(task.due_at);
  els.editPriority.value = task.priority;
  els.editEstimate.value = task.estimated_minutes || "";
  els.editTags.value = task.tags ? task.tags.join(", ") : "";
  els.editNotes.value = task.notes || "";
  els.editDialog.showModal();
}

export async function saveEdit(event) {
  event.preventDefault();

  const tagsStr = els.editTags.value.trim();
  let tags = null;
  if (tagsStr) {
    tags = tagsStr.split(",").map(t => t.trim()).filter(t => t);
  }

  const body = {
    title: els.editTitle.value.trim(),
    due_at: els.editDue.value || null,
    priority: els.editPriority.value,
    estimated_minutes: els.editEstimate.value ? Number(els.editEstimate.value) : null,
    tags,
    notes: els.editNotes.value.trim() || null,
  };
  await requestJson(`/api/tasks/${els.editTaskId.value}`, { method: "PUT", body: JSON.stringify(body) });
  els.editDialog.close();
  await loadTasks();
}

export async function loadTasks() {
  const payload = await requestJson(`/api/tasks?status=${currentStatus}`);
  renderTasks(payload.tasks);
  return payload.tasks;
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
  tasks.filter((t) => !t.parent_task_id).forEach((task) => {
    ordered.push(task);
    ordered.push(...(children.get(task.id) || []));
  });

  tasks
    .filter((t) => t.parent_task_id && !tasks.some((p) => p.id === t.parent_task_id))
    .forEach((t) => ordered.push(t));

  return ordered;
}

function openPostponeDialog(taskId) {
  appEls.postponeTaskId.value = taskId;
  appEls.postponeDays.value = 3;
  appEls.postponeDialog.showModal();
}

export async function savePostpone(event) {
  event.preventDefault();
  
  const taskId = appEls.postponeTaskId.value;
  const days = Number(appEls.postponeDays.value);
  
  if (!days || days < 1) {
    alert("请输入有效的推迟天数");
    return;
  }
  
  await requestJson(`/api/tasks/${taskId}/postpone`, {
    method: "POST", body: JSON.stringify({ days: days }),
  });
  appEls.postponeDialog.close();
  await loadTasks();
}

export function bindPostponeOptions() {
  document.querySelectorAll(".postpone-option").forEach((btn) => {
    btn.addEventListener("click", () => {
      appEls.postponeDays.value = btn.dataset.days;
    });
  });
}
