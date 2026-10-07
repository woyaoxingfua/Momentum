const defaultMonotonicNow = () => globalThis.performance?.now?.() ?? Date.now();

/** Tracks active elapsed time without counting pauses or wall-clock adjustments. */
export class FocusClock {
  constructor(now = defaultMonotonicNow) {
    if (typeof now !== "function") throw new TypeError("now must be a function");
    this._now = now;
    this.reset();
  }

  start(initialElapsedSeconds = 0, maxSampleGapMs = Number.POSITIVE_INFINITY) {
    const elapsedSeconds = Math.max(0, Math.floor(Number(initialElapsedSeconds) || 0));
    const gapLimit = Number(maxSampleGapMs);
    if (!(gapLimit > 0)) throw new TypeError("maxSampleGapMs must be positive");
    this._elapsedBeforeRunMs = elapsedSeconds * 1000;
    this._runningSinceMs = this._readNow();
    this._lastSampleMs = this._runningSinceMs;
    this._maxSampleGapMs = gapLimit;
  }

  pause() {
    if (this._runningSinceMs === null) return;
    this._sampleRunningTime();
    this._runningSinceMs = null;
  }

  resume() {
    if (this._runningSinceMs !== null) return;
    this._runningSinceMs = this._readNow();
    this._lastSampleMs = this._runningSinceMs;
  }

  elapsedMilliseconds() {
    this._sampleRunningTime();
    return this._elapsedBeforeRunMs;
  }

  elapsedSeconds() {
    return Math.floor(this.elapsedMilliseconds() / 1000);
  }

  finish(plannedSeconds) {
    const planned = Math.max(0, Math.floor(Number(plannedSeconds) || 0));
    const elapsed = this.elapsedSeconds();
    this.pause();
    return {
      actual_seconds: Math.min(planned, elapsed),
      outcome: elapsed >= planned ? "completed" : "stopped",
    };
  }

  reset() {
    this._elapsedBeforeRunMs = 0;
    this._runningSinceMs = null;
  }

  _readNow() {
    const value = Number(this._now());
    if (!Number.isFinite(value)) throw new TypeError("clock returned a non-finite value");
    return value;
  }

  _sampleRunningTime() {
    if (this._runningSinceMs === null) return;
    const now = this._readNow();
    const elapsed = Math.max(0, now - this._lastSampleMs);
    if (elapsed <= this._maxSampleGapMs) this._elapsedBeforeRunMs += elapsed;
    this._lastSampleMs = now;
  }
}

export function remainingFocusSeconds(plannedSeconds, elapsedSeconds) {
  return Math.max(0, Math.floor(plannedSeconds) - Math.floor(elapsedSeconds));
}
