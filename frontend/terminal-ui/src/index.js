#!/usr/bin/env node
import { spawn } from "node:child_process";
import process from "node:process";
import { ANSI } from "./ansi.js";
import { bridgeEnvironment, isIgnorableBridgeStderr } from "./bridge-stderr.js";
import { createDebugLog } from "./debug-log.js";
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
  normalizeServerRecord,
  parseArgs,
  parseBridgeCommandJson,
  splitShellLike,
} from "./protocol.js";
import {
  handleSubmitText,
  handleRuntimeInspectorKey,
  hasTaskPanelFocus,
  cancelTaskPanelItem,
  jumpToTaskPanelRecord,
  openSelectedTaskPanelItem,
  pushSystemMessage,
  reduceServerEvent,
  selectTaskPanelOffset,
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
} from "./state.js";
import { captureViewportAnchor, renderScreen, restoreViewportAnchor } from "./render.js";
import {
  jumpTimelineToLatest,
  markTimelineOutput,
  scrollTimeline,
} from "./timeline-follow.js";
import {
  getProjectInputHistory,
  getUiSnapshot,
  loadUiStateStore,
  saveUiStateStore,
  setProjectInputHistory,
  setUiSnapshot,
} from "./ui-state-store.js";

const args = parseArgs(process.argv.slice(2));
const state = createInitialState();
const uiStateStore = loadUiStateStore(process.cwd());
state.inputHistory = getProjectInputHistory(uiStateStore);
const debugLog = createDebugLog({ cwd: process.cwd(), env: process.env });
state.frontendDebugLogPath = debugLog?.path ?? "";

let bridge = null;
let send = null;
let redrawTimer = null;
let uiSnapshotTimer = null;
let inputEscapeTimer = null;
let quitting = false;
let viewportWidth = null;
let viewportHeight = null;
const inputTokenizer = createInputTokenizerState();

main();

function main() {
  debugLog?.log("terminal_ui.state", { frontend_debug_log_path: state.frontendDebugLogPath });
  bridge = startBridge();
  send = createEventSender(bridge.stdin, { debugLog });
  attachJsonlLineReader(bridge.stdout, handleBridgeLine);
  bridge.stderr.on("data", (chunk) => {
    const lines = chunk.toString("utf8").split(/\r?\n/);
    for (const rawLine of lines) {
      const text = rawLine.trim();
      if (!text) continue;
      const ignored = isIgnorableBridgeStderr(text);
      debugLog?.log("bridge.stderr", { text, ignored });
      if (!ignored) {
        pushSystemMessage(state, "bridge stderr", text, "warning");
      }
    }
  });
  bridge.stdin.on("error", (error) => {
    if (quitting) return;
    failQueuedUserMessages(state, {
      code: "bridge_write_failed",
      message: "无法写入本地 Bridge，请检查后端进程后重试。",
    });
    pushSystemMessage(state, "bridge stdin", `本地 Bridge 写入失败: ${error.message}`, "error");
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
      pushSystemMessage(state, "bridge exit", `后端桥接已退出 code=${code} signal=${signal}`, "error");
      redraw();
    }
    restoreTerminal();
    debugLog?.close();
    process.exit(code ?? 0);
  });

  setupTerminal();
  restoreUiSnapshot(state.currentSessionId);
  send("hello", { client: "naumi-terminal-ui" });
  if (state.inspector.open) requestRuntimeInspectorSnapshot();
  redraw();
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
  process.stdout.write(
    ANSI.altOn + ANSI.bracketedPasteOn + ANSI.keyboardDisambiguateOn + ANSI.hideCursor,
  );
  if (process.stdin.isTTY) {
    process.stdin.setRawMode(true);
  }
  process.stdin.resume();
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", handleKeyInput);
  process.stdout.on("resize", handleTerminalResize);
  process.on("SIGINT", exit);
  process.on("SIGTERM", exit);
}

function restoreTerminal() {
  process.stdout.write(
    ANSI.keyboardDisambiguateOff
      + ANSI.bracketedPasteOff
      + ANSI.showCursor
      + ANSI.altOff
      + ANSI.reset,
  );
  if (process.stdin.isTTY) {
    process.stdin.setRawMode(false);
  }
}

function exit() {
  if (quitting) return;
  quitting = true;
  debugLog?.log("terminal_ui.exit", {});
  persistUiSnapshot();
  try {
    send("shutdown", {});
  } catch {
    // ignore shutdown write failures
  }
  restoreTerminal();
  debugLog?.close();
  bridge?.kill("SIGTERM");
  process.exit(0);
}

