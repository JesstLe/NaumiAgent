import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { createHash } from "node:crypto";
import EVALUATION_LANE_GOLDEN from "../../../tests/fixtures/ui17/evaluation-lane-receipt-golden.json" with { type: "json" };
import {
  attachJsonlLineReader,
  createHelloPayload,
  createEventSender,
  createServerSequenceGuard,
  eventPolicy,
  normalizeBudgetStatus,
  normalizeServerRecord,
  parseArgs,
  parseBridgeCommandJson,
  PROTOCOL_COMPATIBLE_REGISTRY_SHA256,
  PROTOCOL_CONTRACT,
  PROTOCOL_REGISTRY_SHA256,
  PROTOCOL_VERSION,
  requiredEventCapability,
  splitShellLike,
  validateCompatibility,
  validateEventRegistry,
  validateEventCapabilities,
} from "../src/protocol.js";

function harnessExplainPayload(revision = 1) {
  return {
    schema_version: 1,
    revision,
    run_id: "detail-run",
    lookup_status: "ok",
    message: "",
    private_payload: "must-drop",
    explanation: {
      status: "completed_unverified",
      objective: "验证 Explain",
      started_at: "2026-07-15T10:00:00+00:00",
      completed_at: "2026-07-15T10:01:00+00:00",
      verified: false,
      running: false,
      summary: "发现验证问题",
      criteria: Array.from({ length: 105 }, (_, index) => ({
        id: `criterion-${index}`,
        description: "定向验证通过",
        status: "unsatisfied",
        evidence_ids: Array.from({ length: 105 }, (__, id) => `evidence-${id}`),
        private_payload: "must-drop",
      })),
      failure_classes: Array.from({ length: 25 }, () => "verification_failure"),
      findings: Array.from({ length: 25 }, (_, index) => ({
        failure_class: "verification_failure",
        source: `check:${index}`,
        message: "失败",
        next_step: "重新运行",
        check_ids: Array.from({ length: 55 }, (__, id) => `check-${id}`),
        evidence_ids: Array.from({ length: 105 }, (__, id) => `evidence-${id}`),
        private_payload: "must-drop",
      })),
      checks: Array.from({ length: 55 }, (_, index) => ({
        id: `check-${index}`,
        status: "failed",
        duration_ms: index,
      })),
      evidence: Array.from({ length: 105 }, (_, index) => ({
        id: `evidence-${index}`,
        kind: "test_report",
        status: "missing",
        digest_prefix: "a".repeat(16),
        uri: `artifact://${index}`,
      })),
    },
  };
}

function harnessReplayPayload(revision = 1) {
  return {
    schema_version: 1,
    revision,
    run_id: "detail-run",
    lookup_status: "ok",
    message: "",
    result: {
      status: "partial",
      baseline_manifest_sha256: "a".repeat(64),
      current_manifest_sha256: "b".repeat(64),
      baseline_rule_version: "1",
      current_rule_version: "1",
      baseline_explanation_sha256: "c".repeat(64),
      current_explanation_sha256: "d".repeat(64),
      timeline: Array.from({ length: 205 }, (_, index) => ({
        kind: "check",
        id: `timeline-${index}`,
        timestamp: "2026-07-15T10:00:00+00:00",
        status: "passed",
      })),
      artifacts: Array.from({ length: 105 }, (_, index) => ({
        id: `artifact-${index}`,
        kind: "test_report",
        reference: `artifact://${index}`,
        status: "verified",
        expected_sha256: "e".repeat(64),
        actual_sha256: "e".repeat(64),
        private_payload: "must-drop",
      })),
      anomalies: Array.from({ length: 55 }, (_, index) => `anomaly-${index}`),
      differences: Array.from({ length: 55 }, (_, index) => ({
        field: `field-${index}`,
        baseline: "before",
        current: "after",
      })),
      legacy_baseline_created: true,
    },
  };
}

function harnessEvalBaselinePayload() {
  return {
    schema_version: 1,
    snapshot_sha256: "f".repeat(64),
    status: "ok",
    suite_id: "surface-protocol",
    message: "",
    active: {
      id: "a".repeat(64),
      version: 2,
      batch_id: "baseline-2",
      sample_count: 5,
      identity_sha256: "b".repeat(64),
      samples_sha256: "c".repeat(64),
      promoted_by: "user",
      promotion_reason: "真实验证完成",
      created_at: "2026-07-18T10:00:00+00:00",
      private_payload: "must-drop",
    },
    comparisons: [{
      id: "d".repeat(64),
      baseline_id: "a".repeat(64),
      current_batch_id: "candidate-2",
      decision: "passed",
      statistical_verdict: "unchanged",
      current_samples: 5,
      created_at: "2026-07-18T10:01:00+00:00",
      private_payload: "must-drop",
    }],
  };
}

function evaluationLanePayload() {
  return structuredClone(EVALUATION_LANE_GOLDEN.typed_payload);
}

function harnessEvalBatchPayload(stage = "evaluating") {
  const terminal = ["completed", "partial", "error"].includes(stage);
  return {
    schema_version: 1,
    stage,
    terminal,
    batch_id: "candidate-1",
    suite_id: "surface-protocol",
    requested: 5,
    completed: stage === "completed" ? 5 : 2,
    persisted: stage === "completed" ? 5 : 0,
    passed_cases: 4,
    implementation_failures: 0,
    evaluation_errors: 0,
    skipped: 0,
    duration_ms: 12.5,
    baseline_eligible: stage === "completed",
    identity_sha256: stage === "completed" ? "a".repeat(64) : "",
    code: "",
    message: "",
    private_payload: "must-drop",
  };
}

function harnessLiveEvalPayload(stage = "evaluating") {
  const completed = stage === "completed" ? 5 : 2;
  return {
    schema_version: 1,
    kind: "live",
    stage,
    terminal: ["completed", "partial", "error"].includes(stage),
    request_id: `hlivebatch_${"a".repeat(24)}`,
    request_sha256: "b".repeat(64),
    batch_id: "live-batch-1",
    suite_id: "live-transport-core",
    model: "provider/model",
    provider_model: "model-20260805",
    requested: 5,
    completed,
    persisted: stage === "completed" ? 5 : 0,
    total_calls: completed,
    total_tokens: completed * 28,
    total_cost_usd: completed * 0.001,
    cost_source: "rate_card_estimate",
    rate_card_source: "catalog",
    billing_status: "unsupported",
    provider_response_ids_observed: completed,
    duration_ms: 12.5,
    max_total_duration_seconds: 30,
    max_total_cost_usd: 0.1,
    actual_cost_exceeded: false,
    baseline_eligible: stage === "completed",
    identity_sha256: stage === "completed" ? "c".repeat(64) : "",
    code: "",
    message: "",
    private_payload: "must-drop",
  };
}

function harnessSandboxEvalPayload(stage = "executing") {
  const persisted = stage === "completed" ? 5 : 2;
  return {
    schema_version: 1,
    kind: "sandbox",
    stage,
    terminal: ["completed", "failed", "cancelled", "expired"].includes(stage),
    batch_id: "sandbox-1",
    check_ids: ["unit", "lint"],
    requested: 5,
    persisted,
    checkpoint_id: `hsbatch_${"a".repeat(24)}`,
    checkpoint_sha256: "b".repeat(64),
    authority_key: "c".repeat(64),
    lane: "sandbox",
    run_id: "manual:session-1",
    run_grant_sha256: "d".repeat(64),
    sample_result_sha256: Array.from(
      { length: persisted },
      (_, index) => String(index + 1).repeat(64),
    ),
    code: ["failed", "cancelled", "expired"].includes(stage)
      ? `sandbox_batch_${stage}`
      : "",
    updated_at: "2026-07-23T10:00:00+08:00",
  };
}

function harnessEvalPromotionPayload(stage = "promoted") {
  const terminal = !["awaiting_reason", "awaiting_confirmation"].includes(stage);
  const successful = ["promoted", "already_active"].includes(stage);
  return {
    schema_version: 1,
    stage,
    terminal,
    suite_id: "surface-protocol",
    batch_id: "candidate-1",
    code: "",
    message: "",
    baseline_id: successful ? "a".repeat(64) : "",
    active_baseline_id: successful ? "a".repeat(64) : "",
    previous_baseline_id: "",
    version: successful ? 1 : 0,
    sample_count: successful ? 5 : 0,
    promoted_by: successful ? "user" : "",
    promotion_reason: stage === "awaiting_confirmation" || successful ? "完整回归已通过" : "",
    created_at: successful ? "2026-07-18T10:00:00+00:00" : "",
    private_payload: "must-drop",
  };
}

function sandboxRetryRecoveryPayload({ status = "ready" } = {}) {
  const items = status === "ready"
    ? [{
      dispatch_id: `hsard_${"1".repeat(24)}`,
      retry_action_id: `hsar_${"2".repeat(24)}`,
      retry_receipt_id: `hsarr_${"3".repeat(24)}`,
      retry_receipt_sha256: "4".repeat(64),
      batch_id: "batch-restart",
      suite_id: "startup-recovery",
      requested_samples: 5,
      persisted_samples: 2,
      recovery_status: "pending",
      dispatch_state: "pending",
      dispatch_epoch: 0,
      ticket_id: "",
      ticket_epoch: 0,
      ticket_state: "",
      ticket_lease_expires_at: "",
      updated_at: "2026-07-24T00:00:00+00:00",
      can_resume: true,
      resume_command: `/harness eval sandbox resume hsar_${"2".repeat(24)}`
        + ` --dispatch hsard_${"1".repeat(24)}`
        + ` --receipt hsarr_${"3".repeat(24)}`
        + ` --sha256 ${"4".repeat(64)}`,
    }]
    : [];
  const counts = {
    pending: items.length,
    live: 0,
    recovery_required: 0,
    reconcile_required: 0,
    clock_regression: 0,
    actionable: items.length,
  };
  const body = {
    schema_version: 1,
    status,
    workspace_sha256: "a".repeat(64),
    assessed_at: "2026-07-24T00:00:01+00:00",
    limit: 20,
    total: items.length,
    truncated: false,
    counts,
    items,
    error_code: status === "ready" ? "" : "startup_scan_failed",
  };
  const digest = createHash("sha256")
    .update(canonicalTestJson(body), "utf8")
    .digest("hex");
  return {
    schema_version: 1,
    snapshot_id: `hsrrs_${digest.slice(0, 24)}`,
    snapshot_sha256: digest,
    status: body.status,
    workspace_sha256: body.workspace_sha256,
    assessed_at: body.assessed_at,
    limit: body.limit,
    total: body.total,
    truncated: body.truncated,
    counts: body.counts,
    items: body.items,
    error_code: body.error_code,
  };
}

function canonicalTestJson(value) {
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalTestJson(item)).join(",")}]`;
  }
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map(
      (key) => `${JSON.stringify(key)}:${canonicalTestJson(value[key])}`,
    ).join(",")}}`;
  }
  return JSON.stringify(value);
}

function doctorHealthPayload() {
  return {
    schema_version: 1,
    status: "degraded",
    generated_at: "2026-07-18T10:00:00+00:00",
    live_probe: false,
    snapshot_sha256: "a".repeat(64),
    items: [{
      id: "provider-1",
      domain: "provider",
      label: "API key",
      severity: "error",
      responsibility: "user_config",
      detail: "未检测到凭据",
      suggestion: "运行 naumi configure。",
      diagnostic_code: "provider_credentials_missing",
      private_payload: "must-drop",
    }],
    private_payload: "must-drop",
  };
}

function doctorTracePayload() {
  return {
    schema_version: 1,
    status: "ready",
    diagnostic_code: "trace_index_ready",
    run_id: "run-1",
    interface: "terminal-ui-bridge",
    assessed_at: "2026-08-05T01:00:00+00:00",
    query: "error",
    limit: 80,
    source_size_bytes: 1024,
    window_size_bytes: 1024,
    window_event_count: 2,
    matched_event_count: 1,
    malformed_line_count: 0,
    truncated: false,
    entries: [{
      cursor: 12,
      timestamp: "2026-08-05T01:00:00+00:00",
      event_type: "exception",
      severity: "error",
      summary: "异常元数据 · RuntimeError（正文已折叠）",
      identifiers: { call_id: "call-1" },
      private_body: "must-drop",
    }],
    snapshot_sha256: "f".repeat(64),
    privacy_notice: "正文、模型输出、reasoning、参数与 traceback 默认折叠。",
    private_payload: "must-drop",
  };
}

function doctorExportPayload(status = "preview") {
  const payload = {
    schema_version: 1,
    status,
    bundle_format: "zip",
    source_snapshot_sha256: "a".repeat(64),
    manifest_sha256: "b".repeat(64),
    bundle_sha256: "c".repeat(64),
    total_bytes: 2048,
    files: [
      {
        path: "health.json",
        sha256: "d".repeat(64),
        size_bytes: 800,
        description: "脱敏 Health",
      },
      {
        path: "README.txt",
        sha256: "e".repeat(64),
        size_bytes: 224,
        description: "说明",
      },
      {
        path: "manifest.json",
        sha256: "b".repeat(64),
        size_bytes: 640,
        description: "清单",
      },
    ],
    privacy_notice: "不包含聊天、reasoning、原始 trace、凭据或源码。",
    private_payload: "must-drop",
  };
  if (status === "written") {
    payload.receipt = {
      schema_version: 1,
      output_path: "/state/diagnostics/report.zip",
      bundle_sha256: payload.bundle_sha256,
      source_snapshot_sha256: payload.source_snapshot_sha256,
      size_bytes: payload.total_bytes,
      reused_existing: false,
      private_payload: "must-drop",
    };
  }
  return payload;
}

function doctorProbePayload(status = "passed") {
  return {
    schema_version: 1,
    status,
    diagnostic_code: status === "passed" ? "" : "provider_timeout",
    message: status === "passed" ? "连接成功：test-model" : "连接超时",
    suggestion: status === "passed" ? "" : "检查网络。",
    request_count: status === "blocked" ? 0 : 1,
    request_limit: 1,
    max_output_tokens: 8,
    duration_ms: 25,
    timeout_ms: 15_000,
    snapshot_sha256: status === "cancelled" ? "" : "f".repeat(64),
    private_payload: "must-drop",
  };
}

test("normalizes nullable budget without inventing zero", () => {
  assert.deepEqual(
    normalizeBudgetStatus({
      enabled: false,
      used_usd: 0.0123,
      max_usd: null,
      remaining_usd: null,
      percentage: null,
      input_tokens: 42,
      max_input_tokens: null,
      output_tokens: 8,
      max_output_tokens: null,
    }),
    {
      enabled: false,
      used_usd: 0.0123,
      max_usd: null,
      remaining_usd: null,
      cost_percentage: null,
      input_tokens: 42,
      max_input_tokens: null,
      input_percentage: null,
      output_tokens: 8,
      max_output_tokens: null,
      output_percentage: null,
      percentage: null,
    },
  );
});

test("nullable budget rejects coercible and non-finite limits", () => {
  for (const max_usd of ["5", {}, -1, Number.POSITIVE_INFINITY]) {
    assert.throws(
      () => normalizeBudgetStatus({ enabled: true, max_usd }),
      /max_usd/,
    );
  }
});

test("parseArgs supports config and bridge command", () => {
  assert.deepEqual(parseArgs([
    "--config",
    "local.yaml",
    "--bridge-command",
    "node fake.js",
    "--bridge-command-json",
    "[\"node\",\"fake.js\"]",
  ]), {
    config: "local.yaml",
    bridgeCommand: "node fake.js",
    bridgeCommandJson: "[\"node\",\"fake.js\"]",
    selfTest: false,
  });
});

test("parseArgs defaults to the project Naumi config", () => {
  assert.equal(parseArgs([]).config, ".naumi/config.yaml");
  assert.equal(parseArgs(["--self-test"]).selfTest, true);
});

test("parseBridgeCommandJson decodes argv without shell splitting", () => {
  assert.deepEqual(
    parseBridgeCommandJson("[\"/path with spaces/python\",\"-m\",\"naumi_agent.ui.bridge\"]"),
    ["/path with spaces/python", "-m", "naumi_agent.ui.bridge"],
  );

  assert.throws(
    () => parseBridgeCommandJson("[\"python\",42]"),
    /必须是非空字符串数组/,
  );
});

test("splitShellLike keeps quoted arguments together", () => {
  assert.deepEqual(splitShellLike('node "fake bridge.js" --flag'), ["node", "fake bridge.js", "--flag"]);
});

