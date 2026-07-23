import test from "node:test";
import assert from "node:assert/strict";

import {
  assessIdleBridgeRecovery,
  bridgeRecoveryHandshakeTimeoutFromEnv,
  createBridgeRecoveryTracker,
} from "../src/bridge-recovery.js";
import {
  createInitialState,
  markBridgeReconnecting,
} from "../src/state.js";

test("idle Bridge recovery preserves the exact session identity", () => {
  const state = createInitialState();
  state.currentSessionId = "session-recovery-1";

  assert.deepEqual(assessIdleBridgeRecovery(state), {
    recoverable: true,
    reason: "idle",
    message: "Bridge 空闲，可安全重连。",
    sessionId: "session-recovery-1",
  });
});

test("Bridge recovery fails closed for active authority and uncertain delivery", () => {
  const cases = [
    [{ running: true }, "active_run"],
    [{ permission: { requestId: "permission-1" } }, "permission_pending"],
    [{ interaction: { requestId: "ask-1" } }, "interaction_pending"],
    [{ interactionQueue: [{ requestId: "ask-2" }] }, "interaction_pending"],
    [{
      messages: [{ kind: "user", deliveryStatus: "queued", requestId: "submit-1" }],
    }, "submission_unconfirmed"],
    [{
      harnessEvalBatch: {
        requestId: "harness-batch-1",
        batchId: "batch-1",
        cancelPending: false,
      },
      harnessEvalBatches: {
        "batch-1": { stage: "executing" },
      },
    }, "control_operation_pending"],
    [{
      workbench: { proposal_action: { phase: "loading" } },
    }, "control_operation_pending"],
  ];

  for (const [partial, reason] of cases) {
    const decision = assessIdleBridgeRecovery({
      ...createInitialState(),
      ...partial,
    });
    assert.equal(decision.recoverable, false);
    assert.equal(decision.reason, reason);
  }
});

test("Bridge recovery tracker is bounded and correlates only its resume", () => {
  const tracker = createBridgeRecoveryTracker({
    maxAttempts: 3,
    delaysMs: [0, 10, 20],
  });
  assert.deepEqual(tracker.begin("session-1"), {
    active: true,
    settling: false,
    incident: 1,
    sessionId: "session-1",
    attempt: 0,
    maxAttempts: 3,
    resumeRequestId: "",
  });
  assert.equal(tracker.nextAttempt().delayMs, 0);
  tracker.markResumeRequest("recover-1");
  assert.equal(tracker.matchesResume({ request_id: "other" }), false);
  assert.equal(tracker.matchesResume({ request_id: "recover-1" }), true);
  assert.equal(tracker.nextAttempt().delayMs, 10);
  assert.equal(tracker.nextAttempt().delayMs, 20);
  assert.equal(tracker.nextAttempt().exhausted, true);
  const completed = tracker.complete();
  assert.equal(completed.attempt, 3);
  assert.equal(tracker.snapshot().active, false);
  assert.equal(tracker.snapshot().settling, true);
  assert.equal(tracker.begin("session-1").incident, 1);
  assert.equal(tracker.nextAttempt().exhausted, true);
  tracker.abort();
  assert.equal(tracker.begin("session-2").incident, 2);
  tracker.complete();
  tracker.settle();
  assert.equal(tracker.begin("session-2").incident, 3);
});

test("Bridge reconnect state invalidates transport authority without clearing receipts", () => {
  const state = createInitialState();
  state.bridgeReady = true;
  state.protocolNegotiated = true;
  state.protocolNegotiation = { selected_version: 1 };
  state.status.protocol_registry = { compatibility: "attested_additive" };
  state.harnessReceipts["run-1"] = { run_id: "run-1", revision: 1 };
  state.inspector.snapshot = { revision: 2 };
  state.agents.snapshot = { revision: 3 };

  markBridgeReconnecting(state);

  assert.equal(state.bridgeReady, false);
  assert.equal(state.protocolNegotiated, false);
  assert.equal(state.protocolNegotiation, null);
  assert.equal(state.status.protocol_registry, undefined);
  assert.equal(state.bridgeHeartbeat.status, "starting");
  assert.equal(state.inspector.stale, true);
  assert.equal(state.agents.stale, true);
  assert.equal(state.harnessReceipts["run-1"].revision, 1);
});

test("Bridge recovery handshake timeout is bounded", () => {
  assert.equal(bridgeRecoveryHandshakeTimeoutFromEnv({}), 8_000);
  assert.equal(
    bridgeRecoveryHandshakeTimeoutFromEnv({ NAUMI_BRIDGE_RECOVERY_TIMEOUT_MS: "10" }),
    100,
  );
  assert.equal(
    bridgeRecoveryHandshakeTimeoutFromEnv({ NAUMI_BRIDGE_RECOVERY_TIMEOUT_MS: "999999" }),
    60_000,
  );
});