function handleBridgeLine(line) {
  if (!line.trim()) return;
  debugLog?.log("protocol.receive.line", { line });
  let record;
  try {
    record = normalizeServerRecord(JSON.parse(line));
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    debugLog?.log("protocol.receive.error", { line, error: message });
    pushSystemMessage(state, "bridge protocol", message, "error");
    return;
  }
  debugLog?.log("protocol.receive.record", { type: record.type, request_id: record.request_id, seq: record.seq, payload: record.payload });
  const previousSessionId = state.currentSessionId;
  const previousSnapshot = createUiSnapshot(state);
  const actions = reduceServerEvent(state, record);
  if (state.currentSessionId !== previousSessionId) {
    setUiSnapshot(uiStateStore, previousSessionId, previousSnapshot);
    restoreUiSnapshot(state.currentSessionId);
    if (state.inspector.open) requestRuntimeInspectorSnapshot();
  }
  if (record.type === "session/replayed") {
    resetHistorySearch(state);
    jumpTimelineToLatest(state);
    if (state.inspector.open && state.currentSessionId === previousSessionId) {
      requestRuntimeInspectorSnapshot();
    }
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
  }
  if (actions.some((action) => action.type === "exit")) {
    exit();
    return;
  }
  scheduleUiSnapshotPersist();
  scheduleRedraw();
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
      if (state.historySearch?.open) {
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
      send("permission_response", { request_id: state.permission.requestId, choice: "allow" });
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
  if (chunk === INPUT_KEYS.upAlt) {
    adjustScrollOffset(state, "up");
    persistUiSnapshot();
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.downAlt) {
    adjustScrollOffset(state, "down");
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
  handleSubmitText(state, text, send);
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

function adjustScrollOffset(state, direction) {
  const step = Math.max(3, Math.floor((process.stdout.rows ?? 24) / 2));
  if (direction === "up") {
    scrollTimeline(state, step);
  } else {
    scrollTimeline(state, -step);
  }
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
  if (chunk === "\t" || key === "n") {
    selectTaskPanelOffset(state, 1);
    return true;
  }
  if (key === "p") {
    selectTaskPanelOffset(state, -1);
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

function restoreUiSnapshot(sessionId) {
  resetHistorySearch(state);
  resetSlashCompletion(state);
  applyUiSnapshot(state, getUiSnapshot(uiStateStore, sessionId));
}

function requestRuntimeInspectorSnapshot() {
  state.inspector.loading = true;
  send("inspector/request", {
    open: true,
    known_revision: state.inspector.revision,
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
    pushSystemMessage(state, "ui state", `无法保存终端 UI 状态: ${error.message}`, "warning");
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
      pushSystemMessage(state, "ui state", `无法保存终端 UI 状态: ${error.message}`, "warning");
      scheduleRedraw();
    }
  }, 100);
}

function scheduleRedraw() {
  if (redrawTimer) return;
  redrawTimer = setTimeout(() => {
    redrawTimer = null;
    redraw();
  }, 16);
}

function handleTerminalResize() {
  const width = Math.max(60, process.stdout.columns ?? 100);
  const height = Math.max(12, process.stdout.rows ?? 30);
  if (viewportWidth === null || viewportHeight === null) {
    viewportWidth = width;
    viewportHeight = height;
    scheduleRedraw();
    return;
  }
  if (width === viewportWidth && height === viewportHeight) return;

  const previousWidth = viewportWidth;
  const previousHeight = viewportHeight;
  const anchor = captureViewportAnchor(
    state,
    previousWidth,
    previousHeight,
    { cwd: process.cwd(), home: process.env.HOME },
  );
  const previousOffset = state.scrollOffset;
  restoreViewportAnchor(
    state,
    anchor,
    width,
    height,
    { cwd: process.cwd(), home: process.env.HOME },
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
    const lines = renderScreen(state, width, height, { cwd: process.cwd(), home: process.env.HOME });
    viewportWidth = width;
    viewportHeight = height;
    debugLog?.log("render.screen", {
      width,
      height,
      line_count: lines.length,
      messages: state.messages.length,
      running: state.running,
      mode: state.mode,
      scroll_offset: state.scrollOffset,
      follow_tail: state.followTail,
      unread_output_count: state.unreadOutputCount,
    });
    process.stdout.write(ANSI.clear + lines.join("\n"));
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
