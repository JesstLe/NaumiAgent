import test from "node:test";
import assert from "node:assert/strict";
import { stripAnsi } from "../src/ansi.js";
import { INPUT_KEYS } from "../src/input-buffer.js";
import { renderScreen } from "../src/render.js";
import {
  DEFAULT_SLASH_COMMAND_CANDIDATES,
  createInitialState,
  createUiSnapshot,
  applyUiSnapshot,
  extractTaskPanelItems,
  failQueuedUserMessages,
  getFoldEntries,
  handleAgentControlKey,
  handleRuntimeInspectorKey,
  handleSubmitText,
  hasTaskPanelFocus,
  getSlashCommandCompletions,
  pushSystemMessage,
  reduceServerEvent,
  requestRunCancel,
  selectTaskPanelOffset,
  setTaskPanelFocus,
  retryUserMessage,
  submitTaskMessage,
  submitUserMessage,
  toggleComposerIntent,
  toggleAgentControlCenter,
  toggleRuntimeInspector,
} from "../src/state.js";

test("welcome becomes ready without creating a timeline message", () => {
  const state = createInitialState();
  assert.deepEqual(state.welcome, { phase: "booting", dismissed: false });

  reduceServerEvent(state, {
    type: "ready",
    payload: {
      version: "0.1.214",
      workspace_root: "/tmp/project",
      model: "openai/gpt-5.4",
      mode: "default",
      permission_mode: "moderate",
    },
  });

  assert.deepEqual(state.welcome, { phase: "ready_empty", dismissed: false });
  assert.equal(state.messages.length, 0);
});

test("working animation frame resets at every run lifecycle boundary", () => {
  const state = createInitialState();
  assert.equal(state.workingAnimationFrame, 0);

  state.workingAnimationFrame = 3;
  reduceServerEvent(state, {
    type: "run/started",
    request_id: "run-animation-1",
    payload: { task: "验证动态图" },
  });
  assert.equal(state.running, true);
  assert.equal(state.workingAnimationFrame, 0);

  state.workingAnimationFrame = 2;
  reduceServerEvent(state, {
    type: "run/completed",
    request_id: "run-animation-1",
    payload: { status: "completed" },
  });
  assert.equal(state.running, false);
  assert.equal(state.workingAnimationFrame, 0);

  reduceServerEvent(state, {
    type: "run/started",
    request_id: "run-animation-2",
    payload: { task: "取消动态图" },
  });
  state.workingAnimationFrame = 1;
  reduceServerEvent(state, {
    type: "run/cancelled",
    request_id: "run-animation-2",
    payload: { status: "cancelled" },
  });
  assert.equal(state.workingAnimationFrame, 0);

  state.workingAnimationFrame = 3;
  reduceServerEvent(state, {
    type: "session/replayed",
    payload: { session_id: "session-animation", title: "动画会话" },
  });
  assert.equal(state.workingAnimationFrame, 0);
  assert.equal("workingAnimationFrame" in createUiSnapshot(state), false);

  reduceServerEvent(state, {
    type: "run/started",
    request_id: "run-animation-error",
    payload: { task: "错误终态" },
  });
  state.workingAnimationFrame = 2;
  reduceServerEvent(state, {
    type: "error",
    request_id: "run-animation-error",
    payload: { code: "model_failed", message: "模型调用失败" },
  });
  assert.equal(state.running, false);
  assert.equal(state.workingAnimationFrame, 0);
});

test("chat and task submissions dismiss welcome before transport completion", () => {
  for (const intent of ["chat", "task"]) {
    const state = createInitialState();
    const sent = [];
    state.welcome.phase = "ready_empty";
    const send = (type, payload) => sent.push({ type, payload });

    if (intent === "chat") submitUserMessage(state, "你好", send);
    else submitTaskMessage(state, "实现欢迎页", send);

    assert.deepEqual(state.welcome, { phase: "dismissed", dismissed: true });
    assert.equal(state.messages[0].deliveryStatus, "queued");
    assert.equal(sent[0].type, intent === "chat" ? "submit" : "task_submit");
  }
});

test("backend user events replay and errors dismiss welcome idempotently", () => {
  const cases = [
    { type: "user/message", payload: { content: "远端消息" } },
    { type: "task/created", payload: { task: { id: "1" }, mission: {}, issue: {} } },
    { type: "session/replayed", payload: { session_id: "old", title: "旧会话" } },
    { type: "error", payload: { code: "bridge_failed", message: "Bridge 失败" } },
  ];
  for (const record of cases) {
    const state = createInitialState();
    reduceServerEvent(state, record);
    assert.deepEqual(state.welcome, { phase: "dismissed", dismissed: true });
  }
});

test("clear mode status drafts and snapshots do not mutate welcome lifecycle", () => {
  const state = createInitialState();
  state.welcome.phase = "ready_empty";
  reduceServerEvent(state, {
    type: "runtime/status",
    payload: { model: "openai/gpt-5.4" },
  });
  reduceServerEvent(state, {
    type: "mode/changed",
    payload: { mode: "plan", status: { mode: "plan", permission_mode: "strict" } },
  });
  assert.deepEqual(state.welcome, { phase: "ready_empty", dismissed: false });

  submitUserMessage(state, "第一条", () => {});
  handleSubmitText(state, "/clear", () => {});
  assert.deepEqual(state.welcome, { phase: "dismissed", dismissed: true });
  assert.equal(Object.hasOwn(createUiSnapshot(state), "welcome"), false);

  applyUiSnapshot(state, { welcome: { phase: "ready_empty", dismissed: false } });
  assert.deepEqual(state.welcome, { phase: "dismissed", dismissed: true });
});

test("only explicit infrastructure notices dismiss welcome", () => {
  const state = createInitialState();
  state.welcome.phase = "ready_empty";

  pushSystemMessage(state, "mode", "已切换到 plan 模式。", "warning");
  assert.deepEqual(state.welcome, { phase: "ready_empty", dismissed: false });

  pushSystemMessage(
    state,
    "bridge protocol",
    "Bridge 协议损坏。",
    "error",
    { dismissWelcome: true },
  );
  assert.deepEqual(state.welcome, { phase: "dismissed", dismissed: true });
});

function agentSnapshot(revision = 1) {
  return {
    schema_version: 1,
    session_id: "session-agents",
    revision,
    generated_at: "2026-07-13T00:00:00+00:00",
    summary: {
      total_agents: 1,
      active_agents: 1,
      attention_agents: 0,
      stoppable_executions: 1,
      pending_messages: 1,
    },
    agents: [{
      name: "coder",
      description: "编程 Agent",
      kind: "preset",
      state: "running",
      task_count: 1,
      model_tier: "capable",
      capabilities: ["代码"],
      tools: ["file_read"],
      permission_level: "moderate",
      age_ms: 500,
      heartbeat_age_ms: 100,
    }],
    executions: [{
      task_id: "task-1",
      session_id: "session-agents",
      agent_name: "coder",
      description: "实现控制中心",
      status: "running",
      phase: "running_tool",
      started_at: 1,
      finished_at: null,
      elapsed_ms: 1000,
      heartbeat_age_ms: 100,
      current_tool: "file_read",
      recent_tools: ["file_read"],
      total_tokens: 42,
      total_cost_usd: 0.01,
      turns: 2,
      error: "",
      stop_supported: true,
      stop_requested: false,
    }],
    team_messages: [{
      sender: "coder",
      recipient: "reviewer",
      topic: "review",
      priority: "high",
      timestamp: 1,
      content: "请检查实现",
    }],
    blackboard: [{
      key: "team/review",
      author: "coder",
      version: 1,
      timestamp: 1,
      value_summary: "ready",
    }],
    warnings: [],
  };
}

test("agent control route preserves conversation presentation and applies revisioned state", () => {
  const state = createInitialState();
  const sent = [];
  const send = (type, payload) => sent.push({ type, payload });
  state.currentSessionId = "session-agents";
  state.input = "尚未发送的草稿";
  state.scrollOffset = 17;
  state.inspector.open = true;
  state.inspector.selectedTab = "changes";
  const messageCount = state.messages.length;

  toggleAgentControlCenter(state, send, true);
  assert.equal(state.route.name, "agents");
  assert.equal(state.input, "尚未发送的草稿");
  assert.equal(state.scrollOffset, 17);
  assert.equal(state.inspector.selectedTab, "changes");
  assert.equal(state.messages.length, messageCount);
  assert.deepEqual(sent[0], {
    type: "agents/request",
    payload: { open: true, known_revision: 0, session_id: "session-agents" },
  });

  reduceServerEvent(state, { type: "agents/snapshot", payload: agentSnapshot(1) });
  assert.equal(state.agents.revision, 1);
  assert.equal(state.agents.snapshot.executions[0].task_id, "task-1");
  const update = agentSnapshot(2);
  const effects = reduceServerEvent(state, {
    type: "agents/update",
    payload: {
      schema_version: 1,
      session_id: "session-agents",
      revision: 2,
      generated_at: update.generated_at,
      changed_sections: { warnings: ["延迟"] },
    },
  });
  assert.deepEqual(effects, []);
  assert.deepEqual(state.agents.snapshot.warnings, ["延迟"]);

  const gapEffects = reduceServerEvent(state, {
    type: "agents/update",
    payload: {
      schema_version: 1,
      session_id: "session-agents",
      revision: 4,
      generated_at: update.generated_at,
      changed_sections: { warnings: [] },
    },
  });
  assert.deepEqual(gapEffects, [{
    type: "refresh_agents",
    knownRevision: 2,
    sessionId: "session-agents",
  }]);

  toggleAgentControlCenter(state, send, false);
  assert.equal(state.route.name, "conversation");
  assert.equal(state.input, "尚未发送的草稿");
  assert.equal(state.scrollOffset, 17);
  assert.equal(state.inspector.open, true);
});

test("agent control keyboard uses stable tabs and confirms one authoritative stop", () => {
  const state = createInitialState();
  const sent = [];
  const send = (type, payload) => sent.push({ type, payload });
  state.currentSessionId = "session-agents";
  toggleAgentControlCenter(state, send, true);
  reduceServerEvent(state, { type: "agents/snapshot", payload: agentSnapshot(1) });

  assert.equal(state.agents.selectedTab, "agents");
  assert.equal(handleAgentControlKey(state, INPUT_KEYS.tab, send), true);
  assert.equal(state.agents.selectedTab, "executions");
  assert.equal(handleAgentControlKey(state, "x", send), true);
  assert.equal(state.agents.stopConfirmationTaskId, "task-1");
  assert.equal(handleAgentControlKey(state, "n", send), true);
  assert.equal(state.agents.stopConfirmationTaskId, "");
  assert.equal(sent.filter((item) => item.type === "agents/stop").length, 0);

  handleAgentControlKey(state, "x", send);
  handleAgentControlKey(state, "y", send);
  handleAgentControlKey(state, "y", send);
  const stops = sent.filter((item) => item.type === "agents/stop");
  assert.equal(stops.length, 1);
  assert.deepEqual(stops[0].payload, {
    session_id: "session-agents",
    task_id: "task-1",
    reason: "用户在 Agent 控制中心确认停止。",
  });
  assert.equal(state.agents.snapshot.executions[0].status, "running");
  assert.equal(state.agents.actionPendingTaskId, "task-1");

  reduceServerEvent(state, {
    type: "agents/action",
    payload: {
      task_id: "task-1",
      accepted: true,
      code: "accepted",
      message: "已请求停止。",
    },
  });
  assert.equal(state.agents.actionPendingTaskId, "task-1");
  assert.equal(state.agents.actionMessage, "已请求停止。");

  const stopping = agentSnapshot(2);
  stopping.executions[0].status = "stopping";
  stopping.executions[0].phase = "stopping";
  stopping.executions[0].stop_supported = false;
  stopping.executions[0].stop_requested = true;
  reduceServerEvent(state, { type: "agents/snapshot", payload: stopping });
  assert.equal(state.agents.actionPendingTaskId, "task-1");

  const terminal = agentSnapshot(3);
  terminal.executions[0].status = "cancelled";
  terminal.executions[0].phase = "finished";
  terminal.executions[0].stop_supported = false;
  terminal.executions[0].stop_requested = true;
  reduceServerEvent(state, { type: "agents/snapshot", payload: terminal });
  assert.equal(state.agents.actionPendingTaskId, "");

  handleAgentControlKey(state, INPUT_KEYS.tab, send);
  assert.equal(state.agents.selectedTab, "team");
  handleAgentControlKey(state, INPUT_KEYS.shiftTab, send);
  assert.equal(state.agents.selectedTab, "executions");

  const persisted = createUiSnapshot(state);
  assert.equal(persisted.agents.open, true);
  assert.equal(persisted.agents.selectedTab, "executions");
  assert.equal("snapshot" in persisted.agents, false);
  assert.equal("revision" in persisted.agents, false);
});

