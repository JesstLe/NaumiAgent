#!/usr/bin/env node
import process from "node:process";
import { attachJsonlLineReader } from "../../src/protocol.js";

let sequence = 1;
let active = null;
const queue = [];

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
        capabilities: ["heartbeat", "typed_ui_messages", "workbench_snapshot"],
      },
    }, record.id);
    emit("ready", statusPayload(), record.id);
    return;
  }
  if (record.type === "submit") {
    const text = String(payload.text ?? "");
    const submission = { id: record.id, text };
    if (active) {
      queue.push(submission);
      emit("user/message", { content: text }, record.id);
      emit("run/queued", { task: text, position: queue.length, queued: queue.length }, record.id);
      emit("runtime/status", statusPayload(), record.id);
      return;
    }
    startSubmission(submission, 700);
    return;
  }
  if (record.type === "shutdown") {
    emit("shutdown", { ok: true }, record.id);
    setTimeout(() => process.exit(0), 5);
  }
});

function emit(type, payload, requestId = "") {
  process.stdout.write(`${JSON.stringify({
    type,
    version: 1,
    seq: sequence++,
    ...(requestId ? { request_id: requestId } : {}),
    payload,
  })}\n`);
}

function startSubmission(submission, durationMs) {
  active = submission;
  emit("user/message", { content: submission.text }, submission.id);
  emit("run/started", { task: submission.text }, submission.id);
  emit("runtime/status", statusPayload(), submission.id);
  emit("ui/message", {
    type: "assistant_stream",
    phase: "token",
    content: submission.text === "生命周期测试" ? "第一条处理中" : "第二条自动执行",
  }, submission.id);
  setTimeout(() => {
    emit("ui/message", { type: "assistant_stream", phase: "end" }, submission.id);
    emit("run/completed", { status: "completed" }, submission.id);
    active = null;
    const next = queue.shift();
    if (next) startSubmission(next, 120);
    else emit("runtime/status", statusPayload(), submission.id);
  }, durationMs);
}

function statusPayload() {
  return {
    version: "0.1.214",
    mode: "default",
    permission_mode: "moderate",
    model: "test/lifecycle",
    workspace_root: process.cwd(),
    usage: { total_tokens: 0 },
    context: { used: 0, window: 256000, percentage: 0 },
    budget: { enabled: false, used_usd: 0, max_usd: null, percentage: null },
    tasks: { queued_conversations: queue.length },
  };
}
