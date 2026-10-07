// 任务附件：只保存在当前浏览器的 IndexedDB，绑定「登录用户 + 任务身份」。
// 文件内容不上传、不写日志、不进入 /api/export 备份，也不参与 Agent/视觉解析。

export const MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024;

export const ALLOWED_ATTACHMENT_TYPES = new Set([
  "image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf",
]);

const BLOCKED_EXTENSIONS = new Set([
  "svg", "html", "htm", "xhtml", "xml", "js", "mjs", "cjs", "exe", "bat", "cmd", "com", "scr",
  "msi", "dll", "sh", "ps1", "jar", "zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "apk",
]);

const DB_NAME = "momentum-attachments-local";
const DB_VERSION = 1;
const STORE_NAME = "attachments";
const TASK_INDEX = "by_task";

export const ATTACHMENT_NOTICE =
  "附件只保存在当前浏览器，清理网站数据可能永久删除，不同浏览器或设备之间不共享，也不会进入 Momentum 备份，且未加密；请勿存放需要额外保密保护的材料。";

export function attachmentUserKey(storage) {
  const raw = String(storage?.getItem("momentum_user") || "anonymous");
  return raw.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 96) || "anonymous";
}

export function taskIdentity(task) {
  const id = task?.id;
  if (id === null || id === undefined || String(id) === "") {
    throw new Error("缺少任务标识，无法绑定附件。");
  }
  const createdAt = String(task?.created_at || "");
  if (!createdAt) throw new Error("缺少任务创建标识，无法绑定附件。");
  return { taskId: String(id), taskCreatedAt: createdAt };
}

export function fileExtension(name) {
  const value = String(name || "");
  const index = value.lastIndexOf(".");
  return index < 0 ? "" : value.slice(index + 1).toLowerCase();
}

export function validateAttachmentFile(file) {
  if (!file) return { ok: false, message: "没有选择文件。" };
  const extension = fileExtension(file.name);
  if (extension && BLOCKED_EXTENSIONS.has(extension)) {
    return { ok: false, message: "不支持 ." + extension + " 文件，只接受常见图片和 PDF。" };
  }
  const type = String(file.type || "").toLowerCase();
  if (!ALLOWED_ATTACHMENT_TYPES.has(type)) {
    return { ok: false, message: "只接受 JPEG、PNG、WebP、GIF 图片或 PDF 文件。" };
  }
  const size = Number(file.size || 0);
  if (size <= 0) return { ok: false, message: "文件为空，未添加。" };
  if (size > MAX_ATTACHMENT_BYTES) return { ok: false, message: "单个附件不能超过 8 MiB。" };
  return { ok: true, message: "" };
}

export function makeAttachmentKey(user, taskId, taskCreatedAt, fileId) {
  return [user, taskId, taskCreatedAt, fileId].map((part) => String(part)).join("|");
}

export function newFileId(random = Math.random, now = Date.now) {
  const rand = Math.floor(random() * 0xffffffff).toString(16).padStart(8, "0");
  return now().toString(36) + "-" + rand;
}

export function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return value + " B";
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KiB";
  return (value / (1024 * 1024)).toFixed(1) + " MiB";
}

export function summarizeAttachments(records) {
  const list = Array.isArray(records) ? records : [];
  return {
    count: list.length,
    totalBytes: list.reduce((sum, item) => sum + Number(item?.size || 0), 0),
  };
}

export function isQuotaError(error) {
  const name = String(error?.name || "");
  return name === "QuotaExceededError" || name === "NS_ERROR_DOM_QUOTA_REACHED";
}

export function attachmentErrorMessage(error, action) {
  if (isQuotaError(error)) {
    return "浏览器本地存储空间不足，" + action + "失败；已有附件没有改动，请清理浏览器存储或先删除部分附件。";
  }
  return action + "失败：" + String(error?.message || error);
}

function openDatabase(indexedDBRef) {
  if (!indexedDBRef) return Promise.reject(new Error("此浏览器不支持 IndexedDB，无法保存本地附件。"));
  return new Promise((resolve, reject) => {
    const request = indexedDBRef.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        const store = db.createObjectStore(STORE_NAME, { keyPath: "key" });
        store.createIndex(TASK_INDEX, ["user", "taskId", "taskCreatedAt"], { unique: false });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("无法打开本地附件存储。"));
  });
}

function transactionDone(tx, message) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error || new Error(message));
    tx.onabort = () => reject(tx.error || new Error(message));
  });
}

export async function addAttachmentRecord(record, { indexedDBRef = globalThis.indexedDB } = {}) {
  const db = await openDatabase(indexedDBRef);
  try {
    const tx = db.transaction(STORE_NAME, "readwrite");
    const done = transactionDone(tx, "附件保存失败");
    tx.objectStore(STORE_NAME).put(record);
    await done;
    return record;
  } finally { db.close(); }
}

