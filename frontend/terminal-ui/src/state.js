import { looksLikeDiff } from "./ansi.js";
import { isFoldExpanded, setFoldExpanded } from "./components/folds.js";

export function createInitialState() {
  return {
    nextMessageId: 1,
    currentSessionId: "",
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
    folds: {},
    foldCursor: 0,
  };
}

export function reduceServerEvent(state, record) {
  const payload = record.payload ?? {};
  switch (record.type) {
    case "ready":
      state.bridgeReady = true;
      mergeStatus(state, payload);
      pushSystemMessage(state, "ready", "新终端 UI 已连接 Python bridge。", "info");
      break;
    case "debug/trace":
      state.debugTrace = payload;
      pushSystemMessage(state, "debug", `调试日志: ${payload.events_path ?? "-"}`, "info");
      break;
    case "runtime/status":
      mergeStatus(state, payload);
      break;
    case "mode/changed":
      state.mode = payload.mode ?? state.mode;
      mergeStatus(state, payload.status ?? {});
      break;
    case "user/message":
      state.messages.push({ kind: "user", content: payload.content ?? "" });
      state.running = true;
      break;
    case "ui/message":
      handleUiMessage(state, payload);
      break;
    case "permission/request":
      state.permission = { requestId: record.request_id, payload };
      break;
    case "permission/resolved":
      state.permission = null;
      pushSystemMessage(state, "permission", `权限已处理: ${payload.choice}`, "info");
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
      state.currentSessionId = payload.session_id || state.currentSessionId;
      if (payload.clear !== false) {
        state.messages = [];
        state.tools = [];
        state.activeAssistant = null;
        state.activeThinking = null;
        state.folds = {};
        state.foldCursor = 0;
      }
      pushSystemMessage(state, "resume", `已恢复会话: ${payload.title ?? payload.session_id}`, "info");
      return [{ type: "session_replayed", sessionId: state.currentSessionId }];
    case "error":
      state.running = false;
      pushSystemMessage(state, "error", payload.message ?? "未知错误", "error");
      break;
    case "shutdown":
      return [{ type: "exit" }];
    default:
      break;
  }
  return [];
}

export function mergeStatus(state, payload) {
  if (!payload || typeof payload !== "object") return;
  state.status = { ...state.status, ...payload };
  if (payload.mode) {
    state.mode = payload.mode;
  }
}

export function handleUiMessage(state, message) {
  switch (message.type) {
    case "user":
      state.messages.push({ kind: "user", content: message.content ?? "", isCommand: Boolean(message.is_command) });
      break;
    case "assistant_stream":
      handleAssistantStream(state, message);
      break;
    case "thinking":
      handleThinking(state, message);
      break;
    case "tool_prepare":
      handleToolPrepare(state, message);
      break;
    case "tool_use":
      handleToolUse(state, message);
      break;
    case "tool_result":
      handleToolResult(state, message);
      break;
    case "todo_status":
      handleTodoStatus(state, message);
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
    case "system_notice":
      pushSystemMessage(state, message.title ?? "notice", message.content ?? message.message ?? "", "info");
      break;
    default:
      break;
  }
}

export function handleAssistantStream(state, message) {
  if (message.phase === "start") {
    state.activeAssistant = { kind: "assistant", id: nextMessageId(state, "assistant"), content: "" };
    state.messages.push(state.activeAssistant);
  } else if (message.phase === "token") {
    if (!state.activeAssistant) {
      const assistant = { kind: "assistant", id: nextMessageId(state, "assistant"), content: message.content ?? "" };
      state.messages.push(assistant);
      if (state.running) {
        state.activeAssistant = assistant;
      }
      return;
    }
    state.activeAssistant.content += message.content ?? "";
  } else if (message.phase === "end") {
    state.activeAssistant = null;
  }
}

