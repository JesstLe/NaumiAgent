import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { stripAnsi } from "../src/ansi.js";

test("terminal UI process handles submit, mode switch, permission, and tool rendering", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。", 7000);
    app.stdin.write("\x1b[Z");
    await waitForOutput(output, "mode: plan");
    app.stdin.write("\x1b[Z");
    await waitForOutput(output, "mode: bypass");

    app.stdin.write("生成一个展示页面\n");
    await waitForOutput(output, "permission: bash_run");
    await waitForOutput(output, "需要确认");
    await waitForLatestScreen(output, "执行过程");
    await waitForLatestScreen(output, "等待权限");

    app.stdin.write("y");
    await waitForOutput(output, "准备 file_write");
    await waitForOutput(output, "生成中 [");
    await waitForOutput(output, "88 lines");
    await waitForOutput(output, "路径: showcase/index.html");
    await waitForOutput(output, "+new");
    await waitForOutput(output, "已折叠");
    await waitForLatestScreen(output, "已完成");
    assert.equal(countLatestScreen(output, "执行过程"), 1);

    app.stdin.write("/expand 1\n");
    await waitForOutput(output, "+line 64");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const plain = stripAnsi(output.text);
    assert(plain.includes("生成一个展示页面"));
    assert(plain.includes("收到，我会创建一个可验证页面。"));
    assert(plain.includes("todo: 1/3 完成"));
    assert(plain.includes("准备 file_write"));
    assert(plain.includes("生成中 ["));
    assert(plain.includes("路径: showcase/index.html"));
    assert(plain.includes("+new"));

    const debugEvents = readDebugEvents(app.debugLogPath);
    assert(debugEvents.some((record) => record.event === "input.chunk"));
    assert(debugEvents.some((record) => record.event === "protocol.send" && record.payload.record.type === "submit"));
    assert(debugEvents.some((record) => record.event === "protocol.send" && record.payload.record.type === "permission_response" && record.payload.record.payload.choice === "allow"));
    assert(debugEvents.some((record) => record.event === "protocol.receive.record" && record.payload.type === "ui/message"));
    assert(debugEvents.some((record) => record.event === "render.screen"));
  } finally {
    forceKill(app);
  }
});

test("terminal UI process can launch bridge from JSON argv", async () => {
  const app = launchTerminalUi("fake-bridge.js", { bridgeMode: "json" });
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。", 7000);
    app.stdin.write("json bridge\n");
    await waitForOutput(output, "json bridge");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const debugEvents = readDebugEvents(app.debugLogPath);
    assert(
      debugEvents.some(
        (record) =>
          record.event === "protocol.send"
          && record.payload.record.type === "submit"
          && record.payload.record.payload.text === "json bridge",
      ),
    );
  } finally {
    forceKill(app);
  }
});

test("terminal UI process creates one workbench task from the composer", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。", 7000);
    app.stdin.write("\x14");
    await waitForOutput(output, "task >");

    app.stdin.write("实现终端任务闭环\n");
    await waitForOutput(output, "任务 #41 · 进行中", 7000);
    await waitForOutput(output, "任务 #41 · 已完成", 7000);
    await waitForOutput(output, "chat >", 7000);

    const code = await stopTerminalUi(app);
    assert.equal(code, 0);
    const events = readDebugEvents(app.debugLogPath);
    const submits = events.filter(
      (record) => record.event === "protocol.send" && record.payload.record.type === "task_submit",
    );
    assert.equal(submits.length, 1);
    assert.equal(submits[0].payload.record.payload.text, "实现终端任务闭环");
  } finally {
    forceKill(app);
  }
});

