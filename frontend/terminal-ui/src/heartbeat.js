const DEFAULT_INTERVAL_MS = 5_000;
const DEFAULT_TIMEOUT_MS = 15_000;

export function heartbeatTimingFromEnv(env = {}) {
  const intervalMs = environmentDuration(
    env.NAUMI_HEARTBEAT_INTERVAL_MS,
    DEFAULT_INTERVAL_MS,
  );
  const configuredTimeout = environmentDuration(
    env.NAUMI_HEARTBEAT_TIMEOUT_MS,
    DEFAULT_TIMEOUT_MS,
  );
  return {
    intervalMs,
    timeoutMs: Math.max(configuredTimeout, intervalMs * 3),
  };
}

export function createHeartbeatController({
  sendPing,
  onHealth = () => {},
  onDebug = () => {},
  now = () => globalThis.performance.now(),
  setTimer = globalThis.setInterval,
  clearTimer = globalThis.clearInterval,
  intervalMs = DEFAULT_INTERVAL_MS,
  timeoutMs = DEFAULT_TIMEOUT_MS,
} = {}) {
  if (typeof sendPing !== "function") throw new TypeError("sendPing 必须是函数");
  const interval = positiveDuration(intervalMs, "intervalMs");
  const timeout = positiveDuration(timeoutMs, "timeoutMs");
  if (timeout < interval) throw new RangeError("timeoutMs 不能小于 intervalMs");

  let running = false;
  let timer = null;
  let pending = null;
  let nextSequence = 1;

  function probe() {
    if (!running) return false;
    const currentTime = Number(now());
    if (pending) {
      const ageMs = Math.max(0, currentTime - pending.sentAt);
      if (ageMs < timeout) return false;
      onDebug("heartbeat.timeout", { requestId: pending.id, ageMs });
      onHealth({ status: "stale", rttMs: null, ageMs });
      pending = null;
    }

    const id = `heartbeat-${nextSequence++}`;
    try {
      sendPing(id);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      onDebug("heartbeat.send_error", { requestId: id, error: message });
      onHealth({ status: "stale", rttMs: null, ageMs: 0 });
      return false;
    }
    pending = { id, sentAt: currentTime };
    onDebug("heartbeat.sent", { requestId: id });
    return true;
  }

  return {
    start() {
      if (running) return false;
      running = true;
      timer = setTimer(probe, interval);
      onDebug("heartbeat.start", { intervalMs: interval, timeoutMs: timeout });
      probe();
      return true;
    },

    stop() {
      if (!running) return false;
      running = false;
      if (timer != null) clearTimer(timer);
      timer = null;
      pending = null;
      onDebug("heartbeat.stop", {});
      return true;
    },

    receivePong(requestId) {
      const normalizedId = String(requestId ?? "");
      if (!running || !pending || normalizedId !== pending.id) {
        onDebug("heartbeat.pong_ignored", { requestId: normalizedId });
        return false;
      }
      const rttMs = Math.max(0, Number(now()) - pending.sentAt);
      pending = null;
      onDebug("heartbeat.pong", { requestId: normalizedId, rttMs });
      onHealth({ status: "healthy", rttMs, ageMs: 0 });
      return true;
    },

    probe,
  };
}

function positiveDuration(value, name) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new RangeError(`${name} 必须是正数`);
  }
  return parsed;
}

function environmentDuration(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}