export async function addAttachmentFile(file, identity, options = {}) {
  const check = validateAttachmentFile(file);
  if (!check.ok) return { ok: false, message: check.message };
  const {
    indexedDBRef = globalThis.indexedDB,
    storage = globalThis.localStorage,
    now = Date.now,
    random = Math.random,
  } = options;
  const user = attachmentUserKey(storage);
  const createdAt = new Date(now()).toISOString();
  const record = {
    key: makeAttachmentKey(user, identity.taskId, identity.taskCreatedAt, newFileId(random, now)),
    user,
    taskId: String(identity.taskId),
    taskCreatedAt: String(identity.taskCreatedAt),
    name: String(file.name || "未命名附件").slice(0, 200),
    type: String(file.type || ""),
    size: Number(file.size || 0),
    createdAt,
    blob: file,
  };
  try {
    await addAttachmentRecord(record, { indexedDBRef });
    return { ok: true, record };
  } catch (error) {
    return { ok: false, message: attachmentErrorMessage(error, "附件保存") };
  }
}

export async function listTaskAttachments(identity, { indexedDBRef = globalThis.indexedDB } = {}) {
  const db = await openDatabase(indexedDBRef);
  try {
    const tx = db.transaction(STORE_NAME, "readonly");
    const index = tx.objectStore(STORE_NAME).index(TASK_INDEX);
    return await new Promise((resolve, reject) => {
      const request = index.getAll([identity.user, String(identity.taskId), String(identity.taskCreatedAt)]);
      request.onsuccess = () => {
        const rows = (request.result || []).slice();
        rows.sort((a, b) => String(a.createdAt).localeCompare(String(b.createdAt)));
        resolve(rows);
      };
      request.onerror = () => reject(request.error || new Error("无法读取本地附件。"));
    });
  } finally { db.close(); }
}

export async function deleteAttachmentRecord(key, { indexedDBRef = globalThis.indexedDB } = {}) {
  const db = await openDatabase(indexedDBRef);
  try {
    const tx = db.transaction(STORE_NAME, "readwrite");
    const done = transactionDone(tx, "附件删除失败");
    tx.objectStore(STORE_NAME).delete(key);
    await done;
  } finally { db.close(); }
}

export async function deleteTaskAttachments(identity, { indexedDBRef = globalThis.indexedDB } = {}) {
  const db = await openDatabase(indexedDBRef);
  try {
    const tx = db.transaction(STORE_NAME, "readwrite");
    const done = transactionDone(tx, "清理任务附件失败");
    const index = tx.objectStore(STORE_NAME).index(TASK_INDEX);
    const request = index.openKeyCursor([identity.user, String(identity.taskId), String(identity.taskCreatedAt)]);
    let removed = 0;
    await new Promise((resolve, reject) => {
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor) { resolve(); return; }
        cursor.delete();
        removed += 1;
        cursor.continue();
      };
      request.onerror = () => reject(request.error || new Error("清理任务附件失败。"));
    });
    await done;
    return removed;
  } finally { db.close(); }
}

export async function countTaskAttachments(tasks, options = {}) {
  const storage = options.storage ?? globalThis.localStorage;
  const user = attachmentUserKey(storage);
  const counts = new Map();
  for (const task of Array.isArray(tasks) ? tasks : []) {
    let identity;
    try { identity = taskIdentity(task); } catch { continue; }
    try {
      const rows = await listTaskAttachments({ user, ...identity }, options);
      counts.set(String(identity.taskId), rows.length);
    } catch {
      counts.set(String(identity.taskId), 0);
    }
  }
  return counts;
}

export async function cleanupAttachmentsForTask(task, options = {}) {
  const storage = options.storage ?? globalThis.localStorage;
  try {
    const identity = taskIdentity(task);
    return await deleteTaskAttachments({ user: attachmentUserKey(storage), ...identity }, options);
  } catch {
    return 0;
  }
}

// ── 界面绑定：任务卡「附件」按钮 + 附件对话框 ──────────────────────

let attachmentUi = null;

function attachmentElements(documentRef) {
  return {
    dialog: documentRef.getElementById("attachmentDialog"),
    title: documentRef.getElementById("attachmentTitle"),
    list: documentRef.getElementById("attachmentList"),
    file: documentRef.getElementById("attachmentFile"),
    status: documentRef.getElementById("attachmentStatus"),
    notice: documentRef.getElementById("attachmentNotice"),
    close: documentRef.getElementById("attachmentCloseButton"),
  };
}

function summaryText(records) {
  const summary = summarizeAttachments(records);
  return summary.count + " 个附件，共 " + formatBytes(summary.totalBytes);
}

