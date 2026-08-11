#!/usr/bin/env node
import { spawn } from "node:child_process";
import process from "node:process";
import { configureAnsiColors, sanitizeTerminalText } from "./ansi.js";
import { bridgeEnvironment, isIgnorableBridgeStderr } from "./bridge-stderr.js";
import {
  assessIdleBridgeRecovery,
  bridgeRecoveryHandshakeTimeoutFromEnv,
  createBridgeRecoveryTracker,
} from "./bridge-recovery.js";
import { createDebugLog } from "./debug-log.js";
import { createHeartbeatController, heartbeatTimingFromEnv } from "./heartbeat.js";
import {
  INPUT_KEYS,
  backspaceInput,
  clearInput,
  createInputTokenizerState,
  deleteInputForward,
  insertInputNewline,
  insertInputText,
  moveInputCursor,
  moveInputCursorToLineBoundary,
  moveInputCursorVertical,
  navigateInputHistory,
  rememberSubmittedInput,
  tokenizeInputChunk,
} from "./input-buffer.js";
import {
  acceptHistorySearch,
  appendHistorySearchQuery,
  backspaceHistorySearchQuery,
  cancelHistorySearch,
  cycleHistorySearch,
  moveHistorySearchSelection,
  openHistorySearch,
  resetHistorySearch,
} from "./history-search.js";
import {
  acceptSlashCompletion,
  dismissSlashCompletion,
  isSlashCompletionOpen,
  moveSlashCompletionSelection,
  resetSlashCompletion,
  syncSlashCompletion,
} from "./slash-completion.js";
import {
  acceptCommandQuickOpen,
  appendCommandQuickOpenQuery,
  backspaceCommandQuickOpenQuery,
  closeCommandQuickOpen,
  moveCommandQuickOpenSelection,
  openCommandQuickOpen,
  requestCommandQuickOpenFiles,
  switchCommandQuickOpenProvider,
} from "./command-quick-open.js";
import {
  attachJsonlLineReader,
  createEventSender,
  createHelloPayload,
  createServerSequenceGuard,
  isValidServerSequence,
  normalizeServerRecord,
  parseArgs,
  parseBridgeCommandJson,
  PROTOCOL_VERSION,
  splitShellLike,
} from "./protocol.js";
import {
  handleAgentControlKey,
  handleHarnessDetailKey,
  handleHarnessEvalBaselineKey,
  handleHarnessEvalBatchKey,
  handleHarnessEvalPromotionKey,
  handleDoctorHealthKey,
  handleEvolutionEvaluationLaneKey,
  handleEvolutionReviewKey,
  handleGoalPanelKey,
  handlePermissionCenterKey,
  handleInteractionKey,
  handleSubmitText,
  isLocalExitCommand,
  handleRuntimeInspectorKey,
  handleWorkbenchOverviewKey,
  hasTaskPanelFocus,
  cancelTaskPanelItem,
  jumpToTaskPanelRecord,
  markBridgeReconnecting,
  openLatestHarnessDetail,
  openSelectedTaskPanelItem,
  pushSystemMessage,
  reduceServerEvent,
  selectTaskPanelBoundary,
  selectTaskPanelOffset,
  selectTaskPanelPage,
  setTaskPanelFocus,
  setTaskPanelItemExpanded,
  toggleTaskPanelItemExpanded,
  createInitialState,
  createUiSnapshot,
  applyUiSnapshot,
  failQueuedUserMessages,
  requestRunCancel,
  requestTaskPanelRefresh,
  toggleComposerIntent,
  toggleRuntimeInspector,
  updateBridgeHeartbeat,
} from "./state.js";
import { captureViewportAnchor, renderScreen, restoreViewportAnchor } from "./render.js";
import {
  jumpTimelineToLatest,
  markTimelineOutput,
  scrollTimeline,
} from "./timeline-follow.js";
import { createTrackpadScrollController } from "./scroll-input.js";
import {
  createGracefulShutdownController,
  shutdownSignalsForPlatform,
} from "./shutdown-controller.js";
import { shouldAnimateWorkingIndicator } from "./components/working-indicator.js";
import { createWorkingAnimationController } from "./working-animation.js";
import { createScreenPainter } from "./screen-painter.js";
import { createRedrawScheduler } from "./redraw-scheduler.js";
import { createProtocolEventBatcher } from "./protocol-event-batcher.js";
import { detectTerminalCapabilities } from "./terminal-capabilities.js";
import { createTerminalSession } from "./terminal-session.js";
import {
  getProjectInputHistory,
  getUiSnapshot,
  loadUiStateStore,
  saveUiStateStore,
  setProjectInputHistory,
  setUiSnapshot,
} from "./ui-state-store.js";

const args = parseArgs(process.argv.slice(2));
const terminalCapabilities = detectTerminalCapabilities();
const state = createInitialState();
const uiStateStore = loadUiStateStore(process.cwd());
const launchUiStateStore = {
  ...uiStateStore,
  sessions: { ...uiStateStore.sessions },
};
const explicitlyRestoredSessionIds = new Set();
const heartbeatTiming = heartbeatTimingFromEnv(process.env);
const bridgeRecoveryTimeoutMs = bridgeRecoveryHandshakeTimeoutFromEnv(process.env);
const bridgeRecovery = createBridgeRecoveryTracker();
const BRIDGE_RECOVERY_STABILITY_MS = 5_000;
state.inputHistory = getProjectInputHistory(uiStateStore);
let debugLog = null;

let bridge = null;
let rawSend = null;
let send = null;
let heartbeat = null;
let helloRequestId = "";
let nextDeferredProtocolId = 1;
const deferredProtocolSends = [];
let uiSnapshotTimer = null;
let inputEscapeTimer = null;
let bridgeRecoveryTimer = null;
let bridgeRecoveryDeadlineTimer = null;
let bridgeRecoveryStabilityTimer = null;
let bridgeRecoveryReplayConfirmed = false;
let quitting = false;
let viewportWidth = null;
let viewportHeight = null;
const inputTokenizer = createInputTokenizerState();
const trackpadScrollController = createTrackpadScrollController({
  onStep(direction) {
    if (quitting) return;
    scrollTimeline(state, direction === "up" ? 1 : -1);
    scheduleUiSnapshotPersist();
    scheduleRedraw();
  },
});
const terminalSession = createTerminalSession({
  stdin: process.stdin,
  stdout: process.stdout,
  capabilities: terminalCapabilities,
});
const screenPainter = createScreenPainter({
  write: (value) => process.stdout.write(value),
  synchronizedOutput: terminalCapabilities.synchronizedOutput,
});
const redrawScheduler = createRedrawScheduler({ onRedraw: redraw });
const protocolEventBatcher = createProtocolEventBatcher({ onRecord: processBridgeRecord });
let serverSequenceGuard = createServerSequenceGuard();
const workingAnimation = createWorkingAnimationController({
  onFrame(frame) {
    state.workingAnimationFrame = frame;
    if (!quitting) scheduleRedraw();
  },
});
const shutdownController = createGracefulShutdownController({
  sendShutdown() {
    protocolEventBatcher.flush();
    if (typeof send !== "function") {
      throw new Error("Bridge 发送通道尚未建立");
    }
    return send("shutdown", {});
  },
  onRequest({ source }) {
    quitting = true;
    try {
      persistUiSnapshot();
    } catch (error) {
      logDebug("terminal_ui.shutdown.persist_failed", {
        source,
        error: safeFatalMessage(error),
      });
    }
    pushSystemMessage(
      state,
      "正在安全关闭",
      "等待本地 Runtime 完成持久化与资源清理…",
      "info",
      { dismissWelcome: true },
    );
    redrawScheduler.flush();
  },
  onLifecycle(event, payload) {
    logDebug(`terminal_ui.shutdown.${event}`, payload);
  },
  cleanup() {
    restoreTerminal();
    terminateBridge();
    debugLog?.close();
  },
  exitProcess(code) {
    if (code === 0) {
      process.exit(0);
      return;
    }
    try {
      process.stderr.write(
        "\nNaumi 终端 UI 安全关闭未完成；运行 `naumi doctor` 查看本地诊断。\n",
      );
    } finally {
      process.exit(code);
    }
  },
});