export function handleThinking(state, message) {
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

export function handleToolPrepare(state, message) {
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

export function handleToolUse(state, message) {
  const tool = {
    kind: "tool",
    id: nextMessageId(state, "tool"),
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

export function handleToolResult(state, message) {
  const tool = [...state.tools].reverse().find((item) => {
    if (item.status !== "running") return false;
    if (message.tool_call_id) return item.callId === message.tool_call_id;
    if (!message.tool_name) return true;
    return item.name === message.tool_name;
  });
  const target = tool ?? {
    kind: "tool",
    id: nextMessageId(state, "tool"),
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

export function handleTodoStatus(state, message) {
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

export function handleSubmitText(state, text, send) {
  if (text === "/folds") {
    showFoldList(state);
    return;
  }
  if (text.startsWith("/fold")) {
    toggleFoldCommand(state, text);
    return;
  }
  if (text.startsWith("/expand")) {
    setFoldCommand(state, text, true);
    return;
  }
  if (text.startsWith("/collapse")) {
    setFoldCommand(state, text, false);
    return;
  }
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
    state.activeAssistant = null;
    state.activeThinking = null;
    state.folds = {};
    return;
  }
  send("submit", { text });
}

export function pushSystemMessage(state, title, content, level) {
  if (!content) return;
  state.messages.push({ kind: "system", id: nextMessageId(state, "system"), title, content, level });
}

export function createUiSnapshot(state) {
  return {
    folds: state.folds,
    foldCursor: state.foldCursor,
    scrollOffset: state.scrollOffset,
  };
}

export function applyUiSnapshot(state, snapshot) {
  if (!snapshot || typeof snapshot !== "object") return;
  state.folds = sanitizeFolds(snapshot.folds);
  state.foldCursor = Number.isFinite(Number(snapshot.foldCursor)) ? Math.max(0, Number(snapshot.foldCursor)) : 0;
  state.scrollOffset = Number.isFinite(Number(snapshot.scrollOffset)) ? Math.max(0, Number(snapshot.scrollOffset)) : 0;
}

export function getFoldEntries(state) {
  const entries = [];
  for (const message of state.messages) {
    if (message.kind === "assistant" && message.content) {
      countCodeBlocks(message.content).forEach((_, index) => {
        const key = `message:${message.id ?? ""}:code:${index}`;
        entries.push({
          key,
          label: `assistant code #${index + 1}`,
          expanded: isFoldExpanded(state.folds, key),
        });
      });
    }
    if (message.kind === "tool" && message.output && looksLikeDiff(message.output)) {
      const key = `tool:${message.callId || message.id || message.name}`;
      entries.push({
        key,
        label: `${message.name || "tool"} diff`,
        expanded: isFoldExpanded(state.folds, key),
      });
    }
  }
  return entries;
}

export function toggleFoldCommand(state, text) {
  const entries = getFoldEntries(state);
  if (!entries.length) {
    pushSystemMessage(state, "fold", "没有可折叠内容。", "info");
    return;
  }
  const index = parseFoldIndex(text, entries, state.foldCursor);
  const entry = entries[index];
  const nextExpanded = !entry.expanded;
  state.folds = setFoldExpanded(state.folds, entry.key, nextExpanded);
  state.foldCursor = (index + 1) % entries.length;
  pushSystemMessage(state, "fold", `${nextExpanded ? "已展开" : "已折叠"} ${index + 1}. ${entry.label}`, "info");
}

export function setFoldCommand(state, text, expanded) {
  const entries = getFoldEntries(state);
  if (!entries.length) {
    pushSystemMessage(state, "fold", "没有可折叠内容。", "info");
    return;
  }
  if (/\s+all\s*$/i.test(text)) {
    state.folds = entries.reduce((folds, entry) => setFoldExpanded(folds, entry.key, expanded), state.folds);
    pushSystemMessage(state, "fold", `${expanded ? "已展开" : "已折叠"}全部 ${entries.length} 个折叠项。`, "info");
    return;
  }
  const index = parseFoldIndex(text, entries, state.foldCursor);
  const entry = entries[index];
  state.folds = setFoldExpanded(state.folds, entry.key, expanded);
  state.foldCursor = (index + 1) % entries.length;
  pushSystemMessage(state, "fold", `${expanded ? "已展开" : "已折叠"} ${index + 1}. ${entry.label}`, "info");
}

function showFoldList(state) {
  const entries = getFoldEntries(state);
  if (!entries.length) {
    pushSystemMessage(state, "folds", "没有可折叠内容。", "info");
    return;
  }
  const content = entries
    .map((entry, index) => `${index + 1}. ${entry.expanded ? "expanded" : "collapsed"} · ${entry.label}`)
    .join("\n");
  pushSystemMessage(state, "folds", content, "info");
}

function parseFoldIndex(text, entries, fallback) {
  const match = text.trim().match(/^\/(?:fold|expand|collapse)(?:\s+(\d+))?/);
  if (!match?.[1]) return Math.min(Math.max(0, fallback), entries.length - 1);
  return Math.min(Math.max(0, Number(match[1]) - 1), entries.length - 1);
}

function countCodeBlocks(text) {
  const blocks = [];
  let inCode = false;
  for (const line of String(text ?? "").split("\n")) {
    if (!line.startsWith("```")) continue;
    if (inCode) {
      blocks.push(true);
      inCode = false;
    } else {
      inCode = true;
    }
  }
  return blocks;
}

function sanitizeFolds(folds) {
  if (!folds || typeof folds !== "object") return {};
  return Object.fromEntries(
    Object.entries(folds)
      .filter(([key, value]) => typeof key === "string" && value && typeof value === "object")
      .map(([key, value]) => [key, { expanded: value.expanded === true }]),
  );
}

function nextMessageId(state, prefix) {
  const value = `${prefix}-${state.nextMessageId}`;
  state.nextMessageId += 1;
  return value;
}