test("terminal UI process cancels one run without exiting the session", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。", 7000);
    app.stdin.write("需要取消的运行\n");
    await waitForOutput(output, "permission: bash_run", 7000);

    app.stdin.write("\x03");
    await waitForOutput(output, "正在停止当前运行", 7000);
    await waitForOutput(output, "运行已取消", 7000);
    await waitForOutput(output, "运行: 空闲", 7000);

    app.stdin.write("/doctor\n");
    await waitForOutput(output, "环境诊断存在提醒", 7000);

    const code = await stopTerminalUi(app);
    assert.equal(code, 0);
    const events = readDebugEvents(app.debugLogPath);
    const cancels = events.filter(
      (record) => record.event === "protocol.send" && record.payload.record.type === "run_cancel",
    );
    assert.equal(cancels.length, 1);
    assert.equal(cancels[0].payload.record.payload.reason, "用户按下 Ctrl+C");
  } finally {
    forceKill(app);
  }
});

test("terminal UI process keeps explicit plan mode across status refreshes", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。", 7000);
    app.stdin.write("/mode plan\n");
    await waitForOutput(output, "mode: plan");

    app.stdin.write("/tasks\n");
    await waitForOutput(output, "tasks todo 1");
    await waitForOutput(output, "mode: plan");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const debugEvents = readDebugEvents(app.debugLogPath);
    const modeChanged = debugEvents.find(
      (record) =>
        record.event === "protocol.receive.record"
        && record.payload.type === "mode/changed"
        && record.payload.payload?.mode === "plan",
    );
    assert.equal(modeChanged.payload.payload.status.permission_mode, "strict");
  } finally {
    forceKill(app);
  }
});

test("terminal UI process talks to the Python JSONL bridge fixture", async () => {
  const app = launchTerminalUi(null, {
    bridgeCommandJson: [pythonExecutable(), "test/fixtures/python-bridge-fixture.py"],
  });
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。", 7000);
    app.stdin.write("\x1b[Z");
    await waitForOutput(output, "mode: plan");
    app.stdin.write("\x1b[Z");
    await waitForOutput(output, "mode: bypass");

    app.stdin.write("python bridge e2e\n");
    await waitForOutput(output, "Python bridge 收到: python bridge e2e", 7000);
    await waitForOutput(output, "permission: file_write");

    app.stdin.write("y");
    await waitForOutput(output, "准备 file_write");
    await waitForOutput(output, "12 lines");
    await waitForOutput(output, "准备阶段已完成");
    await waitForOutput(output, "路径: python-fixture/index.html");
    await waitForOutput(output, "+new from python bridge");

    app.stdin.write("/tasks\n");
    await waitForOutput(output, "正在写入 Python bridge 页面");

    app.stdin.write("/resume\n");
    await waitForOutput(output, "已恢复会话: Python Bridge 恢复");
    await waitForOutput(output, "这是 Python bridge replay。");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const debugEvents = readDebugEvents(app.debugLogPath);
    assert(
      debugEvents.some(
        (record) =>
          record.event === "protocol.receive.record"
          && record.payload.type === "permission/request",
      ),
    );
    assert(
      debugEvents.some(
        (record) =>
          record.event === "protocol.receive.record"
          && record.payload.type === "engine/event",
      ),
    );
    assert(
      debugEvents.some(
        (record) =>
          record.event === "protocol.receive.record"
          && record.payload.type === "mode/changed"
          && record.payload.payload?.mode === "plan"
          && record.payload.payload?.status?.permission_mode === "strict",
      ),
    );
  } finally {
    forceKill(app);
  }
});

test("terminal UI process reports invalid bridge protocol records without crashing", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("bad bridge event\n");
    await waitForOutput(output, "bridge protocol");
    await waitForOutput(output, "未知 Bridge 事件");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const debugEvents = readDebugEvents(app.debugLogPath);
    assert(
      debugEvents.some(
        (record) =>
          record.event === "protocol.receive.error"
          && String(record.payload.error).includes("未知 Bridge 事件"),
      ),
    );
  } finally {
    forceKill(app);
  }
});