main();

function main() {
  if (args.selfTest) {
    process.stdout.write(JSON.stringify({
      ok: true,
      component: "naumi-terminal-ui",
      protocol_version: PROTOCOL_VERSION,
    }) + "\n");
    return;
  }
  if (!terminalCapabilities.interactive) {
    process.stderr.write(
      "Naumi 新终端 UI 需要交互式 TTY；请在 Terminal、iTerm2、Kitty、WezTerm、Windows Terminal 或常见 Linux 终端中运行。\n",
    );
    process.exitCode = 2;
    return;
  }
  if (!terminalCapabilities.fullScreen) {
    process.stderr.write(
      "当前终端未通过 Naumi 全屏控制能力检测，已拒绝发送备用屏幕或光标控制序列；请使用常见现代终端，或改用 `naumi --tui`。\n",
    );
    process.exitCode = 2;
    return;
  }
  configureAnsiColors(terminalCapabilities.colors);
  installProcessHandlers();
  debugLog = createDebugLog({ cwd: process.cwd(), env: process.env });
  state.frontendDebugLogPath = debugLog?.path ?? "";
  debugLog?.log("terminal_ui.state", { frontend_debug_log_path: state.frontendDebugLogPath });
  send = sendWithProtocolGate;
  connectBridge();
  setupTerminal();
  redrawScheduler.settleInitial();
}

function connectBridge() {
  const connectedBridge = startBridge();
  bridge = connectedBridge;
  bridgeRecoveryReplayConfirmed = false;
  serverSequenceGuard = createServerSequenceGuard();
  rawSend = createEventSender(connectedBridge.stdin, { debugLog });
  heartbeat = createHeartbeatController({
    sendPing: (id) => send("ping", {}, { id }),
    onHealth(value) {
      const previousStatus = state.bridgeHeartbeat?.status;
      const addedMessage = updateBridgeHeartbeat(state, value);
      logDebug("heartbeat.health", value);
      if (addedMessage) {
        markTimelineOutput(
          state,
          { type: "heartbeat/status", payload: value },
          `heartbeat-${value.status}-${Date.now()}`,
        );
      }
      if (addedMessage || previousStatus !== value.status) scheduleRedraw();
    },
    onDebug: logDebug,
    ...heartbeatTiming,
  });
  connectedBridge.on("error", (error) => {
    handleBridgeFailure(connectedBridge, {
      kind: "spawn_error",
      code: null,
      signal: null,
      error: safeFatalMessage(error),
    });
  });
  attachJsonlLineReader(connectedBridge.stdout, (line) => {
    if (connectedBridge === bridge) handleBridgeLine(line);
  });
  connectedBridge.stderr.on("data", (chunk) => {
    if (connectedBridge !== bridge) return;
    const lines = chunk.toString("utf8").split(/\r?\n/);
    for (const rawLine of lines) {
      const text = rawLine.trim();
      if (!text) continue;
      const ignored = isIgnorableBridgeStderr(text);
      debugLog?.log("bridge.stderr", { text, ignored });
      if (!ignored) {
        pushSystemMessage(state, "bridge stderr", text, "warning", { dismissWelcome: true });
      }
    }
  });
  connectedBridge.stdin.on("error", (error) => {
    if (quitting || connectedBridge !== bridge) return;
    failQueuedUserMessages(state, {
      code: "bridge_write_failed",
      message: "无法写入本地 Bridge，请检查后端进程后重试。",
    });
    pushSystemMessage(
      state,
      "bridge stdin",
      `本地 Bridge 写入失败: ${error.message}`,
      "error",
      { dismissWelcome: true },
    );
    persistUiSnapshot();
    scheduleRedraw();
  });
  connectedBridge.on("exit", (code, signal) => {
    handleBridgeFailure(connectedBridge, {
      kind: "exit",
      code,
      signal,
      error: "",
    });
  });

  helloRequestId = rawSend("hello", createHelloPayload());
  if (bridgeRecovery.snapshot().active) armBridgeRecoveryDeadline(connectedBridge);
}

function startBridge() {
  const env = bridgeEnvironment(process.env);
  if (args.bridgeCommandJson) {
    const [cmd, ...cmdArgs] = parseBridgeCommandJson(args.bridgeCommandJson);
    return spawn(cmd, cmdArgs, { stdio: ["pipe", "pipe", "pipe"], cwd: process.cwd(), env });
  }
  if (args.bridgeCommand) {
    const [cmd, ...cmdArgs] = splitShellLike(args.bridgeCommand);
    return spawn(cmd, cmdArgs, { stdio: ["pipe", "pipe", "pipe"], cwd: process.cwd(), env });
  }
  return spawn(
    "uv",
    ["run", "python", "-m", "naumi_agent.ui.bridge", "--config", args.config],
    { stdio: ["pipe", "pipe", "pipe"], cwd: process.cwd(), env },
  );
}

function handleBridgeFailure(targetBridge, details) {
  if (targetBridge !== bridge) return;
  logDebug("bridge.failure", {
    ...details,
    quitting,
    recovery: bridgeRecovery.snapshot(),
  });
  bridge = null;
  rawSend = null;
  heartbeat?.stop();
  heartbeat = null;
  clearBridgeRecoveryDeadline();
  clearBridgeRecoveryStabilityTimer();
  bridgeRecoveryReplayConfirmed = false;
  protocolEventBatcher.cancel();
  if (quitting) {
    shutdownController.bridgeExited(details);
    return;
  }

  const activeRecovery = bridgeRecovery.snapshot().active;
  if (!activeRecovery) {
    const decision = assessIdleBridgeRecovery(state);
    if (!decision.recoverable) {
      failBridgeRecoveryClosed(details, decision);
      return;
    }
    bridgeRecovery.begin(decision.sessionId);
    pushSystemMessage(
      state,
      "Bridge 恢复",
      decision.sessionId
        ? `本地 Bridge 已断开；正在恢复会话 ${decision.sessionId}，不会自动重放工具或消息。`
        : "本地 Bridge 已断开；正在重新建立空闲控制面。",
      "warning",
      { dismissWelcome: true },
    );
  }
  markBridgeReconnecting(state);
  scheduleNextBridgeRecovery(details);
}

function scheduleNextBridgeRecovery(lastFailure = {}) {
  clearBridgeRecoveryTimer();
  const next = bridgeRecovery.nextAttempt();
  if (!next.allowed) {
    failBridgeRecoveryClosed(lastFailure, {
      reason: "attempts_exhausted",
      message: `连续 ${next.maxAttempts} 次重连失败。`,
    });
    return;
  }
  logDebug("bridge.recovery.scheduled", {
    incident: next.incident,
    attempt: next.attempt,
    max_attempts: next.maxAttempts,
    delay_ms: next.delayMs,
    session_id: next.sessionId,
  });
  pushSystemMessage(
    state,
    "Bridge 恢复",
    `正在进行第 ${next.attempt}/${next.maxAttempts} 次重连…`,
    "info",
    { dismissWelcome: true },
  );
  scheduleRedraw();
  bridgeRecoveryTimer = setTimeout(() => {
    bridgeRecoveryTimer = null;
    if (quitting || !bridgeRecovery.snapshot().active) return;
    try {
      connectBridge();
    } catch (error) {
      logDebug("bridge.recovery.spawn_error", {
        error: safeFatalMessage(error),
        recovery: bridgeRecovery.snapshot(),
      });
      scheduleNextBridgeRecovery({
        kind: "spawn_throw",
        error: safeFatalMessage(error),
      });
    }
  }, next.delayMs);
}

