import test from "node:test";
import assert from "node:assert/strict";
import { ANSI, stripAnsi, visibleWidth } from "../src/ansi.js";
import { createInitialState, reduceServerEvent } from "../src/state.js";
import { setInputText } from "../src/input-buffer.js";
import {
  captureViewportAnchor,
  renderFooter,
  renderMarkdownExcerpt,
  renderScreen,
  renderToolCard,
  renderToolOutput,
  restoreViewportAnchor,
} from "../src/render.js";
import { detachTimeline } from "../src/timeline-follow.js";
import { renderRuntimeInspector } from "../src/components/runtime-inspector.js";
import { renderAgentControlPage } from "../src/components/agent-control-page.js";

test("conversation viewport renders welcome before the timeline and keeps footer usable", () => {
  const state = createInitialState();
  reduceServerEvent(state, {
    type: "ready",
    payload: {
      version: "0.1.214",
      workspace_root: "/Users/lv/Workspace/NaumiAgent",
      model: "openai/gpt-5.4",
      provider: "openai",
      api_format: "openai_responses",
      upstream_model: "gpt-5.4-2026-06-01",
      mode: "default",
      permission_mode: "moderate",
    },
  });

  const lines = renderScreen(state, 120, 24, {
    cwd: "/tmp",
    home: "/Users/lv",
  });
  const plain = lines.map(stripAnsi).join("\n");
  assert.match(plain, /NaumiAgent v0\.1\.214/);
  assert.match(plain, /模型 openai\/gpt-5\.4/);
  assert.match(plain, /提供方 openai · 接口 OpenAI Responses/);
  assert.match(plain, /提供方: openai\/OpenAI Responses/);
  assert.match(plain, /chat >/);
  assert.equal(lines.length, 24);
  assert(lines.every((line) => visibleWidth(line) <= 120));
});

test("welcome resize tiers stay bounded and dismissed state reveals the timeline", () => {
  const state = createInitialState();
  state.welcome.phase = "ready_empty";
  state.status = {
    version: "0.1.214",
    workspace_root: "/very/long/workspace/path/for/naumi-agent",
    model: "anthropic/claude-opus-4-6",
    mode: "default",
    permission_mode: "moderate",
  };
  for (const [width, height] of [[120, 24], [80, 14], [48, 10], [23, 5]]) {
    const lines = renderScreen(state, width, height, { cwd: "/tmp" });
    assert.equal(lines.length, height);
    assert(lines.every((line) => visibleWidth(line) <= width));
  }

  state.welcome = { phase: "dismissed", dismissed: true };
  state.messages.push({ kind: "assistant", id: "a1", content: "正常时间线" });
  const dismissed = renderScreen(state, 100, 16).map(stripAnsi).join("\n");
  assert.doesNotMatch(dismissed, /NaumiAgent v0\.1\.214/);
  assert.match(dismissed, /正常时间线/);
});

test("active working tail renders responsive image through run phases", () => {
  const state = createInitialState();
  state.welcome = { phase: "dismissed", dismissed: true };
  state.status = {
    model: "openai/kimi-for-coding",
    workspace_root: "/Users/lv/Workspace/NaumiAgent",
    usage: { total_tokens: 0 },
    context: { used: 0, window: 256000, percentage: 0 },
    budget: { enabled: false, used_usd: 0, max_usd: null },
  };

  reduceServerEvent(state, {
    type: "run/started",
    request_id: "run-working-tail",
    payload: { task: "渲染动态图" },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: { type: "assistant_stream", phase: "start" },
  });
  state.workingAnimationFrame = 1;

  const wide = renderScreen(state, 100, 30, { term: "xterm-256color" }).map(stripAnsi);
  assert(wide.some((line) => line.includes("╭─────╮")));
  assert(wide.some((line) => line.includes("◓")));
  assert(wide.some((line) => line.includes("模型工作中 · 生成响应")));

  const compact = renderScreen(state, 60, 12, { term: "xterm-256color" }).map(stripAnsi);
  assert(compact.some((line) => line.includes("◓ 模型工作中 · 生成响应")));
  assert(!compact.some((line) => line.includes("╭─────╮")));

  reduceServerEvent(state, {
    type: "permission/request",
    request_id: "permission-working-tail",
    payload: { tool_name: "bash_run", reason: "需要确认" },
  });
  const waiting = renderScreen(state, 100, 30, { term: "xterm-256color" }).map(stripAnsi);
  assert(waiting.some((line) => line.includes("等待权限确认")));
  assert(!waiting.some((line) => line.includes("模型工作中")));

  reduceServerEvent(state, {
    type: "permission/resolved",
    payload: { request_id: "permission-working-tail", choice: "allow_once" },
  });
  const resumed = renderScreen(state, 100, 30, { term: "xterm-256color" }).map(stripAnsi);
  assert(resumed.some((line) => line.includes("工具执行中")));

  reduceServerEvent(state, {
    type: "run/completed",
    request_id: "run-working-tail",
    payload: { status: "completed" },
  });
  const completed = renderScreen(state, 100, 30, { term: "xterm-256color" }).map(stripAnsi);
  assert(!completed.some((line) => /模型工作中|工具执行中|等待权限确认/.test(line)));
});

