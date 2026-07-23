#!/usr/bin/env node
import fs from "node:fs";
import process from "node:process";

import { attachJsonlLineReader } from "../../src/protocol.js";

const markerPath = String(process.env.NAUMI_TEST_BRIDGE_RESTART_MARKER ?? "");
if (!markerPath) throw new Error("NAUMI_TEST_BRIDGE_RESTART_MARKER 未配置");
const generation = fs.existsSync(markerPath) ? 2 : 1;
if (generation === 1) fs.writeFileSync(markerPath, "first\n", "utf8");

let sequence = 1;
let exitTimer = null;

attachJsonlLineReader(process.stdin, (line) => {
  if (!line.trim()) return;
  const record = JSON.parse(line);
  const payload = record.payload ?? {};

  if (record.type === "hello") {
    emit("ack", {
      event: "hello",
      negotiation: {
        selected_version: 1,
        server_minimum_version: 1,
        server_maximum_version: 1,
        capabilities: ["heartbeat", "sequence_integrity", "typed_ui_messages"],
      },
    }, record.id);
    emit("ready", statusPayload());
    if (generation === 1 && process.env.NAUMI_TEST_BRIDGE_EXIT_DURING_RUN !== "1") {
      exitTimer = setTimeout(() => process.exit(17), 120);
    }
    return;
  }

  if (record.type === "ping") {
    emit("pong", {}, record.id);
    return;
  }

  if (record.type === "submit" && process.env.NAUMI_TEST_BRIDGE_EXIT_DURING_RUN === "1") {
    emit("user/message", { content: String(payload.text ?? "") }, record.id);
    emit("run/started", { task: String(payload.text ?? "") }, record.id);
    setTimeout(() => process.exit(23), 20);
    return;
  }

  if (record.type === "resume" && generation === 2) {
    emit("session/replayed", {
      session_id: payload.session_id,
      title: "Bridge 重连恢复",
      message_count: 0,
      clear: payload.clear === true,
    }, record.id);
    emit("harness/receipt", {
      schema_version: 1,
      revision: 4,
      run_id: "run-reconnected-1",
      status: "completed_verified",
      task_kind: "change",
      changed_files: ["src/recovered.py"],
      checks: [{ id: "focused", status: "passed", tree_fingerprint: "a".repeat(64) }],
      criteria: [{ id: "receipt", status: "satisfied", evidence_ids: ["evidence-1"] }],
      warnings: [],
      tree_fingerprint: "b".repeat(64),
    }, record.id);
    emit("completion/receipt", {
      schema_version: 1,
      receipt_id: "receipt-reconnected-1",
      run_id: "run-reconnected-1",
      outcome: "completed",
      summary: "恢复后的权威回执。",
      changes: [{
        path: "src/recovered.py",
        status: "modified",
        source_tool: "file_write",
        additions: 1,
        deletions: 0,
      }],
      validations: [{
        command: "node --test bridge-recovery",
        scope: "frontend/terminal-ui",
        status: "passed",
        exit_code: 0,
        passed: 1,
      }],
      unverified: [],
      approvals: [],
      risks: [],
      git_state: { available: true, branch: "main", dirty: true, ahead: 0, behind: 0 },
      next_actions: [],
      evidence_refs: ["evidence-1"],
      duration_ms: 25,
    }, record.id);
    emit("runtime/status", statusPayload(), record.id);
    return;
  }

  if (record.type === "shutdown") {
    if (exitTimer) clearTimeout(exitTimer);
    process.exit(0);
  }
});

function emit(type, payload, requestId = undefined) {
  const record = {
    type,
    version: 1,
    seq: sequence++,
    payload,
  };
  if (requestId) record.request_id = requestId;
  process.stdout.write(`${JSON.stringify(record)}\n`);
}

function statusPayload() {
  return {
    version: "0.1.214",
    session_id: "session-reconnect-1",
    mode: "default",
    permission_mode: "moderate",
    model: "openai/kimi-for-coding",
    reasoning_effort: {
      model: "openai/kimi-for-coding",
      effective: "auto",
      source: "auto",
      supported: [],
      default: null,
      warning: null,
    },
    workspace_root: process.cwd(),
    usage: { total_tokens: 0 },
    context: { used: 0, window: 256000, percentage: 0 },
    budget: {
      enabled: false,
      used_usd: 0,
      max_usd: null,
      remaining_usd: null,
      percentage: null,
      input_tokens: 0,
      max_input_tokens: null,
      output_tokens: 0,
      max_output_tokens: null,
    },
    ui: { show_reasoning: false },
    git: { branch: "main", dirty: true },
  };
}
