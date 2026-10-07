export const BUILT_IN_THEMES = ["paper", "ink", "sage", "midnight"];
const TOKEN_KEYS = ["bg", "bg2", "surface", "surface2", "surface3", "border", "border2", "text", "text2", "text3", "accent", "accent2"];
const CSS_VARIABLES = { bg: "--bg", bg2: "--bg-2", surface: "--surface", surface2: "--surface-2", surface3: "--surface-3", border: "--border", border2: "--border-2", text: "--text", text2: "--text-2", text3: "--text-3", accent: "--accent", accent2: "--accent-2" };
const THEME_PALETTES = {
  paper: { bg: "#f3f0e8", bg2: "#ebe6dc", surface: "#fffdf8", surface2: "#f7f2e8", surface3: "#eee7da", border: "#e0d8c8", border2: "#cfc5b3", text: "#252921", text2: "#5d6257", text3: "#8a8d80", accent: "#426b56", accent2: "#315441" },
  ink: { bg: "#20231f", bg2: "#282d27", surface: "#30362f", surface2: "#384137", surface3: "#424c40", border: "#424b40", border2: "#566252", text: "#eff0e8", text2: "#b9c0b1", text3: "#818c7d", accent: "#b3c995", accent2: "#c9dcaa" },
  sage: { bg: "#e8eee5", bg2: "#dfe8dc", surface: "#f7faf4", surface2: "#edf3e9", surface3: "#e1eadc", border: "#d3dfcd", border2: "#bdcfb5", text: "#28352c", text2: "#596a5d", text3: "#829184", accent: "#547b5d", accent2: "#3d6347" },
  midnight: { bg: "#171e2a", bg2: "#1e2736", surface: "#252f40", surface2: "#2d394c", surface3: "#35445a", border: "#354257", border2: "#495b73", text: "#ecf1f5", text2: "#b3c0cd", text3: "#8292a3", accent: "#8ab3c9", accent2: "#a4c8dc" },
};

function validColor(value) { return typeof value === "string" && /^#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$/.test(value); }
export function isSafePalette(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const keys = Object.keys(value);
  return keys.length === TOKEN_KEYS.length && keys.every((key) => TOKEN_KEYS.includes(key) && validColor(value[key]));
}
function readCustom(storage) {
  try {
    const value = JSON.parse(storage.getItem("momentum_custom_theme") || "null");
    return isSafePalette(value) ? value : null;
  } catch { return null; }
}
function setStatus(element, message, isError = false) {
  if (!element) return;
  element.textContent = message;
  element.classList.toggle("error-text", isError);
}

export function applyTheme(theme, { root = document.documentElement, storage = localStorage, palette = null } = {}) {
  const selected = theme === "custom" && isSafePalette(palette || readCustom(storage)) ? "custom" : BUILT_IN_THEMES.includes(theme) ? theme : "paper";
  root.setAttribute("data-theme", selected);
  const colors = selected === "custom" ? (palette || readCustom(storage)) : THEME_PALETTES[selected];
  if (colors) {
    for (const [key, value] of Object.entries(colors)) root.style.setProperty(CSS_VARIABLES[key], value);
    root.style.setProperty("--accent-bg", `color-mix(in srgb, ${colors.accent} 12%, transparent)`);
  }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", colors?.bg || "#f3f0e8");
  const moon = document.getElementById("themeIconMoon");
  const sun = document.getElementById("themeIconSun");
  if (moon && sun) {
    moon.style.display = selected === "ink" || selected === "midnight" ? "none" : "block";
    sun.style.display = selected === "ink" || selected === "midnight" ? "block" : "none";
  }
  storage.setItem("momentum_theme", selected);
  document.querySelectorAll("[data-theme-select]").forEach((select) => { select.value = selected; });
  return selected;
}

async function syncBuiltInTheme(theme) {
  const token = localStorage.getItem("momentum_token");
  if (!token) return;
  const response = await fetch("/api/preferences", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ theme }),
  });
  if (!response.ok) throw new Error("主题已在本机应用，但账户同步失败");
}

export function initAppearance(root = document.documentElement, storage = localStorage) {
  const saved = storage.getItem("momentum_theme") || "paper";
  applyTheme(saved, { root, storage });
  return {
    apply: (theme) => applyTheme(theme, { root, storage }),
    toggle: () => applyTheme(root.getAttribute("data-theme") === "ink" ? "paper" : "ink", { root, storage }),
  };
}

export async function loadAppearancePreference(root = document.documentElement, storage = localStorage) {
  const token = storage.getItem("momentum_token");
  if (!token) return null;
  try {
    const response = await fetch("/api/preferences", { headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok) return null;
    const data = await response.json();
    if (storage.getItem("momentum_theme") !== "custom" && BUILT_IN_THEMES.includes(data.theme)) {
      applyTheme(data.theme, { root, storage });
    }
    return data;
  } catch { return null; }
}

export function bindAppearanceSettings({ documentRef = document, root = document.documentElement, storage = localStorage } = {}) {
  const controls = ["config", "mobileConfig"].map((prefix) => ({
    select: documentRef.getElementById(`${prefix}ThemeSelect`),
    importButton: documentRef.getElementById(`${prefix}ThemeImportButton`),
    file: documentRef.getElementById(`${prefix}ThemeImportFile`),
    status: documentRef.getElementById(`${prefix}ThemeStatus`),
  }));
  controls.forEach(({ select, importButton, file, status }) => {
    if (select) {
      select.value = root.getAttribute("data-theme") || "paper";
      select.addEventListener("change", async () => {
        const theme = select.value;
        if (theme === "custom" && !readCustom(storage)) {
          setStatus(status, "请先导入安全配色 JSON。", true);
          select.value = storage.getItem("momentum_theme") || "paper";
          return;
        }
        applyTheme(theme, { root, storage });
        if (theme === "custom") {
          for (const item of controls) setStatus(item.status, "自定义配色只保存在此浏览器，不执行任意 CSS/JavaScript，也不参与账户同步。");
          return;
        }
        try {
          await syncBuiltInTheme(theme);
          for (const item of controls) setStatus(item.status, "主题已应用并同步到账户。");
        } catch (error) { setStatus(status, error.message, true); }
      });
    }
    importButton?.addEventListener("click", () => file?.click());
    file?.addEventListener("change", async () => {
      const chosen = file.files?.[0];
      file.value = "";
      if (!chosen) return;
      if (chosen.size > 8192) { setStatus(status, "配色文件不能超过 8 KB。", true); return; }
      try {
        const palette = JSON.parse(await chosen.text());
        if (!isSafePalette(palette)) throw new Error("JSON 必须只含完整的白名单颜色键和十六进制颜色值");
        storage.setItem("momentum_custom_theme", JSON.stringify(palette));
        applyTheme("custom", { root, storage, palette });
        for (const item of controls) {
          if (item.select) item.select.value = "custom";
          setStatus(item.status, "安全配色已导入：只接受颜色令牌，不执行 HTML、CSS 或 JavaScript；仅保存在此浏览器。");
        }
      } catch (error) { setStatus(status, `配色导入失败：${error.message}`, true); }
    });
  });
}