test("active working tail keeps scroll anchors stable across frames", () => {
  const state = createInitialState();
  state.welcome = { phase: "dismissed", dismissed: true };
  state.messages = Array.from({ length: 12 }, (_, index) => ({
    kind: "assistant",
    id: `working-anchor-${index}`,
    content: `动画锚点消息 ${index}: ${"稳定正文 ".repeat(30)}`,
  }));
  reduceServerEvent(state, {
    type: "run/started",
    request_id: "run-working-anchor",
    payload: { task: "验证动画滚动锚点" },
  });
  detachTimeline(state, 16);

  state.workingAnimationFrame = 0;
  const first = captureViewportAnchor(state, 100, 16, { term: "xterm" });
  state.workingAnimationFrame = 3;
  const second = captureViewportAnchor(state, 100, 16, { term: "xterm" });

  assert(first);
  assert.equal(second.messageId, first.messageId);
  assert.equal(second.messageIndex, first.messageIndex);
  assert(restoreViewportAnchor(state, second, 60, 16, { term: "xterm" }) > 0);
  assert.equal(
    captureViewportAnchor(state, 60, 16, { term: "xterm" }).messageId,
    first.messageId,
  );
});

test("inspector and agent pages take priority over the startup welcome", () => {
  const state = createInitialState();
  state.inspector.open = true;
  state.inspector.loading = true;
  assert.doesNotMatch(renderScreen(state, 80, 14).map(stripAnsi).join("\n"), /NAUMI/);

  state.inspector.open = false;
  state.route = { name: "agents", originAnchor: null };
  state.agents.open = true;
  state.agents.loading = true;
  assert.doesNotMatch(renderScreen(state, 80, 14).map(stripAnsi).join("\n"), /NAUMI/);
});

test("agent control page renders bounded wide and narrow authoritative layouts", () => {
  const state = createInitialState();
  state.currentSessionId = "session-agents";
  state.route = { name: "agents", originAnchor: null };
  state.agents.open = true;
  state.agents.selectedTab = "executions";
  state.agents.selectedByTab.executions = "task-1";
  state.agents.detailId = "task-1";
  state.agents.revision = 3;
  state.agents.snapshot = {
    schema_version: 1,
    session_id: "session-agents",
    revision: 3,
    generated_at: "2026-07-13T00:00:00+00:00",
    summary: { total_agents: 1, active_agents: 1, attention_agents: 0, stoppable_executions: 1, pending_messages: 0 },
    agents: [{ name: "coder", description: "编程 Agent", kind: "preset", state: "running", task_count: 1, model_tier: "capable", capabilities: ["代码"], tools: ["file_read"], permission_level: "moderate", age_ms: 500, heartbeat_age_ms: 100 }],
    executions: [{ task_id: "task-1", session_id: "session-agents", agent_name: "coder", description: "实现控制中心", status: "running", phase: "running_tool", started_at: 1, finished_at: null, elapsed_ms: 1000, heartbeat_age_ms: 100, current_tool: "file_read", recent_tools: ["file_read"], total_tokens: 42, total_cost_usd: 0.01, turns: 2, error: "", stop_supported: true, stop_requested: false }],
    team_messages: [],
    blackboard: [],
    warnings: [],
  };

  const wide = renderAgentControlPage(state.agents, 120, 20).map(stripAnsi);
  const narrow = renderAgentControlPage(state.agents, 72, 16).map(stripAnsi);
  assert(wide.some((line) => line.includes("Agent Control Center")));
  assert(wide.some((line) => line.includes("task-1")));
  assert(wide.some((line) => line.includes("当前工具 · file_read")));
  assert(narrow.some((line) => line.includes("执行详情")));
  assert.equal(wide.length, 20);
  assert.equal(narrow.length, 16);
  assert(renderAgentControlPage(state.agents, 120, 20).every((line) => visibleWidth(line) <= 120));
  assert(renderAgentControlPage(state.agents, 72, 16).every((line) => visibleWidth(line) <= 72));
});

