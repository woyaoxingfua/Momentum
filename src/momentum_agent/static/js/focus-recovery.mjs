export const FOCUS_SNAPSHOT_VERSION = 2;
export const FOCUS_SNAPSHOT_KEY_PREFIX = "momentum_focus_snapshot_v2:";
export const LEGACY_FOCUS_SNAPSHOT_KEY_PREFIX = "momentum_focus_snapshot_v1:";
export const FOCUS_SETTLED_KEY_PREFIX = "momentum_focus_settled_v1:";

const LEGACY_FOCUS_SNAPSHOT_VERSION = 1;
const FOCUS_SETTLEMENT_VERSION = 1;

function validUserId(value) {
  return typeof value === "string" && value.trim().length > 0 && value.length <= 256;
}

function validSessionId(value) {
  return typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value);
}

function validStartedAt(value) {
  return typeof value === "string" && value.length <= 100 && Number.isFinite(Date.parse(value));
}

function validFinishPayload(payload, snapshot) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
  const expectedKeys = ["actual_seconds", "ended_at", "outcome", "planned_minutes", "session_id", "started_at", "task_id"];
  if (Object.keys(payload).sort().join("|") !== expectedKeys.join("|")) return false;
  if (!validStartedAt(payload.ended_at)) return false;
  if (payload.task_id !== snapshot.task_id
    || payload.session_id !== snapshot.session_id
    || payload.started_at !== snapshot.started_at
    || payload.planned_minutes !== snapshot.planned_minutes) return false;
  if (!Number.isSafeInteger(payload.actual_seconds)
    || payload.actual_seconds < 0
    || payload.actual_seconds > snapshot.planned_minutes * 60) return false;
  if (payload.outcome === "completed") return payload.actual_seconds === snapshot.planned_minutes * 60;
  return payload.outcome === "stopped" && payload.actual_seconds < snapshot.planned_minutes * 60;
}

function normalizeFinishPayload(payload, snapshot) {
  if (!validFinishPayload(payload, snapshot)) return null;
  return {
    task_id: payload.task_id,
    session_id: payload.session_id,
    started_at: payload.started_at,
    planned_minutes: payload.planned_minutes,
    actual_seconds: payload.actual_seconds,
    outcome: payload.outcome,
    ended_at: payload.ended_at,
  };
}

function sameFinishPayload(left, right) {
  if (!left || !right) return false;
  const keys = ["task_id", "session_id", "started_at", "planned_minutes", "actual_seconds", "outcome", "ended_at"];
  return keys.every((key) => left[key] === right[key]);
}

export function normalizeFocusSnapshot(value, expectedUserId) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if ((value.version !== FOCUS_SNAPSHOT_VERSION && value.version !== LEGACY_FOCUS_SNAPSHOT_VERSION)
    || !validUserId(expectedUserId)
    || value.user_id !== expectedUserId) return null;
  if (!validSessionId(value.session_id)) return null;
  if (!Number.isSafeInteger(value.task_id) || value.task_id <= 0) return null;
  if (!Number.isSafeInteger(value.planned_minutes) || value.planned_minutes <= 0 || value.planned_minutes > 1440) return null;
  if (!Number.isSafeInteger(value.elapsed_seconds) || value.elapsed_seconds < 0 || value.elapsed_seconds > value.planned_minutes * 60) return null;
  if (value.state !== "running" && value.state !== "paused") return null;
  if (!validStartedAt(value.started_at)) return null;
  if (value.task_title !== undefined && (typeof value.task_title !== "string" || value.task_title.length > 1000)) return null;

  const snapshot = {
    version: FOCUS_SNAPSHOT_VERSION,
    user_id: expectedUserId,
    session_id: value.session_id,
    task_id: value.task_id,
    planned_minutes: value.planned_minutes,
    elapsed_seconds: value.elapsed_seconds,
    state: value.state,
    started_at: value.started_at,
    task_title: value.task_title || "",
  };
  if (value.finish_payload !== undefined) {
    const payload = normalizeFinishPayload(value.finish_payload, snapshot);
    if (!payload) return null;
    snapshot.finish_payload = payload;
  }
  return snapshot;
}

export function focusSnapshotKey(userId) {
  if (!validUserId(userId)) return null;
  return `${FOCUS_SNAPSHOT_KEY_PREFIX}${encodeURIComponent(userId)}`;
}

