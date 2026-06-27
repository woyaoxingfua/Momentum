import { requestJson } from "./api.js";
import { saveHeartbeatConfig } from "./heartbeat.js";
import { loadAdvice } from "./advice.js";

let elements = {};
let adviceText;
let onConfigSaved = null;

export function initConfig(els, adviceEl) {
  Object.assign(elements, els);
  adviceText = adviceEl;
  bindProviderUI();
}

export function setOnConfigSaved(fn) {
  onConfigSaved = fn;
}

function isOllamaMode() {
  return elements.configProvider?.value === "ollama";
}

function updateProviderUI() {
  const ollama = isOllamaMode();
  const apiKey = elements.configApiKey;
  const base = elements.configApiBase;
  const model = elements.configModel;
  const refreshRow = document.getElementById("configModelRefreshRow");

  if (apiKey) {
    apiKey.placeholder = ollama ? "ollama（本地无需 key）" : "sk-... 留空使用本地 fallback";
    if (ollama && !apiKey.value) apiKey.value = "ollama";
  }
  if (base) {
    base.placeholder = ollama ? "http://localhost:11434" : "https://api.openai.com/v1";
    if (ollama && !base.value) base.value = "http://localhost:11434";
  }
  if (model) {
    model.placeholder = ollama ? "如：llama3.2、qwen2.5" : "gpt-4o、claude-3-opus、deepseek-chat";
  }
  if (refreshRow) refreshRow.style.display = ollama ? "flex" : "none";
}

function bindProviderUI() {
  const provider = elements.configProvider;
  if (!provider) return;
  provider.addEventListener("change", () => {
    updateProviderUI();
    document.getElementById("configModelSelect").style.display = "none";
  });

  const refreshBtn = document.getElementById("configRefreshModels");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", async () => {
      await refreshOllamaModels("config");
    });
  }

  const modelSelect = document.getElementById("configModelSelect");
  if (modelSelect) {
    modelSelect.addEventListener("change", () => {
      if (elements.configModel) elements.configModel.value = modelSelect.value;
    });
  }
}

async function refreshOllamaModels(prefix) {
  const btn = document.getElementById(`${prefix}RefreshModels`);
  const select = document.getElementById(`${prefix}ModelSelect`);
  const original = btn.textContent;
  btn.textContent = "刷新中...";
  btn.disabled = true;
  try {
    const data = await requestJson("/api/provider/models");
    if (data.error) throw new Error(data.error);
    select.innerHTML = data.models.map((m) => `<option value="${m}">${m}</option>`).join("");
    select.style.display = data.models.length ? "block" : "none";
    if (data.models.length) select.value = data.models[0];
  } catch (e) {
    select.innerHTML = `<option>${e.message}</option>`;
    select.style.display = "block";
  } finally {
    btn.textContent = original;
    btn.disabled = false;
  }
}

export async function loadConfig() {
  const payload = await requestJson("/api/config");
  const text = payload.config;
  if (text && !text.startsWith("没有配置项")) {
    const lines = text.split("\n").filter((l) => l.includes("="));
    lines.forEach((line) => {
      const [key, ...rest] = line.replaceAll("  ", "").split("=");
      const value = rest.join("=").trim();
      if (key.trim() === "provider" && elements.configProvider) elements.configProvider.value = value;
      if (key.trim() === "api_key" && elements.configApiKey) elements.configApiKey.value = value;
      if (key.trim() === "api_base" && elements.configApiBase) elements.configApiBase.value = value;
      if (key.trim() === "model" && elements.configModel) elements.configModel.value = value;
      if (key.trim() === "vision_enabled" && elements.configVisionEnabled) {
        elements.configVisionEnabled.checked = value === "true";
      }
      if (key.trim() === "daily_capacity_minutes" && elements.configCapacity) elements.configCapacity.value = value;
      if (key.trim() === "working_hours_start" && elements.configWorkStart) elements.configWorkStart.value = value;
      if (key.trim() === "working_hours_end" && elements.configWorkEnd) elements.configWorkEnd.value = value;
    });
  }
  updateProviderUI();
}

export async function saveConfig() {
  const saveButton = document.querySelector("#configSaveButton");
  const originalText = saveButton.textContent;

  try {
    saveButton.textContent = "保存中...";
    saveButton.disabled = true;

    const entries = [
      ["provider", elements.configProvider?.value || "openai"],
      ["api_key", elements.configApiKey?.value || ""],
      ["api_base", elements.configApiBase?.value || ""],
      ["model", elements.configModel?.value || "gpt-4o"],
      ["vision_enabled", elements.configVisionEnabled?.checked ? "true" : "false"],
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

    await saveHeartbeatConfig();

    await loadAdvice();

    if (onConfigSaved) await onConfigSaved();

    saveButton.textContent = "✓ 已保存";
    saveButton.classList.add("success");

    setTimeout(() => {
      saveButton.textContent = originalText;
      saveButton.disabled = false;
      saveButton.classList.remove("success");
    }, 2000);

  } catch (error) {
    saveButton.textContent = "保存失败";
    saveButton.classList.add("error");

    setTimeout(() => {
      saveButton.textContent = originalText;
      saveButton.disabled = false;
      saveButton.classList.remove("error");
    }, 3000);

    console.error("保存配置失败:", error);
  }
}

export { refreshOllamaModels };
