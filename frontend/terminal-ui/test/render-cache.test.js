import test from "node:test";
import assert from "node:assert/strict";
import { stripAnsi } from "../src/ansi.js";
import { renderBody, renderBodyWindow, renderFooter, renderScreen } from "../src/render.js";
import { createInitialState, handleSubmitText, reduceServerEvent } from "../src/state.js";

test("render cache reuses stable message render output", () => {
  const state = createInitialState();
  state.messages.push({ kind: "assistant", id: "assistant-1", content: "稳定消息" });

  renderScreen(state, 80, 12, { cwd: "/tmp", home: "/Users/lv" });
  assert.equal(state.renderCache.misses, 1);
  assert.equal(state.renderCache.hits, 0);

  renderScreen(state, 80, 12, { cwd: "/tmp", home: "/Users/lv" });
  assert.equal(state.renderCache.hits, 1);
  assert.equal(state.renderCache.misses, 1);
});

test("render cache misses when streaming content changes", () => {
  const state = createInitialState();
  reduceServerEvent(state, { type: "run/started", payload: {} });
  reduceServerEvent(state, { type: "ui/message", payload: { type: "assistant_stream", phase: "start" } });
  reduceServerEvent(state, { type: "ui/message", payload: { type: "assistant_stream", phase: "token", content: "A" } });

  renderScreen(state, 80, 12, { cwd: "/tmp", home: "/Users/lv" });
  const missesBeforeUpdate = state.renderCache.misses;
  reduceServerEvent(state, { type: "ui/message", payload: { type: "assistant_stream", phase: "token", content: "B" } });
  const plain = renderScreen(state, 80, 12, { cwd: "/tmp", home: "/Users/lv" }).map(stripAnsi).join("\n");

  assert.equal(state.renderCache.misses, missesBeforeUpdate + 1);
  assert(plain.includes("AB"));
});

test("render cache misses when live activity progress changes", () => {
  const state = createInitialState();
  const activity = {
    kind: "activity",
    id: "activity-1",
    status: "running",
    title: "准备 file_write",
    phase: "start",
    metrics: { argumentChars: 128, contentChars: 0, contentLines: 0, elapsedMs: 40 },
    details: ["路径: demo.html"],
  };
  state.messages.push(activity);

  renderScreen(state, 90, 14, { cwd: "/tmp", home: "/Users/lv" });
  activity.phase = "snapshot";
  activity.metrics = { argumentChars: 4096, contentChars: 12000, contentLines: 88, elapsedMs: 2400 };
  const plain = renderScreen(state, 90, 14, { cwd: "/tmp", home: "/Users/lv" }).map(stripAnsi).join("\n");

  assert.equal(state.renderCache.misses, 2);
  assert(plain.includes("生成中 ["));
  assert(plain.includes("88 lines"));
  assert(plain.includes("2.4s"));
});

test("run activity updates remain visible after an in-place phase transition", () => {
  const state = createInitialState();
  reduceServerEvent(state, {
    type: "run/started",
    request_id: "submit-cache-activity",
    payload: { task: "验证活动缓存" },
  });

  const before = renderScreen(state, 90, 18, { cwd: "/tmp", home: "/Users/lv" })
    .map(stripAnsi)
    .join("\n");
  reduceServerEvent(state, {
    type: "permission/request",
    request_id: "permission-cache-activity",
    payload: { tool_name: "bash_run", reason: "验证缓存刷新" },
  });
  const after = renderScreen(state, 90, 18, { cwd: "/tmp", home: "/Users/lv" })
    .map(stripAnsi)
    .join("\n");

  assert(before.includes("准备运行"));
  assert(after.includes("等待权限"));
  assert(after.includes("权限请求 1"));
});

