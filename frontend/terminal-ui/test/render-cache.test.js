import test from "node:test";
import assert from "node:assert/strict";
import { stripAnsi } from "../src/ansi.js";
import { renderScreen } from "../src/render.js";
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