test("terminal UI process treats Shift+Tab as bypass while permission is pending", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");

    app.stdin.write("生成一个展示页面\n");
    await waitForOutput(output, "permission: bash_run");

    app.stdin.write("\x1b[Z");
    await waitForOutput(output, "mode: bypass");
    await waitForOutput(output, "路径: showcase/index.html");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const debugEvents = readDebugEvents(app.debugLogPath);
    assert(
      debugEvents.some(
        (record) =>
          record.event === "protocol.send"
          && record.payload.record.type === "permission_response"
          && record.payload.record.payload.choice === "bypass",
      ),
    );
  } finally {
    forceKill(app);
  }
});

test("terminal UI process renders resume replay from typed UI messages", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("/resume\n");
    await waitForOutput(output, "已恢复会话: 恢复测试");
    await waitForOutput(output, "继续检查 config.yaml");
    await waitForOutput(output, "file_read config.yaml");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const plain = stripAnsi(output.text);
    assert(plain.includes("我先读取配置。"));
    assert(plain.includes("provider: openai"));
  } finally {
    forceKill(app);
  }
});

test("terminal UI process submits cursor-edited input text", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("helo\x1b[Dl\n");
    await waitForOutput(output, "hello");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const plain = stripAnsi(output.text);
    assert(plain.includes("hello"));
    assert(!plain.includes("helo\n"));
  } finally {
    forceKill(app);
  }
});

test("terminal UI process supports home, end, delete, and backspace editing", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("bc\x1b[Ha\x1b[Fdx\b\x1b[D\x1b[3~\n");
    await waitForOutput(output, "abc");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const plain = stripAnsi(output.text);
    assert(plain.includes("abc"));
    assert(!plain.includes("abcd"));
  } finally {
    forceKill(app);
  }
});

test("terminal UI process recalls submitted input with arrow history", async () => {
  const app = launchTerminalUi("history-bridge.js");
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("first\n");
    await waitForOutput(output, "submit#1:first");

    app.stdin.write("second\n");
    await waitForOutput(output, "submit#2:second");

    app.stdin.write("\x1b[A\n");
    await waitForOutput(output, "submit#3:second");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
  } finally {
    forceKill(app);
  }
});

test("terminal UI process shows debug paths with /debug", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("/debug\n");
    await waitForOutput(output, "前端日志:");
    await waitForOutput(output, app.debugLogPath);
    await waitForOutput(output, "Bridge events:");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
  } finally {
    forceKill(app);
  }
});

test("terminal UI process opens task panel through bridge protocol", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("/tasks\n");
    await waitForOutput(output, "tasks todo 1");
    await waitForOutput(output, "#1 [running] 写入页面");
    await waitForOutput(output, "暂无后台任务");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const debugEvents = readDebugEvents(app.debugLogPath);
    assert(
      debugEvents.some(
        (record) =>
          record.event === "protocol.send"
          && record.payload.record.type === "task_panel",
      ),
    );
  } finally {
    forceKill(app);
  }
});

test("terminal UI process opens filtered task panel through bridge protocol", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("/tasks background running 6\n");
    await waitForOutput(output, "filter source=background status=running");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const taskPanelSend = readDebugEvents(app.debugLogPath).find(
      (record) =>
        record.event === "protocol.send"
        && record.payload.record.type === "task_panel",
    );
    assert.equal(taskPanelSend.payload.record.payload.source, "background");
    assert.equal(taskPanelSend.payload.record.payload.status, "running");
    assert.equal(taskPanelSend.payload.record.payload.limit, 6);
  } finally {
    forceKill(app);
  }
});

test("terminal UI process opens task detail through bridge protocol", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("/tasks detail bg_0001\n");
    await waitForOutput(output, "detail=bg_0001");
    await waitForOutput(output, "类型: Background");
    await waitForOutput(output, "CWD: /tmp/project");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const taskPanelSend = readDebugEvents(app.debugLogPath).find(
      (record) =>
        record.event === "protocol.send"
        && record.payload.record.type === "task_panel",
    );
    assert.equal(taskPanelSend.payload.record.payload.detail_id, "bg_0001");
  } finally {
    forceKill(app);
  }
});