function armBridgeRecoveryDeadline(targetBridge) {
  clearBridgeRecoveryDeadline();
  bridgeRecoveryDeadlineTimer = setTimeout(() => {
    bridgeRecoveryDeadlineTimer = null;
    if (
      quitting
      || targetBridge !== bridge
      || !bridgeRecovery.snapshot().active
    ) return;
    logDebug("bridge.recovery.timeout", {
      timeout_ms: bridgeRecoveryTimeoutMs,
      recovery: bridgeRecovery.snapshot(),
    });
    try {
      targetBridge.kill();
    } catch (error) {
      handleBridgeFailure(targetBridge, {
        kind: "recovery_timeout",
        code: null,
        signal: null,
        error: safeFatalMessage(error),
      });
    }
  }, bridgeRecoveryTimeoutMs);
  bridgeRecoveryDeadlineTimer?.unref?.();
}

function completeBridgeRecovery(source) {
  const current = bridgeRecovery.snapshot();
  if (!current.active) return false;
  clearBridgeRecoveryDeadline();
  const completed = bridgeRecovery.complete();
  logDebug("bridge.recovery.completed", {
    incident: completed.incident,
    attempt: completed.attempt,
    session_id: completed.sessionId,
    source,
  });
  pushSystemMessage(
    state,
    "Bridge 恢复",
    completed.sessionId
      ? `已重新连接并从权威存储恢复会话 ${completed.sessionId}。`
      : "已重新连接本地 Bridge。",
    "success",
    { dismissWelcome: true },
  );
  flushDeferredProtocolSends();
  scheduleRedraw();
  bridgeRecoveryStabilityTimer = setTimeout(() => {
    bridgeRecoveryStabilityTimer = null;
    const settled = bridgeRecovery.settle();
    logDebug("bridge.recovery.stable", {
      incident: settled.incident,
      attempt: settled.attempt,
      stable_ms: BRIDGE_RECOVERY_STABILITY_MS,
    });
  }, BRIDGE_RECOVERY_STABILITY_MS);
  bridgeRecoveryStabilityTimer?.unref?.();
  return true;
}

function failBridgeRecoveryClosed(details, decision) {
  if (quitting) return;
  quitting = true;
  clearBridgeRecoveryTimer();
  clearBridgeRecoveryDeadline();
  clearBridgeRecoveryStabilityTimer();
  bridgeRecoveryReplayConfirmed = false;
  const recovery = bridgeRecovery.abort();
  failQueuedUserMessages(state, {
    code: "bridge_disconnected",
    message: "本地 Bridge 已断开；消息未自动重放，请在 fallback TUI 中核对。",
  });
  const cause = decision?.message || "后端桥接意外退出。";
  const processDetail = details?.kind === "exit"
    ? ` code=${details.code} signal=${details.signal}`
    : "";
  pushSystemMessage(
    state,
    "Bridge 恢复失败",
    `${cause}${processDetail} 已停止 New UI，避免重复执行或展示未经确认的状态。`,
    "error",
    { dismissWelcome: true },
  );
  logDebug("bridge.recovery.failed_closed", {
    reason: decision?.reason ?? "unknown",
    details,
    recovery,
  });
  redraw();
  restoreTerminal();
  terminateBridge();
  debugLog?.close();
  process.exit(1);
}

function clearBridgeRecoveryTimer() {
  if (bridgeRecoveryTimer === null) return;
  clearTimeout(bridgeRecoveryTimer);
  bridgeRecoveryTimer = null;
}

function clearBridgeRecoveryDeadline() {
  if (bridgeRecoveryDeadlineTimer === null) return;
  clearTimeout(bridgeRecoveryDeadlineTimer);
  bridgeRecoveryDeadlineTimer = null;
}

function clearBridgeRecoveryStabilityTimer() {
  if (bridgeRecoveryStabilityTimer === null) return;
  clearTimeout(bridgeRecoveryStabilityTimer);
  bridgeRecoveryStabilityTimer = null;
}

function setupTerminal() {
  terminalSession.setup({
    onInput: handleKeyInput,
    onResize: handleTerminalResize,
  });
}

function restoreTerminal() {
  clearBridgeRecoveryTimer();
  clearBridgeRecoveryDeadline();
  clearBridgeRecoveryStabilityTimer();
  heartbeat?.stop();
  workingAnimation.stop();
  trackpadScrollController.dispose();
  redrawScheduler.cancel();
  protocolEventBatcher.cancel();
  terminalSession.restore();
}

function exit(source = "user") {
  if (shutdownController.request({ reason: source, exitCode: 0 })) return;
  const snapshot = shutdownController.snapshot();
  if (snapshot.phase === "requested" && source.startsWith("signal:")) {
    shutdownController.force("repeated_signal", 0);
  }
}

function installProcessHandlers() {
  for (const signal of shutdownSignalsForPlatform(process.platform)) {
    process.on(signal, () => exit(`signal:${signal}`));
  }
  process.on("exit", () => terminalSession.restore());
  process.on("uncaughtException", handleFatalError);
  process.on("unhandledRejection", handleFatalError);
}

function handleFatalError(reason) {
  const message = safeFatalMessage(reason);
  if (quitting) {
    logDebug("terminal_ui.shutdown.fatal", {
      error: message,
      stack: reason instanceof Error ? reason.stack : "",
    });
    try {
      process.stderr.write(`\nNaumi 终端 UI 关闭期间发生错误：${message}\n`);
    } finally {
      shutdownController.force("fatal_during_shutdown", 1);
    }
    return;
  }
  quitting = true;
  logDebug("terminal_ui.fatal", {
    error: message,
    stack: reason instanceof Error ? reason.stack : "",
  });
  try {
    persistUiSnapshot();
  } catch {
    // State persistence is best effort during a fatal shutdown.
  }
  restoreTerminal();
  terminateBridge();
  debugLog?.close();
  try {
    process.stderr.write(`\nNaumi 终端 UI 已安全退出：${message}\n`);
  } finally {
    process.exit(1);
  }
}

function terminateBridge() {
  if (!bridge || bridge.killed) return false;
  try {
    return bridge.kill();
  } catch (error) {
    logDebug("bridge.terminate.error", { error: safeFatalMessage(error) });
    return false;
  }
}

function safeFatalMessage(reason) {
  const raw = reason instanceof Error ? reason.message : String(reason ?? "未知错误");
  return sanitizeTerminalText(raw).replace(/\s+/g, " ").trim().slice(0, 300)
    || "未知错误";
}

function logDebug(event, payload) {
  try {
    debugLog?.log(event, payload);
  } catch {
    // Debug logging must never prevent terminal recovery.
  }
}

