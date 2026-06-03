import test from "node:test";
import assert from "node:assert/strict";
import { bridgeEnvironment, isIgnorableBridgeStderr } from "../src/bridge-stderr.js";

test("known LiteLLM optional AWS warnings are hidden from chat", () => {
  const text = "21:34:55 - LiteLLM:WARNING: common_utils.py:24 - litellm: could not pre-load sagemaker-runtime response stream shape — SageMaker event-stream decoding will be unavailable. Error: No module named 'botocore'";

  assert.equal(isIgnorableBridgeStderr(text), true);
  assert.equal(
    isIgnorableBridgeStderr("21:36:21 [INFO] LiteLLM:INFO: utils.py:4053 - LiteLLM completion() model= kimi-for-coding; provider = openai"),
    true,
  );
  assert.equal(isIgnorableBridgeStderr("21:36:31 [INFO] LiteLLM completion() model= kimi-for-coding; provider = openai"), true);
  assert.equal(isIgnorableBridgeStderr("21:36:21 [INFO] naumi_agent.orchestrator.planner: Task intent: 闲聊, complexity: simple"), true);
});

test("unexpected bridge stderr remains visible", () => {
  assert.equal(isIgnorableBridgeStderr("Traceback (most recent call last): boom"), false);
});

test("bridgeEnvironment defaults LiteLLM logging to error", () => {
  assert.deepEqual(bridgeEnvironment({ PATH: "/bin" }), {
    PATH: "/bin",
    LITELLM_LOG: "ERROR",
    LITELLM_LOG_LEVEL: "ERROR",
  });

  assert.equal(bridgeEnvironment({ LITELLM_LOG: "DEBUG" }).LITELLM_LOG, "DEBUG");
});
