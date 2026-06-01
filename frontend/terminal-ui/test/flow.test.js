import test from "node:test";
import assert from "node:assert/strict";
import { stripAnsi, visibleWidth } from "../src/ansi.js";
import { renderScreen } from "../src/render.js";
import { createInitialState, reduceServerEvent } from "../src/state.js";

function replay(records) {
  const state = createInitialState();
  for (const record of records) {
    reduceServerEvent(state, record);
  }
  return state;
}

test("terminal UI consumes a first-phase conversation event flow", () => {
  const state = replay([
    {
      type: "ready",
      payload: {
        mode: "default",
        model: "openai/kimi-for-coding",
        workspace_root: "/Users/lv/Workspace/NaumiAgent",
        usage: { total_tokens: 0 },
        context: { used: 0, window: 256000, percentage: 0 },
        budget: { used_usd: 0, max_usd: 5 },
        git: { branch: "main", dirty: true },
      },
    },
    { type: "mode/changed", payload: { mode: "bypass", status: { mode: "bypass" } } },
    { type: "run/started", payload: {} },
    { type: "user/message", payload: { content: "生成一个 todo 页面" } },
    { type: "ui/message", payload: { type: "assistant_stream", phase: "start" } },
    { type: "ui/message", payload: { type: "assistant_stream", phase: "token", content: "我会创建文件并验证。" } },
    { type: "ui/message", payload: { type: "assistant_stream", phase: "end" } },
    {
      type: "ui/message",
      payload: {
        type: "todo_status",
        total_count: 3,
        completed_count: 1,
        open_count: 2,
        items: [
          { id: 1, subject: "创建文件", status: "completed" },
          { id: 2, subject: "写入 HTML/CSS/JS", status: "in_progress" },
          { id: 3, subject: "浏览器验证", status: "pending" },
        ],
      },
    },
    {
      type: "permission/request",
      request_id: "perm-1",
      payload: { tool_name: "bash_run", reason: "需要启动本地预览服务。" },
    },
    { type: "permission/resolved", payload: { request_id: "perm-1", choice: "allow" } },
    {
      type: "ui/message",
      payload: { type: "tool_prepare", phase: "snapshot", tool_name: "file_write", path: "todo.html", content_lines: 120 },
    },
    {
      type: "ui/message",
      payload: { type: "tool_use", tool_call_id: "call-1", tool_name: "file_write", file_path: "todo.html" },
    },
    {
      type: "ui/message",
      payload: {
        type: "tool_result",
        tool_call_id: "call-1",
        tool_name: "file_write",
        status: "success",
        duration_ms: 18,
        content_preview: "已写入 todo.html",
        content_length: 12,
      },
    },
    { type: "run/completed", payload: {} },
  ]);

  const lines = renderScreen(state, 100, 32, { cwd: "/Users/lv/Workspace/NaumiAgent", home: "/Users/lv" });
  const plain = lines.map(stripAnsi).join("\n");

  assert.equal(state.mode, "bypass");
  assert.equal(state.running, false);
  assert.equal(state.todo.current.subject, "写入 HTML/CSS/JS");
  assert(plain.includes("生成一个 todo 页面"));
  assert(plain.includes("我会创建文件并验证。"));
  assert(plain.includes("file_write todo.html"));
  assert(plain.includes("todo: 1/3 完成"));
  assert(plain.includes("mode: bypass"));
  assert(lines.every((line) => visibleWidth(line) <= 100));
});

test("resume replay displays typed user messages and reconstructed tool cards", () => {
  const state = replay([
    {
      type: "session/replayed",
      payload: { session_id: "s1", title: "旧会话", message_count: 4, clear: true },
    },
    { type: "ui/message", payload: { type: "user", content: "继续检查 config.yaml", is_command: false } },
    { type: "ui/message", payload: { type: "assistant_stream", phase: "token", content: "我先读取配置。" } },
    {
      type: "ui/message",
      payload: { type: "tool_use", tool_call_id: "toolu_1", tool_name: "file_read", file_path: "config.yaml" },
    },
    {
      type: "ui/message",
      payload: {
        type: "tool_result",
        tool_call_id: "toolu_1",
        tool_name: "file_read",
        status: "success",
        content_preview: "models:\n  provider: openai\n",
        content_length: 26,
      },
    },
  ]);

  const lines = renderScreen(state, 88, 18, { cwd: "/Users/lv/Workspace/NaumiAgent", home: "/Users/lv" });
  const plain = lines.map(stripAnsi).join("\n");

  assert(plain.includes("已恢复会话: 旧会话"));
  assert(plain.includes("继续检查 config.yaml"));
  assert(plain.includes("我先读取配置。"));
  assert(plain.includes("file_read config.yaml"));
  assert(plain.includes("provider: openai"));
  assert(lines.every((line) => visibleWidth(line) <= 88));
});
