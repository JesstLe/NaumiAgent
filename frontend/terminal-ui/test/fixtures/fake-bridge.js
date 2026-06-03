#!/usr/bin/env node
import process from "node:process";
import { attachJsonlLineReader } from "../../src/protocol.js";

let sequence = 1;
let mode = "default";
let showReasoning = false;
let sessionId = "session-fake-1";

attachJsonlLineReader(process.stdin, (line) => {
  if (!line.trim()) return;
  const record = JSON.parse(line);
  const payload = record.payload ?? {};

  if (record.type === "hello") {
    emit("ready", statusPayload());
    emit("debug/trace", { events_path: "/tmp/naumi-terminal-ui-test/events.jsonl" });
    return;
  }

  if (record.type === "set_mode") {
    mode = payload.mode === "plan" || payload.mode === "bypass" ? payload.mode : "default";
    emit("mode/changed", { mode, status: statusPayload() });
    return;
  }

  if (record.type === "cycle_mode") {
    mode = mode === "default" ? "plan" : mode === "plan" ? "bypass" : "default";
    emit("mode/changed", { mode, status: statusPayload() });
    return;
  }

  if (record.type === "set_reasoning") {
    showReasoning = Boolean(payload.enabled);
    emit("runtime/status", statusPayload());
    return;
  }

  if (record.type === "submit") {
    if ((payload.text ?? "").includes("bad bridge event")) {
      process.stdout.write('{"type":"unknown/server","payload":{}}\n');
      return;
    }
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
      mode = "bypass";
      emit("mode/changed", { mode, status: statusPayload() });
    }
    emitUi({
      type: "tool_prepare",
      phase: "snapshot",
      tool_name: "file_write",
      tool_call_id: "call-1",
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
    sessionId = payload.session_id || "latest";
    emit("session/replayed", { session_id: sessionId, title: "恢复测试", message_count: 4, clear: true });
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

  if (record.type === "task_panel") {
    const filterLine = (payload.source && payload.source !== "all") || payload.detail_id
      ? [
          `filter: source=${payload.source || "all"} status=${payload.status || "all"}${
            payload.detail_id ? ` detail=${payload.detail_id}` : ""
          }`,
        ]
      : [];
    const detailLines = payload.detail_id
      ? [
          "Detail",
          "  类型: Background",
          `  ID: ${payload.detail_id}`,
          "  状态: running",
          "  命令: npm run dev",
          "  CWD: /tmp/project",
        ]
      : [];
    emitUi({
      type: "system_notice",
      title: "tasks",
      content: [
        "任务面板",
        ...filterLine,
        ...detailLines,
        "Timeline",
        "  - bg_0001 [running] npm run dev | time=2026-06-01T12:00:00; source=background; event=background:bg_0001; output=/tmp/bg.log",
        "  - run_7 [needs_input] 打开页面 | time=2026-06-01T12:00:01; source=browser; event=browser:run_7; records=/tmp/browser-trace.zip",
        "Todo",
        "  - #1 [running] 写入页面 | owner=main; blocked_by=-; blocks=-",
        "Background",
        "  暂无后台任务",
      ].join("\n"),
      level: "info",
    });
    const tasks = payload.pinned
      ? { background_running: 1, background_attention: 0, subagents_active: 0, browser_active: 0, permissions_pending: 0 }
      : { background_running: 0, background_attention: 0, subagents_active: 0, browser_active: 0, permissions_pending: 0 };
    emit("runtime/status", statusPayload({ tasks }));
    return;
  }

  if (record.type === "task_cancel") {
    emitUi({
      type: "system_notice",
      title: "tasks",
      content: `已请求取消${payload.source || "all"}任务 ${payload.task_id}。当前状态: cancelled`,
      level: "info",
    });
    emit("runtime/status", statusPayload({
      tasks: { background_running: 0, background_attention: 0, subagents_active: 0, browser_active: 0, permissions_pending: 0 },
    }));
    return;
  }

  if (record.type === "permissions_panel") {
    emitUi({
      type: "system_notice",
      title: "permissions",
      content: [
        "权限面板",
        `mode: ${mode} | permission: ${permissionModeFor(mode)}`,
        "Pending",
        "  - perm-1 main -> bash_run [needs_confirmation] 需要启动本地预览服务。",
        "History",
        "  - call-1 coder -> file_write [confirmed] 用户已允许。",
      ].join("\n"),
      level: "info",
    });
    emit("runtime/status", statusPayload({
      tasks: { background_running: 0, background_attention: 0, subagents_active: 0, browser_active: 0, permissions_pending: 1 },
    }));
    return;
  }

  if (record.type === "doctor") {
    emitUi({
      type: "system_notice",
      title: "doctor",
      content: [
        "## 环境诊断存在提醒",
        "",
        "- **PASS Python 环境**：Python 3.12",
        "- **WARN browser daemon**：browser daemon 集成已禁用",
        "",
        "这份报告可直接复制给 Agent 或维护者，用于定位本机环境问题。",
      ].join("\n"),
      level: "warn",
    });
    emit("runtime/status", statusPayload());
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

function permissionModeFor(currentMode) {
  if (currentMode === "plan") return "strict";
  if (currentMode === "bypass") return "bypass";
  return "moderate";
}

function statusPayload(overrides = {}) {
  return {
    session_id: sessionId,
    mode,
    permission_mode: permissionModeFor(mode),
    model: "openai/kimi-for-coding",
    workspace_root: "/Users/lv/Workspace/NaumiAgent",
    usage: { total_tokens: 0 },
    context: { used: 0, window: 256000, percentage: 0 },
    budget: { used_usd: 0, max_usd: 5 },
    ui: { show_reasoning: showReasoning },
    git: { branch: "main", dirty: true },
    ...overrides,
  };
}
