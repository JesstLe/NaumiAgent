export function createInitialState() {
  return {
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
      if (payload.clear !== false) {
        state.messages = [];
        state.tools = [];
        state.activeAssistant = null;
        state.activeThinking = null;
      }
      pushSystemMessage(state, "resume", `已恢复会话: ${payload.title ?? payload.session_id}`, "info");
      break;
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

export function pushSystemMessage(state, title, content, level) {
  if (!content) return;
  state.messages.push({ kind: "system", title, content, level });
}