test("terminal UI process opens selected task detail with keyboard", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("/tasks\n");
    await waitForOutput(output, "task: 1/3 1");
    app.stdin.write("\n");
    await waitForOutput(output, "detail=1");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const detailSend = readDebugEvents(app.debugLogPath)
      .filter(
        (record) =>
          record.event === "protocol.send"
          && record.payload.record.type === "task_panel",
      )
      .at(-1);
    assert.equal(detailSend.payload.record.payload.detail_id, "1");
  } finally {
    forceKill(app);
  }
});

test("terminal UI process sends task cancel through bridge protocol", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("/tasks cancel bg_0001\n");
    await waitForOutput(output, "已请求取消all任务 bg_0001");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const cancelSend = readDebugEvents(app.debugLogPath).find(
      (record) =>
        record.event === "protocol.send"
        && record.payload.record.type === "task_cancel",
    );
    assert.equal(cancelSend.payload.record.payload.task_id, "bg_0001");
    assert.equal(cancelSend.payload.record.payload.source, "all");
  } finally {
    forceKill(app);
  }
});

test("terminal UI process uses focused task action keys", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("/tasks\n");
    await waitForOutput(output, "Tab/n 选择");
    app.stdin.write("e");
    await waitForOutput(output, "event flow");
    app.stdin.write("x");
    await waitForOutput(output, "已请求取消todo任务 1");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const cancelSend = readDebugEvents(app.debugLogPath).find(
      (record) =>
        record.event === "protocol.send"
        && record.payload.record.type === "task_cancel",
    );
    assert.equal(cancelSend.payload.record.payload.task_id, "1");
    assert.equal(cancelSend.payload.record.payload.source, "todo");
  } finally {
    forceKill(app);
  }
});

test("terminal UI process folds timeline sources locally", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("/tasks\n");
    await waitForOutput(output, "run_7");
    await waitForOutput(output, "sources: background 1");

    app.stdin.write("/tasks timeline collapse browser\n");
    await waitForOutput(output, "browser 1 folded");
    await waitForOutput(output, "已折叠 1 项来源事件");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const plain = stripAnsi(output.text);
    assert(plain.includes("browser 1 folded"));
    const foldedOutput = plain.slice(plain.lastIndexOf("browser 1 folded"));
    assert(!foldedOutput.includes("打开页面"));
  } finally {
    forceKill(app);
  }
});

test("terminal UI process refreshes pinned task panel on task status changes", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("/tasks pin\n");
    await waitForOutput(output, "tasks todo 1");
    await waitForOutput(output, "tasks: bg 1");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const taskPanelSends = readDebugEvents(app.debugLogPath).filter(
      (record) =>
        record.event === "protocol.send"
        && record.payload.record.type === "task_panel",
    );
    assert(taskPanelSends.length >= 2);
    assert.equal(taskPanelSends[0].payload.record.payload.pinned, true);
    assert.equal(taskPanelSends.at(-1).payload.record.payload.refresh, true);
  } finally {
    forceKill(app);
  }
});

test("terminal UI process opens doctor diagnostics through bridge protocol", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("/doctor\n");
    await waitForOutput(output, "doctor: ## 环境诊断存在提醒");
    await waitForOutput(output, "PASS Python 环境");
    await waitForOutput(output, "browser daemon 集成已禁用");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const debugEvents = readDebugEvents(app.debugLogPath);
    assert(
      debugEvents.some(
        (record) =>
          record.event === "protocol.send"
          && record.payload.record.type === "doctor",
      ),
    );
  } finally {
    forceKill(app);
  }
});

