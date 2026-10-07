export const DEFAULT_BACKGROUND_OPACITY = 15;
export const MAX_LOCAL_BACKGROUND_BYTES = 8 * 1024 * 1024;
const DB_NAME = "momentum-background-local";
const DB_VERSION = 1;
const STORE_NAME = "images";
const SAFE_IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/webp", "image/gif"]);
let activeContext = null;
let currentObjectUrl = "";

function localImageKey(storage) {
  const user = String(storage?.getItem("momentum_user") || "anonymous").replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 96);
  return `background:${user}`;
}

export function normalizeBackgroundUrl(value, baseHref = "http://localhost/") {
  const raw = String(value ?? "").trim();
  if (!raw) return "";
  let url;
  try {
    url = new URL(raw, baseHref);
  } catch {
    throw new Error("请输入有效的图片 URL");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("背景图片只支持 HTTP 或 HTTPS 地址");
  }
  if (url.username || url.password) throw new Error("图片 URL 不能包含账号或密码");
  return url.href;
}

export function isPublicImageUrl(value, baseHref = "http://localhost/") {
  let url;
  try { url = new URL(normalizeBackgroundUrl(value, baseHref)); }
  catch { return false; }
  const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  if (!host || host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local")) return false;
  if (host.includes(":")) {
    if (host === "::1" || host.startsWith("fc") || host.startsWith("fd") || host.startsWith("fe80")) return false;
  } else if (/^\d{1,3}(?:\.\d{1,3}){3}$/.test(host)) {
    const parts = host.split(".").map(Number);
    if (parts.some((part) => part > 255)) return false;
    const [a, b] = parts;
    if (a === 0 || a === 10 || a === 127 || a >= 224 || (a === 169 && b === 254)
      || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168)) return false;
  }
  return true;
}

export function opacityToCss(value) {
  const numeric = Number.parseFloat(value);
  if (!Number.isFinite(numeric)) return DEFAULT_BACKGROUND_OPACITY / 100;
  return Math.min(100, Math.max(0, numeric)) / 100;
}

export function cssImageValue(url) {
  const escaped = String(url)
    .replaceAll("\\", "\\\\")
    .replaceAll('"', '\\"')
    .replaceAll("\n", "\\a ")
    .replaceAll("\r", "\\d ")
    .replaceAll("\f", "\\c ");
  return `url("${escaped}")`;
}

export function applyBackground(root, url, opacity) {
  if (!url) {
    root.style.setProperty("--bg-image", "none");
    root.style.setProperty("--bg-opacity", "0");
    return;
  }
  const safeUrl = String(url).startsWith("blob:") ? url : normalizeBackgroundUrl(url);
  root.style.setProperty("--bg-image", cssImageValue(safeUrl));
  root.style.setProperty("--bg-opacity", String(opacityToCss(opacity)));
}

export function initBackground(root, storage) {
  const url = storage.getItem("momentum_bg_url") || "";
  const opacity = storage.getItem("momentum_bg_opacity") || String(DEFAULT_BACKGROUND_OPACITY);
  try {
    applyBackground(root, url, opacity);
  } catch {
    storage.removeItem("momentum_bg_url");
    storage.removeItem("momentum_bg_opacity");
    applyBackground(root, "", 0);
    return { url: "", opacity: String(DEFAULT_BACKGROUND_OPACITY) };
  }
  return { url, opacity };
}

function setStatus(element, message, isError = false) {
  if (!element) return;
  element.textContent = message;
  element.classList.toggle("error-text", isError);
}

function openDatabase(indexedDBRef = globalThis.indexedDB) {
  if (!indexedDBRef) return Promise.reject(new Error("此浏览器不支持本地图片存储"));
  return new Promise((resolve, reject) => {
    const request = indexedDBRef.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) db.createObjectStore(STORE_NAME);
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("无法打开本地图片存储"));
  });
}

async function saveLocalImage(blob, indexedDBRef, key) {
  const db = await openDatabase(indexedDBRef);
  try {
    await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readwrite");
      tx.objectStore(STORE_NAME).put(blob, key);
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error || new Error("本地图片保存失败"));
      tx.onabort = () => reject(tx.error || new Error("本地图片保存已取消"));
    });
  } finally { db.close(); }
}

async function readLocalImage(indexedDBRef = globalThis.indexedDB, key = "background:anonymous") {
  const db = await openDatabase(indexedDBRef);
  try {
    return await new Promise((resolve, reject) => {
      const request = db.transaction(STORE_NAME, "readonly").objectStore(STORE_NAME).get(key);
      request.onsuccess = () => resolve(request.result || null);
      request.onerror = () => reject(request.error || new Error("无法读取本地图片"));
    });
  } finally { db.close(); }
}

