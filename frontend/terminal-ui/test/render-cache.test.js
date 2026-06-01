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
  reduceServerEvent(state, { type: "ui/message", payload: { type: "assistant_stream", phase: "token", content: "B" } });
  const plain = renderScreen(state, 80, 12, { cwd: "/tmp", home: "/Users/lv" }).map(stripAnsi).join("\n");

  assert.equal(state.renderCache.misses, 2);
  assert(plain.includes("AB"));
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
