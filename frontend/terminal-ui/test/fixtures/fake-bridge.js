#!/usr/bin/env node
import process from "node:process";
import { attachJsonlLineReader } from "../../src/protocol.js";

let sequence = 1;
let mode = "default";
let showReasoning = false;
let sessionId = "session-fake-1";
let activeRun = null;
let inspectorOpen = false;

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

  if (record.type === "inspector/request") {
    inspectorOpen = payload.open !== false;
    if (!inspectorOpen) {
      emit("ack", { event: "inspector/request", open: false, revision: 2 }, record.id);
      return;
    }
    if (Number(payload.known_revision) > 0) {
      emit("inspector/snapshot", inspectorSnapshot(3), record.id);
      return;
    }
    emit("inspector/snapshot", inspectorSnapshot(1), record.id);
    setTimeout(() => {
      if (!inspectorOpen) return;
      const snapshot = inspectorSnapshot(3);
      emit("inspector/update", {
        schema_version: 1,
        session_id: sessionId,
        revision: 3,
        generated_at: "2026-07-13T00:00:01+00:00",
        active_run_id: "run-inspector-live",
        changed_tabs: { tools: snapshot.tools },
      });
    }, 40);
    return;
  }

  if (record.type === "task_submit") {
    const text = payload.text ?? "";
    const mission = { id: "mission-1", title: "终端任务", status: "active" };
    const task = { id: "41", subject: text, status: "in_progress" };
    const issue = {
      task_id: "41",
      mission_id: "mission-1",
      title: text,
      parallel_mode: payload.parallel_mode ?? "exclusive",
      risk_level: payload.risk_level ?? "medium",
    };
    activeRun = {
      requestId: record.id,
      intent: "task",
      taskId: "41",
      missionId: "mission-1",
    };
    emit("user/message", { content: text, intent: "task", task_id: "41" }, record.id);
    emit("task/created", {
      mission,
      task,
      issue,
      workbench_snapshot: workbenchSnapshot(mission, task, issue),
    }, record.id);
    emit("run/started", {
      task: text,
      task_id: "41",
      mission_id: "mission-1",
      intent: "task",
    }, record.id);
    emitUi({ type: "assistant_stream", phase: "start" });
    emitUi({ type: "assistant_stream", phase: "token", content: "任务已创建，正在执行。" });
    emitUi({ type: "assistant_stream", phase: "end" });
    setTimeout(() => {
      if (activeRun?.requestId !== record.id) return;
      const completedTask = { ...task, status: "completed" };
      emit("workbench/snapshot", workbenchSnapshot(mission, completedTask, issue), record.id);
      emit("run/completed", {
        status: "completed",
        task_id: "41",
        mission_id: "mission-1",
        intent: "task",
      }, record.id);
      activeRun = null;
    }, 120);
    return;
  }

  if (record.type === "submit") {
    if ((payload.text ?? "").includes("bad bridge event")) {
      process.stdout.write('{"type":"unknown/server","payload":{}}\n');
      return;
    }
    activeRun = { requestId: record.id, intent: "chat" };
    emit("run/started", {}, record.id);
    emit("user/message", { content: payload.text ?? "" }, record.id);
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

  if (record.type === "run_cancel") {
    if (!activeRun) {
      emit("error", { code: "no_active_run", message: "当前没有正在运行的任务。" }, record.id);
      return;
    }
    const target = activeRun;
    activeRun = null;
    emit("ack", {
      event: "run_cancel",
      status: "accepted",
      target_request_id: target.requestId,
    }, record.id);
    emit("run/cancelled", {
      status: "cancelled",
      target_request_id: target.requestId,
      intent: target.intent,
      ...(target.taskId ? {
        task_id: target.taskId,
        mission_id: target.missionId,
        task_status: "blocked",
      } : {}),
      reason: payload.reason || "用户取消了当前运行。",
    }, record.id);
    return;
  }

  if (record.type === "receipt/request") {
    setTimeout(() => {
      emit("completion/receipt", {
        schema_version: 1,
        receipt_id: payload.receipt_id || "receipt-fake-1",
        run_id: payload.run_id || "run-fake-1",
        outcome: "completed",
        summary: "页面已写入并完成验证。",
        changes: [{ path: "showcase/index.html", status: "modified", source_tool: "file_write", additions: 65, deletions: 1 }],
        validations: [{ command: "node --test", scope: "frontend/terminal-ui", status: "passed", exit_code: 0, passed: 1 }],
        unverified: [],
        approvals: [{ call_id: "call-1", tool_name: "file_write", decision: "allowed_once" }],
        risks: [],
        git_state: { available: true, branch: "main", dirty: true, ahead: 0, behind: 0 },
        next_actions: [{ id: "review", label: "审查本轮改动", kind: "review_changes" }],
        evidence_refs: ["run:run-fake-1:tool:call-1"],
        duration_ms: 140,
      }, record.id);
    }, 80);
    return;
  }

  if (record.type === "permission_response") {
    const targetRun = activeRun;
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
    setTimeout(() => {
      if (!targetRun || activeRun?.requestId !== targetRun.requestId) return;
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
      emit("run/completed", {
        status: "completed",
        receipt_id: "receipt-fake-1",
        run_id: "run-fake-1",
      }, targetRun.requestId);
      activeRun = null;
    }, 30);
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

function workbenchSnapshot(mission, task, issue) {
  return {
    session_id: sessionId,
    missions: [mission],
    tasks: [task],
    issues: [issue],
    failures: [],
    events: [{ id: "event-41", type: "issue.created" }],
  };
}

function inspectorSnapshot(revision) {
  return {
    schema_version: 1,
    session_id: sessionId,
    revision,
    generated_at: "2026-07-13T00:00:00+00:00",
    active_run_id: revision > 1 ? "run-inspector-live" : "",
    plan: {
      state: "ready",
      items: [{
        id: "todo-inspector-1",
        subject: "保持运行检查器实时更新",
        status: "in_progress",
        active_form: "正在同步 Inspector",
        owner: "main",
        blocked_by: [],
      }],
      next_actions: [],
      warnings: [],
    },
    tools: {
      state: revision > 1 ? "ready" : "empty",
      items: revision > 1
        ? [{
            call_id: "read-live-1",
            name: "file_read",
            status: "running",
            summary: "正在读取真实项目文件",
            duration_ms: 45,
            run_id: "run-inspector-live",
          }]
        : [],
      approvals: [],
      warnings: [],
    },
    context: {
      state: "ready",
      workspace_root: "/Users/lv/Workspace/NaumiAgent",
      branch: "main",
      commit: "abc1234",
      git_available: true,
      git_dirty: true,
      model: "openai/kimi-for-coding",
      runtime_mode: mode,
      permission_mode: permissionModeFor(mode),
      context_used: 1200,
      context_window: 256000,
      context_percentage: 0.5,
      budget_used_usd: 0.01,
      budget_max_usd: 5,
      budget_percentage: 0.2,
      input_tokens: 1000,
      output_tokens: 200,
      turns: 1,
      warnings: [],
    },
    changes: {
      state: "empty",
      source_run_id: "",
      receipt_id: "",
      summary: "",
      items: [],
      git_state: {
        available: true,
        branch: "main",
        dirty: true,
        commit: "abc1234",
        ahead: 0,
        behind: 0,
      },
      warnings: [],
    },
    tests: {
      state: "empty",
      source_run_id: "",
      receipt_id: "",
      validations: [],
      unverified: [],
      next_actions: [],
      warnings: [],
    },
  };
}