test("agent control page distinguishes loading empty stale and error states", () => {
  const state = createInitialState();
  state.agents.open = true;
  state.agents.loading = true;
  assert(renderAgentControlPage(state.agents, 80, 10).map(stripAnsi).join("\n").includes("正在加载"));

  state.agents.loading = false;
  state.agents.snapshot = { summary: {}, agents: [], executions: [], team_messages: [], blackboard: [], warnings: [], revision: 1, generated_at: "now" };
  assert(renderAgentControlPage(state.agents, 80, 10).map(stripAnsi).join("\n").includes("暂无 Agent"));

  state.agents.stale = true;
  state.agents.error = "刷新失败";
  assert(renderAgentControlPage(state.agents, 80, 10).map(stripAnsi).join("\n").includes("已过期"));

  state.agents.snapshot = null;
  assert(renderAgentControlPage(state.agents, 80, 10).map(stripAnsi).join("\n").includes("加载失败"));
});

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

test("footer wraps complete status fields without ellipsis", () => {
  const state = createInitialState();
  state.mode = "bypass";
  state.status = {
    model: "openai/kimi-for-coding",
    provider: "openai",
    api_format: "openai_chat",
    workspace_root: "/Users/lv/Workspace/NaumiAgent/some/extremely/long/workspace/path",
    usage: { total_tokens: 12345 },
    context: { used: 88000, window: 256000, percentage: 34.5 },
    budget: { used_usd: 0.3, max_usd: 5 },
    git: { branch: "main", dirty: true },
  };

  const footer = renderFooter(state, 72, { cwd: "/tmp", home: "/Users/lv" });

  assert(footer.every((line) => visibleWidth(line) <= 72));
  assert(stripAnsi(footer[0]).includes("mode: bypass"));
  const plain = footer.map(stripAnsi).join("\n");
  assert.match(plain, /预算: \$0\.3000\/\$5\.00/);
  assert.match(plain, /提供方: openai\/OpenAI Chat/);
  assert.match(plain, /openai\/kimi-for-coding/);
  assert.match(plain, /main\*/);
  assert.doesNotMatch(plain, /…/);
});

test("footer renders unlimited budget without inventing a zero cap", () => {
  const state = createInitialState();
  state.status = {
    model: "openai/kimi-for-coding",
    workspace_root: "/Users/lv/Workspace/NaumiAgent",
    usage: { total_tokens: 50 },
    context: { used: 20, window: 256000, percentage: 0.1 },
    budget: { enabled: false, used_usd: 0.0123, max_usd: null },
  };

  const footer = renderFooter(state, 220, { cwd: "/tmp", home: "/Users/lv" })
    .map(stripAnsi)
    .join("\n");

  assert.match(footer, /预算: 不限 · 已用 \$0\.0123/);
  assert.doesNotMatch(footer, /\/\$0\.00/);
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
      queued_conversations: 2,
      permissions_pending: 1,
    },
  };

  const footer = renderFooter(state, 220, { cwd: "/tmp", home: "/Users/lv" }).map(stripAnsi).join("\n");

  assert(footer.includes("tasks: bg 2 bg! 1 agent 3 browser 1 queue 2 perm 1"));
});

test("footer only surfaces heartbeat when the Bridge is stale", () => {
  const state = createInitialState();
  state.bridgeHeartbeat = { status: "healthy", rttMs: 25, ageMs: 0 };
  const healthy = renderFooter(state, 160).map(stripAnsi).join("\n");
  assert.doesNotMatch(healthy, /Bridge:/);

  state.bridgeHeartbeat = { status: "stale", rttMs: null, ageMs: 17_000 };
  const stale = renderFooter(state, 160).map(stripAnsi).join("\n");
  assert.match(stale, /Bridge: 无响应/);
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
  assert(plain.some((line) => line.includes("chat > hello")));
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
  assert(plain.some((line) => line.includes("chat > 确认一下")));
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
  assert(plain.some((line) => line.includes("chat running")));
  assert(plain.some((line) => line.includes("运行中")));
  assert(!plain.some((line) => line.includes("todo:")));
  assert(lines.every((line) => visibleWidth(line) <= 72));
});

