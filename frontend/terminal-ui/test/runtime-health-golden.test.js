import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { createInitialState, reduceServerEvent } from "../src/state.js";

const goldenPath = new URL(
  "../../../tests/fixtures/ui17/runtime-heartbeat-retention-golden.json",
  import.meta.url,
);
const golden = JSON.parse(readFileSync(goldenPath, "utf8"));

test("New UI preserves the bounded runtime-health golden payload", () => {
  assert.equal(golden.schema_version, 1);
  assert.ok(Array.isArray(golden.scenarios));
  assert.ok(golden.scenarios.length > 0);

  for (const scenario of golden.scenarios) {
    const state = createInitialState();
    reduceServerEvent(state, {
      type: "runtime/status",
      payload: {
        runtime_heartbeat_retention: structuredClone(scenario.expected),
      },
    });

    assert.deepEqual(
      state.status.runtime_heartbeat_retention,
      scenario.expected,
      scenario.id,
    );
  }
});