test("assistant stream updates one active message", () => {
  const state = createInitialState();

  reduceServerEvent(state, { type: "ui/message", payload: { type: "assistant_stream", phase: "start" } });
  reduceServerEvent(state, { type: "ui/message", payload: { type: "assistant_stream", phase: "token", content: "你" } });
  reduceServerEvent(state, { type: "ui/message", payload: { type: "assistant_stream", phase: "token", content: "好" } });
  reduceServerEvent(state, { type: "ui/message", payload: { type: "assistant_stream", phase: "end" } });

  assert.equal(state.messages.length, 1);
  assert.equal(state.messages[0].kind, "assistant");
  assert.equal(state.messages[0].content, "你好");
  assert.equal(state.activeAssistant, null);
});

test("submit queues one local message and echo accepts it without duplication", () => {
  const state = createInitialState();
  const sent = [];
  const send = (type, payload, options = {}) => {
    sent.push({ type, payload, options });
    return options.id;
  };

  handleSubmitText(state, "修复测试", send);
  const pending = state.messages.at(-1);
  assert.equal(pending.kind, "user");
  assert.equal(pending.deliveryStatus, "queued");
  assert.equal(pending.attempt, 1);
  assert.equal(sent[0].options.id, pending.requestId);

  reduceServerEvent(state, {
    type: "user/message",
    request_id: pending.requestId,
    payload: { content: "修复测试" },
  });

  assert.equal(state.messages.filter((message) => message.kind === "user").length, 1);
  assert.equal(pending.deliveryStatus, "accepted");
  assert.equal(pending.localOutbox, false);
});

test("matching error fails only an unconfirmed user submission", () => {
  const state = createInitialState();
  const send = (_type, _payload, options = {}) => options.id;
  handleSubmitText(state, "第一条", send);
  const pending = state.messages.at(-1);

  reduceServerEvent(state, {
    type: "error",
    request_id: pending.requestId,
    payload: { code: "run_in_progress", message: "当前任务仍在执行。" },
  });
  assert.equal(pending.deliveryStatus, "failed");
  assert.equal(pending.errorCode, "run_in_progress");
  assert.match(pending.errorMessage, /当前任务仍在执行/);

  handleSubmitText(state, "第二条", send);
  const accepted = state.messages.at(-1);
  reduceServerEvent(state, {
    type: "user/message",
    request_id: accepted.requestId,
    payload: { content: "第二条" },
  });
  reduceServerEvent(state, {
    type: "error",
    request_id: accepted.requestId,
    payload: { code: "model_not_found", message: "模型不可用。" },
  });
  assert.equal(accepted.deliveryStatus, "accepted");
});

test("run start accepts a queued message when user echo is missing", () => {
  const state = createInitialState();
  const send = (_type, _payload, options = {}) => options.id;
  handleSubmitText(state, "继续执行", send);
  const pending = state.messages.at(-1);

  reduceServerEvent(state, {
    type: "run/started",
    request_id: pending.requestId,
    payload: { task: "继续执行" },
  });

  assert.equal(pending.deliveryStatus, "accepted");
});

test("synchronous sender failure leaves one retryable failed message", () => {
  const state = createInitialState();
  const send = () => {
    throw new Error("broken pipe");
  };

  handleSubmitText(state, "仍需保留", send);

  const users = state.messages.filter((message) => message.kind === "user");
  assert.equal(users.length, 1);
  assert.equal(users[0].deliveryStatus, "failed");
  assert.equal(users[0].errorCode, "transport_write_failed");
  assert.match(users[0].errorMessage, /无法写入本地 Bridge/);
});

test("transport failure terminates queued messages but preserves accepted ones", () => {
  const state = createInitialState();
  const send = (_type, _payload, options = {}) => options.id;
  handleSubmitText(state, "等待确认", send);
  handleSubmitText(state, "已经确认", send);
  const [queued, accepted] = state.messages.filter((message) => message.kind === "user");
  reduceServerEvent(state, {
    type: "user/message",
    request_id: accepted.requestId,
    payload: { content: accepted.content },
  });

  assert.equal(failQueuedUserMessages(state, {
    code: "bridge_disconnected",
    message: "Bridge 已断开。",
  }), 1);
  assert.equal(queued.deliveryStatus, "failed");
  assert.equal(accepted.deliveryStatus, "accepted");
});

test("unmatched backend user message still renders once as accepted", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "user/message",
    request_id: "external-client",
    payload: { content: "来自其他客户端" },
  });

  const user = state.messages.at(-1);
  assert.equal(user.kind, "user");
  assert.equal(user.requestId, "external-client");
  assert.equal(user.deliveryStatus, "accepted");
});

test("retry reuses one failed bubble with a new request id", () => {
  const state = createInitialState();
  const sent = [];
  const send = (type, payload, options = {}) => {
    sent.push({ type, payload, id: options.id });
    return options.id;
  };
  handleSubmitText(state, "失败消息", send);
  const message = state.messages.at(-1);
  const oldRequestId = message.requestId;
  reduceServerEvent(state, {
    type: "error",
    request_id: oldRequestId,
    payload: { code: "run_in_progress", message: "当前任务仍在执行。" },
  });

  handleSubmitText(state, "/retry", send);

  assert.equal(state.messages.filter((item) => item.kind === "user").length, 1);
  assert.equal(message.deliveryStatus, "queued");
  assert.equal(message.attempt, 2);
  assert.notEqual(message.requestId, oldRequestId);
  assert.equal(sent.at(-1).payload.text, "失败消息");
});

test("retry can select a failed request and warns when none is eligible", () => {
  const state = createInitialState();
  const sent = [];
  const send = (type, payload, options = {}) => {
    sent.push({ type, payload, id: options.id });
    return options.id;
  };
  handleSubmitText(state, "/retry", send);
  assert.equal(sent.length, 0);
  assert.match(state.messages.at(-1).content, /没有可重试/);

  handleSubmitText(state, "A", send);
  handleSubmitText(state, "B", send);
  const [a, b] = state.messages.filter((item) => item.kind === "user");
  reduceServerEvent(state, {
    type: "error",
    request_id: a.requestId,
    payload: { code: "rejected", message: "A failed" },
  });
  reduceServerEvent(state, {
    type: "error",
    request_id: b.requestId,
    payload: { code: "rejected", message: "B failed" },
  });

  const bRequestId = b.requestId;
  handleSubmitText(state, `/retry ${a.requestId}`, send);
  assert.equal(a.deliveryStatus, "queued");
  assert.equal(b.deliveryStatus, "failed");
  assert.equal(b.requestId, bRequestId);
});

test("thinking content is hidden and removed after completion by default", () => {
  const state = createInitialState();

  reduceServerEvent(state, { type: "ui/message", payload: { type: "thinking", phase: "start" } });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "thinking", phase: "delta", content: "private reasoning should not render" },
  });

  assert.equal(state.messages.length, 1);
  assert.equal(state.messages[0].kind, "thinking");
  assert.equal(state.messages[0].content, "");
  assert.equal(state.messages[0].chars, "private reasoning should not render".length);

  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "thinking", phase: "end", content: "full private reasoning" },
  });

  assert.equal(state.activeThinking, null);
  assert.equal(state.messages.some((message) => message.kind === "thinking"), false);
});

test("thinking content can be shown when reasoning display is enabled", () => {
  const state = createInitialState();
  reduceServerEvent(state, {
    type: "runtime/status",
    payload: { ui: { show_reasoning: true } },
  });

  reduceServerEvent(state, { type: "ui/message", payload: { type: "thinking", phase: "start" } });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "thinking", phase: "delta", content: "visible reasoning" },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "thinking", phase: "end" },
  });

  assert.equal(state.showReasoning, true);
  assert.equal(state.messages.length, 1);
  assert.equal(state.messages[0].kind, "thinking");
  assert.equal(state.messages[0].content, "visible reasoning");
  assert.equal(state.messages[0].done, true);
});

test("runtime status updates current session id and reasoning flag", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "runtime/status",
    payload: {
      session_id: "sess-2026-06-03",
      ui: { show_reasoning: true },
      mode: "default",
      reasoning_effort: {
        model: "gpt-5",
        effective: "high",
        source: "runtime",
        supported: ["low", "high"],
        default: "low",
        warning: null,
      },
    },
  });

  assert.equal(state.currentSessionId, "sess-2026-06-03");
  assert.equal(state.showReasoning, true);
  assert.equal(state.status.reasoning_effort.effective, "high");
});

test("replayed assistant token messages stay independent outside a running turn", () => {
  const state = createInitialState();

  reduceServerEvent(state, { type: "session/replayed", payload: { session_id: "s1", title: "旧会话", clear: true } });
  reduceServerEvent(state, { type: "ui/message", payload: { type: "assistant_stream", phase: "token", content: "第一条回答" } });
  reduceServerEvent(state, { type: "ui/message", payload: { type: "tool_use", tool_call_id: "call-1", tool_name: "file_read" } });
  reduceServerEvent(state, { type: "ui/message", payload: { type: "assistant_stream", phase: "token", content: "第二条回答" } });

  const assistants = state.messages.filter((message) => message.kind === "assistant");
  assert.equal(assistants.length, 2);
  assert.equal(assistants[0].content, "第一条回答");
  assert.equal(assistants[1].content, "第二条回答");
  assert.equal(state.activeAssistant, null);
});

test("session replay clears stale run, permission, todo, and perf footer state", () => {
  const state = createInitialState();

  reduceServerEvent(state, { type: "run/started", payload: {} });
  reduceServerEvent(state, {
    type: "permission/request",
    request_id: "perm-1",
    payload: { tool_name: "bash_run", reason: "需要确认。" },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "todo_status",
      total_count: 2,
      completed_count: 0,
      open_count: 2,
      items: [{ id: 1, subject: "旧任务", status: "in_progress" }],
    },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "runtime_status", phase: "perf_phase", label: "模型首包", duration_ms: 1800 },
  });

  reduceServerEvent(state, { type: "session/replayed", payload: { session_id: "s2", title: "恢复后", clear: true } });

  assert.equal(state.running, false);
  assert.equal(state.permission, null);
  assert.equal(state.todo, null);
  assert.equal(state.activeToolPrepare, null);
  assert.equal(state.activeRuntimePhase, "");

  const plain = renderScreen(state, 90, 12, { cwd: "/tmp", home: "/Users/lv" }).map(stripAnsi).join("\n");
  assert(!plain.includes("permission: bash_run"));
  assert(!plain.includes("todo:"));
  assert(!plain.includes("运行中"));
});

test("generic errors do not override the active run lifecycle", () => {
  const state = createInitialState();

  reduceServerEvent(state, { type: "run/started", payload: {} });
  reduceServerEvent(state, {
    type: "error",
    payload: { code: "run_in_progress", message: "当前任务仍在执行。" },
  });

  assert.equal(state.running, true);
  reduceServerEvent(state, {
    type: "run/completed",
    payload: { status: "completed", response: "完成", error: "" },
  });
  assert.equal(state.running, false);
});

