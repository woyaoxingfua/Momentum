/* 待确认操作（破坏性操作审批）的界面部分。

后端把被门禁拦下的操作记在 user_memory 里并通过 /api/approvals 暴露出来；
这里负责把它渲染成可见的「待确认操作」面板，并提供批准 / 取消两个动作。 */
import { requestJson } from "./api.js";

let elements = {};
let deps = {
  request: requestJson,
  createElement: (tag) => document.createElement(tag),
};

export function configureApprovals(overrides = {}) {
  const { elements: nextElements, ...rest } = overrides;
  if (nextElements) elements = nextElements;
  deps = { ...deps, ...rest };
}

function resolve(name, id) {
  return elements[name] || (typeof document !== "undefined" ? document.getElementById(id) : null);
}

export function formatApprovalLabel(item) {
  return item?.summary || `待确认操作 ${item?.id || ""}`.trim();
}

export function renderApprovalItem(item) {
  const row = deps.createElement("li");
  row.className = "approvals-item";
  row.setAttribute("data-approval-id", String(item.id));

  const label = deps.createElement("span");
  label.className = "approvals-summary";
  label.textContent = formatApprovalLabel(item);

  const approve = deps.createElement("button");
  approve.type = "button";
  approve.className = "primary approvals-approve";
  approve.textContent = "批准";
  approve.addEventListener("click", () => { void approvePending(item.id); });

  const reject = deps.createElement("button");
  reject.type = "button";
  reject.className = "ghost approvals-reject";
  reject.textContent = "取消";
  reject.addEventListener("click", () => { void rejectPending(item.id); });

  row.append(label, approve, reject);
  return row;
}

export async function loadPendingApprovals() {
  const panel = resolve("approvalsPanel", "approvalsPanel");
  const list = resolve("approvalsList", "approvalsList");
  if (!panel || !list) return [];
  let payload;
  try {
    payload = await deps.request("/api/approvals");
  } catch (error) {
    return [];
  }
  const items = Array.isArray(payload?.approvals) ? payload.approvals : [];
  list.replaceChildren(...items.map((item) => renderApprovalItem(item)));
  panel.hidden = items.length === 0;
  panel.style.display = items.length === 0 ? "none" : "";
  return items;
}

export async function approvePending(id) {
  await deps.request("/api/approvals/approve", { method: "POST", body: JSON.stringify({ id }) });
  return loadPendingApprovals();
}

export async function rejectPending(id) {
  await deps.request("/api/approvals/reject", { method: "POST", body: JSON.stringify({ id }) });
  return loadPendingApprovals();
}

export function initApprovals(els, options = {}) {
  elements = els || {};
  if (options.request) configureApprovals({ request: options.request });
  void loadPendingApprovals();
  if (typeof setInterval === "function") {
    setInterval(() => { void loadPendingApprovals(); }, 30000);
  }
}

