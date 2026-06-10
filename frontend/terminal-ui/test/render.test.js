import test from "node:test";
import assert from "node:assert/strict";
import { ANSI, stripAnsi, visibleWidth } from "../src/ansi.js";
import { createInitialState, reduceServerEvent } from "../src/state.js";
import { renderFooter, renderMarkdownExcerpt, renderScreen, renderToolCard, renderToolOutput } from "../src/render.js";

test("markdown code blocks show a bounded excerpt with lightweight highlighting", () => {
  const codeLines = Array.from({ length: 45 }, (_, index) => `const value${index} = ${index};`);
  const rendered = renderMarkdownExcerpt(["```js", ...codeLines, "```"].join("\n"), 120);
  const plain = rendered.map(stripAnsi);

  assert(plain.includes("... 已折叠 5 行代码"));
  assert(rendered.some((line) => line.includes(`${ANSI.cyan}const${ANSI.reset}`)));
});

test("tool output metadata can force raw content to render as code", () => {
  const rendered = renderToolOutput("return True", 120, {
    format: "code",
    language: "python",
  });

  assert(rendered.some((line) => line.includes(`${ANSI.cyan}return${ANSI.reset}`)));
  assert(rendered.some((line) => line.includes(`${ANSI.yellow}True${ANSI.reset}`)));
});

test("markdown and diff folds can be expanded through persisted fold state", () => {
  const codeLines = Array.from({ length: 45 }, (_, index) => `const value${index} = ${index};`);
  const collapsedCode = renderMarkdownExcerpt(["```js", ...codeLines, "```"].join("\n"), 120, {
    foldKey: "message:a",
    folds: {},
  }).map(stripAnsi);
  const expandedCode = renderMarkdownExcerpt(["```js", ...codeLines, "```"].join("\n"), 120, {
    foldKey: "message:a",
    folds: { "message:a:code:0": { expanded: true } },
  }).map(stripAnsi);

  assert(collapsedCode.includes("... 已折叠 5 行代码"));
  assert(expandedCode.some((line) => line.includes("value44")));

  const diff = ["@@", ...Array.from({ length: 65 }, (_, index) => `+line ${index}`)].join("\n");
  const collapsedDiff = renderToolOutput(diff, 120, { foldKey: "tool:t", folds: {} }).map(stripAnsi);
  const expandedDiff = renderToolOutput(diff, 120, {
    foldKey: "tool:t",
    folds: { "tool:t": { expanded: true } },
  }).map(stripAnsi);

  assert(collapsedDiff.includes("... 已折叠 48 行 diff"));
  assert(expandedDiff.some((line) => line.includes("+line 64")));
});

test("tool card renders diff output inside a bounded card", () => {
  const card = renderToolCard(
    {
      kind: "tool",
      name: "file_edit",
      primary: "demo.py",
      status: "success",
      output: "--- a/demo.py\n+++ b/demo.py\n@@\n-old\n+new",
      outputLength: 0,
    },
    80,
  );

  assert(card.some((line) => line.includes("+ tool")));
  assert(card.some((line) => line.includes(`${ANSI.green}+new${ANSI.reset}`)));
  assert(card.every((line) => visibleWidth(line) <= 80));
});

test("footer truncates status without overflowing terminal width", () => {
  const state = createInitialState();
  state.mode = "bypass";
  state.status = {
    model: "openai/kimi-for-coding",
    workspace_root: "/Users/lv/Workspace/NaumiAgent/some/extremely/long/workspace/path",
    usage: { total_tokens: 12345 },
    context: { used: 88000, window: 256000, percentage: 34.5 },
    budget: { used_usd: 0.3, max_usd: 5 },
    git: { branch: "main", dirty: true },
  };

  const footer = renderFooter(state, 72, { cwd: "/tmp", home: "/Users/lv" });

  assert(footer.every((line) => visibleWidth(line) <= 72));
  assert(stripAnsi(footer[0]).includes("mode: bypass"));
});

test("footer shows compact task activity when backend reports active work", () => {
  const state = createInitialState();
  state.status = {
    model: "openai/kimi-for-coding",
    workspace_root: "/Users/lv/Workspace/NaumiAgent",
    usage: { total_tokens: 12345 },
    context: { used: 12000, window: 256000, percentage: 4.7 },
    budget: { used_usd: 0.03, max_usd: 5 },
    tasks: {
      background_running: 2,
      background_attention: 1,
      subagents_active: 3,
      browser_active: 1,
      permissions_pending: 1,
    },
  };

  const footer = renderFooter(state, 220, { cwd: "/tmp", home: "/Users/lv" }).map(stripAnsi).join("\n");

  assert(footer.includes("tasks: bg 2 bg! 1 agent 3 browser 1 perm 1"));
});

test("footer shows 首字时间", () => {
  const state = createInitialState();
  state.status = {
    model: "openai/kimi-for-coding",
    workspace_root: "/Users/lv/Workspace/NaumiAgent",
    usage: { total_tokens: 0 },
    context: { used: 0, window: 256000, percentage: 0 },
    budget: { used_usd: 0, max_usd: 5 },
  };
  state.lastFirstTokenLatencyMs = 1532;

  const footer = renderFooter(state, 220, { cwd: "/tmp", home: "/Users/lv" }).map(stripAnsi).join("\n");

  assert(footer.includes("首字: 1.5s"));
});