async function deleteLocalImage(indexedDBRef = globalThis.indexedDB, key = "background:anonymous") {
  if (!indexedDBRef) return;
  const db = await openDatabase(indexedDBRef);
  try {
    await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readwrite");
      tx.objectStore(STORE_NAME).delete(key);
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error || new Error("无法删除本地图片"));
    });
  } finally { db.close(); }
}

function verifyImage(url, ImageCtor, timeoutMs = 1000) {
  return new Promise((resolve) => {
    const image = new ImageCtor();
    let finished = false;
    const timer = setTimeout(() => finish(false), timeoutMs);
    function finish(ok) {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      resolve(ok);
    }
    image.referrerPolicy = "no-referrer";
    image.onload = () => finish(true);
    image.onerror = () => finish(false);
    image.src = url;
  });
}

async function saveCloudPreference(source, url, opacity, fetchRef) {
  const token = globalThis.localStorage?.getItem("momentum_token");
  if (!token) throw new Error("登录状态已失效，云端设置未保存");
  const response = await fetchRef("/api/preferences", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({
      background: { source, url, opacity: String(opacity) },
    }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || "云端背景设置保存失败");
}

function controlsFor(documentRef) {
  return ["config", "mobileConfig"].map((prefix) => ({
    prefix,
    url: documentRef.getElementById(`${prefix}BgUrl`),
    file: documentRef.getElementById(`${prefix}BgFile`),
    opacity: documentRef.getElementById(`${prefix}BgOpacity`),
    apply: documentRef.getElementById(`${prefix}BgApply`),
    clear: documentRef.getElementById(`${prefix}BgClear`),
    status: documentRef.getElementById(`${prefix}BgStatus`),
  }));
}

export function bindBackgroundSettings({
  documentRef = document,
  root = document.documentElement,
  storage = localStorage,
  ImageCtor = Image,
  indexedDBRef = globalThis.indexedDB,
  fetchRef = globalThis.fetch?.bind(globalThis),
  urlApi = globalThis.URL,
} = {}) {
  const controls = controlsFor(documentRef);
  const initial = initBackground(root, storage);
  activeContext = { controls, root, storage, ImageCtor, indexedDBRef, fetchRef, urlApi };

  function sync(url, opacity, source) {
    for (const control of controls) {
      if (control.url) control.url.value = source === "remote_url" ? url : "";
      if (control.opacity) control.opacity.value = opacity;
    }
  }
  sync(initial.url, initial.opacity, initial.url ? "remote_url" : "none");

  async function applyFrom(control) {
    const rawUrl = control.url?.value.trim() || "";
    const opacity = control.opacity?.value || String(DEFAULT_BACKGROUND_OPACITY);
    let url;
    try {
      url = normalizeBackgroundUrl(rawUrl, documentRef.location?.href);
      if (!isPublicImageUrl(url)) throw new Error("请输入可公开访问的 HTTP(S) 图片地址");
    } catch (error) { setStatus(control.status, error.message, true); return; }
    if (!url) { await clearBackground(); return; }
    setStatus(control.status, "正在检查公开图片…");
    if (!(await verifyImage(url, ImageCtor))) {
      setStatus(control.status, "图片加载失败，请确认地址公开可访问且指向图片。", true);
      return;
    }
    storage.setItem("momentum_bg_url", url);
    storage.setItem("momentum_bg_opacity", String(opacity));
    storage.setItem("momentum_bg_source", "remote_url");
    applyBackground(root, url, opacity);
    sync(url, opacity, "remote_url");
    try {
      await saveCloudPreference("remote_url", url, opacity, fetchRef);
      for (const item of controls) setStatus(item.status, "公开图片已应用并同步；此 URL 可在其他设备使用。");
    } catch (error) {
      setStatus(control.status, `本机已应用，但云端同步失败：${error.message}`, true);
    }
  }

  async function applyLocalFile(control, file) {
    if (!file) return;
    if (!SAFE_IMAGE_TYPES.has(file.type)) {
      setStatus(control.status, "请选择 PNG、JPEG、WebP 或 GIF 图片。", true);
      return;
    }
    if (file.size > MAX_LOCAL_BACKGROUND_BYTES) {
      setStatus(control.status, "图片不能超过 8 MB。", true);
      return;
    }
    if (!urlApi?.createObjectURL) {
      setStatus(control.status, "浏览器无法创建本地图片预览。", true);
      return;
    }
    try {
      await saveLocalImage(file, indexedDBRef, localImageKey(storage));
      const objectUrl = urlApi.createObjectURL(file);
      if (currentObjectUrl && currentObjectUrl !== objectUrl) urlApi.revokeObjectURL?.(currentObjectUrl);
      currentObjectUrl = objectUrl;
      const opacity = control.opacity?.value || String(DEFAULT_BACKGROUND_OPACITY);
      storage.removeItem("momentum_bg_url");
      storage.setItem("momentum_bg_opacity", String(opacity));
      storage.setItem("momentum_bg_source", "local");
      applyBackground(root, objectUrl, opacity);
      sync("", opacity, "local");
      try {
        await saveCloudPreference("local", "", opacity, fetchRef);
        for (const item of controls) setStatus(item.status, "图片只保存在此浏览器的 IndexedDB；云端仅记录“本地图片”来源，图片内容不会上传，暂不能跨设备同步。");
      } catch (error) {
        setStatus(control.status, `图片已保存在此浏览器；云端来源标记未同步：${error.message}`, true);
      }
    } catch (error) {
      setStatus(control.status, `本地保存失败：${error.message}`, true);
    }
  }

  async function clearBackground() {
    storage.removeItem("momentum_bg_url");
    storage.removeItem("momentum_bg_opacity");
    storage.removeItem("momentum_bg_source");
    applyBackground(root, "", 0);
    sync("", String(DEFAULT_BACKGROUND_OPACITY), "none");
    if (currentObjectUrl) urlApi?.revokeObjectURL?.(currentObjectUrl);
    currentObjectUrl = "";
    try { await deleteLocalImage(indexedDBRef, localImageKey(storage)); }
    catch { /* Clearing the visible setting should still succeed. */ }
    try {
      await saveCloudPreference("none", "", String(DEFAULT_BACKGROUND_OPACITY), fetchRef);
      for (const item of controls) setStatus(item.status, "背景已清除；本机图片和云端背景引用均已移除。");
    } catch (error) {
      setStatus(controls.find((item) => item.status)?.status, `本机已清除，云端同步失败：${error.message}`, true);
    }
  }

  for (const control of controls) {
    control.apply?.addEventListener("click", () => { void applyFrom(control); });
    control.clear?.addEventListener("click", () => { void clearBackground(); });
    control.file?.addEventListener("change", () => {
      const file = control.file.files?.[0];
      void applyLocalFile(control, file);
      control.file.value = "";
    });
  }

  return { restore: restoreBackgroundPreferences, clear: clearBackground };
}

export async function restoreBackgroundPreferences(preferences) {
  if (!activeContext || !preferences || !preferences.configured) return;
  const { controls, root, storage, ImageCtor, indexedDBRef, urlApi } = activeContext;
  const source = preferences.source;
  const opacity = String(preferences.opacity || DEFAULT_BACKGROUND_OPACITY);
  if (source === "remote_url") {
    const url = normalizeBackgroundUrl(preferences.url || "");
    if (!isPublicImageUrl(url) || !(await verifyImage(url, ImageCtor))) {
      for (const item of controls) setStatus(item.status, "已同步的图片 URL 当前无法加载，请检查链接。", true);
      return;
    }
    storage.setItem("momentum_bg_url", url);
    storage.setItem("momentum_bg_opacity", opacity);
    storage.setItem("momentum_bg_source", "remote_url");
    applyBackground(root, url, opacity);
    for (const item of controls) {
      if (item.url) item.url.value = url;
      if (item.opacity) item.opacity.value = opacity;
      setStatus(item.status, "已从账户设置恢复公开图片 URL；图片内容由浏览器直接读取。");
    }
    return;
  }
  if (source === "local") {
    storage.removeItem("momentum_bg_url");
    storage.setItem("momentum_bg_source", "local");
    try {
      const blob = await readLocalImage(indexedDBRef, localImageKey(storage));
      if (blob && urlApi?.createObjectURL) {
        if (currentObjectUrl) urlApi.revokeObjectURL?.(currentObjectUrl);
        currentObjectUrl = urlApi.createObjectURL(blob);
        applyBackground(root, currentObjectUrl, opacity);
        for (const item of controls) {
          if (item.url) item.url.value = "";
          if (item.opacity) item.opacity.value = opacity;
          setStatus(item.status, "图片只在此浏览器本地保存；其他设备需要重新上传，云端不含图片内容。");
        }
      } else {
        applyBackground(root, "", 0);
        for (const item of controls) setStatus(item.status, "账户默认背景是本地图片，但本设备没有图片副本；请在此设备重新上传。", true);
      }
    } catch (error) {
      applyBackground(root, "", 0);
      for (const item of controls) setStatus(item.status, `本地图片暂不可读取：${error.message}`, true);
    }
    return;
  }
  if (source === "none") {
    storage.removeItem("momentum_bg_url");
    storage.removeItem("momentum_bg_source");
    applyBackground(root, "", 0);
    for (const item of controls) {
      if (item.url) item.url.value = "";
      if (item.opacity) item.opacity.value = String(DEFAULT_BACKGROUND_OPACITY);
      setStatus(item.status, "当前没有同步背景。");
    }
  }
}
