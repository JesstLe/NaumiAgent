#!/usr/bin/env node
import fs from "node:fs";
import process from "node:process";

import { attachJsonlLineReader } from "../../src/protocol.js";

const markerPath = String(process.env.NAUMI_TEST_CURSOR_RECOVERY_MARKER ?? "");
if (!markerPath) throw new Error("NAUMI_TEST_CURSOR_RECOVERY_MARKER 未配置");
const generation = fs.existsSync(markerPath) ? 2 : 1;
if (generation === 1) fs.writeFileSync(markerPath, "first\n", "utf8");

const sessionId = "session-cursor-recovery";
const streamId = "tes_0123456789abcdef01234567";
let sequence = 1;

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
        capabilities: [
          "heartbeat",
          "sequence_integrity",
          "terminal_event_cursor",
          "terminal_event_recovery",
          "typed_ui_messages",
        ],
      },
    }, record.id);
    emit("ready", statusPayload());
    if (generation === 1) {
      emit("run/completed", {
        status: "completed",
        run_id: "run-cursor-1",
        receipt_id: "receipt-cursor-1",
      });
      emitDurableReceipt(1, "receipt-cursor-1", "第一条持久回执。");
    }
    return;
  }
  if (record.type === "terminal_events/ack") {
    emit("ack", {
      event: "terminal_events/ack",
      session_id: payload.session_id,
      stream_id: payload.stream_id,
      cursor: payload.cursor,
    }, record.id);
    if (generation === 1 && payload.cursor === 1) {
      setTimeout(() => process.exit(19), 20);
    }
    return;
  }
  if (record.type === "resume" && generation === 2) {
    if (
      payload.session_id !== sessionId
      || payload.terminal_event_stream_id !== streamId
      || payload.resume_after_cursor !== 1
      || !/^tecli_[0-9a-f]{24}$/.test(String(payload.terminal_event_client_id))
    ) {
      emit("error", {
        code: "cursor_resume_mismatch",
        message: "cursor resume payload mismatch",
      }, record.id);
      return;
    }
    emit("session/replayed", {
      session_id: sessionId,
      title: "游标恢复",
      message_count: 0,
      clear: true,
      terminal_event_recovery: {
        mode: "cursor_replay",
        stream_id: streamId,
        requested_cursor: 1,
        earliest_cursor: 1,
        latest_cursor: 2,
      },
    }, record.id);
    emitDurableReceipt(2, "receipt-cursor-2", "第二条游标补发回执。", record.id);
    emit("terminal_events/recovery", {
      schema_version: 1,
      session_id: sessionId,
      mode: "replay_complete",
      stream_id: streamId,
      requested_cursor: 1,
      earliest_cursor: 1,
      latest_cursor: 2,
      replayed_count: 1,
    }, record.id);
    emit("runtime/status", statusPayload(), record.id);
    return;
  }
  if (record.type === "ping") {
    emit("pong", {}, record.id);
    return;
  }
  if (record.type === "shutdown") process.exit(0);
});

function emitDurableReceipt(cursor, receiptId, summary, requestId = undefined) {
  const record = {
    type: "completion/receipt",
    version: 1,
    seq: sequence++,
    event_id: `tev_${String(cursor).padStart(24, "0")}`,
    stream_id: streamId,
    cursor,
    criticality: "terminal",
    payload: {
      schema_version: 1,
      receipt_id: receiptId,
      run_id: `run-cursor-${cursor}`,
      outcome: "completed",
      summary,
      changes: [],
      validations: [],
      unverified: [],
      approvals: [],
      risks: [],
      git_state: {
        available: true,
        branch: "main",
        dirty: false,
        ahead: 0,
        behind: 0,
      },
      next_actions: [],
      evidence_refs: [],
      duration_ms: 1,
    },
  };
  if (requestId) record.request_id = requestId;
  process.stdout.write(`${JSON.stringify(record)}\n`);
}

function emit(type, payload, requestId = undefined) {
  const record = { type, version: 1, seq: sequence++, payload };
  if (requestId) record.request_id = requestId;
  process.stdout.write(`${JSON.stringify(record)}\n`);
}

function statusPayload() {
  return {
    version: "0.1.214",
    session_id: sessionId,
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
    git: { branch: "main", dirty: false },
  };
}
