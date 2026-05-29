import { requestJson } from "./api.js";

let elements = {};
let adviceText;

export function initConfig(els, adviceEl) {
  Object.assign(elements, els);
  adviceText = adviceEl;
}

export async function loadConfig() {
  const payload = await requestJson("/api/config");
  const text = payload.config;
  if (text && !text.startsWith("没有配置项")) {
    const lines = text.split("\n").filter((l) => l.includes("="));
    lines.forEach((line) => {
      const [key, ...rest] = line.replace("  ", "").split("=");
      const value = rest.join("=").trim();
      if (key.trim() === "daily_capacity_minutes") elements.configCapacity.value = value;
      if (key.trim() === "working_hours_start") elements.configWorkStart.value = value;
      if (key.trim() === "working_hours_end") elements.configWorkEnd.value = value;
    });
  }
}

export async function saveConfig() {
  const saveBtn = elements.configSaveButton;
  const originalText = saveBtn.textContent;
  saveBtn.disabled = true;
  saveBtn.textContent = "⏳ 保存中...";
  
  try {
    const entries = [
      ["daily_capacity_minutes", elements.configCapacity.value || "45"],
      ["working_hours_start", elements.configWorkStart.value || "09:00"],
      ["working_hours_end", elements.configWorkEnd.value || "18:00"],
    ];
    for (const [key, value] of entries) {
      await requestJson("/api/config", {
        method: "POST",
        body: JSON.stringify({ key, value }),
      });
    }
    
    // 保存心跳配置
    const { saveHeartbeatConfig } = await import("./heartbeat.js");
    await saveHeartbeatConfig();
    
    const { loadAdvice } = await import("./advice.js");
    await loadAdvice();
    
    if (typeof window.showToast === "function") {
      window.showToast("✅ 配置已保存！");
    }
  } catch (err) {
    if (typeof window.showToast === "function") {
      window.showToast(`❌ 保存失败：${err.message}`);
    }
  } finally {
    saveBtn.disabled = false;
    saveBtn.textContent = originalText;
  }
}
