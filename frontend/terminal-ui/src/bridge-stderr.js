const IGNORABLE_BRIDGE_STDERR_PATTERNS = [
  /LiteLLM:WARNING:.*could not pre-load sagemaker-runtime response stream shape/i,
  /SageMaker event-stream decoding will be unavailable/i,
  /No module named ['"]botocore['"]/i,
  /\bLiteLLM:INFO:\s*utils\.py:\d+\s*-/i,
  /LiteLLM completion\(\)\s*model=/i,
  /^\d{2}:\d{2}:\d{2}\s+\[INFO\]\s+naumi_agent\./i,
  /^\d{2}:\d{2}:\d{2}\s+\[INFO\]\s+LiteLLM:/i,
  /^LiteLLM:INFO:\s*utils\.py:\d+\s*-/i,
];

export function isIgnorableBridgeStderr(text) {
  const trimmed = String(text ?? "").trim();
  if (!trimmed) return true;
  return IGNORABLE_BRIDGE_STDERR_PATTERNS.some((pattern) => pattern.test(trimmed));
}

export function bridgeEnvironment(baseEnv = process.env) {
  return {
    ...baseEnv,
    LITELLM_LOG: baseEnv.LITELLM_LOG ?? "ERROR",
    LITELLM_LOG_LEVEL: baseEnv.LITELLM_LOG_LEVEL ?? "ERROR",
    PYTHONUTF8: baseEnv.PYTHONUTF8 ?? "1",
    PYTHONIOENCODING: baseEnv.PYTHONIOENCODING ?? "utf-8",
  };
}
