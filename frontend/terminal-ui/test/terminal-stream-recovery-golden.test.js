import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { stripAnsi } from "../src/ansi.js";
import { Message } from "../src/components/message.js";
import { renderComponent } from "../src/components/core.js";
import {
  createInitialState,
  handleSubmitText,
  reduceServerEvent,
  retryUserMessage,
  submitUserMessage,
} from "../src/state.js";

const goldenPath = new URL(
  "../../../tests/fixtures/ui17/terminal-stream-recovery-golden.json",
  import.meta.url,
);
const golden = JSON.parse(readFileSync(goldenPath, "utf8"));

function startFixtureRun(state, scenario, send = () => {}) {
  const message = submitUserMessage(state, scenario.submission_text, send);
  assert.equal(message.requestId, scenario.request_id);
  reduceServerEvent(state, {
    type: "run/started",
    request_id: scenario.request_id,
    payload: { run_id: scenario.run_id, task: scenario.submission_text },
  });
  return message;
}

function applyEngineMessages(state, scenarios) {
  for (const scenario of scenarios) {
    reduceServerEvent(state, {
      type: "ui/message",
      payload: structuredClone(scenario.expected_message),
    });
  }
}

test("New UI merges the shared token stream into one completed response", () => {
  const state = createInitialState();
  startFixtureRun(state, golden.stream);
  applyEngineMessages(state, golden.stream.events);

  const assistants = state.messages.filter((message) => message.kind === "assistant");
  assert.equal(assistants.length, 1);
  assert.equal(assistants[0].content, golden.stream.expected_content);
  assert.equal(assistants[0].streamStatus, golden.stream.expected_stream_status);
  assert.equal(state.activeAssistant, null);
});

test("correlated error closes a partial stream while an unrelated error cannot", () => {
  const state = createInitialState();
  const userMessage = startFixtureRun(state, golden.interrupted);
  applyEngineMessages(state, golden.interrupted.partial_events);
  const active = state.activeAssistant;

  reduceServerEvent(state, {
    ...structuredClone(golden.interrupted.error_record),
    request_id: "submit-unrelated",
  });
  assert.equal(state.running, true);
  assert.equal(state.activeAssistant, active);

  reduceServerEvent(state, structuredClone(golden.interrupted.error_record));

  assert.equal(state.running, false);
  assert.equal(state.activeAssistant, null);
  assert.equal(userMessage.deliveryStatus, golden.interrupted.expected_delivery_status);
  assert.equal(active.content, golden.interrupted.expected_content);
  assert.equal(active.streamStatus, golden.interrupted.expected_stream_status);
  assert.equal(retryUserMessage(state, () => {}), null);

  const rendered = renderComponent(Message({ message: active }), { width: 80 })
    .map(stripAnsi)
    .join("\n");
  assert.match(rendered, /回复流已中断/);
  assert.match(rendered, /已经收到的部分/);
});

test("delivery retry reuses the failed bubble with a new request identity", () => {
  const scenario = golden.delivery_retry;
  const state = createInitialState();
  const sent = [];
  const send = (type, payload, options) => sent.push({ type, payload, requestId: options.id });
  const message = submitUserMessage(state, scenario.submission_text, send);
  const originalMessageId = message.id;

  assert.equal(message.requestId, scenario.first_request_id);
  reduceServerEvent(state, structuredClone(scenario.failure_record));
  assert.equal(message.deliveryStatus, scenario.expected_failed_status);

  handleSubmitText(state, scenario.retry_command, send);

  assert.equal(message.id, originalMessageId);
  assert.equal(message.requestId, scenario.retry_request_id);
  assert.equal(message.deliveryStatus, scenario.expected_retry_status);
  assert.equal(message.attempt, scenario.expected_attempt);
  assert.equal(state.messages.filter((item) => item.kind === "user").length, 1);
  assert.deepEqual(sent.map((item) => item.requestId), [
    scenario.first_request_id,
    scenario.retry_request_id,
  ]);
});
