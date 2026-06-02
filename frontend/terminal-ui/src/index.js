#!/usr/bin/env node
import { spawn } from "node:child_process";
import process from "node:process";
import { ANSI } from "./ansi.js";
import { createDebugLog } from "./debug-log.js";
import {
  INPUT_KEYS,
  backspaceInput,
  clearInput,
  deleteInputForward,
  insertInputText,
  moveInputCursor,
  navigateInputHistory,
  rememberSubmittedInput,
  splitInputChunk,
} from "./input-buffer.js";
import {
  attachJsonlLineReader,
  createEventSender,
  parseArgs,
  parseBridgeCommandJson,
  splitShellLike,
} from "./protocol.js";
import { handleSubmitText, pushSystemMessage, reduceServerEvent, createInitialState, createUiSnapshot, applyUiSnapshot } from "./state.js";
import { renderScreen } from "./render.js";
import { getUiSnapshot, loadUiStateStore, saveUiStateStore, setUiSnapshot } from "./ui-state-store.js";

const args = parseArgs(process.argv.slice(2));
const state = createInitialState();
const uiStateStore = loadUiStateStore(process.cwd());
const debugLog = createDebugLog({ cwd: process.cwd(), env: process.env });
state.frontendDebugLogPath = debugLog?.path ?? "";

let bridge = null;
let send = null;
let redrawTimer = null;
let quitting = false;

main();

function main() {
  debugLog?.log("terminal_ui.state", { frontend_debug_log_path: state.frontendDebugLogPath });
  bridge = startBridge();
  send = createEventSender(bridge.stdin, { debugLog });
  attachJsonlLineReader(bridge.stdout, handleBridgeLine);
  bridge.stderr.on("data", (chunk) => {
    const text = chunk.toString("utf8").trim();
    debugLog?.log("bridge.stderr", { text });
    pushSystemMessage(state, "bridge stderr", text, "warning");
  });
  bridge.on("exit", (code, signal) => {
    debugLog?.log("bridge.exit", { code, signal, quitting });
    if (!quitting) {
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
  redraw();
}

function startBridge() {
  if (args.bridgeCommandJson) {
    const [cmd, ...cmdArgs] = parseBridgeCommandJson(args.bridgeCommandJson);
    return spawn(cmd, cmdArgs, { stdio: ["pipe", "pipe", "pipe"], cwd: process.cwd() });
  }
  if (args.bridgeCommand) {
    const [cmd, ...cmdArgs] = splitShellLike(args.bridgeCommand);
    return spawn(cmd, cmdArgs, { stdio: ["pipe", "pipe", "pipe"], cwd: process.cwd() });
  }
  return spawn(
    "uv",
    ["run", "python", "-m", "naumi_agent.ui.bridge", "--config", args.config],
    { stdio: ["pipe", "pipe", "pipe"], cwd: process.cwd() },
  );
}

function setupTerminal() {
  process.stdout.write(ANSI.altOn + ANSI.hideCursor);
  if (process.stdin.isTTY) {
    process.stdin.setRawMode(true);
  }
  process.stdin.resume();
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", handleKeyInput);
  process.stdout.on("resize", scheduleRedraw);
  process.on("SIGINT", exit);
  process.on("SIGTERM", exit);
}

function restoreTerminal() {
  process.stdout.write(ANSI.showCursor + ANSI.altOff + ANSI.reset);
  if (process.stdin.isTTY) {
    process.stdin.setRawMode(false);
  }
}

function exit() {
  if (quitting) return;
  quitting = true;
  debugLog?.log("terminal_ui.exit", {});
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
    record = JSON.parse(line);
  } catch {
    debugLog?.log("protocol.receive.error", { line, error: "JSON.parse failed" });
    pushSystemMessage(state, "bridge json", line, "error");
    return;
  }
  debugLog?.log("protocol.receive.record", { type: record.type, request_id: record.request_id, seq: record.seq, payload: record.payload });
  const actions = reduceServerEvent(state, record);
  for (const action of actions) {
    if (action.type === "session_replayed") {
      restoreUiSnapshot(action.sessionId);
    }
  }
  if (actions.some((action) => action.type === "exit")) {
    exit();
    return;
  }
  persistUiSnapshot();
  scheduleRedraw();
}

function handleKeyInput(chunk) {
  debugLog?.log("input.chunk", {
    chars: String(chunk),
    char_count: Array.from(String(chunk)).length,
  });
  for (const key of splitInputChunk(chunk)) {
    handleSingleKeyInput(key);
  }
}

function handleSingleKeyInput(chunk) {
  if (chunk === "\u0003") exit();
  if (state.permission) {
    const key = chunk.toLowerCase();
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
  if (chunk === INPUT_KEYS.shiftTab) {
    send("cycle_mode", {});
    return;
  }
  if (chunk === "\r" || chunk === "\n") {
    const text = state.input.trim();
    if (text) {
      handleSubmitText(state, text, send);
      rememberSubmittedInput(state, text);
      clearInput(state);
      state.scrollOffset = 0;
      persistUiSnapshot();
    }
    scheduleRedraw();
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
  if (chunk === INPUT_KEYS.left) {
    moveInputCursor(state, "left");
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.right) {
    moveInputCursor(state, "right");
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.up) {
    navigateInputHistory(state, "up");
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.down) {
    navigateInputHistory(state, "down");
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.home || chunk === INPUT_KEYS.homeAlt || chunk === INPUT_KEYS.ctrlA) {
    moveInputCursor(state, "home");
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.end || chunk === INPUT_KEYS.endAlt || chunk === INPUT_KEYS.ctrlE) {
    moveInputCursor(state, "end");
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.pageUp) {
    state.scrollOffset += Math.max(3, Math.floor((process.stdout.rows ?? 24) / 2));
    persistUiSnapshot();
    scheduleRedraw();
    return;
  }
  if (chunk === INPUT_KEYS.pageDown) {
    state.scrollOffset = Math.max(0, state.scrollOffset - Math.max(3, Math.floor((process.stdout.rows ?? 24) / 2)));
    persistUiSnapshot();
    scheduleRedraw();
    return;
  }
  if (chunk >= " " && chunk !== "\x7f") {
    insertInputText(state, chunk);
    scheduleRedraw();
  }
}

function restoreUiSnapshot(sessionId) {
  applyUiSnapshot(state, getUiSnapshot(uiStateStore, sessionId));
}

function persistUiSnapshot() {
  setUiSnapshot(uiStateStore, state.currentSessionId, createUiSnapshot(state));
  try {
    saveUiStateStore(uiStateStore);
  } catch (error) {
    pushSystemMessage(state, "ui state", `无法保存终端 UI 状态: ${error.message}`, "warning");
  }
}

function scheduleRedraw() {
  if (redrawTimer) return;
  redrawTimer = setTimeout(() => {
    redrawTimer = null;
    redraw();
  }, 16);
}

function redraw() {
  const width = Math.max(60, process.stdout.columns ?? 100);
  const height = Math.max(12, process.stdout.rows ?? 30);
  try {
    const lines = renderScreen(state, width, height, { cwd: process.cwd(), home: process.env.HOME });
    debugLog?.log("render.screen", {
      width,
      height,
      line_count: lines.length,
      messages: state.messages.length,
      running: state.running,
      mode: state.mode,
      scroll_offset: state.scrollOffset,
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
