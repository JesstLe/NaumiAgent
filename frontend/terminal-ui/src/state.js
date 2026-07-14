import { looksLikeDiff, sanitizeTerminalText } from "./ansi.js";
import {
  INPUT_KEYS,
  backspaceInput,
  deleteInputForward,
  getInputCursor,
  insertInputText,
  moveInputCursor,
  setInputText,
  truncateInputText,
} from "./input-buffer.js";
import { isFoldExpanded, setFoldExpanded } from "./components/folds.js";
import { clearRenderCache, createRenderCache } from "./render-cache.js";
import { jumpTimelineToLatest } from "./timeline-follow.js";

const MAX_OUTBOX_MESSAGES = 20;
const MAX_OUTBOX_ERROR_CHARS = 500;
const MAX_RUN_ACTIVITY_PERF_PHASES = 5;
const MAX_RUN_ACTIVITY_TOOLS = 100;
const MAX_RUN_ACTIVITY_PERMISSION_IDS = 100;
const MAX_RUN_ACTIVITY_PHASE_LABEL_CHARS = 160;
const MAX_SUBAGENT_ACTIVITY_EVENTS = 20;
const RUNTIME_INSPECTOR_TABS = Object.freeze(["plan", "tools", "context", "changes", "tests"]);
const AGENT_CONTROL_TABS = Object.freeze(["agents", "executions", "team"]);

const RUN_ACTIVITY_PHASE_LABELS = Object.freeze({
  preparing: "准备运行",
  generating: "生成响应",
  executing: "执行工具",
  awaiting_permission: "等待权限",
  awaiting_input: "等待用户输入",
  summarizing: "整理结果",
  completed: "执行完成",
  failed: "执行失败",
  cancelled: "运行取消",
});

export const DEFAULT_SLASH_COMMAND_CANDIDATES = [
  { command: "/help", aliases: ["/h"], description: "显示帮助" },
  { command: "/history", description: "查看历史会话列表" },
  { command: "/load", aliases: ["/l"], description: "加载会话并继续对话" },
  { command: "/resume", aliases: ["/r"], description: "继续最近一次对话" },
  { command: "/task", description: "创建任务、切换任务输入，或按 ID 打开任务" },
  { command: "/tasks", description: "显示/更新任务面板（支持 list/open/cancel/refresh）" },
  { command: "/chat", description: "切换为普通对话输入" },
  { command: "/permissions", description: "显示待确认权限面板" },
  { command: "/agents", description: "打开 Agent 控制中心" },
  { command: "/doctor", description: "运行环境诊断" },
  { command: "/harness", description: "Harness Profile 状态、知识、检查与信任" },
  { command: "/mode", description: "切换 runtime 模式 default / plan / bypass" },
  { command: "/reasoning", description: "显示/切换思考文本" },
  { command: "/effort", description: "查看或切换模型思考强度" },
  { command: "/retry", description: "重试最近一条发送失败或状态待确认的消息" },
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
  { command: "/models", description: "列出 provider 可用模型" },
  { command: "/usage", aliases: ["/u"], description: "查看 Token 与费用" },
  { command: "/version", aliases: ["/v"], description: "查看当前版本" },
];