test("terminal UI process selects slash completion before deliberate submit", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("/d");
    await waitForLatestScreen(output, "命令补全");
    app.stdin.write("\x1b[B");
    await waitForLatestScreen(output, "> 02. /doctor");

    app.stdin.write("\n");
    await waitForLatestScreenWithout(output, "命令补全");
    await waitForLatestScreen(output, "/doctor▌");
    await delay(120);
    assert.equal(
      readDebugEvents(app.debugLogPath).filter(
        (record) => record.event === "protocol.send" && record.payload.record.type === "doctor",
      ).length,
      0,
    );

    app.stdin.write("\n");
    await waitForOutput(output, "doctor: ## 环境诊断存在提醒");
    assert.equal(
      readDebugEvents(app.debugLogPath).filter(
        (record) => record.event === "protocol.send" && record.payload.record.type === "doctor",
      ).length,
      1,
    );
    assert.equal(await stopTerminalUi(app), 0);
  } finally {
    forceKill(app);
  }
});

test("terminal UI process opens permission panel through bridge protocol", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("/permissions\n");
    await waitForOutput(output, "permissions pending 1");
    await waitForOutput(output, "perm-1 main -> bash_run");
    await waitForOutput(output, "tasks: perm 1");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const debugEvents = readDebugEvents(app.debugLogPath);
    assert(
      debugEvents.some(
        (record) =>
          record.event === "protocol.send"
          && record.payload.record.type === "permissions_panel",
      ),
    );
  } finally {
    forceKill(app);
  }
});

test("terminal UI process toggles reasoning display through bridge protocol", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    await waitForOutput(output, "reasoning: off");
    app.stdin.write("/reasoning on\n");
    await waitForOutput(output, "reasoning 文本显示已开启。");
    await waitForOutput(output, "reasoning: on");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const debugEvents = readDebugEvents(app.debugLogPath);
    assert(
      debugEvents.some(
        (record) =>
          record.event === "protocol.send"
          && record.payload.record.type === "set_reasoning"
          && record.payload.record.payload.enabled === true,
      ),
    );
  } finally {
    forceKill(app);
  }
});

test("terminal UI process keeps bracketed multiline paste atomic until submit", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("\x1b[20");
    await delay(20);
    app.stdin.write("0~第一行\n第二行\x1b[20");
    await delay(20);
    app.stdin.write("1~");
    await delay(80);

    const sendsBeforeEnter = readDebugEvents(app.debugLogPath).filter(
      (record) => record.event === "protocol.send" && record.payload.record.type === "submit",
    );
    assert.equal(sendsBeforeEnter.length, 0);

    app.stdin.write("\n");
    const submit = await waitForDebugEvent(
      app.debugLogPath,
      (record) => record.event === "protocol.send" && record.payload.record.type === "submit",
    );
    assert.equal(submit.payload.record.payload.text, "第一行\n第二行");

    assert.equal(await stopTerminalUi(app), 0);
  } finally {
    forceKill(app);
  }
});

test("terminal UI process inserts Shift Enter and submits multiline text with Ctrl Enter", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("检查 API");
    app.stdin.write("\x1b[13;2u");
    app.stdin.write("修复测试");
    await delay(80);
    assert.equal(
      readDebugEvents(app.debugLogPath).filter(
        (record) => record.event === "protocol.send" && record.payload.record.type === "submit",
      ).length,
      0,
    );

    app.stdin.write("\x1b[13;5u");
    const submit = await waitForDebugEvent(
      app.debugLogPath,
      (record) => record.event === "protocol.send" && record.payload.record.type === "submit",
    );
    assert.equal(submit.payload.record.payload.text, "检查 API\n修复测试");
    assert.equal(await stopTerminalUi(app), 0);
  } finally {
    forceKill(app);
  }
});

