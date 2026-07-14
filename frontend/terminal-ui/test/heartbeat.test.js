import test from "node:test";
import assert from "node:assert/strict";
import { createHeartbeatController, heartbeatTimingFromEnv } from "../src/heartbeat.js";

function harness() {
  let now = 0;
  const timers = new Map();
  let nextTimerId = 1;
  const sent = [];
  const health = [];
  const debug = [];
  const controller = createHeartbeatController({
    sendPing: (id) => sent.push({ id, at: now }),
    onHealth: (value) => health.push(value),
    onDebug: (event, payload) => debug.push({ event, payload }),
    now: () => now,
    setTimer: (callback, delay) => {
      const id = nextTimerId++;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimer: (id) => timers.delete(id),
    intervalMs: 5_000,
    timeoutMs: 15_000,
  });
  return {
    controller,
    sent,
    health,
    debug,
    timers,
    advance(ms) {
      now += ms;
      for (const timer of timers.values()) timer.callback();
    },
  };
}

test("heartbeat is single-flight, detects stale transport, and recovers", () => {
  const h = harness();

  assert.equal(h.controller.start(), true);
  assert.equal(h.controller.start(), false);
  assert.deepEqual(h.sent, [{ id: "heartbeat-1", at: 0 }]);
  assert.equal(h.timers.size, 1);

  h.advance(5_000);
  h.advance(5_000);
  assert.equal(h.sent.length, 1);
  assert.equal(h.health.length, 0);

  h.advance(5_000);
  assert.equal(h.health.at(-1).status, "stale");
  assert.equal(h.health.at(-1).ageMs, 15_000);
  assert.deepEqual(h.sent.at(-1), { id: "heartbeat-2", at: 15_000 });

  h.advance(40);
  assert.equal(h.controller.receivePong("heartbeat-2"), true);
  assert.deepEqual(h.health.at(-1), { status: "healthy", rttMs: 40, ageMs: 0 });
  assert.equal(h.controller.receivePong("heartbeat-1"), false);
  assert.equal(h.health.length, 2);
});

test("heartbeat stop clears timers and ignores later pongs", () => {
  const h = harness();
  h.controller.start();

  assert.equal(h.controller.stop(), true);
  assert.equal(h.controller.stop(), false);
  assert.equal(h.timers.size, 0);
  assert.equal(h.controller.receivePong("heartbeat-1"), false);

  h.advance(30_000);
  assert.equal(h.sent.length, 1);
  assert(h.debug.some((entry) => entry.event === "heartbeat.stop"));
});

test("heartbeat validates timing configuration", () => {
  assert.throws(
    () => createHeartbeatController({
      sendPing() {},
      intervalMs: 0,
      timeoutMs: 10,
    }),
    /intervalMs/,
  );
  assert.throws(
    () => createHeartbeatController({
      sendPing() {},
      intervalMs: 10,
      timeoutMs: 9,
    }),
    /timeoutMs/,
  );
});

test("heartbeat environment timing falls back safely and keeps timeout above interval", () => {
  assert.deepEqual(heartbeatTimingFromEnv({}), {
    intervalMs: 5_000,
    timeoutMs: 15_000,
  });
  assert.deepEqual(heartbeatTimingFromEnv({
    NAUMI_HEARTBEAT_INTERVAL_MS: "20",
    NAUMI_HEARTBEAT_TIMEOUT_MS: "60",
  }), { intervalMs: 20, timeoutMs: 60 });
  assert.deepEqual(heartbeatTimingFromEnv({
    NAUMI_HEARTBEAT_INTERVAL_MS: "20000",
    NAUMI_HEARTBEAT_TIMEOUT_MS: "invalid",
  }), { intervalMs: 20_000, timeoutMs: 60_000 });
});
