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
    emit("ready", {
      mode: "default",
      model: "openai/kimi-for-coding",
      workspace_root: "/Users/lv/Workspace/NaumiAgent",
      usage: { total_tokens: 0 },
      context: { used: 0, window: 256000, percentage: 0 },
      budget: { used_usd: 0, max_usd: 5 },
      git: { branch: "main", dirty: true },
    });
    return;
  }

  if (record.type === "submit") {
    submitCount += 1;
    emit("run/started", {});
    emit("user/message", { content: payload.text ?? "" });
    emit("ui/message", { type: "assistant_stream", phase: "token", content: `submit#${submitCount}:${payload.text ?? ""}` });
    emit("run/completed", {});
    return;
  }

  if (record.type === "shutdown") {
    emit("shutdown", { ok: true });
    setTimeout(() => process.exit(0), 5);
  }
});

function emit(type, payload) {
  process.stdout.write(`${JSON.stringify({ type, version: 1, seq: sequence++, payload })}\n`);
}
