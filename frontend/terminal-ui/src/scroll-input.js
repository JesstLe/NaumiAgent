export const TRACKPAD_SCROLL_INTERVAL_MS = 48;
export const TRACKPAD_SCROLL_IDLE_RESET_MS = 24;

export function createTrackpadScrollController({
  intervalMs = TRACKPAD_SCROLL_INTERVAL_MS,
  idleResetMs = TRACKPAD_SCROLL_IDLE_RESET_MS,
  now = () => performance.now(),
  schedule = (callback, delayMs) => setTimeout(callback, delayMs),
  cancel = (timer) => clearTimeout(timer),
  onStep = () => {},
} = {}) {
  const requestedInterval = Number(intervalMs);
  const safeInterval = Number.isFinite(requestedInterval)
    ? Math.max(0, Math.min(1_000, requestedInterval))
    : TRACKPAD_SCROLL_INTERVAL_MS;
  const requestedIdleReset = Number(idleResetMs);
  const safeIdleReset = Number.isFinite(requestedIdleReset)
    ? Math.max(0, Math.min(safeInterval, requestedIdleReset))
    : Math.min(safeInterval, TRACKPAD_SCROLL_IDLE_RESET_MS);
  let lastDirection = null;
  let lastEmittedAt = Number.NEGATIVE_INFINITY;
  let lastInputAt = Number.NEGATIVE_INFINITY;
  let pendingDirection = null;
  let pendingTimer = null;
  let disposed = false;

  function clearPending() {
    pendingDirection = null;
    if (pendingTimer === null) return;
    cancel(pendingTimer);
    pendingTimer = null;
  }

  function emit(direction, timestamp) {
    lastDirection = direction;
    lastEmittedAt = timestamp;
    onStep(direction);
  }

  function schedulePending(delayMs) {
    pendingTimer = schedule(() => {
      pendingTimer = null;
      if (disposed || pendingDirection === null) return;
      const direction = pendingDirection;
      pendingDirection = null;
      const timestamp = Number(now());
      if (
        !Number.isFinite(timestamp)
        || timestamp < lastInputAt
        || timestamp - lastInputAt > safeIdleReset
      ) {
        return;
      }
      emit(
        direction,
        Math.max(timestamp, lastEmittedAt + safeInterval),
      );
    }, Math.max(0, delayMs));
  }

  return {
    push(direction) {
      if (disposed || (direction !== "up" && direction !== "down")) {
        return false;
      }
      const timestamp = Number(now());
      if (!Number.isFinite(timestamp) || timestamp < lastEmittedAt) {
        clearPending();
        return false;
      }
      lastInputAt = timestamp;

      if (
        direction !== lastDirection
        || safeInterval === 0
        || timestamp - lastEmittedAt >= safeInterval
      ) {
        clearPending();
        emit(direction, timestamp);
        return true;
      }

      pendingDirection = direction;
      if (pendingTimer === null) {
        schedulePending(safeInterval - (timestamp - lastEmittedAt));
      }
      return true;
    },

    dispose() {
      if (disposed) return;
      disposed = true;
      clearPending();
    },
  };
}
