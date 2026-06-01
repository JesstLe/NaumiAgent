#!/usr/bin/env node
import { spawn } from "node:child_process";
import process from "node:process";
import { ANSI } from "./ansi.js";
import { attachJsonlLineReader, createEventSender, parseArgs, splitShellLike } from "./protocol.js";
import { handleSubmitText, pushSystemMessage, reduceServerEvent, createInitialState } from "./state.js";
import { renderScreen } from "./render.js";

const args = parseArgs(process.argv.slice(2));
const state = createInitialState();

let bridge = null;
let send = null;
let redrawTimer = null;
let quitting = false;

main();

function main() {
  bridge = startBridge();
  send = createEventSender(bridge.stdin);
  attachJsonlLineReader(bridge.stdout, handleBridgeLine);
  bridge.stderr.on("data", (chunk) => {
    pushSystemMessage(state, "bridge stderr", chunk.toString("utf8").trim(), "warning");
  });
  bridge.on("exit", (code, signal) => {
    if (!quitting) {
      pushSystemMessage(state, "bridge exit", `后端桥接已退出 code=${code} signal=${signal}`, "error");
      redraw();
    }
    restoreTerminal();
    process.exit(code ?? 0);
  });

  setupTerminal();
  send("hello", { client: "naumi-terminal-ui" });
  redraw();
}

function startBridge() {
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
  try {
    send("shutdown", {});
  } catch {
    // ignore shutdown write failures
  }
  restoreTerminal();
  bridge?.kill("SIGTERM");
  process.exit(0);
}

function handleBridgeLine(line) {
  if (!line.trim()) return;
  let record;
  try {
    record = JSON.parse(line);
  } catch {
    pushSystemMessage(state, "bridge json", line, "error");
    return;
  }
  const actions = reduceServerEvent(state, record);
  if (actions.some((action) => action.type === "exit")) {
    exit();
    return;
  }
  scheduleRedraw();
}

function handleKeyInput(chunk) {
  if (chunk === "\u0003") exit();
  if (chunk === "\x1b[Z") {
    send("cycle_mode", {});
    return;
  }
  if (state.permission) {
    const key = chunk.toLowerCase();
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
  if (chunk === "\r" || chunk === "\n") {
    const text = state.input.trim();
    if (text) {
      handleSubmitText(state, text, send);
      state.input = "";
      state.scrollOffset = 0;
    }
    scheduleRedraw();
    return;
  }
  if (chunk === "\u007f" || chunk === "\b") {
    state.input = Array.from(state.input).slice(0, -1).join("");
    scheduleRedraw();
    return;
  }
  if (chunk === "\x1b[5~") {
    state.scrollOffset += Math.max(3, Math.floor((process.stdout.rows ?? 24) / 2));
    scheduleRedraw();
    return;
  }
  if (chunk === "\x1b[6~") {
    state.scrollOffset = Math.max(0, state.scrollOffset - Math.max(3, Math.floor((process.stdout.rows ?? 24) / 2)));
    scheduleRedraw();
    return;
  }
  if (chunk >= " " && chunk !== "\x7f") {
    state.input += chunk;
    scheduleRedraw();
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
  const lines = renderScreen(state, width, height, { cwd: process.cwd(), home: process.env.HOME });
  process.stdout.write(ANSI.clear + lines.join("\n"));
}
