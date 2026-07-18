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
import { renderWorkbenchOverview } from "../src/components/workbench-overview.js";

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

function workbenchOverviewFixture() {
  return {
    schema_version: 1,
    stream_id: "stream-overview",
    revision: 7,
    generated_at: "2026-07-17T14:20:00+08:00",
    full: true,
    session_id: "session-workbench",
    counts: { missions: 1, tasks: 1, worktrees: 1, reviews: 1, failures: 1 },
    active_selection: {
      mission_id: "mission-1",
      task_id: "task-1",
      worktree: "ui-10-overview",
      review_id: "approval-1",
    },
    summary: { active_agents: 1, open_issues: 1, blocked_issues: 0, pending_approvals: 1, failed_validations: 1 },
    missions: [{ id: "mission-1", title: "完善终端 Workbench", goal: "让用户直接掌握工程进展", status: "active" }],
    tasks: [{ id: "task-1", subject: "实现 Overview", description: "展示目标、验证与风险", status: "in_progress", owner: "" }],
    issues: [{ task_id: "task-1", mission_id: "mission-1", risk_level: "high", related_branch: "codex/ui-10-overview", related_worktree: "ui-10-overview", related_pr: "#128", expected_artifacts: ["snapshot.png"], acceptance_criteria: ["80/120/200 列无溢出"] }],
    leases: [{ task_id: "task-1", agent_id: "Frontend-Agent", state: "active", worktree_name: "ui-10-overview" }],
    worktrees_status: "ready",
    worktrees_code: "",
    worktrees: [{
      name: "ui-10-overview",
      path: "/repo/.naumi/worktrees/ui-10-overview",
      branch: "codex/ui-10-overview",
      status: "dirty",
      task_id: "task-1",
      dirty_files: 2,
      commits_ahead: 1,
      removable: false,
      task: { id: "task-1", subject: "实现 Overview", status: "in_progress" },
      lease: { id: "lease-1", state: "active", expires_at: "2099-01-01T00:00:00Z" },
      agent_id: "Frontend-Agent",
    }],
    validation_runs: [{ id: "validation-1", task_id: "task-1", command: ["node", "--test", "render.test.js"], status: "failed", exit_code: 1, output: "PRIVATE_VALIDATION_OUTPUT", started_at: "2026-07-17T14:00:00+08:00", completed_at: "2026-07-17T14:00:02+08:00" }],
    failures: [{ id: "failure-1", task_id: "task-1", kind: "test_failed", title: "窄屏快照失败", detail: "中文行宽超出", status: "open" }],
    approvals: [{ id: "approval-1", task_id: "task-1", state: "waiting", title: "等待 UI 审查", requester: "Frontend-Agent" }],
    events: [],
    loading: false,
    error: "",
  };
}

test("workbench overview renders authoritative wide and narrow fields", () => {
  const view = workbenchOverviewFixture();
  for (const width of [80, 120, 200]) {
    const rendered = renderWorkbenchOverview(view, width, 24);
    const plain = rendered.map(stripAnsi).join("\n");
    assert.equal(rendered.length, 24);
    assert(rendered.every((line) => visibleWidth(line) <= width));
    assert(plain.includes("Workbench"));
    assert(plain.includes("完善终端 Workbench"));
    assert(plain.includes("实现 Overview"));
    assert(plain.includes("Frontend-Agent"));
    assert(plain.includes("codex/ui-10-overview"));
    assert(plain.includes("ui-10-overview"));
    assert(plain.includes("验证失败"));
    assert(plain.includes("高风险"));
    assert(plain.includes("窄屏快照失败"));
    assert(!plain.includes("PRIVATE_VALIDATION_OUTPUT"));
  }
  const wide = renderWorkbenchOverview(view, 120, 24).join("\n");
  assert(wide.includes(`${ANSI.red}`));
  assert(renderWorkbenchOverview(view, 120, 24).map(stripAnsi).some((line) => line.includes("│")));
});