test("event sender writes versioned JSONL records", () => {
  const chunks = [];
  const writable = { write: (chunk) => chunks.push(chunk) };
  const send = createEventSender(writable);

  const id = send("submit", { text: "hi" });

  assert.equal(id, "ui-1");
  assert.deepEqual(JSON.parse(chunks[0]), {
    id: "ui-1",
    type: "submit",
    version: 1,
    payload: { text: "hi" },
  });
});

test("event sender accepts a caller supplied request id", () => {
  const chunks = [];
  const send = createEventSender({ write: (chunk) => chunks.push(chunk) });

  assert.equal(
    send("submit", { text: "修复测试" }, { id: "submit-local-1" }),
    "submit-local-1",
  );
  assert.equal(JSON.parse(chunks[0]).id, "submit-local-1");
  assert.equal(send("ping", {}), "ui-1");
});

test("event sender publishes an explicit queue promotion target", () => {
  const chunks = [];
  const send = createEventSender({ write: (chunk) => chunks.push(chunk) });

  send("queue_promote", { target_request_id: "submit-later" });

  assert.deepEqual(JSON.parse(chunks[0]).payload, {
    target_request_id: "submit-later",
  });
});

test("queue cancellation uses an explicit target and normalizes its receipt", () => {
  const chunks = [];
  const send = createEventSender({ write: (chunk) => chunks.push(chunk) });

  send("queue_cancel", { target_request_id: "submit-cancel" });
  const normalized = normalizeServerRecord({
    type: "run/queue_cancelled",
    payload: {
      target_request_id: 42,
      queued: "1",
      reason: "用户取消",
    },
  });

  assert.equal(JSON.parse(chunks[0]).payload.target_request_id, "submit-cancel");
  assert.deepEqual(normalized.payload, {
    target_request_id: "42",
    queued: 1,
    reason: "用户取消",
  });
});

test("interaction cancellation uses a strict target and terminal receipt", () => {
  const chunks = [];
  const send = createEventSender({ write: (chunk) => chunks.push(chunk) });

  send("interaction_cancel", { interaction_id: "ask-goal-1" });
  const resolved = normalizeServerRecord({
    type: "interaction/resolved",
    payload: {
      request_id: "ask-goal-1",
      status: "cancelled",
      reason: "用户已取消。",
    },
  });

  assert.equal(JSON.parse(chunks[0]).payload.interaction_id, "ask-goal-1");
  assert.equal(resolved.payload.status, "cancelled");
  assert.throws(
    () => normalizeServerRecord({
      type: "interaction/resolved",
      payload: {
        request_id: "ask-goal-1",
        status: "cancelled",
        reason: "用户已取消。",
        value: "private",
      },
    }),
    /不能携带答案字段/,
  );
});

test("interaction takeover sends one strict host-binding target", () => {
  const chunks = [];
  const send = createEventSender({ write: (chunk) => chunks.push(chunk) });

  send("interaction_takeover", { interaction_id: "ask-goal-takeover" });

  assert.deepEqual(JSON.parse(chunks[0]).payload, {
    interaction_id: "ask-goal-takeover",
  });
});

test("queue promotion receipt normalizes boundary metadata", () => {
  const normalized = normalizeServerRecord({
    type: "run/queue_promoted",
    payload: {
      target_request_id: 42,
      position: "1",
      queued: "3",
      boundary: "after_current_run",
      message: "已提升",
    },
  });

  assert.deepEqual(normalized.payload, {
    target_request_id: "42",
    position: 1,
    queued: 3,
    boundary: "after_current_run",
    message: "已提升",
  });
});

test("interaction sender rejects ambiguous answers and drops private fields", () => {
  const chunks = [];
  const send = createEventSender({ write: (chunk) => chunks.push(chunk) });

  send("interaction_response", {
    request_id: "ask-option-1",
    kind: "option",
    value: "safe",
    private_payload: "drop",
  });
  assert.deepEqual(JSON.parse(chunks[0]).payload, {
    request_id: "ask-option-1",
    kind: "option",
    value: "safe",
    custom_text: "",
  });
  assert.throws(
    () => send("interaction_response", {
      request_id: "ask-option-1",
      kind: "option",
      value: "safe",
      custom_text: "ambiguous",
    }),
    /字段组合无效/,
  );
  assert.equal(chunks.length, 1);
});

test("protocol contract drives client and server event validation", () => {
  assert.equal(PROTOCOL_VERSION, PROTOCOL_CONTRACT.version);
  assert.deepEqual(PROTOCOL_CONTRACT.negotiation, {
    minimum_version: 1,
    maximum_version: 1,
    capabilities: [
      "agent_recovery_actions",
      "doctor_export",
      "doctor_live_probe",
      "doctor_trace_index",
      "evolution_evaluation_lane",
      "goal_snapshot",
      "heartbeat",
      "pursuit_recovery_actions",
      "sequence_integrity",
      "session_list",
      "task_snapshot",
      "terminal_event_cursor",
      "terminal_event_recovery",
      "typed_ui_messages",
      "workbench_proposal_actions",
      "workbench_snapshot",
    ],
    required_capabilities: ["typed_ui_messages"],
  });
  assert(PROTOCOL_CONTRACT.client_events.includes("submit"));
  assert(PROTOCOL_CONTRACT.client_events.includes("task_panel"));
  assert(PROTOCOL_CONTRACT.client_events.includes("run_cancel"));
  assert(PROTOCOL_CONTRACT.client_events.includes("receipt/request"));
  assert(PROTOCOL_CONTRACT.client_events.includes("harness/explain/request"));
  assert(PROTOCOL_CONTRACT.client_events.includes("harness/replay/request"));
  assert(PROTOCOL_CONTRACT.client_events.includes("harness/eval-baseline/request"));
  assert(PROTOCOL_CONTRACT.client_events.includes("harness/eval-batch/request"));
  assert(PROTOCOL_CONTRACT.client_events.includes("harness/eval-promotion/request"));
  assert(PROTOCOL_CONTRACT.client_events.includes("evolution/evaluation-lane/request"));
  assert(PROTOCOL_CONTRACT.client_events.includes("pursuit/recovery/resume"));
  assert(PROTOCOL_CONTRACT.server_events.includes("pursuit/recovery/action_result"));
  assert(PROTOCOL_CONTRACT.client_events.includes("inspector/request"));
  assert(PROTOCOL_CONTRACT.client_events.includes("agents/request"));
  assert(PROTOCOL_CONTRACT.client_events.includes("agents/stop"));
  assert(PROTOCOL_CONTRACT.server_events.includes("ui/message"));
  assert(PROTOCOL_CONTRACT.server_events.includes("runtime/status"));
  assert(PROTOCOL_CONTRACT.server_events.includes("run/cancelled"));
  assert(PROTOCOL_CONTRACT.server_events.includes("completion/receipt"));
  assert(PROTOCOL_CONTRACT.server_events.includes("harness/receipt"));
  assert(PROTOCOL_CONTRACT.server_events.includes("harness/explain"));
  assert(PROTOCOL_CONTRACT.server_events.includes("harness/replay"));
  assert(PROTOCOL_CONTRACT.server_events.includes("harness/eval-baseline"));
  assert(PROTOCOL_CONTRACT.server_events.includes("harness/eval-batch"));
  assert(PROTOCOL_CONTRACT.server_events.includes("harness/eval-promotion"));
  assert(PROTOCOL_CONTRACT.server_events.includes("evolution/evaluation-lane"));
  assert(PROTOCOL_CONTRACT.server_events.includes("doctor/health"));
  assert(PROTOCOL_CONTRACT.client_events.includes("doctor/probe"));
  assert(PROTOCOL_CONTRACT.client_events.includes("doctor/probe/cancel"));
  assert(PROTOCOL_CONTRACT.server_events.includes("doctor/probe/result"));
  assert(PROTOCOL_CONTRACT.server_events.includes("tasks/snapshot"));
  assert.deepEqual(PROTOCOL_CONTRACT.harness_receipt.statuses, [
    "completed_verified",
    "completed_unverified",
    "blocked",
  ]);
  assert(PROTOCOL_CONTRACT.server_events.includes("inspector/snapshot"));
  assert(PROTOCOL_CONTRACT.server_events.includes("inspector/update"));
  assert(PROTOCOL_CONTRACT.server_events.includes("agents/snapshot"));
  assert(PROTOCOL_CONTRACT.server_events.includes("agents/update"));
  assert(PROTOCOL_CONTRACT.server_events.includes("agents/action"));
  assert(PROTOCOL_CONTRACT.runtime_status.reasoning_effort.efforts.includes("xhigh"));
  assert(PROTOCOL_CONTRACT.runtime_status.reasoning_effort.efforts.includes("max"));
  assert.deepEqual(PROTOCOL_CONTRACT.ui_messages.tool_prepare.phases, ["start", "snapshot", "end"]);
  assert(PROTOCOL_CONTRACT.ui_messages.tool_prepare.fields.includes("tool_call_id"));
  assert(PROTOCOL_CONTRACT.ui_messages.tool_prepare.fields.includes("content_lines"));
  assert(PROTOCOL_CONTRACT.ui_messages.tool_prepare.fields.includes("elapsed_ms"));
  assert(PROTOCOL_CONTRACT.ui_messages.tool_use.fields.includes("tool_call_id"));

  const chunks = [];
  const send = createEventSender({ write: (chunk) => chunks.push(chunk) });

  assert.throws(
    () => send("not_a_real_event", {}),
    /未知客户端事件/,
  );
  assert.equal(chunks.length, 0);
});

test("event governance registry exactly covers all published events", () => {
  assert.equal(validateEventRegistry(structuredClone(PROTOCOL_CONTRACT)), true);
  assert.deepEqual(
    Object.keys(PROTOCOL_CONTRACT.event_registry.client).sort(),
    [...PROTOCOL_CONTRACT.client_events].sort(),
  );
  assert.deepEqual(
    Object.keys(PROTOCOL_CONTRACT.event_registry.server).sort(),
    [...PROTOCOL_CONTRACT.server_events].sort(),
  );
  assert.equal(eventPolicy("server", "permission/request").owner, "safety");
  assert.equal(eventPolicy("server", "run/completed").criticality, "terminal");
  assert.equal(eventPolicy("client", "ping").persistence, "never");
});

test("compatibility ledger accepts only explicit prior registry digests", () => {
  assert.equal(validateCompatibility(structuredClone(PROTOCOL_CONTRACT)), true);
  assert.deepEqual(PROTOCOL_COMPATIBLE_REGISTRY_SHA256, [
    PROTOCOL_REGISTRY_SHA256,
    ...PROTOCOL_CONTRACT.compatibility.previous_registry_sha256,
  ]);

  const prior = structuredClone(PROTOCOL_CONTRACT);
  prior.compatibility.previous_registry_sha256 = ["a".repeat(64)];
  assert.equal(validateCompatibility(prior), true);

  const duplicate = structuredClone(PROTOCOL_CONTRACT);
  duplicate.compatibility.previous_registry_sha256 = [
    PROTOCOL_REGISTRY_SHA256,
  ];
  assert.throws(() => validateCompatibility(duplicate), /不得重复当前摘要/);

  const unsafePolicy = structuredClone(PROTOCOL_CONTRACT);
  unsafePolicy.compatibility.unknown_informational_events = "render_payload";
  assert.throws(() => validateCompatibility(unsafePolicy), /策略无效/);
});

test("event capability registry governs typed feature events", () => {
  assert.equal(validateEventCapabilities(structuredClone(PROTOCOL_CONTRACT)), true);
  assert.equal(
    requiredEventCapability("client", "evolution/evaluation-lane/request"),
    "evolution_evaluation_lane",
  );
  assert.equal(
    requiredEventCapability("server", "evolution/evaluation-lane"),
    "evolution_evaluation_lane",
  );
  assert.equal(
    requiredEventCapability("client", "doctor/export"),
    "doctor_export",
  );
  assert.equal(
    requiredEventCapability("server", "doctor/export/result"),
    "doctor_export",
  );
  assert.equal(
    requiredEventCapability("client", "doctor/probe"),
    "doctor_live_probe",
  );
  assert.equal(
    requiredEventCapability("server", "doctor/probe/result"),
    "doctor_live_probe",
  );
  assert.equal(requiredEventCapability("client", "submit"), null);
  assert.throws(() => requiredEventCapability("sideways", "submit"), /未知事件方向/);
});

test("event capability registry rejects unknown duplicate and empty bindings", () => {
  const unknownCapability = structuredClone(PROTOCOL_CONTRACT);
  unknownCapability.event_capabilities.future = {
    client_events: ["ping"],
    server_events: [],
  };
  assert.throws(() => validateEventCapabilities(unknownCapability), /未发布能力/);

  const unknownEvent = structuredClone(PROTOCOL_CONTRACT);
  unknownEvent.event_capabilities.evolution_evaluation_lane.client_events.push("future/request");
  assert.throws(() => validateEventCapabilities(unknownEvent), /未注册 client 事件/);

  const duplicate = structuredClone(PROTOCOL_CONTRACT);
  duplicate.event_capabilities.goal_snapshot = {
    client_events: ["evolution/evaluation-lane/request"],
    server_events: [],
  };
  assert.throws(() => validateEventCapabilities(duplicate), /多个能力重复绑定/);

  const empty = structuredClone(PROTOCOL_CONTRACT);
  empty.event_capabilities.evolution_evaluation_lane = {
    client_events: [],
    server_events: [],
  };
  assert.throws(() => validateEventCapabilities(empty), /至少需要一个事件/);
});

test("event governance registry rejects gaps and unredacted sensitive fields", () => {
  const missing = structuredClone(PROTOCOL_CONTRACT);
  delete missing.event_registry.client.ping;
  assert.throws(() => validateEventRegistry(missing), /精确覆盖/);

  const unsafe = structuredClone(PROTOCOL_CONTRACT);
  unsafe.event_registry.server["ui/message"].redaction = "none";
  assert.throws(() => validateEventRegistry(unsafe), /redaction/);

  const extraField = structuredClone(PROTOCOL_CONTRACT);
  extraField.event_registry.server.ready.undocumented = true;
  assert.throws(() => validateEventRegistry(extraField), /字段不完整/);

  const extraGroup = structuredClone(PROTOCOL_CONTRACT);
  extraGroup.event_registry.future = {};
  assert.throws(() => validateEventRegistry(extraGroup), /只能包含 client\/server/);
  assert.throws(() => eventPolicy("server", "future/unknown"), /未注册/);
});

test("hello payload is generated from the embedded negotiation contract", () => {
  assert.deepEqual(createHelloPayload(" naumi-terminal-ui "), {
    client: "naumi-terminal-ui",
    minimum_version: 1,
    maximum_version: 1,
    capabilities: [
      "agent_recovery_actions",
      "doctor_export",
      "doctor_live_probe",
      "doctor_trace_index",
      "evolution_evaluation_lane",
      "goal_snapshot",
      "heartbeat",
      "pursuit_recovery_actions",
      "sequence_integrity",
      "session_list",
      "task_snapshot",
      "terminal_event_cursor",
      "terminal_event_recovery",
      "typed_ui_messages",
      "workbench_proposal_actions",
      "workbench_snapshot",
    ],
  });
});

test("hello ack requires a valid negotiated version and capability subset", () => {
  const record = normalizeServerRecord({
    type: "ack",
    version: 1,
    payload: {
      event: "hello",
      negotiation: {
        selected_version: 1,
        server_minimum_version: 1,
        server_maximum_version: 1,
        capabilities: ["workbench_snapshot", "typed_ui_messages", "heartbeat"],
      },
    },
  });
  assert.deepEqual(record.payload.negotiation, {
    selected_version: 1,
    server_minimum_version: 1,
    server_maximum_version: 1,
    capabilities: ["heartbeat", "typed_ui_messages", "workbench_snapshot"],
  });

  assert.throws(
    () => normalizeServerRecord({
      type: "ack",
      version: 1,
      payload: {
        event: "hello",
        negotiation: {
          selected_version: 2,
          server_minimum_version: 2,
          server_maximum_version: 2,
          capabilities: ["typed_ui_messages"],
        },
      },
    }),
    /协商版本不兼容/,
  );
});