test("permission request creates an updatable history card", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "permission/request",
    request_id: "perm-1",
    payload: {
      tool_name: "bash_run",
      reason: "需要启动本地服务。",
      choices: ["allow_once", "deny", "grant_session"],
    },
  });

  const card = state.messages.at(-1);
  assert.equal(state.permission.requestId, "perm-1");
  assert.equal(card.kind, "permission");
  assert.equal(card.requestId, "perm-1");
  assert.equal(card.message.status, "needs_confirmation");
  assert.equal(card.message.requires_confirmation, true);

  reduceServerEvent(state, {
    type: "permission/resolved",
    payload: { request_id: "perm-1", choice: "allow_once" },
  });

  assert.equal(state.permission, null);
  assert.equal(state.messages.at(-1), card);
  assert.equal(card.message.status, "allowed");
  assert.equal(card.message.requires_confirmation, false);
});

test("permission grant changes are visible without disturbing the composer", () => {
  const state = createInitialState();
  state.input = "保留草稿";

  reduceServerEvent(state, {
    type: "permission/grants_changed",
    payload: { revoked: 2, grants: [] },
  });

  assert.equal(state.input, "保留草稿");
  const notice = state.messages.at(-1);
  assert.equal(notice.kind, "system");
  assert.equal(notice.title, "permissions");
  assert(String(notice.content).includes("已撤销 2 项"));
});

test("mode changed emits one visible notice only when mode changes", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "mode/changed",
    payload: { mode: "bypass", status: { mode: "bypass" } },
  });

  assert.equal(state.mode, "bypass");
  const notice = state.messages.at(-1);
  assert.equal(notice.kind, "system");
  assert.equal(notice.title, "mode");
  assert.equal(notice.level, "warning");
  assert(String(notice.content).includes("已切换到 bypass"));

  reduceServerEvent(state, {
    type: "mode/changed",
    payload: { mode: "bypass", status: { mode: "bypass" } },
  });

  assert.equal(state.messages.filter((message) => message.kind === "system" && message.title === "mode").length, 1);

  reduceServerEvent(state, {
    type: "mode/changed",
    payload: { mode: "plan", status: { mode: "plan" } },
  });

  const secondNotice = state.messages.at(-1);
  assert.equal(state.mode, "plan");
  assert.equal(secondNotice.title, "mode");
  assert.equal(secondNotice.level, "info");
  assert(String(secondNotice.content).includes("只读规划模式"));
});

test("tool results prefer stable call id before falling back to tool name", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "tool_use", tool_call_id: "call-a", tool_name: "file_write", file_path: "a.py" },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "tool_use", tool_call_id: "call-b", tool_name: "file_write", file_path: "b.py" },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "tool_result",
      tool_call_id: "call-a",
      tool_name: "file_write",
      status: "success",
      duration_ms: 7,
      content_preview: "done a",
    },
  });

  assert.equal(state.tools[0].status, "success");
  assert.equal(state.tools[0].output, "done a");
  assert.equal(state.tools[1].status, "running");
});

test("tool result stores preview highlight metadata", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "tool_use", tool_call_id: "call-code", tool_name: "file_write", file_path: "demo.py" },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "tool_result",
      tool_call_id: "call-code",
      tool_name: "file_write",
      status: "success",
      content_preview: "print('ok')",
      preview_format: "code",
      preview_language: "python",
    },
  });

  assert.equal(state.tools[0].outputFormat, "code");
  assert.equal(state.tools[0].outputLanguage, "python");
});

test("tool prepare creates a durable activity message before tool cards", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "tool_prepare",
      phase: "snapshot",
      tool_name: "file_write",
      path: "showcase/index.html",
      content_lines: 88,
      argument_chars: 4096,
    },
  });

  assert.equal(state.activeToolPrepare.kind, "activity");
  assert.equal(state.messages.at(-1).kind, "activity");
  assert.equal(state.messages.at(-1).status, "running");
  assert.deepEqual(state.messages.at(-1).details, [
    "路径: showcase/index.html",
    "内容: 88 行",
    "参数: 4.1K 字符",
  ]);
  assert.equal(state.messages.at(-1).phase, "snapshot");
  assert.equal(state.messages.at(-1).metrics.argumentChars, 4096);

  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "tool_use", tool_call_id: "call-file", tool_name: "file_write", file_path: "showcase/index.html" },
  });

  assert.equal(state.activeToolPrepare, null);
  assert.equal(state.messages[0].status, "done");
  assert(state.messages[0].details.includes("已交给工具执行"));
  assert.equal(state.messages.at(-1).kind, "tool");
  assert.equal(state.messages.at(-1).prepareTitle, "准备 file_write");
  assert(state.messages.at(-1).prepareDetails.includes("路径: showcase/index.html"));
});

test("tool prepare end stays attached until the following tool card consumes it", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "tool_prepare",
      phase: "start",
      tool_name: "file_write",
      tool_call_id: "call-file",
      path: "demo.html",
      argument_chars: 128,
    },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "tool_prepare",
      phase: "snapshot",
      tool_name: "file_write",
      tool_call_id: "call-file",
      path: "demo.html",
      argument_chars: 4096,
      content_lines: 88,
      elapsed_ms: 2400,
    },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "tool_prepare",
      phase: "snapshot",
      tool_name: "file_write",
      tool_call_id: "call-file",
      path: "demo.html",
      content_lines: 42,
    },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "tool_prepare",
      phase: "end",
      tool_name: "file_write",
      tool_call_id: "call-file",
      path: "demo.html",
      argument_chars: 4096,
      content_lines: 88,
      elapsed_ms: 2600,
    },
  });

  assert.equal(state.activeToolPrepare.status, "done");
  assert.equal(state.activeToolPrepare.toolCallId, "call-file");
  assert.equal(state.activeToolPrepare.phase, "end");
  assert.equal(state.activeToolPrepare.metrics.elapsedMs, 2600);

  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "tool_use", tool_call_id: "call-file", tool_name: "file_write", file_path: "demo.html" },
  });

  const tool = state.messages.at(-1);
  assert.equal(state.activeToolPrepare, null);
  assert.equal(tool.kind, "tool");
  assert.equal(tool.prepareTitle, "准备 file_write");
  assert.equal(tool.preparePhase, "end");
  assert.deepEqual(tool.prepareMetrics, {
    argumentChars: 4096,
    contentChars: 0,
    contentLines: 88,
    elapsedMs: 2600,
  });
  assert(tool.prepareDetails.includes("准备阶段已完成"));
  assert(tool.prepareDetails.includes("已交给工具执行"));
});

test("tool prepare is not attached to a mismatched tool call id", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "tool_prepare",
      phase: "end",
      tool_name: "file_write",
      tool_call_id: "call-file",
      path: "demo.html",
      content_lines: 42,
    },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "tool_use",
      tool_call_id: "call-browser",
      tool_name: "browser_goto",
      url: "http://localhost:8765",
    },
  });

  const tool = state.messages.at(-1);
  assert.equal(state.activeToolPrepare, null);
  assert.equal(tool.kind, "tool");
  assert.equal(tool.name, "browser_goto");
  assert.equal(tool.prepareTitle, "");
  assert.deepEqual(tool.prepareDetails, []);
});

test("sequential tools keep separate prepare summaries", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "tool_prepare",
      phase: "snapshot",
      tool_name: "file_write",
      path: "a.html",
      content_lines: 20,
      argument_chars: 2048,
    },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "tool_prepare",
      phase: "end",
      tool_name: "file_write",
      path: "a.html",
      content_lines: 20,
      argument_chars: 2048,
    },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "tool_use", tool_call_id: "call-a", tool_name: "file_write", file_path: "a.html" },
  });

  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "tool_prepare",
      phase: "snapshot",
      tool_name: "browser_goto",
      url: "http://localhost:8765",
      argument_chars: 96,
    },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "tool_use", tool_call_id: "call-b", tool_name: "browser_goto", url: "http://localhost:8765" },
  });

  assert.equal(state.tools.length, 2);
  assert.equal(state.tools[0].name, "file_write");
  assert.equal(state.tools[0].prepareDetails.includes("路径: a.html"), true);
  assert.equal(state.tools[0].prepareDetails.some((detail) => detail.includes("URL:")), false);
  assert.equal(state.tools[1].name, "browser_goto");
  assert.equal(state.tools[1].prepareDetails.includes("URL: http://localhost:8765"), true);
  assert.equal(state.tools[1].prepareDetails.some((detail) => detail.includes("路径: a.html")), false);
});

test("runtime perf phase does not corrupt active tool prepare state", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "runtime_status",
      phase: "perf_phase",
      label: "模型首包",
      duration_ms: 2400,
    },
  });

  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "tool_use", tool_call_id: "call-1", tool_name: "bash_run", command: "pwd" },
  });

  assert.equal(state.activeRuntimePhase, "模型首包: 2400ms");
  assert.equal(state.activeToolPrepare, null);
  assert.equal(state.tools[0].name, "bash_run");
  assert.equal(state.tools[0].prepareTitle, "");
  assert.deepEqual(state.tools[0].prepareDetails, []);

  state.running = true;
  const plain = renderScreen(state, 90, 12, { cwd: "/tmp", home: "/Users/lv" }).map(stripAnsi).join("\n");
  assert(plain.includes("运行中... · 模型首包: 2400ms"));
});

test("todo footer state tracks open work and clears when complete", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "todo_status",
      total_count: 3,
      completed_count: 1,
      open_count: 2,
      items: [
        { id: 1, subject: "已完成", status: "completed" },
        { id: 2, subject: "正在写文件", status: "in_progress" },
        { id: 3, subject: "验证", status: "pending" },
      ],
    },
  });

  assert.equal(state.todo.completed, 1);
  assert.equal(state.todo.current.subject, "正在写文件");

  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "todo_status", total_count: 3, completed_count: 3, open_count: 0, items: [] },
  });

  assert.equal(state.todo, null);
});

test("todo prepare gives immediate sticky feedback before backend snapshot", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "tool_prepare",
      phase: "start",
      tool_name: "todo_write",
      argument_chars: 128,
    },
  });

  assert.equal(state.todo.completed, 0);
  assert.equal(state.todo.current.subject, "正在同步任务列表 (参数 128 字符)");

  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "todo_status",
      total_count: 2,
      completed_count: 1,
      open_count: 1,
      items: [{ id: 2, subject: "验证页面", status: "pending" }],
    },
  });

  assert.equal(state.todo.completed, 1);
  assert.equal(state.todo.current.subject, "验证页面");
});

test("todo prepare streams real todo progress from todo_write arguments", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "tool_prepare",
      phase: "snapshot",
      tool_name: "todo_write",
      todo_total: 3,
      todo_completed: 1,
      todo_open: 2,
      todo_items: [
        { id: "2", status: "in_progress", subject: "编写 CSS" },
        { id: "3", status: "pending", subject: "浏览器验证" },
      ],
    },
  });

  assert.equal(state.todo.total, 3);
  assert.equal(state.todo.completed, 1);
  assert.equal(state.todo.current.subject, "编写 CSS");
});

test("tool prepare snapshots update one live activity with progress metrics", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "tool_prepare",
      phase: "start",
      tool_name: "file_write",
      path: "showcase/index.html",
      argument_chars: 128,
      elapsed_ms: 40,
    },
  });
  const activity = state.activeToolPrepare;

  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "tool_prepare",
      phase: "snapshot",
      tool_name: "file_write",
      path: "showcase/index.html",
      argument_chars: 4096,
      content_chars: 12000,
      content_lines: 88,
      elapsed_ms: 2400,
    },
  });

  assert.equal(state.activeToolPrepare, activity);
  assert.equal(state.messages.filter((message) => message.kind === "activity").length, 1);
  assert.equal(activity.phase, "snapshot");
  assert.deepEqual(activity.metrics, {
    argumentChars: 4096,
    contentChars: 12000,
    contentLines: 88,
    elapsedMs: 2400,
  });
  assert(activity.details.includes("路径: showcase/index.html"));
  assert(activity.details.includes("内容: 88 行"));
  assert(activity.details.includes("内容: 12.0K 字符"));
  assert(activity.details.includes("参数: 4.1K 字符"));
  assert(activity.details.includes("已准备: 2.4s"));
});

