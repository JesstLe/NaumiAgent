export const GRACEFUL_SHUTDOWN_TIMEOUT_MS = 1_200;

export function shutdownSignalsForPlatform(platform = process.platform) {
  return platform === "win32"
    ? Object.freeze(["SIGINT", "SIGTERM", "SIGBREAK"])
    : Object.freeze(["SIGINT", "SIGTERM", "SIGHUP"]);
}

export function createGracefulShutdownController({
  sendShutdown,
  onRequest = () => {},
  onLifecycle = () => {},
  cleanup = () => {},
  exitProcess = (code) => process.exit(code),
  setTimer = setTimeout,
  clearTimer = clearTimeout,
  timeoutMs = GRACEFUL_SHUTDOWN_TIMEOUT_MS,
} = {}) {
  if (typeof sendShutdown !== "function") {
    throw new TypeError("graceful shutdown 需要 sendShutdown。");
  }
  for (const [name, callback] of Object.entries({
    onRequest,
    onLifecycle,
    cleanup,
    exitProcess,
    setTimer,
    clearTimer,
  })) {
    if (typeof callback !== "function") {
      throw new TypeError(`graceful shutdown ${name} 必须是函数。`);
    }
  }

  const boundedTimeoutMs = Math.min(
    10_000,
    Math.max(50, Math.trunc(Number(timeoutMs) || GRACEFUL_SHUTDOWN_TIMEOUT_MS)),
  );
  let phase = "idle";
  let source = "";
  let requestId = "";
  let requestedExitCode = 0;
  let timer = null;

  function request({ reason = "user", exitCode = 0 } = {}) {
    if (phase !== "idle") return false;
    phase = "requested";
    source = normalizeReason(reason);
    requestedExitCode = normalizeExitCode(exitCode);
    safeCall(onRequest, { source, timeoutMs: boundedTimeoutMs });
    safeCall(onLifecycle, "requested", {
      source,
      timeoutMs: boundedTimeoutMs,
    });
    try {
      requestId = normalizeIdentity(sendShutdown());
    } catch (error) {
      safeCall(onLifecycle, "send_failed", {
        source,
        error: safeError(error),
      });
      finalize("send_failed", requestedExitCode);
      return true;
    }
    safeCall(onLifecycle, "sent", {
      source,
      requestId,
      timeoutMs: boundedTimeoutMs,
    });
    try {
      timer = setTimer(() => {
        timer = null;
        finalize("timeout", requestedExitCode);
      }, boundedTimeoutMs);
    } catch (error) {
      safeCall(onLifecycle, "timer_failed", {
        source,
        requestId,
        error: safeError(error),
      });
      finalize("timer_failed", requestedExitCode);
      return true;
    }
    timer?.unref?.();
    return true;
  }

  function acknowledge({ responseRequestId = "", ok = true } = {}) {
    const responseId = normalizeIdentity(responseRequestId);
    if (phase === "idle") {
      phase = "requested";
      source = "bridge";
      safeCall(onRequest, { source, timeoutMs: 0 });
      safeCall(onLifecycle, "remote_requested", {
        source,
        responseRequestId: responseId,
      });
      finalize(ok === false ? "remote_ack_failed" : "remote_ack", ok === false ? 1 : 0);
      return true;
    }
    if (phase !== "requested") return false;
    if (requestId && requestId !== responseId) {
      safeCall(onLifecycle, "ack_mismatch", {
        source,
        requestId,
        responseRequestId: responseId,
      });
      return false;
    }
    finalize(ok === false ? "ack_failed" : "ack", ok === false ? 1 : requestedExitCode);
    return true;
  }

  function bridgeExited(details = {}) {
    if (phase !== "requested") return false;
    safeCall(onLifecycle, "bridge_exited", {
      source,
      requestId,
      code: details.code ?? null,
      signal: details.signal ?? null,
    });
    const bridgeFailed = details.code !== 0 || Boolean(details.signal);
    finalize(
      bridgeFailed ? "bridge_exit_failed" : "bridge_exit",
      bridgeFailed ? 1 : requestedExitCode,
    );
    return true;
  }

  function force(reason = "forced", exitCode = 0) {
    if (phase === "finalized") return false;
    if (phase === "idle") {
      phase = "requested";
      source = normalizeReason(reason);
      safeCall(onRequest, { source, timeoutMs: 0 });
    }
    finalize(normalizeReason(reason), exitCode);
    return true;
  }

  function finalize(outcome, exitCode) {
    if (phase === "finalized") return false;
    phase = "finalized";
    if (timer !== null) {
      safeCall(clearTimer, timer);
      timer = null;
    }
    const normalizedCode = normalizeExitCode(exitCode);
    safeCall(onLifecycle, "finalized", {
      source,
      requestId,
      outcome,
      exitCode: normalizedCode,
    });
    safeCall(cleanup, {
      source,
      requestId,
      outcome,
      exitCode: normalizedCode,
    });
    exitProcess(normalizedCode);
    return true;
  }

  return Object.freeze({
    request,
    acknowledge,
    bridgeExited,
    force,
    snapshot() {
      return Object.freeze({
        phase,
        source,
        requestId,
        timeoutMs: boundedTimeoutMs,
      });
    },
  });
}

function safeCall(callback, ...args) {
  try {
    return callback(...args);
  } catch {
    return undefined;
  }
}

function normalizeIdentity(value) {
  return String(value ?? "").trim().slice(0, 128);
}

function normalizeReason(value) {
  return String(value ?? "").trim().slice(0, 64) || "unknown";
}

function normalizeExitCode(value) {
  const code = Number(value);
  return Number.isSafeInteger(code) && code >= 0 && code <= 255 ? code : 1;
}

function safeError(error) {
  return String(error instanceof Error ? error.message : error ?? "unknown")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 300);
}
