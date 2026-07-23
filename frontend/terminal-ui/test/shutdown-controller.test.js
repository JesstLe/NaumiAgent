import assert from "node:assert/strict";
import test from "node:test";

import {
  GRACEFUL_SHUTDOWN_TIMEOUT_MS,
  createGracefulShutdownController,
  shutdownSignalsForPlatform,
} from "../src/shutdown-controller.js";

function harness(overrides = {}) {
  const events = [];
  const cleanups = [];
  const exits = [];
  const timers = [];
  const controller = createGracefulShutdownController({
    sendShutdown: () => "shutdown-1",
    onRequest: (payload) => events.push(["request", payload]),
    onLifecycle: (event, payload) => events.push([event, payload]),
    cleanup: (payload) => cleanups.push(payload),
    exitProcess: (code) => exits.push(code),
    setTimer(callback, delay) {
      const timer = { callback, delay, cleared: false, unref() {} };
      timers.push(timer);
      return timer;
    },
    clearTimer(timer) {
      timer.cleared = true;
    },
    ...overrides,
  });
  return { controller, events, cleanups, exits, timers };
}

test("graceful shutdown waits for the matching Bridge acknowledgement", () => {
  let sends = 0;
  const value = harness({
    sendShutdown() {
      sends += 1;
      return "shutdown-exact";
    },
  });

  assert.equal(value.controller.request({ reason: "slash_q" }), true);
  assert.equal(value.controller.request({ reason: "duplicate" }), false);
  assert.equal(sends, 1);
  assert.deepEqual(value.cleanups, []);
  assert.deepEqual(value.exits, []);
  assert.equal(value.controller.snapshot().phase, "requested");
  assert.equal(value.controller.snapshot().requestId, "shutdown-exact");

  assert.equal(
    value.controller.acknowledge({ responseRequestId: "shutdown-exact" }),
    true,
  );
  assert.equal(value.controller.snapshot().phase, "finalized");
  assert.equal(value.timers[0].cleared, true);
  assert.equal(value.cleanups.length, 1);
  assert.deepEqual(value.exits, [0]);
  assert.equal(value.cleanups[0].outcome, "ack");
});

test("mismatched acknowledgement cannot end the wait and timeout is bounded", () => {
  const value = harness();

  value.controller.request({ reason: "signal:SIGTERM" });
  assert.equal(
    value.controller.acknowledge({ responseRequestId: "wrong-request" }),
    false,
  );
  assert.equal(
    value.controller.acknowledge({ responseRequestId: "" }),
    false,
  );
  assert.equal(value.controller.snapshot().phase, "requested");
  assert.deepEqual(value.exits, []);
  assert.equal(value.timers[0].delay, GRACEFUL_SHUTDOWN_TIMEOUT_MS);

  value.timers[0].callback();

  assert.equal(value.controller.snapshot().phase, "finalized");
  assert.equal(value.cleanups[0].outcome, "timeout");
  assert.deepEqual(value.exits, [0]);
  assert(value.events.some(([event]) => event === "ack_mismatch"));
});

test("a repeated signal forces one non-blocking cleanup", () => {
  let cleanupAttempts = 0;
  const value = harness({
    cleanup() {
      cleanupAttempts += 1;
      throw new Error("terminal restore failed");
    },
  });

  value.controller.request({ reason: "signal:SIGINT" });
  assert.equal(value.controller.force("repeated_signal", 0), true);
  assert.equal(value.controller.force("duplicate_force", 1), false);

  assert.equal(value.controller.snapshot().phase, "finalized");
  assert.equal(value.timers[0].cleared, true);
  assert.equal(cleanupAttempts, 1);
  assert.deepEqual(value.exits, [0]);
  assert(value.events.some(
    ([event, payload]) => event === "finalized"
      && payload.outcome === "repeated_signal",
  ));
});

test("timer setup failure cannot strand the terminal in requested state", () => {
  const value = harness({
    setTimer() {
      throw new Error("timer unavailable");
    },
  });

  assert.equal(value.controller.request({ reason: "slash_q" }), true);
  assert.equal(value.controller.snapshot().phase, "finalized");
  assert.equal(value.cleanups[0].outcome, "timer_failed");
  assert.deepEqual(value.exits, [0]);
  assert(value.events.some(([event]) => event === "timer_failed"));
});

test("Bridge exit completes an in-flight shutdown without waiting for timeout", () => {
  const value = harness();

  value.controller.request({ reason: "slash_q" });
  assert.equal(
    value.controller.bridgeExited({ code: 0, signal: null }),
    true,
  );

  assert.equal(value.cleanups[0].outcome, "bridge_exit");
  assert.deepEqual(value.exits, [0]);
  assert.equal(value.timers[0].cleared, true);
  assert.equal(value.controller.bridgeExited({ code: 0 }), false);
});

test("failed acknowledgement and abnormal Bridge exit preserve failure status", () => {
  const failedReceipt = harness();
  failedReceipt.controller.request({ reason: "slash_q" });
  assert.equal(
    failedReceipt.controller.acknowledge({
      responseRequestId: "shutdown-1",
      ok: false,
    }),
    true,
  );
  assert.equal(failedReceipt.cleanups[0].outcome, "ack_failed");
  assert.deepEqual(failedReceipt.exits, [1]);

  const failedExit = harness();
  failedExit.controller.request({ reason: "slash_q" });
  assert.equal(
    failedExit.controller.bridgeExited({ code: null, signal: "SIGTERM" }),
    true,
  );
  assert.equal(failedExit.cleanups[0].outcome, "bridge_exit_failed");
  assert.deepEqual(failedExit.exits, [1]);
});

test("remote shutdown and send failure both restore the terminal exactly once", () => {
  const remote = harness();
  assert.equal(
    remote.controller.acknowledge({ responseRequestId: "server-stop" }),
    true,
  );
  assert.equal(remote.cleanups[0].outcome, "remote_ack");
  assert.deepEqual(remote.exits, [0]);

  const failed = harness({
    sendShutdown() {
      throw new Error("pipe closed\nsecret detail");
    },
  });
  assert.equal(failed.controller.request({ reason: "slash_q" }), true);
  assert.equal(failed.cleanups[0].outcome, "send_failed");
  assert.deepEqual(failed.exits, [0]);
  assert.equal(failed.timers.length, 0);
});

test("platform signal sets include Windows console break and POSIX hangup", () => {
  assert.deepEqual(
    shutdownSignalsForPlatform("win32"),
    ["SIGINT", "SIGTERM", "SIGBREAK"],
  );
  assert.deepEqual(
    shutdownSignalsForPlatform("darwin"),
    ["SIGINT", "SIGTERM", "SIGHUP"],
  );
  assert.deepEqual(
    shutdownSignalsForPlatform("linux"),
    ["SIGINT", "SIGTERM", "SIGHUP"],
  );
});