test("workbench overview distinguishes loading empty and error without list explosions", () => {
  const loading = renderWorkbenchOverview({ loading: true, revision: 0 }, 80, 12).map(stripAnsi).join("\n");
  assert(loading.includes("正在加载 Workbench"));

  const empty = renderWorkbenchOverview({ ...workbenchOverviewFixture(), missions: [], tasks: [], issues: [], counts: { missions: 0, tasks: 0, worktrees: 0, reviews: 0, failures: 0 } }, 80, 12).map(stripAnsi).join("\n");
  assert(empty.includes("暂无 Workbench 任务"));

  const error = renderWorkbenchOverview({ loading: false, error: "状态库暂不可用", revision: 0 }, 80, 12).map(stripAnsi).join("\n");
  assert(error.includes("加载失败"));
  assert(error.includes("状态库暂不可用"));

  const crowded = workbenchOverviewFixture();
  crowded.counts = { missions: 1, tasks: 100, worktrees: 100, reviews: 100, failures: 100 };
  crowded.leases = Array.from({ length: 100 }, (_, index) => ({ task_id: `task-${index}`, agent_id: `agent-${index}`, worktree_name: `worktree-${index}` }));
  crowded.approvals = Array.from({ length: 100 }, (_, index) => ({ id: `approval-${index}`, task_id: `task-${index}`, state: "waiting", title: `审查 ${index}` }));
  const rendered = renderWorkbenchOverview(crowded, 80, 16);
  assert.equal(rendered.length, 16);
  assert(rendered.every((line) => visibleWidth(line) <= 80));
  assert(rendered.map(stripAnsi).join("\n").includes("worktree 100"));
  assert(rendered.map(stripAnsi).join("\n").includes("待审 100"));
});

test("workbench route takes full-screen priority and keeps the footer usable", () => {
  const state = createInitialState();
  state.route = { name: "workbench", originAnchor: { scrollOffset: 0, followTail: true } };
  state.workbench = workbenchOverviewFixture();

  const rendered = renderScreen(state, 120, 24).map(stripAnsi);
  const plain = rendered.join("\n");
  assert(plain.includes("Workbench Overview"));
  assert(plain.includes("workbench: Tab 标签 · r 刷新 · Esc 返回"));
  assert(!plain.includes("chat >"));
  assert(!plain.includes("NAUMI"));
  assert.equal(rendered.length, 24);
});

test("workbench Worktrees tab renders real status and bounded 0/1/100 navigation", () => {
  const view = { ...workbenchOverviewFixture(), selected_tab: "worktrees", selected_worktree_name: "ui-10-overview" };
  for (const width of [80, 120, 200]) {
    const rendered = renderWorkbenchOverview(view, width, 24);
    const plain = rendered.map(stripAnsi).join("\n");
    assert.equal(rendered.length, 24);
    assert(rendered.every((line) => visibleWidth(line) <= width));
    assert(plain.includes("Worktrees"));
    assert(plain.includes("ui-10-overview"));
    assert(plain.includes("codex/ui-10-overview"));
    assert(plain.includes("Frontend-Agent"));
    assert(plain.includes("未提交 2"));
    assert(plain.includes("领先 1"));
    assert(plain.includes("不可安全移除"));
  }

  const empty = {
    ...view,
    worktrees: [],
    counts: { ...view.counts, worktrees: 0 },
    selected_worktree_name: "",
  };
  assert(renderWorkbenchOverview(empty, 80, 12).map(stripAnsi).join("\n").includes("暂无由 NaumiAgent 管理的 worktree"));

  const crowded = { ...view };
  crowded.worktrees = Array.from({ length: 100 }, (_, index) => ({
    ...view.worktrees[0],
    name: `worktree-${index}`,
    path: `/repo/.naumi/worktrees/worktree-${index}`,
    branch: `codex/worktree-${index}`,
    task_id: `task-${index}`,
    task: { id: `task-${index}`, subject: `任务 ${index}`, status: "in_progress" },
    agent_id: `agent-${index}`,
  }));
  crowded.counts = { ...view.counts, worktrees: 100 };
  crowded.selected_worktree_name = "worktree-99";
  const rendered = renderWorkbenchOverview(crowded, 80, 18);
  const plain = rendered.map(stripAnsi).join("\n");
  assert(rendered.every((line) => visibleWidth(line) <= 80));
  assert(plain.includes("worktree-99"));
  assert(!plain.includes("worktree-50"));

  const unavailable = { ...view, worktrees_status: "unavailable", worktrees_code: "worktree_snapshot_failed", worktrees: [] };
  assert(renderWorkbenchOverview(unavailable, 80, 12).map(stripAnsi).join("\n").includes("Worktree 状态暂不可用"));
});

