import assert from "node:assert/strict";
import test from "node:test";

import {
  createProtocolEventBatcher,
  isCoalescibleStreamDelta,
  PROTOCOL_EVENT_BATCH_DEFAULTS,
} from "../src/protocol-event-batcher.js";

test("stream batcher concatenates exact consecutive assistant tokens", () => {
  const clock = createFakeTimers();
  const delivered = [];
  const batcher = createProtocolEventBatcher({
    onRecord: (record) => delivered.push(record),
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });

  batcher.push(delta("assistant_stream", "token", "你", 1, "run-1"));
  batcher.push(delta("assistant_stream", "token", "好", 2, "run-1"));

  assert.equal(batcher.pendingCount, 2);
  assert.equal(delivered.length, 0);
  assert.equal(clock.lastDelay, PROTOCOL_EVENT_BATCH_DEFAULTS.flushDelayMs);
  clock.fire(clock.lastId);
  assert.equal(delivered.length, 1);
  assert.equal(delivered[0].payload.content, "你好");
  assert.equal(delivered[0].seq, 2);
  assert.equal(batcher.pending, false);
});

test("one frame reduces a thousand-token burst once without content loss", () => {
  const delivered = [];
  const batcher = createProtocolEventBatcher({ onRecord: (record) => delivered.push(record) });

  for (let index = 0; index < 1_000; index += 1) {
    batcher.push(delta("assistant_stream", "token", String(index % 10), index + 1, "run-load"));
  }
  assert.equal(delivered.length, 0);
  assert.equal(batcher.pendingCount, 1_000);

  assert.equal(batcher.flush(), 1_000);
  assert.equal(delivered.length, 1);
  assert.equal(delivered[0].payload.content.length, 1_000);
  assert.equal(delivered[0].payload.content.slice(0, 20), "01234567890123456789");
  assert.equal(delivered[0].seq, 1_000);
});

test("control event is an ordering barrier and is never delayed", () => {
  const clock = createFakeTimers();
  const delivered = [];
  const batcher = createProtocolEventBatcher({
    onRecord: (record) => delivered.push(record),
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  const permission = {
    type: "permission/request",
    request_id: "perm-1",
    payload: { request_id: "perm-1" },
  };

  batcher.push(delta("assistant_stream", "token", "准备", 1, "run-1"));
  batcher.push(permission);

  assert.deepEqual(delivered.map((record) => record.type), [
    "ui/message",
    "permission/request",
  ]);
  assert.equal(delivered[0].payload.content, "准备");
  assert.equal(batcher.pending, false);
  assert.equal(clock.activeCount, 0);
});

test("start and end preserve stream order around a coalesced token burst", () => {
  const delivered = [];
  const batcher = createProtocolEventBatcher({ onRecord: (record) => delivered.push(record) });

  batcher.push(delta("assistant_stream", "start", "", 1, "run-1"));
  batcher.push(delta("assistant_stream", "token", "A", 2, "run-1"));
  batcher.push(delta("assistant_stream", "token", "B", 3, "run-1"));
  batcher.push(delta("assistant_stream", "end", "", 4, "run-1"));

  assert.deepEqual(
    delivered.map((record) => [record.payload.phase, record.payload.content]),
    [["start", ""], ["token", "AB"], ["end", ""]],
  );
});

test("thinking and assistant deltas or different requests never merge", () => {
  const delivered = [];
  const batcher = createProtocolEventBatcher({ onRecord: (record) => delivered.push(record) });

  batcher.push(delta("thinking", "delta", "思考", 1, "run-1"));
  batcher.push(delta("assistant_stream", "token", "回答", 2, "run-1"));
  batcher.push(delta("assistant_stream", "token", "另一个", 3, "run-2"));
  batcher.flush();

  assert.deepEqual(delivered.map((record) => record.payload.content), ["思考", "回答", "另一个"]);
});

test("content bound flushes before accepting the next delta", () => {
  const delivered = [];
  const batcher = createProtocolEventBatcher({
    onRecord: (record) => delivered.push(record),
    maxContentChars: 4,
  });

  batcher.push(delta("assistant_stream", "token", "1234", 1, "run-1"));
  batcher.push(delta("assistant_stream", "token", "5", 2, "run-1"));

  assert.equal(delivered[0].payload.content, "1234");
  assert.equal(batcher.pendingCount, 1);
  batcher.flush();
  assert.equal(delivered[1].payload.content, "5");
});

test("event bound prevents an empty-delta burst from becoming unbounded", () => {
  const delivered = [];
  const batcher = createProtocolEventBatcher({
    onRecord: (record) => delivered.push(record),
    maxEvents: 2,
  });

  batcher.push(delta("thinking", "delta", "", 1, "run-1"));
  batcher.push(delta("thinking", "delta", "", 2, "run-1"));
  batcher.push(delta("thinking", "delta", "", 3, "run-1"));

  assert.equal(delivered.length, 1);
  assert.equal(batcher.pendingCount, 1);
  batcher.flush();
  assert.equal(delivered.length, 2);
});

test("one oversized delta bypasses pending storage without splitting content", () => {
  const delivered = [];
  const batcher = createProtocolEventBatcher({
    onRecord: (record) => delivered.push(record),
    maxContentChars: 4,
  });

  assert.equal(
    batcher.push(delta("assistant_stream", "token", "12345", 1, "run-1")),
    false,
  );
  assert.equal(batcher.pending, false);
  assert.equal(delivered[0].payload.content, "12345");
});

test("cancel drops only pending deltas and validation rejects invalid adapters", () => {
  assert.throws(() => createProtocolEventBatcher({}), /onRecord/);
  assert.throws(
    () => createProtocolEventBatcher({ onRecord: () => {}, setTimer: null }),
    /计时器/,
  );
  const batcher = createProtocolEventBatcher({ onRecord: () => {} });
  batcher.push(delta("thinking", "delta", "private", 1, "run-1"));
  assert.equal(batcher.cancel(), 1);
  assert.equal(batcher.cancel(), 0);
  assert.equal(isCoalescibleStreamDelta({ type: "run/completed", payload: {} }), false);
});

function delta(type, phase, content, seq, requestId) {
  return {
    type: "ui/message",
    seq,
    request_id: requestId,
    payload: { type, phase, content },
  };
}

function createFakeTimers() {
  let nextId = 1;
  let lastId = null;
  let lastDelay = null;
  const active = new Map();

  return {
    setTimer(callback, delay) {
      const id = nextId;
      nextId += 1;
      lastId = id;
      lastDelay = delay;
      active.set(id, callback);
      return id;
    },
    clearTimer(id) {
      active.delete(id);
    },
    fire(id) {
      const callback = active.get(id);
      if (!callback) throw new Error(`timer ${id} is not active`);
      active.delete(id);
      callback();
    },
    get lastId() { return lastId; },
    get lastDelay() { return lastDelay; },
    get activeCount() { return active.size; },
  };
}
