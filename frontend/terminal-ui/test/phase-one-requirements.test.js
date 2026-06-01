import test from "node:test";
import assert from "node:assert/strict";
import { stripAnsi, visibleWidth } from "../src/ansi.js";
import { renderScreen, renderToolCard } from "../src/render.js";
import {
  applyUiSnapshot,
  createInitialState,
  createUiSnapshot,
  getFoldEntries,
  handleSubmitText,
  reduceServerEvent,
} from "../src/state.js";

test("phase 1: complete conversation renders without footer overlap", () => {
  const state = replay([
    { type: "run/started", payload: {} },
    { type: "user/message", payload: { content: "生成一个展示页面" } },
    { type: "ui/message", payload: { type: "assistant_stream", phase: "start" } },
    { type: "ui/message", payload: { type: "assistant_stream", phase: "token", content: "我会创建文件并验证。" } },
    { type: "ui/message", payload: { type: "assistant_stream", phase: "end" } },
    { type: "run/completed", payload: {} },
  ]);

  const lines = renderScreen(state, 88, 16, { cwd: "/Users/lv/Workspace/NaumiAgent", home: "/Users/lv" });
  const plain = lines.map(stripAnsi).join("\n");

  assert(plain.includes("生成一个展示页面"));
  assert(plain.includes("我会创建文件并验证。"));
  assert(plain.includes("default >"));
  assert(lines.every((line) => visibleWidth(line) <= 88));
});

test("phase 1: tool cards use call ids and keep large diff folded", () => {
  const state = replay([
    {
      type: "ui/message",
      payload: { type: "tool_use", tool_call_id: "call-1", tool_name: "file_edit", file_path: "demo.py" },
    },
    {
      type: "ui/message",
      payload: { type: "tool_use", tool_call_id: "call-2", tool_name: "file_edit", file_path: "other.py" },
    },
    {
      type: "ui/message",
      payload: {
        type: "tool_result",
        tool_call_id: "call-1",
        tool_name: "file_edit",
        status: "success",
        content_preview: ["@@", ...Array.from({ length: 35 }, (_, index) => `+line ${index}`)].join("\n"),
        content_length: 500,
      },
    },
  ]);

  const plain = renderToolCard(state.tools[0], 96).map(stripAnsi).join("\n");

  assert.equal(state.tools[0].status, "success");
  assert.equal(state.tools[1].status, "running");
  assert(plain.includes("success file_edit demo.py"));
  assert(plain.includes("已折叠"));
});

test("phase 1: mode, permission, todo, and status footer render together", () => {
  const state = replay([
    {
      type: "ready",
      payload: {
        mode: "default",
        model: "openai/kimi-for-coding",
        workspace_root: "/Users/lv/Workspace/NaumiAgent",
        usage: { total_tokens: 123 },
        context: { used: 12000, window: 256000, percentage: 4.7 },
        budget: { used_usd: 0.03, max_usd: 5 },
        git: { branch: "main", dirty: true },
      },
    },
    { type: "mode/changed", payload: { mode: "bypass", status: { mode: "bypass" } } },
    {
      type: "permission/request",
      request_id: "perm-1",
      payload: { tool_name: "bash_run", reason: "需要确认。" },
    },
    {
      type: "ui/message",
      payload: {
        type: "todo_status",
        total_count: 2,
        completed_count: 1,
        open_count: 1,
        items: [{ id: 2, subject: "验证页面", status: "in_progress" }],
      },
    },
  ]);

  const plain = renderScreen(state, 110, 16, { cwd: "/Users/lv/Workspace/NaumiAgent", home: "/Users/lv" }).map(stripAnsi).join("\n");

  assert(plain.includes("permission: bash_run"));
  assert(plain.includes("todo: 1/2 完成"));
  assert(plain.includes("mode: bypass"));
  assert(plain.includes("openai/kimi-for-coding"));
});

test("phase 1: code and diff folds can be inspected and expanded", () => {
  const state = createInitialState();
  const sent = [];
  const send = (type, payload) => sent.push({ type, payload });
  const code = Array.from({ length: 45 }, (_, index) => `const value${index} = ${index};`).join("\n");

  reduceServerEvent(state, { type: "ui/message", payload: { type: "assistant_stream", phase: "token", content: `\`\`\`js\n${code}\n\`\`\`` } });
  reduceServerEvent(state, { type: "ui/message", payload: { type: "tool_use", tool_call_id: "call-1", tool_name: "file_edit", file_path: "demo.py" } });
  reduceServerEvent(state, { type: "ui/message", payload: { type: "tool_result", tool_call_id: "call-1", tool_name: "file_edit", status: "success", content_preview: ["@@", ...Array.from({ length: 35 }, (_, index) => `+line ${index}`)].join("\n") } });

  assert.equal(getFoldEntries(state).length, 2);
  handleSubmitText(state, "/expand all", send);
  assert.equal(sent.length, 0);
  const plain = renderScreen(state, 120, 80, { cwd: "/tmp", home: "/Users/lv" }).map(stripAnsi).join("\n");

  assert(plain.includes("value44"));
  assert(plain.includes("+line 34"));
});

test("phase 1: resume replay restores messages and local UI snapshot", () => {
  const state = replay([
    { type: "session/replayed", payload: { session_id: "s1", title: "旧会话", clear: true } },
    { type: "ui/message", payload: { type: "user", content: "继续检查 config.yaml" } },
    { type: "ui/message", payload: { type: "assistant_stream", phase: "token", content: "我先读取配置。" } },
    { type: "ui/message", payload: { type: "tool_use", tool_call_id: "call-r", tool_name: "file_read", file_path: "config.yaml" } },
    { type: "ui/message", payload: { type: "tool_result", tool_call_id: "call-r", tool_name: "file_read", status: "success", content_preview: "models:\n  provider: openai\n" } },
  ]);
  state.scrollOffset = 4;
  state.folds = { "tool:call-r": { expanded: true } };

  const restored = createInitialState();
  applyUiSnapshot(restored, createUiSnapshot(state));
  const plain = renderScreen(state, 100, 20, { cwd: "/tmp", home: "/Users/lv" }).map(stripAnsi).join("\n");

  assert.equal(restored.scrollOffset, 4);
  assert.deepEqual(restored.folds, { "tool:call-r": { expanded: true } });
  assert(plain.includes("继续检查 config.yaml"));
  assert(plain.includes("provider: openai"));
});

function replay(records) {
  const state = createInitialState();
  for (const record of records) {
    reduceServerEvent(state, record);
  }
  return state;
}
