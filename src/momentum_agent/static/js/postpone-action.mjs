import { getCurrentFocusUserId } from "./focus-recovery.mjs";

export const POSTPONE_INTENT_VERSION = 1;
export const POSTPONE_INTENT_KEY_PREFIX = "momentum_task_postpone_v1:";
const UUID_V4_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function postponeIntentStorageKey(userId, taskId) {
  if (!validUserId(userId) || !validTaskId(taskId)) return null;
  return `${POSTPONE_INTENT_KEY_PREFIX}${encodeURIComponent(userId)}:${encodeURIComponent(String(taskId))}`;
}

function validUserId(value) {
  return typeof value === "string" && value.trim().length > 0 && value.length <= 256;
}

function validTaskId(value) {
  return value !== null && value !== undefined && /^\d+$/.test(String(value)) && Number.isSafeInteger(Number(value)) && Number(value) > 0;
}

function currentStorage(injected) {
  if (injected) return injected;
  try { return globalThis.localStorage; } catch { return null; }
}

function makeUuidV4() {
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi?.randomUUID === "function") return cryptoApi.randomUUID();
  if (typeof cryptoApi?.getRandomValues !== "function") throw new Error("当前环境无法安全生成顺延幂等键");
  const bytes = cryptoApi.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function makeFingerprint(taskId, body) {
  return JSON.stringify({ method: "POST", task_id: String(taskId), days: 1, body });
}

function errorMessageForIntent(intent) {
  if (intent.status === "conflict") {
    return "检测到 409 idempotency_conflict：本地请求与服务器记录不一致。已保留原 UUID 与 days=1 请求；不会自动换 key 或改请求。";
  }
  if (intent.status === "key_error") {
    return "服务端以 HTTP 400 拒绝了顺延幂等键/请求。已保留原 UUID 与原请求；不会自动生成新 key。";
  }
  if (intent.status === "sending") {
    return "顺延请求正在处理；为避免重复顺延，普通顺延入口已锁定。";
  }
  return intent.last_error_message || "顺延请求结果不确定。请只使用原请求再试；页面加载不会自动重发。";
}

function withErrorProperties(error, properties) {
  const normalized = error instanceof Error ? error : new Error(String(error || "顺延失败"));
  Object.assign(normalized, properties);
  return normalized;
}

function responseCode(error) {
  const payload = error?.payload;
  return [payload?.error, payload?.code, error?.code, error?.message]
    .filter((value) => typeof value === "string")
    .some((value) => value === "idempotency_conflict" || value.includes("idempotency_conflict"));
}

function safeParseIntent(raw, userId, taskId) {
  if (raw === null) return { exists: false, intent: null };
  let value;
  try { value = JSON.parse(raw); } catch { return { exists: true, intent: null, invalid: true }; }
  const expectedKey = postponeIntentStorageKey(userId, taskId);
  const valid = value && typeof value === "object" && !Array.isArray(value)
    && value.version === POSTPONE_INTENT_VERSION
    && value.user_id === userId
    && String(value.task_id) === String(taskId)
    && UUID_V4_PATTERN.test(value.idempotency_key || "")
    && value.days === 1
    && value.method === "POST"
    && value.body === JSON.stringify({ days: 1 })
    && value.fingerprint === makeFingerprint(taskId, value.body)
    && typeof value.created_at === "string" && Number.isFinite(Date.parse(value.created_at))
    && ["sending", "uncertain", "key_error", "conflict"].includes(value.status)
    && expectedKey !== null;
  if (!valid) return { exists: true, intent: null, invalid: true };
  const intent = { ...value };
  // A page reload can never infer that an in-flight request did not reach the server.
  const wasSending = intent.status === "sending";
  if (wasSending) intent.status = "uncertain";
  return { exists: true, intent, invalid: false, wasSending };
}