test("terminal receipt cursors are strict, complete, and event-scoped", () => {
  const normalized = normalizeServerRecord({
    type: "completion/receipt",
    version: 1,
    event_id: "tev_0123456789abcdef01234567",
    stream_id: "tes_89abcdef0123456701234567",
    cursor: 7,
    payload: {
      schema_version: 1,
      receipt_id: "receipt-cursor",
      run_id: "run-cursor",
      outcome: "completed",
    },
  });
  assert.equal(normalized.event_id, "tev_0123456789abcdef01234567");
  assert.equal(normalized.stream_id, "tes_89abcdef0123456701234567");
  assert.equal(normalized.cursor, 7);

  assert.throws(
    () => normalizeServerRecord({
      type: "completion/receipt",
      event_id: "tev_0123456789abcdef01234567",
      payload: {
        schema_version: 1,
        receipt_id: "receipt-cursor",
        run_id: "run-cursor",
        outcome: "completed",
      },
    }),
    /必须同时提供/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "ui/message",
      event_id: "tev_0123456789abcdef01234567",
      stream_id: "tes_89abcdef0123456701234567",
      cursor: 7,
      payload: { type: "text", content: "not journaled" },
    }),
    /不允许携带持久游标/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "completion/receipt",
      event_id: "bad",
      stream_id: "tes_89abcdef0123456701234567",
      cursor: 7,
      payload: {
        schema_version: 1,
        receipt_id: "receipt-cursor",
        run_id: "run-cursor",
        outcome: "completed",
      },
    }),
    /event_id 格式无效/,
  );
});

test("typed harness receipt is strict and bounded", () => {
  const normalized = normalizeServerRecord({
    type: "harness/receipt",
    payload: {
      schema_version: 1,
      revision: 3,
      run_id: "harness-run-1",
      status: "completed_unverified",
      task_kind: "change",
      changed_files: Array.from({ length: 120 }, (_, index) => `src/${index}.py`),
      checks: [{ id: "unit", status: "failed", private_payload: "must-drop" }],
      criteria: [],
      warnings: ["定向检查失败"],
      tree_fingerprint: "a".repeat(64),
    },
  });

  assert.equal(normalized.payload.run_id, "harness-run-1");
  assert.equal(normalized.payload.status, "completed_unverified");
  assert.equal(normalized.payload.revision, 3);
  assert.equal(normalized.payload.changed_files.length, 100);
  assert.deepEqual(normalized.payload.checks[0], {
    id: "unit",
    status: "failed",
    tree_fingerprint: "",
  });
  assert.throws(
    () => normalizeServerRecord({
      type: "harness/receipt",
      payload: { schema_version: 1, run_id: "", status: "completed_verified" },
    }),
    /run_id/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "harness/receipt",
      payload: { schema_version: 1, run_id: "run", status: "guessed" },
    }),
    /status/,
  );
});

test("harness explain response is strict and bounded", () => {
  const normalized = normalizeServerRecord({
    type: "harness/explain",
    payload: harnessExplainPayload(3),
  }).payload;

  assert.deepEqual(Object.keys(normalized), [
    "schema_version",
    "revision",
    "run_id",
    "lookup_status",
    "message",
    "explanation",
  ]);
  assert.equal(normalized.revision, 3);
  assert.equal(normalized.explanation.criteria.length, 100);
  assert.equal(normalized.explanation.criteria[0].evidence_ids.length, 100);
  assert.equal(Object.hasOwn(normalized.explanation.criteria[0], "private_payload"), false);
  assert.equal(normalized.explanation.failure_classes.length, 20);
  assert.equal(normalized.explanation.findings.length, 20);
  assert.equal(normalized.explanation.findings[0].check_ids.length, 50);
  assert.equal(normalized.explanation.findings[0].evidence_ids.length, 100);
  assert.equal(normalized.explanation.checks.length, 50);
  assert.equal(normalized.explanation.evidence.length, 100);
  assert.equal(Object.hasOwn(normalized, "private_payload"), false);
  assert.equal(Object.hasOwn(normalized.explanation.findings[0], "private_payload"), false);
});

test("harness replay response is strict and bounded", () => {
  const normalized = normalizeServerRecord({
    type: "harness/replay",
    payload: harnessReplayPayload(4),
  }).payload;

  assert.equal(normalized.revision, 4);
  assert.equal(normalized.result.status, "partial");
  assert.equal(normalized.result.timeline.length, 200);
  assert.equal(normalized.result.artifacts.length, 100);
  assert.equal(normalized.result.anomalies.length, 50);
  assert.equal(normalized.result.differences.length, 50);
  assert.equal(Object.hasOwn(normalized.result.artifacts[0], "private_payload"), false);
});

test("harness eval baseline response is strict and drops private fields", () => {
  const normalized = normalizeServerRecord({
    type: "harness/eval-baseline",
    payload: harnessEvalBaselinePayload(),
  }).payload;

  assert.equal(normalized.status, "ok");
  assert.equal(normalized.active.version, 2);
  assert.equal(normalized.comparisons[0].decision, "passed");
  assert.equal(Object.hasOwn(normalized.active, "private_payload"), false);
  assert.equal(Object.hasOwn(normalized.comparisons[0], "private_payload"), false);

  const mismatched = harnessEvalBaselinePayload();
  mismatched.comparisons[0].baseline_id = "e".repeat(64);
  assert.throws(
    () => normalizeServerRecord({ type: "harness/eval-baseline", payload: mismatched }),
    /active/,
  );
});

test("evaluation lane response is bounded, linked and cannot claim finality", () => {
  const normalized = normalizeServerRecord({
    type: "evolution/evaluation-lane",
    payload: evaluationLanePayload(),
  }).payload;
  assert.equal(normalized.statistical_verdict, "improved");
  assert.equal(normalized.baseline.failed_samples, 5);
  assert.equal(normalized.candidate.passed_samples, 5);
  assert.deepEqual({
    comparison_decision: normalized.comparison_decision,
    statistical_verdict: normalized.statistical_verdict,
    baseline_failed_samples: normalized.baseline.failed_samples,
    candidate_passed_samples: normalized.candidate.passed_samples,
    baseline_tokens: normalized.baseline.observed_tokens,
    candidate_tokens: normalized.candidate.observed_tokens,
    candidate_evaluation_complete: normalized.candidate_evaluation_complete,
    aggregation_required: normalized.aggregation_required,
  }, EVALUATION_LANE_GOLDEN.public_semantics);

  assert.throws(() => normalizeServerRecord({
    type: "evolution/evaluation-lane",
    payload: { ...evaluationLanePayload(), candidate_evaluation_complete: true },
  }), /最终结论/);
  const broken = evaluationLanePayload();
  broken.artifacts[3].sha256 = "9".repeat(64);
  assert.throws(
    () => normalizeServerRecord({ type: "evolution/evaluation-lane", payload: broken }),
    /公开摘要/,
  );
});

test("harness eval batch response validates factual progress and terminal state", () => {
  const progress = normalizeServerRecord({
    type: "harness/eval-batch",
    payload: harnessEvalBatchPayload(),
  }).payload;
  const completed = normalizeServerRecord({
    type: "harness/eval-batch",
    payload: harnessEvalBatchPayload("completed"),
  }).payload;

  assert.equal(progress.stage, "evaluating");
  assert.equal(progress.terminal, false);
  assert.equal(completed.persisted, 5);
  assert.equal(completed.baseline_eligible, true);
  assert.equal(Object.hasOwn(progress, "private_payload"), false);

  const invalid = harnessEvalBatchPayload("completed");
  invalid.persisted = 4;
  assert.throws(
    () => normalizeServerRecord({ type: "harness/eval-batch", payload: invalid }),
    /完整样本/,
  );
});

test("harness live eval response validates budget and privacy facts", () => {
  const progress = normalizeServerRecord({
    type: "harness/eval-batch",
    payload: harnessLiveEvalPayload(),
  }).payload;
  const completed = normalizeServerRecord({
    type: "harness/eval-batch",
    payload: harnessLiveEvalPayload("completed"),
  }).payload;

  assert.equal(progress.kind, "live");
  assert.equal(progress.total_calls, 2);
  assert.equal(progress.cost_source, "rate_card_estimate");
  assert.equal(progress.rate_card_source, "catalog");
  assert.equal(progress.billing_status, "unsupported");
  assert.equal(progress.provider_response_ids_observed, 2);
  assert.equal(completed.baseline_eligible, true);
  assert.equal(Object.hasOwn(progress, "private_payload"), false);

  const forged = harnessLiveEvalPayload("partial");
  forged.total_cost_usd = 0.2;
  assert.throws(
    () => normalizeServerRecord({ type: "harness/eval-batch", payload: forged }),
    /超支标记/,
  );

  const forgedBilling = harnessLiveEvalPayload("partial");
  forgedBilling.cost_source = "provider_billing";
  assert.throws(
    () => normalizeServerRecord({
      type: "harness/eval-batch",
      payload: forgedBilling,
    }),
    /账单来源与状态/,
  );

  const legacy = harnessLiveEvalPayload("partial");
  delete legacy.cost_source;
  delete legacy.rate_card_source;
  delete legacy.billing_status;
  delete legacy.provider_response_ids_observed;
  const normalizedLegacy = normalizeServerRecord({
    type: "harness/eval-batch",
    payload: legacy,
  }).payload;
  assert.equal(normalizedLegacy.cost_source, "unavailable");
  assert.equal(normalizedLegacy.rate_card_source, "unavailable");
  assert.equal(normalizedLegacy.billing_status, "unavailable");
  assert.equal(normalizedLegacy.provider_response_ids_observed, 0);
});

test("harness sandbox eval response preserves coordinator checkpoint semantics", () => {
  const progress = normalizeServerRecord({
    type: "harness/eval-batch",
    payload: harnessSandboxEvalPayload(),
  }).payload;
  const completed = normalizeServerRecord({
    type: "harness/eval-batch",
    payload: harnessSandboxEvalPayload("completed"),
  }).payload;

  assert.equal(progress.kind, "sandbox");
  assert.equal(progress.stage, "executing");
  assert.deepEqual(progress.check_ids, ["unit", "lint"]);
  assert.equal(progress.sample_result_sha256.length, 2);
  assert.equal(completed.terminal, true);
  assert.equal(completed.persisted, 5);

  const invalid = harnessSandboxEvalPayload();
  invalid.sample_result_sha256.pop();
  assert.throws(
    () => normalizeServerRecord({ type: "harness/eval-batch", payload: invalid }),
    /摘要数量/,
  );
});

test("harness sandbox admission response requires a coherent durable snapshot", () => {
  const queuedPayload = {
    ...harnessSandboxEvalPayload("queued"),
    persisted: 0,
    sample_result_sha256: [],
    run_id: "",
    run_grant_sha256: "",
    admission_ticket_id: `hsadm_${"e".repeat(24)}`,
    admission_epoch: 1,
    admission_state: "queued",
    queue_position: 2,
    max_active: 1,
    max_queued: 4,
    active_count: 1,
    queued_count: 3,
  };
  const queued = normalizeServerRecord({
    type: "harness/eval-batch",
    payload: queuedPayload,
  }).payload;

  assert.equal(queued.stage, "queued");
  assert.equal(queued.admission_ticket_id, `hsadm_${"e".repeat(24)}`);
  assert.equal(queued.queue_position, 2);
  assert.equal(queued.active_count, 1);

  assert.throws(
    () => normalizeServerRecord({
      type: "harness/eval-batch",
      payload: { ...queuedPayload, queue_position: 4 },
    }),
    /queue position/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "harness/eval-batch",
      payload: { ...queuedPayload, admission_state: "active" },
    }),
    /queue position|stage 与 admission state/,
  );
});

test("harness sandbox cancel result preserves exact fence and durable receipt", () => {
  const payload = normalizeServerRecord({
    type: "harness/eval-sandbox/cancel-result",
    payload: {
      schema_version: 1,
      receipt_id: `hsacr_${"a".repeat(24)}`,
      receipt_sha256: "a".repeat(64),
      action_id: `hsac_${"b".repeat(24)}`,
      ticket_id: `hsadm_${"c".repeat(24)}`,
      authority_key: "d".repeat(64),
      presented_epoch: 2,
      presented_state: "active",
      decision: "accepted",
      observed_state: "cancelled",
      code: "sandbox_batch_cancelled_by_user",
      actor_id: "new-ui",
      reason: "用户取消",
      created_at: "2026-07-23T12:00:00+00:00",
      current: {
        ticket_id: `hsadm_${"c".repeat(24)}`,
        authority_key: "d".repeat(64),
        epoch: 2,
        state: "cancelled",
        queue_position: 0,
        max_active: 1,
        max_queued: 8,
        active_count: 0,
        queued_count: 0,
        updated_at: "2026-07-23T12:00:00+00:00",
        terminal_code: "sandbox_batch_cancelled_by_user",
      },
    },
  }).payload;

  assert.equal(payload.decision, "accepted");
  assert.equal(payload.current.state, "cancelled");
  assert.throws(
    () => normalizeServerRecord({
      type: "harness/eval-sandbox/cancel-result",
      payload: { ...payload, observed_state: "missing" },
    }),
    /current 与 observed_state/,
  );
});

test("harness sandbox retry result preserves receipt dispatch and H5a authority", () => {
  const payload = normalizeServerRecord({
    type: "harness/eval-sandbox/retry-result",
    payload: {
      schema_version: 1,
      receipt_id: `hsarr_${"a".repeat(24)}`,
      receipt_sha256: "a".repeat(64),
      action_id: `hsar_${"b".repeat(24)}`,
      cancel_receipt_id: `hsacr_${"c".repeat(24)}`,
      cancel_receipt_sha256: "c".repeat(64),
      source_ticket_id: `hsadm_${"d".repeat(24)}`,
      eval_request_sha256: "e".repeat(64),
      execution_authority_key: "f".repeat(64),
      decision: "accepted",
      outcome: "completed",
      code: "sandbox_batch_retry_authorized",
      actor_id: "new-ui",
      reason: "用户恢复",
      created_at: "2026-07-23T12:00:00+00:00",
      dispatch: {
        dispatch_id: `hsard_${"1".repeat(24)}`,
        retry_action_id: `hsar_${"b".repeat(24)}`,
        retry_receipt_id: `hsarr_${"a".repeat(24)}`,
        retry_receipt_sha256: "a".repeat(64),
        execution_authority_key: "f".repeat(64),
        state: "completed",
        epoch: 1,
        ticket_id: `hsadm_${"2".repeat(24)}`,
        ticket_epoch: 1,
        updated_at: "2026-07-23T12:01:00+00:00",
        terminal_code: "",
      },
      batch_id: "sandbox-retry",
      requested: 5,
      persisted: 5,
      message: "已完成",
    },
  }).payload;

  assert.equal(payload.outcome, "completed");
  assert.equal(payload.dispatch.ticket_id, `hsadm_${"2".repeat(24)}`);
  assert.equal(payload.persisted, 5);
  assert.throws(
    () => normalizeServerRecord({
      type: "harness/eval-sandbox/retry-result",
      payload: {
        ...payload,
        dispatch: {
          ...payload.dispatch,
          execution_authority_key: "0".repeat(64),
        },
      },
    }),
    /dispatch 与 decision\/action/,
  );
});

test("harness eval promotion response validates guided and authoritative state", () => {
  const waiting = normalizeServerRecord({
    type: "harness/eval-promotion",
    payload: harnessEvalPromotionPayload("awaiting_confirmation"),
  }).payload;
  const promoted = normalizeServerRecord({
    type: "harness/eval-promotion",
    payload: harnessEvalPromotionPayload(),
  }).payload;

  assert.equal(waiting.terminal, false);
  assert.equal(waiting.promotion_reason, "完整回归已通过");
  assert.equal(promoted.baseline_id, "a".repeat(64));
  assert.equal(Object.hasOwn(promoted, "private_payload"), false);

  const incomplete = harnessEvalPromotionPayload();
  incomplete.promoted_by = "";
  assert.throws(
    () => normalizeServerRecord({ type: "harness/eval-promotion", payload: incomplete }),
    /权威字段/,
  );
  const missingReason = harnessEvalPromotionPayload("awaiting_confirmation");
  missingReason.promotion_reason = "";
  assert.throws(
    () => normalizeServerRecord({ type: "harness/eval-promotion", payload: missingReason }),
    /缺少晋升理由/,
  );
});

test("doctor health response is strict bounded and drops private fields", () => {
  const normalized = normalizeServerRecord({
    type: "doctor/health",
    payload: doctorHealthPayload(),
  }).payload;

  assert.equal(normalized.status, "degraded");
  assert.equal(normalized.items[0].domain, "provider");
  assert.equal(normalized.items[0].responsibility, "user_config");
  assert.equal(normalized.items[0].diagnostic_code, "provider_credentials_missing");
  assert.equal(Object.hasOwn(normalized, "private_payload"), false);
  assert.equal(Object.hasOwn(normalized.items[0], "private_payload"), false);

  const invalid = doctorHealthPayload();
  invalid.items[0].severity = "warning";
  assert.throws(
    () => normalizeServerRecord({ type: "doctor/health", payload: invalid }),
    /severity/,
  );
  const duplicate = doctorHealthPayload();
  duplicate.items.push({ ...duplicate.items[0] });
  assert.throws(
    () => normalizeServerRecord({ type: "doctor/health", payload: duplicate }),
    /必须唯一/,
  );
  const invalidCode = doctorHealthPayload();
  invalidCode.items[0].diagnostic_code = "BAD-CODE";
  assert.throws(
    () => normalizeServerRecord({ type: "doctor/health", payload: invalidCode }),
    /diagnostic_code/,
  );
});