test("terminal UI process restores an unsubmitted multiline draft after restart", async () => {
  const statePath = path.join(
    tmpdir(),
    `naumi-terminal-ui-draft-${Date.now()}-${Math.random()}.json`,
  );
  const first = launchTerminalUi("fake-bridge.js", { statePath });
  const firstOutput = collectOutput(first);

  try {
    await waitForOutput(firstOutput, "新终端 UI 已连接 Python bridge。");
    first.stdin.write("保留第一行\x1b[13;2u保留第二行");
    await waitForOutput(firstOutput, "保留第二行");
    await delay(180);
    assert.equal(await stopTerminalUi(first), 0);

    const second = launchTerminalUi("fake-bridge.js", { statePath });
    const secondOutput = collectOutput(second);
    try {
      await waitForOutput(secondOutput, "保留第一行", 3000);
      await waitForOutput(secondOutput, "保留第二行▌", 3000);
      assert.equal(await stopTerminalUi(second), 0);
    } finally {
      forceKill(second);
    }
  } finally {
    forceKill(first);
    fs.rmSync(statePath, { force: true });
  }
});

test("terminal UI process preserves detached scroll and jumps back to live output", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("生成一个展示页面\n");
    await waitForOutput(output, "permission: bash_run");

    app.stdin.write("\x1b[5~");
    const detached = await waitForDebugEvent(
      app.debugLogPath,
      (record) => record.event === "render.screen"
        && record.payload.follow_tail === false
        && record.payload.scroll_offset > 0,
    );

    app.stdin.write("y");
    await waitForOutput(output, "有 1 条新输出");
    const unread = await waitForDebugEvent(
      app.debugLogPath,
      (record) => record.event === "render.screen"
        && record.payload.follow_tail === false
        && record.payload.unread_output_count === 1
        && record.payload.scroll_offset === detached.payload.scroll_offset,
    );

    app.stdin.write("\x0c");
    await waitForDebugEvent(
      app.debugLogPath,
      (record) => record.event === "render.screen"
        && record.ts > unread.ts
        && record.payload.follow_tail === true
        && record.payload.unread_output_count === 0
        && record.payload.scroll_offset === 0,
    );

    assert.equal(await stopTerminalUi(app), 0);
  } finally {
    forceKill(app);
  }
});

test("terminal UI process shows queued, accepted, failed, and retried delivery lifecycle", async () => {
  const app = launchTerminalUi("message-lifecycle-bridge.js");
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("生命周期测试\n");
    await waitForLatestScreen(output, "发送中...");
    await waitForLatestScreen(output, "已确认普通消息");
    assert.equal(countLatestScreen(output, "生命周期测试"), 1);

    app.stdin.write("失败后重试\n");
    await waitForLatestScreen(output, "发送中...");
    await waitForLatestScreen(output, "发送失败: 当前任务仍在执行。");
    app.stdin.write("/retry\n");
    await waitForLatestScreen(output, "重试已接受");
    assert.equal(countLatestScreen(output, "失败后重试"), 1);

    const submits = readDebugEvents(app.debugLogPath).filter(
      (record) => record.event === "protocol.send" && record.payload.record.type === "submit",
    );
    assert.equal(submits.length, 3);
    assert.notEqual(submits[1].payload.record.id, submits[2].payload.record.id);
    assert.equal(await stopTerminalUi(app), 0);
  } finally {
    forceKill(app);
  }
});

test("terminal UI process restores queued outbox as uncertain without automatic resend", async () => {
  const statePath = path.join(
    tmpdir(),
    `naumi-terminal-ui-outbox-${Date.now()}-${Math.random()}.json`,
  );
  const first = launchTerminalUi("message-lifecycle-bridge.js", { statePath });
  const firstOutput = collectOutput(first);

  try {
    await waitForOutput(firstOutput, "新终端 UI 已连接 Python bridge。");
    first.stdin.write("等待重启确认\n");
    await waitForLatestScreen(firstOutput, "发送中...");
    assert.equal(await stopTerminalUi(first), 0);

    const second = launchTerminalUi("message-lifecycle-bridge.js", { statePath });
    const secondOutput = collectOutput(second);
    try {
      await waitForLatestScreen(secondOutput, "发送状态待确认");
      await delay(250);
      assert.equal(
        readDebugEvents(second.debugLogPath).filter(
          (record) => record.event === "protocol.send" && record.payload.record.type === "submit",
        ).length,
        0,
      );

      second.stdin.write("/retry\n");
      await waitForLatestScreen(secondOutput, "已确认普通消息");
      const retrySubmit = readDebugEvents(second.debugLogPath).find(
        (record) => record.event === "protocol.send" && record.payload.record.type === "submit",
      );
      assert(retrySubmit);
      assert.equal(retrySubmit.payload.record.payload.text, "等待重启确认");
      assert.equal(await stopTerminalUi(second), 0);
    } finally {
      forceKill(second);
    }
  } finally {
    forceKill(first);
    fs.rmSync(statePath, { force: true });
  }
});

