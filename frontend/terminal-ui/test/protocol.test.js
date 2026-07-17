import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import {
  attachJsonlLineReader,
  createHelloPayload,
  createEventSender,
  eventPolicy,
  normalizeBudgetStatus,
  normalizeServerRecord,
  parseArgs,
  parseBridgeCommandJson,
  PROTOCOL_CONTRACT,
  PROTOCOL_REGISTRY_SHA256,
  PROTOCOL_VERSION,
  splitShellLike,
  validateEventRegistry,
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
      private_payload: "must-drop",
    }],
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

test("protocol contract drives client and server event validation", () => {
  assert.equal(PROTOCOL_VERSION, PROTOCOL_CONTRACT.version);
  assert.deepEqual(PROTOCOL_CONTRACT.negotiation, {
    minimum_version: 1,
    maximum_version: 1,
    capabilities: ["heartbeat", "typed_ui_messages", "workbench_snapshot"],
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
  assert(PROTOCOL_CONTRACT.server_events.includes("doctor/health"));
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
    capabilities: ["heartbeat", "typed_ui_messages", "workbench_snapshot"],
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
      history: [],
      warnings: [],
    },
  }).payload;

  assert.equal(normalized.pending.length, 50);
  assert.equal(normalized.pending[0].policy.source, "TOOL_PERMISSIONS:bash_run");
  assert.equal(Object.hasOwn(normalized.pending[0], "private_payload"), false);
  assert.throws(
    () => normalizeServerRecord({
      type: "permissions/snapshot",
      payload: { ...normalized, permission_mode: "invented" },
    }),
    /permission_mode/,
  );
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

  const update = normalizeServerRecord({
    type: "agents/update",
    payload: {
      schema_version: 1,
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
        schema_version: 1,
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
    schema_version: 1,
    session_id: "session-1",
    revision,
    generated_at: "2026-07-13T00:00:00+00:00",
    summary: {
      total_agents: 1,
      active_agents: 1,
      attention_agents: 0,
      stoppable_executions: 1,
      pending_messages: 0,
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
    executions: [{
      task_id: "task-1",
      session_id: "session-1",
      agent_name: "coder",
      description: "实现功能",
      status: "running",
      phase: "running_tool",
      started_at: 1,
      finished_at: null,
      elapsed_ms: 10,
      heartbeat_age_ms: 2,
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
    seq: "7",
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

  assert.deepEqual(normalizeServerRecord({
    type: "session/replayed",
    payload: { session_id: 100, title: null, message_count: "4", clear: "false" },
  }).payload, {
    session_id: "100",
    title: "",
    message_count: 4,
    clear: false,
  });

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
      request_id: 7,
      header: "实现策略",
      question: "请选择",
      options: [{ value: 1, label: "A", description: null }],
      allow_custom: 1,
      custom_label: null,
    },
  }).payload, {
    request_id: "7",
    header: "实现策略",
    question: "请选择",
    options: [{ value: "1", label: "A", description: "" }],
    allow_custom: true,
    custom_label: "其他",
  });

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

test("normalizeServerRecord rejects invalid bridge records", () => {
  assert.throws(
    () => normalizeServerRecord({ type: "surprise", payload: {} }),
    /未知 Bridge 事件/,
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
        client_event_count: 27,
        server_event_count: 38,
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
        client_event_count: 27,
        server_event_count: 38,
      },
    },
  );
  assert.equal(changed.payload.status.model, "anthropic/claude-opus-4-6");
  assert.deepEqual(partial.payload, { model: "openai/gpt-5.4-mini" });

  assert.throws(
    () => normalizeServerRecord({
      type: "runtime/status",
      version: 1,
      payload: {
        protocol_registry: {
          contract_version: 1,
          registry_sha256: "not-a-digest",
          client_event_count: 27,
          server_event_count: 38,
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
          client_event_count: 27,
          server_event_count: 38,
        },
      },
    }),
    /与内置协议不一致/,
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
      active_selection: { task_id: 7, mission_id: "m1" },
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
  assert.equal(record.payload.missions[0].title, "Mac 工作台");
  assert.equal(record.payload.worktrees_status, "ready");
  assert.equal(record.payload.worktrees_total, 1);
  assert.equal(record.payload.worktrees_truncated, false);
  assert.equal(record.payload.worktrees[0].dirty_files, 2);
  assert.equal(record.payload.worktrees[0].removable, false);
  assert.equal(record.payload.worktrees[0].agent_id, "Agent-1");
  assert.equal(record.payload.worktrees[0].task.private_prompt, undefined);
  assert.equal(record.payload.worktrees[0].lease.private_token, undefined);
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

test("jsonl reader emits complete lines across chunk boundaries", () => {
  const stream = new EventEmitter();
  const lines = [];
  attachJsonlLineReader(stream, (line) => lines.push(line));

  stream.emit("data", Buffer.from('{"a":'));
  stream.emit("data", Buffer.from("1}\n{\"b\":2}\r\n"));

  assert.deepEqual(lines, ['{"a":1}', '{"b":2}']);
});
