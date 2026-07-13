export const TRACKPAD_SCROLL_INTERVAL_MS = 32;

export function createTrackpadScrollFilter({
  intervalMs = TRACKPAD_SCROLL_INTERVAL_MS,
  now = () => performance.now(),
} = {}) {
  const safeInterval = Math.max(0, Number(intervalMs) || 0);
  let lastDirection = null;
  let lastAcceptedAt = Number.NEGATIVE_INFINITY;

  return {
    accept(direction) {
      if (direction !== "up" && direction !== "down") return false;
      const timestamp = Number(now());
      if (!Number.isFinite(timestamp)) return false;
      if (direction === lastDirection && timestamp - lastAcceptedAt < safeInterval) {
        return false;
      }
      lastDirection = direction;
      lastAcceptedAt = timestamp;
      return true;
    },
  };
}
