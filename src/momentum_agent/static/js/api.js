export async function requestJson(url, options = {}) {
  const token = localStorage.getItem("momentum_token");
  const headers = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const response = await fetch(url, { headers, ...options });

  if (response.status === 401) {
    localStorage.removeItem("momentum_token");
    window.location.href = "/login.html";
    throw new Error("未登录");
  }

  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "请求失败");
  }
  return payload;
}

export async function logout() {
  const token = localStorage.getItem("momentum_token");
  // 尝试调用登出 API，但不阻塞页面跳转
  if (token) {
    fetch("/api/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": `Bearer ${token}` },
    }).catch(() => {}); // 静默忽略错误，页面会立即跳转
  }
  localStorage.removeItem("momentum_token");
  localStorage.removeItem("momentum_user");
  window.location.href = "/login.html";
}

export function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

export function formatDue(value) {
  if (!value) return "";
  const date = new Date(value);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const dueDay = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const diffDays = Math.round((dueDay - today) / 86400000);

  if (diffDays < 0) return `逾期${-diffDays}天`;
  if (diffDays === 0) return `今天 ${date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`;
  if (diffDays === 1) return "明天";
  if (diffDays <= 7) return `${diffDays}天后`;
  return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

export function toDatetimeLocal(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toISOString().slice(0, 16);
}

export function priorityText(priority) {
  return { high: "高", medium: "中", low: "低" }[priority] || "中";
}

export function recurrenceText(value) {
  return { daily: "每天", weekly: "每周", monthly: "每月" }[value] || "";
}