test("screen renderer reserves footer lines and keeps prompt visible", () => {
  const state = createInitialState();
  state.input = "hello";
  state.messages = Array.from({ length: 20 }, (_, index) => ({ kind: "assistant", content: `line ${index}` }));

  const lines = renderScreen(state, 60, 12, { cwd: "/tmp", home: "/Users/lv" });
  const plain = lines.map(stripAnsi);

  assert.equal(lines.length, 12);
  assert(plain.some((line) => line.includes("default > hello")));
  assert(lines.every((line) => visibleWidth(line) <= 60));
});

test("screen renderer clamps oversized footer in tiny terminals", () => {
  const state = createInitialState();
  state.mode = "bypass";
  state.input = "确认一下";
  state.permission = {
    requestId: "perm-1",
    payload: {
      tool_name: "bash_run",
      reason: "需要确认一个非常长的命令说明，窄窗口下会换成很多行。",
    },
  };
  state.todo = {
    total: 4,
    completed: 1,
    current: { id: 2, subject: "继续写入非常长的前端文件并验证", status: "in_progress" },
  };
  state.status = {
    model: "openai/kimi-for-coding",
    workspace_root: "/Users/lv/Workspace/NaumiAgent/very/deep/path",
    usage: { total_tokens: 999 },
    context: { used: 240000, window: 256000, percentage: 93.7 },
    budget: { used_usd: 1.23, max_usd: 5 },
    git: { branch: "main", dirty: true },
  };
  state.messages = Array.from({ length: 8 }, (_, index) => ({ kind: "assistant", content: `正文 ${index}` }));

  const lines = renderScreen(state, 34, 5, { cwd: "/tmp", home: "/Users/lv" });
  const plain = lines.map(stripAnsi);

  assert.equal(lines.length, 5);
  assert(plain.some((line) => line.includes("bypass > 确认一下")));
  assert(plain.some((line) => line.includes("permission: bash_run")));
  assert(!plain.some((line) => line.includes("Shift+Tab 模式")));
  assert(lines.every((line) => visibleWidth(line) <= 34));
});

test("screen renderer stays stable after resume replay then a new run starts", () => {
  const state = createInitialState();
  reduceServerEvent(state, {
    type: "session/replayed",
    payload: { session_id: "resume-1", title: "恢复测试", clear: true },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "user", content: "继续检查 config.yaml" },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "assistant_stream", phase: "token", content: "我先读取配置。" },
  });
  reduceServerEvent(state, {
    type: "runtime/status",
    payload: {
      mode: "default",
      model: "openai/kimi-for-coding",
      workspace_root: "/Users/lv/Workspace/NaumiAgent",
      usage: { total_tokens: 42 },
      context: { used: 12000, window: 256000, percentage: 4.7 },
      budget: { used_usd: 0.01, max_usd: 5 },
      git: { branch: "main", dirty: true },
    },
  });
  reduceServerEvent(state, { type: "run/started", payload: {} });
  reduceServerEvent(state, { type: "user/message", payload: { content: "恢复后继续执行" } });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "assistant_stream", phase: "start" },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "assistant_stream", phase: "token", content: "收到，继续处理。" },
  });

  const lines = renderScreen(state, 72, 10, { cwd: "/tmp", home: "/Users/lv" });
  const plain = lines.map(stripAnsi);

  assert.equal(lines.length, 10);
  assert.equal(plain.filter((line) => line.includes("mode: default")).length, 1);
  assert(plain.some((line) => line.includes("default running")));
  assert(plain.some((line) => line.includes("运行中")));
  assert(!plain.some((line) => line.includes("todo:")));
  assert(lines.every((line) => visibleWidth(line) <= 72));
});

test("screen renderer keeps oversized task panel header visible", () => {
  const state = createInitialState();
  state.status = {
    model: "openai/kimi-for-coding",
    workspace_root: "/Users/lv/Workspace/NaumiAgent",
    usage: { total_tokens: 100 },
    context: { used: 1000, window: 256000, percentage: 0.4 },
    budget: { used_usd: 0.01, max_usd: 5 },
    git: { branch: "main", dirty: true },
  };
  state.messages = [{
    kind: "system",
    id: "tasks-long",
    title: "tasks",
    level: "info",
    content: [
      "任务面板",
      "Timeline",
      ...Array.from({ length: 30 }, (_, index) => `  - bg_${String(index).padStart(4, "0")} [running] task ${index} | source=background; event=background:bg_${index}`),
      "Background",
      ...Array.from({ length: 12 }, (_, index) => `  - bg_${index} [running] command ${index}`),
    ].join("\n"),
  }];

  const lines = renderScreen(state, 96, 30, { cwd: "/tmp", home: "/Users/lv" });
  const plain = lines.map(stripAnsi);

  assert(plain.some((line) => line.includes("+ tasks")));
  assert(plain.some((line) => line.includes("tasks timeline")));
  assert(plain.some((line) => line.includes("还有")));
  assert(lines.every((line) => visibleWidth(line) <= 96));
});

test("footer renders session id from state.currentSessionId", () => {
  const state = createInitialState();
  state.currentSessionId = "sess-abc-2026-06-03";
  state.status = {
    model: "openai/kimi-for-coding",
    workspace_root: "/Users/lv/Workspace/NaumiAgent",
    usage: { total_tokens: 0 },
    context: { used: 0, window: 256000, percentage: 0 },
    budget: { used_usd: 0, max_usd: 5 },
  };

  const footer = renderFooter(state, 140, { cwd: "/tmp", home: "/Users/lv" }).map(stripAnsi).join("\n");

  assert(footer.includes("会话:sess-abc"));
  assert(footer.includes("mode: default"));
});