test("todo prepare end still refreshes footer with final todo progress", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "tool_prepare",
      phase: "start",
      tool_name: "todo_write",
      argument_chars: 24,
    },
  });

  assert.equal(state.todo.current.subject, "正在同步任务列表 (参数 24 字符)");

  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "tool_prepare",
      phase: "end",
      tool_name: "todo_write",
      todo_total: 2,
      todo_completed: 1,
      todo_open: 1,
      todo_items: [{ id: "2", status: "pending", subject: "写 CSS" }],
    },
  });

  assert.equal(state.todo.total, 2);
  assert.equal(state.todo.completed, 1);
  assert.equal(state.todo.current.subject, "写 CSS");
});

test("slash commands route through protocol without adding chat noise", () => {
  const state = createInitialState();
  const sent = [];
  const send = (type, payload) => sent.push({ type, payload });

  handleSubmitText(state, "/mode bypass", send);
  handleSubmitText(state, "/load abc123", send);
  handleSubmitText(state, "/tasks 5", send);
  handleSubmitText(state, "/permissions 6", send);
  handleSubmitText(state, "/permissions revoke grant-1", send);
  handleSubmitText(state, "/permissions revoke all", send);
  handleSubmitText(state, "/doctor", send);
  handleSubmitText(state, "/reasoning on", send);
  handleSubmitText(state, "/effort high", send);
  state.messages.push({ kind: "assistant", content: "old" });
  state.folds["message:old:code:0"] = { expanded: true };
  handleSubmitText(state, "/clear", send);
  handleSubmitText(state, "/c", send);
  assert.deepEqual(state.messages, []);
  handleSubmitText(state, "你好", send);

  assert.deepEqual(sent, [
    { type: "set_mode", payload: { mode: "bypass" } },
    { type: "resume", payload: { session_id: "abc123" } },
    { type: "task_panel", payload: { limit: 5, source: "all", status: "all", pinned: false, refresh: false } },
    { type: "permissions_panel", payload: { limit: 6 } },
    { type: "permission_revoke", payload: { grant_id: "grant-1" } },
    { type: "permission_revoke", payload: { scope: "all" } },
    { type: "doctor", payload: {} },
    { type: "set_reasoning", payload: { enabled: true } },
    { type: "submit", payload: { text: "/effort high" } },
    { type: "submit", payload: { text: "/clear" } },
    { type: "submit", payload: { text: "/c" } },
    { type: "submit", payload: { text: "你好" } },
  ]);
  assert.equal(state.messages.length, 1);
  assert.equal(state.messages[0].kind, "user");
  assert.equal(state.messages[0].deliveryStatus, "queued");
  assert.deepEqual(state.folds, {});
});

test("task panel can be pinned, refreshed, and updated in place", () => {
  const state = createInitialState();
  const sent = [];
  const send = (type, payload) => sent.push({ type, payload });

  handleSubmitText(state, "/tasks pin 7", send);

  assert.equal(state.taskPanel.pinned, true);
  assert.equal(state.taskPanel.limit, 7);
  assert.deepEqual(sent, [{ type: "task_panel", payload: { limit: 7, source: "all", status: "all", pinned: true, refresh: false } }]);

  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "system_notice", title: "tasks", content: "任务面板\nTodo\n旧任务" },
  });
  const firstPanel = state.messages.at(-1);
  assert.equal(firstPanel.title, "tasks");

  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "system_notice", title: "tasks", content: "任务面板\nTodo\n新任务" },
  });

  assert.equal(state.messages.filter((message) => message.title === "tasks").length, 1);
  assert.equal(firstPanel.content, "任务面板\nTodo\n新任务");

  const actions = reduceServerEvent(state, {
    type: "runtime/status",
    payload: { tasks: { background_running: 1 } },
  });
  assert.deepEqual(actions, [{ type: "refresh_task_panel", limit: 7, source: "all", status: "all" }]);

  handleSubmitText(state, "/tasks off", send);
  assert.equal(state.taskPanel.pinned, false);
  assert.equal(state.taskPanel.messageId, "");
  assert.equal(state.messages.filter((message) => message.title === "tasks").length, 0);
  assert.equal(state.messages.at(-1).title, "任务面板");
  assert.equal(state.messages.at(-1).content, "已取消钉住。");
});

test("task panel command parses source and status filters", () => {
  const state = createInitialState();
  const sent = [];
  const send = (type, payload) => sent.push({ type, payload });

  handleSubmitText(state, "/tasks todo open 8", send);
  handleSubmitText(state, "/tasks pin source=background status=running limit=6", send);

  assert.deepEqual(sent, [
    { type: "task_panel", payload: { limit: 8, source: "todo", status: "open", pinned: false, refresh: false } },
    { type: "task_panel", payload: { limit: 6, source: "background", status: "running", pinned: true, refresh: false } },
  ]);
  assert.equal(state.taskPanel.source, "background");
  assert.equal(state.taskPanel.status, "running");

  const actions = reduceServerEvent(state, {
    type: "runtime/status",
    payload: { tasks: { background_running: 1 } },
  });

  assert.deepEqual(actions, [{ type: "refresh_task_panel", limit: 6, source: "background", status: "running" }]);
});

test("task panel history command requests acknowledged background history", () => {
  const state = createInitialState();
  const sent = [];

  handleSubmitText(state, "/tasks history", (type, payload) => sent.push({ type, payload }));

  assert.deepEqual(sent, [{
    type: "task_panel",
    payload: {
      limit: 12,
      source: "background",
      status: "all",
      history: true,
      pinned: false,
      refresh: false,
    },
  }]);
  assert.equal(state.taskPanel.history, true);
});

test("task panel command parses detail id and preserves it for pinned refresh", () => {
  const state = createInitialState();
  const sent = [];
  const send = (type, payload) => sent.push({ type, payload });

  handleSubmitText(state, "/tasks detail bg_0001", send);
  handleSubmitText(state, "/tasks pin detail=run_1 source=browser status=needs_input", send);

  assert.deepEqual(sent, [
    {
      type: "task_panel",
      payload: {
        limit: 12,
        source: "all",
        status: "all",
        pinned: false,
        refresh: false,
        detail_id: "bg_0001",
      },
    },
    {
      type: "task_panel",
      payload: {
        limit: 12,
        source: "browser",
        status: "needs_input",
        pinned: true,
        refresh: false,
        detail_id: "run_1",
      },
    },
  ]);
  assert.equal(state.taskPanel.detailId, "run_1");

  const actions = reduceServerEvent(state, {
    type: "runtime/status",
    payload: { tasks: { browser_active: 1 } },
  });

  assert.deepEqual(actions, [{
    type: "refresh_task_panel",
    limit: 12,
    source: "browser",
    status: "needs_input",
    detailId: "run_1",
  }]);
});

test("task panel extracts selectable items and opens selected detail locally", () => {
  const state = createInitialState();
  const sent = [];
  const send = (type, payload) => sent.push({ type, payload });
  const content = [
    "任务面板",
    "Todo",
    "  - #1 [running] 写入页面 | owner=main",
    "Subagent",
    "  - running: reviewer / task-9 正在审查",
    "Background",
    "  - bg_0001 [running] npm run dev | cwd=/tmp/project",
    "Browser Runs",
    "  - run_7 [needs_input] 打开页面 | steps=3; records=/tmp/browser-trace.zip, /tmp/screen.png",
  ].join("\n");

  assert.deepEqual(extractTaskPanelItems(content).map((item) => item.id), ["1", "task-9", "bg_0001", "run_7"]);
  assert.equal(extractTaskPanelItems(content).find((item) => item.id === "run_7").recordPath, "/tmp/browser-trace.zip");

  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "system_notice", title: "tasks", content },
  });

  assert.equal(hasTaskPanelFocus(state), true);
  assert.equal(state.taskPanel.selectedId, "1");

  selectTaskPanelOffset(state, 1);
  assert.equal(state.taskPanel.selectedId, "task-9");

  handleSubmitText(state, "/tasks select bg_0001", send);
  assert.equal(state.taskPanel.selectedId, "bg_0001");

  handleSubmitText(state, "/tasks open", send);
  assert.equal(state.taskPanel.detailId, "bg_0001");
  assert.equal(sent.at(-1).type, "task_panel");
  assert.equal(sent.at(-1).payload.detail_id, "bg_0001");

  handleSubmitText(state, "/tasks cancel", send);
  assert.equal(sent.at(-1).type, "task_cancel");
  assert.deepEqual(sent.at(-1).payload, {
    task_id: "bg_0001",
    source: "background",
    reason: "用户从任务面板取消。",
  });

  handleSubmitText(state, "/tasks jump run_7", send);
  const jump = state.messages.at(-1);
  assert.equal(jump.title, "任务记录");
  assert(String(jump.content).includes("记录: /tmp/browser-trace.zip"));

  handleSubmitText(state, "/tasks expand bg_0001", send);
  assert.equal(state.taskPanel.expandedIds.bg_0001, true);
  handleSubmitText(state, "/tasks collapse bg_0001", send);
  assert.equal(state.taskPanel.expandedIds.bg_0001, undefined);

  setTaskPanelFocus(state, false);
  assert.equal(hasTaskPanelFocus(state), false);
  assert.equal(state.messages.at(-1).content, "任务面板焦点已退出。");

  handleSubmitText(state, "/tasks focus", send);
  assert.equal(hasTaskPanelFocus(state), true);
});

test("task panel timeline rows preserve concrete source and record paths", () => {
  const content = [
    "任务面板",
    "Timeline",
    "  - run_7 [needs_input] 打开页面 | time=2026-06-01T12:00:00; source=browser; event=browser:run_7; records=/tmp/browser-trace.zip, /tmp/screen.png",
    "  - bg_0001 [running] npm run dev | time=-; source=background; event=background:bg_0001; output=/tmp/bg.log",
  ].join("\n");

  const items = extractTaskPanelItems(content);

  assert.deepEqual(items.map((item) => item.id), ["run_7", "bg_0001"]);
  assert.equal(items[0].source, "browser");
  assert.equal(items[0].recordPath, "/tmp/browser-trace.zip");
  assert.equal(items[1].source, "background");
  assert.equal(items[1].recordPath, "/tmp/bg.log");
});

test("task panel selectable order keeps timeline behind primary task rows", () => {
  const content = [
    "任务面板",
    "Timeline",
    "  - bg_0001 [running] npm run dev | source=background; output=/tmp/bg.log",
    "  - run_7 [needs_input] 打开页面 | source=browser; records=/tmp/browser.zip",
    "Todo",
    "  - #1 [running] 写入页面 | owner=main",
    "Background",
    "  - bg_0001 [running] npm run dev | output=/tmp/bg.log",
  ].join("\n");

  const items = extractTaskPanelItems(content);

  assert.deepEqual(items.map((item) => item.id), ["1", "bg_0001", "run_7"]);
  assert.equal(items[0].source, "todo");
  assert.equal(items[2].source, "browser");
});

test("task panel timeline source collapse is local UI state", () => {
  const state = createInitialState();
  const sent = [];
  const send = (type, payload) => sent.push({ type, payload });

  handleSubmitText(state, "/tasks timeline collapse browser", send);
  assert.equal(state.taskPanel.collapsedTimelineSources.browser, true);
  assert.equal(sent.length, 0);
  assert.equal(state.messages.at(-1).title, "任务面板");
  assert(state.messages.at(-1).content.includes("已折叠"));

  handleSubmitText(state, "/tasks timeline expand browser", send);
  assert.equal(state.taskPanel.collapsedTimelineSources.browser, undefined);

  handleSubmitText(state, "/tasks timeline toggle background", send);
  assert.equal(state.taskPanel.collapsedTimelineSources.background, true);

  handleSubmitText(state, "/tasks timeline clear", send);
  assert.deepEqual(state.taskPanel.collapsedTimelineSources, {});

  handleSubmitText(state, "/tasks timeline collapse nope", send);
  assert.equal(state.messages.at(-1).level, "warning");

  state.taskPanel.collapsedTimelineSources.browser = true;
  handleSubmitText(state, "/tasks off", send);
  assert.deepEqual(state.taskPanel.collapsedTimelineSources, {});
});

