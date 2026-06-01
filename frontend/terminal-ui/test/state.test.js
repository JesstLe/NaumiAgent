import test from "node:test";
import assert from "node:assert/strict";
import { createInitialState, createUiSnapshot, applyUiSnapshot, getFoldEntries, handleSubmitText, reduceServerEvent } from "../src/state.js";

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

test("slash commands route through protocol without adding chat noise", () => {
  const state = createInitialState();
  const sent = [];
  const send = (type, payload) => sent.push({ type, payload });

  handleSubmitText(state, "/mode bypass", send);
  handleSubmitText(state, "/load abc123", send);
  state.messages.push({ kind: "assistant", content: "old" });
  state.folds["message:old:code:0"] = { expanded: true };
  handleSubmitText(state, "/clear", send);
  handleSubmitText(state, "你好", send);

  assert.deepEqual(sent, [
    { type: "set_mode", payload: { mode: "bypass" } },
    { type: "resume", payload: { session_id: "abc123" } },
    { type: "submit", payload: { text: "你好" } },
  ]);
  assert.deepEqual(state.messages, []);
  assert.deepEqual(state.folds, {});
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

test("ui snapshots persist folds and scroll offset only", () => {
  const state = createInitialState();
  state.scrollOffset = 9;
  state.foldCursor = 2;
  state.folds = { "message:assistant-1:code:0": { expanded: true } };
  state.input = "不会持久化";

  const restored = createInitialState();
  applyUiSnapshot(restored, createUiSnapshot(state));

  assert.equal(restored.scrollOffset, 9);
  assert.equal(restored.foldCursor, 2);
  assert.deepEqual(restored.folds, { "message:assistant-1:code:0": { expanded: true } });
  assert.equal(restored.input, "");
});
