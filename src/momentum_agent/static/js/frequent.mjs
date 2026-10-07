// 历史常完成任务 — 只渲染服务端聚合结果；分组口径完全由服务端决定（不做前端标题匹配）。

export const FREQUENT_COMPLETED_WINDOW_DAYS = 180;

function nonNegativeInteger(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? Math.floor(number) : 0;
}

export function formatLastCompletedAt(value, now = new Date()) {
  const text = String(value == null ? "" : value).trim();
  if (!text) return "";
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) return "";
  if (
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate()
  ) {
    return "今天";
  }
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return month + "-" + day;
}

export function formatFrequentSummary(item) {
  const count = nonNegativeInteger(item && item.completion_count);
  const when = formatLastCompletedAt(item && item.last_completed_at);
  return when ? "完成 " + count + " 次 · 最近 " + when : "完成 " + count + " 次";
}

export function normalizeFrequentItems(payload) {
  const rows = payload && Array.isArray(payload.tasks) ? payload.tasks : [];
  return rows
    .map((item) => ({
      key: String((item && item.key) || ""),
      title: String((item && item.title) || ""),
      completion_count: nonNegativeInteger(item && item.completion_count),
      last_completed_at: String((item && item.last_completed_at) || ""),
    }))
    .filter((item) => item.key !== "" && item.title !== "");
}

export async function loadFrequentCompleted(requestJson, options = {}) {
  const days = (options && options.days) || FREQUENT_COMPLETED_WINDOW_DAYS;
  const payload = await requestJson("/api/tasks/frequent?days=" + encodeURIComponent(String(days)));
  return normalizeFrequentItems(payload);
}

export async function createTaskAgain(requestJson, options = {}) {
  const key = String((options && options.key) || "");
  return requestJson("/api/frequent/recreate", {
    method: "POST",
    body: JSON.stringify({ key }),
  });
}

export function createFrequentCompletedEntry({ requestJson, onCreated, documentRef = globalThis.document } = {}) {
  if (!documentRef || typeof documentRef.createElement !== "function") return null;

  const element = documentRef.createElement("section");
  element.className = "insight frequent-completed-entry";
  element.setAttribute("role", "region");
  element.setAttribute("aria-label", "历史常完成任务");

  const label = documentRef.createElement("span");
  label.className = "insight-label";
  label.textContent = "历史常完成";

  const status = documentRef.createElement("p");
  status.className = "frequent-completed-status";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  status.hidden = true;

  const list = documentRef.createElement("div");
  list.className = "frequent-completed-list";
  list.hidden = true;

  const toggle = documentRef.createElement("button");
  toggle.type = "button";
  toggle.className = "primary";
  toggle.textContent = "查看常完成任务";

  let loaded = false;
  let busy = false;

  function setBusy(value) {
    busy = Boolean(value);
    toggle.disabled = busy;
    for (const row of list.children) {
      const again = row && row.__againButton;
      if (again) again.disabled = busy;
    }
  }

  function updateMessage(text) {
    status.textContent = text == null ? "" : String(text);
    status.hidden = status.textContent === "";
  }

  function renderItem(item) {
    const row = documentRef.createElement("div");
    row.className = "frequent-completed-item";

    const title = documentRef.createElement("span");
    title.className = "frequent-completed-title";
    title.textContent = item.title;

    const meta = documentRef.createElement("span");
    meta.className = "frequent-completed-meta";
    meta.textContent = formatFrequentSummary(item);

    const again = documentRef.createElement("button");
    again.type = "button";
    again.className = "ghost";
    again.textContent = "再来一个";
    again.addEventListener("click", () => { void recreate(item, again); });

    row.append(title, meta, again);
    row.__againButton = again;
    return row;
  }

  function update(items) {
    const rows = Array.isArray(items) ? items : [];
    list.replaceChildren();
    for (const item of rows) list.append(renderItem(item));
    if (rows.length === 0) {
      updateMessage("最近 180 天还没有出现两次以上的同类完成记录。");
      list.hidden = true;
    } else {
      updateMessage("");
      list.hidden = false;
    }
    return rows.length;
  }

  async function recreate(item, button) {
    if (busy) return;
    setBusy(true);
    button.disabled = true;
    try {
      const result = await createTaskAgain(requestJson, { key: item.key });
      updateMessage((result && result.message) || "已创建任务。");
      if (typeof onCreated === "function") await onCreated(result);
    } catch (error) {
      updateMessage("创建失败：" + String((error && error.message) || error));
    } finally {
      setBusy(false);
      button.disabled = false;
    }
  }

  async function expand() {
    if (busy) return;
    if (loaded && !list.hidden) {
      list.hidden = true;
      updateMessage("");
      return;
    }
    setBusy(true);
    try {
      const items = await loadFrequentCompleted(requestJson);
      loaded = true;
      update(items);
    } catch (error) {
      updateMessage("读取失败：" + String((error && error.message) || error));
    } finally {
      setBusy(false);
    }
  }

  toggle.addEventListener("click", () => { void expand(); });
  element.append(label, toggle, status, list);
  return { element, toggle, list, status, update, updateMessage, setBusy, expand };
}
