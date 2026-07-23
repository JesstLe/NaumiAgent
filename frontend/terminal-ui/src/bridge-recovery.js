const DEFAULT_MAX_ATTEMPTS = 3;
const DEFAULT_DELAYS_MS = Object.freeze([0, 250, 750]);
const DEFAULT_HANDSHAKE_TIMEOUT_MS = 8_000;
const TERMINAL_HARNESS_BATCH_STAGES = new Set([
  "completed",
  "partial",
  "error",
  "failed",
  "cancelled",
  "expired",
]);

export function assessIdleBridgeRecovery(state = {}) {
  if (
    state.running === true
    || state.cancelPending === true
    || state.activeRunActivity?.status === "running"
    || state.activeAssistant
    || state.activeThinking
    || state.activeToolPrepare
    || state.activeRuntimePhase
  ) {
    return blocked("active_run", "当前运行仍在执行，不能自动重启后端。");
  }
  if (state.permission) {
    return blocked("permission_pending", "当前权限请求尚未裁决，不能自动重启后端。");
  }
  if (state.interaction || (state.interactionQueue?.length ?? 0) > 0) {
    return blocked("interaction_pending", "当前交互请求尚未完成，不能自动重启后端。");
  }
  const pendingMessage = (state.messages ?? []).find(
    (message) => message?.kind === "user" && message.deliveryStatus === "queued",
  );
  if (pendingMessage) {
    return blocked("submission_unconfirmed", "存在尚未确认送达的消息，不能自动重放。");
  }
  if (hasPendingControlOperation(state)) {
    return blocked("control_operation_pending", "存在尚未结束的控制操作，不能自动重启后端。");
  }
  return {
    recoverable: true,
    reason: "idle",
    message: "Bridge 空闲，可安全重连。",
    sessionId: String(state.currentSessionId ?? "").trim(),
  };
}

function hasPendingControlOperation(state) {
  if (
    state.harnessEvalBatch?.cancelPending
    || state.harnessEvalBatch?.retryPending
    || state.agents?.actionPendingTaskId
    || state.workbench?.proposal_action?.phase === "loading"
    || state.doctorHealth?.exportLoading
    || (
      state.evolutionReview?.loading
      && state.evolutionReview?.request?.action === "enqueue"
    )
  ) return true;

  const batchRequestId = String(state.harnessEvalBatch?.requestId ?? "");
  const batchId = String(state.harnessEvalBatch?.batchId ?? "");
  if (batchRequestId) {
    const stage = String(state.harnessEvalBatches?.[batchId]?.stage ?? "");
    if (!TERMINAL_HARNESS_BATCH_STAGES.has(stage)) return true;
  }

  const promotionRequestId = String(state.harnessEvalPromotion?.requestId ?? "");
  if (
    promotionRequestId
    && !state.harnessEvalPromotions?.[promotionRequestId]
  ) return true;
  return false;
}

export function createBridgeRecoveryTracker({
  maxAttempts = DEFAULT_MAX_ATTEMPTS,
  delaysMs = DEFAULT_DELAYS_MS,
} = {}) {
  const maximum = boundedInteger(maxAttempts, "maxAttempts", 1, 10);
  if (!Array.isArray(delaysMs) || delaysMs.length < 1) {
    throw new TypeError("delaysMs 必须是非空数组");
  }
  const delays = delaysMs.map(
    (value, index) => boundedInteger(value, `delaysMs[${index}]`, 0, 60_000),
  );
  let incident = 0;
  let active = false;
  let settling = false;
  let sessionId = "";
  let attempt = 0;
  let resumeRequestId = "";

  function snapshot() {
    return {
      active,
      settling,
      incident,
      sessionId,
      attempt,
      maxAttempts: maximum,
      resumeRequestId,
    };
  }

  return {
    begin(nextSessionId = "") {
      if (!active) {
        if (!settling) {
          incident += 1;
          attempt = 0;
        }
        active = true;
        settling = false;
        sessionId = String(nextSessionId ?? "").trim();
        resumeRequestId = "";
      }
      return snapshot();
    },

    nextAttempt() {
      if (!active) throw new Error("Bridge 恢复尚未开始");
      if (attempt >= maximum) {
        return { ...snapshot(), allowed: false, exhausted: true, delayMs: 0 };
      }
      attempt += 1;
      resumeRequestId = "";
      return {
        ...snapshot(),
        allowed: true,
        exhausted: false,
        delayMs: delays[Math.min(attempt - 1, delays.length - 1)],
      };
    },

    markResumeRequest(requestId) {
      if (!active) throw new Error("Bridge 恢复尚未开始");
      resumeRequestId = String(requestId ?? "").trim();
      if (!resumeRequestId) throw new Error("恢复 request id 不能为空");
      return snapshot();
    },

    matchesResume(record) {
      return active
        && Boolean(resumeRequestId)
        && String(record?.request_id ?? "") === resumeRequestId;
    },

    complete() {
      const completed = snapshot();
      active = false;
      settling = true;
      resumeRequestId = "";
      return completed;
    },

    settle() {
      const settled = snapshot();
      active = false;
      settling = false;
      sessionId = "";
      attempt = 0;
      resumeRequestId = "";
      return settled;
    },

    abort() {
      const aborted = snapshot();
      active = false;
      settling = false;
      sessionId = "";
      attempt = 0;
      resumeRequestId = "";
      return aborted;
    },

    snapshot,
  };
}

export function bridgeRecoveryHandshakeTimeoutFromEnv(env = {}) {
  const value = Number(env.NAUMI_BRIDGE_RECOVERY_TIMEOUT_MS);
  if (!Number.isFinite(value) || value <= 0) return DEFAULT_HANDSHAKE_TIMEOUT_MS;
  return Math.min(60_000, Math.max(100, Math.trunc(value)));
}

function blocked(reason, message) {
  return {
    recoverable: false,
    reason,
    message,
    sessionId: "",
  };
}

function boundedInteger(value, name, minimum, maximum) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < minimum || parsed > maximum) {
    throw new RangeError(`${name} 必须是 ${minimum}..${maximum} 的整数`);
  }
  return parsed;
}