function legacyFocusSnapshotKey(userId) {
  if (!validUserId(userId)) return null;
  return `${LEGACY_FOCUS_SNAPSHOT_KEY_PREFIX}${encodeURIComponent(userId)}`;
}

export function focusSettledKey(userId, sessionId) {
  if (!validUserId(userId) || !validSessionId(sessionId)) return null;
  return `${FOCUS_SETTLED_KEY_PREFIX}${encodeURIComponent(userId)}:${encodeURIComponent(sessionId)}`;
}

export function focusRecoveryMode(snapshot) {
  return snapshot?.finish_payload ? "retry-only" : snapshot ? "choose" : "none";
}

export function getCurrentFocusUserId(storage) {
  try {
    const userId = (storage ?? globalThis.localStorage)?.getItem("momentum_user");
    return validUserId(userId) ? userId : null;
  } catch {
    return null;
  }
}

function parseSnapshot(raw, userId) {
  if (raw === null) return null;
  try {
    return normalizeFocusSnapshot(JSON.parse(raw), userId);
  } catch {
    return null;
  }
}

function normalizeSettlement(value, userId, sessionId) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (value.version !== FOCUS_SETTLEMENT_VERSION || value.user_id !== userId || value.session_id !== sessionId) return null;
  if (!Number.isSafeInteger(value.task_id) || value.task_id <= 0) return null;
  if (!Number.isSafeInteger(value.planned_minutes) || value.planned_minutes <= 0 || value.planned_minutes > 1440) return null;
  if (!validStartedAt(value.started_at)) return null;
  const base = {
    task_id: value.task_id,
    session_id: sessionId,
    started_at: value.started_at,
    planned_minutes: value.planned_minutes,
  };
  const finishPayload = normalizeFinishPayload(value.finish_payload, base);
  if (!finishPayload) return null;
  return {
    version: FOCUS_SETTLEMENT_VERSION,
    user_id: userId,
    session_id: sessionId,
    task_id: value.task_id,
    planned_minutes: value.planned_minutes,
    started_at: value.started_at,
    finish_payload: finishPayload,
  };
}

export class FocusRecoveryStore {
  constructor(userId, storage) {
    this.userId = validUserId(userId) ? userId : null;
    try {
      this.storage = storage ?? globalThis.localStorage;
    } catch {
      this.storage = null;
    }
    this.key = focusSnapshotKey(this.userId);
    this.legacyKey = legacyFocusSnapshotKey(this.userId);
  }

  isSettled(snapshot) {
    return Boolean(this.getSettlement(snapshot?.session_id));
  }

  getSettlement(sessionId) {
    if (!this.userId || !this.storage || !validSessionId(sessionId)) return null;
    try {
      const key = focusSettledKey(this.userId, sessionId);
      const raw = key ? this.storage.getItem(key) : null;
      if (raw === null) return null;
      return normalizeSettlement(JSON.parse(raw), this.userId, sessionId);
    } catch {
      return null;
    }
  }

  load() {
    if (!this.key || !this.storage) return null;
    try {
      const currentRaw = this.storage.getItem(this.key);
      if (currentRaw !== null) {
        const current = parseSnapshot(currentRaw, this.userId);
        if (!current) {
          if (this.storage.getItem(this.key) === currentRaw) this.storage.removeItem(this.key);
        } else if (this.isSettled(current)) {
          this.removeMatchingSnapshot(this.key, current);
          return null;
        } else {
          return current;
        }
      }

      if (!this.legacyKey) return null;
      const legacyRaw = this.storage.getItem(this.legacyKey);
      if (legacyRaw === null) return null;
      const legacy = parseSnapshot(legacyRaw, this.userId);
      if (!legacy) {
        if (this.storage.getItem(this.legacyKey) === legacyRaw) this.storage.removeItem(this.legacyKey);
        return null;
      }
      if (this.isSettled(legacy)) {
        this.removeMatchingSnapshot(this.legacyKey, legacy);
        return null;
      }

      const migrated = { ...legacy, version: FOCUS_SNAPSHOT_VERSION };
      if (!this.save(migrated)) return legacy;
      if (this.storage.getItem(this.legacyKey) === legacyRaw) this.storage.removeItem(this.legacyKey);
      return migrated;
    } catch {
      return null;
    }
  }

  save(value) {
    if (!this.key || !this.storage) return false;
    const snapshot = normalizeFocusSnapshot(value, this.userId);
    if (!snapshot) return false;
    try {
      this.storage.setItem(this.key, JSON.stringify(snapshot));
      return true;
    } catch {
      return false;
    }
  }