function handleBridgeLine(line) {
  if (!line.trim()) return;
  debugLog?.log("protocol.receive.line", {
    bytes: Buffer.byteLength(line, "utf8"),
  });
  let record;
  let rawRecord;
  try {
    rawRecord = JSON.parse(line);
    if (rawRecord?.seq != null && !isValidServerSequence(rawRecord.seq)) {
      const decision = serverSequenceGuard.invalidate(rawRecord.seq);
      if (decision.action !== "accept") {
        handleServerSequenceDecision(decision, rawRecord);
        return;
      }
    }
    record = normalizeServerRecord(rawRecord);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    debugLog?.log("protocol.receive.error", {
      type: sanitizeTerminalText(String(rawRecord?.type ?? ""))
        .replace(/\s+/g, " ")
        .slice(0, 128),
      bytes: Buffer.byteLength(line, "utf8"),
      error: message,
    });
    protocolEventBatcher.flush();
    if (rawRecord?.type === "ack" && rawRecord?.payload?.event === "hello") {
      deferredProtocolSends.length = 0;
      failQueuedUserMessages(state, {
        code: "protocol_negotiation_invalid",
        message: `终端协议协商响应无效：${message}`,
      });
    }
    pushSystemMessage(state, "bridge protocol", message, "error", { dismissWelcome: true });
    scheduleRedraw();
    return;
  }
  const enablesSequenceIntegrity = record.type === "ack"
    && record.payload?.event === "hello"
    && record.payload.negotiation.capabilities.includes("sequence_integrity");
  const sequenceDecision = enablesSequenceIntegrity
    ? serverSequenceGuard.enable(record)
    : serverSequenceGuard.observe(record);
  if (!handleServerSequenceDecision(sequenceDecision, record)) return;
  if (record.unknown_informational === true) {
    protocolEventBatcher.flush();
    const registryCompatibility = String(
      state.status?.protocol_registry?.compatibility ?? "",
    );
    if (registryCompatibility !== "attested_additive") {
      logDebug("protocol.unknown.rejected", {
        type: record.type,
        request_id: record.request_id ?? "",
        seq: record.seq ?? null,
        criticality: "informational",
        reason: "registry_not_attested_additive",
        payload_omitted: true,
      });
      pushSystemMessage(
        state,
        "bridge protocol",
        `Bridge 发送了未被兼容清单证明的新增事件 ${record.type}；已忽略其内容并继续运行。`,
        "warning",
        { dismissWelcome: true },
      );
      scheduleRedraw();
      return;
    }
    logDebug("protocol.unknown.informational", {
      type: record.type,
      request_id: record.request_id ?? "",
      seq: record.seq ?? null,
      criticality: "informational",
      payload_omitted: true,
    });
    return;
  }
  debugLog?.log("protocol.receive.record", { type: record.type, request_id: record.request_id, seq: record.seq, payload: record.payload });
  if (record.type === "pong") {
    processBridgeRecord(record);
    return;
  }
  protocolEventBatcher.push(record);
}

function handleServerSequenceDecision(decision, record) {
  if (decision.action === "accept") return true;
  const details = {
    code: decision.code,
    type: record?.type,
    expected_seq: decision.expectedSeq,
    received_seq: decision.receivedSeq,
    last_seq: decision.lastSeq,
  };
  if (decision.action === "ignore") {
    logDebug("protocol.sequence.ignored", details);
    return false;
  }
  if (decision.action === "quarantine") {
    logDebug("protocol.sequence.quarantined", details);
    return false;
  }
  protocolEventBatcher.flush();
  logDebug("protocol.sequence.desync", details);
  const expected = decision.expectedSeq == null ? "有效起始序号" : String(decision.expectedSeq);
  const received = decision.receivedSeq == null ? "缺失或无效值" : String(decision.receivedSeq);
  handleFatalError(new Error(
    `事件流序号不完整（期望 ${expected}，收到 ${received}）。`
    + "当前运行状态待确认，已隔离后续事件并将切换到 Textual TUI；请使用 /resume 核对。",
  ));
  return false;
}

function processBridgeRecord(record) {
  if (record.type === "pong") {
    heartbeat?.receivePong(record.request_id);
    return;
  }
  const durableDecision = assessDurableTerminalEvent(record);
  if (durableDecision.action === "duplicate") {
    acknowledgeTerminalEvent({
      stream_id: state.terminalEventCursor.streamId,
      cursor: state.terminalEventCursor.cursor,
    });
    return;
  }
  if (durableDecision.action === "gap") {
    handleFatalError(new Error(
      `持久回执游标不连续（期望 ${durableDecision.expectedCursor}，收到 ${record.cursor}）。`
      + "已停止消费，避免把缺失回执误报为完成。",
    ));
    return;
  }
  const previousSessionId = state.currentSessionId;
  const previousSnapshot = createUiSnapshot(state);
  const actions = reduceServerEvent(state, record);
  if (record.type === "ack" && record.payload?.event === "hello") {
    heartbeat?.start();
    const recovery = bridgeRecovery.snapshot();
    if (recovery.active && recovery.sessionId) {
      const requestId = `bridge-recovery-${recovery.incident}-${recovery.attempt}`;
      bridgeRecovery.markResumeRequest(requestId);
      rawSend("resume", {
        session_id: recovery.sessionId,
        clear: true,
        ...terminalEventResumeFields(recovery.sessionId),
      }, { id: requestId });
      logDebug("bridge.recovery.resume_sent", {
        incident: recovery.incident,
        attempt: recovery.attempt,
        session_id: recovery.sessionId,
        request_id: requestId,
      });
    } else if (!recovery.active) {
      flushDeferredProtocolSends();
    }
  } else if (record.type === "error" && record.request_id === helloRequestId) {
    deferredProtocolSends.length = 0;
    failQueuedUserMessages(state, {
      code: record.payload?.code ?? "protocol_negotiation_failed",
      message: record.payload?.message ?? "终端协议协商失败，请升级后重试。",
    });
    if (bridgeRecovery.snapshot().active) {
      failBridgeRecoveryClosed(
        { kind: "hello_rejected", error: record.payload?.message ?? "" },
        {
          reason: "protocol_negotiation_failed",
          message: "重连后的 Bridge 协议协商失败。",
        },
      );
      return;
    }
  } else if (
    record.type === "error"
    && bridgeRecovery.matchesResume(record)
  ) {
    failBridgeRecoveryClosed(
      { kind: "resume_rejected", error: record.payload?.message ?? "" },
      {
        reason: "session_resume_failed",
        message: "重连成功，但指定会话无法从权威存储恢复。",
      },
    );
    return;
  } else if (
    record.type === "session/replayed"
    && bridgeRecovery.matchesResume(record)
  ) {
    bridgeRecoveryReplayConfirmed = true;
    logDebug("bridge.recovery.session_replayed", {
      session_id: record.payload?.session_id ?? "",
      request_id: record.request_id ?? "",
    });
  } else if (
    record.type === "runtime/status"
    && bridgeRecovery.snapshot().active
    && bridgeRecoveryReplayConfirmed
  ) {
    completeBridgeRecovery("resume_status");
  } else if (
    record.type === "ready"
    && bridgeRecovery.snapshot().active
    && !bridgeRecovery.snapshot().sessionId
  ) {
    completeBridgeRecovery("ready_without_session");
  }
  syncWorkingAnimation();
  if (state.currentSessionId !== previousSessionId) {
    setUiSnapshot(uiStateStore, previousSessionId, previousSnapshot);
  }
  if (record.type === "session/replayed") {
    restoreUiSnapshot(state.currentSessionId, {
      preferLaunchSnapshot: !bridgeRecovery.snapshot().active,
    });
    applyTerminalEventRecoveryStart(record.payload?.terminal_event_recovery);
    resetHistorySearch(state);
    jumpTimelineToLatest(state);
    if (state.inspector.open) requestRuntimeInspectorSnapshot();
    if (state.agents.open) requestAgentControlSnapshot();
  }
  if (record.type === "terminal_events/recovery") {
    completeTerminalEventRecovery(record.payload);
  } else if (durableDecision.action === "accept") {
    const sameStream = state.terminalEventCursor.streamId === String(record.stream_id);
    state.terminalEventCursor = {
      streamId: String(record.stream_id),
      cursor: sameStream
        ? Math.max(state.terminalEventCursor.cursor, Number(record.cursor))
        : Number(record.cursor),
    };
    persistTerminalEventCursor();
    acknowledgeTerminalEvent({
      stream_id: state.terminalEventCursor.streamId,
      cursor: state.terminalEventCursor.cursor,
    });
  }
  if (
    record.type === "runtime/status"
    && state.terminalEventRecovery?.mode === "legacy_snapshot"
  ) {
    state.terminalEventRecovery = { mode: "" };
  }
  if (!(record.type === "ui/message" && record.payload?.type === "thinking" && !state.showReasoning)) {
    markTimelineOutput(state, record, timelineEntryId(record));
  }
  for (const action of actions) {
    if (action.type === "refresh_task_panel") {
      requestTaskPanelRefresh(state, send, action);
    }
    if (action.type === "request_completion_receipt") {
      send("receipt/request", {
        session_id: action.sessionId ?? "",
        receipt_id: action.receiptId ?? "",
        run_id: action.runId ?? "",
      });
    }
    if (action.type === "refresh_inspector") {
      send("inspector/request", {
        open: true,
        known_revision: action.knownRevision ?? state.inspector.revision,
        session_id: action.sessionId ?? state.currentSessionId,
      });
    }
    if (action.type === "refresh_agents") {
      send("agents/request", {
        open: true,
        known_revision: action.knownRevision ?? state.agents.revision,
        session_id: action.sessionId ?? state.currentSessionId,
      });
    }
    if (action.type === "refresh_workbench") {
      send("workbench/request", {
        open: true,
        subscribe: true,
        session_id: action.sessionId ?? state.currentSessionId,
        known_stream_id: action.knownStreamId ?? state.workbench.stream_id,
        known_revision: action.knownRevision ?? state.workbench.revision,
        known_timeline_stream_id:
          action.knownTimelineStreamId ?? state.workbench.timeline_stream_id,
        known_timeline_cursor:
          action.knownTimelineCursor ?? state.workbench.timeline_cursor,
      });
    }
    if (action.type === "doctor_export_preview") {
      state.doctorHealth.exportRequestId = String(send("doctor/export", {
        action: "preview",
      }) || "");
    }
  }
  if (actions.some((action) => action.type === "exit")) {
    shutdownController.acknowledge({
      responseRequestId: record.request_id,
      ok: record.payload?.ok !== false,
    });
    return;
  }
  scheduleUiSnapshotPersist();
  if (isUrgentProtocolRecord(record)) {
    redrawScheduler.flush();
  } else {
    scheduleRedraw();
  }
}

