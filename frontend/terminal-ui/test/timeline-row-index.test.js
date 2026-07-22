import test from "node:test";
import assert from "node:assert/strict";

import { stripAnsi } from "../src/ansi.js";
import { clearRenderCache } from "../src/render-cache.js";
import { renderScreen } from "../src/render.js";
import { createInitialState, handleAssistantStream } from "../src/state.js";
import {
  ensureTimelineRowIndex,
  findTimelineSegmentAtRow,
  timelineRowIndexDebug,
} from "../src/timeline-row-index.js";

function historicalState(count = 300) {
  const state = createInitialState();
  state.welcome.dismissed = true;
  state.followTail = false;
  state.messages = Array.from({ length: count }, (_, index) => ({
    kind: index % 7 === 0 ? "user" : "assistant",
    id: `message-${index}`,
    content: `历史消息 ${index}\n第二行 ${index}`,
  }));
  return state;
}

test("deep timeline index preserves the legacy viewport exactly", () => {
  const state = historicalState();
  state.scrollOffset = 160;
  const env = { cwd: "/tmp", home: "/Users/lv", clockText: "12:34:56" };

  const indexed = renderScreen(state, 84, 18, env).map(stripAnsi);
  const legacy = renderScreen(state, 84, 18, {
    ...env,
    disableVirtualTimeline: true,
  }).map(stripAnsi);

  assert.deepEqual(indexed, legacy);
  const debug = timelineRowIndexDebug(state);
  assert.equal(debug.storesRenderedLines, false);
  assert(debug.lastRenderedRange.last - debug.lastRenderedRange.first < 10);
  assert.equal(debug.lastRenderedRange.overscanStart, debug.lastRenderedRange.first - 1);
  assert.equal(debug.lastRenderedRange.overscanEnd, debug.lastRenderedRange.last + 1);
});

test("deep timeline index reuses warm metadata and rebuilds on semantic invalidation", () => {
  const state = historicalState();
  state.scrollOffset = 180;
  const env = { cwd: "/tmp", home: "/Users/lv", clockText: "12:34:56" };

  renderScreen(state, 84, 18, env);
  const cold = timelineRowIndexDebug(state);
  renderScreen(state, 84, 18, env);
  const warm = timelineRowIndexDebug(state);
  assert.equal(warm.buildCount, cold.buildCount);
  assert(warm.reuseCount > cold.reuseCount);

  clearRenderCache(state.renderCache);
  renderScreen(state, 84, 18, env);
  const invalidated = timelineRowIndexDebug(state);
  assert.equal(invalidated.buildCount, warm.buildCount + 1);
  assert.equal(invalidated.generation, state.renderCache.generation);

  renderScreen(state, 72, 18, env);
  const resized = timelineRowIndexDebug(state);
  assert.equal(resized.buildCount, invalidated.buildCount + 1);
  assert.equal(resized.width, 72);
});

test("stream append and token growth update the deep index without a full rebuild", () => {
  const state = historicalState();
  state.scrollOffset = 160;
  const env = { cwd: "/tmp", home: "/Users/lv", clockText: "12:34:56" };
  renderScreen(state, 84, 18, env);
  const initial = timelineRowIndexDebug(state);

  handleAssistantStream(state, { phase: "start" });
  renderScreen(state, 84, 18, env);
  const appended = timelineRowIndexDebug(state);
  assert.equal(appended.buildCount, initial.buildCount);
  assert.equal(appended.appendCount, initial.appendCount + 1);

  handleAssistantStream(state, {
    phase: "token",
    content: "这是一段会跨越多行的流式内容。".repeat(20),
  });
  handleAssistantStream(state, { phase: "token", content: "第二个尚未渲染的批次" });
  const indexed = renderScreen(state, 84, 18, env).map(stripAnsi);
  const updated = timelineRowIndexDebug(state);
  const legacy = renderScreen(state, 84, 18, {
    ...env,
    disableVirtualTimeline: true,
  }).map(stripAnsi);
  assert.equal(updated.buildCount, appended.buildCount);
  assert.equal(updated.partialUpdateCount, appended.partialUpdateCount + 1);
  assert.deepEqual(indexed, legacy);
});

test("timeline row lookup handles empty and boundary rows", () => {
  const state = { messages: [], renderCache: { generation: 0 } };
  const empty = ensureTimelineRowIndex(state, { width: 80 }, () => []);
  assert.equal(findTimelineSegmentAtRow(empty, 0), -1);

  state.messages = [{ id: "zero" }, { id: "visible" }, { id: "last" }];
  const index = ensureTimelineRowIndex(
    state,
    { width: 80 },
    (message) => message.id === "zero" ? [] : [message.id, `${message.id}-2`],
  );
  assert.equal(findTimelineSegmentAtRow(index, 0), 1);
  assert.equal(findTimelineSegmentAtRow(index, 1), 1);
  assert.equal(findTimelineSegmentAtRow(index, 2), 2);
  assert.equal(findTimelineSegmentAtRow(index, 999), 2);
});

test("timeline row index rejects missing construction inputs", () => {
  assert.throws(
    () => ensureTimelineRowIndex(null, { width: 80 }, () => []),
    /缺少 state/,
  );
  assert.throws(
    () => ensureTimelineRowIndex({ messages: [] }, { width: 80 }),
    /缺少 renderMessage/,
  );
});
