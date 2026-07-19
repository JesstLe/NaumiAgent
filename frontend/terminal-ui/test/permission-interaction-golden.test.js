import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { INPUT_KEYS } from "../src/input-buffer.js";
import {
  createInitialState,
  handleInteractionKey,
  reduceServerEvent,
} from "../src/state.js";

const goldenPath = new URL(
  "../../../tests/fixtures/ui17/permission-interaction-golden.json",
  import.meta.url,
);
const golden = JSON.parse(readFileSync(goldenPath, "utf8"));

test("New UI permission cards preserve request and terminal choice semantics", () => {
  const fixture = golden.permission;
  for (const [choice, expectedStatus] of Object.entries(fixture.terminal_choices)) {
    const state = createInitialState();
    reduceServerEvent(state, {
      type: "permission/request",
      request_id: fixture.expected_request.request_id,
      payload: structuredClone(fixture.expected_request),
    });

    assert.deepEqual(state.permission.payload, fixture.expected_request);
    assert.equal(state.messages.at(-1).message.status, "needs_confirmation");
    reduceServerEvent(state, {
      type: "permission/resolved",
      payload: {
        request_id: fixture.expected_request.request_id,
        choice,
      },
    });

    assert.equal(state.permission, null);
    assert.equal(state.messages.at(-1).message.status, expectedStatus);
    assert.equal(state.messages.at(-1).message.choice, choice);
  }
});

test("New UI interaction choices and custom input match shared golden", () => {
  const fixture = golden.interaction;
  const optionState = createInitialState();
  const optionSent = [];
  reduceServerEvent(optionState, {
    type: "interaction/request",
    request_id: fixture.request_id,
    payload: structuredClone(fixture.expected_request),
  });
  handleInteractionKey(optionState, INPUT_KEYS.down, (type, payload) => {
    optionSent.push({ type, payload });
  });
  handleInteractionKey(optionState, "\r", (type, payload) => {
    optionSent.push({ type, payload });
  });
  assert.deepEqual(optionSent, [{
    type: "interaction_response",
    payload: { request_id: fixture.request_id, ...fixture.option_response.raw },
  }]);

  const customState = createInitialState();
  const customSent = [];
  reduceServerEvent(customState, {
    type: "interaction/request",
    request_id: fixture.request_id,
    payload: structuredClone(fixture.expected_request),
  });
  handleInteractionKey(customState, "3", () => {});
  handleInteractionKey(customState, "\r", () => {});
  assert.equal(customState.interaction.customMode, true);
  for (const character of fixture.custom_response.raw.custom_text) {
    handleInteractionKey(customState, character, () => {});
  }
  handleInteractionKey(customState, "\r", (type, payload) => {
    customSent.push({ type, payload });
  });
  assert.deepEqual(customSent, [{
    type: "interaction_response",
    payload: { request_id: fixture.request_id, ...fixture.custom_response.raw },
  }]);
});