test("doctor trace response is bounded correlated metadata and drops bodies", () => {
  const normalized = normalizeServerRecord({
    type: "doctor/trace/result",
    payload: doctorTracePayload(),
  }).payload;

  assert.equal(normalized.status, "ready");
  assert.equal(normalized.entries[0].event_type, "exception");
  assert.deepEqual(normalized.entries[0].identifiers, { call_id: "call-1" });
  assert.equal(Object.hasOwn(normalized, "private_payload"), false);
  assert.equal(Object.hasOwn(normalized.entries[0], "private_body"), false);

  const tooMany = doctorTracePayload();
  tooMany.entries = Array.from({ length: 201 }, () => tooMany.entries[0]);
  assert.throws(
    () => normalizeServerRecord({ type: "doctor/trace/result", payload: tooMany }),
    /超过 200/,
  );
  const unknownIdentifier = doctorTracePayload();
  unknownIdentifier.entries[0].identifiers = { secret: "must-not-pass" };
  assert.throws(
    () => normalizeServerRecord({ type: "doctor/trace/result", payload: unknownIdentifier }),
    /未知键/,
  );
});

test("doctor export response validates preview and exact written receipt", () => {
  const preview = normalizeServerRecord({
    type: "doctor/export/result",
    payload: doctorExportPayload(),
  }).payload;
  const written = normalizeServerRecord({
    type: "doctor/export/result",
    payload: doctorExportPayload("written"),
  }).payload;

  assert.equal(preview.status, "preview");
  assert.equal(preview.receipt, null);
  assert.equal(preview.files.length, 3);
  assert.equal(Object.hasOwn(preview, "private_payload"), false);
  assert.equal(written.receipt.output_path, "/state/diagnostics/report.zip");
  assert.equal(Object.hasOwn(written.receipt, "private_payload"), false);

  const mismatch = doctorExportPayload("written");
  mismatch.receipt.bundle_sha256 = "f".repeat(64);
  assert.throws(
    () => normalizeServerRecord({ type: "doctor/export/result", payload: mismatch }),
    /摘要不一致/,
  );
  const duplicate = doctorExportPayload();
  duplicate.files[2].path = "health.json";
  assert.throws(
    () => normalizeServerRecord({ type: "doctor/export/result", payload: duplicate }),
    /必须唯一/,
  );
});

test("doctor probe response enforces exact request and token budgets", () => {
  const passed = normalizeServerRecord({
    type: "doctor/probe/result",
    payload: doctorProbePayload(),
  }).payload;
  const cancelled = normalizeServerRecord({
    type: "doctor/probe/result",
    payload: doctorProbePayload("cancelled"),
  }).payload;

  assert.equal(passed.status, "passed");
  assert.equal(passed.request_count, 1);
  assert.equal(passed.max_output_tokens, 8);
  assert.equal(Object.hasOwn(passed, "private_payload"), false);
  assert.equal(cancelled.snapshot_sha256, "");

  for (const mutate of [
    (payload) => { payload.request_limit = 2; },
    (payload) => { payload.max_output_tokens = 64; },
    (payload) => { payload.request_count = 0; },
    (payload) => { payload.timeout_ms = 60_001; },
  ]) {
    const invalid = doctorProbePayload();
    mutate(invalid);
    assert.throws(
      () => normalizeServerRecord({ type: "doctor/probe/result", payload: invalid }),
      /doctor\/probe/,
    );
  }
});

test("harness detail responses reject malformed authoritative state", () => {
  const invalidRevision = harnessExplainPayload(0);
  assert.throws(
    () => normalizeServerRecord({ type: "harness/explain", payload: invalidRevision }),
    /revision/,
  );

  const missingExplanation = harnessExplainPayload();
  delete missingExplanation.explanation;
  assert.throws(
    () => normalizeServerRecord({ type: "harness/explain", payload: missingExplanation }),
    /explanation/,
  );

  const invalidBoolean = harnessExplainPayload();
  invalidBoolean.explanation.verified = "false";
  assert.throws(
    () => normalizeServerRecord({ type: "harness/explain", payload: invalidBoolean }),
    /verified/,
  );

  const runningExplain = harnessExplainPayload();
  runningExplain.explanation.status = "running";
  runningExplain.explanation.running = true;
  assert.throws(
    () => normalizeServerRecord({ type: "harness/explain", payload: runningExplain }),
    /尚未完成/,
  );

  const invalidReplay = harnessReplayPayload();
  invalidReplay.result.status = "executed_again";
  assert.throws(
    () => normalizeServerRecord({ type: "harness/replay", payload: invalidReplay }),
    /status/,
  );

  const runningReplay = harnessReplayPayload();
  runningReplay.result.anomalies = ["run_not_finished"];
  assert.throws(
    () => normalizeServerRecord({ type: "harness/replay", payload: runningReplay }),
    /尚未完成/,
  );

  const unavailable = harnessReplayPayload();
  unavailable.lookup_status = "unavailable";
  delete unavailable.result;
  assert.equal(
    normalizeServerRecord({ type: "harness/replay", payload: unavailable }).payload.lookup_status,
    "unavailable",
  );
});

test("permission snapshot is strict bounded and drops private fields", () => {
  const pending = Array.from({ length: 55 }, (_, index) => ({
    request_id: `perm-${index}`,
    call_id: `call-${index}`,
    session_id: "session-1",
    run_id: "run-1",
    agent_name: "main",
    tool_name: "bash_run",
    tool_family: "shell",
    arguments_summary: "command=echo safe",
    reason: "需要执行定向检查。",
    risk_level: "medium",
    choices: ["allow_once", "deny", "grant_session"],
    scope: "session",
    expires_at: "",
    status: "needs_confirmation",
    policy: {
      source: "TOOL_PERMISSIONS:bash_run",
      risk: "medium",
      modes: "bypass/permissive/moderate",
      confirmation: "需要确认",
      bypass: "bypass 全权限放行",
    },
    private_payload: "must-drop",
  }));
  const normalized = normalizeServerRecord({
    type: "permissions/snapshot",
    payload: {
      schema_version: 1,
      runtime_mode: "default",
      permission_mode: "moderate",
      pending,
      grants: [],
      history: [{
        ...pending[0],
        status: "allow_once",
        receipt_id: "receipt-1",
        actor: "user",
        source: "user_confirmation",
        decided_at: "2026-07-19T08:00:00+00:00",
      }],
      warnings: [],
    },
  }).payload;

  assert.equal(normalized.pending.length, 50);
  assert.equal(normalized.pending[0].policy.source, "TOOL_PERMISSIONS:bash_run");
  assert.equal(normalized.history[0].receipt_id, "receipt-1");
  assert.equal(normalized.history[0].actor, "user");
  assert.equal(Object.hasOwn(normalized.pending[0], "private_payload"), false);
  assert.throws(
    () => normalizeServerRecord({
      type: "permissions/snapshot",
      payload: { ...normalized, permission_mode: "invented" },
    }),
    /permission_mode/,
  );
});

test("task snapshot is strict, bounded, and drops private fields", () => {
  const item = {
    view_id: "subagent:sub_1",
    source: "subagent",
    task_id: "sub_1",
    status: "running",
    raw_status: "started",
    title: "探索项目结构",
    owner: "Explore",
    priority: null,
    dependency_ids: [],
    child_ids: [],
    created_at: "2026-07-18T00:00:00+00:00",
    updated_at: "2026-07-18T00:00:01+00:00",
    age_seconds: 1,
    detail: "phase=running",
    artifact_refs: [],
    private_reasoning: "must-drop",
  };
  const normalized = normalizeServerRecord({
    type: "tasks/snapshot",
    payload: {
      schema_version: 1,
      generated_at: "2026-07-18T00:00:01+00:00",
      full: true,
      filters: { source: "all", status: "all", detail_id: "", history: false },
      items: Array.from({ length: 205 }, () => item),
      timeline: [],
      warnings: [],
    },
  }).payload;
  assert.equal(normalized.items.length, 200);
  assert.equal(normalized.items[0].owner, "Explore");
  assert.equal(Object.hasOwn(normalized.items[0], "private_reasoning"), false);
  assert.throws(
    () => normalizeServerRecord({
      type: "tasks/snapshot",
      payload: { ...normalized, items: [{ ...item, status: "invented" }] },
    }),
    /status/,
  );
});

test("session list is workspace scoped, bounded, strict, and drops private fields", () => {
  const item = {
    session_id: "session-1",
    title: "历史会话",
    model: "provider/model",
    updated_at: "2026-07-22T08:00:00+00:00",
    message_count: 2,
    user_message_count: 1,
    git_branch: "main",
    is_current: false,
    resumable: true,
    messages: [{ role: "user", content: "must-drop" }],
    summary: "must-drop",
    workspace_root: "/private/workspace",
  };
  const normalized = normalizeServerRecord({
    type: "sessions/list",
    payload: {
      schema_version: 1,
      generated_at: "2026-07-22T08:00:01+00:00",
      scope: "workspace",
      page: 1,
      page_size: 100,
      total: 125,
      query: "history",
      items: Array.from({ length: 105 }, () => item),
      warnings: [],
    },
  }).payload;
  assert.equal(normalized.items.length, 100);
  assert.equal(normalized.items[0].title, "历史会话");
  assert.equal(Object.hasOwn(normalized.items[0], "messages"), false);
  assert.equal(Object.hasOwn(normalized.items[0], "summary"), false);
  assert.equal(Object.hasOwn(normalized.items[0], "workspace_root"), false);
  assert.throws(
    () => normalizeServerRecord({
      type: "sessions/list",
      payload: { ...normalized, scope: "global" },
    }),
    /scope/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "sessions/list",
      payload: { ...normalized, items: [{ ...item, session_id: "bad;id" }] },
    }),
    /session_id/,
  );
});

test("workspace file list is bounded, relative, and template-correlated", () => {
  const item = {
    path: "src/中文 file.py",
    name: "中文 file.py",
    directory: "src",
    extension: ".py",
    template: "/read 'src/中文 file.py'",
    absolute_path: "/private/workspace/src/中文 file.py",
  };
  const normalized = normalizeServerRecord({
    type: "workspace/files",
    payload: {
      schema_version: 1,
      status: "ready",
      revision: 3,
      index_sha256: "b".repeat(64),
      query: "中文",
      items: [item],
      total_indexed: 12,
      truncated: false,
      source: "git",
      built_at: "2026-07-23T00:00:00+00:00",
      message: "",
      workspace_root: "/private/workspace",
    },
  }).payload;

  assert.equal(normalized.items[0].path, "src/中文 file.py");
  assert.equal(Object.hasOwn(normalized.items[0], "absolute_path"), false);
  assert.equal(Object.hasOwn(normalized, "workspace_root"), false);
  for (const invalid of [
    { ...item, path: "/etc/passwd", template: "/read /etc/passwd" },
    { ...item, path: "../secret", template: "/read ../secret" },
    { ...item, template: "/write 'src/中文 file.py'" },
    { ...item, name: "安全文件.py" },
  ]) {
    assert.throws(
      () => normalizeServerRecord({
        type: "workspace/files",
        payload: { ...normalized, items: [invalid] },
      }),
      /workspace\/files/,
    );
  }
});

