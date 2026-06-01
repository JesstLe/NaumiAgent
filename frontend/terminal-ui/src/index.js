#!/usr/bin/env node
import { spawn } from "node:child_process";
import process from "node:process";
import { StringDecoder } from "node:string_decoder";

const ANSI = {
  clear: "\x1b[2J\x1b[H",
  hideCursor: "\x1b[?25l",
  showCursor: "\x1b[?25h",
  altOn: "\x1b[?1049h",
  altOff: "\x1b[?1049l",
  reset: "\x1b[0m",
  dim: "\x1b[2m",
  bold: "\x1b[1m",
  red: "\x1b[31m",
  green: "\x1b[32m",
  yellow: "\x1b[33m",
  cyan: "\x1b[36m",
  magenta: "\x1b[35m",
  blue: "\x1b[34m",
};

const args = parseArgs(process.argv.slice(2));
const state = {
  input: "",
  mode: "default",
  status: {},
  messages: [],
  tools: [],
  activeAssistant: null,
  activeThinking: null,
  activeToolPrepare: null,
  todo: null,
  permission: null,
  running: false,
  scrollOffset: 0,
  bridgeReady: false,
  debugTrace: null,
};

let nextClientId = 1;
let bridge = null;
let redrawTimer = null;
let quitting = false;

main();

function main() {
  bridge = startBridge();
  attachJsonlLineReader(bridge.stdout, handleBridgeLine);
  bridge.stderr.on("data", (chunk) => {
    pushSystemMessage("bridge stderr", chunk.toString("utf8").trim(), "warning");
  });
  bridge.on("exit", (code, signal) => {
    if (!quitting) {
      pushSystemMessage("bridge exit", `后端桥接已退出 code=${code} signal=${signal}`, "error");
      redraw();
    }
    restoreTerminal();
    process.exit(code ?? 0);
  });

  setupTerminal();
  send("hello", { client: "naumi-terminal-ui" });
  redraw();
}

function parseArgs(argv) {
  const parsed = { config: "config.yaml", bridgeCommand: "" };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if ((arg === "--config" || arg === "-c") && argv[i + 1]) {
      parsed.config = argv[i + 1];
      i += 1;
    } else if (arg === "--bridge-command" && argv[i + 1]) {
      parsed.bridgeCommand = argv[i + 1];
      i += 1;
    }
  }
  return parsed;
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

