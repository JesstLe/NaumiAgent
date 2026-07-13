import { looksLikeDiff } from "./ansi.js";
import { isFoldExpanded, setFoldExpanded } from "./components/folds.js";
import { clearRenderCache, createRenderCache } from "./render-cache.js";

export const DEFAULT_SLASH_COMMAND_CANDIDATES = [
  { command: "/help", aliases: ["/h"], description: "显示帮助" },
  { command: "/history", description: "查看历史会话列表" },
  { command: "/load", aliases: ["/l"], description: "加载会话并继续对话" },
  { command: "/resume", aliases: ["/r"], description: "继续最近一次对话" },
  { command: "/tasks", aliases: ["/task"], description: "显示/更新任务面板（支持 list/open/cancel/refresh）" },
  { command: "/permissions", description: "显示待确认权限面板" },
  { command: "/doctor", description: "运行环境诊断" },
  { command: "/mode", description: "切换 runtime 模式 default / plan / bypass" },
  { command: "/reasoning", description: "显示/切换思考过程输出" },
  { command: "/folds", description: "显示可折叠代码片段列表" },
  { command: "/fold", description: "切换指定折叠项（按编号或类型）" },
  { command: "/expand", description: "展开指定折叠项（按编号/全部）" },
  { command: "/collapse", description: "折叠指定折叠项（按编号/全部）" },
  { command: "/glob", description: "按 glob 规则搜索工作区文件路径" },
  { command: "/grep", description: "搜索文件内容（可配置过滤）" },
  { command: "/read", description: "读取文件内容（可分页）", aliases: ["/file_read"] },
  { command: "/write", description: "写入文件（覆盖）", aliases: ["/file_write"] },
  { command: "/edit", description: "按文本替换更新文件", aliases: ["/file_edit"] },
  { command: "/clear", aliases: ["/c"], description: "清空当前会话显示" },
  { command: "/debug", description: "显示前端与后端调试路径" },
  { command: "/pwd", description: "显示工作区与会话库路径" },
  { command: "/tools", description: "列出可用工具" },
  { command: "/model", aliases: ["/m"], description: "查看当前模型配置" },
  { command: "/usage", aliases: ["/u"], description: "查看 Token 与费用" },
  { command: "/version", aliases: ["/v"], description: "查看当前版本" },
];

const SLASH_COMMAND_ALIAS_HINTS = Object.freeze({
  "/help": ["/h"],
  "/resume": ["/r"],
  "/load": ["/l"],
  "/tasks": ["/task"],
  "/clear": ["/c"],
  "/model": ["/m"],
  "/usage": ["/u"],
  "/version": ["/v"],
});

function mergeAliasesIntoEntry(existing, aliases) {
  const merged = new Set(existing.aliases || []);
  for (const alias of aliases) {
    if (typeof alias === "string" && alias.trim()) {
      merged.add(alias.trim());
    }
  }
  existing.aliases = [...merged];
}

function normalizeSlashCommandList(rawCommands) {
  const sourceCommands =
    Array.isArray(rawCommands) && rawCommands.length
      ? [...rawCommands, ...DEFAULT_SLASH_COMMAND_CANDIDATES]
      : DEFAULT_SLASH_COMMAND_CANDIDATES;
  const normalized = new Map();
  for (const item of sourceCommands) {
    if (!item || typeof item !== "object") continue;
    const command = String(item.command || "").trim().toLowerCase();
    if (!command.startsWith("/")) continue;
    const canonical = command;
    const description = String(item.description || "").trim();
    const existing = normalized.get(canonical);
    if (existing) {
      if (!existing.description && description) {
        existing.description = description;
      }
      mergeAliasesIntoEntry(existing, Array.isArray(item.aliases) ? item.aliases : []);
      continue;
    }
    const aliases = Array.isArray(item.aliases) ? item.aliases : [];
    const entry = {
      command: canonical,
      aliases: [...aliases],
      description,
    };
    normalized.set(canonical, entry);
  }
  if (!normalized.size) {
    return DEFAULT_SLASH_COMMAND_CANDIDATES;
  }
  if (normalized.has("/help") && !normalized.get("/help").aliases.includes("/h")) {
    normalized.get("/help").aliases.push("/h");
  }
  if (normalized.has("/resume") && !normalized.get("/resume").aliases.includes("/r")) {
    normalized.get("/resume").aliases.push("/r");
  }
  if (normalized.has("/load") && !normalized.get("/load").aliases.includes("/l")) {
    normalized.get("/load").aliases.push("/l");
  }
  if (normalized.has("/tasks") && !normalized.get("/tasks").aliases.includes("/task")) {
    normalized.get("/tasks").aliases.push("/task");
  }
  if (normalized.has("/clear") && !normalized.get("/clear").aliases.includes("/c")) {
    normalized.get("/clear").aliases.push("/c");
  }
  if (normalized.has("/model") && !normalized.get("/model").aliases.includes("/m")) {
    normalized.get("/model").aliases.push("/m");
  }
  if (normalized.has("/usage") && !normalized.get("/usage").aliases.includes("/u")) {
    normalized.get("/usage").aliases.push("/u");
  }
  if (normalized.has("/version") && !normalized.get("/version").aliases.includes("/v")) {
    normalized.get("/version").aliases.push("/v");
  }
  return [...normalized.values()].map((entry) => ({ ...entry, aliases: [...entry.aliases] }));
}

function hasAlias(alias, query) {
  if (!alias) return false;
  const normalizedAlias = String(alias).toLowerCase();
  return normalizedAlias.includes(query) || normalizedAlias.startsWith(query);
}