test("debug command shows frontend and bridge trace paths without backend calls", () => {
  const state = createInitialState();
  const sent = [];
  const send = (type, payload) => sent.push({ type, payload });
  state.frontendDebugLogPath = "/tmp/terminal-ui-debug.jsonl";
  reduceServerEvent(state, {
    type: "debug/trace",
    payload: {
      run_id: "run-1",
      events_path: "/tmp/bridge-events.jsonl",
      transcript_path: "/tmp/bridge-transcript.jsonl",
    },
  });

  handleSubmitText(state, "/debug", send);

  assert.deepEqual(sent, []);
  const message = state.messages.at(-1);
  assert.equal(message.kind, "system");
  assert.equal(message.title, "debug");
  assert(String(message.content).includes("前端日志: /tmp/terminal-ui-debug.jsonl"));
  assert(String(message.content).includes("Bridge events: /tmp/bridge-events.jsonl"));
  assert(String(message.content).includes("Bridge run: run-1"));
});

test("fold commands list and toggle fold entries without backend calls", () => {
  const state = createInitialState();
  const sent = [];
  const send = (type, payload) => sent.push({ type, payload });
  const codeLines = Array.from({ length: 45 }, (_, index) => `const value${index} = ${index};`).join("\n");

  reduceServerEvent(state, { type: "ui/message", payload: { type: "assistant_stream", phase: "token", content: `\`\`\`js\n${codeLines}\n\`\`\`` } });
  assert.equal(getFoldEntries(state).length, 1);

  handleSubmitText(state, "/folds", send);
  handleSubmitText(state, "/expand 1", send);
  assert.equal(Object.values(state.folds)[0].expanded, true);
  handleSubmitText(state, "/collapse all", send);
  assert.equal(Object.values(state.folds)[0].expanded, false);
  assert.deepEqual(sent, []);
  assert(state.messages.some((message) => message.kind === "system" && String(message.content).includes("assistant code")));
});

test("ui snapshots persist folds, scroll offset, and multiline composer draft", () => {
  const state = createInitialState();
  state.scrollOffset = 9;
  state.foldCursor = 2;
  state.folds = { "message:assistant-1:code:0": { expanded: true } };
  state.input = "第一行\n第二行";
  state.inputCursor = 3;
  state.inputPreferredColumn = 2;
  state.composerIntent = "task";
  state.activeTaskSubmission = {
    requestId: "submit-4",
    taskId: "task-4",
    missionId: "mission-2",
    state: "running",
  };

  const restored = createInitialState();
  applyUiSnapshot(restored, createUiSnapshot(state));

  assert.equal(restored.scrollOffset, 9);
  assert.equal(restored.foldCursor, 2);
  assert.deepEqual(restored.folds, { "message:assistant-1:code:0": { expanded: true } });
  assert.equal(restored.input, "第一行\n第二行");
  assert.equal(restored.inputCursor, 3);
  assert.equal(restored.inputPreferredColumn, 2);
  assert.equal(restored.composerIntent, "task");
  assert.deepEqual(restored.activeTaskSubmission, state.activeTaskSubmission);
});

test("snapshot does not persist task intent without an unsent draft", () => {
  const state = createInitialState();
  state.composerIntent = "task";
  state.input = "";

  const snapshot = createUiSnapshot(state);
  const restored = createInitialState();
  applyUiSnapshot(restored, snapshot);

  assert.equal(snapshot.composer.intent, "chat");
  assert.equal(restored.composerIntent, "chat");
});

test("snapshot rejects malformed active task submission metadata", () => {
  const restored = createInitialState();

  applyUiSnapshot(restored, {
    activeTaskSubmission: {
      requestId: "x".repeat(500),
      taskId: ["invalid"],
      missionId: "mission-9",
      state: "mystery",
    },
  });

  assert.equal(restored.activeTaskSubmission, null);
});

test("follow tail snapshot derives detached state without stale unread", () => {
  const source = createInitialState();
  source.scrollOffset = 7;
  source.followTail = false;
  source.unreadOutputCount = 3;
  source.unreadOutputKeys = { "assistant:old": true };

  const restored = createInitialState();
  applyUiSnapshot(restored, createUiSnapshot(source));
  assert.equal(restored.scrollOffset, 7);
  assert.equal(restored.followTail, false);
  assert.equal(restored.unreadOutputCount, 0);
  assert.deepEqual(restored.unreadOutputKeys, {});

  source.scrollOffset = 0;
  applyUiSnapshot(restored, createUiSnapshot(source));
  assert.equal(restored.followTail, true);
  assert.equal(restored.scrollOffset, 0);
});

test("outbox snapshot restores queued delivery as uncertain without accepted messages", () => {
  const source = createInitialState();
  const send = (_type, _payload, options = {}) => options.id;
  handleSubmitText(source, "等待确认", send);
  handleSubmitText(source, "已经确认", send);
  const [queued, accepted] = source.messages.filter((message) => message.kind === "user");
  reduceServerEvent(source, {
    type: "user/message",
    request_id: accepted.requestId,
    payload: { content: accepted.content },
  });

  const snapshot = createUiSnapshot(source);
  assert.equal(snapshot.outbox.length, 1);
  assert.equal(snapshot.outbox[0].requestId, queued.requestId);

  const restored = createInitialState();
  applyUiSnapshot(restored, snapshot);
  const message = restored.messages.find((item) => item.localOutbox);
  assert.equal(message.deliveryStatus, "uncertain");
  assert.equal(message.content, "等待确认");
  assert.equal(restored.messages.some((item) => item.content === "已经确认"), false);
});

test("outbox snapshot keeps failures, bounds entries, and ignores malformed values", () => {
  const restored = createInitialState();
  applyUiSnapshot(restored, {
    outbox: [
      null,
      { requestId: "", content: "missing id", deliveryStatus: "failed" },
      { requestId: "accepted", content: "not local", deliveryStatus: "accepted" },
      ...Array.from({ length: 24 }, (_, index) => ({
        requestId: `request-${index}`,
        content: index === 23 ? "x".repeat(200_100) : `消息 ${index}`,
        deliveryStatus: "failed",
        attempt: index + 1,
        errorCode: "offline",
        errorMessage: "Bridge offline",
      })),
    ],
  });

  const outbox = restored.messages.filter((message) => message.localOutbox);
  assert.equal(outbox.length, 20);
  assert.equal(outbox[0].requestId, "request-4");
  assert.equal(outbox.at(-1).content.length, 200_000);
  assert(outbox.every((message) => message.deliveryStatus === "failed"));
});

test("applying an outbox snapshot twice does not duplicate local messages", () => {
  const state = createInitialState();
  const snapshot = {
    outbox: [{
      requestId: "submit-8",
      content: "只出现一次",
      deliveryStatus: "queued",
      attempt: 1,
    }],
  };

  applyUiSnapshot(state, snapshot);
  applyUiSnapshot(state, snapshot);

  assert.equal(state.messages.filter((message) => message.localOutbox).length, 1);
  assert.equal(state.nextSubmitId, 9);
});

test("replayed user content reconciles one uncertain outbox entry", () => {
  const state = createInitialState();
  applyUiSnapshot(state, {
    outbox: [{
      requestId: "submit-4",
      content: "可能已发送",
      deliveryStatus: "queued",
      attempt: 1,
    }],
  });

  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "user", content: "可能已发送", is_command: false },
  });

  const users = state.messages.filter((message) => message.kind === "user");
  assert.equal(users.length, 1);
  assert.equal(users[0].requestId, "submit-4");
  assert.equal(users[0].attempt, 1);
  assert.equal(users[0].deliveryStatus, "accepted");
  assert.equal(users[0].localOutbox, false);
});

test("applying a missing snapshot clears presentation state for a new session", () => {
  const state = createInitialState();
  state.input = "旧会话草稿";
  state.inputCursor = 4;
  state.scrollOffset = 8;
  state.folds = { stale: { expanded: true } };

  applyUiSnapshot(state, null);

  assert.equal(state.input, "");
  assert.equal(state.inputCursor, 0);
  assert.equal(state.scrollOffset, 0);
  assert.deepEqual(state.folds, {});
});

test("composer snapshot preserves an absent preferred column as null", () => {
  const state = createInitialState();

  applyUiSnapshot(state, {
    composer: { text: "草稿", cursor: 2, preferredColumn: null },
  });

  assert.equal(state.input, "草稿");
  assert.equal(state.inputCursor, 2);
  assert.equal(state.inputPreferredColumn, null);
});

test("slash completion lists candidates when input is only slash", () => {
  const candidates = getSlashCommandCompletions("/");

  assert.equal(candidates.length >= 10, true);
  assert.equal(candidates.some((item) => item.command === "/help"), true);
  assert.equal(candidates.some((item) => item.aliases.includes("/h")), true);
  assert.equal(candidates.some((item) => item.command === "/folds"), true);
  assert.equal(candidates.some((item) => item.command === "/expand"), true);
  assert.equal(candidates.some((item) => item.command === "/collapse"), true);
});

test("default slash completion exposes provider model discovery", () => {
  assert.equal(
    DEFAULT_SLASH_COMMAND_CANDIDATES.some((item) => item.command === "/models"),
    true,
  );
});

test("slash completion uses complete backend registry without truncation", () => {
  const longList = Array.from({ length: 30 }, (_, index) => ({
    command: `/cmd-${String(index).padStart(2, "0")}`,
    aliases: [`/c${String(index).padStart(2, "0")}`],
    description: `命令 ${index}`,
  }));
  const candidates = getSlashCommandCompletions("/", longList);

  assert.equal(candidates.length >= 30, true);
  assert.equal(candidates.some((item) => item.command === "/cmd-00"), true);
  assert.equal(candidates.some((item) => item.command === "/cmd-29"), true);
});

test("slash completion merges backend and local commands", () => {
  const candidates = getSlashCommandCompletions("/", [{ command: "/help", description: "后端帮助" }]);

  assert.equal(candidates.some((item) => item.command === "/help"), true);
  assert.equal(candidates.some((item) => item.command === "/folds"), true);
});

test("harness command is available before backend metadata arrives", () => {
  const harness = DEFAULT_SLASH_COMMAND_CANDIDATES.find((item) => item.command === "/harness");
  assert.equal(Boolean(harness), true);
  assert.equal(harness.description.includes("知识"), true);
});

test("slash completion filters by partial command", () => {
  const candidates = getSlashCommandCompletions("/h");

  assert(candidates.length >= 1);
  assert.equal(candidates.some((item) => item.command === "/help"), true);
});

test("slash completion closes after typing argument separator", () => {
  assert.equal(getSlashCommandCompletions("/help foo").length, 0);
  assert.equal(getSlashCommandCompletions("/help ").length, 0);
});

test("initial state includes empty workbench bucket", () => {
  const state = createInitialState();

  assert.deepEqual(state.workbench, {
    session_id: "",
    missions: [],
    tasks: [],
    issues: [],
    failures: [],
    events: [],
  });
});

test("workbench snapshot replaces dashboard state", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "workbench/snapshot",
    payload: {
      session_id: "s",
      missions: [{ id: "m1", title: "Mac 工作台" }],
      tasks: [{ id: "t1", subject: "实现协议" }],
      issues: [{ task_id: "1", risk_level: "high" }],
      failures: [{ id: "f1", kind: "test_failed" }],
      events: [{ id: "e1", type: "issue.created" }],
    },
  });

  assert.equal(state.workbench.session_id, "s");
  assert.equal(state.workbench.missions[0].title, "Mac 工作台");
  assert.equal(state.workbench.tasks[0].subject, "实现协议");
  assert.equal(state.workbench.issues[0].risk_level, "high");
  assert.equal(state.workbench.failures[0].kind, "test_failed");
  assert.equal(state.workbench.events[0].type, "issue.created");
});

