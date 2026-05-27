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

export function logout() {
  const token = localStorage.getItem("momentum_token");
  if (token) {
    fetch("/api/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": `Bearer ${token}` },
    });
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
  if (!value) return "无截止";
  const date = new Date(value);
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
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
