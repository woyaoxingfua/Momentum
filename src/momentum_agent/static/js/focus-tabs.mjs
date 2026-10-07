export const FOCUS_START_LOCK_PREFIX = "momentum_focus_start_lock_v1:";
export const FOCUS_TAB_OWNER_KEY_PREFIX = "momentum_focus_tab_owner_v1:";
export const FOCUS_TAB_OWNER_VERSION = 1;
export const FOCUS_TAB_OWNER_LEASE_MS = 10_000;

function validUserId(value) {
  return typeof value === "string" && value.trim().length > 0 && value.length <= 256;
}

function validSessionId(value) {
  return typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value);
}

function makeTabId() {
  try {
    if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
  } catch {
    // Use the non-cryptographic fallback only as an in-tab coordination label.
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export function focusStartLockName(userId) {
  if (!validUserId(userId)) return null;
  return `${FOCUS_START_LOCK_PREFIX}${encodeURIComponent(userId)}`;
}

export function focusTabOwnerKey(userId) {
  if (!validUserId(userId)) return null;
  return `${FOCUS_TAB_OWNER_KEY_PREFIX}${encodeURIComponent(userId)}`;
}

function normalizeOwner(value, userId, sessionId) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (value.version !== FOCUS_TAB_OWNER_VERSION
    || value.user_id !== userId
    || value.session_id !== sessionId
    || typeof value.tab_id !== "string"
    || value.tab_id.length < 1
    || value.tab_id.length > 128
    || !Number.isSafeInteger(value.expires_at)) return null;
  return {
    version: FOCUS_TAB_OWNER_VERSION,
    user_id: userId,
    session_id: sessionId,
    tab_id: value.tab_id,
    expires_at: value.expires_at,
  };
}

export class FocusTabCoordinator {
  constructor(userId, {
    storage,
    lockManager,
    now = () => Date.now(),
    tabId = makeTabId(),
  } = {}) {
    this.userId = validUserId(userId) ? userId : null;
    this.now = now;
    this.tabId = String(tabId);
    this.lockManager = lockManager ?? globalThis.navigator?.locks ?? null;
    try {
      this.storage = storage ?? globalThis.localStorage;
    } catch {
      this.storage = null;
    }
    this.ownerKey = focusTabOwnerKey(this.userId);
  }

  withStartLock(callback) {
    const name = focusStartLockName(this.userId);
    if (!name || typeof this.lockManager?.request !== "function") {
      return Promise.reject(new Error("当前浏览器不支持同源多标签启动互斥，未发送启动请求"));
    }
    return this.lockManager.request(name, { mode: "exclusive" }, callback);
  }

  readOwner(sessionId) {
    if (!this.ownerKey || !this.storage || !validSessionId(sessionId)) return null;
    try {
      const raw = this.storage.getItem(this.ownerKey);
      if (raw === null) return null;
      return normalizeOwner(JSON.parse(raw), this.userId, sessionId);
    } catch {
      return null;
    }
  }

  hasLiveOwner(sessionId) {
    const owner = this.readOwner(sessionId);
    return Boolean(owner && owner.expires_at > this.now());
  }

  isOwnedByAnotherTab(sessionId) {
    const owner = this.readOwner(sessionId);
    return Boolean(owner && owner.expires_at > this.now() && owner.tab_id !== this.tabId);
  }

  refreshOwner(sessionId, { force = false } = {}) {
    if (!this.ownerKey || !this.storage || !this.userId || !validSessionId(sessionId)) return false;
    try {
      const existing = this.readOwner(sessionId);
      if (!force && existing?.tab_id === this.tabId
        && existing.expires_at - this.now() > FOCUS_TAB_OWNER_LEASE_MS / 2) return true;
      const owner = {
        version: FOCUS_TAB_OWNER_VERSION,
        user_id: this.userId,
        session_id: sessionId,
        tab_id: this.tabId,
        expires_at: this.now() + FOCUS_TAB_OWNER_LEASE_MS,
      };
      this.storage.setItem(this.ownerKey, JSON.stringify(owner));
      return true;
    } catch {
      return false;
    }
  }

  releaseOwner(sessionId) {
    if (!this.ownerKey || !this.storage || !validSessionId(sessionId)) return false;
    try {
      const owner = this.readOwner(sessionId);
      if (!owner || owner.tab_id !== this.tabId) return false;
      if (this.storage.getItem(this.ownerKey) === JSON.stringify(owner)) {
        this.storage.removeItem(this.ownerKey);
        return true;
      }
      return false;
    } catch {
      return false;
    }
  }
}