test("task submit reuses optimistic delivery and task created accepts it in place", () => {
  const state = createInitialState();
  const sent = [];
  const send = (type, payload, options = {}) => {
    sent.push({ type, payload, options });
    return options.id;
  };

  const message = submitTaskMessage(state, "实现任务联动", send, {
    mission_id: "mission-1",
    acceptance_criteria: ["测试通过"],
    parallel_mode: "cooperative",
    risk_level: "high",
  });

  assert.equal(message.intent, "task");
  assert.equal(message.deliveryStatus, "queued");
  assert.equal(sent[0].type, "task_submit");
  assert.equal(sent[0].payload.text, "实现任务联动");
  assert.equal(sent[0].payload.mission_id, "mission-1");
  assert.equal(sent[0].options.id, message.requestId);

  reduceServerEvent(state, {
    type: "task/created",
    request_id: message.requestId,
    payload: {
      mission: { id: "mission-1", title: "任务联动" },
      task: { id: "7", subject: "实现任务联动", status: "in_progress" },
      issue: { task_id: "7", mission_id: "mission-1", risk_level: "high" },
      workbench_snapshot: {
        session_id: "session-1",
        missions: [{ id: "mission-1" }],
        tasks: [{ id: "7", status: "in_progress" }],
        issues: [{ task_id: "7" }],
        failures: [],
        events: [],
      },
    },
  });

  assert.equal(state.messages.length, 1);
  assert.equal(message.deliveryStatus, "accepted");
  assert.equal(message.taskId, "7");
  assert.equal(message.missionId, "mission-1");
  assert.equal(state.activeTaskSubmission.taskId, "7");
  assert.equal(state.activeTaskSubmission.state, "running");
  assert.equal(state.composerIntent, "chat");
  assert.equal(state.workbench.tasks[0].id, "7");

  reduceServerEvent(state, {
    type: "run/completed",
    request_id: message.requestId,
    payload: { status: "completed", task_id: "7", mission_id: "mission-1", intent: "task" },
  });
  assert.equal(state.activeTaskSubmission.state, "completed");
  assert.equal(message.taskStatus, "completed");
});

test("composer intent and task commands route without stealing task detail", () => {
  const state = createInitialState();
  const sent = [];
  const send = (type, payload, options = {}) => {
    sent.push({ type, payload, options });
    return options.id;
  };

  assert.equal(toggleComposerIntent(state), "task");
  handleSubmitText(state, "持续任务模式提交", send);
  handleSubmitText(state, "/chat", send);
  handleSubmitText(state, "/task create 显式创建任务", send);
  handleSubmitText(state, "/task 17", send);
  handleSubmitText(state, "/task #18", send);

  assert.equal(state.composerIntent, "chat");
  assert.equal(sent[0].type, "task_submit");
  assert.equal(sent[0].payload.text, "持续任务模式提交");
  assert.equal(sent[1].type, "task_submit");
  assert.equal(sent[1].payload.text, "显式创建任务");
  assert.deepEqual(sent[2], {
    type: "task_panel",
    payload: { limit: 12, source: "all", status: "all", detail_id: "17", pinned: false, refresh: false },
    options: {},
  });
  assert.deepEqual(sent[3], {
    type: "task_panel",
    payload: { limit: 12, source: "all", status: "all", detail_id: "18", pinned: false, refresh: false },
    options: {},
  });
  assert(state.messages.some((message) => message.kind === "system" && message.content.includes("对话模式")));
});

test("task command validates missing content and persistent intent waits for acceptance", () => {
  const state = createInitialState();
  const sent = [];
  toggleComposerIntent(state);

  handleSubmitText(state, "/task create", (type, payload) => sent.push({ type, payload }));

  assert.equal(sent.length, 0);
  assert.equal(state.composerIntent, "task");
  assert(state.messages.some((message) => message.kind === "system" && message.level === "warning"));

  const message = handleSubmitText(state, "等待 Bridge 接受", (type, payload, options = {}) => {
    sent.push({ type, payload, options });
    return options.id;
  });
  assert.equal(state.composerIntent, "task");
  reduceServerEvent(state, {
    type: "task/created",
    request_id: message.requestId,
    payload: { mission: { id: "m1" }, task: { id: "9", status: "in_progress" }, issue: {} },
  });
  assert.equal(state.composerIntent, "chat");
});

test("retry preserves task submit intent and structured draft", () => {
  const state = createInitialState();
  const sent = [];
  const send = (type, payload, options = {}) => {
    sent.push({ type, payload, options });
    return options.id;
  };
  const message = submitTaskMessage(state, "修复任务", send, {
    mission_id: "mission-2",
    acceptance_criteria: ["pytest 通过"],
    risk_level: "high",
  });
  reduceServerEvent(state, {
    type: "error",
    request_id: message.requestId,
    payload: { code: "task_create_failed", message: "创建失败" },
  });

  retryUserMessage(state, send, message.requestId);

  assert.equal(sent.length, 2);
  assert.equal(sent[1].type, "task_submit");
  assert.equal(sent[1].payload.text, "修复任务");
  assert.equal(sent[1].payload.mission_id, "mission-2");
  assert.deepEqual(sent[1].payload.acceptance_criteria, ["pytest 通过"]);
  assert.equal(message.intent, "task");
  assert.equal(message.attempt, 2);
});

test("task execution error blocks the accepted task instead of making it retry-create", () => {
  const state = createInitialState();
  const message = submitTaskMessage(state, "执行后失败", (_type, _payload, options = {}) => options.id);
  reduceServerEvent(state, {
    type: "task/created",
    request_id: message.requestId,
    payload: {
      mission: { id: "mission-4" },
      task: { id: "11", status: "in_progress" },
      issue: { task_id: "11" },
    },
  });

  reduceServerEvent(state, {
    type: "error",
    request_id: message.requestId,
    payload: {
      code: "run_failed",
      message: "模型执行失败",
      intent: "task",
      task_id: "11",
      mission_id: "mission-4",
      task_status: "blocked",
    },
  });

  assert.equal(state.activeTaskSubmission.state, "blocked");
  assert.equal(message.taskStatus, "blocked");
  assert.equal(message.deliveryStatus, "accepted");
  assert.equal(message.localOutbox, false);
});

test("run cancellation request is single-flight and exposes stopping state", () => {
  const state = createInitialState();
  state.running = true;
  const sent = [];
  const send = (type, payload, options = {}) => {
    sent.push({ type, payload, options });
    return options.id;
  };

  assert.equal(requestRunCancel(state, send), true);
  assert.equal(requestRunCancel(state, send), false);

  assert.equal(state.cancelPending, true);
  assert.equal(state.cancelRequestId, "cancel-1");
  assert.deepEqual(sent, [{
    type: "run_cancel",
    payload: { reason: "用户按下 Ctrl+C" },
    options: { id: "cancel-1" },
  }]);
  assert(state.messages.some(
    (message) => message.kind === "system" && message.content.includes("正在停止当前运行"),
  ));
});

test("run cancelled clears live state and blocks linked workbench task", () => {
  const state = createInitialState();
  const taskMessage = submitTaskMessage(
    state,
    "取消我",
    (_type, _payload, options = {}) => options.id,
  );
  reduceServerEvent(state, {
    type: "task/created",
    request_id: taskMessage.requestId,
    payload: {
      mission: { id: "mission-5" },
      task: { id: "12", status: "in_progress" },
      issue: { task_id: "12" },
    },
  });
  state.cancelPending = true;
  state.cancelRequestId = "cancel-2";
  state.permission = { requestId: "perm-1", payload: {} };
  state.activeToolPrepare = { id: "prepare-1" };

  reduceServerEvent(state, {
    type: "run/cancelled",
    request_id: "cancel-2",
    payload: {
      status: "cancelled",
      target_request_id: taskMessage.requestId,
      intent: "task",
      task_id: "12",
      mission_id: "mission-5",
      task_status: "blocked",
      reason: "用户取消了当前运行。",
    },
  });

  assert.equal(state.running, false);
  assert.equal(state.cancelPending, false);
  assert.equal(state.cancelRequestId, "");
  assert.equal(state.permission, null);
  assert.equal(state.activeToolPrepare, null);
  assert.equal(state.activeTaskSubmission.state, "blocked");
  assert.equal(taskMessage.taskStatus, "blocked");
  assert(state.messages.some(
    (message) => message.kind === "system" && message.content.includes("运行已取消"),
  ));
});

test("correlated cancel rejection restores running presentation", () => {
  const state = createInitialState();
  state.running = true;
  state.cancelPending = true;
  state.cancelRequestId = "cancel-3";

  reduceServerEvent(state, {
    type: "error",
    request_id: "cancel-3",
    payload: { code: "no_active_run", message: "当前没有正在运行的任务。" },
  });

  assert.equal(state.cancelPending, false);
  assert.equal(state.cancelRequestId, "");
  assert.equal(state.running, false);
});

test("outbox snapshot restores task intent without automatic downgrade", () => {
  const source = createInitialState();
  submitTaskMessage(source, "持久化任务", () => {}, {
    mission_id: "mission-3",
    title: "任务标题",
    acceptance_criteria: ["验证通过"],
    parallel_mode: "exclusive",
    risk_level: "medium",
  });
  const snapshot = createUiSnapshot(source);
  assert.equal(snapshot.outbox[0].submitType, "task_submit");
  assert.equal(snapshot.outbox[0].taskDraft.mission_id, "mission-3");

  const restored = createInitialState();
  applyUiSnapshot(restored, snapshot);
  const message = restored.messages.find((item) => item.localOutbox);
  assert.equal(message.intent, "task");
  assert.equal(message.submitType, "task_submit");
  assert.equal(message.deliveryStatus, "uncertain");

  const sent = [];
  retryUserMessage(restored, (type, payload) => sent.push({ type, payload }));
  assert.equal(sent[0].type, "task_submit");
  assert.equal(sent[0].payload.mission_id, "mission-3");
});

test("workbench event appends to event log and keeps last 100", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "workbench/snapshot",
    payload: {
      session_id: "s",
      missions: [],
      tasks: [],
      issues: [],
      failures: [],
      events: [{ id: "e0", type: "snapshot.seed" }],
    },
  });

  for (let index = 1; index <= 105; index += 1) {
    reduceServerEvent(state, {
      type: "workbench/event",
      payload: { id: `e${index}`, type: "issue.updated", actor: "agent", subject_id: String(index), payload: {}, timestamp: "" },
    });
  }

  assert.equal(state.workbench.events.length, 100);
  assert.equal(state.workbench.events[0].id, "e6");
  assert.equal(state.workbench.events.at(-1).id, "e105");
});

test("run activity group aggregates backend phases into one durable message", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "run/started",
    request_id: "submit-activity-1",
    payload: { task: "实现活动组", intent: "chat" },
  });
  const activity = state.activeRunActivity;
  assert(activity);
  assert.equal(activity.kind, "run_activity");
  assert.equal(activity.phase, "preparing");
  assert.equal(state.messages.filter((message) => message.kind === "run_activity").length, 1);

  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "runtime_status", phase: "turn_start", turn: 2, model: "model-a" },
  });
  for (let index = 0; index < 7; index += 1) {
    reduceServerEvent(state, {
      type: "ui/message",
      payload: {
        type: "runtime_status",
        phase: "perf_phase",
        label: `阶段 ${index}`,
        duration_ms: index + 10,
      },
    });
  }

  assert.equal(activity.turn, 2);
  assert.equal(activity.model, "model-a");
  assert.equal(activity.perfPhases.length, 5);
  assert.equal(activity.perfPhases[0].label, "阶段 2");
  assert.equal(activity.perfPhases.at(-1).label, "阶段 6");
  assert.equal(state.messages.filter((message) => message.kind === "run_activity").length, 1);
});

