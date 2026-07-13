import test from "node:test";
import assert from "node:assert/strict";
import { createInitialState, reduceServerEvent } from "../src/state.js";
import {
  detachTimeline,
  jumpTimelineToLatest,
  markTimelineOutput,
  scrollTimeline,
  timelineOutputKey,
} from "../src/timeline-follow.js";

function assistantToken(sequence, content = "token") {
  return {
    type: "ui/message",
    seq: sequence,
    payload: { type: "assistant_stream", phase: "token", content },
  };
}

function toolEvent(sequence, phase, toolCallId) {
  return {
    type: "ui/message",
    seq: sequence,
    payload: { type: phase, tool_call_id: toolCallId, tool_name: "file_read" },
  };
}

test("following output stays pinned without unread items", () => {
  const state = createInitialState();
  state.scrollOffset = 9;

  assert.equal(markTimelineOutput(state, assistantToken(1), "assistant-1"), true);
  assert.equal(state.followTail, true);
  assert.equal(state.scrollOffset, 0);
  assert.equal(state.unreadOutputCount, 0);
  assert.deepEqual(state.unreadOutputKeys, {});
});

test("detached streaming tokens count one semantic assistant entry", () => {
  const state = createInitialState();
  detachTimeline(state, 12);

  markTimelineOutput(state, assistantToken(1), "assistant-1");
  markTimelineOutput(state, assistantToken(2), "assistant-1");
  markTimelineOutput(state, toolEvent(3, "tool_result", "call-1"), "call-1");

  assert.equal(state.followTail, false);
  assert.equal(state.scrollOffset, 12);
  assert.equal(state.unreadOutputCount, 2);
  assert.deepEqual(Object.keys(state.unreadOutputKeys).sort(), ["assistant:assistant-1", "tool:call-1"]);
});

test("tool lifecycle phases share one unread identity", () => {
  const state = createInitialState();
  detachTimeline(state, 4);

  for (const [sequence, phase] of [[1, "tool_prepare"], [2, "tool_use"], [3, "tool_result"]]) {
    markTimelineOutput(state, toolEvent(sequence, phase, "call-shared"), "call-shared");
  }

  assert.equal(state.unreadOutputCount, 1);
  assert.deepEqual(state.unreadOutputKeys, { "tool:call-shared": true });
});

test("tool preparation waits for a stable call identity", () => {
  const state = createInitialState();
  detachTimeline(state, 4);

  assert.equal(markTimelineOutput(state, toolEvent(1, "tool_prepare", ""), "activity-1"), false);
  assert.equal(state.unreadOutputCount, 0);
  assert.equal(markTimelineOutput(state, toolEvent(2, "tool_use", "call-stable"), "call-stable"), true);
  assert.equal(state.unreadOutputCount, 1);
});

test("footer-only and lifecycle records do not create unread output", () => {
  const state = createInitialState();
  detachTimeline(state, 3);
  const ignored = [
    { type: "runtime/status", seq: 1, payload: { model: "test" } },
    { type: "run/started", seq: 2, payload: {} },
    { type: "workbench/snapshot", seq: 3, payload: {} },
    { type: "permission/resolved", seq: 4, payload: { request_id: "permission-1" } },
    { type: "ui/message", seq: 5, payload: { type: "todo_status", total_count: 1 } },
    { type: "ui/message", seq: 6, payload: { type: "runtime_status", phase: "perf_phase" } },
  ];

  assert.deepEqual(ignored.map((record) => timelineOutputKey(record, "entry")), ignored.map(() => null));
  for (const record of ignored) {
    assert.equal(markTimelineOutput(state, record, "entry"), false);
  }
  assert.equal(state.unreadOutputCount, 0);
  assert.equal(state.scrollOffset, 3);
});

test("scrolling to the tail and explicit jump clear unread output", () => {
  const state = createInitialState();
  detachTimeline(state, 6);
  markTimelineOutput(state, assistantToken(1), "assistant-1");

  scrollTimeline(state, -3);
  assert.equal(state.followTail, false);
  assert.equal(state.scrollOffset, 3);
  assert.equal(state.unreadOutputCount, 1);

  scrollTimeline(state, -3);
  assert.equal(state.followTail, true);
  assert.equal(state.scrollOffset, 0);
  assert.equal(state.unreadOutputCount, 0);

  detachTimeline(state, 2);
  markTimelineOutput(state, assistantToken(2), "assistant-2");
  jumpTimelineToLatest(state);
  assert.equal(state.followTail, true);
  assert.deepEqual(state.unreadOutputKeys, {});
});

test("session replay resets detached timeline state", () => {
  const state = createInitialState();
  detachTimeline(state, 7);
  markTimelineOutput(state, assistantToken(1), "assistant-old");

  reduceServerEvent(state, {
    type: "session/replayed",
    payload: { session_id: "session-new", title: "新会话", clear: true },
  });

  assert.equal(state.followTail, true);
  assert.equal(state.scrollOffset, 0);
  assert.equal(state.unreadOutputCount, 0);
  assert.deepEqual(state.unreadOutputKeys, {});
});