function splitShellLike(command) {
  return command.match(/(?:[^\s"]+|"[^"]*")+/g)?.map((part) => part.replace(/^"|"$/g, "")) ?? [];
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

function send(type, payload) {
  const record = {
    id: `ui-${nextClientId++}`,
    type,
    version: 1,
    payload,
  };
  bridge.stdin.write(`${JSON.stringify(record)}\n`);
  return record.id;
}

function attachJsonlLineReader(stream, onLine) {
  const decoder = new StringDecoder("utf8");
  let buffer = "";
  stream.on("data", (chunk) => {
    buffer += typeof chunk === "string" ? chunk : decoder.write(chunk);
    while (true) {
      const index = buffer.indexOf("\n");
      if (index < 0) return;
      const line = buffer.slice(0, index).replace(/\r$/, "");
      buffer = buffer.slice(index + 1);
      onLine(line);
    }
  });
}

function handleBridgeLine(line) {
  if (!line.trim()) return;
  let record;
  try {
    record = JSON.parse(line);
  } catch {
    pushSystemMessage("bridge json", line, "error");
    return;
  }
  handleServerEvent(record);
  scheduleRedraw();
}

function handleServerEvent(record) {
  const payload = record.payload ?? {};
  switch (record.type) {
    case "ready":
      state.bridgeReady = true;
      mergeStatus(payload);
      pushSystemMessage("ready", "新终端 UI 已连接 Python bridge。", "info");
      break;
    case "debug/trace":
      state.debugTrace = payload;
      pushSystemMessage("debug", `调试日志: ${payload.events_path ?? "-"}`, "info");
      break;
    case "runtime/status":
      mergeStatus(payload);
      break;
    case "mode/changed":
      state.mode = payload.mode ?? state.mode;
      mergeStatus(payload.status ?? {});
      break;
    case "user/message":
      state.messages.push({ kind: "user", content: payload.content ?? "" });
      state.running = true;
      break;
    case "ui/message":
      handleUiMessage(payload);
      break;
    case "permission/request":
      state.permission = { requestId: record.request_id, payload };
      break;
    case "permission/resolved":
      state.permission = null;
      pushSystemMessage("permission", `权限已处理: ${payload.choice}`, "info");
      break;
    case "run/started":
      state.running = true;
      break;
    case "run/completed":
      state.running = false;
      state.activeToolPrepare = null;
      state.permission = null;
      break;
    case "session/replayed":
      if (payload.clear !== false) {
        state.messages = [];
        state.tools = [];
        state.activeAssistant = null;
        state.activeThinking = null;
      }
      pushSystemMessage("resume", `已恢复会话: ${payload.title ?? payload.session_id}`, "info");
      break;
    case "error":
      state.running = false;
      pushSystemMessage("error", payload.message ?? "未知错误", "error");
      break;
    case "shutdown":
      exit();
      break;
    default:
      break;
  }
}

function mergeStatus(payload) {
  if (!payload || typeof payload !== "object") return;
  state.status = { ...state.status, ...payload };
  if (payload.mode) {
    state.mode = payload.mode;
  }
}

function handleUiMessage(message) {
  switch (message.type) {
    case "assistant_stream":
      handleAssistantStream(message);
      break;
    case "thinking":
      handleThinking(message);
      break;
    case "tool_prepare":
      handleToolPrepare(message);
      break;
    case "tool_use":
      handleToolUse(message);
      break;
    case "tool_result":
      handleToolResult(message);
      break;
    case "todo_status":
      handleTodoStatus(message);
      break;
    case "permission_bubble":
      state.messages.push({ kind: "permission", message });
      break;
    case "runtime_status":
      if (message.phase === "perf_phase") {
        state.activeToolPrepare = `${message.label}: ${message.duration_ms}ms`;
      }
      break;
    case "recovery":
    case "context_compact":
    case "runtime_notification":
    case "subagent_event":
    case "team_event":
    case "hook_trace":
    case "error":
      state.messages.push({ kind: message.type, message });
      break;
    default:
      break;
  }
}

function handleAssistantStream(message) {
  if (message.phase === "start") {
    state.activeAssistant = { kind: "assistant", content: "" };
    state.messages.push(state.activeAssistant);
  } else if (message.phase === "token") {
    if (!state.activeAssistant) {
      state.activeAssistant = { kind: "assistant", content: "" };
      state.messages.push(state.activeAssistant);
    }
    state.activeAssistant.content += message.content ?? "";
  } else if (message.phase === "end") {
    state.activeAssistant = null;
  }
}

function handleThinking(message) {
  if (message.phase === "start") {
    state.activeThinking = { kind: "thinking", content: "", done: false };
    state.messages.push(state.activeThinking);
  } else if (message.phase === "delta") {
    if (!state.activeThinking) {
      state.activeThinking = { kind: "thinking", content: "", done: false };
      state.messages.push(state.activeThinking);
    }
    state.activeThinking.content += message.content ?? "";
  } else if (message.phase === "end" && state.activeThinking) {
    state.activeThinking.content = message.content || state.activeThinking.content;
    state.activeThinking.done = true;
    state.activeThinking = null;
  }
}

function handleToolPrepare(message) {
  if (message.phase === "end") {
    state.activeToolPrepare = null;
    return;
  }
  const parts = [`准备 ${message.tool_name || "tool"}`];
  if (message.path) parts.push(message.path);
  if (message.content_lines) parts.push(`${message.content_lines} 行`);
  if (message.argument_chars) parts.push(`${message.argument_chars} 字符参数`);
  if (message.elapsed_ms > 1000) parts.push(`${(message.elapsed_ms / 1000).toFixed(1)}s`);
  state.activeToolPrepare = parts.join(" · ");
}

function handleToolUse(message) {
  const tool = {
    kind: "tool",
    callId: message.tool_call_id || "",
    name: message.tool_name,
    primary: message.primary_arg || message.file_path || message.command || message.query || message.url || "",
    status: "running",
    durationMs: 0,
    output: "",
  };
  state.tools.push(tool);
  state.messages.push(tool);
}

function handleToolResult(message) {
  const tool = [...state.tools].reverse().find((item) => {
    if (item.status !== "running") return false;
    if (message.tool_call_id) return item.callId === message.tool_call_id;
    if (!message.tool_name) return true;
    return item.name === message.tool_name;
  });
  const target = tool ?? {
    kind: "tool",
    callId: message.tool_call_id || "",
    name: message.tool_name,
    primary: "",
    status: "running",
    durationMs: 0,
    output: "",
  };
  if (!tool) {
    state.tools.push(target);
    state.messages.push(target);
  }
  target.status = message.status;
  target.durationMs = message.duration_ms;
  target.output = message.content_preview ?? "";
  target.outputLength = message.content_length ?? 0;
}

function handleTodoStatus(message) {
  if ((message.open_count ?? 0) <= 0) {
    state.todo = null;
    return;
  }
  const items = Array.isArray(message.items) ? message.items : [];
  const priority = { in_progress: 0, blocked: 1, pending: 2 };
  const current = items
    .filter((item) => item && item.status !== "completed")
    .sort((a, b) => (priority[a.status] ?? 9) - (priority[b.status] ?? 9))[0];
  state.todo = {
    total: message.total_count ?? 0,
    completed: message.completed_count ?? 0,
    current,
  };
}

function pushSystemMessage(title, content, level) {
  if (!content) return;
  state.messages.push({ kind: "system", title, content, level });
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
      handleSubmitText(text);
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

function handleSubmitText(text) {
  if (text === "/resume" || text === "/r") {
    send("resume", {});
    return;
  }
  if (text.startsWith("/load ")) {
    send("resume", { session_id: text.slice(6).trim() });
    return;
  }
  if (text.startsWith("/mode ")) {
    send("set_mode", { mode: text.slice(6).trim() });
    return;
  }
  if (text === "/clear") {
    state.messages = [];
    state.tools = [];
    return;
  }
  send("submit", { text });
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
  const footer = renderFooter(width);
  const footerHeight = footer.length;
  const bodyHeight = Math.max(1, height - footerHeight);
  const bodyLines = renderBody(width);
  const start = Math.max(0, bodyLines.length - bodyHeight - state.scrollOffset);
  const visible = bodyLines.slice(start, start + bodyHeight);
  while (visible.length < bodyHeight) visible.push("");
  process.stdout.write(ANSI.clear + visible.map((line) => padRight(line, width)).join("\n"));
  process.stdout.write("\n" + footer.map((line) => padRight(line, width)).join("\n"));
}

function renderBody(width) {
  const lines = [];
  for (const message of state.messages) {
    lines.push(...renderMessage(message, width));
  }
  if (state.activeToolPrepare) {
    lines.push(color(ANSI.dim, `tool prepare: ${state.activeToolPrepare}`));
  }
  if (state.running) {
    lines.push(color(ANSI.dim, "运行中..."));
  }
  return lines.flatMap((line) => wrapAnsiLine(line, width));
}

function renderMessage(message, width) {
  if (message.kind === "user") {
    return ["", `${color(ANSI.green, ">")} ${message.content}`];
  }
  if (message.kind === "assistant") {
    return ["", ...renderMarkdownExcerpt(message.content, width)];
  }
  if (message.kind === "thinking") {
    const content = compactText(message.content || "思考中...");
    const label = message.done ? "thinking" : "thinking...";
    return ["", color(ANSI.dim, `${label}: ${content}`)];
  }
  if (message.kind === "tool") {
    return renderToolCard(message, width);
  }
  if (message.kind === "permission") {
    return ["", color(ANSI.yellow, `permission: ${message.message.tool_name} · ${message.message.status}`)];
  }
  if (message.kind === "system") {
    const style = message.level === "error" ? ANSI.red : message.level === "warning" ? ANSI.yellow : ANSI.dim;
    return ["", color(style, `${message.title}: ${message.content}`)];
  }
  return ["", color(ANSI.dim, `${message.kind}: ${JSON.stringify(message.message ?? {})}`)];
}

function renderToolCard(tool, width) {
  const title = `${tool.name}${tool.primary ? ` ${tool.primary}` : ""}`;
  const statusStyle = tool.status === "success" ? ANSI.green : tool.status === "running" ? ANSI.cyan : ANSI.red;
  const titleLine = `${color(statusStyle, tool.status === "running" ? "running" : tool.status)} ${title}`;
  const output = tool.output ? renderToolOutput(tool.output, width - 4) : [];
  const inner = [titleLine, ...output];
  if (tool.outputLength > (tool.output?.length ?? 0)) {
    inner.push(color(ANSI.dim, `... 已截断，完整输出 ${tool.outputLength} 字符`));
  }
  return boxLines("tool", inner, width);
}

function renderToolOutput(text, width) {
  if (looksLikeDiff(text)) {
    return text.split("\n").slice(0, 60).map(colorDiffLine);
  }
  return renderMarkdownExcerpt(text, width).slice(0, 60);
}

function renderMarkdownExcerpt(text, width) {
  const lines = [];
  const raw = String(text ?? "").split("\n");
  let inCode = false;
  let codeLineCount = 0;
  let omitted = 0;
  for (const line of raw) {
    if (line.startsWith("```")) {
      if (inCode && omitted) {
        lines.push(color(ANSI.dim, `... 已隐藏 ${omitted} 行代码`));
        omitted = 0;
      }
      inCode = !inCode;
      codeLineCount = 0;
      lines.push(color(ANSI.dim, line));
      continue;
    }
    if (inCode) {
      if (codeLineCount < 40) {
        lines.push(colorCodeLine(line));
      } else {
        omitted += 1;
      }
      codeLineCount += 1;
      continue;
    }
    lines.push(line);
  }
  if (omitted) {
    lines.push(color(ANSI.dim, `... 已隐藏 ${omitted} 行代码`));
  }
  return lines.flatMap((line) => wrapAnsiLine(line, width));
}

function renderFooter(width) {
  const lines = [];
  if (state.permission) {
    const tool = state.permission.payload.tool_name ?? "tool";
    const reason = compactText(state.permission.payload.reason ?? "");
    lines.push(color(ANSI.yellow, `permission: ${tool}  y=允许 n=拒绝 b=bypass  ${reason}`));
  }
  if (state.todo) {
    const current = state.todo.current;
    const currentText = current ? `#${current.id} ${current.subject}` : "有未完成任务";
    lines.push(color(ANSI.cyan, `todo: ${state.todo.completed}/${state.todo.total} 完成 | ${currentText}`));
  }
  const status = state.status ?? {};
  const context = status.context ?? {};
  const budget = status.budget ?? {};
  const git = status.git ?? {};
  const parts = [
    `mode: ${state.mode}`,
    status.model || "model: -",
    `工作区: ${shortPath(status.workspace_root || process.cwd())}`,
    `Token: ${status.usage?.total_tokens ?? 0}`,
    `上下文: ${formatContext(context)}`,
    `预算: ${formatMoney(budget.used_usd)}/${formatMoney(budget.max_usd)}`,
  ];
  if (git.branch) parts.push(`${git.branch}${git.dirty ? "*" : ""}`);
  lines.push(color(ANSI.dim, truncateAnsi(parts.join(" | "), width)));
  lines.push(`${color(ANSI.green, state.mode)} ${state.running ? color(ANSI.dim, "running") : ">"} ${state.input}`);
  lines.push(color(ANSI.dim, "Shift+Tab 切换模式 · Enter 发送 · PageUp/PageDown 滚动 · Ctrl+C 退出"));
  return lines.flatMap((line) => wrapAnsiLine(line, width));
}

function boxLines(title, inner, width) {
  const boxWidth = Math.max(30, width - 2);
  const top = `+ ${title} ${"-".repeat(Math.max(0, boxWidth - visibleWidth(title) - 4))}+`;
  const bottom = `+${"-".repeat(Math.max(0, boxWidth - 1))}+`;
  const body = inner.flatMap((line) => wrapAnsiLine(line, boxWidth - 4)).map((line) => {
    const rawPad = Math.max(0, boxWidth - 4 - visibleWidth(line));
    return `| ${line}${" ".repeat(rawPad)} |`;
  });
  return ["", color(ANSI.blue, top), ...body, color(ANSI.blue, bottom)];
}

function color(style, text) {
  return `${style}${text}${ANSI.reset}`;
}

function colorDiffLine(line) {
  if (line.startsWith("+") && !line.startsWith("+++")) return color(ANSI.green, line);
  if (line.startsWith("-") && !line.startsWith("---")) return color(ANSI.red, line);
  if (line.startsWith("@@")) return color(ANSI.magenta, line);
  return color(ANSI.dim, line);
}

function colorCodeLine(line) {
  let result = line
    .replace(/\b(class|def|function|const|let|var|return|if|else|for|while|import|from|async|await)\b/g, `${ANSI.cyan}$1${ANSI.reset}`)
    .replace(/\b(True|False|None|null|undefined)\b/g, `${ANSI.yellow}$1${ANSI.reset}`);
  if (/^\s*(#|\/\/)/.test(line)) result = color(ANSI.dim, line);
  return result;
}

function looksLikeDiff(text) {
  const sample = String(text).split("\n").slice(0, 20);
  return sample.some((line) => line.startsWith("@@") || line.startsWith("---") || line.startsWith("+++"));
}

function compactText(text) {
  return String(text).replace(/\s+/g, " ").trim().slice(0, 180);
}

function formatContext(context) {
  const used = Number(context.used ?? 0);
  const window = Number(context.window ?? 0);
  const percent = context.percentage ?? 0;
  return `${Math.round(used / 1000)}K/${Math.round(window / 1000)}K (${percent}%)`;
}

function formatMoney(value) {
  const num = Number(value ?? 0);
  return `$${num.toFixed(4)}`;
}

function shortPath(value) {
  const home = process.env.HOME;
  if (home && value.startsWith(home)) return `~${value.slice(home.length)}`;
  return value;
}

function wrapAnsiLine(line, width) {
  const result = [];
  let remaining = String(line ?? "");
  while (visibleWidth(remaining) > width) {
    let take = 0;
    let visible = 0;
    let ansi = false;
    for (let i = 0; i < remaining.length; i += 1) {
      const ch = remaining[i];
      if (ch === "\x1b") ansi = true;
      if (!ansi) visible += charWidth(ch);
      if (ansi && ch === "m") ansi = false;
      if (visible >= width) {
        take = i + 1;
        break;
      }
    }
    if (take <= 0) break;
    result.push(remaining.slice(0, take));
    remaining = remaining.slice(take);
  }
  result.push(remaining);
  return result;
}

function truncateAnsi(line, width) {
  if (visibleWidth(line) <= width) return line;
  return `${stripAnsi(line).slice(0, Math.max(0, width - 1))}…`;
}

function padRight(line, width) {
  return line + " ".repeat(Math.max(0, width - visibleWidth(line)));
}

function visibleWidth(text) {
  return Array.from(stripAnsi(String(text))).reduce((sum, ch) => sum + charWidth(ch), 0);
}

function charWidth(ch) {
  return /[\u1100-\u115f\u2e80-\ua4cf\uf900-\ufaff\ufe10-\ufe19\ufe30-\ufe6f\uff00-\uff60\uffe0-\uffe6]/.test(ch) ? 2 : 1;
}

function stripAnsi(text) {
  return String(text).replace(/\x1b\[[0-9;?]*[A-Za-z]/g, "");
}