test("run activity deduplicates tools and follows permission lifecycle", () => {
  const state = createInitialState();
  reduceServerEvent(state, {
    type: "run/started",
    request_id: "submit-activity-2",
    payload: { task: "运行工具" },
  });

  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "tool_prepare", phase: "start", tool_name: "file_read", tool_call_id: "call-1" },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "tool_use", tool_name: "file_read", tool_call_id: "call-1", file_path: "README.md" },
  });
  reduceServerEvent(state, {
    type: "permission/request",
    request_id: "perm-activity-1",
    payload: { tool_name: "bash_run", reason: "需要执行" },
  });

  const activity = state.activeRunActivity;
  assert.equal(Object.keys(activity.toolCalls).length, 1);
  assert.equal(activity.toolCalls["call-1"].status, "running");
  assert.equal(activity.phase, "awaiting_permission");
  assert.equal(activity.permissionCount, 1);

  reduceServerEvent(state, {
    type: "permission/resolved",
    payload: { request_id: "perm-activity-1", choice: "allow" },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "tool_result", tool_name: "file_read", tool_call_id: "call-1", status: "success" },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "assistant_stream", phase: "start" },
  });

  assert.equal(activity.phase, "summarizing");
  assert.equal(activity.toolCalls["call-1"].status, "success");
  assert.equal(Object.keys(activity.toolCalls).length, 1);
});

test("run activity terminal event releases active pointer and preserves receipt", () => {
  const state = createInitialState();
  reduceServerEvent(state, {
    type: "run/started",
    request_id: "submit-activity-3",
    payload: { task: "完成活动" },
  });
  const activity = state.activeRunActivity;

  reduceServerEvent(state, {
    type: "run/completed",
    request_id: "submit-activity-3",
    payload: { status: "" },
  });

  assert.equal(state.activeRunActivity, null);
  assert.equal(activity.status, "completed");
  assert.equal(activity.phase, "completed");
  assert(Number.isFinite(activity.durationMs));
  assert(state.messages.includes(activity));
  assert.equal(state.messages.at(-1), activity);
});

test("completion receipt is deduplicated and finalized after run activity", () => {
  const state = createInitialState();
  reduceServerEvent(state, {
    type: "run/started",
    request_id: "submit-receipt-1",
    payload: { task: "修改并验证" },
  });
  reduceServerEvent(state, {
    type: "completion/receipt",
    request_id: "submit-receipt-1",
    payload: {
      schema_version: 1,
      receipt_id: "receipt-ui-1",
      run_id: "run-ui-1",
      outcome: "partial",
      summary: "验证失败，已保留改动。",
      changes: [{ path: "src/app.py", status: "modified", additions: 3, deletions: 1 }],
      validations: [{ command: "pytest -q", status: "failed", exit_code: 1, failed: 1 }],
      unverified: [],
      approvals: [],
      risks: [{ code: "validation_failed", level: "high", message: "1 项验证失败。" }],
      git_state: { available: true, branch: "main", dirty: true },
      next_actions: [{ id: "retry", label: "重试失败验证", kind: "retry_validation" }],
      evidence_refs: ["run:run-ui-1:tool:test-1"],
      duration_ms: 1200,
    },
  });
  reduceServerEvent(state, {
    type: "completion/receipt",
    request_id: "submit-receipt-1",
    payload: {
      schema_version: 1,
      receipt_id: "receipt-ui-1",
      run_id: "run-ui-1",
      outcome: "partial",
      summary: "不应覆盖已接收回执。",
    },
  });
  reduceServerEvent(state, {
    type: "run/completed",
    request_id: "submit-receipt-1",
    payload: {
      status: "completed",
      receipt_id: "receipt-ui-1",
      run_id: "run-ui-1",
    },
  });

  const receipts = state.messages.filter((message) => message.kind === "completion_receipt");
  assert.equal(receipts.length, 1);
  assert.equal(receipts[0].receipt.summary, "验证失败，已保留改动。");
  assert.equal(state.messages.at(-1), receipts[0]);
  assert.equal(state.activeRunActivity, null);
});

test("run completion requests a missing authoritative receipt", () => {
  const state = createInitialState();
  reduceServerEvent(state, {
    type: "run/started",
    request_id: "submit-missing-receipt",
    payload: { task: "等待回执" },
  });

  const actions = reduceServerEvent(state, {
    type: "run/completed",
    request_id: "submit-missing-receipt",
    payload: {
      status: "completed",
      receipt_id: "receipt-missing",
      run_id: "run-missing",
    },
  });

  assert.deepEqual(actions, [
    {
      type: "request_completion_receipt",
      sessionId: "",
      receiptId: "receipt-missing",
      runId: "run-missing",
    },
  ]);
});

test("replayed completion receipt remains visible without an active run", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "completion/receipt",
    request_id: "resume-1",
    payload: {
      schema_version: 1,
      receipt_id: "receipt-history",
      run_id: "run-history",
      outcome: "cancelled",
      summary: "历史运行已取消。",
      changes: [],
      validations: [],
      unverified: [],
      approvals: [],
      risks: [],
      git_state: { available: false, dirty: false },
      next_actions: [],
      evidence_refs: [],
    },
  });

  assert.equal(state.messages.at(-1).kind, "completion_receipt");
  assert.equal(state.messages.at(-1).receipt.outcome, "cancelled");
});

test("cancelled run keeps its authoritative receipt as the final card", () => {
  const state = createInitialState();
  reduceServerEvent(state, {
    type: "run/started",
    request_id: "submit-cancel-receipt",
    payload: { task: "执行后取消" },
  });
  reduceServerEvent(state, {
    type: "completion/receipt",
    request_id: "submit-cancel-receipt",
    payload: {
      schema_version: 1,
      receipt_id: "receipt-cancelled",
      run_id: "run-cancelled",
      outcome: "cancelled",
      summary: "运行已取消，改动证据已保留。",
    },
  });
  reduceServerEvent(state, {
    type: "run/cancelled",
    request_id: "cancel-request",
    payload: {
      target_request_id: "submit-cancel-receipt",
      receipt_id: "receipt-cancelled",
      run_id: "run-cancelled",
      reason: "用户取消。",
    },
  });

  assert.equal(state.messages.at(-1).kind, "completion_receipt");
  assert.equal(state.messages.at(-1).receiptId, "receipt-cancelled");
});

test("run activity keeps sequential same-name tools distinct without call ids", () => {
  const state = createInitialState();
  reduceServerEvent(state, {
    type: "run/started",
    request_id: "submit-activity-anonymous",
    payload: { task: "连续读取文件" },
  });

  for (const filePath of ["a.txt", "b.txt"]) {
    reduceServerEvent(state, {
      type: "ui/message",
      payload: { type: "tool_prepare", phase: "start", tool_name: "file_read", path: filePath },
    });
    reduceServerEvent(state, {
      type: "ui/message",
      payload: { type: "tool_use", tool_name: "file_read", file_path: filePath },
    });
    reduceServerEvent(state, {
      type: "ui/message",
      payload: { type: "tool_result", tool_name: "file_read", status: "success" },
    });
  }

  const tools = Object.values(state.activeRunActivity.toolCalls);
  assert.equal(tools.length, 2);
  assert.deepEqual(tools.map((tool) => tool.status), ["success", "success"]);
});

test("session replay drops the previous active run pointer", () => {
  const state = createInitialState();
  reduceServerEvent(state, {
    type: "run/started",
    request_id: "submit-before-replay",
    payload: { task: "旧运行" },
  });

  reduceServerEvent(state, {
    type: "session/replayed",
    payload: { session_id: "replayed-session", title: "恢复会话", clear: true },
  });

  assert.equal(state.activeRunActivity, null);
  assert.equal(state.messages.some((message) => message.kind === "run_activity"), false);
});

test("run activity keeps concurrent same-name preparations distinct without call ids", () => {
  const state = createInitialState();
  reduceServerEvent(state, {
    type: "run/started",
    request_id: "submit-concurrent-tools",
    payload: { task: "并发读取文件" },
  });

  for (const path of ["a.txt", "b.txt"]) {
    reduceServerEvent(state, {
      type: "ui/message",
      payload: { type: "tool_prepare", phase: "start", tool_name: "file_read", path },
    });
  }
  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "tool_prepare", phase: "snapshot", tool_name: "file_read", path: "b.txt" },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "tool_use", tool_name: "file_read", path: "b.txt" },
  });

  const tools = Object.values(state.activeRunActivity.toolCalls);
  assert.equal(tools.length, 2);
  assert.deepEqual(tools.map((tool) => tool.status), ["prepared", "running"]);
});

test("run activity terminal events only affect their correlated active run", () => {
  const state = createInitialState();
  reduceServerEvent(state, {
    type: "run/started",
    request_id: "run-active",
    payload: { task: "保持运行" },
  });
  const activity = state.activeRunActivity;

  reduceServerEvent(state, {
    type: "run/cancelled",
    request_id: "cancel-other",
    payload: { target_request_id: "run-other" },
  });

  assert.equal(state.running, true);
  assert.equal(state.activeRunActivity, activity);
  assert.equal(activity.status, "running");

  reduceServerEvent(state, {
    type: "run/completed",
    request_id: "run-other",
    payload: {},
  });
  assert.equal(state.running, true);
  assert.equal(state.activeRunActivity, activity);
  assert.equal(activity.status, "running");

  reduceServerEvent(state, {
    type: "run/completed",
    request_id: "run-active",
    payload: {},
  });
  assert.equal(state.running, false);
  assert.equal(activity.status, "completed");
});

test("identified run ignores terminal events and errors without a target id", () => {
  const state = createInitialState();
  reduceServerEvent(state, {
    type: "run/started",
    request_id: "run-identified",
    payload: { task: "不能被空 ID 误杀" },
  });
  const activity = state.activeRunActivity;

  for (const record of [
    { type: "run/completed", payload: {} },
    { type: "run/cancelled", payload: {} },
    { type: "error", payload: { code: "bad_request", message: "请求格式错误" } },
  ]) {
    reduceServerEvent(state, record);
    assert.equal(state.running, true);
    assert.equal(state.activeRunActivity, activity);
    assert.equal(activity.status, "running");
  }
});

test("run cancelled uses target request id and legacy missing ids remain compatible", () => {
  const state = createInitialState();
  reduceServerEvent(state, { type: "run/started", request_id: "run-cancel-target", payload: { task: "取消目标" } });
  const activity = state.activeRunActivity;

  reduceServerEvent(state, {
    type: "run/cancelled",
    request_id: "cancel-request",
    payload: { target_request_id: "run-cancel-target" },
  });
  assert.equal(activity.status, "cancelled");

  reduceServerEvent(state, { type: "run/started", payload: { task: "旧协议运行" } });
  const legacyActivity = state.activeRunActivity;
  reduceServerEvent(state, { type: "run/completed", payload: {} });
  assert.equal(legacyActivity.status, "completed");

  reduceServerEvent(state, { type: "run/started", request_id: "run-cancel-fallback", payload: { task: "回退取消" } });
  const fallbackActivity = state.activeRunActivity;
  reduceServerEvent(state, {
    type: "run/cancelled",
    request_id: "run-cancel-fallback",
    payload: {},
  });
  assert.equal(fallbackActivity.status, "cancelled");
});

test("run activity ignores unrelated errors while matching run errors fail it", () => {
  const state = createInitialState();
  reduceServerEvent(state, { type: "run/started", request_id: "run-errors", payload: { task: "错误关联" } });
  const activity = state.activeRunActivity;

  reduceServerEvent(state, {
    type: "error",
    request_id: "unrelated-request",
    payload: { code: "transport", message: "另一条请求失败" },
  });
  assert.equal(state.running, true);
  assert.equal(state.activeRunActivity, activity);
  assert.equal(activity.status, "running");

  reduceServerEvent(state, {
    type: "error",
    request_id: "run-errors",
    payload: { code: "run_failed", message: "本轮执行失败" },
  });
  assert.equal(state.running, false);
  assert.equal(state.activeRunActivity, null);
  assert.equal(activity.status, "failed");
});

test("correlated cancel errors reconcile a stale active run", () => {
  const state = createInitialState();
  reduceServerEvent(state, { type: "run/started", request_id: "run-cancel-error", payload: { task: "取消后同步" } });
  const activity = state.activeRunActivity;
  state.cancelPending = true;
  state.cancelRequestId = "cancel-reconcile";

  reduceServerEvent(state, {
    type: "error",
    request_id: "cancel-reconcile",
    payload: { code: "no_active_run", message: "当前没有正在运行的任务。" },
  });

  assert.equal(state.running, false);
  assert.equal(state.activeRunActivity, null);
  assert.equal(activity.status, "failed");
});