export function bindAttachmentDialog(options = {}) {
  const documentRef = options.documentRef || globalThis.document;
  const storage = options.storage || globalThis.localStorage;
  const indexedDBRef = options.indexedDBRef || globalThis.indexedDB;
  const urlApi = options.urlApi || globalThis.URL;
  const openWindow = options.openWindow || ((url) => globalThis.open?.(url, "_blank", "noopener"));
  const els = attachmentElements(documentRef);
  let currentTask = null;
  let objectUrls = [];

  function releaseUrls() {
    for (const url of objectUrls) urlApi?.revokeObjectURL?.(url);
    objectUrls = [];
  }

  function setStatus(message, isError = false) {
    if (!els.status) return;
    els.status.textContent = message || "";
    els.status.classList.toggle("error-text", Boolean(isError));
  }

  function renderRows(records) {
    if (!els.list) return;
    els.list.replaceChildren();
    if (!records.length) {
      const empty = documentRef.createElement("div");
      empty.className = "attachment-empty";
      empty.textContent = "此任务还没有本地附件。";
      els.list.append(empty);
      return;
    }
    for (const record of records) els.list.append(renderRow(record));
  }

  function renderRow(record) {
    const row = documentRef.createElement("div");
    row.className = "attachment-row";
    if (String(record.type || "").startsWith("image/") && urlApi?.createObjectURL) {
      const url = urlApi.createObjectURL(record.blob);
      objectUrls.push(url);
      const image = documentRef.createElement("img");
      image.className = "attachment-thumb";
      image.alt = record.name;
      image.src = url;
      row.append(image);
    }
    const meta = documentRef.createElement("div");
    meta.className = "attachment-meta";
    const name = documentRef.createElement("span");
    name.className = "attachment-name";
    name.textContent = record.name;
    const size = documentRef.createElement("span");
    size.className = "attachment-size";
    size.textContent = formatBytes(record.size);
    meta.append(name, size);
    row.append(meta);
    const actions = documentRef.createElement("div");
    actions.className = "attachment-actions";
    const openButton = documentRef.createElement("button");
    openButton.type = "button";
    openButton.textContent = "打开";
    openButton.addEventListener("click", () => {
      if (!urlApi?.createObjectURL) { setStatus("浏览器无法创建本地预览。", true); return; }
      const url = urlApi.createObjectURL(record.blob);
      objectUrls.push(url);
      openWindow(url);
    });
    const removeButton = documentRef.createElement("button");
    removeButton.type = "button";
    removeButton.className = "danger";
    removeButton.textContent = "删除";
    removeButton.addEventListener("click", () => { void removeRecord(record); });
    actions.append(openButton, removeButton);
    row.append(actions);
    return row;
  }

  async function reload() {
    if (!currentTask) return;
    let identity;
    try { identity = taskIdentity(currentTask); }
    catch (error) { renderRows([]); setStatus(error.message, true); return; }
    try {
      const rows = await listTaskAttachments({ user: attachmentUserKey(storage), ...identity }, { indexedDBRef });
      releaseUrls();
      renderRows(rows);
      setStatus(rows.length ? summaryText(rows) : "", false);
    } catch (error) {
      releaseUrls();
      renderRows([]);
      setStatus(attachmentErrorMessage(error, "读取附件"), true);
    }
  }

  async function removeRecord(record) {
    try {
      await deleteAttachmentRecord(record.key, { indexedDBRef });
      await reload();
    } catch (error) {
      setStatus(attachmentErrorMessage(error, "附件删除"), true);
    }
  }

  async function addFiles(files) {
    if (!currentTask) return;
    let identity;
    try { identity = taskIdentity(currentTask); }
    catch (error) { setStatus(error.message, true); return; }
    const messages = [];
    let added = 0;
    for (const file of Array.from(files || [])) {
      const result = await addAttachmentFile(file, identity, { indexedDBRef, storage });
      if (result.ok) added += 1;
      else messages.push(result.message);
    }
    await reload();
    if (messages.length) setStatus(messages.join(" "), true);
    else if (added > 0) setStatus("已添加 " + added + " 个本地附件；文件未上传。", false);
  }

  function close() {
    releaseUrls();
    currentTask = null;
    els.dialog?.close?.();
  }

  async function open(task) {
    currentTask = task;
    if (els.title) els.title.textContent = task?.title ? "任务附件：" + task.title : "任务附件";
    if (els.notice) els.notice.textContent = ATTACHMENT_NOTICE;
    setStatus("", false);
    if (els.dialog && !els.dialog.open) els.dialog.showModal?.();
    await reload();
  }

  els.close?.addEventListener("click", close);
  els.dialog?.addEventListener("close", () => { releaseUrls(); currentTask = null; });
  els.file?.addEventListener("change", () => {
    const files = els.file.files;
    els.file.value = "";
    void addFiles(files);
  });

  const handle = { open, close, reload, elements: els };
  attachmentUi = handle;
  return handle;
}

export function openAttachmentDialog(task, options = {}) {
  const handle = attachmentUi || bindAttachmentDialog(options);
  return handle.open(task);
}

export async function refreshAttachmentBadges(tasks, options = {}) {
  const documentRef = options.documentRef || globalThis.document;
  if (!documentRef?.querySelectorAll) return;
  const counts = await countTaskAttachments(tasks, options);
  documentRef.querySelectorAll("[data-attachments]").forEach((button) => {
    const count = counts.get(String(button.dataset.attachments)) || 0;
    button.textContent = count > 0 ? "附件 " + count : "附件";
  });
}
