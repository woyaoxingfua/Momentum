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
      if (key.trim() === "api_key" && elements.configApiKey) elements.configApiKey.value = value;
      if (key.trim() === "api_base" && elements.configApiBase) elements.configApiBase.value = value;
      if (key.trim() === "model" && elements.configModel) elements.configModel.value = value;
      if (key.trim() === "daily_capacity_minutes" && elements.configCapacity) elements.configCapacity.value = value;
      if (key.trim() === "working_hours_start" && elements.configWorkStart) elements.configWorkStart.value = value;
      if (key.trim() === "working_hours_end" && elements.configWorkEnd) elements.configWorkEnd.value = value;
    });
  }
}

export async function saveConfig() {
  const saveButton = document.querySelector("#configSaveButton");
  const originalText = saveButton.textContent;
  
  try {
    // 显示保存中状态
    saveButton.textContent = "保存中...";
    saveButton.disabled = true;
    
    const entries = [
      ["api_key", elements.configApiKey?.value || ""],
      ["api_base", elements.configApiBase?.value || ""],
      ["model", elements.configModel?.value || "gpt-4o"],
      ["daily_capacity_minutes", elements.configCapacity?.value || "45"],
      ["working_hours_start", elements.configWorkStart?.value || "09:00"],
      ["working_hours_end", elements.configWorkEnd?.value || "18:00"],
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
    
    // 重新加载 provider 状态
    const { loadProvider } = await import("./app.js");
    await loadProvider();
    
    // 显示成功提示
    saveButton.textContent = "✓ 已保存";
    saveButton.classList.add("success");
    
    // 3秒后恢复原状
    setTimeout(() => {
      saveButton.textContent = originalText;
      saveButton.disabled = false;
      saveButton.classList.remove("success");
    }, 2000);
    
  } catch (error) {
    // 显示错误提示
    saveButton.textContent = "保存失败";
    saveButton.classList.add("error");
    
    // 3秒后恢复原状
    setTimeout(() => {
      saveButton.textContent = originalText;
      saveButton.disabled = false;
      saveButton.classList.remove("error");
    }, 3000);
    
    console.error("保存配置失败:", error);
  }
}
