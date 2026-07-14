export function createWorkingAnimationController({
  onFrame,
  setTimer = globalThis.setInterval,
  clearTimer = globalThis.clearInterval,
  intervalMs = 120,
  frameCount = 4,
}) {
  if (typeof onFrame !== "function") {
    throw new TypeError("working animation requires an onFrame callback");
  }
  const safeInterval = Math.max(40, Number(intervalMs) || 120);
  const safeFrameCount = Math.max(1, Math.trunc(Number(frameCount) || 1));
  let timer = null;
  let frame = 0;

  const stop = () => {
    if (timer !== null) {
      clearTimer(timer);
      timer = null;
    }
    if (frame !== 0) {
      frame = 0;
      onFrame(frame);
    }
  };

  return {
    get active() {
      return timer !== null;
    },
    sync(active) {
      if (!active) {
        stop();
        return;
      }
      if (timer !== null) return;
      timer = setTimer(() => {
        frame = (frame + 1) % safeFrameCount;
        onFrame(frame);
      }, safeInterval);
      timer?.unref?.();
    },
    stop,
  };
}