function isUrgentProtocolRecord(record) {
  if ([
    "error",
    "permission/request",
    "interaction/request",
    "run/completed",
    "run/cancelled",
    "completion/receipt",
  ].includes(record.type)) return true;
  return record.type === "ui/message" && record.payload?.type === "tool_result";
}

function sendWithProtocolGate(type, payload, options = {}) {
  if (state.protocolNegotiated) {
    const normalizedPayload = type === "resume"
      ? { ...payload, ...terminalEventResumeFields(payload?.session_id) }
      : payload;
    return rawSend(type, normalizedPayload, options);
  }
  const id = options.id ? String(options.id) : `ui-deferred-${nextDeferredProtocolId++}`;
  deferredProtocolSends.push({
    type,
    payload,
    options: { ...options, id },
  });
  debugLog?.log("protocol.send.deferred", { type, id });
  return id;
}

function terminalEventResumeFields(sessionId) {
  if (!terminalEventRecoveryNegotiated()) return {};
  const normalizedSessionId = String(sessionId ?? "").trim();
  if (!normalizedSessionId) return {};
  const snapshot = normalizedSessionId === String(state.currentSessionId || "")
    ? { terminalEventCursor: state.terminalEventCursor }
    : getUiSnapshot(uiStateStore, normalizedSessionId);
  const cursor = snapshot?.terminalEventCursor;
  if (
    !/^tes_[0-9a-f]{24}$/.test(String(cursor?.streamId ?? ""))
    || !Number.isSafeInteger(cursor?.cursor)
    || cursor.cursor < 1
  ) return {};
  return {
    terminal_event_client_id: uiStateStore.terminalEventClientId,
    terminal_event_stream_id: cursor.streamId,
    resume_after_cursor: cursor.cursor,
  };
}

function assessDurableTerminalEvent(record) {
  if (!record?.event_id) return { action: "none" };
  const streamId = String(record.stream_id ?? "");
  const cursor = Number(record.cursor);
  const current = state.terminalEventCursor || { streamId: "", cursor: 0 };
  const recoveryMode = String(state.terminalEventRecovery?.mode || "");
  if (recoveryMode === "legacy_snapshot") return { action: "accept" };
  if (!current.streamId || current.streamId !== streamId) {
    return current.streamId
      ? { action: "gap", expectedCursor: current.cursor + 1 }
      : { action: "accept" };
  }
  if (cursor <= current.cursor) return { action: "duplicate" };
  if (cursor !== current.cursor + 1) {
    return { action: "gap", expectedCursor: current.cursor + 1 };
  }
  return { action: "accept" };
}

function applyTerminalEventRecoveryStart(recovery) {
  const mode = String(recovery?.mode || "legacy_snapshot");
  state.terminalEventRecovery = { ...recovery, mode };
  if (mode === "gap_snapshot") {
    state.terminalEventCursor = { streamId: "", cursor: 0 };
    persistTerminalEventCursor();
    return;
  }
  if (mode !== "cursor_replay") return;
  const current = state.terminalEventCursor || {};
  if (
    current.streamId !== recovery.stream_id
    || current.cursor !== recovery.requested_cursor
  ) {
    handleFatalError(new Error(
      "本地回执游标与 Bridge 恢复计划不一致；已停止恢复以避免跳过事件。",
    ));
  }
}

function completeTerminalEventRecovery(recovery) {
  if (String(recovery?.session_id || "") !== String(state.currentSessionId || "")) {
    handleFatalError(new Error("终端事件恢复结果不属于当前会话。"));
    return;
  }
  const latestCursor = Number(recovery.latest_cursor);
  const streamId = String(recovery.stream_id || "");
  if (recovery.mode === "replay_complete") {
    if (
      latestCursor > 0
      && (
        state.terminalEventCursor.streamId !== streamId
        || state.terminalEventCursor.cursor !== latestCursor
      )
    ) {
      handleFatalError(new Error("终端事件重放未到达权威最新游标。"));
      return;
    }
  } else if (recovery.mode === "snapshot_complete") {
    state.terminalEventCursor = streamId && latestCursor > 0
      ? { streamId, cursor: latestCursor }
      : { streamId: "", cursor: 0 };
    persistTerminalEventCursor();
  }
  state.terminalEventRecovery = { mode: "" };
  if (streamId && latestCursor > 0) {
    acknowledgeTerminalEvent({
      stream_id: streamId,
      cursor: latestCursor,
    });
  }
}

function persistTerminalEventCursor() {
  setUiSnapshot(uiStateStore, state.currentSessionId, createUiSnapshot(state));
  if (!saveUiStateStore(uiStateStore)) {
    handleFatalError(new Error(
      "无法持久保存回执游标；已停止确认，避免下次重连跳过事件。",
    ));
  }
}

