import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import {
  attachJsonlLineReader,
  createEventSender,
  normalizeBudgetStatus,
  normalizeServerRecord,
  parseArgs,
  parseBridgeCommandJson,
  PROTOCOL_CONTRACT,
  PROTOCOL_VERSION,
  splitShellLike,
} from "../src/protocol.js";

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
  });
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
  assert(PROTOCOL_CONTRACT.client_events.includes("submit"));
  assert(PROTOCOL_CONTRACT.client_events.includes("task_panel"));
  assert(PROTOCOL_CONTRACT.client_events.includes("run_cancel"));
  assert(PROTOCOL_CONTRACT.client_events.includes("receipt/request"));
  assert(PROTOCOL_CONTRACT.client_events.includes("inspector/request"));
  assert(PROTOCOL_CONTRACT.client_events.includes("agents/request"));
  assert(PROTOCOL_CONTRACT.client_events.includes("agents/stop"));
  assert(PROTOCOL_CONTRACT.server_events.includes("ui/message"));
  assert(PROTOCOL_CONTRACT.server_events.includes("runtime/status"));
  assert(PROTOCOL_CONTRACT.server_events.includes("run/cancelled"));
  assert(PROTOCOL_CONTRACT.server_events.includes("completion/receipt"));
  assert(PROTOCOL_CONTRACT.server_events.includes("inspector/snapshot"));
  assert(PROTOCOL_CONTRACT.server_events.includes("inspector/update"));
  assert(PROTOCOL_CONTRACT.server_events.includes("agents/snapshot"));
  assert(PROTOCOL_CONTRACT.server_events.includes("agents/update"));
  assert(PROTOCOL_CONTRACT.server_events.includes("agents/action"));
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
      changes: null,
      validations: [{ command: "pytest", status: "failed", exit_code: "1" }],
      git_state: { available: 1, dirty: true, ahead: "2" },
    },
  }).payload, {
    schema_version: 1,
    receipt_id: "receipt-1",
    run_id: "run-1",
    outcome: "partial",
    summary: "42",
    changes: [],
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
      mode: " DEFAULT ",
      permission_mode: " MODERATE ",
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
      mode: ready.payload.mode,
      permission_mode: ready.payload.permission_mode,
    },
    {
      version: "0.1.214",
      workspace_root: "/tmp/project",
      model: "openai/gpt-5.4",
      mode: "default",
      permission_mode: "moderate",
    },
  );
  assert.equal(changed.payload.status.model, "anthropic/claude-opus-4-6");
  assert.deepEqual(partial.payload, { model: "openai/gpt-5.4-mini" });
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
      session_id: "s",
      missions: [{ id: "m1", title: "Mac 工作台" }],
      issues: [],
      tasks: [],
      failures: [],
      events: [],
    },
  });

  assert.equal(record.payload.session_id, "s");
  assert.equal(record.payload.missions[0].title, "Mac 工作台");
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
    },
  });

  assert.equal(record.payload.id, "evt-1");
  assert.equal(record.payload.type, "issue.claimed");
  assert.equal(record.payload.actor, "Backend-Agent");
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