test("goal snapshot is strict, bounded, and preserves stable Pursuit links", () => {
  const pursuit = {
    run_id: "pursuit_1",
    goal: "完成 Goal 页面",
    status: "waiting",
    phase: "waiting",
    started_at: "2026-07-18T00:00:00+00:00",
    updated_at: "2026-07-18T00:00:01+00:00",
    iteration: 3,
    criteria_total: 4,
    criteria_verified: 2,
    failure_count: 0,
    blocked_reason: "",
    next_action: "等待用户选择",
    boundary_decision: {
      schema_version: 2,
      decision_id: "a".repeat(64),
      facts_sha256: "b".repeat(64),
      status: "waiting",
      code: "waiting_for_interaction",
      reason: "目标追踪正在等待用户回答。",
      next_action: "回答当前交互后，从持久 checkpoint 继续。",
      terminal: false,
      resumable: true,
      private_payload: "drop",
    },
    worktree_name: "",
    worktree_path: "",
    waits: Array.from({ length: 25 }, (_, index) => ({
      task_id: `bg_${index}`,
      action_id: `a_${index}`,
      command: "pytest -q",
      created_at: "2026-07-18T00:00:00+00:00",
      private_payload: "drop",
    })),
    evidence: Array.from({ length: 25 }, (_, index) => ({
      kind: "test",
      source: `case:${index}`,
      summary: "验证通过",
      is_hard: true,
      timestamp: "2026-07-18T00:00:00+00:00",
      private_payload: "drop",
    })),
    recovery: {
      schema_version: 2,
      run_id: "pursuit_1",
      generated_at: "2026-07-18T00:00:01+00:00",
      recovery_state: "reconcile_required",
      heartbeat: {
        health: "stale", phase: "running", instance_id: "worker-a",
        epoch: 2, sequence: 7, observed_at: "2026-07-18T00:00:00+00:00",
        timeout_seconds: 30, age_seconds: 31, detail_code: "lease_active",
        private_payload: "drop",
      },
      lease: {
        status: "active", owner_id: "worker-a", epoch: 2,
        expires_at: "2026-07-18T00:05:00+00:00",
        updated_at: "2026-07-18T00:00:00+00:00", expired: false,
      },
      checkpoint: {
        status: "ready", checkpoint_id: "pchk_1", sequence: 4,
        phase: "action_inflight", iteration: 3,
        created_at: "2026-07-18T00:00:00+00:00",
      },
      reconcile_required: true,
      reconcile_reason: "stale_preparing",
      alerts: ["需要核对后台任务"],
      resume_action: {
        schema_version: 1,
        action: "resume",
        state: "blocked",
        code: "reconcile_required",
        reason: "存在未核对副作用，必须先完成人工核对。",
        command: "/pursue resume pursuit_1",
      },
      attempts: [{
        schema_version: 1,
        attempt_id: `recovery-${"c".repeat(64)}`,
        state: "resolved",
        requested_at: "2026-07-18T00:00:00+00:00",
        updated_at: "2026-07-18T00:00:01+00:00",
        admitted_at: "",
        resolved_at: "2026-07-18T00:00:01+00:00",
        lease_epoch: 0,
        checkpoint_id: "",
        result_code: "operation_busy",
        boundary_decision_id: "",
        source_request_sha256: "private-digest",
      }],
      private_reasoning: "drop",
    },
    private_reasoning: "drop",
  };
  const goal = {
    goal_id: "goal_1",
    objective: "完成 Goal 页面",
    status: "active",
    note: "",
    session_id: "session_1",
    pursuit_run_id: "pursuit_1",
    pursuit_link_status: "ready",
    created_at: "2026-07-18T00:00:00+00:00",
    updated_at: "2026-07-18T00:00:01+00:00",
    pursuit,
    private_payload: "drop",
  };
  const normalized = normalizeServerRecord({
    type: "goals/snapshot",
    payload: {
      schema_version: 2,
      generated_at: "2026-07-18T00:00:01+00:00",
      full: true,
      current_goal_id: "goal_1",
      goals: [goal, ...Array.from({ length: 54 }, (_, index) => ({
        ...goal,
        goal_id: `goal_${index + 2}`,
        pursuit_run_id: "",
        pursuit_link_status: "not_linked",
        pursuit: null,
      }))],
      warnings: [],
      truncated: true,
      include_finished: true,
      interactions: [{
        interaction_id: "ask-goal-1",
        pursuit_run_id: "pursuit_1",
        state: "pending",
        sequence: 2,
        header: "继续方式",
        question: "是否继续执行？",
        created_at: "2026-07-18T00:00:00+00:00",
        expires_at: "2026-07-18T01:00:00+00:00",
        updated_at: "2026-07-18T00:00:01+00:00",
        can_cancel: true,
        can_takeover: true,
        owner_id: "private-owner",
      }],
      interaction_filter: "pending",
      interaction_cursor: "",
      interaction_next_cursor: "opaque-next",
      interaction_has_more: true,
      selected_interaction: {
        interaction_id: "ask-goal-1",
        pursuit_run_id: "pursuit_1",
        state: "pending",
        sequence: 2,
        header: "继续方式",
        question: "是否继续执行？",
        created_at: "2026-07-18T00:00:00+00:00",
        expires_at: "2026-07-18T01:00:00+00:00",
        updated_at: "2026-07-18T00:00:01+00:00",
        can_cancel: true,
        can_takeover: true,
        options: [{
          value: "continue",
          label: "继续",
          description: "继续执行",
          private_payload: "drop",
        }],
        allow_custom: true,
        custom_label: "其他",
        answer_kind: "",
        answer_value: "",
        answer_label: "",
        custom_text: "",
        answered_at: "",
        owner_epoch: 1,
        question_expired: false,
        lease_expired: true,
        owner_id: "private-owner",
      },
      terminal_outbox: {
        schema_version: 1,
        enabled: true,
        status: "recovering",
        worker_state: "waiting",
        assessed_at: "2026-07-18T00:00:01+00:00",
        counts: {
          total_pending: 3,
          due: 1,
          backoff: 1,
          live_claimed: 1,
          expired_claimed: 0,
          private_owner: "drop",
        },
        pass_count: 8,
        delivered_count: 4,
        retry_scheduled_count: 2,
        failure_count: 0,
        next_delay_seconds: 12.5,
        failure_codes: [],
        warning: "",
        private_owner: "drop",
      },
    },
  }).payload;

  assert.equal(normalized.goals.length, 50);
  assert.equal(normalized.goals[0].pursuit.run_id, "pursuit_1");
  assert.equal(normalized.goals[0].pursuit.waits.length, 20);
  assert.equal(normalized.goals[0].pursuit.evidence.length, 20);
  assert.equal(
    normalized.goals[0].pursuit.boundary_decision.code,
    "waiting_for_interaction",
  );
  assert.equal(normalized.goals[0].pursuit.boundary_decision.schema_version, 2);
  assert.equal(
    Object.hasOwn(
      normalized.goals[0].pursuit.boundary_decision,
      "private_payload",
    ),
    false,
  );
  assert.equal(normalized.goals[0].pursuit.recovery.recovery_state, "reconcile_required");
  assert.equal(normalized.goals[0].pursuit.recovery.heartbeat.health, "stale");
  assert.equal(normalized.goals[0].pursuit.recovery.schema_version, 2);
  assert.equal(normalized.goals[0].pursuit.recovery.resume_action.state, "blocked");
  assert.equal(normalized.goals[0].pursuit.recovery.attempts[0].result_code, "operation_busy");
  assert.equal(
    Object.hasOwn(normalized.goals[0].pursuit.recovery.attempts[0], "source_request_sha256"),
    false,
  );
  assert.equal(Object.hasOwn(normalized.goals[0].pursuit.recovery, "private_reasoning"), false);
  assert.equal(Object.hasOwn(normalized.goals[0].pursuit.recovery.heartbeat, "private_payload"), false);
  assert.equal(Object.hasOwn(normalized.goals[0], "private_payload"), false);
  assert.equal(Object.hasOwn(normalized.goals[0].pursuit, "private_reasoning"), false);
  assert.equal(Object.hasOwn(normalized.goals[0].pursuit.waits[0], "private_payload"), false);
  assert.equal(normalized.interactions[0].interaction_id, "ask-goal-1");
  assert.equal(normalized.interactions[0].can_cancel, true);
  assert.equal(normalized.interactions[0].can_takeover, true);
  assert.equal(Object.hasOwn(normalized.interactions[0], "owner_id"), false);
  assert.equal(normalized.schema_version, 2);
  assert.equal(normalized.interaction_filter, "pending");
  assert.equal(normalized.interaction_has_more, true);
  assert.equal(normalized.selected_interaction.options[0].label, "继续");
  assert.equal(normalized.terminal_outbox.status, "recovering");
  assert.equal(normalized.terminal_outbox.counts.total_pending, 3);
  assert.equal(Object.hasOwn(normalized.terminal_outbox, "private_owner"), false);
  assert.equal(Object.hasOwn(normalized.terminal_outbox.counts, "private_owner"), false);
  assert.equal(Object.hasOwn(normalized.selected_interaction, "owner_id"), false);
  assert.equal(
    Object.hasOwn(normalized.selected_interaction.options[0], "private_payload"),
    false,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "goals/snapshot",
      payload: {
        ...normalized,
        terminal_outbox: {
          ...normalized.terminal_outbox,
          counts: { ...normalized.terminal_outbox.counts, total_pending: 4 },
        },
      },
    }),
    /分类计数/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "goals/snapshot",
      payload: {
        ...normalized,
        terminal_outbox: {
          ...normalized.terminal_outbox,
          assessed_at: "2026-07-18T00:00:01",
        },
      },
    }),
    /带时区/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "goals/snapshot",
      payload: { ...normalized, goals: [{ ...goal, status: "invented" }] },
    }),
    /status/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "goals/snapshot",
      payload: {
        ...normalized,
        goals: [{ ...goal, pursuit_run_id: "pursuit_other" }],
      },
    }),
    /一致/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "goals/snapshot",
      payload: {
        ...normalized,
        goals: [{
          ...goal,
          pursuit: { ...pursuit, recovery: { ...pursuit.recovery, run_id: "pursuit_other" } },
        }],
      },
    }),
    /recovery.run_id/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "goals/snapshot",
      payload: {
        ...normalized,
        interactions: [{ ...normalized.interactions[0], can_cancel: false }],
      },
    }),
    /can_cancel/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "goals/snapshot",
      payload: {
        ...normalized,
        interactions: [{
          ...normalized.interactions[0],
          state: "answered",
          can_cancel: false,
          can_takeover: true,
        }],
      },
    }),
    /can_takeover/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "goals/snapshot",
      payload: {
        ...normalized,
        goals: [{
          ...goal,
          pursuit: {
            ...pursuit,
            recovery: {
              ...pursuit.recovery,
              heartbeat: { ...pursuit.recovery.heartbeat, timeout_seconds: 0 },
            },
          },
        }],
      },
    }),
    /timeout/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "goals/snapshot",
      payload: {
        ...normalized,
        goals: [{
          ...goal,
          pursuit: {
            ...pursuit,
            boundary_decision: {
              ...pursuit.boundary_decision,
              terminal: true,
            },
          },
        }],
      },
    }),
    /terminal/,
  );
  const legacyBoundary = normalizeServerRecord({
    type: "goals/snapshot",
    payload: {
      ...normalized,
      goals: [{
        ...goal,
        pursuit: {
          ...pursuit,
          boundary_decision: {
            ...pursuit.boundary_decision,
            schema_version: 1,
          },
        },
      }],
    },
  }).payload;
  assert.equal(
    legacyBoundary.goals[0].pursuit.boundary_decision.schema_version,
    1,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "goals/snapshot",
      payload: {
        ...normalized,
        goals: [{
          ...goal,
          pursuit: {
            ...pursuit,
            boundary_decision: {
              ...pursuit.boundary_decision,
              schema_version: 3,
            },
          },
        }],
      },
    }),
    /schema_version/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "goals/snapshot",
      payload: {
        ...normalized,
        goals: [{
          ...goal,
          pursuit: {
            ...pursuit,
            boundary_decision: {
              ...pursuit.boundary_decision,
              reason: "x".repeat(301),
            },
          },
        }],
      },
    }),
    /长度/,
  );
});

test("Pursuit recovery action result is typed and attempt-bound", () => {
  const attempt = {
    schema_version: 1,
    attempt_id: `recovery-${"d".repeat(64)}`,
    state: "admitted",
    requested_at: "2026-07-18T00:00:00+00:00",
    updated_at: "2026-07-18T00:00:01+00:00",
    admitted_at: "2026-07-18T00:00:01+00:00",
    resolved_at: "",
    lease_epoch: 3,
    checkpoint_id: "checkpoint-1",
    result_code: "",
    boundary_decision_id: "",
    source_request_sha256: "drop-private-digest",
  };
  const normalized = normalizeServerRecord({
    type: "pursuit/recovery/action_result",
    payload: {
      schema_version: 1,
      run_id: "pursuit_1",
      status: "admitted",
      code: "admitted",
      message: "已准入并在后台继续。",
      attempt,
      resume_action: {
        schema_version: 1,
        action: "resume",
        state: "busy",
        code: "recovery_attempt_active",
        reason: "已有恢复请求正在执行。",
        command: "/pursue resume pursuit_1",
      },
    },
  }).payload;

  assert.equal(normalized.attempt.state, "admitted");
  assert.equal(normalized.resume_action.state, "busy");
  assert.equal(Object.hasOwn(normalized.attempt, "source_request_sha256"), false);
  assert.throws(
    () => normalizeServerRecord({
      type: "pursuit/recovery/action_result",
      payload: { ...normalized, status: "resolved" },
    }),
    /attempt 与状态不一致/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "pursuit/recovery/action_result",
      payload: {
        ...normalized,
        resume_action: {
          ...normalized.resume_action,
          command: "/pursue resume pursuit_other",
        },
      },
    }),
    /command 与 run_id 不一致/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "pursuit/recovery/action_result",
      payload: {
        ...normalized,
        status: "requested",
        attempt: {
          ...attempt,
          state: "requested",
        },
      },
    }),
    /requested Pursuit recovery attempt 携带了后续阶段事实/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "pursuit/recovery/action_result",
      payload: {
        ...normalized,
        status: "failed",
        attempt: {
          ...attempt,
          state: "failed",
          resolved_at: "2026-07-18T00:00:02+00:00",
          result_code: "runtime_exception",
          boundary_decision_id: "e".repeat(64),
        },
      },
    }),
    /failed Pursuit recovery attempt 不得携带边界裁判/,
  );
});

test("evolution review snapshot is strict and drops private fields", () => {
  const item = {
    candidate_id: `evc_${"a".repeat(24)}`, finding_code: "user_reported_defect",
    kind: "correctness", scope: "ui:footer", risk: "medium", occurrence_count: 2,
    source_kinds: ["user_feedback"], last_observed_at: "now", revision: 2,
    decision: "review_ready", review_ready: true, human_review_required: false,
    experiment_eligible: false, private_payload: "drop-me",
  };
  const normalized = normalizeServerRecord({ type: "evolution/review", payload: {
    schema_version: 1, mode: "list", filters: { query: "", risk: "", source_kind: "", limit: 50 },
    items: [item], selected: null, events: [], read_only: true,
  } }).payload;
  assert.equal(normalized.items[0].decision, "review_ready");
  assert.equal(Object.hasOwn(normalized.items[0], "private_payload"), false);
  assert.throws(() => normalizeServerRecord({ type: "evolution/review", payload: { ...normalized, mode: "write" } }), /mode/);
  assert.throws(() => normalizeServerRecord({ type: "evolution/review", payload: { ...normalized, read_only: false } }), /只读/);
  assert.throws(() => normalizeServerRecord({ type: "evolution/review", payload: { ...normalized, items: [{ ...item, experiment_eligible: true }] } }), /实验资格/);
  const proposalSource = {
    candidate_id: `evc_${"a".repeat(24)}`, candidate_revision: 2,
    candidate_sha256: "c".repeat(64), occurrence_count: 2,
    last_observed_at: "now", aggregation_policy: "candidate-aggregation-v1",
    trend: "insufficient",
  };
  const proposalId = `evp_${createHash("sha256").update(JSON.stringify({
    candidate_id: proposalSource.candidate_id,
    candidate_revision: proposalSource.candidate_revision,
    candidate_sha256: proposalSource.candidate_sha256,
    generator_version: "evolution-proposal-v1",
    proposal_kind: "code",
  })).digest("hex").slice(0, 24)}`;
  const detail = normalizeServerRecord({ type: "evolution/review", payload: {
    ...normalized, mode: "detail", items: [], events: [], selected: {
      ...item, status: "draft", hypothesis: "机械验证", providers: [], models: [], platforms: [],
      first_observed_at: "before", expected_metrics: [], evidence_refs: [],
      policy_version: "candidate-eligibility-v2", checks: [],
      governance: {
        policy_version: "proposal-governance-v1", allowed: true,
        reason: "no_active_cooldown", proposal_state: "", proposal_revision: 0,
        cooldown_until: "", significant_new_evidence: false,
        private_decision_note: "drop-me",
      },
      aggregation: {
        policy_version: "candidate-aggregation-v1", anchor_at: "now", span_seconds: 10,
        total_count: 2, count_24h: 2, count_7d: 2, count_30d: 2,
        previous_7d_count: 0, trend: "insufficient", source_counts: [],
        source_unique_count: 0,
        provider_counts: [{ value: "openai", count: 2, percentage: 100 }],
        provider_unique_count: 1, model_counts: [], model_unique_count: 0,
        platform_counts: [], platform_unique_count: 0, representatives: [],
      },
      proposal: {
        schema_version: 1, proposal_id: proposalId,
        generator_version: "evolution-proposal-v1", proposal_kind: "code",
        classification_reason: "fallback:code", title: "代码改进建议",
        summary: "机械验证", impact_scope: "ui:footer", intended_files: [],
        validation_plan: [{
          metric_name: "feedback.recurrence", direction: "decrease", target: 0,
          verifier: "feedback_recurrence", procedure: "比较反馈复发率。",
        }],
        risk_level: "medium", review_notes: ["eligibility:review_ready"],
        source: proposalSource,
        requires_human_review: true, executable: false,
        experiment_eligible: false, state: "preview",
      },
    },
  } }).payload;
  assert.equal(detail.selected.aggregation.provider_counts[0].count, 2);
  assert.equal(detail.selected.governance.reason, "no_active_cooldown");
  assert.equal(Object.hasOwn(detail.selected.governance, "private_decision_note"), false);
  assert.equal(detail.selected.proposal.proposal_kind, "code");
  assert.equal(detail.selected.proposal.executable, false);
  assert.throws(() => normalizeServerRecord({ type: "evolution/review", payload: {
    ...detail, selected: { ...detail.selected, aggregation: { ...detail.selected.aggregation, trend: "exploding" } },
  } }), /trend/);
  assert.throws(() => normalizeServerRecord({ type: "evolution/review", payload: {
    ...detail, selected: { ...detail.selected, proposal: { ...detail.selected.proposal, executable: true } },
  } }), /authority contract/);
  assert.throws(() => normalizeServerRecord({ type: "evolution/review", payload: {
    ...detail, selected: {
      ...detail.selected,
      proposal: { ...detail.selected.proposal, proposal_id: `evp_${"0".repeat(24)}` },
    },
  } }), /source snapshot/);
});

test("normalizes strict runtime inspector snapshots and updates", () => {
  const snapshot = inspectorSnapshotFixture(4);
  const normalized = normalizeServerRecord({
    type: "inspector/snapshot",
    payload: snapshot,
  }).payload;
  assert.equal(normalized.revision, 4);
  assert.equal(normalized.context.git_available, false);
  assert.equal(normalized.plan.items[0].subject, "实现 Inspector");

  const update = normalizeServerRecord({
    type: "inspector/update",
    payload: {
      schema_version: 1,
      session_id: "session-1",
      revision: 5,
      generated_at: "2026-07-13T00:00:01+00:00",
      changed_tabs: { tools: snapshot.tools },
    },
  }).payload;
  assert.deepEqual(Object.keys(update.changed_tabs), ["tools"]);
  assert.equal(update.changed_tabs.tools.items[0].call_id, "read-1");
});

test("rejects malformed runtime inspector state and unknown changed tabs", () => {
  const invalidState = inspectorSnapshotFixture(1);
  invalidState.plan.state = "invented";
  assert.throws(
    () => normalizeServerRecord({ type: "inspector/snapshot", payload: invalidState }),
    /plan.state/,
  );

  const snapshot = inspectorSnapshotFixture(1);
  assert.throws(
    () => normalizeServerRecord({
      type: "inspector/update",
      payload: {
        schema_version: 1,
        session_id: "session-1",
        revision: 2,
        generated_at: "now",
        changed_tabs: { surprise: snapshot.plan },
      },
    }),
    /未知 Inspector 标签/,
  );

  const invalidExitCode = inspectorSnapshotFixture(1);
  invalidExitCode.tests.validations = [{
    command: "pytest",
    scope: "unit",
    status: "failed",
    exit_code: "not-an-integer",
  }];
  assert.throws(
    () => normalizeServerRecord({ type: "inspector/snapshot", payload: invalidExitCode }),
    /exit_code 必须是整数/,
  );
});