test("workbench Reviews tab renders checks blockers files and semantic diff colors", () => {
  const view = {
    ...workbenchOverviewFixture(),
    selected_tab: "reviews",
    selected_review_id: "approval-1",
    review_loading: false,
    review_error: "",
    review_detail: {
      schema_version: 1,
      session_id: "session-workbench",
      review_id: "approval-1",
      status: "ready",
      evidence: {
        approval: {
          id: "approval-1",
          task_id: "task-1",
          title: "等待 UI 审查",
          detail: "确认真实差异与验证结果",
          requester: "Frontend-Agent",
        },
        worktree: { name: "ui-10-overview", path: "/repo/wt", status: "present" },
        validation_runs: [{ status: "failed", command: ["node", "--test"], exit_code: 1 }],
        changed_files: [{ path: "src/ui.js", status: "modified" }],
        diff_hunks: [{ path: "src/ui.js", patch: "@@ -1 +1 @@\n-old\n+new" }],
      },
    },
  };

  for (const width of [80, 120, 200]) {
    const rendered = renderWorkbenchOverview(view, width, 24);
    const plain = rendered.map(stripAnsi).join("\n");
    assert(rendered.every((line) => visibleWidth(line) <= width));
    assert(plain.includes("Reviews"));
    assert(plain.includes("阻塞 · 1 项验证失败"));
    assert(plain.includes("src/ui.js"));
    assert(plain.includes("-old"));
    assert(rendered.join("\n").includes(ANSI.red));
    assert(rendered.join("\n").includes(ANSI.green));
  }
});

test("workbench Reviews tab renders Proposal preview and decision form at common widths", () => {
  const proposal = {
    id: "proposal-1", state: "open", title: "优化 footer 截断", risk_level: "medium",
    proposal_kind: "code", source_kind: "evolution_candidate",
    source_id: `evc_${"a".repeat(24)}`, source_revision: 4,
    impact_scope: "frontend/terminal-ui/src/components/footer.js",
    agent_id: "Evolution-Agent", task_id: "task-1",
    intended_files: ["frontend/terminal-ui/src/components/footer.js"],
    validation_plan: ["node --test footer.test.js"],
  };
  const view = {
    ...workbenchOverviewFixture(),
    selected_tab: "reviews",
    selected_review_id: "proposal-1",
    selected_review_kind: "proposal",
    approvals: [],
    proposals: [proposal],
    proposal_action: {
      proposal_id: "proposal-1", action: "reject", phase: "note", input: "证据不足",
    },
  };

  for (const width of [80, 120, 200]) {
    const rendered = renderWorkbenchOverview(view, width, 26);
    const plain = rendered.map(stripAnsi).join("\n");
    assert(rendered.every((line) => visibleWidth(line) <= width));
    assert(plain.includes("Proposal"));
    assert(plain.includes("优化 footer 截断"));
    assert(plain.includes("批准只进入下一 policy gate"));
    assert(plain.includes("拒绝原因"));
    assert(plain.includes("证据不足"));
  }
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

test("footer only surfaces non-verified model capability contracts", () => {
  const state = createInitialState();
  state.status.model_contract = { status: "verified", warnings: [], errors: [] };
  const verified = renderFooter(state, 160).map(stripAnsi).join("\n");
  assert.doesNotMatch(verified, /模型契约:/);

  state.status.model_contract = { status: "unverified", warnings: ["token 上限未验证"], errors: [] };
  const unverified = renderFooter(state, 160).map(stripAnsi).join("\n");
  assert.match(unverified, /模型契约: 未验证/);

  state.status.model_contract = { status: "incompatible", warnings: [], errors: ["不支持工具调用"] };
  const incompatible = renderFooter(state, 160).map(stripAnsi).join("\n");
  assert.match(incompatible, /模型契约: 不兼容/);
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
