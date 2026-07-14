#!/usr/bin/env node
import process from "node:process";
import { attachJsonlLineReader } from "../../src/protocol.js";

let sequence = 1;
let pingCount = 0;

emit("ready", {
  version: "0.1.214",
  mode: "default",
  permission_mode: "moderate",
  model: "test/heartbeat",
  workspace_root: process.cwd(),
  usage: { total_tokens: 0 },
  context: { used: 0, window: 256000, percentage: 0 },
  budget: { enabled: false, used_usd: 0, max_usd: null, percentage: null },
});

attachJsonlLineReader(process.stdin, (line) => {
  if (!line.trim()) return;
  const record = JSON.parse(line);
  if (record.type === "hello") {
    emit("ack", { event: "hello" }, record.id);
    return;
  }
  if (record.type === "ping") {
    pingCount += 1;
    if (pingCount === 1) return;
    setTimeout(() => {
      emit("pong", { ok: true, active_run: false, queued_conversations: 0 }, record.id);
    }, 30);
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