export function getSlashCommandCompletions(input, slashCommands) {
  if (!String(input || "").startsWith("/")) return [];
  const raw = String(input).slice(1).trim();
  if (raw.includes(" ")) return [];

  const query = raw.toLowerCase();
  const candidates = normalizeSlashCommandList(slashCommands);
  const matched = candidates.filter((entry) => {
    if (!query) return true;
    const command = entry.command.slice(1).toLowerCase();
    if (command.includes(query)) return true;
    return (entry.aliases || []).some((alias) => hasAlias(alias, query));
  });

  return matched
    .sort((a, b) => a.command.localeCompare(b.command))
    .map((entry) => ({
      command: entry.command,
      aliases: [...(entry.aliases || [])],
      description: entry.description,
      selected: false,
    }));
}

export function createInitialState() {
  return {
    nextMessageId: 1,
    currentSessionId: "",
    input: "",
    inputCursor: null,
    inputPreferredColumn: null,
    inputHistory: [],
    inputHistoryCursor: null,
    inputHistoryDraft: "",
    mode: "default",
    status: {},
    showReasoning: false,
    slashCommands: DEFAULT_SLASH_COMMAND_CANDIDATES,
    currentTurnStartedAtMs: null,
    currentTurnFirstTokenAtMs: null,
    lastFirstTokenLatencyMs: null,
    messages: [],
    tools: [],
    activeAssistant: null,
    activeThinking: null,
    activeToolPrepare: null,
    activeRuntimePhase: "",
    todo: null,
    taskPanel: {
      pinned: false,
      limit: 12,
      source: "all",
      status: "all",
      history: false,
      detailId: "",
      selectedId: "",
      selectedIndex: 0,
      items: [],
      expandedIds: {},
      collapsedTimelineSources: {},
      focused: false,
      messageId: "",
      lastStatusSignature: "",
    },
    permission: null,
    running: false,
    scrollOffset: 0,
    bridgeReady: false,
    debugTrace: null,
    frontendDebugLogPath: "",
    folds: {},
    foldCursor: 0,
    renderCache: createRenderCache(),
    workbench: {
      session_id: "",
      missions: [],
      tasks: [],
      issues: [],
      failures: [],
      events: [],
    },
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
      return maybeRefreshPinnedTaskPanel(state);
    case "mode/changed":
      {
        const previousMode = state.mode;
        const nextMode = payload.mode ?? state.mode;
        state.mode = nextMode;
        mergeStatus(state, payload.status ?? {});
        if (nextMode && nextMode !== previousMode) {
          pushSystemMessage(state, "mode", modeNoticeText(nextMode), modeNoticeLevel(nextMode));
        }
        return maybeRefreshPinnedTaskPanel(state);
      }
    case "user/message":
      state.messages.push({ kind: "user", content: payload.content ?? "" });
      state.running = true;
      break;
    case "ui/message":
      handleUiMessage(state, payload);
      break;
    case "permission/request":
      handlePermissionRequest(state, record);
      break;
    case "permission/resolved":
      handlePermissionResolved(state, payload);
      break;
    case "run/started":
      state.running = true;
      state.currentTurnStartedAtMs = Date.now();
      state.currentTurnFirstTokenAtMs = null;
      break;
    case "run/completed":
      state.running = false;
      if (state.currentTurnStartedAtMs && state.currentTurnFirstTokenAtMs) {
        state.lastFirstTokenLatencyMs = state.currentTurnFirstTokenAtMs - state.currentTurnStartedAtMs;
      } else {
        state.lastFirstTokenLatencyMs = null;
      }
      state.currentTurnStartedAtMs = null;
      state.currentTurnFirstTokenAtMs = null;
      finishActiveToolPrepare(state, "本轮执行已结束");
      state.activeToolPrepare = null;
      state.activeRuntimePhase = "";
      state.permission = null;
      return state.taskPanel.pinned ? [taskPanelRefreshAction(state)] : [];
    case "session/replayed":
      state.currentSessionId = payload.session_id || state.currentSessionId;
      state.running = false;
      state.currentTurnStartedAtMs = null;
      state.currentTurnFirstTokenAtMs = null;
      state.lastFirstTokenLatencyMs = null;
      state.permission = null;
      state.todo = null;
      state.activeToolPrepare = null;
      state.activeRuntimePhase = "";
      if (payload.clear !== false) {
        state.messages = [];
        state.tools = [];
        state.activeAssistant = null;
        state.activeThinking = null;
        state.folds = {};
        state.foldCursor = 0;
        clearRenderCache(state.renderCache);
      }
      pushSystemMessage(state, "resume", `已恢复会话: ${payload.title ?? payload.session_id}`, "info");
      return [{ type: "session_replayed", sessionId: state.currentSessionId }];
    case "error":
      state.running = false;
      pushSystemMessage(state, "error", payload.message ?? "未知错误", "error");
      break;
    case "shutdown":
      return [{ type: "exit" }];
    case "workbench/snapshot":
      state.workbench = record.payload;
      break;
    case "workbench/event":
      state.workbench.events = [...state.workbench.events, record.payload].slice(-100);
      break;
    default:
      break;
  }
  return [];
}