test("render cache misses when tool prepare progress summary changes", () => {
  const state = createInitialState();
  const tool = {
    kind: "tool",
    id: "tool-1",
    callId: "call-1",
    name: "file_write",
    primary: "demo.html",
    status: "success",
    prepareTitle: "准备 file_write",
    preparePhase: "start",
    prepareMetrics: { argumentChars: 128, contentChars: 0, contentLines: 0, elapsedMs: 40 },
    prepareDetails: ["路径: demo.html"],
    output: "done",
  };
  state.messages.push(tool);

  renderScreen(state, 90, 14, { cwd: "/tmp", home: "/Users/lv" });
  tool.preparePhase = "snapshot";
  tool.prepareMetrics = { argumentChars: 4096, contentChars: 12000, contentLines: 88, elapsedMs: 2400 };
  const plain = renderScreen(state, 90, 14, { cwd: "/tmp", home: "/Users/lv" }).map(stripAnsi).join("\n");

  assert.equal(state.renderCache.misses, 2);
  assert(plain.includes("生成中 ["));
  assert(plain.includes("88 lines"));
  assert(plain.includes("2.4s"));
});

test("fold commands clear render cache before re-rendering expanded content", () => {
  const state = createInitialState();
  const send = () => {};
  const code = Array.from({ length: 45 }, (_, index) => `const value${index} = ${index};`).join("\n");
  state.messages.push({ kind: "assistant", id: "assistant-1", content: `\`\`\`js\n${code}\n\`\`\`` });

  renderScreen(state, 120, 16, { cwd: "/tmp", home: "/Users/lv" });
  assert(state.renderCache.entries.size > 0);

  handleSubmitText(state, "/expand 1", send);
  assert.equal(state.renderCache.entries.size, 0);

  const plain = renderScreen(state, 120, 80, { cwd: "/tmp", home: "/Users/lv" }).map(stripAnsi).join("\n");
  assert(plain.includes("value44"));
});

test("task timeline source collapse clears render cache before re-rendering", () => {
  const state = createInitialState();
  const send = () => {};
  state.messages.push({
    kind: "system",
    id: "tasks-1",
    title: "tasks",
    content: [
      "任务面板",
      "Timeline",
      "  - run_7 [needs_input] 浏览器时间线事件 | source=browser; records=/tmp/browser.zip",
    ].join("\n"),
  });
  state.taskPanel.messageId = "tasks-1";

  renderScreen(state, 100, 16, { cwd: "/tmp", home: "/Users/lv" });
  assert(state.renderCache.entries.size > 0);

  handleSubmitText(state, "/tasks timeline collapse browser", send);
  assert.equal(state.renderCache.entries.size, 0);

  const plain = renderScreen(state, 100, 16, { cwd: "/tmp", home: "/Users/lv" }).map(stripAnsi).join("\n");
  assert(!plain.includes("浏览器时间线事件"));
  assert(plain.includes("browser 1 folded"));
});

test("viewport rendering does not render every historical message near the bottom", () => {
  const state = createInitialState();
  state.messages = Array.from({ length: 1000 }, (_, index) => ({
    kind: "assistant",
    id: `assistant-${index}`,
    content: `历史消息 ${index}`,
  }));

  const lines = renderScreen(state, 80, 12, { cwd: "/tmp", home: "/Users/lv" }).map(stripAnsi);

  assert(lines.some((line) => line.includes("历史消息 999")));
  assert(!lines.some((line) => line.includes("历史消息 0")));
  assert(state.renderCache.misses < 20);
});

test("viewport rendering matches full body slicing when scrolled upward", () => {
  const state = createInitialState();
  state.scrollOffset = 4;
  state.messages = Array.from({ length: 30 }, (_, index) => ({
    kind: "assistant",
    id: `assistant-${index}`,
    content: `line ${index}`,
  }));
  const width = 72;
  const height = 14;
  const bodyHeight = height - renderFooter(state, width, { cwd: "/tmp", home: "/Users/lv" }).length;

  const full = renderBody(state, width);
  const expectedStart = Math.max(0, full.length - bodyHeight - state.scrollOffset);
  const expected = full.slice(expectedStart, expectedStart + bodyHeight).map(stripAnsi);
  const actual = renderBodyWindow(state, width, bodyHeight, state.scrollOffset).slice(-bodyHeight - state.scrollOffset, -state.scrollOffset || undefined).map(stripAnsi);

  assert.deepEqual(actual, expected);
});