function acknowledgeTerminalEvent(record) {
  if (
    !terminalEventRecoveryNegotiated()
    || !state.currentSessionId
    || !record?.stream_id
    || !record?.cursor
  ) return;
  send("terminal_events/ack", {
    client_id: uiStateStore.terminalEventClientId,
    session_id: state.currentSessionId,
    stream_id: String(record.stream_id),
    cursor: Number(record.cursor),
  });
}

function terminalEventRecoveryNegotiated() {
  return state.protocolNegotiated
    && Array.isArray(state.protocolNegotiation?.capabilities)
    && state.protocolNegotiation.capabilities.includes("terminal_event_recovery");
}

function flushDeferredProtocolSends() {
  while (deferredProtocolSends.length > 0) {
    const pending = deferredProtocolSends.shift();
    const payload = pending.type === "resume"
      ? {
        ...pending.payload,
        ...terminalEventResumeFields(pending.payload?.session_id),
      }
      : pending.payload;
    rawSend(pending.type, payload, pending.options);
  }
}

function handleKeyInput(chunk) {
  const previousInput = state.input;
  const previousCursor = state.inputCursor;
  if (inputEscapeTimer) {
    clearTimeout(inputEscapeTimer);
    inputEscapeTimer = null;
  }
  const tokens = tokenizeInputChunk(chunk, inputTokenizer);
  debugLog?.log("input.chunk", {
    chars: String(chunk),
    char_count: Array.from(String(chunk)).length,
    pending_escape_chars: inputTokenizer.pendingEscape.length,
    paste_chars: inputTokenizer.pasteBuffer === null
      ? 0
      : Array.from(inputTokenizer.pasteBuffer).length,
  });
  for (const token of tokens) {
    if (token.type === "paste") {
      if (state.commandQuickOpen?.open) {
        appendCommandQuickOpenQuery(state, token.value);
        refreshCommandQuickOpenFiles();
      } else if (state.interaction) {
        handleInteractionKey(state, token.value, send);
      } else if (state.historySearch?.open) {
        appendHistorySearchQuery(state, token.value);
      } else {
        insertInputText(state, token.value);
      }
      scheduleRedraw();
      continue;
    }
    handleSingleKeyInput(token.value);
  }
  if (inputTokenizer.pendingEscape === INPUT_KEYS.escape) {
    inputEscapeTimer = setTimeout(() => {
      inputEscapeTimer = null;
      if (inputTokenizer.pendingEscape !== INPUT_KEYS.escape) return;
      inputTokenizer.pendingEscape = "";
      handleSingleKeyInput(INPUT_KEYS.escape);
    }, 30);
  }
  if (state.input !== previousInput || state.inputCursor !== previousCursor) {
    syncSlashCompletion(state);
    scheduleUiSnapshotPersist();
  }
}

function handleSingleKeyInput(chunk) {
  if (chunk === "\u0003") {
    if (state.running && !state.cancelPending) {
      requestRunCancel(state, send);
      syncWorkingAnimation();
      persistUiSnapshot();
      scheduleRedraw();
      return;
    }
    exit("ctrl_c");
    return;
  }
  if (state.permission) {
    const key = chunk.toLowerCase();
    if (chunk === INPUT_KEYS.ctrlR) return;
    if (
      chunk === INPUT_KEYS.ctrlI
      || chunk === INPUT_KEYS.ctrlO
      || chunk === INPUT_KEYS.tab
    ) return;
    if (chunk === INPUT_KEYS.shiftTab) {
      send("permission_response", { request_id: state.permission.requestId, choice: "bypass" });
      return;
    }
    if (key === "y" || key === "a") {
      send("permission_response", { request_id: state.permission.requestId, choice: "allow_once" });
      return;
    }
    if (key === "n" || key === "d" || key === "\u001b") {
      send("permission_response", { request_id: state.permission.requestId, choice: "deny" });
      return;
    }
    if (key === "b") {
      send("permission_response", { request_id: state.permission.requestId, choice: "bypass" });
      return;
    }
    if (key === "g" && state.permission.payload.choices?.includes("grant_session")) {
      send("permission_response", { request_id: state.permission.requestId, choice: "grant_session" });
      return;
    }
    if (
      state.agents?.open
      && [
        "x",
        "r",
        "[",
        "]",
        "\r",
        "\n",
        INPUT_KEYS.up,
        INPUT_KEYS.upAlt,
        INPUT_KEYS.down,
        INPUT_KEYS.downAlt,
        INPUT_KEYS.left,
        INPUT_KEYS.leftAlt,
        INPUT_KEYS.right,
        INPUT_KEYS.rightAlt,
      ].includes(chunk)
    ) return;
  }
  if (state.interaction && handleInteractionKey(state, chunk, send)) {
    scheduleRedraw();
    return;
  }
  if (state.commandQuickOpen?.open) {
    handleCommandQuickOpenKey(chunk);
    scheduleRedraw();
    return;
  }
  if (isCommandQuickOpenKey(chunk)) {
    if (state.historySearch?.open) cancelHistorySearch(state);
    openCommandQuickOpen(state);
    scheduleRedraw();
    return;
  }
  if (state.route?.name === "harness_detail" && handleHarnessDetailKey(state, chunk, send)) {
    scheduleRedraw();
    return;
  }
  if (state.route?.name === "harness_eval_baseline" && handleHarnessEvalBaselineKey(state, chunk)) {
    scheduleRedraw();
    return;
  }
  if (state.route?.name === "harness_eval_batch" && handleHarnessEvalBatchKey(state, chunk, send)) {
    scheduleRedraw();
    return;
  }
  if (state.route?.name === "harness_eval_promotion" && handleHarnessEvalPromotionKey(state, chunk)) {
    scheduleRedraw();
    return;
  }
  if (state.route?.name === "doctor_health" && handleDoctorHealthKey(state, chunk, send)) {
    scheduleRedraw();
    return;
  }
  if (state.route?.name === "permissions" && handlePermissionCenterKey(state, chunk, send)) {
    scheduleRedraw();
    return;
  }
  if (state.route?.name === "goals" && handleGoalPanelKey(state, chunk, send)) {
    scheduleRedraw();
    return;
  }
  if (state.route?.name === "evolution_review" && handleEvolutionReviewKey(state, chunk, send)) {
    scheduleRedraw();
    return;
  }
  if (
    state.route?.name === "evolution_evaluation_lane"
    && handleEvolutionEvaluationLaneKey(state, chunk, send)
  ) {
    scheduleRedraw();
    return;
  }
  if (state.route?.name === "workbench" && handleWorkbenchOverviewKey(state, chunk, send)) {
    persistUiSnapshot();
    scheduleRedraw();
    return;
  }
  if (state.agents?.open && handleAgentControlKey(state, chunk, send)) {
    persistUiSnapshot();
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.ctrlI) {
    toggleRuntimeInspector(state, send);
    persistUiSnapshot();
    scheduleRedraw();
    return;
  }
  if (state.historySearch?.open && handleHistorySearchKey(chunk)) {
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.ctrlR) {
    openHistorySearch(state);
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.ctrlT) {
    toggleComposerIntent(state);
    persistUiSnapshot();
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.ctrlO) {
    openLatestHarnessDetail(state, send);
    persistUiSnapshot();
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.shiftTab) {
    send("cycle_mode", {});
    return;
  }
  if (chunk === INPUT_KEYS.shiftEnter) {
    insertInputNewline(state);
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.ctrlEnter) {
    submitComposer();
    return;
  }
  if (
    (chunk === "\r" || chunk === "\n")
    && isLocalExitCommand(state.input)
  ) {
    submitComposer();
    return;
  }
  if (isSlashCompletionOpen(state) && handleSlashCompletionKey(chunk)) {
    scheduleRedraw();
    return;
  }
  if (!state.input.trim() && state.inspector.open) {
    if (handleRuntimeInspectorKey(state, chunk, send)) {
      persistUiSnapshot();
      scheduleRedraw();
      return;
    }
  }
  if (!state.input.trim() && chunk === INPUT_KEYS.tab && !state.inspector.open) {
    toggleRuntimeInspector(state, send);
    persistUiSnapshot();
    scheduleRedraw();
    return;
  }
  if (!state.input.trim() && hasTaskPanelFocus(state) && handleTaskPanelFocusedKey(chunk)) {
    scheduleRedraw();
    return;
  }
  if (chunk === "\r" || chunk === "\n") {
    if (state.input.trim()) {
      submitComposer();
    } else if (hasTaskPanelFocus(state)) {
      openSelectedTaskPanelItem(state, send);
      scheduleRedraw();
    }
    return;
  }
  if (chunk === "\u007f" || chunk === "\b") {
    backspaceInput(state);
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.delete) {
    deleteInputForward(state);
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.left || chunk === INPUT_KEYS.leftAlt) {
    moveInputCursor(state, "left");
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.right || chunk === INPUT_KEYS.rightAlt) {
    moveInputCursor(state, "right");
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.up) {
    if (state.input.includes("\n")) {
      moveInputCursorVertical(state, "up");
    } else {
      navigateInputHistory(state, "up");
    }
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.down) {
    if (state.input.includes("\n")) {
      moveInputCursorVertical(state, "down");
    } else {
      navigateInputHistory(state, "down");
    }
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.upAlt || chunk === INPUT_KEYS.downAlt) {
    const direction = chunk === INPUT_KEYS.upAlt ? "up" : "down";
    trackpadScrollController.push(direction);
    return;
  }
  if (/^[Oo][ABab]$/.test(chunk)) {
    return;
  }
  if (chunk === INPUT_KEYS.ctrlA) {
    moveInputCursor(state, "home");
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.ctrlE) {
    moveInputCursor(state, "end");
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.ctrlL) {
    jumpTimelineToLatest(state);
    persistUiSnapshot();
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.home || chunk === INPUT_KEYS.homeAlt || chunk === INPUT_KEYS.homeSs3) {
    moveInputCursorToLineBoundary(state, "start");
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.end || chunk === INPUT_KEYS.endAlt || chunk === INPUT_KEYS.endSs3) {
    if (state.input) {
      moveInputCursorToLineBoundary(state, "end");
    } else {
      jumpTimelineToLatest(state);
      persistUiSnapshot();
    }
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.pageUp) {
    scrollTimeline(state, Math.max(3, Math.floor((process.stdout.rows ?? 24) / 2)));
    persistUiSnapshot();
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.pageDown) {
    scrollTimeline(state, -Math.max(3, Math.floor((process.stdout.rows ?? 24) / 2)));
    persistUiSnapshot();
    scheduleRedraw();
    return;
  }
  if (chunk >= " " && chunk !== "\x7f") {
    insertInputText(state, chunk);
    scheduleRedraw();
  }
}

