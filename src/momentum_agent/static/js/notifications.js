/* ── Browser Notifications ───────────────────────────────────── */

import { requestJson } from "./api.js";

let _lastNotifiedIds = new Set();

export function initNotifications() {
  if (!("Notification" in window)) return;

  // 请求权限
  if (Notification.permission === "default") {
    Notification.requestPermission();
  }

  // 每 2 分钟检查一次即将到来的任务
  checkUpcoming();
  setInterval(checkUpcoming, 2 * 60 * 1000);
}

async function checkUpcoming() {
  if (Notification.permission !== "granted") return;
  if (document.visibilityState === "visible") return; // 页面可见时不弹原生通知

  try {
    const data = await requestJson("/api/notifications/upcoming");
    for (const n of data.notifications) {
      const key = `${n.id}-${n.minutes_left}`;
      if (_lastNotifiedIds.has(key)) continue;
      _lastNotifiedIds.add(key);

      new Notification(n.minutes_left <= 0 ? "任务已到期" : "任务即将到期", {
        body: `「${n.title}」${n.minutes_left <= 0 ? "已经到期" : `还有 ${n.minutes_left} 分钟到期`}`,
        icon: "/icon.svg",
        tag: String(n.id),
      });
    }
  } catch {
    // 静默失败
  }
}

export function notify(title, body) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  new Notification(title, { body, icon: "/icon.svg" });
}