  removeMatchingSnapshot(key, expected) {
    if (!key || !this.storage || !expected?.session_id) return false;
    try {
      const raw = this.storage.getItem(key);
      const current = parseSnapshot(raw, this.userId);
      if (!current || current.session_id !== expected.session_id) return false;
      if (current.finish_payload && expected.finish_payload
        && !sameFinishPayload(current.finish_payload, expected.finish_payload)) return false;
      if (this.storage.getItem(key) !== raw) return false;
      this.storage.removeItem(key);
      return true;
    } catch {
      return false;
    }
  }

  settle(value) {
    if (!this.userId || !this.storage) return false;
    const snapshot = normalizeFocusSnapshot(value, this.userId);
    if (!snapshot?.finish_payload) return false;
    const key = focusSettledKey(this.userId, snapshot.session_id);
    if (!key) return false;
    const settlement = {
      version: FOCUS_SETTLEMENT_VERSION,
      user_id: this.userId,
      session_id: snapshot.session_id,
      task_id: snapshot.task_id,
      planned_minutes: snapshot.planned_minutes,
      started_at: snapshot.started_at,
      finish_payload: snapshot.finish_payload,
    };
    try {
      // The independent per-session marker survives stale page/SW writes to either snapshot key.
      this.storage.setItem(key, JSON.stringify(settlement));
    } catch {
      return false;
    }
    this.removeMatchingSnapshot(this.key, snapshot);
    this.removeMatchingSnapshot(this.legacyKey, snapshot);
    return true;
  }
}

export function clearFocusRecoverySnapshot(store, expectedSnapshot) {
  return store?.settle(expectedSnapshot) ?? false;
}

export function makeFocusFinishPayload(snapshot, elapsedSeconds = snapshot?.elapsed_seconds, endedAt = new Date().toISOString()) {
  const normalized = normalizeFocusSnapshot(snapshot, snapshot?.user_id);
  if (!normalized) throw new TypeError("invalid focus snapshot");
  if (normalized.finish_payload) return { ...normalized.finish_payload };
  if (!Number.isSafeInteger(elapsedSeconds) || elapsedSeconds < 0 || elapsedSeconds > normalized.planned_minutes * 60) {
    throw new TypeError("invalid focus elapsed seconds");
  }
  return {
    task_id: normalized.task_id,
    session_id: normalized.session_id,
    started_at: normalized.started_at,
    planned_minutes: normalized.planned_minutes,
    actual_seconds: elapsedSeconds,
    outcome: elapsedSeconds >= normalized.planned_minutes * 60 ? "completed" : "stopped",
    ended_at: endedAt,
  };
}

export class FocusFinishCoordinator {
  constructor(store, requestJson, now = () => new Date()) {
    this.store = store;
    this.requestJson = requestJson;
    this.now = now;
    this.lastPayload = null;
    this.savedResult = null;
    this.inFlight = null;
  }

  submit(snapshot, elapsedSeconds = snapshot?.elapsed_seconds) {
    if (this.savedResult) return Promise.resolve(this.savedResult);
    if (this.inFlight) return this.inFlight;
    const normalized = normalizeFocusSnapshot(snapshot, this.store?.userId);
    if (!normalized) return Promise.reject(new TypeError("invalid focus snapshot"));
    const payload = normalized.finish_payload
      ? { ...normalized.finish_payload }
      : makeFocusFinishPayload(normalized, elapsedSeconds, this.now().toISOString());
    const pendingSnapshot = {
      ...normalized,
      state: "paused",
      elapsed_seconds: payload.actual_seconds,
      finish_payload: payload,
    };
    if (!this.store?.save(pendingSnapshot)) {
      return Promise.reject(new Error("无法持久化固定的结束记录，尚未发送保存请求"));
    }
    this.lastPayload = { ...payload };
    this.inFlight = Promise.resolve()
      .then(() => this.requestJson("/api/focus/finish", {
        method: "POST",
        body: JSON.stringify(payload),
      }))
      .then((response) => {
        this.savedResult = {
          actual_seconds: payload.actual_seconds,
          outcome: payload.outcome,
          response,
          snapshotSettled: this.store.settle(pendingSnapshot),
        };
        return this.savedResult;
      })
      .finally(() => {
        this.inFlight = null;
      });
    return this.inFlight;
  }
}
