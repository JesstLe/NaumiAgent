#!/usr/bin/env node
import { spawn } from "node:child_process";
import process from "node:process";
import { configureAnsiColors, sanitizeTerminalText } from "./ansi.js";
import { bridgeEnvironment, isIgnorableBridgeStderr } from "./bridge-stderr.js";
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
  attachJsonlLineReader,
  createEventSender,
  createHelloPayload,
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
  handleEvolutionReviewKey,
  handleGoalPanelKey,
  handlePermissionCenterKey,
  handleInteractionKey,
  handleSubmitText,
  handleRuntimeInspectorKey,
  handleWorkbenchOverviewKey,
  hasTaskPanelFocus,
  cancelTaskPanelItem,
  jumpToTaskPanelRecord,
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
import { createTrackpadScrollFilter } from "./scroll-input.js";
import { shouldAnimateWorkingIndicator } from "./components/working-indicator.js";
import { createWorkingAnimationController } from "./working-animation.js";
import { createScreenPainter } from "./screen-painter.js";
import { createRedrawScheduler } from "./redraw-scheduler.js";
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
let quitting = false;
let viewportWidth = null;
let viewportHeight = null;
const inputTokenizer = createInputTokenizerState();
const trackpadScrollFilter = createTrackpadScrollFilter();
const terminalSession = createTerminalSession({
  stdin: process.stdin,
  stdout: process.stdout,
  capabilities: terminalCapabilities,
});
const screenPainter = createScreenPainter({
  write: (value) => process.stdout.write(value),
});
const redrawScheduler = createRedrawScheduler({ onRedraw: redraw });
const workingAnimation = createWorkingAnimationController({
  onFrame(frame) {
    state.workingAnimationFrame = frame;
    if (!quitting) scheduleRedraw();
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
  configureAnsiColors(terminalCapabilities.colors);
  installProcessHandlers();
  debugLog = createDebugLog({ cwd: process.cwd(), env: process.env });
  state.frontendDebugLogPath = debugLog?.path ?? "";
  debugLog?.log("terminal_ui.state", { frontend_debug_log_path: state.frontendDebugLogPath });
  bridge = startBridge();
  bridge.on("error", handleFatalError);
  rawSend = createEventSender(bridge.stdin, { debugLog });
  send = sendWithProtocolGate;
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
  attachJsonlLineReader(bridge.stdout, handleBridgeLine);
  bridge.stderr.on("data", (chunk) => {
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
  bridge.stdin.on("error", (error) => {
    if (quitting) return;
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
  bridge.on("exit", (code, signal) => {
    debugLog?.log("bridge.exit", { code, signal, quitting });
    if (!quitting) {
      failQueuedUserMessages(state, {
        code: "bridge_disconnected",
        message: "本地 Bridge 已断开，请重启后重试。",
      });
      pushSystemMessage(
        state,
        "bridge exit",
        `后端桥接已退出 code=${code} signal=${signal}`,
        "error",
        { dismissWelcome: true },
      );
      redraw();
    }
    restoreTerminal();
    debugLog?.close();
    process.exit(code ?? 0);
  });

  setupTerminal();
  helloRequestId = rawSend("hello", createHelloPayload());
  redrawScheduler.settleInitial();
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

function setupTerminal() {
  terminalSession.setup({
    onInput: handleKeyInput,
    onResize: handleTerminalResize,
  });
}

function restoreTerminal() {
  heartbeat?.stop();
  workingAnimation.stop();
  redrawScheduler.cancel();
  terminalSession.restore();
}

function exit() {
  if (quitting) return;
  quitting = true;
  logDebug("terminal_ui.exit", {});
  persistUiSnapshot();
  try {
    send("shutdown", {});
  } catch {
    // ignore shutdown write failures
  }
  restoreTerminal();
  debugLog?.close();
  terminateBridge();
  process.exit(0);
}

function installProcessHandlers() {
  process.on("SIGINT", exit);
  process.on("SIGTERM", exit);
  process.on("uncaughtException", handleFatalError);
  process.on("unhandledRejection", handleFatalError);
}

function handleFatalError(reason) {
  if (quitting) return;
  quitting = true;
  const message = safeFatalMessage(reason);
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
  debugLog?.log("protocol.receive.line", { line });
  let record;
  let rawRecord;
  try {
    rawRecord = JSON.parse(line);
    record = normalizeServerRecord(rawRecord);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    debugLog?.log("protocol.receive.error", { line, error: message });
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
  debugLog?.log("protocol.receive.record", { type: record.type, request_id: record.request_id, seq: record.seq, payload: record.payload });
  if (record.type === "pong") {
    heartbeat?.receivePong(record.request_id);
    return;
  }
  const previousSessionId = state.currentSessionId;
  const previousSnapshot = createUiSnapshot(state);
  const actions = reduceServerEvent(state, record);
  if (record.type === "ack" && record.payload?.event === "hello") {
    heartbeat?.start();
    flushDeferredProtocolSends();
  } else if (record.type === "error" && record.request_id === helloRequestId) {
    deferredProtocolSends.length = 0;
    failQueuedUserMessages(state, {
      code: record.payload?.code ?? "protocol_negotiation_failed",
      message: record.payload?.message ?? "终端协议协商失败，请升级后重试。",
    });
  }
  syncWorkingAnimation();
  if (state.currentSessionId !== previousSessionId) {
    setUiSnapshot(uiStateStore, previousSessionId, previousSnapshot);
  }
  if (record.type === "session/replayed") {
    restoreUiSnapshot(state.currentSessionId, { preferLaunchSnapshot: true });
    resetHistorySearch(state);
    jumpTimelineToLatest(state);
    if (state.inspector.open) requestRuntimeInspectorSnapshot();
    if (state.agents.open) requestAgentControlSnapshot();
  }
  if (!(record.type === "ui/message" && record.payload?.type === "thinking" && !state.showReasoning)) {
    markTimelineOutput(state, record, timelineEntryId(record));
  }
  for (const action of actions) {
    if (action.type === "refresh_task_panel") {
      send("task_panel", {
        limit: action.limit ?? 12,
        source: action.source ?? "all",
        status: action.status ?? "all",
        ...(action.detailId ? { detail_id: action.detailId } : {}),
        ...(action.history ? { history: true } : {}),
        pinned: true,
        refresh: true,
      });
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
        session_id: action.sessionId ?? state.currentSessionId,
        known_stream_id: action.knownStreamId ?? state.workbench.stream_id,
        known_revision: action.knownRevision ?? state.workbench.revision,
      });
    }
  }
  if (actions.some((action) => action.type === "exit")) {
    exit();
    return;
  }
  scheduleUiSnapshotPersist();
  scheduleRedraw();
}

function sendWithProtocolGate(type, payload, options = {}) {
  if (state.protocolNegotiated) return rawSend(type, payload, options);
  const id = options.id ? String(options.id) : `ui-deferred-${nextDeferredProtocolId++}`;
  deferredProtocolSends.push({ type, payload, options: { ...options, id } });
  debugLog?.log("protocol.send.deferred", { type, id });
  return id;
}

function flushDeferredProtocolSends() {
  while (deferredProtocolSends.length > 0) {
    const pending = deferredProtocolSends.shift();
    rawSend(pending.type, pending.payload, pending.options);
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
      if (state.interaction) {
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
    exit();
    return;
  }
  if (state.permission) {
    const key = chunk.toLowerCase();
    if (chunk === INPUT_KEYS.ctrlR) return;
    if (chunk === INPUT_KEYS.ctrlI || chunk === INPUT_KEYS.tab) return;
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
  if (state.route?.name === "harness_detail" && handleHarnessDetailKey(state, chunk)) {
    scheduleRedraw();
    return;
  }
  if (state.route?.name === "harness_eval_baseline" && handleHarnessEvalBaselineKey(state, chunk)) {
    scheduleRedraw();
    return;
  }
  if (state.route?.name === "harness_eval_batch" && handleHarnessEvalBatchKey(state, chunk)) {
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
    if (!trackpadScrollFilter.accept(direction)) return;
    scrollTimeline(state, direction === "up" ? 1 : -1);
    persistUiSnapshot();
    scheduleRedraw();
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
    exit();
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