test("normalizes strict agent control snapshots updates and actions", () => {
  const snapshot = agentControlSnapshotFixture(3);
  const normalized = normalizeServerRecord({
    type: "agents/snapshot",
    payload: snapshot,
  }).payload;
  assert.equal(normalized.revision, 3);
  assert.equal(normalized.agents[0].name, "coder");
  assert.equal(normalized.executions[0].stop_supported, true);
  assert.equal(normalized.executions[0].worker_backend, "independent");
  assert.equal(normalized.executions[0].heartbeat_phase, "running");
  assert.equal(normalized.executions[0].heartbeat_subject_id, "agent-execution-test");
  assert.equal(normalized.executions[0].worker_request_sha256, "a".repeat(64));
  assert.equal(normalized.executions[0].worker_result_sha256, "");
  assert.deepEqual(normalized.executions[0].worker_tool_scope, ["file_read"]);
  assert.equal(normalized.executions[0].worker_job_id, "agent-job-test");
  assert.equal(normalized.executions[0].worker_job_state, "running");
  assert.equal(normalized.executions[0].worker_claim_epoch, 2);
  assert.equal(normalized.summary.durable_capacity_configured, true);
  assert.equal(normalized.summary.durable_active_jobs, 1);
  assert.equal(normalized.summary.durable_waiting_jobs, 2);
  assert.equal(normalized.summary.durable_results_visible, 1);
  assert.equal(normalized.results[0].task_id, "result-task");
  assert.equal(normalized.results[0].delivery_sha256, "c".repeat(64));
  assert.equal(normalized.recovery_catalog.items[0].recovery_state, "recovery_required");
  assert.equal(normalized.recovery_catalog.items[1].recovery_state, "publication_quarantined");
  assert.equal(normalized.recovery_catalog.items[0].session_scope, "current");
  assert.equal(Object.hasOwn(normalized.recovery_catalog.items[0], "owner_id"), false);

  const update = normalizeServerRecord({
    type: "agents/update",
    payload: {
      schema_version: 6,
      session_id: "session-1",
      revision: 4,
      generated_at: "2026-07-13T00:00:01+00:00",
      changed_sections: { executions: snapshot.executions },
    },
  }).payload;
  assert.deepEqual(Object.keys(update.changed_sections), ["executions"]);

  const action = normalizeServerRecord({
    type: "agents/action",
    payload: {
      task_id: "task-1",
      accepted: false,
      code: "already_finished",
      message: "执行已结束。",
    },
  }).payload;
  assert.equal(action.accepted, false);
  assert.equal(action.code, "already_finished");

  const recoveryAction = normalizeServerRecord({
    type: "agents/recovery/action_result",
    payload: {
      action: "resolve_unknown",
      job_id: "agent-job-recovery",
      accepted: true,
      applied: true,
      code: "recovery_resolved_unknown",
      message: "已收口。",
      job_state: "unknown",
      claim_epoch: 2,
      receipt_sha256: "f".repeat(64),
      owner_id: "must-not-survive",
    },
  }).payload;
  assert.equal(recoveryAction.job_state, "unknown");
  assert.equal(recoveryAction.receipt_sha256, "f".repeat(64));
  assert.equal(Object.hasOwn(recoveryAction, "owner_id"), false);
});

test("rejects malformed agent control payloads and unknown sections", () => {
  const invalidBoolean = agentControlSnapshotFixture(1);
  invalidBoolean.executions[0].stop_supported = "yes";
  assert.throws(
    () => normalizeServerRecord({ type: "agents/snapshot", payload: invalidBoolean }),
    /stop_supported.*boolean/,
  );

  const stringRevision = agentControlSnapshotFixture(1);
  stringRevision.revision = "1";
  assert.throws(
    () => normalizeServerRecord({ type: "agents/snapshot", payload: stringRevision }),
    /revision.*非负整数/,
  );

  const nonStringTool = agentControlSnapshotFixture(1);
  nonStringTool.agents[0].tools = [42];
  assert.throws(
    () => normalizeServerRecord({ type: "agents/snapshot", payload: nonStringTool }),
    /agent.tools.*字符串/,
  );

  const unknownState = agentControlSnapshotFixture(1);
  unknownState.agents[0].state = "sleeping";
  assert.throws(
    () => normalizeServerRecord({ type: "agents/snapshot", payload: unknownState }),
    /agent.state 无效/,
  );

  const unknownStatus = agentControlSnapshotFixture(1);
  unknownStatus.executions[0].status = "paused";
  assert.throws(
    () => normalizeServerRecord({ type: "agents/snapshot", payload: unknownStatus }),
    /execution.status 无效/,
  );

  const unknownHeartbeat = agentControlSnapshotFixture(1);
  unknownHeartbeat.executions[0].heartbeat_phase = "guessing";
  assert.throws(
    () => normalizeServerRecord({ type: "agents/snapshot", payload: unknownHeartbeat }),
    /execution.heartbeat_phase 无效/,
  );

  const invalidWorkerDigest = agentControlSnapshotFixture(1);
  invalidWorkerDigest.executions[0].worker_request_sha256 = "not-a-digest";
  assert.throws(
    () => normalizeServerRecord({
      type: "agents/snapshot",
      payload: invalidWorkerDigest,
    }),
    /worker_request_sha256.*SHA-256/,
  );

  const oversizedWorkerScope = agentControlSnapshotFixture(1);
  oversizedWorkerScope.executions[0].worker_tool_scope = Array(257).fill("file_read");
  assert.throws(
    () => normalizeServerRecord({
      type: "agents/snapshot",
      payload: oversizedWorkerScope,
    }),
    /worker_tool_scope.*最多 256 项/,
  );

  const invalidWorkerJobState = agentControlSnapshotFixture(1);
  invalidWorkerJobState.executions[0].worker_job_state = "guessing";
  assert.throws(
    () => normalizeServerRecord({
      type: "agents/snapshot",
      payload: invalidWorkerJobState,
    }),
    /worker_job_state 无效/,
  );

  const invalidWorkerBackend = agentControlSnapshotFixture(1);
  invalidWorkerBackend.executions[0].worker_backend = "guessed";
  assert.throws(
    () => normalizeServerRecord({
      type: "agents/snapshot",
      payload: invalidWorkerBackend,
    }),
    /worker_backend 无效/,
  );

  const invalidWorkerClaimEpoch = agentControlSnapshotFixture(1);
  invalidWorkerClaimEpoch.executions[0].worker_claim_epoch = -1;
  assert.throws(
    () => normalizeServerRecord({
      type: "agents/snapshot",
      payload: invalidWorkerClaimEpoch,
    }),
    /worker_claim_epoch.*非负整数/,
  );

  const invalidResultDigest = agentControlSnapshotFixture(1);
  invalidResultDigest.results[0].delivery_sha256 = "not-a-digest";
  assert.throws(
    () => normalizeServerRecord({
      type: "agents/snapshot",
      payload: invalidResultDigest,
    }),
    /delivery_sha256.*SHA-256/,
  );

  const oversizedResults = agentControlSnapshotFixture(1);
  oversizedResults.results = Array(51).fill(oversizedResults.results[0]);
  assert.throws(
    () => normalizeServerRecord({
      type: "agents/snapshot",
      payload: oversizedResults,
    }),
    /results.*最多 50 项/,
  );

  const invalidRecoveryDigest = agentControlSnapshotFixture(1);
  invalidRecoveryDigest.recovery_catalog.items[0].receipt_sha256 = "invalid";
  assert.throws(
    () => normalizeServerRecord({
      type: "agents/snapshot",
      payload: invalidRecoveryDigest,
    }),
    /recovery.receipt_sha256.*SHA-256/,
  );

  const invalidRecoveryIdentity = agentControlSnapshotFixture(1);
  invalidRecoveryIdentity.recovery_catalog.items[0].publication_id = "publication-1";
  assert.throws(
    () => normalizeServerRecord({
      type: "agents/snapshot",
      payload: invalidRecoveryIdentity,
    }),
    /job recovery 标识不一致/,
  );

  assert.throws(
    () => normalizeServerRecord({
      type: "agents/recovery/action_result",
      payload: {
        action: "resolve_unknown",
        job_id: "agent-job-recovery",
        accepted: false,
        applied: true,
        code: "impossible",
        message: "invalid",
        job_state: "",
        claim_epoch: 0,
        receipt_sha256: "",
      },
    }),
    /applied.*rejected/,
  );

  const missingSection = agentControlSnapshotFixture(1);
  delete missingSection.blackboard;
  assert.throws(
    () => normalizeServerRecord({ type: "agents/snapshot", payload: missingSection }),
    /缺少 blackboard/,
  );

  const snapshot = agentControlSnapshotFixture(1);
  assert.throws(
    () => normalizeServerRecord({
      type: "agents/update",
      payload: {
        schema_version: 6,
        session_id: "session-1",
        revision: 2,
        generated_at: "now",
        changed_sections: { invented: [] },
      },
    }),
    /未知 Agent Control section/,
  );
});

function agentControlSnapshotFixture(revision) {
  return {
    schema_version: 6,
    session_id: "session-1",
    revision,
    generated_at: "2026-07-13T00:00:00+00:00",
    summary: {
      total_agents: 1,
      active_agents: 1,
      attention_agents: 0,
      stoppable_executions: 1,
      pending_messages: 0,
      durable_capacity_configured: true,
      durable_active_jobs: 1,
      durable_max_active_jobs: 4,
      durable_waiting_jobs: 2,
      durable_max_waiters: 64,
      durable_reclaimable_jobs: 0,
      durable_recovery_required_jobs: 0,
      durable_results_visible: 1,
      durable_publications_pending: 0,
      durable_publications_claimed: 0,
      durable_publications_expired: 0,
      durable_publications_quarantined: 0,
    },
    agents: [{
      name: "coder",
      description: "编程 Agent",
      kind: "preset",
      state: "running",
      task_count: 1,
      model_tier: "capable",
      capabilities: ["file_operations"],
      tools: ["file_read"],
      permission_level: "moderate",
      age_ms: 10,
      heartbeat_age_ms: 2,
    }],
    results: [{
      delivery_id: "delivery-1",
      publication_id: "publication-1",
      job_id: "agent-job-result",
      task_id: "result-task",
      agent_name: "coder",
      status: "completed",
      delivered_at: "2026-07-13T00:00:01+00:00",
      result_sha256: "b".repeat(64),
      delivery_sha256: "c".repeat(64),
      task_excerpt: "验证结果投影",
      response_excerpt: "结果正文",
      error_excerpt: "",
      content_truncated: false,
      response_bytes: 12,
      total_tokens: 8,
      total_cost_usd: 0.001,
      turns: 1,
      reason_code: "agent_completed",
    }],
    recovery_catalog: {
      assessed_at: "2026-07-13T00:00:02+00:00",
      items: [{
        kind: "job",
        item_id: "agent-job-recovery",
        job_id: "agent-job-recovery",
        publication_id: "",
        agent_name: "coder",
        job_state: "running",
        recovery_state: "recovery_required",
        session_scope: "current",
        claim_epoch: 3,
        claim_expires_at: "2026-07-13T00:00:01+00:00",
        attempt_count: 0,
        occurred_at: "2026-07-13T00:00:00+00:00",
        request_sha256: "d".repeat(64),
        receipt_sha256: "e".repeat(64),
        reason_code: "agent_job_running",
      }, {
        kind: "publication",
        item_id: "publication-quarantined",
        job_id: "agent-job-quarantined",
        publication_id: "publication-quarantined",
        agent_name: "coder",
        job_state: "completed",
        recovery_state: "publication_quarantined",
        session_scope: "current",
        claim_epoch: 4,
        claim_expires_at: "",
        attempt_count: 5,
        occurred_at: "2026-07-13T00:00:01+00:00",
        request_sha256: "f".repeat(64),
        receipt_sha256: "0".repeat(64),
        reason_code: "agent_publication_recovery_delivery_failed",
      }],
      truncated: false,
    },
    executions: [{
      task_id: "task-1",
      session_id: "session-1",
      agent_name: "coder",
      description: "实现功能",
      status: "running",
      phase: "running_tool",
      worker_backend: "independent",
      started_at: 1,
      finished_at: null,
      elapsed_ms: 10,
      heartbeat_age_ms: 2,
      heartbeat_subject_id: "agent-execution-test",
      heartbeat_phase: "running",
      heartbeat_failure_code: "",
      worker_request_sha256: "a".repeat(64),
      worker_result_sha256: "",
      worker_tool_scope: ["file_read"],
      worker_contract_failure_code: "",
      worker_job_id: "agent-job-test",
      worker_job_state: "running",
      worker_claim_epoch: 2,
      worker_job_failure_code: "",
      current_tool: "file_read",
      recent_tools: ["file_read"],
      total_tokens: 0,
      total_cost_usd: 0,
      turns: 0,
      error: "",
      stop_supported: true,
      stop_requested: false,
    }],
    team_messages: [],
    blackboard: [],
    warnings: [],
  };
}

function inspectorSnapshotFixture(revision) {
  return {
    schema_version: 1,
    session_id: "session-1",
    revision,
    generated_at: "2026-07-13T00:00:00+00:00",
    active_run_id: "run-1",
    plan: {
      state: "ready",
      items: [{ id: "1", subject: "实现 Inspector", status: "in_progress", blocked_by: [] }],
      next_actions: [],
      warnings: [],
    },
    tools: {
      state: "ready",
      items: [{ call_id: "read-1", name: "file_read", status: "success", duration_ms: 4 }],
      approvals: [],
      warnings: [],
    },
    context: {
      state: "ready",
      workspace_root: "/tmp/project",
      branch: "main",
      commit: "abc",
      git_available: false,
      git_dirty: false,
      context_used: 12,
      context_window: 100,
      context_percentage: 12,
      budget_used_usd: 0,
      budget_max_usd: 5,
      budget_percentage: 0,
      input_tokens: 1,
      output_tokens: 2,
      turns: 1,
      warnings: [],
    },
    changes: {
      state: "empty",
      items: [],
      git_state: { available: false, dirty: false },
      warnings: [],
    },
    tests: {
      state: "empty",
      validations: [],
      unverified: [],
      next_actions: [],
      warnings: [],
    },
  };
}