export function createTaskPostponeAction({
  requestJson,
  refreshTaskList = async () => {},
  onTaskUpdated = async () => {},
  onTaskObserved = async () => {},
  storage,
  getUserId,
  createId = makeUuidV4,
  now = () => new Date(),
} = {}) {
  const inFlight = new Map();
  const listeners = new Set();
  const storageProvider = () => currentStorage(storage);
  const userIdProvider = () => {
    let userId;
    try { userId = typeof getUserId === "function" ? getUserId() : getCurrentFocusUserId(storageProvider()); }
    catch { userId = null; }
    return validUserId(userId) ? userId : null;
  };

  function publish(event) {
    if (userIdProvider() !== event.user_id) return;
    for (const listener of [...listeners]) {
      try { listener(event); } catch { /* A view listener must not affect request settlement. */ }
    }
  }

  function readIntent(userId, taskId) {
    const key = postponeIntentStorageKey(userId, taskId);
    const store = storageProvider();
    if (!key || !store) return { exists: false, intent: null, unavailable: true };
    try {
      const result = safeParseIntent(store.getItem(key), userId, taskId);
      if (result.wasSending && inFlight.has(`${userId}:${taskId}`)) result.intent.status = "sending";
      return result;
    }
    catch { return { exists: false, intent: null, unavailable: true }; }
  }

  function writeIntent(intent) {
    if (userIdProvider() !== intent.user_id) return false;
    const key = postponeIntentStorageKey(intent.user_id, intent.task_id);
    const store = storageProvider();
    if (!key || !store) return false;
    try {
      const existing = readIntent(intent.user_id, intent.task_id);
      if (existing.exists && existing.intent && existing.intent.idempotency_key !== intent.idempotency_key) return false;
      store.setItem(key, JSON.stringify(intent));
      return true;
    } catch { return false; }
  }

  function removeIntent(intent) {
    if (userIdProvider() !== intent.user_id) return false;
    const key = postponeIntentStorageKey(intent.user_id, intent.task_id);
    const store = storageProvider();
    if (!key || !store) return false;
    try {
      const existing = safeParseIntent(store.getItem(key), intent.user_id, intent.task_id);
      if (!existing.intent || existing.intent.idempotency_key !== intent.idempotency_key) return false;
      store.removeItem(key);
      return true;
    } catch { return false; }
  }

  function getPendingIntent(taskId) {
    const userId = userIdProvider();
    if (!userId || !validTaskId(taskId)) return null;
    const result = readIntent(userId, taskId);
    if (result.invalid) {
      return {
        version: POSTPONE_INTENT_VERSION,
        user_id: userId,
        task_id: taskId,
        status: "conflict",
        invalid: true,
        last_error_message: "发现无法验证的本地顺延记录；为避免重复操作，已锁定普通顺延入口。请检查此浏览器的该账号顺延记录。",
      };
    }
    return result.intent;
  }

  async function observeCurrentTask(task, intent) {
    const taskId = String(intent.task_id);
    if (userIdProvider() !== intent.user_id) return null;
    let currentTask = null;
    try {
      const payload = await requestJson(`/api/tasks/${encodeURIComponent(taskId)}/with-subtasks`);
      currentTask = payload?.task || null;
    } catch { /* A GET cannot establish whether the write or event settled. */ }
    if (currentTask && userIdProvider() === intent.user_id) {
      try { await onTaskObserved(currentTask); } catch { /* View refreshes do not alter intent settlement. */ }
      try { await refreshTaskList(); } catch { /* A GET failure must never trigger another POST. */ }
    }
    return currentTask;
  }

  async function execute(intent, task, { retry = false } = {}) {
    const lockKey = `${intent.user_id}:${intent.task_id}`;
    if (inFlight.has(lockKey)) {
      return { postponePending: true, intent: getPendingIntent(intent.task_id) || intent };
    }
    inFlight.set(lockKey, true);
    try {
      if (retry) {
        intent = { ...intent, status: "sending", last_error_message: "" };
        if (!writeIntent(intent)) {
          const error = new Error("无法保存原顺延意图；未发送请求。");
          throw withErrorProperties(error, { postponeStorageError: true, pendingIntent: getPendingIntent(intent.task_id) || intent });
        }
        publish({ type: "intent", user_id: intent.user_id, task_id: intent.task_id, intent: { ...intent } });
      }

      try {
        await requestJson(`/api/tasks/${encodeURIComponent(String(intent.task_id))}/postpone`, {
          method: "POST",
          headers: { "Idempotency-Key": intent.idempotency_key },
          body: intent.body,
        });
      } catch (error) {
        if (error?.status === 409 && responseCode(error)) {
          const conflict = {
            ...intent,
            status: "conflict",
            last_error_message: "检测到 409 idempotency_conflict：本地请求与服务器记录不一致。已保留原 UUID 与 days=1 请求；不会自动换 key 或改请求。",
          };
          writeIntent(conflict);
          publish({ type: "intent", user_id: intent.user_id, task_id: intent.task_id, intent: { ...conflict }, error: conflict.last_error_message });
          throw withErrorProperties(new Error(conflict.last_error_message), {
            status: 409,
            payload: error?.payload,
            idempotencyConflict: true,
            pendingIntent: conflict,
          });
        }
        if (error?.status === 404 || error?.status === 409) {
          removeIntent(intent);
          publish({ type: "cleared", outcome: "definite_failure", user_id: intent.user_id, task_id: intent.task_id, error });
          throw error;
        }

        const keyError = Number(error?.status) === 400;
        const message = keyError
          ? `顺延请求被 HTTP 400 拒绝：${error?.message || "幂等键或固定请求体无效"}。已保留原 UUID 与原请求，不会自动生成新 key。`
          : "顺延请求结果不确定。GET 只能显示当前截止日，不能证明本次请求或 updated event 已结算；已保留原 UUID 与原请求。请显式使用原请求再试。";
        const uncertain = {
          ...intent,
          status: keyError ? "key_error" : "uncertain",
          last_error_message: message,
        };
        writeIntent(uncertain);
        publish({ type: "intent", user_id: intent.user_id, task_id: intent.task_id, intent: { ...uncertain }, error: message });
        const reconciledTask = keyError ? null : await observeCurrentTask(task, intent);
        const enriched = withErrorProperties(new Error(message), {
          status: error?.status,
          payload: error?.payload,
          postponeUncertain: !keyError,
          postponeKeyError: keyError,
          reconciledTask,
          pendingIntent: uncertain,
          cause: error,
        });
        throw enriched;
      }

      // A 2xx response is definitive; GET only supplies the current display data.
      let updatedTask = null;
      let warning = "";
      if (userIdProvider() === intent.user_id) {
        try {
          const payload = await requestJson(`/api/tasks/${encodeURIComponent(String(intent.task_id))}/with-subtasks`);
          updatedTask = payload?.task || null;
          if (!updatedTask) warning = "顺延已由服务端确认，但暂未读取到任务详情；请稍后刷新任务列表。";
        } catch {
          warning = "顺延已由服务端确认，但读取最新截止日失败；请稍后刷新任务列表。";
        }
      } else {
        warning = "顺延已由原认证账号确认；当前账号已切换，未读取或清理原账号的本地状态。";
      }

      const cleared = removeIntent(intent);
      if (userIdProvider() === intent.user_id) {
        if (updatedTask) {
          try { await onTaskUpdated(updatedTask); } catch { /* Keep the confirmed result even if a view cannot refresh. */ }
        }
        let refreshWarning = "";
        try { await refreshTaskList(); }
        catch { refreshWarning = "任务列表刷新失败，请稍后手动刷新。"; }
        warning = [warning, refreshWarning].filter(Boolean).join(" ");
        publish({ type: "cleared", outcome: "success", user_id: intent.user_id, task_id: intent.task_id, task: updatedTask, cleared });
      }
      return updatedTask
        ? (warning ? { ...updatedTask, postponeRefreshWarning: warning, postponeConfirmed: true } : { ...updatedTask, postponeConfirmed: true })
        : { postponeConfirmed: true, postponeRefreshWarning: warning || "顺延已确认，但最新截止日暂不可用。" };
    } finally {
      inFlight.delete(lockKey);
    }
  }

  function postponeTask(task) {
    const taskId = task?.id;
    if (!validTaskId(taskId)) return Promise.reject(new TypeError("无效的任务 ID，无法顺延"));
    const userId = userIdProvider();
    if (!userId) return Promise.reject(new Error("无法确认当前认证用户，未创建或发送顺延请求。"));
    const existing = readIntent(userId, taskId);
    if (existing.invalid) {
      return Promise.resolve({ postponePending: true, intent: getPendingIntent(taskId) });
    }
    if (existing.intent) return Promise.resolve({ postponePending: true, intent: existing.intent });

    const body = JSON.stringify({ days: 1 });
    const idempotencyKey = createId();
    if (!UUID_V4_PATTERN.test(idempotencyKey || "")) return Promise.reject(new Error("无法安全生成 UUIDv4 顺延幂等键，未发送请求。"));
    const intent = {
      version: POSTPONE_INTENT_VERSION,
      user_id: userId,
      task_id: taskId,
      idempotency_key: idempotencyKey,
      method: "POST",
      days: 1,
      body,
      fingerprint: makeFingerprint(taskId, body),
      created_at: now().toISOString(),
      status: "sending",
      last_error_message: "",
    };
    if (!writeIntent(intent)) return Promise.reject(new Error("无法先持久化顺延意图；为避免产生无法恢复的请求，尚未发送。"));
    publish({ type: "intent", user_id: userId, task_id: taskId, intent: { ...intent } });
    return execute(intent, task);
  }

  postponeTask.retry = function retryPendingPostpone(task) {
    const taskId = task?.id;
    if (!validTaskId(taskId)) return Promise.reject(new TypeError("无效的任务 ID，无法重试顺延"));
    const userId = userIdProvider();
    if (!userId) return Promise.reject(new Error("无法确认当前认证用户，未重试顺延请求。"));
    const existing = readIntent(userId, taskId);
    if (existing.invalid || !existing.intent) {
      return Promise.reject(new Error("没有可验证的原顺延意图；不会生成新 UUID。"));
    }
    const intent = existing.intent;
    if (inFlight.has(`${userId}:${taskId}`)) return Promise.resolve({ postponePending: true, intent });
    return execute(intent, task, { retry: true });
  };

  postponeTask.getPendingIntent = getPendingIntent;
  postponeTask.subscribe = (listener) => {
    if (typeof listener !== "function") return () => {};
    listeners.add(listener);
    return () => listeners.delete(listener);
  };
  postponeTask.getCurrentUserId = userIdProvider;
  return postponeTask;
}
