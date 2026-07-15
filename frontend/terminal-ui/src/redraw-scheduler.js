export function createRedrawScheduler({
  onRedraw,
  setTimer = globalThis.setTimeout,
  clearTimer = globalThis.clearTimeout,
  frameDelayMs = 16,
  initialSettleMs = 32,
} = {}) {
  if (typeof onRedraw !== "function") {
    throw new TypeError("重绘调度器需要 onRedraw 回调");
  }
  if (typeof setTimer !== "function" || typeof clearTimer !== "function") {
    throw new TypeError("重绘调度器需要有效的计时器函数");
  }

  const frameDelay = normalizeDelay(frameDelayMs, 16);
  const initialDelay = normalizeDelay(initialSettleMs, 32);
  let timer = null;
  let painted = false;

  const arm = (delay, restart) => {
    if (timer !== null) {
      if (!restart) return false;
      clearTimer(timer);
      timer = null;
    }
    timer = setTimer(() => {
      timer = null;
      onRedraw();
    }, delay);
    timer?.unref?.();
    return true;
  };

  return {
    get pending() {
      return timer !== null;
    },
    get painted() {
      return painted;
    },
    schedule() {
      return arm(painted ? frameDelay : initialDelay, false);
    },
    settleInitial() {
      return painted ? arm(frameDelay, false) : arm(initialDelay, true);
    },
    markPainted() {
      painted = true;
    },
    cancel() {
      if (timer === null) return false;
      clearTimer(timer);
      timer = null;
      return true;
    },
  };
}

function normalizeDelay(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.trunc(number)) : fallback;
}