test("normalizeServerRecord stabilizes bridge payloads", () => {
  assert.deepEqual(normalizeServerRecord({
    id: 42,
    seq: 7,
    type: "user/message",
    version: "1",
    payload: { content: 123 },
  }), {
    id: "42",
    seq: 7,
    type: "user/message",
    version: 1,
    payload: { content: "123" },
  });

  for (const seq of ["7", 0, -1, true, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(
      () => normalizeServerRecord({ type: "user/message", seq, payload: { content: "x" } }),
      /seq 必须是正安全整数/,
    );
  }

  assert.deepEqual(normalizeServerRecord({
    type: "session/replayed",
    payload: { session_id: 100, title: null, message_count: "4", clear: "false" },
  }).payload, {
    session_id: "100",
    title: "",
    message_count: 4,
    clear: false,
    terminal_event_recovery: { mode: "legacy_snapshot" },
  });

  assert.deepEqual(normalizeServerRecord({
    type: "terminal_events/recovery",
    payload: {
      schema_version: 1,
      session_id: "session-cursor",
      mode: "replay_complete",
      stream_id: "tes_0123456789abcdef01234567",
      requested_cursor: 3,
      earliest_cursor: 1,
      latest_cursor: 5,
      replayed_count: 2,
    },
  }).payload, {
    schema_version: 1,
    session_id: "session-cursor",
    mode: "replay_complete",
    stream_id: "tes_0123456789abcdef01234567",
    requested_cursor: 3,
    earliest_cursor: 1,
    latest_cursor: 5,
    gap_reason: "",
    replayed_count: 2,
  });
  assert.throws(
    () => normalizeServerRecord({
      type: "terminal_events/recovery",
      payload: {
        schema_version: 1,
        session_id: "session-cursor",
        mode: "replay_complete",
        stream_id: "tes_0123456789abcdef01234567",
        requested_cursor: 3,
        earliest_cursor: 5,
        latest_cursor: 4,
        replayed_count: 1,
      },
    }),
    /cursor 边界无效/,
  );
  for (const payload of [
    {
      schema_version: 1,
      session_id: "session-cursor",
      mode: "replay_complete",
      stream_id: "",
      requested_cursor: 3,
      earliest_cursor: 1,
      latest_cursor: 5,
      replayed_count: 2,
    },
    {
      schema_version: 1,
      session_id: "session-cursor",
      mode: "replay_complete",
      stream_id: "tes_0123456789abcdef01234567",
      requested_cursor: 3,
      earliest_cursor: 1,
      latest_cursor: 5,
      replayed_count: 1,
    },
    {
      schema_version: 1,
      session_id: "session-cursor",
      mode: "snapshot_complete",
      stream_id: "tes_0123456789abcdef01234567",
      requested_cursor: 3,
      earliest_cursor: 4,
      latest_cursor: 5,
      replayed_count: 1,
    },
  ]) {
    assert.throws(
      () => normalizeServerRecord({
        type: "terminal_events/recovery",
        payload,
      }),
      /terminal event recovery/,
    );
  }

  assert.deepEqual(normalizeServerRecord({
    type: "permission/resolved",
    payload: { request_id: 99, choice: "BYPASS" },
  }).payload, {
    request_id: "99",
    choice: "bypass",
  });

  assert.deepEqual(normalizeServerRecord({
    type: "interaction/request",
    payload: {
      request_id: "ask-7",
      header: "实现策略",
      question: "请选择",
      options: [
        { value: "a", label: "A", description: null },
        { value: "b", label: "B", description: "保留兼容" },
      ],
      allow_custom: true,
      custom_label: null,
    },
  }).payload, {
    request_id: "ask-7",
    session_id: "",
    run_id: "",
    agent_name: "main",
    header: "实现策略",
    question: "请选择",
    options: [
      { value: "a", label: "A", description: "" },
      { value: "b", label: "B", description: "保留兼容" },
    ],
    allow_custom: true,
    custom_label: "其他",
    timeout_seconds: null,
    expires_at: "",
    status: "needs_input",
  });

  assert.throws(
    () => normalizeServerRecord({
      type: "interaction/request",
      payload: {
        request_id: "ask-bad",
        header: "实现策略",
        question: "请选择",
        options: [{ value: "same", label: "A" }, { value: "same", label: "B" }],
        allow_custom: false,
      },
    }),
    /不能重复/,
  );

  assert.deepEqual(normalizeServerRecord({
    type: "interaction/resolved",
    payload: {
      request_id: "ask-7",
      status: "answered",
      kind: "custom",
      value: "",
      label: "其他",
      custom_text: "仅当前工作区",
      private_payload: "drop",
    },
  }).payload, {
    request_id: "ask-7",
    kind: "custom",
    value: "",
    custom_text: "仅当前工作区",
    status: "answered",
    label: "其他",
  });
  assert.deepEqual(normalizeServerRecord({
    type: "interaction/resolved",
    payload: {
      request_id: "ask-8",
      status: "expired",
      reason: "等待用户回答超时。",
      private_payload: "drop",
    },
  }).payload, {
    request_id: "ask-8",
    status: "expired",
    reason: "等待用户回答超时。",
  });
  assert.throws(
    () => normalizeServerRecord({
      type: "interaction/resolved",
      payload: {
        request_id: "ask-8",
        status: "expired",
        reason: "等待用户回答超时。",
        kind: "option",
        value: "safe",
      },
    }),
    /不能携带答案字段/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "interaction/resolved",
      payload: {
        request_id: "ask-7",
        status: "answered",
        kind: "option",
        value: "safe",
        custom_text: "ambiguous",
        label: "安全方案",
      },
    }),
    /字段组合无效/,
  );

  assert.deepEqual(normalizeServerRecord({
    type: "permission/grants_changed",
    payload: { revoked: "2", grants: [{ grant_id: 7, tool_family: "shell" }, null] },
  }).payload, {
    revoked: 2,
    grants: [{ grant_id: "7", tool_family: "shell" }],
  });

  assert.deepEqual(normalizeServerRecord({
    type: "completion/receipt",
    payload: {
      schema_version: 1,
      receipt_id: "receipt-1",
      run_id: "run-1",
      outcome: "partial",
      summary: 42,
      changes: [
        { path: "src/example.py", status: "modified" },
        { path: ".naumi/terminal-ui-debug.jsonl", status: "modified", scope: "background" },
      ],
      validations: [{ command: "pytest", status: "failed", exit_code: "1" }],
      git_state: { available: 1, dirty: true, ahead: "2" },
    },
  }).payload, {
    schema_version: 1,
    receipt_id: "receipt-1",
    run_id: "run-1",
    outcome: "partial",
    summary: "42",
    changes: [
      { path: "src/example.py", status: "modified", scope: "task" },
      { path: ".naumi/terminal-ui-debug.jsonl", status: "modified", scope: "background" },
    ],
    validations: [{ command: "pytest", status: "failed", exit_code: "1" }],
    unverified: [],
    approvals: [],
    risks: [],
    git_state: { available: true, dirty: true, ahead: 2 },
    next_actions: [],
    evidence_refs: [],
    started_at: "",
    completed_at: "",
    duration_ms: 0,
  });
});

test("server sequence guard accepts contiguous records and quarantines gaps", () => {
  const guard = createServerSequenceGuard();
  assert.deepEqual(guard.observe({ seq: 99 }), {
    action: "accept",
    code: "preflight_baseline",
    lastSeq: 99,
  });
  assert.equal(guard.enable({ seq: 100 }).code, "contiguous");
  assert.equal(guard.observe({ seq: 101 }).code, "contiguous");
  assert.deepEqual(guard.observe({ seq: 101 }), {
    action: "ignore",
    code: "duplicate_sequence",
    expectedSeq: 102,
    receivedSeq: 101,
    lastSeq: 101,
  });
  assert.equal(guard.observe({ seq: 4 }).code, "out_of_order_sequence");
  assert.deepEqual(guard.observe({ seq: 109 }), {
    action: "desync",
    code: "sequence_gap",
    expectedSeq: 102,
    receivedSeq: 109,
    lastSeq: 101,
  });
  assert.equal(guard.observe({ seq: 102 }).action, "quarantine");
  assert.deepEqual(guard.snapshot(), { enabled: true, desynced: true, lastSeq: 101 });
});

test("server sequence guard fails closed on missing and invalid negotiated sequence", () => {
  const missing = createServerSequenceGuard();
  assert.equal(missing.enable({}).code, "missing_sequence");
  assert.equal(missing.observe({ seq: 1 }).action, "quarantine");

  const invalid = createServerSequenceGuard();
  assert.equal(invalid.enable({ seq: 1 }).action, "accept");
  assert.deepEqual(invalid.invalidate("oops"), {
    action: "desync",
    code: "invalid_sequence",
    expectedSeq: 2,
    receivedSeq: "oops",
    lastSeq: 1,
  });

  const startupGap = createServerSequenceGuard();
  assert.equal(startupGap.observe({ seq: 1 }).action, "accept");
  assert.equal(startupGap.observe({ seq: 3 }).action, "accept");
  assert.deepEqual(startupGap.enable({ seq: 4 }), {
    action: "desync",
    code: "sequence_gap",
    expectedSeq: 2,
    receivedSeq: 3,
    lastSeq: 3,
  });
});

test("normalizeServerRecord rejects invalid bridge records", () => {
  assert.throws(
    () => normalizeServerRecord({ type: "surprise", payload: {} }),
    /未知.*Bridge 事件/,
  );
  assert.throws(
    () => normalizeServerRecord({ type: "ready", version: 99, payload: {} }),
    /协议版本不兼容/,
  );
  assert.throws(
    () => normalizeServerRecord({ type: "ready", payload: [] }),
    /payload 必须是对象/,
  );
  assert.throws(
    () => normalizeServerRecord({ type: "ui/message", payload: {} }),
    /缺少 type/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "completion/receipt",
      payload: { schema_version: 2, receipt_id: "r", run_id: "run", outcome: "completed" },
    }),
    /schema_version/,
  );
});

test("unknown informational records consume only safe envelope metadata", () => {
  const normalized = normalizeServerRecord({
    type: "future/progress",
    version: 1,
    seq: 7,
    request_id: "future-1",
    criticality: "informational",
    top_level_secret: "must-also-drop",
    payload: {
      secret: "must-not-enter-state-or-debug-record",
      nested: { private: true },
    },
  });

  assert.equal(normalized.type, "future/progress");
  assert.equal(normalized.seq, 7);
  assert.equal(normalized.request_id, "future-1");
  assert.equal(normalized.criticality, "informational");
  assert.equal(normalized.unknown_informational, true);
  assert.deepEqual(normalized.payload, {});
  assert.equal(Object.hasOwn(normalized, "top_level_secret"), false);
  assert.throws(
    () => normalizeServerRecord({
      type: "future/completed",
      version: 1,
      seq: 8,
      criticality: "terminal",
      payload: {},
    }),
    /未知关键 Bridge 事件/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "ready",
      version: 1,
      criticality: "informational",
      payload: {},
    }),
    /criticality 与发布合同不一致/,
  );
});

test("normalizes authoritative terminal welcome identity fields", () => {
  const ready = normalizeServerRecord({
    type: "ready",
    version: 1,
    payload: {
      version: " 0.1.214 ",
      workspace_root: " /tmp/project ",
      model: " openai/gpt-5.4 ",
      provider: " openai ",
      api_format: " openai_responses ",
      upstream_model: " gpt-5.4-2026-06-01 ",
      mode: " DEFAULT ",
      permission_mode: " MODERATE ",
      reasoning_effort: {
        model: " openai/gpt-5.4 ",
        effective: " HIGH ",
        source: " GLOBAL ",
        supported: [" low ", " high "],
        default: " low ",
        warning: null,
      },
      model_contract: {
        requested_model: " openai/gpt-5.4 ",
        canonical_model: "openai/gpt-5.4",
        upstream_model: "gpt-5.4-2026-06-01",
        provider: "openai",
        api_format: "openai_responses",
        max_context: 256000,
        max_output: 32768,
        request_max_tokens: 4096,
        input_cost_per_million: 2.5,
        output_cost_per_million: 10,
        supports_tools: true,
        supports_streaming: true,
        supports_parallel_tools: true,
        supports_structured_output: true,
        supports_reasoning: true,
        supports_vision: false,
        input_modalities: ["text"],
        output_modalities: ["text"],
        field_sources: { max_context: "catalog" },
        status: "verified",
        warnings: [],
        errors: [],
      },
      protocol_registry: {
        contract_version: 1,
        registry_sha256: PROTOCOL_REGISTRY_SHA256,
        compatible_registry_sha256: [PROTOCOL_REGISTRY_SHA256],
        client_event_count: PROTOCOL_CONTRACT.client_events.length,
        server_event_count: PROTOCOL_CONTRACT.server_events.length,
      },
    },
  });
  const changed = normalizeServerRecord({
    type: "mode/changed",
    version: 1,
    payload: {
      mode: "bypass",
      status: {
        version: "0.1.214",
        workspace_root: "/tmp/project",
        model: "anthropic/claude-opus-4-6",
        mode: "bypass",
        permission_mode: "bypass",
      },
    },
  });
  const partial = normalizeServerRecord({
    type: "runtime/status",
    version: 1,
    payload: { model: " openai/gpt-5.4-mini " },
  });

  assert.deepEqual(
    {
      version: ready.payload.version,
      workspace_root: ready.payload.workspace_root,
      model: ready.payload.model,
      provider: ready.payload.provider,
      api_format: ready.payload.api_format,
      upstream_model: ready.payload.upstream_model,
      mode: ready.payload.mode,
      permission_mode: ready.payload.permission_mode,
      reasoning_effort: ready.payload.reasoning_effort,
      model_contract: ready.payload.model_contract,
      protocol_registry: ready.payload.protocol_registry,
    },
    {
      version: "0.1.214",
      workspace_root: "/tmp/project",
      model: "openai/gpt-5.4",
      provider: "openai",
      api_format: "openai_responses",
      upstream_model: "gpt-5.4-2026-06-01",
      mode: "default",
      permission_mode: "moderate",
      reasoning_effort: {
        model: "openai/gpt-5.4",
        effective: "high",
        source: "global",
        supported: ["low", "high"],
        default: "low",
        warning: null,
      },
      model_contract: {
        requested_model: "openai/gpt-5.4",
        canonical_model: "openai/gpt-5.4",
        upstream_model: "gpt-5.4-2026-06-01",
        provider: "openai",
        api_format: "openai_responses",
        max_context: 256000,
        max_output: 32768,
        request_max_tokens: 4096,
        input_cost_per_million: 2.5,
        output_cost_per_million: 10,
        supports_tools: true,
        supports_streaming: true,
        supports_parallel_tools: true,
        supports_structured_output: true,
        supports_reasoning: true,
        supports_vision: false,
        input_modalities: ["text"],
        output_modalities: ["text"],
        field_sources: { max_context: "catalog" },
        status: "verified",
        warnings: [],
        errors: [],
      },
      protocol_registry: {
        contract_version: 1,
        registry_sha256: PROTOCOL_REGISTRY_SHA256,
        compatible_registry_sha256: [PROTOCOL_REGISTRY_SHA256],
        compatibility: "exact",
        client_event_count: PROTOCOL_CONTRACT.client_events.length,
        server_event_count: PROTOCOL_CONTRACT.server_events.length,
      },
    },
  );
  assert.equal(changed.payload.status.model, "anthropic/claude-opus-4-6");
  assert.deepEqual(partial.payload, { model: "openai/gpt-5.4-mini" });

  const futureDigest = "f".repeat(64);
  const additive = normalizeServerRecord({
    type: "runtime/status",
    version: 1,
    payload: {
      protocol_registry: {
        contract_version: 1,
        registry_sha256: futureDigest,
        compatible_registry_sha256: [
          futureDigest,
          PROTOCOL_REGISTRY_SHA256,
        ],
        client_event_count: PROTOCOL_CONTRACT.client_events.length,
        server_event_count: PROTOCOL_CONTRACT.server_events.length + 1,
      },
    },
  });
  assert.equal(
    additive.payload.protocol_registry.compatibility,
    "attested_additive",
  );

  assert.throws(
    () => normalizeServerRecord({
      type: "runtime/status",
      version: 1,
      payload: {
        protocol_registry: {
          contract_version: 1,
          registry_sha256: "not-a-digest",
          client_event_count: 29,
          server_event_count: 41,
        },
      },
    }),
    /必须是 SHA-256/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "runtime/status",
      version: 1,
      payload: {
        protocol_registry: {
          contract_version: 1,
          registry_sha256: "0".repeat(64),
          client_event_count: 29,
          server_event_count: 41,
        },
      },
    }),
    /与内置协议不一致/,
  );
});

test("runtime status validates canonical navigation page metadata", () => {
  const validPage = {
    schema_version: 1,
    page_id: "goals",
    command: "/goal",
    label: "Goal",
    description: "查看持久 Goal、Pursuit 状态与阻塞信息。",
    keywords: ["goal", "pursuit", "目标"],
    order: 20,
    surface: "new_ui",
  };
  const record = normalizeServerRecord({
    type: "runtime/status",
    version: 1,
    payload: { navigation_pages: [validPage] },
  });

  assert.deepEqual(record.payload.navigation_pages, [validPage]);
  for (const invalid of [
    [{ ...validPage, command: "/goal now" }],
    [{ ...validPage, surface: "tui" }],
    [{ ...validPage, private_payload: "must-reject" }],
    [{ ...validPage, keywords: ["目标", "goal"] }],
    [validPage, { ...validPage }],
  ]) {
    assert.throws(
      () => normalizeServerRecord({
        type: "runtime/status",
        version: 1,
        payload: { navigation_pages: invalid },
      }),
      /navigation_pages/,
    );
  }
  assert.deepEqual(
    normalizeServerRecord({
      type: "runtime/status",
      version: 1,
      payload: {},
    }).payload,
    {},
  );
});

test("normalizes and validates evolution patch recovery status", () => {
  const record = normalizeServerRecord({
    type: "ready",
    version: 1,
    payload: {
      evolution_patch_recovery: {
        total: 3,
        single_file_total: 2,
        multi_file_total: 1,
        completed: 1,
        rolled_back: 1,
        already_baseline: 0,
        orphan_lock_removed: 0,
        deferred: 1,
        failed: 1,
        filesystem_changed: 1,
        failure_codes: ["journal_corrupt"],
      },
    },
  });

  assert.deepEqual(record.payload.evolution_patch_recovery, {
    total: 3,
    single_file_total: 2,
    multi_file_total: 1,
    completed: 1,
    rolled_back: 1,
    already_baseline: 0,
    orphan_lock_removed: 0,
    deferred: 1,
    failed: 1,
    filesystem_changed: 1,
    failure_codes: ["journal_corrupt"],
  });
  assert.throws(
    () => normalizeServerRecord({
      type: "ready",
      version: 1,
      payload: {
        evolution_patch_recovery: {
          total: 1,
          completed: 1,
          rolled_back: 0,
          already_baseline: 0,
          orphan_lock_removed: 0,
          deferred: 0,
          failed: 0,
          filesystem_changed: 0,
          failure_codes: [],
        },
      },
    }),
    /completed 与完成分类不一致/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "ready",
      version: 1,
      payload: {
        evolution_patch_recovery: {
          total: 2,
          single_file_total: 0,
          multi_file_total: 1,
          completed: 0,
          rolled_back: 0,
          already_baseline: 0,
          orphan_lock_removed: 0,
          deferred: 2,
          failed: 0,
          filesystem_changed: 0,
          failure_codes: [],
        },
      },
    }),
    /单\/多文件事务分类不一致/,
  );
});