test("terminal UI process restores project history and accepts search without sending", async () => {
  const statePath = path.join(
    tmpdir(),
    `naumi-terminal-ui-history-${Date.now()}-${Math.random()}.json`,
  );
  const first = launchTerminalUi("message-lifecycle-bridge.js", { statePath });
  const firstOutput = collectOutput(first);

  try {
    await waitForOutput(firstOutput, "新终端 UI 已连接 Python bridge。");
    first.stdin.write("/doctor\x1b[13;5u");
    await delay(120);
    first.stdin.write("历史 alpha\n");
    await waitForLatestScreen(firstOutput, "已确认普通消息");
    first.stdin.write("历史 beta\n");
    await delay(250);
    assert.equal(await stopTerminalUi(first), 0);

    const second = launchTerminalUi("message-lifecycle-bridge.js", { statePath });
    const secondOutput = collectOutput(second);
    try {
      await waitForOutput(secondOutput, "新终端 UI 已连接 Python bridge。");
      second.stdin.write("保留草稿");
      second.stdin.write("\x12alpha");
      await waitForLatestScreen(secondOutput, "历史搜索");
      await waitForLatestScreen(secondOutput, "历史 alpha");

      second.stdin.write("\n");
      await waitForLatestScreen(secondOutput, "历史 alpha▌");
      await delay(180);
      assert.equal(
        readDebugEvents(second.debugLogPath).filter(
          (record) => record.event === "protocol.send" && record.payload.record.type === "submit",
        ).length,
        0,
      );

      second.stdin.write("\n");
      await waitForLatestScreen(secondOutput, "已确认普通消息");
      const submits = readDebugEvents(second.debugLogPath).filter(
        (record) => record.event === "protocol.send" && record.payload.record.type === "submit",
      );
      assert.equal(submits.length, 1);
      assert.equal(submits[0].payload.record.payload.text, "历史 alpha");

      second.stdin.write("\x12doctor");
      await waitForLatestScreen(secondOutput, "历史搜索");
      await waitForLatestScreen(secondOutput, "/doctor");
      second.stdin.write("\n");
      await waitForLatestScreenWithout(secondOutput, "历史搜索");
      await delay(120);
      assert.equal(
        readDebugEvents(second.debugLogPath).filter(
          (record) => record.event === "protocol.send" && record.payload.record.type === "doctor",
        ).length,
        0,
      );
      second.stdin.write("\n");
      await delay(120);
      assert.equal(
        readDebugEvents(second.debugLogPath).filter(
          (record) => record.event === "protocol.send" && record.payload.record.type === "doctor",
        ).length,
        1,
      );

      second.stdin.write("新的草稿");
      second.stdin.write("\x12beta");
      await waitForLatestScreen(secondOutput, "历史搜索");
      await waitForLatestScreen(secondOutput, "历史 beta");
      second.stdin.write("\x1b");
      await waitForLatestScreenWithout(secondOutput, "历史搜索");
      assert.match(latestScreen(secondOutput), /新的草稿▌/);
      assert.equal(await stopTerminalUi(second), 0);
    } finally {
      forceKill(second);
    }
  } finally {
    forceKill(first);
    fs.rmSync(statePath, { force: true });
  }
});

