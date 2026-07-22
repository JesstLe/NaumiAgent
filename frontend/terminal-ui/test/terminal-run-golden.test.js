import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  createInitialState,
  reduceServerEvent,
  requestRunCancel,
  submitUserMessage,
} from "../src/state.js";

const goldenPath = new URL(
  "../../../tests/fixtures/ui17/terminal-run-lifecycle-golden.json",
  import.meta.url,
);
const golden = JSON.parse(readFileSync(goldenPath, "utf8"));

test("New UI preserves shared submit, tool, and receipt lifecycle semantics", () => {
  const state = createInitialState();
  const sent = [];
  const userMessage = submitUserMessage(state, golden.submission.text, (type, payload, options) => {
    sent.push({ type, payload, requestId: options.id });
  });

  assert.deepEqual(sent, [{
    ...golden.submission.expected_client,
    requestId: golden.submission.request_id,
  }]);
  assert.equal(userMessage.deliveryStatus, golden.submission.expected_delivery_status);

  reduceServerEvent(state, {
    type: "run/started",
    request_id: golden.submission.request_id,
    payload: { run_id: golden.completion.data.run_id, task: golden.submission.text },
  });
  for (const scenario of golden.tool_lifecycle.events) {
    reduceServerEvent(state, {
      type: "ui/message",
      request_id: golden.submission.request_id,
      payload: structuredClone(scenario.expected_message),
    });
  }
  reduceServerEvent(state, {
    type: "completion/receipt",
    request_id: golden.submission.request_id,
    payload: structuredClone(golden.completion.expected_message.receipt),
  });
  reduceServerEvent(state, {
    type: "run/completed",
    request_id: golden.submission.request_id,
    payload: {
      run_id: golden.completion.data.run_id,
      receipt_id: golden.completion.data.receipt_id,
      status: golden.completion.data.outcome,
    },
  });

  assert.equal(state.running, false);
  assert.equal(userMessage.deliveryStatus, "accepted");
  assert.equal(state.tools.length, 1);
  assert.deepEqual(
    {
      callId: state.tools[0].callId,
      name: state.tools[0].name,
      status: state.tools[0].status,
      durationMs: state.tools[0].durationMs,
      output: state.tools[0].output,
    },
    {
      callId: "tool-golden-1",
      name: "bash_run",
      status: "success",
      durationMs: 12,
      output: "ok\n",
    },
  );
  const receipt = state.messages.find((message) => message.kind === "completion_receipt");
  assert.ok(receipt);
  assert.deepEqual(receipt.receipt, golden.completion.expected_message.receipt);
  assert.equal(state.messages.at(-1), receipt);
});

test("New UI Ctrl+C cancellation matches shared terminal semantics", () => {
  const state = createInitialState();
  submitUserMessage(state, golden.submission.text, () => {});
  reduceServerEvent(state, {
    type: "run/started",
    request_id: golden.submission.request_id,
    payload: { run_id: golden.completion.data.run_id, task: golden.submission.text },
  });

  const sent = [];
  assert.equal(requestRunCancel(state, (type, payload, options) => {
    sent.push({ type, payload, requestId: options.id });
  }), true);
  assert.deepEqual(sent, [{
    ...golden.cancel.expected_client,
    requestId: golden.cancel.request_id,
  }]);
  assert.equal(state.cancelPending, true);

  reduceServerEvent(state, {
    type: "run/cancelled",
    request_id: golden.submission.request_id,
    payload: {
      run_id: golden.completion.data.run_id,
      reason: golden.cancel.reason,
    },
  });

  assert.equal(state.running, false);
  assert.equal(state.cancelPending, false);
  assert.equal(state.activeRunActivity, null);
  const activity = state.messages.find((message) => message.kind === "run_activity");
  assert.equal(activity.status, golden.cancel.expected_terminal_status);
  assert.match(state.messages.at(-1).content, /运行已取消/);
});