test("normalizes and verifies bounded Sandbox retry startup recovery", () => {
  const ready = sandboxRetryRecoveryPayload();
  const record = normalizeServerRecord({
    type: "ready",
    version: 1,
    payload: { sandbox_retry_recovery: ready },
  });

  assert.deepEqual(record.payload.sandbox_retry_recovery, ready);
  assert.equal(
    record.payload.sandbox_retry_recovery.items[0].can_resume,
    true,
  );

  const commandTamper = structuredClone(ready);
  commandTamper.items[0].resume_command = "/harness eval sandbox resume forged";
  assert.throws(
    () => normalizeServerRecord({
      type: "ready",
      version: 1,
      payload: { sandbox_retry_recovery: commandTamper },
    }),
    /resume_command 与持久事实不一致/,
  );

  const countTamper = structuredClone(ready);
  countTamper.counts.actionable = 0;
  assert.throws(
    () => normalizeServerRecord({
      type: "ready",
      version: 1,
      payload: { sandbox_retry_recovery: countTamper },
    }),
    /actionable\/total 不一致/,
  );

  const digestTamper = structuredClone(ready);
  digestTamper.snapshot_sha256 = "f".repeat(64);
  assert.throws(
    () => normalizeServerRecord({
      type: "ready",
      version: 1,
      payload: { sandbox_retry_recovery: digestTamper },
    }),
    /摘要或 snapshot_id 不一致/,
  );

  const unavailable = sandboxRetryRecoveryPayload({ status: "unavailable" });
  assert.deepEqual(
    normalizeServerRecord({
      type: "ready",
      version: 1,
      payload: { sandbox_retry_recovery: unavailable },
    }).payload.sandbox_retry_recovery,
    unavailable,
  );
});

test("normalizes bounded retention worker runtime status", () => {
  const record = normalizeServerRecord({
    type: "runtime/status",
    version: 1,
    payload: {
      retention_worker: {
        configured_enabled: true,
        owner_id: "worker-1",
        state: "WAITING",
        lease_held: true,
        pass_count: 2,
        completed_session_count: 1,
        retry_scheduled_count: 0,
        failure_count: 0,
        consecutive_empty_passes: 0,
        next_delay_seconds: 12.5,
        last_pass_status: "completed",
        last_error_code: "",
        started_at: "2026-07-18T00:00:00+00:00",
        last_pass_at: "2026-07-18T00:00:01+00:00",
      },
    },
  });

  assert.equal(record.payload.retention_worker.state, "waiting");
  assert.equal(record.payload.retention_worker.pass_count, 2);
  assert.throws(
    () => normalizeServerRecord({
      type: "runtime/status",
      version: 1,
      payload: {
        retention_worker: {
          ...record.payload.retention_worker,
          state: "unknown",
        },
      },
    }),
    /retention_worker.state 无效/,
  );
});

test("rejects non-string terminal welcome identity fields", () => {
  assert.throws(
    () => normalizeServerRecord({
      type: "ready",
      version: 1,
      payload: { version: { injected: true } },
    }),
    /ready.version 必须是字符串/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "ready",
      version: 1,
      payload: { api_format: { injected: true } },
    }),
    /ready.api_format 必须是字符串/,
  );
  assert.throws(
    () => normalizeServerRecord({
      type: "ready",
      version: 1,
      payload: {
        reasoning_effort: {
          model: "gpt-5",
          effective: "high",
          source: "global",
          supported: [{ injected: true }],
          default: null,
          warning: null,
        },
      },
    }),
    /supported\[0\] 必须是字符串/,
  );
});

test("event sender accepts explicit missing-receipt recovery requests", () => {
  const chunks = [];
  const send = createEventSender({ write: (chunk) => chunks.push(chunk) });

  send("receipt/request", {
    session_id: "session-1",
    receipt_id: "receipt-missing",
    run_id: "run-missing",
  });

  assert.deepEqual(JSON.parse(chunks[0]).payload, {
    session_id: "session-1",
    receipt_id: "receipt-missing",
    run_id: "run-missing",
  });
});

test("normalizes workbench snapshot events", () => {
  const proposal = {
    id: "proposal-1", session_id: "s", mission_id: "m1", task_id: "7",
    agent_id: "Evolution-Agent", title: "优化 footer", impact_scope: "ui:footer",
    intended_files: ["frontend/footer.js"], validation_plan: ["node --test footer"],
    risk_level: "medium", questions: [], state: "open", decision_note: "",
    source_kind: "evolution_candidate", source_id: `evc_${"a".repeat(24)}`,
    source_revision: 2, source_occurrence_count: 4,
    source_proposal_id: `evp_${"b".repeat(24)}`, proposal_kind: "code",
    reviewer: "", decision_at: "", cooldown_until: "", merged_into_id: "",
    merge_target_ids: ["proposal-2"],
    governance_policy_version: "", created_at: "now", updated_at: "now",
    private_prompt: "drop-me",
  };
  const record = normalizeServerRecord({
    type: "workbench/snapshot",
    version: 1,
    payload: {
      schema_version: 1,
      stream_id: "stream-a",
      revision: 3,
      generated_at: "2026-07-17T12:00:00+08:00",
      full: true,
      session_id: "s",
      counts: { tasks: "2", worktrees: 1, reviews: 1 },
      active_selection: { task_id: 7, mission_id: "m1", review_id: "proposal-1", review_kind: "proposal" },
      worktrees_status: "ready",
      worktrees_code: "",
      worktrees_total: 1,
      worktrees_truncated: false,
      worktrees: [{
        name: "wt-1", path: "/repo/wt-1", branch: "codex/wt-1", status: "dirty",
        task_id: "7", dirty_files: 2, commits_ahead: 1, removable: false,
        task: { id: "7", subject: "协议", private_prompt: "do not expose" },
        lease: { id: "lease-1", state: "active", private_token: "secret" }, agent_id: "Agent-1",
      }],
      missions: [{ id: "m1", title: "Mac 工作台" }],
      issues: [],
      tasks: [],
      proposals: [proposal],
      failures: [],
      events: [],
    },
  });

  assert.equal(record.payload.session_id, "s");
  assert.equal(record.payload.stream_id, "stream-a");
  assert.equal(record.payload.revision, 3);
  assert.deepEqual(record.payload.counts, {
    missions: 0,
    tasks: 2,
    worktrees: 1,
    reviews: 1,
    failures: 0,
  });
  assert.equal(record.payload.active_selection.task_id, "7");
  assert.equal(record.payload.active_selection.review_kind, "proposal");
  assert.equal(record.payload.proposals[0].source_revision, 2);
  assert.deepEqual(record.payload.proposals[0].merge_target_ids, ["proposal-2"]);
  assert.equal(Object.hasOwn(record.payload.proposals[0], "private_prompt"), false);
  assert.equal(record.payload.missions[0].title, "Mac 工作台");
  assert.equal(record.payload.worktrees_status, "ready");
  assert.equal(record.payload.worktrees_total, 1);
  assert.equal(record.payload.worktrees_truncated, false);
  assert.equal(record.payload.worktrees[0].dirty_files, 2);
  assert.equal(record.payload.worktrees[0].removable, false);
  assert.equal(record.payload.worktrees[0].agent_id, "Agent-1");
  assert.equal(record.payload.worktrees[0].task.private_prompt, undefined);
  assert.equal(record.payload.worktrees[0].lease.private_token, undefined);

  for (const mergeTargetIds of [
    ["proposal-2", "proposal-2"],
    ["proposal-2\nforged"],
    ["x".repeat(129)],
    Array.from({ length: 21 }, (_, index) => `proposal-${index}`),
  ]) {
    assert.throws(() => normalizeServerRecord({
      type: "workbench/snapshot",
      payload: {
        ...record.payload,
        proposals: [{ ...proposal, merge_target_ids: mergeTargetIds }],
      },
    }), /merge_target_ids/);
  }
});

test("normalizes strict workbench proposal action results", () => {
  const payload = normalizeServerRecord({
    type: "workbench/proposal/action_result",
    payload: {
      schema_version: 1,
      session_id: "s",
      proposal_id: "proposal-1",
      action: "reject",
      status: "completed",
      message: "Proposal 已拒绝。",
      proposal: {
        id: "proposal-1", session_id: "s", mission_id: "m1", task_id: "t1",
        agent_id: "Evolution-Agent", title: "优化 footer", impact_scope: "ui:footer",
        intended_files: [], validation_plan: [], risk_level: "medium", questions: [],
        state: "rejected", decision_note: "证据不足", source_kind: "manual",
        source_id: "", source_revision: 0, source_occurrence_count: 0,
        source_proposal_id: "", proposal_kind: "code", reviewer: "Human",
        decision_at: "now", cooldown_until: "later", merged_into_id: "",
        governance_policy_version: "proposal-governance-v1", created_at: "now",
        updated_at: "now", private_audit: "drop-me",
      },
      workbench_snapshot: null,
    },
  }).payload;

  assert.equal(payload.status, "completed");
  assert.equal(payload.proposal.state, "rejected");
  assert.equal(Object.hasOwn(payload.proposal, "private_audit"), false);
  assert.throws(() => normalizeServerRecord({
    type: "workbench/proposal/action_result",
    payload: { ...payload, status: "invented" },
  }), /status/);

  const deferred = normalizeServerRecord({
    type: "workbench/proposal/action_result",
    payload: {
      ...payload,
      action: "defer",
      message: "Proposal 已延后。",
      proposal: { ...payload.proposal, state: "deferred" },
    },
  }).payload;
  assert.equal(deferred.action, "defer");
  assert.equal(deferred.proposal.state, "deferred");

  const merged = normalizeServerRecord({
    type: "workbench/proposal/action_result",
    payload: {
      ...payload,
      action: "merge",
      message: "Proposal 已合并。",
      proposal: {
        ...payload.proposal,
        state: "merged",
        merged_into_id: "proposal-2",
      },
    },
  }).payload;
  assert.equal(merged.action, "merge");
  assert.equal(merged.proposal.merged_into_id, "proposal-2");
  for (const invalidProposal of [
    null,
    { ...payload.proposal, state: "open", merged_into_id: "proposal-2" },
    { ...payload.proposal, state: "merged", merged_into_id: "" },
    { ...payload.proposal, id: "proposal-other", state: "merged", merged_into_id: "proposal-2" },
  ]) {
    assert.throws(() => normalizeServerRecord({
      type: "workbench/proposal/action_result",
      payload: {
        ...payload,
        action: "merge",
        proposal: invalidProposal,
      },
    }), /merge 绑定无效/);
  }

  const contract = {
    schema_version: 1,
    authority_id: `evxauth_${"a".repeat(24)}`,
    authority_sha256: "b".repeat(64),
    contract_id: `evx_${"c".repeat(24)}`,
    manifest_sha256: "d".repeat(64),
    proposal_id: "proposal-1",
    candidate_id: `evc_${"e".repeat(24)}`,
    candidate_revision: 3,
    impact_scope: "frontend/terminal-ui/src/components/footer.js:renderFooter",
    allowed_files: ["frontend/terminal-ui/src/components/footer.js"],
    budget: {
      policy_version: "evolution-experiment-budget-v1",
      max_changed_files: 1,
      max_changed_lines: 120,
      max_tool_calls: 20,
      max_duration_seconds: 600,
      max_attempts: 2,
    },
    execution_ready: false,
    promotion_ready: false,
  };
  const issued = normalizeServerRecord({
    type: "workbench/proposal/action_result",
    payload: {
      schema_version: 1,
      session_id: "s",
      proposal_id: "proposal-1",
      action: "issue_contract",
      status: "completed",
      message: "Experiment Contract 已持久化。",
      proposal: null,
      experiment_contract: contract,
      workbench_snapshot: null,
    },
  }).payload;
  assert.deepEqual(issued.experiment_contract, contract);
  assert.throws(() => normalizeServerRecord({
    type: "workbench/proposal/action_result",
    payload: {
      schema_version: 1, session_id: "s", proposal_id: "proposal-1",
      action: "issue_contract", status: "completed", message: "missing",
      proposal: null, experiment_contract: null, workbench_snapshot: null,
    },
  }), /绑定无效/);
  assert.throws(() => normalizeServerRecord({
    type: "workbench/proposal/action_result",
    payload: {
      schema_version: 1, session_id: "s", proposal_id: "other-proposal",
      action: "issue_contract", status: "completed", message: "mismatch",
      proposal: null, experiment_contract: contract, workbench_snapshot: null,
    },
  }), /绑定无效/);
  for (const invalid of [
    { ...contract, execution_ready: true },
    { ...contract, allowed_files: ["../secret"] },
    { ...contract, impact_scope: "/private/secret" },
    { ...contract, allowed_files: Array.from({ length: 17 }, (_, index) => `src/f${index}.py`) },
    { ...contract, allowed_files: ["src/a.py", "src/a.py"] },
    {
      ...contract,
      allowed_files: ["src/a.py", "src/b.py"],
      budget: { ...contract.budget, max_changed_files: 1 },
    },
  ]) {
    assert.throws(() => normalizeServerRecord({
      type: "workbench/proposal/action_result",
      payload: {
        schema_version: 1,
        session_id: "s",
        proposal_id: "proposal-1",
        action: "issue_contract",
        status: "completed",
        message: "invalid",
        proposal: null,
        experiment_contract: invalid,
        workbench_snapshot: null,
      },
    }), /experiment contract/);
  }
});

test("normalizes workbench event payloads", () => {
  const record = normalizeServerRecord({
    type: "workbench/event",
    version: 1,
    payload: {
      id: "evt-1",
      type: "issue.claimed",
      actor: "Backend-Agent",
      subject_id: "1",
      payload: { lease_id: "lease-1" },
      timestamp: "2026-06-27T10:00:00",
      stream_id: "stream-a",
      revision: 4,
    },
  });

  assert.equal(record.payload.id, "evt-1");
  assert.equal(record.payload.type, "issue.claimed");
  assert.equal(record.payload.actor, "Backend-Agent");
  assert.equal(record.payload.stream_id, "stream-a");
  assert.equal(record.payload.revision, 4);
  assert.equal(record.payload.subject_id, "1");
  assert.equal(record.payload.payload.lease_id, "lease-1");
  assert.equal(record.payload.timestamp, "2026-06-27T10:00:00");
});

test("normalizes bounded workbench review evidence and rejects mismatches", () => {
  const payload = {
    schema_version: 1,
    session_id: "s",
    review_id: "approval-1",
    status: "ready",
    code: "",
    evidence: {
      approval: {
        id: "approval-1", session_id: "s", mission_id: "mission-1", task_id: "task-1",
        state: "waiting", title: "审查", detail: "检查", requester: "Agent",
        reviewer: "", decision_note: "", created_at: "now", updated_at: "now", private: "secret",
      },
      issue: {
        id: "issue-1", session_id: "s", mission_id: "mission-1", task_id: "task-1",
        risk_level: "high", related_branch: "main", related_worktree: "wt", related_pr: "",
        private: "drop",
      },
      worktree: { name: "wt", path: "/repo/wt", status: "present" },
      validation_runs: [{
        id: "run-1", status: "failed", command: ["node", "--test"], exit_code: 1,
        started_at: "2026-07-18T00:00:00Z", completed_at: "2026-07-18T00:00:01Z",
      }],
      changed_files: [{ path: "src/ui.js", status: "modified" }],
      diff_hunks: [{ path: "src/ui.js", patch: "-old\n+new" }],
      agent_notes: [{ actor: "Agent", note: "检查完成", type: "review.note", timestamp: "now", secret: "drop" }],
      events: [],
    },
  };
  const record = normalizeServerRecord({ type: "workbench/review", version: 1, payload });

  assert.equal(record.payload.evidence.approval.private, undefined);
  assert.equal(record.payload.evidence.issue.private, undefined);
  assert.equal(record.payload.evidence.diff_hunks[0].patch, "-old\n+new");
  assert.equal(record.payload.evidence.agent_notes[0].secret, undefined);
  assert.throws(
    () => normalizeServerRecord({
      type: "workbench/review",
      version: 1,
      payload: {
        ...payload,
        evidence: {
          ...payload.evidence,
          approval: { ...payload.evidence.approval, id: "other" },
        },
      },
    }),
    /id 不匹配/,
  );
});

test("jsonl reader emits complete lines across chunk boundaries", () => {
  const stream = new EventEmitter();
  const lines = [];
  attachJsonlLineReader(stream, (line) => lines.push(line));

  stream.emit("data", Buffer.from('{"a":'));
  stream.emit("data", Buffer.from("1}\n{\"b\":2}\r\n"));

  assert.deepEqual(lines, ['{"a":1}', '{"b":2}']);
});