test("session replay drops an in-progress run activity without clearing other messages", () => {
  const state = createInitialState();
  reduceServerEvent(state, { type: "run/started", request_id: "run-replay-keep", payload: { task: "旧运行" } });
  const activity = state.activeRunActivity;
  state.messages.push({ kind: "assistant", id: "keep-assistant", content: "保留内容" });

  reduceServerEvent(state, {
    type: "session/replayed",
    payload: { session_id: "replayed-session", title: "恢复会话", clear: false },
  });

  assert.equal(state.activeRunActivity, null);
  assert.equal(state.messages.includes(activity), false);
  assert(state.messages.some((message) => message.id === "keep-assistant"));
});

test("run activity derives one completed terminal status for activity and linked task", () => {
  const state = createInitialState();
  const taskMessage = submitTaskMessage(state, "完成任务", (_type, _payload, options = {}) => options.id);
  reduceServerEvent(state, {
    type: "task/created",
    request_id: taskMessage.requestId,
    payload: { mission: { id: "mission-complete" }, task: { id: "task-complete" }, issue: {} },
  });
  reduceServerEvent(state, {
    type: "run/started",
    request_id: taskMessage.requestId,
    payload: { intent: "task", task_id: "task-complete", task: "完成任务" },
  });
  const activity = state.activeRunActivity;

  reduceServerEvent(state, {
    type: "run/completed",
    request_id: taskMessage.requestId,
    payload: { intent: "task", task_id: "task-complete" },
  });

  assert.equal(activity.status, "completed");
  assert.equal(state.activeTaskSubmission.state, "completed");
  assert.equal(taskMessage.taskStatus, "completed");
});

test("run activity bounds stable tool ids while retaining updates for tracked tools", () => {
  const state = createInitialState();
  reduceServerEvent(state, { type: "run/started", request_id: "run-tool-bound", payload: { task: "工具上限" } });

  for (let index = 0; index < 101; index += 1) {
    reduceServerEvent(state, {
      type: "ui/message",
      payload: { type: "tool_prepare", phase: "start", tool_name: "file_read", tool_call_id: `call-${index}` },
    });
  }
  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "tool_result", tool_name: "file_read", tool_call_id: "call-0", status: "success" },
  });

  assert.equal(Object.keys(state.activeRunActivity.toolCalls).length, 100);
  assert.equal(state.activeRunActivity.toolCalls["call-0"].status, "success");
  assert.equal(state.activeRunActivity.toolCalls["call-100"], undefined);
});

test("run activity uses the authoritative bounded run-started label", () => {
  const state = createInitialState();
  reduceServerEvent(state, { type: "run/started", request_id: "run-runtime-label", payload: { task: "运行标签" } });
  const activity = state.activeRunActivity;
  const label = `后端确认正在准备${"中".repeat(300)}`;

  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "runtime_status", phase: "run_started", label },
  });

  assert.equal(activity.phase, "preparing");
  assert.match(activity.phaseLabel, /^后端确认正在准备/);
  assert(activity.phaseLabel.length < label.length);
  assert(activity.phaseLabel.length <= 160);
});

test("runtime inspector applies contiguous revisions and requests a full refresh for gaps", () => {
  const state = createInitialState();
  state.currentSessionId = "session-inspector";
  state.inspector.open = true;
  const first = runtimeInspectorFixture(3);

  assert.deepEqual(reduceServerEvent(state, {
    type: "inspector/snapshot",
    payload: first,
  }), []);
  assert.equal(state.inspector.revision, 3);
  assert.equal(state.inspector.snapshot.plan.items[0].subject, "实现运行检查器");
  assert.equal(state.inspector.loading, false);

  assert.deepEqual(reduceServerEvent(state, {
    type: "inspector/update",
    payload: {
      schema_version: 1,
      session_id: "session-inspector",
      revision: 4,
      generated_at: "2026-07-13T00:00:01+00:00",
      active_run_id: "run-1",
      changed_tabs: {
        tools: {
          ...first.tools,
          items: [{ call_id: "read-2", name: "file_read", status: "running" }],
        },
      },
    },
  }), []);
  assert.equal(state.inspector.revision, 4);
  assert.equal(state.inspector.snapshot.tools.items[0].call_id, "read-2");

  const beforeGap = state.inspector.snapshot;
  assert.deepEqual(reduceServerEvent(state, {
    type: "inspector/update",
    payload: {
      schema_version: 1,
      session_id: "session-inspector",
      revision: 6,
      generated_at: "2026-07-13T00:00:03+00:00",
      active_run_id: "run-1",
      changed_tabs: { plan: first.plan },
    },
  }), [{ type: "refresh_inspector", knownRevision: 4, sessionId: "session-inspector" }]);
  assert.equal(state.inspector.snapshot, beforeGap);
  assert.equal(state.inspector.loading, true);

  reduceServerEvent(state, {
    type: "inspector/update",
    payload: {
      schema_version: 1,
      session_id: "session-inspector",
      revision: 4,
      generated_at: "older",
      active_run_id: "run-1",
      changed_tabs: { plan: first.plan },
    },
  });
  assert.equal(state.inspector.revision, 4);

  reduceServerEvent(state, {
    type: "inspector/snapshot",
    payload: runtimeInspectorFixture(2),
  });
  assert.equal(state.inspector.revision, 4);
});

test("runtime inspector rejects cross-session data and clears authoritative data on replay", () => {
  const state = createInitialState();
  state.currentSessionId = "session-current";
  state.inspector.open = true;
  state.inspector.selectedTab = "tests";

  reduceServerEvent(state, {
    type: "inspector/snapshot",
    payload: { ...runtimeInspectorFixture(1), session_id: "session-other" },
  });
  assert.equal(state.inspector.snapshot, null);

  reduceServerEvent(state, {
    type: "inspector/snapshot",
    payload: { ...runtimeInspectorFixture(2), session_id: "session-current" },
  });
  reduceServerEvent(state, {
    type: "session/replayed",
    payload: { session_id: "session-next", title: "新会话", clear: true },
  });

  assert.equal(state.inspector.snapshot, null);
  assert.equal(state.inspector.revision, 0);
  assert.equal(state.inspector.selectedTab, "tests");
});

test("runtime inspector refresh errors keep the active run and last good snapshot", () => {
  const state = createInitialState();
  state.currentSessionId = "session-inspector";
  state.inspector.open = true;
  reduceServerEvent(state, { type: "inspector/snapshot", payload: runtimeInspectorFixture(1) });
  reduceServerEvent(state, {
    type: "run/started",
    request_id: "run-inspector-error",
    payload: { task: "继续运行" },
  });
  const snapshot = state.inspector.snapshot;

  reduceServerEvent(state, {
    type: "error",
    payload: {
      code: "inspector_refresh_failed",
      message: "Inspector 刷新失败，已保留上一次快照。",
    },
  });

  assert.equal(state.running, true);
  assert.equal(state.inspector.snapshot, snapshot);
  assert.equal(state.inspector.stale, true);
  assert.match(state.inspector.error, /刷新失败/);
});

test("runtime inspector toggles without changing the composer draft", () => {
  const state = createInitialState();
  state.currentSessionId = "session-toggle";
  state.input = "保留这份草稿";
  state.inputCursor = 4;
  const sent = [];
  const send = (type, payload) => sent.push({ type, payload });

  assert.equal(toggleRuntimeInspector(state, send), true);
  assert.equal(state.inspector.open, true);
  assert.equal(state.inspector.loading, true);
  assert.equal(state.input, "保留这份草稿");
  assert.equal(state.inputCursor, 4);
  assert.deepEqual(sent.at(-1), {
    type: "inspector/request",
    payload: { open: true, known_revision: 0, session_id: "session-toggle" },
  });

  assert.equal(toggleRuntimeInspector(state, send), false);
  assert.equal(state.inspector.open, false);
  assert.equal(state.inspector.focused, false);
  assert.equal(state.input, "保留这份草稿");
  assert.equal(sent.at(-1).payload.open, false);
});

test("runtime inspector explicitly focuses navigates expands and returns before closing", () => {
  const state = createInitialState();
  state.currentSessionId = "session-inspector";
  state.inspector.open = true;
  state.inspector.snapshot = runtimeInspectorFixture(1);
  state.inspector.snapshot.plan.items.push({
    id: "todo-2",
    subject: "验证交互",
    status: "pending",
    blocked_by: [],
  });
  const sent = [];
  const send = (type, payload) => sent.push({ type, payload });

  assert.equal(handleRuntimeInspectorKey(state, "\t", send), true);
  assert.equal(state.inspector.focused, true);
  assert.equal(handleRuntimeInspectorKey(state, "]", send), true);
  assert.equal(state.inspector.selectedTab, "tools");
  assert.equal(handleRuntimeInspectorKey(state, "\x1b[D", send), true);
  assert.equal(state.inspector.selectedTab, "plan");
  assert.equal(handleRuntimeInspectorKey(state, "\x1b[B", send), true);
  assert.equal(state.inspector.selectionByTab.plan, 1);
  assert.equal(handleRuntimeInspectorKey(state, "\r", send), true);
  assert.equal(state.inspector.expandedByTab.plan["1"], true);

  assert.equal(handleRuntimeInspectorKey(state, "\x1b", send), true);
  assert.equal(state.inspector.focused, false);
  assert.equal(state.inspector.open, true);
  assert.equal(handleRuntimeInspectorKey(state, "\x1b", send), true);
  assert.equal(state.inspector.open, false);
  assert.equal(sent.at(-1).payload.open, false);
});

test("task panel and runtime inspector never retain focus simultaneously", () => {
  const state = createInitialState();
  state.inspector.open = true;
  state.inspector.focused = true;
  state.taskPanel.messageId = "tasks-focus";
  state.taskPanel.items = [{ id: "todo:1", source: "todo" }];

  assert.equal(setTaskPanelFocus(state, true), true);
  assert.equal(state.taskPanel.focused, true);
  assert.equal(state.inspector.focused, false);
});

test("ui snapshot persists only bounded runtime inspector presentation state", () => {
  const state = createInitialState();
  state.inspector.open = true;
  state.inspector.focused = true;
  state.inspector.selectedTab = "changes";
  state.inspector.selectionByTab = { plan: 2, changes: 4, surprise: 99 };
  state.inspector.expandedByTab = { changes: { "4": true, nope: "yes" } };
  state.inspector.scrollByTab = { changes: 8 };
  state.inspector.snapshot = runtimeInspectorFixture(7);
  state.inspector.revision = 7;

  const snapshot = createUiSnapshot(state);
  assert.deepEqual(snapshot.inspector, {
    open: true,
    selectedTab: "changes",
    selectionByTab: { plan: 2, changes: 4 },
    expandedByTab: { changes: { "4": true } },
    scrollByTab: { changes: 8 },
  });
  assert.equal("snapshot" in snapshot.inspector, false);

  const restored = createInitialState();
  applyUiSnapshot(restored, snapshot);
  assert.equal(restored.inspector.open, true);
  assert.equal(restored.inspector.focused, false);
  assert.equal(restored.inspector.selectedTab, "changes");
  assert.equal(restored.inspector.snapshot, null);
  assert.equal(restored.inspector.loading, true);
});

function runtimeInspectorFixture(revision) {
  return {
    schema_version: 1,
    session_id: "session-inspector",
    revision,
    generated_at: "2026-07-13T00:00:00+00:00",
    active_run_id: "run-1",
    plan: {
      state: "ready",
      items: [{ id: "todo-1", subject: "实现运行检查器", status: "in_progress", blocked_by: [] }],
      next_actions: [],
      warnings: [],
    },
    tools: {
      state: "ready",
      items: [{ call_id: "read-1", name: "file_read", status: "success" }],
      approvals: [],
      warnings: [],
    },
    context: { state: "empty", warnings: [] },
    changes: { state: "empty", items: [], git_state: {}, warnings: [] },
    tests: { state: "empty", validations: [], unverified: [], next_actions: [], warnings: [] },
  };
}