function submitComposer() {
  if (!state.input.trim()) {
    scheduleRedraw();
    return false;
  }
  const text = state.input;
  const action = handleSubmitText(state, text, send);
  if (action?.type === "exit") {
    clearInput(state);
    exit("slash_command");
    return true;
  }
  rememberSubmittedInput(state, text);
  setProjectInputHistory(uiStateStore, state.inputHistory);
  clearInput(state);
  jumpTimelineToLatest(state);
  persistUiSnapshot();
  scheduleRedraw();
  return true;
}

function handleHistorySearchKey(chunk) {
  if (chunk === INPUT_KEYS.ctrlR) return cycleHistorySearch(state) || true;
  if (chunk === INPUT_KEYS.escape) return cancelHistorySearch(state);
  if (chunk === INPUT_KEYS.up) return moveHistorySearchSelection(state, "newer") || true;
  if (chunk === INPUT_KEYS.down || chunk === INPUT_KEYS.tab) {
    return moveHistorySearchSelection(state, "older") || true;
  }
  if (chunk === "\r" || chunk === "\n" || chunk === INPUT_KEYS.ctrlEnter) {
    const accepted = acceptHistorySearch(state);
    if (accepted) {
      syncSlashCompletion(state);
      dismissSlashCompletion(state);
    }
    return true;
  }
  if (chunk === "\u007f" || chunk === "\b") {
    return backspaceHistorySearchQuery(state) || true;
  }
  if (chunk >= " " && chunk !== "\x7f") {
    appendHistorySearchQuery(state, chunk);
    return true;
  }
  return true;
}

function handleCommandQuickOpenKey(chunk) {
  if (chunk === INPUT_KEYS.escape || isCommandQuickOpenKey(chunk)) {
    if (state.commandQuickOpen?.provider === "files" && state.commandQuickOpen?.fileLoading) {
      send("workspace/files/cancel", {});
    }
    return closeCommandQuickOpen(state);
  }
  if (chunk === INPUT_KEYS.up) return moveCommandQuickOpenSelection(state, "previous") || true;
  if (chunk === INPUT_KEYS.tab) {
    switchCommandQuickOpenProvider(
      state,
      {
        tasks: () => send("task_panel", {
          limit: 50,
          source: "all",
          status: "all",
          history: false,
        }),
        sessions: () => send("sessions/list/request", {
          page: 1,
          page_size: 100,
          query: "",
        }),
        files: (query, refresh) => send("workspace/files/request", {
          query,
          limit: 200,
          refresh,
        }),
        agents: () => send("agents/request", {
          open: true,
          subscribe: false,
          known_revision: 0,
          session_id: String(state.currentSessionId || ""),
        }),
      },
    );
    return true;
  }
  if (chunk === INPUT_KEYS.down) {
    return moveCommandQuickOpenSelection(state, "next") || true;
  }
  if (chunk === "\r" || chunk === "\n" || chunk === INPUT_KEYS.ctrlEnter) {
    const accepted = acceptCommandQuickOpen(state);
    if (accepted) {
      syncSlashCompletion(state);
      dismissSlashCompletion(state);
    }
    return true;
  }
  if (chunk === "\u007f" || chunk === "\b") {
    const changed = backspaceCommandQuickOpenQuery(state);
    if (changed) refreshCommandQuickOpenFiles();
    return changed || true;
  }
  if (chunk >= " " && chunk !== "\x7f") {
    appendCommandQuickOpenQuery(state, chunk);
    refreshCommandQuickOpenFiles();
    return true;
  }
  return true;
}

function refreshCommandQuickOpenFiles() {
  requestCommandQuickOpenFiles(
    state,
    (query, refresh) => send("workspace/files/request", {
      query,
      limit: 200,
      refresh,
    }),
  );
}

function isCommandQuickOpenKey(chunk) {
  return chunk === INPUT_KEYS.ctrlP || chunk === INPUT_KEYS.ctrlPEnhanced;
}

function handleSlashCompletionKey(chunk) {
  if (chunk === INPUT_KEYS.escape) return dismissSlashCompletion(state);
  if (chunk === INPUT_KEYS.up) return moveSlashCompletionSelection(state, "previous");
  if (chunk === INPUT_KEYS.down || chunk === INPUT_KEYS.tab) {
    return moveSlashCompletionSelection(state, "next");
  }
  if (chunk === "\r" || chunk === "\n") return acceptSlashCompletion(state);
  return false;
}