export function mergeStatus(state, payload) {
  if (!payload || typeof payload !== "object") return;
  state.status = { ...state.status, ...payload };
  if ("session_id" in payload) {
    state.currentSessionId = String(payload.session_id || "");
  }
  if (Array.isArray(payload.slash_commands)) {
    state.slashCommands = normalizeSlashCommandList(payload.slash_commands);
  }
  if (payload.mode) {
    state.mode = payload.mode;
  }
  if (payload.ui && typeof payload.ui === "object" && "show_reasoning" in payload.ui) {
    state.showReasoning = Boolean(payload.ui.show_reasoning);
  } else if ("show_reasoning" in payload) {
    state.showReasoning = Boolean(payload.show_reasoning);
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
      handleTodoPrepare(state, message);
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
        state.activeRuntimePhase = `${message.label}: ${message.duration_ms}ms`;
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

export function handlePermissionRequest(state, record) {
  const payload = record.payload ?? {};
  const requestId = record.request_id ?? record.id ?? "";
  state.permission = { requestId, payload };
  state.messages.push({
    kind: "permission",
    id: nextMessageId(state, "permission"),
    requestId,
    message: {
      ...payload,
      status: "needs_confirmation",
      requires_confirmation: true,
    },
  });
}

export function handlePermissionResolved(state, payload) {
  const requestId = payload.request_id ?? "";
  state.permission = null;
  const message = [...state.messages]
    .reverse()
    .find((item) => item.kind === "permission" && item.requestId === requestId);
  if (message) {
    message.message = {
      ...(message.message ?? {}),
      status: permissionChoiceStatus(payload.choice),
      choice: payload.choice ?? "",
      requires_confirmation: false,
    };
    clearRenderCache(state.renderCache);
    return;
  }
  pushSystemMessage(state, "permission", `权限已处理: ${payload.choice}`, "info");
}

function permissionChoiceStatus(choice) {
  if (choice === "allow") return "allowed";
  if (choice === "deny") return "denied";
  if (choice === "bypass") return "bypass_enabled";
  return String(choice || "resolved");
}

function modeNoticeText(mode) {
  if (mode === "plan") {
    return "已切换到 plan：只读规划模式，写文件和执行命令会被拦截。";
  }
  if (mode === "bypass") {
    return "已切换到 bypass：高风险工具将不再逐次确认，请只在可信工作区使用。";
  }
  return "已切换到 default：高风险工具会按权限策略请求确认。";
}

function modeNoticeLevel(mode) {
  return mode === "bypass" ? "warning" : "info";
}

export function handleAssistantStream(state, message) {
  if (message.phase === "start") {
    if (state.currentTurnStartedAtMs && !state.currentTurnFirstTokenAtMs) {
      state.currentTurnFirstTokenAtMs = Date.now();
      state.lastFirstTokenLatencyMs = state.currentTurnFirstTokenAtMs - state.currentTurnStartedAtMs;
    }
    state.activeAssistant = { kind: "assistant", id: nextMessageId(state, "assistant"), content: "" };
    state.messages.push(state.activeAssistant);
  } else if (message.phase === "token") {
    if (state.currentTurnStartedAtMs && !state.currentTurnFirstTokenAtMs) {
      state.currentTurnFirstTokenAtMs = Date.now();
      state.lastFirstTokenLatencyMs = state.currentTurnFirstTokenAtMs - state.currentTurnStartedAtMs;
    }
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
    state.activeThinking = {
      kind: "thinking",
      id: nextMessageId(state, "thinking"),
      content: "",
      chars: 0,
      done: false,
    };
    state.messages.push(state.activeThinking);
  } else if (message.phase === "delta") {
    if (!state.activeThinking) {
      state.activeThinking = {
        kind: "thinking",
        id: nextMessageId(state, "thinking"),
        content: "",
        chars: 0,
        done: false,
      };
      state.messages.push(state.activeThinking);
    }
    const content = String(message.content ?? "");
    state.activeThinking.chars += content.length;
    if (state.showReasoning) {
      state.activeThinking.content += content;
    }
  } else if (message.phase === "end" && state.activeThinking) {
    const content = String(message.content ?? "");
    state.activeThinking.chars += content.length;
    if (state.showReasoning && content) {
      state.activeThinking.content = content;
    }
    state.activeThinking.done = true;
    if (!state.showReasoning) {
      state.messages = state.messages.filter((item) => item !== state.activeThinking);
    }
    state.activeThinking = null;
  }
}

export function handleToolPrepare(state, message) {
  if (message.phase === "end") {
    if (!state.activeToolPrepare) return;
    const activity = state.activeToolPrepare;
    activity.toolCallId = message.tool_call_id || activity.toolCallId || "";
    activity.toolName = message.tool_name || activity.toolName || "";
    activity.phase = message.phase || activity.phase || "end";
    activity.metrics = buildToolPrepareMetrics(message);
    activity.details = buildToolPrepareDetails(message);
    finishActiveToolPrepare(state, "准备阶段已完成", { keepForToolUse: true });
    return;
  }
  const activity = ensureActiveToolPrepare(state, message);
  activity.status = "running";
  activity.title = `准备 ${message.tool_name || "tool"}`;
  activity.toolCallId = message.tool_call_id || activity.toolCallId || "";
  activity.toolName = message.tool_name || activity.toolName || "";
  activity.phase = message.phase || activity.phase || "start";
  activity.metrics = buildToolPrepareMetrics(message);
  activity.details = buildToolPrepareDetails(message);
}

export function handleToolUse(state, message) {
  const prepare = consumeActiveToolPrepareForToolUse(state, message);
  const tool = {
    kind: "tool",
    id: nextMessageId(state, "tool"),
    callId: message.tool_call_id || "",
    name: message.tool_name,
    primary: message.primary_arg || message.file_path || message.command || message.query || message.url || "",
    status: "running",
    durationMs: 0,
    output: "",
    prepareTitle: prepare?.title ?? "",
    preparePhase: prepare?.phase ?? "",
    prepareMetrics: prepare?.metrics ?? null,
    prepareDetails: prepare?.details ?? [],
  };
  state.tools.push(tool);
  state.messages.push(tool);
}

function consumeActiveToolPrepareForToolUse(state, message) {
  const activity = state.activeToolPrepare;
  if (!activity) return null;
  const prepareCallId = activity.toolCallId || "";
  const toolCallId = message.tool_call_id || "";
  const prepareName = activity.toolName || "";
  const toolName = message.tool_name || "";
  const mismatchedCallId = prepareCallId && toolCallId && prepareCallId !== toolCallId;
  const mismatchedName = !prepareCallId && !toolCallId && prepareName && toolName && prepareName !== toolName;
  if (mismatchedCallId || mismatchedName) {
    state.activeToolPrepare = null;
    return null;
  }
  return finishActiveToolPrepare(state, "已交给工具执行");
}

function ensureActiveToolPrepare(state, message) {
  if (state.activeToolPrepare && state.activeToolPrepare.status === "running") {
    return state.activeToolPrepare;
  }
  const activity = {
    kind: "activity",
    id: nextMessageId(state, "activity"),
    status: "running",
    title: `准备 ${message.tool_name || "tool"}`,
    toolCallId: message.tool_call_id || "",
    toolName: message.tool_name || "",
    phase: message.phase || "start",
    metrics: buildToolPrepareMetrics(message),
    details: [],
  };
  state.activeToolPrepare = activity;
  state.messages.push(activity);
  return activity;
}

function finishActiveToolPrepare(state, detail, { keepForToolUse = false } = {}) {
  if (!state.activeToolPrepare) return null;
  const activity = state.activeToolPrepare;
  state.activeToolPrepare.status = "done";
  if (detail && !state.activeToolPrepare.details?.includes(detail)) {
    state.activeToolPrepare.details = [...(state.activeToolPrepare.details ?? []), detail];
  }
  if (!keepForToolUse) {
    state.activeToolPrepare = null;
  }
  return activity;
}

function buildToolPrepareDetails(message) {
  const details = [];
  if (message.path) details.push(`路径: ${message.path}`);
  if (message.command) details.push(`命令: ${message.command}`);
  if (message.query) details.push(`查询: ${message.query}`);
  if (message.url) details.push(`URL: ${message.url}`);
  if (message.content_lines) details.push(`内容: ${message.content_lines} 行`);
  if (message.content_chars) details.push(`内容: ${formatCount(message.content_chars)} 字符`);
  if (message.argument_chars) details.push(`参数: ${formatCount(message.argument_chars)} 字符`);
  if (message.elapsed_ms > 1000) details.push(`已准备: ${(message.elapsed_ms / 1000).toFixed(1)}s`);
  return details;
}

function buildToolPrepareMetrics(message) {
  return {
    argumentChars: Number(message.argument_chars ?? 0) || 0,
    contentChars: Number(message.content_chars ?? 0) || 0,
    contentLines: Number(message.content_lines ?? 0) || 0,
    elapsedMs: Number(message.elapsed_ms ?? 0) || 0,
  };
}

function formatCount(value) {
  const num = Number(value || 0);
  if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
  if (num >= 1000) return `${(num / 1000).toFixed(1)}K`;
  return String(num);
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
    outputFormat: "text",
    outputLanguage: "",
  };
  if (!tool) {
    state.tools.push(target);
    state.messages.push(target);
  }
  target.status = message.status;
  target.durationMs = message.duration_ms;
  target.output = message.content_preview ?? "";
  target.outputLength = message.content_length ?? 0;
  target.outputFormat = message.preview_format ?? "text";
  target.outputLanguage = message.preview_language ?? "";
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

export function handleTodoPrepare(state, message) {
  if (!["todo_write", "task_create", "task_update", "task_delete"].includes(message.tool_name)) {
    return;
  }
  if (message.tool_name === "todo_write" && Array.isArray(message.todo_items) && message.todo_items.length) {
    const priority = { in_progress: 0, blocked: 1, pending: 2 };
    const current = message.todo_items
      .filter((item) => item && item.status !== "completed")
      .sort((a, b) => (priority[a.status] ?? 9) - (priority[b.status] ?? 9))[0];
    state.todo = {
      total: message.todo_total ?? message.todo_items.length,
      completed: message.todo_completed ?? 0,
      current,
    };
    return;
  }
  if (message.phase === "end") {
    return;
  }
  const currentTodo = state.todo ?? {};
  const argumentText = Number(message.argument_chars) > 0 ? `参数 ${Number(message.argument_chars)} 字符` : "等待任务参数";
  state.todo = {
    total: currentTodo.total ?? 0,
    completed: currentTodo.completed ?? 0,
    current: {
      id: "...",
      status: "in_progress",
      subject: `正在同步任务列表 (${argumentText})`,
    },
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
  if (text === "/tasks" || text.startsWith("/tasks ")) {
    handleTasksCommand(state, text, send);
    return;
  }
  if (text === "/permissions" || text.startsWith("/permissions ")) {
    const raw = text.slice("/permissions".length).trim();
    const limit = Number.parseInt(raw, 10);
    if (raw && Number.isFinite(limit) && limit > 0) {
      send("permissions_panel", { limit });
    } else {
      send("permissions_panel", {});
    }
    return;
  }
  if (text === "/doctor") {
    send("doctor", {});
    return;
  }
  if (text.startsWith("/mode ")) {
    send("set_mode", { mode: text.slice(6).trim() });
    return;
  }
  if (text === "/reasoning" || text.startsWith("/reasoning ")) {
    handleReasoningCommand(state, text, send);
    return;
  }
  if (text === "/clear" || text === "/c") {
    state.messages = [];
    state.tools = [];
    state.activeAssistant = null;
    state.activeThinking = null;
    state.folds = {};
    state.foldCursor = 0;
    clearRenderCache(state.renderCache);
    send("submit", { text });
    return;
  }
  if (text === "/debug") {
    showDebugInfo(state);
    return;
  }
  send("submit", { text });
}

function handleReasoningCommand(state, text, send) {
  const raw = text.slice("/reasoning".length).trim().toLowerCase();
  let enabled;
  if (!raw || raw === "toggle") {
    enabled = !state.showReasoning;
  } else if (["on", "true", "1", "show", "open"].includes(raw)) {
    enabled = true;
  } else if (["off", "false", "0", "hide", "close"].includes(raw)) {
    enabled = false;
  } else {
    pushSystemMessage(state, "reasoning", "用法: /reasoning on|off|toggle", "warning");
    return;
  }
  state.showReasoning = enabled;
  send("set_reasoning", { enabled });
  pushSystemMessage(
    state,
    "reasoning",
    enabled ? "reasoning 文本显示已开启。" : "reasoning 文本显示已关闭。",
    enabled ? "warning" : "info",
  );
}

export function pushSystemMessage(state, title, content, level) {
  if (!content) return;
  if (title === "tasks" && state.taskPanel) {
    const existing = state.messages.find((message) => message.id === state.taskPanel.messageId);
    if (existing) {
      existing.content = content;
      existing.level = level;
      syncTaskPanelItems(state, content);
      clearRenderCache(state.renderCache);
      return;
    }
  }
  state.messages.push({ kind: "system", id: nextMessageId(state, "system"), title, content, level });
  if (title === "tasks" && state.taskPanel) {
    state.taskPanel.messageId = state.messages.at(-1)?.id ?? "";
    syncTaskPanelItems(state, content);
    if (state.taskPanel.items.length) {
      state.taskPanel.focused = true;
    }
  }
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
  clearRenderCache(state.renderCache);
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
    clearRenderCache(state.renderCache);
    pushSystemMessage(state, "fold", `${expanded ? "已展开" : "已折叠"}全部 ${entries.length} 个折叠项。`, "info");
    return;
  }
  const index = parseFoldIndex(text, entries, state.foldCursor);
  const entry = entries[index];
  state.folds = setFoldExpanded(state.folds, entry.key, expanded);
  state.foldCursor = (index + 1) % entries.length;
  clearRenderCache(state.renderCache);
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

function showDebugInfo(state) {
  const trace = state.debugTrace ?? {};
  const lines = [
    `前端日志: ${state.frontendDebugLogPath || "未启用"}`,
    `Bridge events: ${trace.events_path || "-"}`,
    `Bridge transcript: ${trace.transcript_path || "-"}`,
    `Bridge run: ${trace.run_id || "-"}`,
    `Bridge ready: ${state.bridgeReady ? "yes" : "no"}`,
    `当前消息: ${state.messages.length}`,
    `工具卡片: ${state.tools.length}`,
    `模式: ${state.mode}`,
  ];
  pushSystemMessage(state, "debug", lines.join("\n"), "info");
}

function handleTasksCommand(state, text, send) {
  const raw = text.slice(6).trim();
  if (/^timeline(?:\s|$)/.test(raw)) {
    handleTaskTimelineCommand(state, raw);
    return;
  }
  const { command, commandArg, limit, source, status, detailId, history } = parseTaskCommand(raw, state.taskPanel.limit);
  if (command === "off") {
    closeTaskPanel(state);
    pushSystemMessage(state, "任务面板", "已取消钉住。", "info");
    return;
  }
  if (command === "focus") {
    setTaskPanelFocus(state, true);
    return;
  }
  if (command === "blur") {
    setTaskPanelFocus(state, false);
    return;
  }
  if (command === "next" || command === "prev") {
    selectTaskPanelOffset(state, command === "next" ? 1 : -1);
    return;
  }
  if (command === "select") {
    selectTaskPanelItem(state, commandArg);
    return;
  }
  if (command === "open") {
    openSelectedTaskPanelItem(state, send);
    return;
  }
  if (command === "jump") {
    jumpToTaskPanelRecord(state, commandArg);
    return;
  }
  if (command === "expand") {
    setTaskPanelItemExpanded(state, commandArg, true);
    return;
  }
  if (command === "collapse") {
    setTaskPanelItemExpanded(state, commandArg, false);
    return;
  }
  if (command === "clear") {
    state.taskPanel.detailId = "";
    sendCurrentTaskPanelRequest(state, send, { refresh: true });
    return;
  }
  if (command === "cancel") {
    cancelTaskPanelItem(state, send, commandArg);
    return;
  }

  state.taskPanel.limit = limit;
  state.taskPanel.source = source;
  state.taskPanel.status = status;
  state.taskPanel.history = history;
  state.taskPanel.detailId = detailId;
  state.taskPanel.focused = true;
  if (command === "pin") {
    state.taskPanel.pinned = true;
    state.taskPanel.lastStatusSignature = "";
  }
  sendCurrentTaskPanelRequest(state, send, { refresh: command === "refresh" });
}

function handleTaskTimelineCommand(state, raw) {
  const tokens = String(raw ?? "").split(/\s+/).filter(Boolean);
  tokens.shift();
  const action = normalizeTaskFilterToken(tokens.shift() || "toggle");
  if (action === "clear") {
    state.taskPanel.collapsedTimelineSources = {};
    clearRenderCache(state.renderCache);
    pushSystemMessage(state, "任务面板", "已清除 Timeline 来源折叠。", "info");
    return true;
  }

  const source = normalizeTaskFilterToken(tokens.shift() || "");
  const validSources = new Set(["todo", "subagent", "permissions", "background", "browser"]);
  if (!validSources.has(source) || !["collapse", "expand", "toggle"].includes(action)) {
    pushSystemMessage(
      state,
      "任务面板",
      "用法: /tasks timeline collapse|expand|toggle <todo|subagent|permissions|background|browser>，或 /tasks timeline clear",
      "warning",
    );
    return false;
  }

  const current = Boolean(state.taskPanel.collapsedTimelineSources?.[source]);
  const collapsed = action === "toggle" ? !current : action === "collapse";
  state.taskPanel.collapsedTimelineSources = {
    ...(state.taskPanel.collapsedTimelineSources ?? {}),
    [source]: collapsed,
  };
  if (!collapsed) {
    delete state.taskPanel.collapsedTimelineSources[source];
  }
  clearRenderCache(state.renderCache);
  pushSystemMessage(
    state,
    "任务面板",
    `${collapsed ? "已折叠" : "已展开"} Timeline 来源: ${source}`,
    "info",
  );
  return true;
}

function parseTaskCommand(raw, fallbackLimit = 12) {
  const sources = new Set(["all", "todo", "subagent", "background", "browser", "permissions"]);
  const statuses = new Set([
    "all",
    "open",
    "running",
    "pending",
    "completed",
    "failed",
    "blocked",
    "attention",
    "needs_input",
    "needs_confirmation",
    "cancelled",
    "timed_out",
  ]);
  const tokens = String(raw ?? "").split(/\s+/).filter(Boolean);
  let command = "";
  let commandArg = "";
  let limit = fallbackLimit;
  let source = "all";
  let status = "all";
  let detailId = "";
  let history = false;
  if (tokens[0] === "history") {
    tokens.shift();
    history = true;
    source = "background";
  }
  if (["pin", "refresh", "off", "detail", "next", "prev", "select", "open", "jump", "expand", "collapse", "clear", "cancel", "focus", "blur"].includes(tokens[0])) {
    command = tokens.shift();
    if (["select", "cancel", "jump", "expand", "collapse"].includes(command) && tokens[0]) {
      commandArg = tokens.shift();
    }
  }
  for (const rawToken of tokens) {
    const [key, rawValue] = rawToken.includes("=") ? rawToken.split("=", 2) : ["", rawToken];
    const value = normalizeTaskFilterToken(rawValue);
    if (key === "limit") {
      limit = parseTaskPanelLimit(value, limit);
    } else if (!key && /^\d+$/.test(value)) {
      limit = parseTaskPanelLimit(value, limit);
    } else if ((key === "source" || !key) && sources.has(value)) {
      source = value;
    } else if ((key === "status" || !key) && statuses.has(value)) {
      status = value;
    } else if (key === "detail" || key === "detail_id" || (command === "detail" && !key && !detailId)) {
      detailId = String(rawValue ?? "").trim();
    }
  }
  return { command, commandArg, limit, source, status, detailId, history };
}

function normalizeTaskFilterToken(value) {
  return String(value ?? "").trim().toLowerCase().replace(/-/g, "_");
}

function parseTaskPanelLimit(raw, fallback = 12) {
  const limit = raw ? Number.parseInt(raw, 10) : fallback;
  return Number.isFinite(limit) && limit > 0 ? Math.min(limit, 50) : fallback;
}

function closeTaskPanel(state) {
  state.taskPanel.pinned = false;
  state.taskPanel.lastStatusSignature = "";
  state.taskPanel.detailId = "";
  state.taskPanel.history = false;
  state.taskPanel.selectedId = "";
  state.taskPanel.selectedIndex = 0;
  state.taskPanel.items = [];
  state.taskPanel.expandedIds = {};
  state.taskPanel.collapsedTimelineSources = {};
  state.taskPanel.focused = false;
  if (state.taskPanel.messageId) {
    state.messages = state.messages.filter((message) => message.id !== state.taskPanel.messageId);
    state.taskPanel.messageId = "";
    clearRenderCache(state.renderCache);
  }
}

function maybeRefreshPinnedTaskPanel(state) {
  if (!state.taskPanel?.pinned) return [];
  const signature = taskStatusSignature(state.status?.tasks);
  if (!signature || signature === state.taskPanel.lastStatusSignature) return [];
  state.taskPanel.lastStatusSignature = signature;
  return [taskPanelRefreshAction(state)];
}

function taskPanelRefreshAction(state) {
  const action = {
    type: "refresh_task_panel",
    limit: state.taskPanel.limit,
    source: state.taskPanel.source,
    status: state.taskPanel.status,
  };
  if (state.taskPanel.detailId) action.detailId = state.taskPanel.detailId;
  if (state.taskPanel.history) action.history = true;
  return action;
}

function taskStatusSignature(tasks) {
  if (!tasks || typeof tasks !== "object") return "";
  return [
    tasks.background_running ?? 0,
    tasks.background_attention ?? 0,
    tasks.subagents_active ?? 0,
    tasks.browser_active ?? 0,
    tasks.permissions_pending ?? 0,
  ].join(":");
}

export function hasTaskPanelFocus(state) {
  return Boolean(state.taskPanel?.focused && state.taskPanel?.messageId && state.taskPanel?.items?.length);
}

export function setTaskPanelFocus(state, focused) {
  const nextFocused = Boolean(focused);
  if (nextFocused && !(state.taskPanel?.messageId && state.taskPanel?.items?.length)) {
    pushSystemMessage(state, "任务面板", "当前没有可聚焦的任务面板。", "info");
    return false;
  }
  state.taskPanel.focused = nextFocused;
  clearRenderCache(state.renderCache);
  pushSystemMessage(state, "任务面板", nextFocused ? "任务面板已聚焦。" : "任务面板焦点已退出。", "info");
  return true;
}

export function selectTaskPanelOffset(state, delta) {
  const items = state.taskPanel?.items ?? [];
  if (!items.length) {
    pushSystemMessage(state, "任务面板", "当前任务面板没有可选择项。", "info");
    return false;
  }
  const currentIndex = normalizeTaskPanelIndex(state.taskPanel.selectedIndex, items.length);
  const nextIndex = (currentIndex + delta + items.length) % items.length;
  setTaskPanelSelection(state, nextIndex, { notify: true });
  return true;
}

export function selectTaskPanelItem(state, selector) {
  const items = state.taskPanel?.items ?? [];
  if (!items.length) {
    pushSystemMessage(state, "任务面板", "当前任务面板没有可选择项。", "info");
    return false;
  }
  const raw = String(selector ?? "").trim();
  let index = -1;
  if (/^\d+$/.test(raw)) {
    index = Number(raw) - 1;
  } else if (raw) {
    index = items.findIndex((item) => item.id === raw);
  }
  if (index < 0 || index >= items.length) {
    pushSystemMessage(state, "任务面板", `未找到可选择项: ${raw || "-"}`, "warning");
    return false;
  }
  setTaskPanelSelection(state, index, { notify: true });
  return true;
}

export function openSelectedTaskPanelItem(state, send) {
  const item = selectedTaskPanelItem(state);
  if (!item) {
    pushSystemMessage(state, "任务面板", "没有已选中的任务项。先打开 /tasks，再用 Tab 或 /tasks select 选择。", "info");
    return false;
  }
  state.taskPanel.detailId = item.id;
  sendCurrentTaskPanelRequest(state, send, { refresh: true });
  return true;
}

export function cancelTaskPanelItem(state, send, selector = "") {
  const item = resolveTaskPanelItem(state, selector);
  const raw = String(selector ?? "").trim();
  if (!item && !raw) {
    pushSystemMessage(state, "任务面板", "没有已选中的任务项。先打开 /tasks，再用 Tab 或 /tasks select 选择。", "info");
    return false;
  }
  const taskId = item?.id ?? raw;
  const source = item?.source ?? state.taskPanel.source ?? "all";
  send("task_cancel", {
    task_id: taskId,
    source,
    reason: "用户从任务面板取消。",
  });
  return true;
}

export function jumpToTaskPanelRecord(state, selector = "") {
  const item = resolveTaskPanelItem(state, selector);
  const raw = String(selector ?? "").trim();
  if (!item && !raw) {
    pushSystemMessage(state, "任务面板", "没有已选中的任务项。先打开 /tasks，再用 Tab 或 /tasks select 选择。", "info");
    return false;
  }
  if (!item) {
    pushSystemMessage(state, "任务面板", `未找到可跳转任务项: ${raw}`, "warning");
    return false;
  }
  if (!item.recordPath) {
    pushSystemMessage(state, "任务面板", `任务 ${item.id} 暂无可跳转运行记录。可先用 /tasks open 查看详情。`, "warning");
    return false;
  }
  pushSystemMessage(
    state,
    "任务记录",
    [`任务: ${item.id}`, `来源: ${item.source}`, `记录: ${item.recordPath}`].join("\n"),
    "info",
  );
  return true;
}

export function toggleTaskPanelItemExpanded(state, selector = "") {
  const item = resolveTaskPanelItem(state, selector);
  const raw = String(selector ?? "").trim();
  if (!item && !raw) {
    pushSystemMessage(state, "任务面板", "没有已选中的任务项。先打开 /tasks，再用 Tab 或 /tasks select 选择。", "info");
    return false;
  }
  if (!item) {
    pushSystemMessage(state, "任务面板", `未找到可展开任务项: ${raw}`, "warning");
    return false;
  }
  return setTaskPanelItemExpanded(state, item.id, !state.taskPanel.expandedIds?.[item.id]);
}

export function setTaskPanelItemExpanded(state, selector = "", expanded = true) {
  const item = resolveTaskPanelItem(state, selector);
  const raw = String(selector ?? "").trim();
  if (!item && !raw) {
    pushSystemMessage(state, "任务面板", "没有已选中的任务项。先打开 /tasks，再用 Tab 或 /tasks select 选择。", "info");
    return false;
  }
  if (!item) {
    pushSystemMessage(state, "任务面板", `未找到可展开任务项: ${raw}`, "warning");
    return false;
  }
  state.taskPanel.expandedIds = {
    ...(state.taskPanel.expandedIds ?? {}),
    [item.id]: Boolean(expanded),
  };
  if (!expanded) {
    delete state.taskPanel.expandedIds[item.id];
  }
  clearRenderCache(state.renderCache);
  pushSystemMessage(state, "任务面板", `${expanded ? "已展开" : "已折叠"} ${item.label}`, "info");
  return true;
}

function sendCurrentTaskPanelRequest(state, send, overrides = {}) {
  const payload = {
    limit: state.taskPanel.limit,
    source: state.taskPanel.source,
    status: state.taskPanel.status,
    pinned: state.taskPanel.pinned,
    refresh: Boolean(overrides.refresh),
  };
  if (state.taskPanel.detailId) payload.detail_id = state.taskPanel.detailId;
  if (state.taskPanel.history) payload.history = true;
  send("task_panel", payload);
}

function syncTaskPanelItems(state, content) {
  const items = extractTaskPanelItems(content);
  state.taskPanel.items = items;
  if (!items.length) {
    state.taskPanel.selectedId = "";
    state.taskPanel.selectedIndex = 0;
    state.taskPanel.focused = false;
    return;
  }
  const validIds = new Set(items.map((item) => item.id));
  state.taskPanel.expandedIds = Object.fromEntries(
    Object.entries(state.taskPanel.expandedIds ?? {}).filter(([id, expanded]) => validIds.has(id) && expanded),
  );
  const existingIndex = items.findIndex((item) => item.id === state.taskPanel.selectedId);
  setTaskPanelSelection(state, existingIndex >= 0 ? existingIndex : 0, { notify: false });
}

export function extractTaskPanelItems(content) {
  const items = [];
  const timelineItems = [];
  let section = "";
  const seen = new Set();
  for (const rawLine of String(content ?? "").split("\n")) {
    const line = rawLine.replace(/\x1b\[[0-9;]*m/g, "").trim();
    if (["Timeline", "Detail", "Todo", "Subagent", "Background", "Browser Runs"].includes(line)) {
      section = line;
      continue;
    }
    const item = parseTaskPanelSelectableLine(section, line);
    if (!item) continue;
    if (section === "Timeline") {
      timelineItems.push(item);
      continue;
    }
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    items.push({ ...item, index: items.length });
  }
  for (const item of timelineItems) {
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    items.push({ ...item, index: items.length });
  }
  return items;
}

function parseTaskPanelSelectableLine(section, line) {
  if (section === "Timeline") {
    const match = line.match(/^-\s+([^\s#]+)\s+\[([^\]]+)\]\s+(.+?)(?:\s+\|\s+(.+))?$/);
    if (match) {
      const detail = match[4] ?? "";
      const source = extractDetailField(detail, "source") || "timeline";
      return {
        id: match[1],
        source,
        status: match[2],
        label: `${source} ${match[1]} ${match[3]}`,
        recordPath: extractRecordPath(detail),
      };
    }
  }
  if (section === "Todo") {
    const match = line.match(/^-\s+#([^\s]+)\s+\[([^\]]+)\]\s+(.+?)(?:\s+\||$)/);
    if (match) return { id: match[1], source: "todo", status: match[2], label: `todo #${match[1]} ${match[3]}` };
  }
  if (section === "Subagent") {
    const eventMatch = line.match(/^-\s+([^:]+):\s+(.+?)\s+\/\s+([^\s]+)\s*(.*)$/);
    if (eventMatch) return { id: eventMatch[3], source: "subagent", status: eventMatch[1], label: `subagent ${eventMatch[2]} / ${eventMatch[3]}` };
  }
  if (section === "Background") {
    const match = line.match(/^-\s+([^\s#]+)\s+\[([^\]]+)\]\s+(.+?)(?:\s+\|\s+(.+))?$/);
    if (match) {
      return {
        id: match[1],
        source: "background",
        status: match[2],
        label: `background ${match[1]} ${match[3]}`,
        recordPath: extractRecordPath(match[4], "output"),
      };
    }
  }
  if (section === "Browser Runs") {
    const match = line.match(/^-\s+([^\s#]+)\s+\[([^\]]+)\]\s+(.+?)(?:\s+\|\s+(.+))?$/);
    if (match) {
      return {
        id: match[1],
        source: "browser",
        status: match[2],
        label: `browser ${match[1]} ${match[3]}`,
        recordPath: extractRecordPath(match[4], "records"),
      };
    }
  }
  return null;
}

function extractRecordPath(detail = "", preferredKey = "") {
  const fields = String(detail ?? "").split(/\s*;\s*/);
  const candidates = preferredKey ? [preferredKey, "records", "output"] : ["records", "output"];
  for (const key of candidates) {
    const field = fields.find((item) => item.startsWith(`${key}=`));
    if (!field) continue;
    const value = field.slice(key.length + 1).trim();
    if (!value || value === "-") continue;
    return value.split(/\s*,\s*/)[0] ?? "";
  }
  return "";
}

function extractDetailField(detail = "", key = "") {
  const field = String(detail ?? "")
    .split(/\s*;\s*/)
    .find((item) => item.startsWith(`${key}=`));
  if (!field) return "";
  return field.slice(key.length + 1).trim();
}

function selectedTaskPanelItem(state) {
  const items = state.taskPanel?.items ?? [];
  if (!items.length) return null;
  return items[normalizeTaskPanelIndex(state.taskPanel.selectedIndex, items.length)] ?? null;
}

function resolveTaskPanelItem(state, selector = "") {
  const raw = String(selector ?? "").trim();
  const items = state.taskPanel?.items ?? [];
  if (!raw) return selectedTaskPanelItem(state);
  if (/^\d+$/.test(raw)) {
    return items[Number(raw) - 1] ?? items.find((item) => item.id === raw) ?? null;
  }
  return items.find((item) => item.id === raw) ?? null;
}

function setTaskPanelSelection(state, index, { notify }) {
  const items = state.taskPanel?.items ?? [];
  if (!items.length) return;
  const nextIndex = normalizeTaskPanelIndex(index, items.length);
  const item = items[nextIndex];
  state.taskPanel.selectedIndex = nextIndex;
  state.taskPanel.selectedId = item.id;
  clearRenderCache(state.renderCache);
  if (notify) {
    pushSystemMessage(state, "任务面板", `已选中 ${nextIndex + 1}/${items.length}: ${item.label}`, "info");
  }
}

function normalizeTaskPanelIndex(index, length) {
  if (!length) return 0;
  const value = Number.isFinite(Number(index)) ? Number(index) : 0;
  return Math.max(0, Math.min(length - 1, value));
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
