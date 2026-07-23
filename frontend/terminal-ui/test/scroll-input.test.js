import assert from "node:assert/strict";
import test from "node:test";

import {
  TRACKPAD_SCROLL_IDLE_RESET_MS,
  TRACKPAD_SCROLL_INTERVAL_MS,
  createTrackpadScrollController,
} from "../src/scroll-input.js";

function fakeClock(start = 100) {
  let now = start;
  let nextTimer = 1;
  const timers = new Map();

  return {
    now: () => now,
    schedule(callback, delayMs) {
      const timer = nextTimer;
      nextTimer += 1;
      timers.set(timer, { callback, dueAt: now + delayMs });
      return timer;
    },
    cancel(timer) {
      timers.delete(timer);
    },
    advance(milliseconds) {
      now += milliseconds;
      const due = [...timers.entries()]
        .filter(([, timer]) => timer.dueAt <= now)
        .sort((left, right) => left[1].dueAt - right[1].dueAt);
      for (const [timerId, timer] of due) {
        if (!timers.delete(timerId)) continue;
        timer.callback();
      }
    },
    pendingCount: () => timers.size,
    setNow(value) {
      now = value;
    },
  };
}

test("trackpad controller emits one immediate line then paces a sustained burst", () => {
  const clock = fakeClock();
  const steps = [];
  const controller = createTrackpadScrollController({
    now: clock.now,
    schedule: clock.schedule,
    cancel: clock.cancel,
    onStep: (direction) => steps.push(direction),
  });

  assert.equal(controller.push("down"), true);
  for (let index = 0; index < 2; index += 1) {
    clock.advance(TRACKPAD_SCROLL_IDLE_RESET_MS / 2);
    assert.equal(controller.push("down"), true);
  }

  assert.deepEqual(steps, ["down"]);
  assert.equal(clock.pendingCount(), 1);
  clock.advance(
    TRACKPAD_SCROLL_INTERVAL_MS
      - TRACKPAD_SCROLL_IDLE_RESET_MS
      - 1,
  );
  assert.deepEqual(steps, ["down"]);
  clock.advance(1);
  assert.deepEqual(steps, ["down", "down"]);
  assert.equal(clock.pendingCount(), 0);
});

test("trackpad controller discards a deferred line after a short gesture", () => {
  const clock = fakeClock();
  const steps = [];
  const controller = createTrackpadScrollController({
    now: clock.now,
    schedule: clock.schedule,
    cancel: clock.cancel,
    onStep: (direction) => steps.push(direction),
  });

  controller.push("up");
  for (let index = 0; index < 1_000; index += 1) controller.push("up");
  clock.advance(TRACKPAD_SCROLL_INTERVAL_MS);
  clock.advance(TRACKPAD_SCROLL_INTERVAL_MS * 10);

  assert.deepEqual(steps, ["up"]);
  assert.equal(clock.pendingCount(), 0);
});

test("trackpad controller cancels stale momentum on direction reversal", () => {
  const clock = fakeClock();
  const steps = [];
  const controller = createTrackpadScrollController({
    now: clock.now,
    schedule: clock.schedule,
    cancel: clock.cancel,
    onStep: (direction) => steps.push(direction),
  });

  controller.push("down");
  controller.push("down");
  assert.equal(clock.pendingCount(), 1);
  assert.equal(controller.push("up"), true);

  assert.deepEqual(steps, ["down", "up"]);
  assert.equal(clock.pendingCount(), 0);
  clock.advance(TRACKPAD_SCROLL_INTERVAL_MS);
  assert.deepEqual(steps, ["down", "up"]);
});

test("trackpad controller rejects malformed time and disposes pending work", () => {
  const clock = fakeClock();
  const steps = [];
  const controller = createTrackpadScrollController({
    now: clock.now,
    schedule: clock.schedule,
    cancel: clock.cancel,
    onStep: (direction) => steps.push(direction),
  });

  assert.equal(controller.push("sideways"), false);
  assert.equal(controller.push("down"), true);
  assert.equal(controller.push("down"), true);
  assert.equal(clock.pendingCount(), 1);
  controller.dispose();
  assert.equal(clock.pendingCount(), 0);
  assert.equal(controller.push("up"), false);

  const invalidClock = fakeClock();
  invalidClock.setNow(Number.NaN);
  const invalid = createTrackpadScrollController({ now: invalidClock.now });
  assert.equal(invalid.push("down"), false);
  assert.deepEqual(steps, ["down"]);
});

test("trackpad controller cancels pending movement on clock rollback", () => {
  const clock = fakeClock();
  const steps = [];
  const controller = createTrackpadScrollController({
    now: clock.now,
    schedule: clock.schedule,
    cancel: clock.cancel,
    onStep: (direction) => steps.push(direction),
  });

  controller.push("down");
  controller.push("down");
  assert.equal(clock.pendingCount(), 1);
  clock.setNow(99);

  assert.equal(controller.push("down"), false);
  assert.equal(clock.pendingCount(), 0);
  clock.advance(TRACKPAD_SCROLL_INTERVAL_MS);
  assert.deepEqual(steps, ["down"]);
});