function timelineEntryId(record) {
  const payload = record.payload ?? {};
  if (record.type === "ui/message" && payload.type === "assistant_stream") {
    return state.activeAssistant?.id
      || latestMessageId("assistant")
      || `assistant-${record.seq ?? "unknown"}`;
  }
  if (record.type === "ui/message" && payload.type === "thinking") {
    return state.activeThinking?.id
      || latestMessageId("thinking")
      || `thinking-${record.seq ?? "unknown"}`;
  }
  if (record.type === "ui/message" && ["tool_prepare", "tool_use", "tool_result"].includes(payload.type)) {
    return payload.tool_call_id || "";
  }
  if (record.type === "permission/request") {
    return record.request_id || record.id || "";
  }
  if (record.type === "ui/message" && payload.type === "permission_bubble") {
    return payload.request_id || record.request_id || record.seq || "";
  }
  return record.request_id || record.seq || "";
}

function latestMessageId(kind) {
  return [...state.messages].reverse().find((message) => message.kind === kind)?.id || "";
}

function handleTaskPanelFocusedKey(chunk) {
  const key = String(chunk ?? "").toLowerCase();
  if (chunk === "\u001b") {
    setTaskPanelFocus(state, false);
    return true;
  }
  if (chunk === "\t" || key === "n" || chunk === INPUT_KEYS.down) {
    selectTaskPanelOffset(state, 1);
    return true;
  }
  if (key === "p" || chunk === INPUT_KEYS.up) {
    selectTaskPanelOffset(state, -1);
    return true;
  }
  if (chunk === INPUT_KEYS.pageUp) {
    selectTaskPanelPage(state, -1);
    return true;
  }
  if (chunk === INPUT_KEYS.pageDown) {
    selectTaskPanelPage(state, 1);
    return true;
  }
  if ([INPUT_KEYS.home, INPUT_KEYS.homeAlt, INPUT_KEYS.homeSs3].includes(chunk)) {
    selectTaskPanelBoundary(state, "first");
    return true;
  }
  if ([INPUT_KEYS.end, INPUT_KEYS.endAlt, INPUT_KEYS.endSs3].includes(chunk)) {
    selectTaskPanelBoundary(state, "last");
    return true;
  }
  if (chunk === "\r" || chunk === "\n" || key === "o") {
    openSelectedTaskPanelItem(state, send);
    return true;
  }
  if (key === "j") {
    jumpToTaskPanelRecord(state);
    return true;
  }
  if (key === "e") {
    toggleTaskPanelItemExpanded(state);
    return true;
  }
  if (key === "c") {
    setTaskPanelItemExpanded(state, "", false);
    return true;
  }
  if (key === "x") {
    cancelTaskPanelItem(state, send);
    return true;
  }
  return false;
}

function restoreUiSnapshot(sessionId, { preferLaunchSnapshot = false } = {}) {
  resetHistorySearch(state);
  resetSlashCompletion(state);
  const normalizedSessionId = String(sessionId || "");
  const launchSnapshot = preferLaunchSnapshot
    && !explicitlyRestoredSessionIds.has(normalizedSessionId)
    ? getUiSnapshot(launchUiStateStore, normalizedSessionId)
    : null;
  applyUiSnapshot(state, launchSnapshot ?? getUiSnapshot(uiStateStore, normalizedSessionId));
  if (preferLaunchSnapshot) explicitlyRestoredSessionIds.add(normalizedSessionId);
}

function requestRuntimeInspectorSnapshot() {
  state.inspector.loading = true;
  send("inspector/request", {
    open: true,
    known_revision: state.inspector.revision,
    session_id: String(state.currentSessionId || ""),
  });
}

function requestAgentControlSnapshot() {
  state.agents.loading = true;
  send("agents/request", {
    open: true,
    known_revision: state.agents.revision,
    session_id: String(state.currentSessionId || ""),
  });
}

function persistUiSnapshot() {
  if (uiSnapshotTimer) {
    clearTimeout(uiSnapshotTimer);
    uiSnapshotTimer = null;
  }
  setUiSnapshot(uiStateStore, state.currentSessionId, createUiSnapshot(state));
  try {
    saveUiStateStore(uiStateStore);
  } catch (error) {
    pushSystemMessage(
      state,
      "ui state",
      `无法保存终端 UI 状态: ${error.message}`,
      "warning",
      { dismissWelcome: true },
    );
  }
}

function scheduleUiSnapshotPersist() {
  setUiSnapshot(uiStateStore, state.currentSessionId, createUiSnapshot(state));
  if (uiSnapshotTimer) clearTimeout(uiSnapshotTimer);
  uiSnapshotTimer = setTimeout(() => {
    uiSnapshotTimer = null;
    try {
      saveUiStateStore(uiStateStore);
    } catch (error) {
      pushSystemMessage(
        state,
        "ui state",
        `无法保存终端 UI 状态: ${error.message}`,
        "warning",
        { dismissWelcome: true },
      );
      scheduleRedraw();
    }
  }, 100);
}

function scheduleRedraw() {
  redrawScheduler.schedule();
}

function handleTerminalResize() {
  const width = Math.max(60, process.stdout.columns ?? 100);
  const height = Math.max(12, process.stdout.rows ?? 30);
  if (!redrawScheduler.painted) {
    viewportWidth = width;
    viewportHeight = height;
    redrawScheduler.settleInitial();
    return;
  }
  if (width === viewportWidth && height === viewportHeight) return;

  const previousWidth = viewportWidth;
  const previousHeight = viewportHeight;
  const anchor = captureViewportAnchor(
    state,
    previousWidth,
    previousHeight,
    terminalRenderEnvironment(),
  );
  const previousOffset = state.scrollOffset;
  restoreViewportAnchor(
    state,
    anchor,
    width,
    height,
    terminalRenderEnvironment(),
  );
  viewportWidth = width;
  viewportHeight = height;
  debugLog?.log("viewport.resize_anchor", {
    previous_width: previousWidth,
    previous_height: previousHeight,
    width,
    height,
    message_id: anchor?.messageId ?? "",
    message_index: anchor?.messageIndex ?? null,
    previous_offset: previousOffset,
    scroll_offset: state.scrollOffset,
    follow_tail: state.followTail,
  });
  scheduleUiSnapshotPersist();
  scheduleRedraw();
}

function redraw() {
  const width = Math.max(60, process.stdout.columns ?? 100);
  const height = Math.max(12, process.stdout.rows ?? 30);
  try {
    const lines = renderScreen(state, width, height, terminalRenderEnvironment());
    viewportWidth = width;
    viewportHeight = height;
    const paint = screenPainter.paint(lines, width, height);
    redrawScheduler.markPainted();
    debugLog?.log("render.screen", {
      width,
      height,
      line_count: lines.length,
      paint_mode: paint.mode,
      changed_rows: paint.changedRows,
      terminal_write: paint.written,
      messages: state.messages.length,
      running: state.running,
      mode: state.mode,
      scroll_offset: state.scrollOffset,
      follow_tail: state.followTail,
      unread_output_count: state.unreadOutputCount,
    });
  } catch (error) {
    debugLog?.log("render.error", {
      width,
      height,
      error: `${error.name}: ${error.message}`,
      stack: error.stack,
    });
    throw error;
  }
}

function syncWorkingAnimation() {
  workingAnimation.sync(shouldAnimateWorkingIndicator(state, {
    isTTY: terminalCapabilities.animate,
    term: terminalCapabilities.terminal,
    ci: !terminalCapabilities.animate,
    reduceMotion: !terminalCapabilities.animate,
  }));
}

function terminalRenderEnvironment() {
  return {
    cwd: process.cwd(),
    home: terminalCapabilities.home,
    term: terminalCapabilities.terminal,
    forceAscii: !terminalCapabilities.unicode,
  };
}
