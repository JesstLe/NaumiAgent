#!/usr/bin/env node
import process from "node:process";
import { attachJsonlLineReader } from "../../src/protocol.js";

let sequence = 1;
const attempts = new Map();

attachJsonlLineReader(process.stdin, (line) => {
  if (!line.trim()) return;
  const record = JSON.parse(line);
  const payload = record.payload ?? {};

  if (record.type === "hello") {
    emit("ready", statusPayload(), record.id);
    return;
  }
  if (record.type === "submit") {
    const text = String(payload.text ?? "");
    const attempt = (attempts.get(text) ?? 0) + 1;
    attempts.set(text, attempt);
    if (text === "失败后重试" && attempt === 1) {
      setTimeout(() => {
        emit("error", { code: "run_in_progress", message: "当前任务仍在执行。" }, record.id);
      }, 160);
      return;
    }
    setTimeout(() => {
      emit("user/message", { content: text }, record.id);
      emit("run/started", { task: text }, record.id);
      emit("ui/message", {
        type: "assistant_stream",
        phase: "token",
        content: attempt > 1 ? "重试已接受" : "已确认普通消息",
      }, record.id);
      emit("run/completed", { status: "completed" }, record.id);
    }, 160);
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

function statusPayload() {
  return {
    version: "0.1.214",
    mode: "default",
    permission_mode: "moderate",
    model: "test/lifecycle",
    workspace_root: process.cwd(),
    usage: { total_tokens: 0 },
    context: { used: 0, window: 256000, percentage: 0 },
    budget: { used_usd: 0, max_usd: 5 },
  };
}
