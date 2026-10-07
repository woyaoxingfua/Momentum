export const MAX_BACKUP_REQUEST_BYTES = 16 * 1024 * 1024;

export const V2_RESTORE_CONFIRMATION =
  "这是 v2.0 完整恢复，不是合并导入。只能恢复到当前登录用户的空数据域。" +
  "将恢复备份中的任务、任务关系、可归属的任务事件和非凭据用户记忆；" +
  "无法安全归属的事件（数量未知，不代表为零）及凭据类记忆不会恢复。" +
  "导入请求体上限为 16 MiB。请先确认目标数据域为空，再继续提交恢复。是否继续？";

function setStatus(status, text, isError = false) {
  if (!status) return;
  status.textContent = text;
  status.hidden = false;
  status.classList?.toggle("error-text", isError);
}

export function formatImportSuccess(version, payload) {
  const message = typeof payload?.message === "string" && payload.message.trim()
    ? payload.message.trim()
    : "备份数据已导入。";

  if (version === "1.0") {
    const rawMemoryCount = payload?.excluded_memory?.count;
    const memoryReason = typeof payload?.excluded_memory?.reason === "string" && payload.excluded_memory.reason.trim()
      ? payload.excluded_memory.reason.trim()
      : "user_memory 中键名标识为凭据的项目不会导出或导入，以避免备份携带 API key、口令或令牌。";
    const credentialNotice = Number.isSafeInteger(rawMemoryCount) && rawMemoryCount > 0
      ? `\n敏感凭据已跳过：${rawMemoryCount} 项。${memoryReason}`
      : "";
    return `v1.0 兼容合并已完成。${message}${credentialNotice}`;
  }

  const eventReason = typeof payload?.excluded_events?.reason === "string"
    ? payload.excluded_events.reason.trim()
    : "";
  const eventNotice = "事件日志未恢复：数量未知；这不表示为零，且无法安全归属。" +
    (eventReason ? ` ${eventReason}` : "");

  const rawMemoryCount = payload?.excluded_memory?.count;
  const memoryCount = Number.isSafeInteger(rawMemoryCount) && rawMemoryCount >= 0
    ? `${rawMemoryCount} 项`
    : "数量未返回";
  const memoryReason = typeof payload?.excluded_memory?.reason === "string"
    ? payload.excluded_memory.reason.trim()
    : "凭据类记忆不会导入。";

  return [
    `v2.0 完整恢复已完成。${message}`,
    eventNotice,
    `凭据类记忆未恢复：${memoryCount}。${memoryReason}`,
  ].join("\n");
}

export function formatImportError(error) {
  const detail = typeof error?.message === "string" ? error.message.trim() : "";
  if (error?.status === 409 || detail.includes("空数据域")) {
    return `完整恢复未执行，数据未改动。目标认证用户的数据域必须为空。${detail ? ` 服务器说明：${detail}` : ""}`;
  }
  if (error?.status === 400) {
    return `备份格式或版本错误，导入未执行，现有数据未改动。${detail ? ` 服务器说明：${detail}` : ""}`;
  }
  if (error?.status === 413) {
    return `导入失败：文件过大，服务器因超过 16 MiB 上限拒绝请求；数据未改动。${detail ? ` 服务器说明：${detail}` : ""}`;
  }
  if (detail.includes("超过 16 MiB")) {
    return `导入失败：文件过大（超过 16 MiB 上传上限），未提交，数据未改动。${detail ? ` ${detail}` : ""}`;
  }
  return `导入失败，现有数据未改动。${detail || "请检查备份文件并重试。"}`;
}

export function formatExportError(error) {
  const detail = typeof error?.message === "string" ? error.message.trim() : "";
  if (error?.status === 413 || /(?:超过|超出|过大).{0,16}16\s*MiB|16\s*MiB.{0,16}(?:上限|过大)/i.test(detail)) {
    return `导出失败：备份 JSON 超过 16 MiB 上限，未生成或下载文件。${detail ? ` 服务器说明：${detail}` : ""}`;
  }
  return `导出失败：${detail || "服务器未能生成完整备份。"} 未生成或下载文件。`;
}

export async function exportBackupData({
  requestJson,
  status,
  download,
  createObjectURL = (blob) => URL.createObjectURL(blob),
  revokeObjectURL = (url) => URL.revokeObjectURL(url),
}) {
  let objectUrl = null;
  try {
    const payload = await requestJson("/api/export");
    if (!payload || typeof payload !== "object" || Array.isArray(payload) || Object.hasOwn(payload, "error")) {
      throw new Error(typeof payload?.error === "string" ? payload.error : "服务器未返回有效备份 JSON。");
    }

    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    objectUrl = createObjectURL(blob);
    download(objectUrl, "momentum-export.json");
    setStatus(status, "备份 JSON 已生成并开始下载。");
    return payload;
  } catch (error) {
    setStatus(status, formatExportError(error), true);
    return null;
  } finally {
    if (objectUrl) revokeObjectURL(objectUrl);
  }
}

export async function importBackupFile(file, {
  status,
  requestJson,
  afterImport,
  confirmRestore = (message) => window.confirm(message),
}) {
  try {
    if (!file) throw new Error("未选择备份文件。");
    if (Number.isFinite(file.size) && file.size > MAX_BACKUP_REQUEST_BYTES) {
      throw new Error("备份文件超过 16 MiB 上传上限，未发送到服务器，数据未改动。");
    }

    let backup;
    try {
      backup = JSON.parse(await file.text());
    } catch {
      throw new Error("备份文件不是有效 JSON，未发送到服务器，现有数据未改动。");
    }
    if (!backup || typeof backup !== "object" || Array.isArray(backup)) {
      throw new Error("备份格式错误：顶层必须是 JSON 对象，未发送到服务器，现有数据未改动。");
    }

    const version = backup.version;
    if (version !== "1.0" && version !== "2.0") {
      throw new Error(`不支持的备份版本（${String(version ?? "缺失")}）；仅支持 1.0 和 2.0。未提交导入，现有数据未改动。`);
    }
    if (version === "2.0" && !confirmRestore(V2_RESTORE_CONFIRMATION)) {
      setStatus(status, "已取消 v2.0 完整恢复；数据未改动。");
      return null;
    }

    const body = JSON.stringify({ data: backup });
    const bodySize = new TextEncoder().encode(body).byteLength;
    if (bodySize > MAX_BACKUP_REQUEST_BYTES) {
      throw new Error("序列化后的导入请求体超过 16 MiB 上传上限，未发送到服务器，数据未改动。");
    }

    const payload = await requestJson("/api/import", { method: "POST", body });
    const successText = formatImportSuccess(version, payload);
    setStatus(status, successText);
    if (afterImport) {
      try {
        await afterImport();
      } catch {
        setStatus(status, `${successText}\n页面刷新失败；恢复已完成，请手动刷新查看数据。`);
      }
    }
    return payload;
  } catch (error) {
    setStatus(status, formatImportError(error), true);
    return null;
  }
}

export function bindBackupImport({ input, status, requestJson, afterImport }) {
  if (!input) return;
  input.addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    if (file) {
      await importBackupFile(file, { status, requestJson, afterImport });
    }
    event.target.value = "";
  });
}
