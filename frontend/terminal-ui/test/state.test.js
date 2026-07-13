import test from "node:test";
import assert from "node:assert/strict";
import { stripAnsi } from "../src/ansi.js";
import { renderScreen } from "../src/render.js";
import {
  createInitialState,
  createUiSnapshot,
  applyUiSnapshot,
  extractTaskPanelItems,
  failQueuedUserMessages,
  getFoldEntries,
  handleSubmitText,
  hasTaskPanelFocus,
  getSlashCommandCompletions,
  reduceServerEvent,
  selectTaskPanelOffset,
  setTaskPanelFocus,
  retryUserMessage,
  submitTaskMessage,
  toggleComposerIntent,
} from "../src/state.js";

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
    },
  });

  assert.equal(state.currentSessionId, "sess-2026-06-03");
  assert.equal(state.showReasoning, true);
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

test("permission request creates an updatable history card", () => {
  const state = createInitialState();

  reduceServerEvent(state, {
    type: "permission/request",
    request_id: "perm-1",
    payload: { tool_name: "bash_run", reason: "需要启动本地服务。" },
  });

  const card = state.messages.at(-1);
  assert.equal(state.permission.requestId, "perm-1");
  assert.equal(card.kind, "permission");
  assert.equal(card.requestId, "perm-1");
  assert.equal(card.message.status, "needs_confirmation");
  assert.equal(card.message.requires_confirmation, true);

  reduceServerEvent(state, {
    type: "permission/resolved",
    payload: { request_id: "perm-1", choice: "allow" },
  });

  assert.equal(state.permission, null);
  assert.equal(state.messages.at(-1), card);
  assert.equal(card.message.status, "allowed");
  assert.equal(card.message.requires_confirmation, false);
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
  handleSubmitText(state, "/doctor", send);
  handleSubmitText(state, "/reasoning on", send);
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
    { type: "doctor", payload: {} },
    { type: "set_reasoning", payload: { enabled: true } },
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
