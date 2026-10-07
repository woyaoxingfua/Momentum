const STORAGE_PREFIX = "momentum_task_completion_v1:";
const UUID_V4_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function createUuidV4() {
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi?.randomUUID === "function") return cryptoApi.randomUUID();
  if (typeof cryptoApi?.getRandomValues !== "function") {
    throw new Error("当前环境无法安全生成 UUIDv4；没有发送完成请求。");
  }
  const bytes = cryptoApi.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function scopeStorageKey(userId, taskId) {
  return `${STORAGE_PREFIX}${encodeURIComponent(userId)}:${encodeURIComponent(String(taskId))}`;
}

function isDefiniteClientResponse(error) {
  return Number.isInteger(error?.status) && error.status >= 400 && error.status < 500;
}

export function createTaskCompletionAction({
  requestJson,
  storage = globalThis.localStorage,
  getUserId = () => storage?.getItem("momentum_user"),
  createId = createUuidV4,
} = {}) {
  if (typeof requestJson !== "function") throw new TypeError("requestJson is required");

  const inFlight = new Set();
  const listeners = new Set();

  function currentUserId() {
    const value = getUserId?.();
    return value == null || String(value).trim() === "" ? null : String(value).trim();
  }

  function emit(event) {
    for (const listener of listeners) {
      try { listener(event); } catch { /* UI observers must not affect request semantics. */ }
    }
  }

  function readIntent(userId, taskId) {
    const key = scopeStorageKey(userId, taskId);
    let raw;
    try { raw = storage?.getItem(key); }
    catch (error) { return { error: new Error(`无法读取本地完成请求状态：${error.message || "存储不可用"}`) }; }
    if (raw == null) return { key, intent: null };

    let parsed;
    try { parsed = JSON.parse(raw); }
    catch { return { key, intent: { task_id: String(taskId), status: "corrupt" } }; }
    if (
      !parsed || String(parsed.task_id) !== String(taskId)
      || !UUID_V4_PATTERN.test(String(parsed.idempotency_key || ""))
      || !["sending", "uncertain"].includes(parsed.status)
    ) {
      return { key, intent: { task_id: String(taskId), status: "corrupt" } };
    }

    const intent = {
      task_id: String(taskId),
      idempotency_key: String(parsed.idempotency_key),
      status: parsed.status,
    };
    if (intent.status === "sending" && !inFlight.has(key)) {
      intent.status = "uncertain";
      try { storage.setItem(key, JSON.stringify(intent)); }
      catch (error) { return { key, intent, error: new Error(`无法更新本地完成请求状态：${error.message || "存储不可用"}`) }; }
    }
    return { key, intent };
  }

  function persist(key, intent) {
    storage.setItem(key, JSON.stringify({
      task_id: String(intent.task_id),
      idempotency_key: intent.idempotency_key,
      status: intent.status,
    }));
  }

  function getPendingIntent(taskId) {
    const userId = currentUserId();
    if (!userId || taskId == null) return null;
    return readIntent(userId, taskId).intent || null;
  }

  function getPendingIntents() {
    const userId = currentUserId();
    if (!userId || !storage || typeof storage.key !== "function" || !Number.isInteger(storage.length)) return [];
    const prefix = `${STORAGE_PREFIX}${encodeURIComponent(userId)}:`;
    const taskIds = [];
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (typeof key !== "string" || !key.startsWith(prefix)) continue;
      try { taskIds.push(decodeURIComponent(key.slice(prefix.length))); }
      catch { /* Ignore keys that are outside the controller's encoding format. */ }
    }
    return taskIds.map((taskId) => getPendingIntent(taskId)).filter(Boolean);
  }

  function clearIntent(userId, taskId) {
    storage.removeItem(scopeStorageKey(userId, taskId));
  }

  async function send(taskId, { retry = false } = {}) {
    if (taskId == null || String(taskId).trim() === "") {
      return { status: "error", error: new Error("缺少任务 ID；没有发送完成请求。") };
    }
    const normalizedTaskId = String(taskId);
    const userId = currentUserId();
    if (!userId) {
      return { status: "error", error: new Error("无法识别当前登录用户；为避免跨用户复用请求，没有发送完成请求。") };
    }

    const key = scopeStorageKey(userId, normalizedTaskId);
    if (inFlight.has(key)) {
      return { status: "pending", intent: getPendingIntent(normalizedTaskId), inFlight: true };
    }

    const loaded = readIntent(userId, normalizedTaskId);
    if (loaded.error) return { status: "error", error: loaded.error };
    if (loaded.intent?.status === "corrupt") {
      return {
        status: "blocked",
        intent: loaded.intent,
        error: new Error("本地完成请求记录无法验证；为避免生成新 UUID 重复完成，已停止发送。"),
      };
    }
    if (retry && !loaded.intent) return { status: "error", error: new Error("没有可显式重试的待确认完成请求。") };
    if (!retry && loaded.intent) return { status: "pending", intent: loaded.intent, inFlight: false };

    let intent = loaded.intent;
    if (!intent) {
      let idempotencyKey;
      try { idempotencyKey = String(createId()); }
      catch (error) { return { status: "error", error }; }
      if (!UUID_V4_PATTERN.test(idempotencyKey)) {
        return { status: "error", error: new Error("UUID 生成器未提供有效 UUIDv4；没有发送完成请求。") };
      }
      intent = { task_id: normalizedTaskId, idempotency_key: idempotencyKey, status: "uncertain" };
    }

    // Lock and persist synchronously before invoking requestJson/fetch.
    inFlight.add(key);
    intent = { ...intent, status: "sending" };
    try { persist(key, intent); }
    catch (error) {
      inFlight.delete(key);
      return { status: "error", error: new Error(`无法持久化完成请求；没有发送请求：${error.message || "存储不可用"}`) };
    }
    emit({ type: "intent", user_id: userId, task_id: normalizedTaskId, intent: { ...intent } });

    try {
      const response = await requestJson(`/api/tasks/${encodeURIComponent(normalizedTaskId)}/done`, {
        method: "POST",
        headers: { "Idempotency-Key": intent.idempotency_key },
      });
      clearIntent(userId, normalizedTaskId);
      emit({ type: "outcome", outcome: "success", user_id: userId, task_id: normalizedTaskId, response });
      return { status: "success", response, intent: { ...intent } };
    } catch (error) {
      if (isDefiniteClientResponse(error)) {
        try { clearIntent(userId, normalizedTaskId); }
        catch (storageError) {
          return { status: "error", error, storageError, intent: { ...intent } };
        }
        emit({ type: "outcome", outcome: "error", user_id: userId, task_id: normalizedTaskId, error });
        return { status: "error", error, intent: { ...intent } };
      }

      intent = { ...intent, status: "uncertain" };
      try { persist(key, intent); }
      catch (storageError) {
        error.storageError = storageError;
      }
      emit({ type: "intent", user_id: userId, task_id: normalizedTaskId, intent: { ...intent }, error });
      return { status: "uncertain", error, intent: { ...intent } };
    } finally {
      inFlight.delete(key);
    }
  }

  const action = (taskId) => send(taskId, { retry: false });
  action.retry = (taskId) => send(taskId, { retry: true });
  action.getPendingIntent = getPendingIntent;
  action.getPendingIntents = getPendingIntents;
  action.getCurrentUserId = currentUserId;
  action.subscribe = (listener) => {
    if (typeof listener !== "function") return () => {};
    listeners.add(listener);
    return () => listeners.delete(listener);
  };
  return action;
}

export { UUID_V4_PATTERN };
