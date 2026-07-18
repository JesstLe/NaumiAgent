import assert from "node:assert/strict";
import test from "node:test";

import { createRedrawScheduler } from "../src/redraw-scheduler.js";

test("redraw scheduler restarts only the unpainted settle window", () => {
  const clock = createFakeTimers();
  const frames = [];
  const scheduler = createRedrawScheduler({
    onRedraw: () => frames.push("paint"),
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
    frameDelayMs: 16,
    initialSettleMs: 32,
  });

  assert.equal(scheduler.settleInitial(), true);
  assert.equal(clock.lastDelay, 32);
  const firstTimer = clock.lastId;
  assert.equal(scheduler.settleInitial(), true);
  assert.deepEqual(clock.cleared, [firstTimer]);
  assert.equal(clock.lastDelay, 32);
  clock.fire(clock.lastId);

  assert.deepEqual(frames, ["paint"]);
  assert.equal(scheduler.pending, false);
  assert.equal(scheduler.painted, false);
});

test("redraw scheduler coalesces repeated normal requests", () => {
  const clock = createFakeTimers();
  let redraws = 0;
  const scheduler = createRedrawScheduler({
    onRedraw: () => { redraws += 1; },
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });

  assert.equal(scheduler.schedule(), true);
  const timer = clock.lastId;
  assert.equal(scheduler.schedule(), false);
  assert.equal(clock.activeCount, 1);
  assert.equal(clock.lastId, timer);
  clock.fire(timer);

  assert.equal(redraws, 1);
  assert.equal(scheduler.pending, false);
});

test("redraw scheduler uses the frame delay after a successful paint", () => {
  const clock = createFakeTimers();
  const scheduler = createRedrawScheduler({
    onRedraw: () => {},
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
    frameDelayMs: 16,
    initialSettleMs: 32,
  });

  scheduler.markPainted();
  assert.equal(scheduler.painted, true);
  assert.equal(scheduler.schedule(), true);
  const timer = clock.lastId;
  assert.equal(clock.lastDelay, 16);
  assert.equal(scheduler.settleInitial(), false);
  assert.equal(clock.lastId, timer);
  assert.deepEqual(clock.cleared, []);
});

test("redraw scheduler cancellation is idempotent", () => {
  const clock = createFakeTimers();
  const scheduler = createRedrawScheduler({
    onRedraw: () => {},
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });

  scheduler.schedule();
  const timer = clock.lastId;
  assert.equal(scheduler.cancel(), true);
  assert.equal(scheduler.cancel(), false);
  assert.deepEqual(clock.cleared, [timer]);
  assert.equal(clock.activeCount, 0);
  assert.equal(scheduler.pending, false);
});

test("redraw scheduler flushes an armed frame immediately", () => {
  const clock = createFakeTimers();
  let redraws = 0;
  const scheduler = createRedrawScheduler({
    onRedraw: () => { redraws += 1; },
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });

  scheduler.schedule();
  const timer = clock.lastId;
  assert.equal(scheduler.flush(), true);

  assert.equal(redraws, 1);
  assert.deepEqual(clock.cleared, [timer]);
  assert.equal(clock.activeCount, 0);
  assert.equal(scheduler.pending, false);
});

test("redraw scheduler does not mark a failed callback as painted", () => {
  const clock = createFakeTimers();
  const scheduler = createRedrawScheduler({
    onRedraw: () => { throw new Error("render failed"); },
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });

  scheduler.schedule();
  assert.throws(() => clock.fire(clock.lastId), /render failed/);
  assert.equal(scheduler.pending, false);
  assert.equal(scheduler.painted, false);
  assert.equal(scheduler.schedule(), true);
  assert.equal(clock.lastDelay, 32);
});

test("redraw scheduler validates callbacks and normalizes delay edges", () => {
  assert.throws(() => createRedrawScheduler({}), /onRedraw/);
  assert.throws(
    () => createRedrawScheduler({ onRedraw: () => {}, setTimer: null }),
    /计时器/,
  );

  const clock = createFakeTimers();
  const scheduler = createRedrawScheduler({
    onRedraw: () => {},
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
    frameDelayMs: -10,
    initialSettleMs: Number.NaN,
  });

  scheduler.schedule();
  assert.equal(clock.lastDelay, 32);
  clock.fire(clock.lastId);
  scheduler.markPainted();
  scheduler.schedule();
  assert.equal(clock.lastDelay, 0);
});

function createFakeTimers() {
  let nextId = 1;
  let lastId = null;
  let lastDelay = null;
  const active = new Map();
  const cleared = [];

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
      cleared.push(id);
      active.delete(id);
    },
    fire(id) {
      const callback = active.get(id);
      if (!callback) throw new Error(`timer ${id} is not active`);
      active.delete(id);
      callback();
    },
    get lastId() {
      return lastId;
    },
    get lastDelay() {
      return lastDelay;
    },
    get activeCount() {
      return active.size;
    },
    cleared,
  };
}
