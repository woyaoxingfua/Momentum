import { requestJson } from "./api.js";

let heartbeatConfig = null;
let elements = {};
let heartbeatText, heartbeatSection;

export function initHeartbeat(els) {
  elements = els;
  heartbeatSection = els.heartbeatSection;
  heartbeatText = els.heartbeatText;
  
  if (els.heartbeatDismiss) {
    els.heartbeatDismiss.addEventListener("click", dismissHeartbeat);
  }
}

export async function loadHeartbeatConfig() {
  try {
    const payload = await requestJson("/api/heartbeat/config");
    heartbeatConfig = payload.config;
    
    if (elements.configHeartbeatEnabled) {
      elements.configHeartbeatEnabled.checked = heartbeatConfig.enabled;
    }
    if (elements.configHeartbeatStart) {
      elements.configHeartbeatStart.value = String(heartbeatConfig.start_hour).padStart(2, "0") + ":00";
    }
    if (elements.configHeartbeatEnd) {
      elements.configHeartbeatEnd.value = String(heartbeatConfig.end_hour).padStart(2, "0") + ":00";
    }
    if (elements.configHeartbeatInterval) {
      elements.configHeartbeatInterval.value = heartbeatConfig.interval_hours;
    }
    
    return heartbeatConfig;
  } catch (e) {
    console.warn("Failed to load heartbeat config", e);
    return null;
  }
}

export async function saveHeartbeatConfig() {
  const enabled = elements.configHeartbeatEnabled?.checked ?? false;
  const startHour = parseInt(elements.configHeartbeatStart?.value?.split(":")[0]) ?? 9;
  const endHour = parseInt(elements.configHeartbeatEnd?.value?.split(":")[0]) ?? 21;
  const intervalHours = parseInt(elements.configHeartbeatInterval?.value) ?? 4;
  
  await requestJson("/api/heartbeat/config", {
    method: "POST",
    body: JSON.stringify({ enabled, start_hour: startHour, end_hour: endHour, interval_hours: intervalHours }),
  });
  
  heartbeatConfig = { enabled, start_hour: startHour, end_hour: endHour, interval_hours: intervalHours };
  return heartbeatConfig;
}

async function checkAndShowHeartbeat() {
  if (!heartbeatConfig?.enabled) return;
  
  try {
    const payload = await requestJson("/api/heartbeat/suggestion");
    if (payload.should_trigger) {
      heartbeatText.textContent = payload.suggestion;
      heartbeatSection.style.display = "block";
    }
  } catch (e) {
    console.warn("Heartbeat check failed", e);
  }
}

function dismissHeartbeat() {
  heartbeatSection.style.display = "none";
}

export function startHeartbeatChecks() {
  // 每分钟检查一次
  setInterval(() => {
    if (heartbeatConfig?.enabled) checkAndShowHeartbeat();
  }, 60000);

  // 页面加载后立即检查一次
  setTimeout(() => {
    if (heartbeatConfig?.enabled) checkAndShowHeartbeat();
  }, 2000);
}