const SLASH_COMMAND_ALIAS_HINTS = Object.freeze({
  "/help": ["/h"],
  "/resume": ["/r"],
  "/load": ["/l"],
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
  const raw = String(input).slice(1);
  if (/\s/.test(raw)) return [];

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
    route: { name: "conversation", originAnchor: null },
    nextMessageId: 1,
    nextSubmitId: 1,
    nextCancelId: 1,
    currentSessionId: "",
    input: "",
    inputCursor: null,
    inputPreferredColumn: null,
    inputHistory: [],
    inputHistoryCursor: null,
    inputHistoryDraft: "",
    composerIntent: "chat",
    activeTaskSubmission: null,
    activeRunActivity: null,
    cancelPending: false,
    cancelRequestId: "",
    historySearch: {
      open: false,
      query: "",
      matches: [],
      selectedIndex: 0,
      draftText: "",
      draftCursor: 0,
    },
    mode: "default",
    status: {},
    welcome: {
      phase: "booting",
      dismissed: false,
    },
    showReasoning: false,
    slashCommands: DEFAULT_SLASH_COMMAND_CANDIDATES,
    slashCompletion: {
      input: "",
      selectedIndex: 0,
      dismissedInput: null,
    },
    currentTurnStartedAtMs: null,
    currentTurnFirstTokenAtMs: null,
    lastFirstTokenLatencyMs: null,
    workingAnimationFrame: 0,
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
    inspector: {
      open: false,
      loading: false,
      focused: false,
      selectedTab: "plan",
      revision: 0,
      snapshot: null,
      error: "",
      stale: false,
      selectionByTab: {},
      expandedByTab: {},
      scrollByTab: {},
    },
    agents: {
      open: false,
      loading: false,
      selectedTab: "agents",
      selectedByTab: {},
      detailId: "",
      scrollByTab: {},
      revision: 0,
      snapshot: null,
      error: "",
      stale: false,
      stopConfirmationTaskId: "",
      actionPendingTaskId: "",
      actionMessage: "",
    },
    permission: null,
    interaction: null,
    interactionQueue: [],
    running: false,
    scrollOffset: 0,
    followTail: true,
    unreadOutputCount: 0,
    unreadOutputKeys: {},
    bridgeReady: false,
    bridgeHeartbeat: { status: "starting", rttMs: null, ageMs: 0 },
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

export function dismissWelcome(state) {
  if (!state.welcome || state.welcome.dismissed) return false;
  state.welcome.phase = "dismissed";
  state.welcome.dismissed = true;
  return true;
}

export function updateBridgeHeartbeat(state, value = {}) {
  const previousStatus = String(state.bridgeHeartbeat?.status ?? "starting");
  const status = value.status === "stale" ? "stale" : "healthy";
  const rttMs = Number.isFinite(Number(value.rttMs))
    ? Math.max(0, Math.round(Number(value.rttMs)))
    : null;
  const ageMs = Math.max(0, Math.round(Number(value.ageMs) || 0));
  state.bridgeHeartbeat = { status, rttMs, ageMs };
  let notificationAdded = false;

  if (status === "stale" && previousStatus !== "stale") {
    const seconds = Math.max(1, Math.ceil(ageMs / 1_000));
    pushSystemMessage(
      state,
      "Bridge 心跳",
      `后端控制面已连续 ${seconds} 秒无响应；当前任务不会被自动重启。`,
      "warning",
      { dismissWelcome: true },
    );
    notificationAdded = true;
  } else if (status === "healthy" && previousStatus === "stale") {
    pushSystemMessage(
      state,
      "Bridge 心跳",
      `后端控制面已恢复，往返延迟 ${rttMs ?? 0}ms。`,
      "info",
      { dismissWelcome: true },
    );
    notificationAdded = true;
  }
  if (status !== previousStatus || notificationAdded) {
    clearRenderCache(state.renderCache);
  }
  return notificationAdded;
}

export function reduceServerEvent(state, record) {
  const payload = record.payload ?? {};
  switch (record.type) {
    case "ack":
      if (payload.event === "submit") {
        acceptUserMessage(state, record.request_id);
      }
      if (payload.event === "agents/request" && payload.open === true) {
        state.agents.loading = false;
      }
      break;
    case "ready":
      state.bridgeReady = true;
      mergeStatus(state, payload);
      if (!state.welcome.dismissed) state.welcome.phase = "ready_empty";
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
      dismissWelcome(state);
      if (!acceptUserMessage(state, record.request_id, payload.content ?? "")) {
        appendAcceptedUserMessage(state, payload.content ?? "", record.request_id);
      }
      state.running = true;
      break;
    case "run/queued":
      scheduleUserMessage(state, record.request_id, payload.position);
      break;
    case "task/created":
      {
        dismissWelcome(state);
        const message = acceptUserMessage(state, record.request_id);
        const taskId = String(payload.task?.id ?? payload.issue?.task_id ?? "");
        const missionId = String(payload.mission?.id ?? payload.issue?.mission_id ?? "");
        if (message) {
          message.intent = "task";
          message.taskId = taskId;
          message.missionId = missionId;
          message.taskStatus = String(payload.task?.status ?? "in_progress");
        }
        state.activeTaskSubmission = {
          requestId: String(record.request_id ?? ""),
          taskId,
          missionId,
          state: "running",
        };
        state.composerIntent = "chat";
        if (payload.workbench_snapshot && typeof payload.workbench_snapshot === "object") {
          state.workbench = payload.workbench_snapshot;
        }
      }
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
    case "interaction/request":
      handleInteractionRequest(state, record);
      break;
    case "interaction/resolved":
      handleInteractionResolved(state, payload);
      break;
    case "permission/grants_changed":
      pushSystemMessage(
        state,
        "permissions",
        `已撤销 ${Number(payload.revoked) || 0} 项权限授权，当前剩余 ${payload.grants?.length ?? 0} 项。`,
        "info",
      );
      break;
    case "completion/receipt":
      addCompletionReceipt(state, payload, record.request_id);
      break;
    case "inspector/snapshot":
      if (!inspectorMatchesCurrentSession(state, payload)) break;
      if (
        state.inspector.snapshot?.session_id === payload.session_id
        && Number(payload.revision) < state.inspector.revision
      ) break;
      state.inspector.snapshot = payload;
      state.inspector.revision = Number(payload.revision) || 0;
      state.inspector.loading = false;
      state.inspector.error = "";
      state.inspector.stale = inspectorSnapshotIsStale(payload);
      break;
    case "inspector/update": {
      if (!inspectorMatchesCurrentSession(state, payload)) break;
      const nextRevision = Number(payload.revision) || 0;
      if (nextRevision <= state.inspector.revision) break;
      if (!state.inspector.snapshot || nextRevision !== state.inspector.revision + 1) {
        state.inspector.loading = true;
        return [{
          type: "refresh_inspector",
          knownRevision: state.inspector.revision,
          sessionId: String(payload.session_id || state.currentSessionId || ""),
        }];
      }
      state.inspector.snapshot = {
        ...state.inspector.snapshot,
        schema_version: payload.schema_version,
        session_id: payload.session_id,
        revision: nextRevision,
        generated_at: payload.generated_at,
        active_run_id: payload.active_run_id,
        ...payload.changed_tabs,
      };
      state.inspector.revision = nextRevision;
      state.inspector.loading = false;
      state.inspector.error = "";
      state.inspector.stale = inspectorSnapshotIsStale(state.inspector.snapshot);
      break;
    }
    case "agents/snapshot":
      if (!agentControlMatchesCurrentSession(state, payload)) break;
      if (
        state.agents.snapshot?.session_id === payload.session_id
        && Number(payload.revision) < state.agents.revision
      ) break;
      state.agents.snapshot = payload;
      state.agents.revision = Number(payload.revision) || 0;
      state.agents.loading = false;
      state.agents.error = "";
      state.agents.stale = false;
      settleAgentActionFromSnapshot(state.agents);
      ensureAgentControlSelection(state.agents);
      break;
    case "agents/update": {
      if (!agentControlMatchesCurrentSession(state, payload)) break;
      const nextRevision = Number(payload.revision) || 0;
      if (nextRevision <= state.agents.revision) break;
      if (!state.agents.snapshot || nextRevision !== state.agents.revision + 1) {
        state.agents.loading = true;
        return [{
          type: "refresh_agents",
          knownRevision: state.agents.revision,
          sessionId: String(payload.session_id || state.currentSessionId || ""),
        }];
      }
      state.agents.snapshot = {
        ...state.agents.snapshot,
        schema_version: payload.schema_version,
        session_id: payload.session_id,
        revision: nextRevision,
        generated_at: payload.generated_at,
        ...payload.changed_sections,
      };
      state.agents.revision = nextRevision;
      state.agents.loading = false;
      state.agents.error = "";
      state.agents.stale = false;
      settleAgentActionFromSnapshot(state.agents);
      ensureAgentControlSelection(state.agents);
      break;
    }
    case "agents/action":
      if (String(payload.task_id || "") !== state.agents.actionPendingTaskId) break;
      state.agents.actionMessage = String(payload.message || "");
      state.agents.stopConfirmationTaskId = "";
      if (payload.accepted !== true) {
        state.agents.actionPendingTaskId = "";
      }
      break;
    case "run/started":
      resetRunCancellation(state);
      startRunActivity(state, record, payload);
      acceptUserMessage(state, record.request_id, payload.task ?? "");
      state.running = true;
      state.workingAnimationFrame = 0;
      state.currentTurnStartedAtMs = Date.now();
      state.currentTurnFirstTokenAtMs = null;
      break;
    case "run/completed": {
      if (!matchesActiveRunActivity(state, record.request_id)) break;
      const terminalStatus = deriveRunCompletionStatus(payload.status);
      state.running = false;
      state.workingAnimationFrame = 0;
      resetRunCancellation(state);
      finishRunActivity(state, terminalStatus);
      moveCompletionReceiptToEnd(state, payload.receipt_id, payload.run_id);
      if (payload.intent === "task" && state.activeTaskSubmission) {
        const taskState = terminalStatus === "completed" ? "completed" : "blocked";
        state.activeTaskSubmission.state = taskState;
        const taskMessage = state.messages.find(
          (message) => message.kind === "user"
            && message.taskId === String(payload.task_id ?? state.activeTaskSubmission.taskId),
        );
        if (taskMessage) {
          taskMessage.taskStatus = taskState;
          clearRenderCache(state.renderCache);
        }
      }
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
      clearPendingInteractions(state, "cancelled");
      return terminalRunActions(state, payload);
    }
    case "run/cancelled":
      if (!matchesActiveRunActivity(state, runCancelledTargetRequestId(record, payload))) break;
      state.running = false;
      state.workingAnimationFrame = 0;
      resetRunCancellation(state);
      finishRunActivity(state, "cancelled");
      finishActiveToolPrepare(state, "本轮执行已取消");
      state.activeToolPrepare = null;
      state.activeRuntimePhase = "";
      state.permission = null;
      clearPendingInteractions(state, "cancelled");
      if (payload.intent === "task" && state.activeTaskSubmission) {
        state.activeTaskSubmission.state = "blocked";
        const taskId = String(payload.task_id ?? state.activeTaskSubmission.taskId);
        const taskMessage = state.messages.find(
          (message) => message.kind === "user" && message.taskId === taskId,
        );
        if (taskMessage) taskMessage.taskStatus = "blocked";
      }
      clearRenderCache(state.renderCache);
      pushSystemMessage(
        state,
        "运行取消",
        `运行已取消。${payload.reason ? ` ${payload.reason}` : ""}`,
        "warning",
      );
      moveCompletionReceiptToEnd(state, payload.receipt_id, payload.run_id);
      return terminalRunActions(state, payload);
    case "session/replayed":
      dismissWelcome(state);
      jumpTimelineToLatest(state);
      state.currentSessionId = payload.session_id || state.currentSessionId;
      state.running = false;
      state.workingAnimationFrame = 0;
      discardActiveRunActivity(state);
      state.currentTurnStartedAtMs = null;
      state.currentTurnFirstTokenAtMs = null;
      state.lastFirstTokenLatencyMs = null;
      state.permission = null;
      clearPendingInteractions(state, "cancelled");
      resetRunCancellation(state);
      state.todo = null;
      state.activeToolPrepare = null;
      state.activeRuntimePhase = "";
      resetInspectorSnapshot(state.inspector);
      resetAgentControlSnapshot(state.agents);
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
    case "error": {
      dismissWelcome(state);
      if (payload.code === "inspector_refresh_failed") {
        state.inspector.loading = false;
        state.inspector.error = payload.message ?? "Inspector 刷新失败，已保留上一次快照。";
        state.inspector.stale = Boolean(state.inspector.snapshot);
        break;
      }
      if (["agents_refresh_failed", "agents_snapshot_failed"].includes(payload.code)) {
        state.agents.loading = false;
        state.agents.error = payload.message ?? "Agent 页面刷新失败，已保留上一次快照。";
        state.agents.stale = Boolean(state.agents.snapshot);
        break;
      }
      if (String(payload.code || "").startsWith("agents_")) {
        state.agents.loading = false;
        state.agents.error = payload.message ?? "Agent 页面操作失败。";
        state.agents.stale = Boolean(state.agents.snapshot);
        state.agents.stopConfirmationTaskId = "";
        state.agents.actionPendingTaskId = "";
        state.agents.actionMessage = state.agents.error;
        break;
      }
      const errorRequestId = String(record.request_id ?? "");
      const hasActiveRunActivity = Boolean(state.activeRunActivity);
      const activeRunRequestId = String(state.activeRunActivity?.requestId ?? "").trim();
      const matchesActiveRun = Boolean(errorRequestId.trim() && activeRunRequestId)
        && matchesActiveRunActivity(state, errorRequestId);
      const isCorrelatedCancelError = Boolean(state.cancelRequestId)
        && state.cancelRequestId === errorRequestId;
      if (!hasActiveRunActivity || matchesActiveRun || isCorrelatedCancelError) {
        state.running = false;
        state.workingAnimationFrame = 0;
      }
      if (isCorrelatedCancelError) {
        resetRunCancellation(state);
      }
      if (
        payload.intent === "task"
        && state.activeTaskSubmission
        && state.activeTaskSubmission.requestId === String(record.request_id ?? "")
      ) {
        const taskState = payload.task_status === "completed" ? "completed" : "blocked";
        state.activeTaskSubmission.state = taskState;
        const taskId = String(payload.task_id ?? state.activeTaskSubmission.taskId);
        const taskMessage = state.messages.find(
          (message) => message.kind === "user" && message.taskId === taskId,
        );
        if (taskMessage) {
          taskMessage.taskStatus = taskState;
          clearRenderCache(state.renderCache);
        }
      }
      if (hasActiveRunActivity && (matchesActiveRun || isCorrelatedCancelError)) {
        finishRunActivity(state, "failed");
      }
      failUserMessage(state, record.request_id, {
        code: payload.code ?? "error",
        message: payload.message ?? "发送失败。",
      });
      pushSystemMessage(state, "error", payload.message ?? "未知错误", "error");
      moveCompletionReceiptToEnd(state, payload.receipt_id, payload.run_id);
      break;
    }
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

function inspectorMatchesCurrentSession(state, payload) {
  const sessionId = String(payload?.session_id || "");
  return Boolean(sessionId)
    && (!state.currentSessionId || sessionId === String(state.currentSessionId));
}

function inspectorSnapshotIsStale(snapshot) {
  return ["plan", "tools", "context", "changes", "tests"]
    .some((name) => snapshot?.[name]?.state === "stale");
}

function resetInspectorSnapshot(inspector) {
  inspector.loading = Boolean(inspector.open);
  inspector.revision = 0;
  inspector.snapshot = null;
  inspector.error = "";
  inspector.stale = false;
}

function agentControlMatchesCurrentSession(state, payload) {
  const sessionId = String(payload?.session_id || "");
  return Boolean(sessionId)
    && (!state.currentSessionId || sessionId === String(state.currentSessionId));
}

function resetAgentControlSnapshot(agents) {
  agents.loading = Boolean(agents.open);
  agents.revision = 0;
  agents.snapshot = null;
  agents.error = "";
  agents.stale = false;
  agents.stopConfirmationTaskId = "";
  agents.actionPendingTaskId = "";
  agents.actionMessage = "";
}

export function toggleAgentControlCenter(state, send, forceOpen = null) {
  const open = forceOpen === null ? !Boolean(state.agents.open) : Boolean(forceOpen);
  if (open === state.agents.open && state.route?.name === (open ? "agents" : "conversation")) {
    if (open) {
      state.agents.loading = true;
      send("agents/request", {
        open: true,
        known_revision: state.agents.revision,
        session_id: String(state.currentSessionId || ""),
      });
    }
    return open;
  }
  state.agents.open = open;
  state.agents.loading = open;
  state.agents.error = "";
  state.agents.stopConfirmationTaskId = "";
  if (open) {
    state.route = {
      name: "agents",
      originAnchor: {
        scrollOffset: Math.max(0, Number(state.scrollOffset) || 0),
        followTail: Boolean(state.followTail),
      },
    };
  } else {
    state.route = { name: "conversation", originAnchor: null };
    state.agents.detailId = "";
  }
  send("agents/request", {
    open,
    known_revision: state.agents.revision,
    session_id: String(state.currentSessionId || ""),
  });
  return open;
}

export function handleAgentControlKey(state, key, send) {
  const agents = state.agents;
  if (!agents?.open || state.route?.name !== "agents") return false;
  const normalized = String(key || "").toLowerCase();

  if (agents.stopConfirmationTaskId) {
    if (normalized === "y") {
      if (!agents.actionPendingTaskId) {
        agents.actionPendingTaskId = agents.stopConfirmationTaskId;
        agents.actionMessage = "正在请求停止…";
        send("agents/stop", {
          session_id: String(state.currentSessionId || ""),
          task_id: agents.stopConfirmationTaskId,
          reason: "用户在 Agent 控制中心确认停止。",
        });
      }
      agents.stopConfirmationTaskId = "";
      return true;
    }
    if (normalized === "n" || key === INPUT_KEYS.escape) {
      agents.stopConfirmationTaskId = "";
      return true;
    }
    return true;
  }

  if (key === INPUT_KEYS.escape) {
    if (agents.detailId) {
      agents.detailId = "";
    } else {
      toggleAgentControlCenter(state, send, false);
    }
    return true;
  }
  if (key === INPUT_KEYS.tab || key === "]" || key === INPUT_KEYS.right || key === INPUT_KEYS.rightAlt) {
    selectAgentControlTab(agents, 1);
    return true;
  }
  if (key === INPUT_KEYS.shiftTab || key === "[" || key === INPUT_KEYS.left || key === INPUT_KEYS.leftAlt) {
    selectAgentControlTab(agents, -1);
    return true;
  }
  if (key === INPUT_KEYS.up || key === INPUT_KEYS.upAlt) {
    selectAgentControlItem(agents, -1);
    return true;
  }
  if (key === INPUT_KEYS.down || key === INPUT_KEYS.downAlt) {
    selectAgentControlItem(agents, 1);
    return true;
  }
  if (key === "\r" || key === "\n" || key === INPUT_KEYS.ctrlEnter) {
    agents.detailId = selectedAgentControlId(agents);
    return true;
  }
  if (normalized === "r") {
    agents.loading = true;
    send("agents/request", {
      open: true,
      known_revision: agents.revision,
      session_id: String(state.currentSessionId || ""),
    });
    return true;
  }
  if (normalized === "x" && agents.selectedTab === "executions") {
    const taskId = selectedAgentControlId(agents);
    const execution = (agents.snapshot?.executions || []).find((item) => item.task_id === taskId);
    if (execution?.stop_supported === true && !agents.actionPendingTaskId) {
      agents.stopConfirmationTaskId = taskId;
    }
    return true;
  }
  return true;
}

function selectAgentControlTab(agents, delta) {
  const current = Math.max(0, AGENT_CONTROL_TABS.indexOf(agents.selectedTab));
  agents.selectedTab = AGENT_CONTROL_TABS[
    (current + delta + AGENT_CONTROL_TABS.length) % AGENT_CONTROL_TABS.length
  ];
  agents.detailId = "";
  ensureAgentControlSelection(agents);
}

function selectAgentControlItem(agents, delta) {
  const ids = agentControlItemIds(agents);
  if (!ids.length) return false;
  const selected = selectedAgentControlId(agents);
  const current = Math.max(0, ids.indexOf(selected));
  agents.selectedByTab[agents.selectedTab] = ids[
    (current + delta + ids.length) % ids.length
  ];
  agents.scrollByTab[agents.selectedTab] = ids.indexOf(
    agents.selectedByTab[agents.selectedTab],
  );
  agents.detailId = "";
  return true;
}

function selectedAgentControlId(agents) {
  ensureAgentControlSelection(agents);
  return String(agents.selectedByTab[agents.selectedTab] || "");
}

function ensureAgentControlSelection(agents) {
  const ids = agentControlItemIds(agents);
  const selected = String(agents.selectedByTab[agents.selectedTab] || "");
  agents.selectedByTab[agents.selectedTab] = ids.includes(selected) ? selected : (ids[0] || "");
  if (agents.detailId && !ids.includes(agents.detailId)) agents.detailId = "";
}

function agentControlItemIds(agents) {
  const snapshot = agents.snapshot || {};
  if (agents.selectedTab === "agents") {
    return (snapshot.agents || []).map((item) => String(item.name || "")).filter(Boolean);
  }
  if (agents.selectedTab === "executions") {
    return (snapshot.executions || []).map((item) => String(item.task_id || "")).filter(Boolean);
  }
  return [
    ...(snapshot.team_messages || []).map(
      (item) => `message:${item.timestamp}:${item.sender}:${item.topic}`,
    ),
    ...(snapshot.blackboard || []).map((item) => `blackboard:${item.key}`),
  ];
}

function settleAgentActionFromSnapshot(agents) {
  const taskId = agents.actionPendingTaskId;
  if (!taskId) return;
  const execution = (agents.snapshot?.executions || []).find((item) => item.task_id === taskId);
  const terminalStatuses = new Set([
    "completed",
    "error",
    "failed",
    "timeout",
    "max_turns",
    "cancelled",
  ]);
  if (!execution || terminalStatuses.has(execution.status)) {
    agents.actionPendingTaskId = "";
    agents.actionMessage = execution?.status
      ? `执行已进入终态：${execution.status}`
      : "执行已结束。";
  }
}

export function toggleRuntimeInspector(state, send) {
  const open = !Boolean(state.inspector.open);
  state.inspector.open = open;
  state.inspector.focused = false;
  state.inspector.loading = open;
  state.inspector.error = "";
  send("inspector/request", {
    open,
    known_revision: state.inspector.revision,
    session_id: String(state.currentSessionId || ""),
  });
  return open;
}

export function handleRuntimeInspectorKey(state, key, send) {
  const inspector = state.inspector;
  if (!inspector?.open) return false;
  if (!inspector.focused) {
    if (key === INPUT_KEYS.tab) {
      inspector.focused = true;
      if (state.taskPanel) state.taskPanel.focused = false;
      return true;
    }
    if (key === INPUT_KEYS.escape) {
      toggleRuntimeInspector(state, send);
      return true;
    }
    return false;
  }

  if (key === INPUT_KEYS.escape) {
    inspector.focused = false;
    return true;
  }
  if (key === INPUT_KEYS.tab) return true;
  if (["]", INPUT_KEYS.right, INPUT_KEYS.rightAlt].includes(key)) {
    selectRuntimeInspectorTab(inspector, 1);
    return true;
  }
  if (["[", INPUT_KEYS.left, INPUT_KEYS.leftAlt].includes(key)) {
    selectRuntimeInspectorTab(inspector, -1);
    return true;
  }
  if (key === INPUT_KEYS.up || key === INPUT_KEYS.upAlt) {
    selectRuntimeInspectorItem(inspector, -1);
    return true;
  }
  if (key === INPUT_KEYS.down || key === INPUT_KEYS.downAlt) {
    selectRuntimeInspectorItem(inspector, 1);
    return true;
  }
  if (key === "\r" || key === "\n" || key === INPUT_KEYS.ctrlEnter) {
    toggleRuntimeInspectorExpanded(inspector);
    return true;
  }
  return false;
}

function selectRuntimeInspectorTab(inspector, delta) {
  const current = Math.max(0, RUNTIME_INSPECTOR_TABS.indexOf(inspector.selectedTab));
  inspector.selectedTab = RUNTIME_INSPECTOR_TABS[
    (current + delta + RUNTIME_INSPECTOR_TABS.length) % RUNTIME_INSPECTOR_TABS.length
  ];
  clampRuntimeInspectorSelection(inspector);
}

function selectRuntimeInspectorItem(inspector, delta) {
  const count = runtimeInspectorItemCount(inspector);
  if (!count) return false;
  const tab = inspector.selectedTab;
  const current = Math.min(count - 1, Math.max(0, Number(inspector.selectionByTab[tab]) || 0));
  inspector.selectionByTab[tab] = (current + delta + count) % count;
  return true;
}

function toggleRuntimeInspectorExpanded(inspector) {
  const count = runtimeInspectorItemCount(inspector);
  if (!count) return false;
  const tab = inspector.selectedTab;
  const index = Math.min(count - 1, Math.max(0, Number(inspector.selectionByTab[tab]) || 0));
  const expanded = inspector.expandedByTab[tab] && typeof inspector.expandedByTab[tab] === "object"
    ? { ...inspector.expandedByTab[tab] }
    : {};
  expanded[String(index)] = !Boolean(expanded[String(index)]);
  inspector.expandedByTab[tab] = expanded;
  return true;
}

function clampRuntimeInspectorSelection(inspector) {
  const count = runtimeInspectorItemCount(inspector);
  const tab = inspector.selectedTab;
  inspector.selectionByTab[tab] = count
    ? Math.min(count - 1, Math.max(0, Number(inspector.selectionByTab[tab]) || 0))
    : 0;
}

function runtimeInspectorItemCount(inspector) {
  const tab = inspector.snapshot?.[inspector.selectedTab];
  if (!tab || typeof tab !== "object") return 0;
  if (inspector.selectedTab === "plan") return Array.isArray(tab.items) ? tab.items.length : 0;
  if (inspector.selectedTab === "tools") return Array.isArray(tab.items) ? tab.items.length : 0;
  if (inspector.selectedTab === "changes") return Array.isArray(tab.items) ? tab.items.length : 0;
  if (inspector.selectedTab === "tests") return Array.isArray(tab.validations) ? tab.validations.length : 0;
  return 0;
}

function startRunActivity(state, record, payload) {
  const requestId = String(record.request_id ?? "");
  if (state.activeRunActivity) {
    return state.activeRunActivity;
  }
  const activity = {
    kind: "run_activity",
    id: nextMessageId(state, "run_activity"),
    requestId,
    intent: payload.intent === "task" ? "task" : "chat",
    taskId: String(payload.task_id ?? payload.task?.id ?? ""),
    missionId: String(payload.mission_id ?? payload.mission?.id ?? ""),
    status: "running",
    phase: "preparing",
    phaseLabel: RUN_ACTIVITY_PHASE_LABELS.preparing,
    turn: 0,
    model: "",
    toolCalls: {},
    toolCallOrder: [],
    nextFallbackToolId: 1,
    permissionCount: 0,
    permissionRequestIds: [],
    nextFallbackPermissionId: 1,
    perfPhases: [],
    startedAtMs: Date.now(),
    completedAtMs: null,
    durationMs: 0,
  };
  state.activeRunActivity = activity;
  state.messages.push(activity);
  clearRenderCache(state.renderCache);
  return activity;
}

function finishRunActivity(state, status) {
  const activity = state.activeRunActivity;
  if (!activity) return null;
  const completedAtMs = Date.now();
  activity.status = status;
  activity.phase = status;
  activity.phaseLabel = RUN_ACTIVITY_PHASE_LABELS[status] ?? RUN_ACTIVITY_PHASE_LABELS.failed;
  activity.completedAtMs = completedAtMs;
  activity.durationMs = Math.max(0, completedAtMs - Number(activity.startedAtMs || completedAtMs));
  const messageIndex = state.messages.indexOf(activity);
  if (messageIndex >= 0 && messageIndex !== state.messages.length - 1) {
    state.messages.splice(messageIndex, 1);
    state.messages.push(activity);
  }
  state.activeRunActivity = null;
  clearRenderCache(state.renderCache);
  return activity;
}

function addCompletionReceipt(state, receipt, requestId) {
  const receiptId = String(receipt.receipt_id ?? "");
  const runId = String(receipt.run_id ?? "");
  if (!receiptId || !runId) return null;
  const existing = state.messages.find(
    (message) => message.kind === "completion_receipt"
      && message.receiptId === receiptId,
  );
  if (existing) return existing;
  const message = {
    kind: "completion_receipt",
    id: nextMessageId(state, "completion_receipt"),
    requestId: String(requestId ?? ""),
    receiptId,
    runId,
    receipt,
  };
  state.messages.push(message);
  clearRenderCache(state.renderCache);
  return message;
}

function moveCompletionReceiptToEnd(state, receiptId, runId) {
  const normalizedReceiptId = String(receiptId ?? "");
  const normalizedRunId = String(runId ?? "");
  if (!normalizedReceiptId && !normalizedRunId) return null;
  const index = state.messages.findIndex(
    (message) => message.kind === "completion_receipt"
      && (
        (normalizedReceiptId && message.receiptId === normalizedReceiptId)
        || (normalizedRunId && message.runId === normalizedRunId)
      ),
  );
  if (index < 0) return null;
  const [message] = state.messages.splice(index, 1);
  state.messages.push(message);
  clearRenderCache(state.renderCache);
  return message;
}

function terminalRunActions(state, payload) {
  const actions = state.taskPanel.pinned ? [taskPanelRefreshAction(state)] : [];
  const receiptId = String(payload.receipt_id ?? "");
  const runId = String(payload.run_id ?? "");
  if ((receiptId || runId) && !hasCompletionReceipt(state, receiptId, runId)) {
    actions.push({
      type: "request_completion_receipt",
      sessionId: String(state.currentSessionId ?? ""),
      receiptId,
      runId,
    });
  }
  return actions;
}

function hasCompletionReceipt(state, receiptId, runId) {
  return state.messages.some(
    (message) => message.kind === "completion_receipt"
      && (
        (receiptId && message.receiptId === receiptId)
        || (runId && message.runId === runId)
      ),
  );
}

function discardActiveRunActivity(state) {
  const activity = state.activeRunActivity;
  if (!activity) return;
  state.messages = state.messages.filter((message) => message !== activity);
  state.activeRunActivity = null;
  clearRenderCache(state.renderCache);
}

function matchesActiveRunActivity(state, targetRequestId) {
  const activity = state.activeRunActivity;
  if (!activity) return true;
  const targetId = String(targetRequestId ?? "").trim();
  const activityRequestId = String(activity.requestId ?? "").trim();
  if (!activityRequestId) return true;
  return Boolean(targetId) && activityRequestId === targetId;
}

function runCancelledTargetRequestId(record, payload) {
  return String(payload.target_request_id ?? "").trim() || String(record.request_id ?? "").trim();
}

function deriveRunCompletionStatus(status) {
  const normalized = String(status ?? "").trim().toLowerCase();
  return !normalized || normalized === "completed" ? "completed" : "failed";
}

function updateRunActivityPhase(state, phase) {
  const activity = state.activeRunActivity;
  if (!activity || activity.status !== "running" || !RUN_ACTIVITY_PHASE_LABELS[phase]) return;
  const phaseLabel = RUN_ACTIVITY_PHASE_LABELS[phase];
  if (activity.phase === phase && activity.phaseLabel === phaseLabel) return;
  activity.phase = phase;
  activity.phaseLabel = phaseLabel;
  clearRenderCache(state.renderCache);
}

function updateRunActivityRuntime(state, message) {
  const activity = state.activeRunActivity;
  if (!activity || activity.status !== "running") return;
  let changed = false;
  if (message.phase === "run_started") {
    const phaseLabel = boundedText(message.label, MAX_RUN_ACTIVITY_PHASE_LABEL_CHARS).trim();
    if (phaseLabel && activity.phaseLabel !== phaseLabel) {
      activity.phaseLabel = phaseLabel;
      changed = true;
    }
  }
  if (message.phase === "turn_start") {
    const turn = Number(message.turn);
    if (Number.isFinite(turn) && activity.turn !== turn) {
      activity.turn = turn;
      changed = true;
    }
    const model = String(message.model ?? "");
    if (model && activity.model !== model) {
      activity.model = model;
      changed = true;
    }
  }
  if (message.phase === "perf_phase") {
    const label = String(message.label ?? "性能阶段");
    const durationMs = Math.max(0, Number(message.duration_ms) || 0);
    activity.perfPhases = [...activity.perfPhases, { label, durationMs }].slice(-MAX_RUN_ACTIVITY_PERF_PHASES);
    changed = true;
  }
  if (changed) clearRenderCache(state.renderCache);
}

function updateRunActivityTool(state, message, status) {
  const activity = state.activeRunActivity;
  if (!activity || activity.status !== "running") return;
  const name = String(message.tool_name ?? "未知工具");
  const callId = String(message.tool_call_id ?? "").trim();
  let key = callId;
  let tool = key ? activity.toolCalls[key] : null;
  let created = false;

  if (!tool && key) {
    const preparedFallback = findLatestRunTool(activity, name, ["prepared"], true);
    if (preparedFallback) {
      key = replaceRunToolKey(activity, preparedFallback.key, key);
      tool = activity.toolCalls[key];
    }
  }
  if (!tool && !key && (status !== "prepared" || message.phase !== "start")) {
    const pendingStatuses = status === "prepared"
      ? ["prepared"]
      : status === "running"
        ? ["prepared"]
        : ["running", "prepared"];
    const fallback = findLatestRunTool(activity, name, pendingStatuses, true);
    if (fallback) {
      key = fallback.key;
      tool = fallback.tool;
    }
  }
  if (!tool) {
    if (activity.toolCallOrder.length >= MAX_RUN_ACTIVITY_TOOLS) return;
    key = key || `fallback:${activity.nextFallbackToolId++}`;
    tool = { name, status };
    activity.toolCalls[key] = tool;
    activity.toolCallOrder.push(key);
    created = true;
  }

  const nextName = name || tool.name;
  const changed = tool.name !== nextName || tool.status !== status;
  tool.name = nextName;
  tool.status = status;
  if (created || changed) {
    clearRenderCache(state.renderCache);
  }
}

function findLatestRunTool(activity, name, statuses, fallbackOnly) {
  for (let index = activity.toolCallOrder.length - 1; index >= 0; index -= 1) {
    const key = activity.toolCallOrder[index];
    const tool = activity.toolCalls[key];
    if (!tool || tool.name !== name || !statuses.includes(tool.status)) continue;
    if (fallbackOnly && !key.startsWith("fallback:")) continue;
    return { key, tool };
  }
  return null;
}

function replaceRunToolKey(activity, previousKey, nextKey) {
  if (previousKey === nextKey || activity.toolCalls[nextKey]) return nextKey;
  activity.toolCalls[nextKey] = activity.toolCalls[previousKey];
  delete activity.toolCalls[previousKey];
  const index = activity.toolCallOrder.lastIndexOf(previousKey);
  if (index >= 0) activity.toolCallOrder[index] = nextKey;
  return nextKey;
}

function normalizeRunToolStatus(status) {
  const normalized = String(status ?? "unknown").trim().toLowerCase();
  if (["success", "succeeded", "completed", "done", "ok"].includes(normalized)) return "success";
  if (["cancelled", "canceled", "cancel"].includes(normalized)) return "cancelled";
  if (["error", "failed", "failure"].includes(normalized)) return "error";
  return normalized || "unknown";
}

function updateRunActivityPermission(state, requestId) {
  const activity = state.activeRunActivity;
  if (!activity || activity.status !== "running") return;
  const id = String(requestId ?? "").trim() || `missing:${activity.nextFallbackPermissionId++}`;
  if (activity.permissionRequestIds.includes(id)) return;
  if (activity.permissionRequestIds.length >= MAX_RUN_ACTIVITY_PERMISSION_IDS) return;
  activity.permissionRequestIds = [...activity.permissionRequestIds, id];
  activity.permissionCount += 1;
  clearRenderCache(state.renderCache);
}

function handleSubagentEvent(state, message) {
  const sourceMessageId = boundedText(message.message_id, 200).trim();
  if (sourceMessageId && state.messages.some(
    (item) => item.kind === "subagent_activity" && item.sourceMessageIds?.includes(sourceMessageId),
  )) return;
  const status = boundedText(sanitizeTerminalText(message.status), 80).trim().toLowerCase() || "event";
  const taskId = boundedText(sanitizeTerminalText(message.task_id), 200).trim();
  const incomingAgentName = boundedText(sanitizeTerminalText(message.agent_name), 200).trim();
  const description = boundedText(sanitizeTerminalText(message.description), 4000).trim();
  const latestMessage = boundedText(sanitizeTerminalText(message.message), 2000).trim();
  const aggregationKey = taskId || `agent:${incomingAgentName || "未匹配"}:${description.slice(0, 200)}`;
  const matching = [...state.messages].reverse().find(
    (item) => item.kind === "subagent_activity" && item.aggregationKey === aggregationKey,
  );
  const agentName = incomingAgentName || matching?.agentName || "未匹配";
  const startsNewRun = status === "started" && matching && isTerminalSubagentStatus(matching.status);
  let activity = startsNewRun ? null : matching;
  const timestampMs = normalizeSubagentTimestamp(message.timestamp);

  if (!activity) {
    activity = {
      kind: "subagent_activity",
      id: nextMessageId(state, "subagent"),
      aggregationKey,
      taskId,
      agentName,
      description,
      status,
      latestMessage,
      tokens: normalizeNonNegativeNumber(message.tokens, true),
      cost: normalizeNonNegativeNumber(message.cost, false),
      startedAtMs: timestampMs,
      updatedAtMs: timestampMs,
      durationMs: 0,
      events: [],
      sourceMessageIds: [],
    };
    state.messages.push(activity);
  }

  activity.taskId = taskId || activity.taskId;
  activity.agentName = incomingAgentName || activity.agentName;
  activity.description = description || activity.description;
  activity.status = status;
  activity.latestMessage = latestMessage || activity.latestMessage;
  activity.tokens = normalizeNonNegativeNumber(message.tokens, true) || activity.tokens || 0;
  activity.cost = normalizeNonNegativeNumber(message.cost, false) || activity.cost || 0;
  activity.startedAtMs = Number(activity.startedAtMs) || timestampMs;
  activity.updatedAtMs = Math.max(Number(activity.updatedAtMs) || 0, timestampMs);
  activity.durationMs = Math.max(0, activity.updatedAtMs - activity.startedAtMs);
  activity.events = [
    ...(activity.events || []),
    { status, message: latestMessage, timestampMs },
  ].slice(-MAX_SUBAGENT_ACTIVITY_EVENTS);
  activity.sourceMessageIds = [
    ...(activity.sourceMessageIds || []),
    sourceMessageId,
  ].filter(Boolean).slice(-MAX_SUBAGENT_ACTIVITY_EVENTS);
  clearRenderCache(state.renderCache);
}

function normalizeSubagentTimestamp(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) return Date.now();
  return parsed >= 1_000_000_000_000 ? Math.round(parsed) : Math.round(parsed * 1000);
}

function normalizeNonNegativeNumber(value, integer) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) return 0;
  return integer ? Math.round(parsed) : parsed;
}

function isTerminalSubagentStatus(status) {
  return ["completed", "failed", "error", "cancelled", "canceled"].includes(
    String(status || "").toLowerCase(),
  );
}

export function handleUiMessage(state, message) {
  switch (message.type) {
    case "user":
      appendAcceptedUserMessage(state, message.content ?? "", message.request_id ?? "", {
        isCommand: Boolean(message.is_command),
      });
      break;
    case "assistant_stream":
      if (message.phase === "start") {
        const hasTools = Object.keys(state.activeRunActivity?.toolCalls ?? {}).length > 0;
        updateRunActivityPhase(state, hasTools ? "summarizing" : "generating");
      }
      handleAssistantStream(state, message);
      break;
    case "thinking":
      handleThinking(state, message);
      break;
    case "tool_prepare":
      updateRunActivityTool(state, message, "prepared");
      updateRunActivityPhase(state, "executing");
      handleTodoPrepare(state, message);
      handleToolPrepare(state, message);
      break;
    case "tool_use":
      updateRunActivityTool(state, message, "running");
      updateRunActivityPhase(state, "executing");
      handleToolUse(state, message);
      break;
    case "tool_result":
      updateRunActivityTool(state, message, normalizeRunToolStatus(message.status));
      updateRunActivityPhase(state, "executing");
      handleToolResult(state, message);
      break;
    case "todo_status":
      handleTodoStatus(state, message);
      break;
    case "permission_bubble":
      state.messages.push({ kind: "permission", message });
      break;
    case "runtime_status":
      updateRunActivityRuntime(state, message);
      if (message.phase === "perf_phase") {
        state.activeRuntimePhase = `${message.label}: ${message.duration_ms}ms`;
      }
      break;
    case "subagent_event":
      handleSubagentEvent(state, message);
      break;
    case "recovery":
    case "context_compact":
    case "runtime_notification":
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
  updateRunActivityPermission(state, requestId);
  updateRunActivityPhase(state, "awaiting_permission");
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
  updateRunActivityPhase(state, "executing");
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

export function handleInteractionRequest(state, record) {
  const payload = record.payload ?? {};
  const requestId = String(payload.request_id ?? record.request_id ?? record.id ?? "");
  if (
    !requestId
    || findInteraction(state, requestId)
    || state.messages.some(
      (message) => message.kind === "interaction" && message.requestId === requestId,
    )
  ) return;
  const queued = Boolean(state.interaction);
  const interaction = {
    requestId,
    payload,
    selectedIndex: 0,
    customMode: false,
    input: "",
    inputCursor: 0,
    inputPreferredColumn: null,
    inputHistoryCursor: null,
    inputHistoryDraft: "",
    submitting: false,
  };
  state.messages.push({
    kind: "interaction",
    id: nextMessageId(state, "interaction"),
    requestId,
    message: { ...payload, request_id: requestId, status: queued ? "queued" : "needs_input" },
  });
  if (queued) {
    state.interactionQueue.push(interaction);
  } else {
    state.interaction = interaction;
  }
  updateRunActivityPhase(state, "awaiting_input");
  clearRenderCache(state.renderCache);
}

export function handleInteractionResolved(state, payload) {
  const requestId = String(payload.request_id ?? "");
  const card = state.messages.find(
    (message) => message.kind === "interaction" && message.requestId === requestId,
  );
  if (card) card.message = { ...(card.message ?? {}), ...payload, status: "answered" };
  if (state.interaction?.requestId === requestId) {
    state.interaction = state.interactionQueue.shift() ?? null;
    if (state.interaction) {
      const nextCard = state.messages.find(
        (message) => message.kind === "interaction"
          && message.requestId === state.interaction.requestId,
      );
      if (nextCard) {
        nextCard.message = { ...(nextCard.message ?? {}), status: "needs_input" };
      }
    }
  } else {
    state.interactionQueue = state.interactionQueue.filter((item) => item.requestId !== requestId);
  }
  updateRunActivityPhase(state, state.interaction ? "awaiting_input" : "executing");
  clearRenderCache(state.renderCache);
}

export function handleInteractionKey(state, chunk, send) {
  const interaction = state.interaction;
  if (!interaction) return false;
  if (interaction.submitting) return true;
  const options = Array.isArray(interaction.payload?.options) ? interaction.payload.options : [];
  const choiceCount = options.length + (interaction.payload?.allow_custom ? 1 : 0);
  if (choiceCount <= 0) return true;

  if (interaction.customMode) {
    if (chunk === INPUT_KEYS.escape) {
      interaction.customMode = false;
      return true;
    }
    if (chunk === "\r" || chunk === "\n" || chunk === INPUT_KEYS.ctrlEnter) {
      const customText = String(interaction.input ?? "").trim();
      if (!customText) return true;
      interaction.submitting = true;
      send("interaction_response", {
        request_id: interaction.requestId,
        kind: "custom",
        custom_text: customText,
      });
      return true;
    }
    if (chunk === "\u007f" || chunk === "\b") return backspaceInput(interaction) || true;
    if (chunk === INPUT_KEYS.delete) return deleteInputForward(interaction) || true;
    if ([INPUT_KEYS.left, INPUT_KEYS.leftAlt].includes(chunk)) {
      moveInputCursor(interaction, "left");
      return true;
    }
    if ([INPUT_KEYS.right, INPUT_KEYS.rightAlt].includes(chunk)) {
      moveInputCursor(interaction, "right");
      return true;
    }
    if ([INPUT_KEYS.home, INPUT_KEYS.homeAlt, INPUT_KEYS.homeSs3, INPUT_KEYS.ctrlA].includes(chunk)) {
      moveInputCursor(interaction, "home");
      return true;
    }
    if ([INPUT_KEYS.end, INPUT_KEYS.endAlt, INPUT_KEYS.endSs3, INPUT_KEYS.ctrlE].includes(chunk)) {
      moveInputCursor(interaction, "end");
      return true;
    }
    if (chunk >= " " && chunk !== "\x7f") {
      insertInputText(interaction, chunk);
      interaction.input = Array.from(interaction.input).slice(0, 4_000).join("");
      interaction.inputCursor = Math.min(interaction.inputCursor, Array.from(interaction.input).length);
    }
    return true;
  }

  if ([INPUT_KEYS.up, INPUT_KEYS.upAlt].includes(chunk)) {
    interaction.selectedIndex = (interaction.selectedIndex - 1 + choiceCount) % choiceCount;
    return true;
  }
  if ([INPUT_KEYS.down, INPUT_KEYS.downAlt, INPUT_KEYS.tab].includes(chunk)) {
    interaction.selectedIndex = (interaction.selectedIndex + 1) % choiceCount;
    return true;
  }
  if (/^[1-9]$/.test(chunk)) {
    const index = Number(chunk) - 1;
    if (index < choiceCount) interaction.selectedIndex = index;
    return true;
  }
  if (chunk === "\r" || chunk === "\n") {
    if (interaction.selectedIndex >= options.length) {
      interaction.customMode = true;
      return true;
    }
    const option = options[interaction.selectedIndex];
    interaction.submitting = true;
    send("interaction_response", {
      request_id: interaction.requestId,
      kind: "option",
      value: String(option.value ?? ""),
    });
    return true;
  }
  return true;
}

function findInteraction(state, requestId) {
  if (state.interaction?.requestId === requestId) return state.interaction;
  return state.interactionQueue.find((item) => item.requestId === requestId) ?? null;
}

function clearPendingInteractions(state, status) {
  const requestIds = new Set([
    state.interaction?.requestId,
    ...(state.interactionQueue ?? []).map((item) => item.requestId),
  ].filter(Boolean));
  for (const message of state.messages) {
    if (message.kind !== "interaction" || !requestIds.has(message.requestId)) continue;
    message.message = { ...(message.message ?? {}), status };
  }
  state.interaction = null;
  state.interactionQueue = [];
  clearRenderCache(state.renderCache);
}

function permissionChoiceStatus(choice) {
  if (choice === "allow" || choice === "allow_once") return "allowed";
  if (choice === "deny") return "denied";
  if (choice === "bypass") return "bypass_enabled";
  if (choice === "grant_session") return "granted";
  return String(choice || "resolved");
}

function modeNoticeText(mode) {
  if (mode === "plan") {
    return "已切换到 plan：只读规划模式，写文件和执行命令会被拦截。";
  }
  if (mode === "bypass") {
    return "已切换到 bypass：所有工具权限直接放行，请只在可信工作区使用。";
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
  const commandText = String(text ?? "").trim();
  if (["/q", "/quit", "/exit"].includes(commandText.toLowerCase())) {
    return { type: "exit" };
  }
  if (commandText === "/agents") {
    toggleAgentControlCenter(state, send, true);
    return;
  }
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
  if (text === "/retry" || text.startsWith("/retry ")) {
    return retryUserMessage(state, send, text.slice("/retry".length).trim());
  }
  if (text.startsWith("/load ")) {
    send("resume", { session_id: text.slice(6).trim() });
    return;
  }
  if (commandText === "/chat") {
    setComposerIntent(state, "chat");
    return;
  }
  if (commandText === "/task") {
    setComposerIntent(state, "task");
    return;
  }
  if (commandText === "/task create") {
    pushSystemMessage(state, "任务输入", "请在 /task create 后填写任务内容。", "warning");
    return;
  }
  if (commandText.startsWith("/task create ")) {
    return submitTaskMessage(state, commandText.slice("/task create ".length).trim(), send);
  }
  const taskDetail = commandText.match(/^\/task\s+#?(\d+)$/);
  if (taskDetail) {
    handleTasksCommand(state, `/tasks detail ${taskDetail[1]}`, send);
    return;
  }
  if (commandText.startsWith("/task ")) {
    return submitTaskMessage(state, commandText.slice("/task ".length).trim(), send);
  }
  if (text === "/tasks" || text.startsWith("/tasks ")) {
    handleTasksCommand(state, text, send);
    return;
  }
  if (text === "/permissions" || text.startsWith("/permissions ")) {
    const raw = text.slice("/permissions".length).trim();
    if (raw === "revoke all") {
      send("permission_revoke", { scope: "all" });
      return;
    }
    if (raw.startsWith("revoke ")) {
      const grantId = raw.slice("revoke ".length).trim();
      if (grantId) {
        send("permission_revoke", { grant_id: grantId });
        return;
      }
    }
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
  if (text === "/effort" || text.startsWith("/effort ")) {
    send("submit", { text });
    return;
  }
  if (text === "/clear" || text === "/c") {
    state.messages = [];
    state.tools = [];
    state.activeRunActivity = null;
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
  if (state.composerIntent === "task") {
    return submitTaskMessage(state, text, send);
  }
  return submitUserMessage(state, text, send);
}

export function toggleComposerIntent(state) {
  return setComposerIntent(state, state.composerIntent === "task" ? "chat" : "task");
}

function setComposerIntent(state, intent) {
  const next = intent === "task" ? "task" : "chat";
  state.composerIntent = next;
  pushSystemMessage(
    state,
    "输入模式",
    next === "task"
      ? "已切换到任务模式。下一条普通输入会创建 Workbench 任务。"
      : "已切换到对话模式。普通输入不会创建新任务。",
    "info",
  );
  return next;
}

export function submitUserMessage(state, text, send, existingMessage = null) {
  return submitMessage(state, text, send, existingMessage, {
    eventType: "submit",
    intent: "chat",
    payload: {},
  });
}

export function requestRunCancel(state, send) {
  if (!state.running || state.cancelPending) return false;
  const requestId = `cancel-${state.nextCancelId++}`;
  try {
    send(
      "run_cancel",
      { reason: "用户按下 Ctrl+C" },
      { id: requestId },
    );
  } catch (error) {
    pushSystemMessage(
      state,
      "运行取消",
      `无法发送取消请求: ${error instanceof Error ? error.message : String(error)}`,
      "error",
    );
    return false;
  }
  state.cancelPending = true;
  state.cancelRequestId = requestId;
  pushSystemMessage(state, "运行取消", "正在停止当前运行... 再按 Ctrl+C 可强制退出。", "warning");
  return true;
}

function resetRunCancellation(state) {
  state.cancelPending = false;
  state.cancelRequestId = "";
}

export function submitTaskMessage(state, text, send, taskDraft = {}, existingMessage = null) {
  return submitMessage(state, text, send, existingMessage, {
    eventType: "task_submit",
    intent: "task",
    payload: taskDraft,
  });
}

function submitMessage(state, text, send, existingMessage, submission) {
  const content = String(text ?? "");
  const requestId = `submit-${state.nextSubmitId++}`;
  const message = existingMessage ?? {
    kind: "user",
    id: nextMessageId(state, "user"),
    content,
    attempt: 0,
  };
  message.requestId = requestId;
  message.content = content;
  message.deliveryStatus = "queued";
  message.attempt = Math.max(0, Number(message.attempt) || 0) + 1;
  message.errorCode = "";
  message.errorMessage = "";
  message.localOutbox = true;
  message.intent = submission.intent;
  message.submitType = submission.eventType;
  message.taskDraft = submission.intent === "task" ? { ...submission.payload } : null;
  if (!existingMessage) {
    state.messages.push(message);
  }
  dismissWelcome(state);

  try {
    send(submission.eventType, { text: content, ...submission.payload }, { id: requestId });
  } catch (error) {
    failUserMessage(state, requestId, {
      code: "transport_write_failed",
      message: `无法写入本地 Bridge: ${error instanceof Error ? error.message : String(error)}`,
    });
  }
  clearRenderCache(state.renderCache);
  return message;
}

export function retryUserMessage(state, send, requestId = "") {
  const normalizedRequestId = String(requestId ?? "").trim();
  const message = [...state.messages].reverse().find(
    (item) => item.kind === "user"
      && ["failed", "uncertain"].includes(item.deliveryStatus)
      && (!normalizedRequestId || item.requestId === normalizedRequestId),
  );
  if (!message) {
    pushSystemMessage(
      state,
      "retry",
      normalizedRequestId
        ? `没有可重试的消息: ${normalizedRequestId}`
        : "当前没有可重试的发送失败消息。",
      "warning",
    );
    return null;
  }
  if (message.submitType === "task_submit" || message.intent === "task") {
    return submitTaskMessage(state, message.content, send, message.taskDraft ?? {}, message);
  }
  return submitUserMessage(state, message.content, send, message);
}

export function acceptUserMessage(state, requestId, content = "") {
  const normalizedRequestId = String(requestId ?? "");
  if (!normalizedRequestId) return null;
  const message = state.messages.find(
    (item) => item.kind === "user" && item.requestId === normalizedRequestId,
  );
  if (!message) return null;
  if (content) message.content = String(content);
  message.deliveryStatus = "accepted";
  message.errorCode = "";
  message.errorMessage = "";
  message.localOutbox = false;
  message.queuePosition = 0;
  clearRenderCache(state.renderCache);
  return message;
}

function scheduleUserMessage(state, requestId, position) {
  const normalizedRequestId = String(requestId ?? "");
  if (!normalizedRequestId) return null;
  const message = state.messages.find(
    (item) => item.kind === "user" && item.requestId === normalizedRequestId,
  );
  if (!message) return null;
  message.deliveryStatus = "scheduled";
  message.queuePosition = Math.max(1, Math.floor(Number(position) || 1));
  message.errorCode = "";
  message.errorMessage = "";
  message.localOutbox = false;
  clearRenderCache(state.renderCache);
  return message;
}

export function failUserMessage(state, requestId, error = {}) {
  const normalizedRequestId = String(requestId ?? "");
  if (!normalizedRequestId) return null;
  const message = state.messages.find(
    (item) => item.kind === "user"
      && item.requestId === normalizedRequestId
      && ["queued", "uncertain"].includes(item.deliveryStatus),
  );
  if (!message) return null;
  message.deliveryStatus = "failed";
  message.errorCode = String(error.code ?? "send_failed");
  message.errorMessage = String(error.message ?? "发送失败，请重试。");
  message.localOutbox = true;
  clearRenderCache(state.renderCache);
  return message;
}

export function failQueuedUserMessages(state, error = {}) {
  let failed = 0;
  for (const message of state.messages) {
    if (message.kind !== "user" || message.deliveryStatus !== "queued") continue;
    message.deliveryStatus = "failed";
    message.errorCode = String(error.code ?? "bridge_disconnected");
    message.errorMessage = String(error.message ?? "Bridge 已断开，请重试。");
    message.localOutbox = true;
    failed += 1;
  }
  if (failed) clearRenderCache(state.renderCache);
  return failed;
}

function appendAcceptedUserMessage(state, content, requestId = "", extra = {}) {
  const normalizedContent = String(content ?? "");
  const uncertain = state.messages.find(
    (item) => item.kind === "user"
      && item.localOutbox
      && item.deliveryStatus === "uncertain"
      && item.content === normalizedContent,
  );
  if (uncertain) {
    uncertain.requestId = String(requestId || uncertain.requestId || "");
    uncertain.deliveryStatus = "accepted";
    uncertain.errorCode = "";
    uncertain.errorMessage = "";
    uncertain.localOutbox = false;
    Object.assign(uncertain, extra);
    clearRenderCache(state.renderCache);
    return uncertain;
  }
  const message = {
    kind: "user",
    id: nextMessageId(state, "user"),
    requestId: String(requestId ?? ""),
    content: normalizedContent,
    deliveryStatus: "accepted",
    attempt: 1,
    errorCode: "",
    errorMessage: "",
    localOutbox: false,
    ...extra,
  };
  state.messages.push(message);
  return message;
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
    enabled ? "思考文本显示已开启。" : "思考文本显示已关闭。",
    enabled ? "warning" : "info",
    { dismissWelcome: true },
  );
}

export function pushSystemMessage(state, title, content, level, options = {}) {
  if (!content) return;
  if (options.dismissWelcome === true) dismissWelcome(state);
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
      state.inspector.focused = false;
    }
  }
}

export function createUiSnapshot(state) {
  return {
    folds: state.folds,
    foldCursor: state.foldCursor,
    scrollOffset: state.scrollOffset,
    outbox: serializeUserOutbox(state.messages),
    activeTaskSubmission: sanitizeActiveTaskSubmission(state.activeTaskSubmission),
    inspector: sanitizeInspectorPresentation(state.inspector),
    agents: sanitizeAgentControlPresentation(state.agents),
    composer: {
      text: truncateInputText(state.input),
      cursor: getInputCursor(state),
      intent: state.composerIntent === "task" && String(state.input ?? "").trim()
        ? "task"
        : "chat",
      preferredColumn: Number.isFinite(state.inputPreferredColumn)
        ? Math.max(0, Number(state.inputPreferredColumn))
        : null,
    },
  };
}

export function applyUiSnapshot(state, snapshot) {
  const safeSnapshot = snapshot && typeof snapshot === "object" ? snapshot : {};
  state.folds = sanitizeFolds(safeSnapshot.folds);
  state.foldCursor = Number.isFinite(Number(safeSnapshot.foldCursor)) ? Math.max(0, Number(safeSnapshot.foldCursor)) : 0;
  state.scrollOffset = Number.isFinite(Number(safeSnapshot.scrollOffset)) ? Math.max(0, Number(safeSnapshot.scrollOffset)) : 0;
  state.followTail = state.scrollOffset === 0;
  state.unreadOutputCount = 0;
  state.unreadOutputKeys = {};
  restoreUserOutbox(state, safeSnapshot.outbox);
  state.activeTaskSubmission = sanitizeActiveTaskSubmission(safeSnapshot.activeTaskSubmission);
  const inspector = sanitizeInspectorPresentation(safeSnapshot.inspector);
  state.inspector.open = inspector.open;
  state.inspector.focused = false;
  state.inspector.selectedTab = inspector.selectedTab;
  state.inspector.selectionByTab = inspector.selectionByTab;
  state.inspector.expandedByTab = inspector.expandedByTab;
  state.inspector.scrollByTab = inspector.scrollByTab;
  resetInspectorSnapshot(state.inspector);
  const agents = sanitizeAgentControlPresentation(safeSnapshot.agents);
  state.agents.open = agents.open;
  state.agents.selectedTab = agents.selectedTab;
  state.agents.selectedByTab = agents.selectedByTab;
  state.agents.detailId = agents.detailId;
  state.agents.scrollByTab = agents.scrollByTab;
  state.route = agents.open
    ? { name: "agents", originAnchor: null }
    : { name: "conversation", originAnchor: null };
  resetAgentControlSnapshot(state.agents);
  const composer = safeSnapshot.composer && typeof safeSnapshot.composer === "object"
    ? safeSnapshot.composer
    : {};
  const text = truncateInputText(typeof composer.text === "string" ? composer.text : "");
  setInputText(state, text, Number(composer.cursor));
  state.composerIntent = composer.intent === "task" && text.trim() ? "task" : "chat";
  state.inputPreferredColumn = composer.preferredColumn !== null
    && composer.preferredColumn !== undefined
    && Number.isFinite(Number(composer.preferredColumn))
    ? Math.max(0, Number(composer.preferredColumn))
    : null;
}

function sanitizeInspectorPresentation(value) {
  const safe = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  return {
    open: safe.open === true,
    selectedTab: RUNTIME_INSPECTOR_TABS.includes(safe.selectedTab) ? safe.selectedTab : "plan",
    selectionByTab: sanitizeInspectorNumberMap(safe.selectionByTab),
    expandedByTab: sanitizeInspectorExpandedMap(safe.expandedByTab),
    scrollByTab: sanitizeInspectorNumberMap(safe.scrollByTab),
  };
}

function sanitizeAgentControlPresentation(value) {
  const safe = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const selectedByTab = {};
  const rawSelected = safe.selectedByTab && typeof safe.selectedByTab === "object"
    ? safe.selectedByTab
    : {};
  for (const tab of AGENT_CONTROL_TABS) {
    if (typeof rawSelected[tab] === "string") {
      selectedByTab[tab] = rawSelected[tab].slice(0, 500);
    }
  }
  return {
    open: safe.open === true,
    selectedTab: AGENT_CONTROL_TABS.includes(safe.selectedTab) ? safe.selectedTab : "agents",
    selectedByTab,
    detailId: typeof safe.detailId === "string" ? safe.detailId.slice(0, 500) : "",
    scrollByTab: sanitizeAgentControlNumberMap(safe.scrollByTab),
  };
}

function sanitizeAgentControlNumberMap(value) {
  const safe = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const result = {};
  for (const tab of AGENT_CONTROL_TABS) {
    const parsed = Number(safe[tab]);
    if (Number.isInteger(parsed) && parsed >= 0 && parsed <= 10_000) result[tab] = parsed;
  }
  return result;
}

function sanitizeInspectorNumberMap(value) {
  const safe = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const result = {};
  for (const tab of RUNTIME_INSPECTOR_TABS) {
    const parsed = Number(safe[tab]);
    if (Number.isInteger(parsed) && parsed >= 0 && parsed <= 10_000) result[tab] = parsed;
  }
  return result;
}

function sanitizeInspectorExpandedMap(value) {
  const safe = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const result = {};
  for (const tab of RUNTIME_INSPECTOR_TABS) {
    const rawEntries = safe[tab] && typeof safe[tab] === "object" && !Array.isArray(safe[tab])
      ? Object.entries(safe[tab])
      : [];
    const kept = {};
    for (const [index, expanded] of rawEntries.slice(0, 100)) {
      if (!/^\d{1,5}$/.test(index) || expanded !== true) continue;
      kept[index] = true;
    }
    if (Object.keys(kept).length) result[tab] = kept;
  }
  return result;
}

function serializeUserOutbox(messages) {
  return messages
    .filter((message) => message.kind === "user"
      && message.localOutbox
      && ["queued", "failed", "uncertain"].includes(message.deliveryStatus))
    .slice(-MAX_OUTBOX_MESSAGES)
    .map((message) => ({
      requestId: String(message.requestId ?? ""),
      content: truncateInputText(message.content ?? ""),
      deliveryStatus: message.deliveryStatus,
      attempt: Math.max(1, Math.floor(Number(message.attempt) || 1)),
      errorCode: boundedText(message.errorCode, MAX_OUTBOX_ERROR_CHARS),
      errorMessage: boundedText(message.errorMessage, MAX_OUTBOX_ERROR_CHARS),
      submitType: message.submitType === "task_submit" ? "task_submit" : "submit",
      intent: message.intent === "task" ? "task" : "chat",
      taskDraft: message.intent === "task" ? sanitizeTaskDraft(message.taskDraft) : null,
    }));
}

function restoreUserOutbox(state, rawOutbox) {
  state.messages = state.messages.filter((message) => !message.localOutbox);
  if (!Array.isArray(rawOutbox)) return;

  const entries = rawOutbox
    .map(sanitizeOutboxEntry)
    .filter(Boolean)
    .slice(-MAX_OUTBOX_MESSAGES);
  for (const entry of entries) {
    const deliveryStatus = entry.deliveryStatus === "queued" ? "uncertain" : entry.deliveryStatus;
    state.messages.push({
      kind: "user",
      id: nextMessageId(state, "user"),
      requestId: entry.requestId,
      content: entry.content,
      deliveryStatus,
      attempt: entry.attempt,
      errorCode: entry.errorCode,
      errorMessage: deliveryStatus === "uncertain"
        ? "上次发送未获得 Bridge 确认。"
        : entry.errorMessage,
      localOutbox: true,
      submitType: entry.submitType,
      intent: entry.intent,
      taskDraft: entry.taskDraft,
    });
    const submitId = entry.requestId.match(/^submit-(\d+)$/)?.[1];
    if (submitId) {
      state.nextSubmitId = Math.max(state.nextSubmitId, Number(submitId) + 1);
    }
  }
  if (entries.length) clearRenderCache(state.renderCache);
}

function sanitizeOutboxEntry(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const requestId = boundedText(value.requestId, MAX_OUTBOX_ERROR_CHARS).trim();
  const content = truncateInputText(typeof value.content === "string" ? value.content : "");
  const deliveryStatus = String(value.deliveryStatus ?? "");
  if (!requestId || !content || !["queued", "failed", "uncertain"].includes(deliveryStatus)) {
    return null;
  }
  return {
    requestId,
    content,
    deliveryStatus,
    attempt: Math.max(1, Math.floor(Number(value.attempt) || 1)),
    errorCode: boundedText(value.errorCode, MAX_OUTBOX_ERROR_CHARS),
    errorMessage: boundedText(value.errorMessage, MAX_OUTBOX_ERROR_CHARS),
    submitType: value.submitType === "task_submit" ? "task_submit" : "submit",
    intent: value.submitType === "task_submit" || value.intent === "task" ? "task" : "chat",
    taskDraft: value.submitType === "task_submit" || value.intent === "task"
      ? sanitizeTaskDraft(value.taskDraft)
      : null,
  };
}

function sanitizeTaskDraft(value) {
  const source = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const parallelMode = ["exclusive", "cooperative", "competitive", "exploratory"]
    .includes(source.parallel_mode) ? source.parallel_mode : "exclusive";
  const riskLevel = ["low", "medium", "high", "critical"]
    .includes(source.risk_level) ? source.risk_level : "medium";
  return {
    mission_id: boundedText(source.mission_id, 200).trim(),
    title: boundedText(source.title, 200).trim(),
    acceptance_criteria: boundedTextList(source.acceptance_criteria, 20, 500),
    blocked_by: boundedTextList(source.blocked_by, 50, 128),
    parallel_mode: parallelMode,
    risk_level: riskLevel,
  };
}

function sanitizeActiveTaskSubmission(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const requestId = typeof value.requestId === "string"
    ? boundedText(value.requestId, 200).trim()
    : "";
  const taskId = typeof value.taskId === "string"
    ? boundedText(value.taskId, 200).trim()
    : "";
  const missionId = typeof value.missionId === "string"
    ? boundedText(value.missionId, 200).trim()
    : "";
  const state = ["creating", "running", "completed", "blocked"].includes(value.state)
    ? value.state
    : "";
  if (!requestId || !taskId || !state) return null;
  return { requestId, taskId, missionId, state };
}

function boundedTextList(value, maxItems, maxChars) {
  if (!Array.isArray(value)) return [];
  return value.slice(0, maxItems).map((item) => boundedText(item, maxChars).trim()).filter(Boolean);
}

function boundedText(value, maxChars) {
  return Array.from(String(value ?? "")).slice(0, maxChars).join("");
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
    if (message.kind === "subagent_activity") {
      const key = `subagent:${message.id}`;
      entries.push({
        key,
        label: `子智能体 ${message.agentName || message.taskId || "activity"}`,
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
    `Bridge heartbeat: ${state.bridgeHeartbeat?.status || "starting"}`,
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
  state.inspector.focused = false;
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
    if (command === "detail" && !key && !detailId) {
      detailId = String(rawValue ?? "").trim();
    } else if (key === "limit") {
      limit = parseTaskPanelLimit(value, limit);
    } else if (!key && /^\d+$/.test(value)) {
      limit = parseTaskPanelLimit(value, limit);
    } else if ((key === "source" || !key) && sources.has(value)) {
      source = value;
    } else if ((key === "status" || !key) && statuses.has(value)) {
      status = value;
    } else if (key === "detail" || key === "detail_id") {
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
  if (nextFocused) state.inspector.focused = false;
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