test("screen renderer keeps oversized task panel header visible", () => {
  const state = createInitialState();
  state.welcome = { phase: "dismissed", dismissed: true };
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

test("tiny terminal preserves the multiline composer cursor row", () => {
  const state = createInitialState();
  state.permission = {
    requestId: "p1",
    payload: { tool_name: "bash_run", reason: "需要确认" },
  };
  setInputText(state, "第一行\n第二行\n最后一行");

  const screen = renderScreen(state, 60, 4).map(stripAnsi);

  assert.equal(screen.length, 4);
  assert(screen.some((line) => line.includes("最后一行▌")));
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

test("resize anchor preserves the top visible message across width changes", () => {
  const state = createInitialState();
  state.messages = Array.from({ length: 12 }, (_, index) => ({
    kind: "assistant",
    id: `assistant-${index}`,
    content: `消息 ${index}: ${"这是一段用于验证终端宽度变化后阅读位置保持稳定的内容。".repeat(4)}`,
  }));
  detachTimeline(state, 16);

  const wideAnchor = captureViewportAnchor(state, 120, 16);
  assert(wideAnchor);
  assert.match(wideAnchor.messageId, /^assistant-/);

  const narrowOffset = restoreViewportAnchor(state, wideAnchor, 60, 16);
  const narrowAnchor = captureViewportAnchor(state, 60, 16);
  assert(narrowOffset > 0);
  assert.equal(narrowAnchor.messageId, wideAnchor.messageId);

  const restoredOffset = restoreViewportAnchor(state, narrowAnchor, 120, 16);
  const restoredAnchor = captureViewportAnchor(state, 120, 16);
  assert(restoredOffset > 0);
  assert.equal(restoredAnchor.messageId, wideAnchor.messageId);
});

test("resize anchor falls back to message index when ids are absent", () => {
  const state = createInitialState();
  state.messages = Array.from({ length: 10 }, (_, index) => ({
    kind: "assistant",
    content: `无 ID 消息 ${index}: ${"内容 ".repeat(30)}`,
  }));
  detachTimeline(state, 12);

  const anchor = captureViewportAnchor(state, 100, 14);
  assert(anchor);
  assert.equal(anchor.messageId, "");
  restoreViewportAnchor(state, anchor, 60, 14);

  assert.equal(captureViewportAnchor(state, 60, 14).messageIndex, anchor.messageIndex);
});

test("resize anchor keeps follow mode pinned to the latest output", () => {
  const state = createInitialState();
  state.messages = Array.from({ length: 20 }, (_, index) => ({
    kind: "assistant",
    id: `assistant-${index}`,
    content: `跟随消息 ${index} ${"正文 ".repeat(20)}`,
  }));

  assert.equal(captureViewportAnchor(state, 120, 16), null);
  assert.equal(restoreViewportAnchor(state, null, 60, 16), 0);
  assert.equal(state.followTail, true);
  assert.equal(state.scrollOffset, 0);
});

test("resize anchor clamps the offset when its message disappeared", () => {
  const state = createInitialState();
  state.messages = [{ kind: "assistant", id: "remaining", content: "仍然存在" }];
  detachTimeline(state, 200);

  const offset = restoreViewportAnchor(
    state,
    { messageId: "removed", messageIndex: 99 },
    80,
    14,
  );

  assert.equal(offset, 0);
  assert.equal(state.followTail, true);
});

test("runtime inspector renders authoritative empty states without inventing activity", () => {
  const state = createInitialState();
  state.inspector.open = true;
  state.inspector.snapshot = runtimeInspectorRenderFixture(1, { empty: true });
  state.inspector.revision = 1;

  for (const [tab, expected] of [
    ["plan", "尚未产生计划"],
    ["tools", "尚未调用工具"],
    ["changes", "尚未记录文件改动"],
    ["tests", "尚未记录验证"],
  ]) {
    state.inspector.selectedTab = tab;
    const plain = renderRuntimeInspector(state.inspector, 46, 18).map(stripAnsi).join("\n");
    assert.match(plain, new RegExp(expected));
  }
});

test("runtime inspector uses drawer overlay and page layouts across breakpoints", () => {
  const state = createInitialState();
  state.currentSessionId = "session-layout";
  state.input = "继续实现";
  state.messages = [{ kind: "assistant", id: "timeline-1", content: "时间线正文仍然可见" }];
  state.permission = {
    requestId: "permission-layout",
    payload: { tool_name: "bash_run", reason: "需要确认真实命令" },
  };
  state.inspector.open = true;
  state.inspector.snapshot = runtimeInspectorRenderFixture(2);
  state.inspector.revision = 2;
  const scrollOffset = state.scrollOffset;

  const wide = renderScreen(state, 140, 20).map(stripAnsi);
  assert(wide.some((line) => line.includes("时间线正文仍然可见")));
  assert(wide.some((line) => line.includes("Runtime Inspector")));
  assert(wide.some((line) => line.includes("实现运行检查器")));
  assert(wide.some((line) => line.includes("permission: bash_run")));
  assert.equal(wide.find((line) => line.includes("┌ Runtime Inspector")).indexOf("┌"), 94);

  const overlay = renderScreen(state, 110, 20).map(stripAnsi);
  assert(overlay.some((line) => line.includes("时间线正文仍然可见")));
  assert(overlay.some((line) => line.includes("Runtime Inspector")));
  assert(overlay.some((line) => line.includes("permission: bash_run")));
  assert.equal(overlay.find((line) => line.includes("┌ Runtime Inspector")).indexOf("┌"), 73);

  state.messages = [{
    kind: "assistant",
    id: "timeline-wide-cjk",
    content: "超长中文时间线".repeat(80),
  }];
  const cjkOverlay = renderScreen(state, 110, 20);
  assert(cjkOverlay.every((line) => visibleWidth(line) <= 110));

  const page = renderScreen(state, 80, 20).map(stripAnsi);
  assert(page.some((line) => line.includes("Runtime Inspector")));
  assert(page.some((line) => line.includes("实现运行检查器")));
  assert(!page.some((line) => line.includes("时间线正文仍然可见")));
  assert(page.some((line) => line.includes("chat > 继续实现")));
  assert(page.some((line) => line.includes("permission: bash_run")));

  for (const [lines, width] of [[wide, 140], [overlay, 110], [page, 80]]) {
    assert.equal(lines.length, 20);
    assert(lines.every((line) => visibleWidth(line) <= width));
  }
  assert.equal(state.scrollOffset, scrollOffset);
});

test("runtime inspector never exceeds an extremely small terminal", () => {
  const state = createInitialState();
  state.inspector.open = true;
  state.inspector.snapshot = runtimeInspectorRenderFixture(1);
  state.input = "x";

  const lines = renderScreen(state, 12, 3);

  assert.equal(lines.length, 3);
  assert(lines.every((line) => visibleWidth(line) <= 12));
  assert(lines.map(stripAnsi).some((line) => line.includes("chat > x")));
});

test("runtime inspector renders unlimited budget within its width", () => {
  const state = createInitialState();
  state.inspector.open = true;
  state.inspector.selectedTab = "context";
  state.inspector.snapshot = runtimeInspectorRenderFixture(3);
  state.inspector.snapshot.context = {
    state: "ready",
    budget_enabled: false,
    budget_used_usd: 0.0123,
    budget_max_usd: null,
    budget_percentage: null,
    budget_max_input_tokens: null,
    budget_max_output_tokens: null,
    input_tokens: 42,
    output_tokens: 8,
    turns: 1,
    warnings: [],
  };

  const lines = renderRuntimeInspector(state.inspector, 54, 18);
  const plain = lines.map(stripAnsi).join("\n");

  assert.match(plain, /预算: 不限 · 已用 \$0\.0123/);
  assert(lines.every((line) => visibleWidth(line) <= 54));
});

function runtimeInspectorRenderFixture(revision, { empty = false } = {}) {
  const state = empty ? "empty" : "ready";
  return {
    schema_version: 1,
    session_id: "session-layout",
    revision,
    generated_at: "2026-07-13T00:00:00+00:00",
    active_run_id: "run-layout",
    plan: {
      state,
      items: empty ? [] : [{ id: "todo-1", subject: "实现运行检查器", status: "in_progress", blocked_by: [] }],
      next_actions: [],
      warnings: [],
    },
    tools: { state, items: [], approvals: [], warnings: [] },
    context: { state, warnings: [] },
    changes: { state, items: [], git_state: { available: true, branch: "main", dirty: true }, warnings: [] },
    tests: { state, validations: [], unverified: [], next_actions: [], warnings: [] },
  };
}
