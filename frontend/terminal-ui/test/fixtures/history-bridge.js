#!/usr/bin/env node
import process from "node:process";
import { attachJsonlLineReader } from "../../src/protocol.js";

let sequence = 1;
let submitCount = 0;

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
    emit("ready", {
      version: "0.1.214",
      mode: "default",
      permission_mode: "moderate",
      model: "openai/kimi-for-coding",
      workspace_root: "/Users/lv/Workspace/NaumiAgent",
      usage: { total_tokens: 0 },
      context: { used: 0, window: 256000, percentage: 0 },
      budget: { enabled: false, used_usd: 0, max_usd: null, percentage: null },
      git: { branch: "main", dirty: true },
    });
    return;
  }

  if (record.type === "submit") {
    submitCount += 1;
    emit("run/started", {}, record.id);
    emit("user/message", { content: payload.text ?? "" }, record.id);
    emit("ui/message", { type: "assistant_stream", phase: "token", content: `submit#${submitCount}:${payload.text ?? ""}` });
    emit("run/completed", {}, record.id);
    return;
  }

  if (record.type === "shutdown") {
    emit("shutdown", { ok: true });
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
