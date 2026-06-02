import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { stripAnsi } from "../src/ansi.js";

test("terminal UI process handles submit, mode switch, permission, and tool rendering", async () => {
  const app = launchTerminalUi();
  const output = collectOutput(app);

  try {
    await waitForOutput(output, "新终端 UI 已连接 Python bridge。");
    app.stdin.write("\x1b[Z");
    await waitForOutput(output, "mode: bypass");

    app.stdin.write("生成一个展示页面\n");
    await waitForOutput(output, "permission: bash_run");

    app.stdin.write("y");
    await waitForOutput(output, "准备 file_write");
    await waitForOutput(output, "file_write showcase/index.html");
    await waitForOutput(output, "+new");
    await waitForOutput(output, "已折叠");

    app.stdin.write("/expand 1\n");
    await waitForOutput(output, "+line 64");

    const code = await stopTerminalUi(app);

    assert.equal(code, 0);
    const plain = stripAnsi(output.text);
    assert(plain.includes("生成一个展示页面"));
    assert(plain.includes("收到，我会创建一个可验证页面。"));
    assert(plain.includes("todo: 1/3 完成"));
    assert(plain.includes("准备 file_write"));
    assert(plain.includes("success file_write showcase/index.html"));

    const debugEvents = readDebugEvents(app.debugLogPath);
    assert(debugEvents.some((record) => record.event === "input.chunk"));
    assert(debugEvents.some((record) => record.event === "protocol.send" && record.payload.record.type === "submit"));
    assert(debugEvents.some((record) => record.event === "protocol.receive.record" && record.payload.type === "ui/message"));
    assert(debugEvents.some((record) => record.event === "render.screen"));
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

function launchTerminalUi(fixtureName = "fake-bridge.js") {
  const fakeBridge = new URL(`./fixtures/${fixtureName}`, import.meta.url).pathname;
  const debugLogPath = path.join(tmpdir(), `naumi-terminal-ui-debug-${Date.now()}-${Math.random()}.jsonl`);
  const child = spawn(
    process.execPath,
    ["src/index.js", "--bridge-command", `${process.execPath} ${fakeBridge}`],
    {
      cwd: new URL("..", import.meta.url).pathname,
      stdio: ["pipe", "pipe", "pipe"],
      env: {
        ...process.env,
        FORCE_COLOR: "0",
        NAUMI_TERMINAL_UI_STATE_PATH: path.join(tmpdir(), `naumi-terminal-ui-state-${Date.now()}-${Math.random()}.json`),
        NAUMI_TERMINAL_UI_DEBUG_LOG: debugLogPath,
      },
    },
  );
  child.debugLogPath = debugLogPath;
  return child;
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

function readDebugEvents(filePath) {
  return fs.readFileSync(filePath, "utf8").trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
}
