#!/usr/bin/env node
import process from "node:process";
import { attachJsonlLineReader } from "../../src/protocol.js";

let sequence = 1;

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
    emit("debug/trace", { events_path: "/tmp/naumi-terminal-ui-test/events.jsonl" });
    return;
  }

  if (record.type === "cycle_mode") {
    emit("mode/changed", { mode: "bypass", status: { mode: "bypass" } });
    return;
  }

  if (record.type === "submit") {
    emit("run/started", {});
    emit("user/message", { content: payload.text ?? "" });
    emitUi({ type: "assistant_stream", phase: "start" });
    emitUi({ type: "assistant_stream", phase: "token", content: "收到，我会创建一个可验证页面。" });
    emitUi({ type: "assistant_stream", phase: "end" });
    emitUi({
      type: "todo_status",
      total_count: 3,
      completed_count: 1,
      open_count: 2,
      items: [
        { id: 1, subject: "创建目录", status: "completed" },
        { id: 2, subject: "写入页面", status: "in_progress" },
        { id: 3, subject: "浏览器验证", status: "pending" },
      ],
    });
    emit("permission/request", {
      tool_name: "bash_run",
      reason: "需要启动本地预览服务。",
    }, "perm-1");
    return;
  }

  if (record.type === "permission_response") {
    emit("permission/resolved", { request_id: payload.request_id, choice: payload.choice });
    if (payload.choice === "bypass") {
      emit("mode/changed", { mode: "bypass", status: { mode: "bypass" } });
    }
    emitUi({
      type: "tool_prepare",
      phase: "snapshot",
      tool_name: "file_write",
      path: "showcase/index.html",
      content_lines: 88,
      elapsed_ms: 120,
    });
    emitUi({
      type: "tool_use",
      tool_call_id: "call-1",
      tool_name: "file_write",
      file_path: "showcase/index.html",
    });
    emitUi({
      type: "tool_result",
      tool_call_id: "call-1",
      tool_name: "file_write",
      status: "success",
      duration_ms: 21,
      content_preview: ["--- a/showcase/index.html", "+++ b/showcase/index.html", "@@", "-old", "+new", ...Array.from({ length: 65 }, (_, index) => `+line ${index}`)].join("\n"),
      content_length: 640,
    });
    emit("run/completed", {});
    return;
  }

  if (record.type === "resume") {
    emit("session/replayed", { session_id: payload.session_id || "latest", title: "恢复测试", message_count: 4, clear: true });
    emitUi({ type: "user", content: "继续检查 config.yaml", is_command: false });
    emitUi({ type: "assistant_stream", phase: "token", content: "我先读取配置。" });
    emitUi({ type: "tool_use", tool_call_id: "call-resume", tool_name: "file_read", file_path: "config.yaml" });
    emitUi({
      type: "tool_result",
      tool_call_id: "call-resume",
      tool_name: "file_read",
      status: "success",
      duration_ms: 8,
      content_preview: "models:\n  provider: openai\n",
      content_length: 26,
    });
    return;
  }

  if (record.type === "shutdown") {
    emit("shutdown", { ok: true });
    setTimeout(() => process.exit(0), 5);
  }
});

process.stdin.on("end", () => {
  process.exit(0);
});

function emitUi(payload) {
  emit("ui/message", payload);
}

function emit(type, payload, requestId = undefined) {
  const record = {
    type,
    version: 1,
    seq: sequence++,
    payload,
  };
  if (requestId) {
    record.request_id = requestId;
  }
  process.stdout.write(`${JSON.stringify(record)}\n`);
}