function launchTerminalUi(fixtureName = "fake-bridge.js", options = {}) {
  const debugLogPath = path.join(tmpdir(), `naumi-terminal-ui-debug-${Date.now()}-${Math.random()}.jsonl`);
  const fakeBridge = fixtureName
    ? fileURLToPath(new URL(`./fixtures/${fixtureName}`, import.meta.url))
    : "";
  const bridgeArgs = options.bridgeCommandJson
    ? ["--bridge-command-json", JSON.stringify(options.bridgeCommandJson)]
    : options.bridgeMode === "json"
      ? ["--bridge-command-json", JSON.stringify([process.execPath, fakeBridge])]
      : ["--bridge-command", `"${process.execPath}" "${fakeBridge}"`];
  const child = spawn(
    process.execPath,
    ["src/index.js", ...bridgeArgs],
    {
      cwd: fileURLToPath(new URL("..", import.meta.url)),
      stdio: ["pipe", "pipe", "pipe"],
      env: {
        ...process.env,
        FORCE_COLOR: "0",
        NAUMI_TERMINAL_UI_STATE_PATH: options.statePath
          || path.join(tmpdir(), `naumi-terminal-ui-state-${Date.now()}-${Math.random()}.json`),
        NAUMI_TERMINAL_UI_DEBUG_LOG: debugLogPath,
      },
    },
  );
  child.debugLogPath = debugLogPath;
  return child;
}

function pythonExecutable() {
  const configured = process.env.NAUMI_TEST_PYTHON;
  if (configured) {
    return configured;
  }
  const repoRoot = fileURLToPath(new URL("../../..", import.meta.url));
  const virtualenvPython = path.join(
    repoRoot,
    ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );
  if (fs.existsSync(virtualenvPython)) {
    return virtualenvPython;
  }
  return process.platform === "win32" ? "python" : "python3";
}

function collectOutput(child) {
  const state = { text: "" };
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    state.text += chunk;
  });
  child.stderr.on("data", (chunk) => {
    state.text += chunk;
  });
  return state;
}

async function stopTerminalUi(child) {
  if (child.exitCode !== null) {
    return child.exitCode;
  }
  child.stdin.write("\u0003");
  const [code] = await Promise.race([
    once(child, "exit"),
    delay(1500).then(() => {
      forceKill(child);
      return once(child, "exit");
    }).then(([code]) => [code]),
  ]);
  return code;
}

function forceKill(child) {
  if (child.exitCode === null && !child.killed) {
    child.kill("SIGTERM");
  }
}

async function waitForOutput(output, needle, timeoutMs = 1500) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (stripAnsi(output.text).includes(needle)) {
      return;
    }
    await delay(20);
  }
  assert.fail(`等待输出超时: ${needle}\n\n${stripAnsi(output.text).slice(-3000)}`);
}

async function waitForLatestScreen(output, needle, timeoutMs = 2000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (latestScreen(output).includes(needle)) return;
    await delay(20);
  }
  assert.fail(`等待最新画面超时: ${needle}\n\n${latestScreen(output).slice(-3000)}`);
}

async function waitForLatestScreenWithout(output, needle, timeoutMs = 2000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (!latestScreen(output).includes(needle)) return;
    await delay(20);
  }
  assert.fail(`等待最新画面关闭超时: ${needle}\n\n${latestScreen(output).slice(-3000)}`);
}

function countLatestScreen(output, needle) {
  return latestScreen(output).split(needle).length - 1;
}

function latestScreen(output) {
  return stripAnsi(output.text.split("\x1b[2J\x1b[H").at(-1) ?? "");
}

function readDebugEvents(filePath) {
  return fs.readFileSync(filePath, "utf8").trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

async function waitForDebugEvent(filePath, predicate, timeoutMs = 1500) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const match = readDebugEvents(filePath).find(predicate);
    if (match) return match;
    await delay(20);
  }
  assert.fail(`等待调试事件超时: ${filePath}`);
}
