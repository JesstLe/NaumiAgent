import test from "node:test";
import assert from "node:assert/strict";
import { ANSI, stripAnsi, visibleWidth } from "../src/ansi.js";
import { boxComponent, createRenderContext, line, renderComponent, stack } from "../src/components/core.js";
import {
  CommandCompletionFooter,
  Footer,
  HistorySearchFooter,
  InteractionFooter,
  NewOutputFooter,
  PermissionFooter,
  PromptFooter,
  TodoFooter,
} from "../src/components/footer.js";
import { Message } from "../src/components/message.js";
import { ActivityCard } from "../src/components/activity-card.js";
import { CompletionReceiptCard } from "../src/components/completion-receipt-card.js";
import { RunActivityCard } from "../src/components/run-activity-card.js";
import { PermissionCard } from "../src/components/permission-card.js";
import { InteractionCard } from "../src/components/interaction-card.js";
import { parsePermissionPanel, PermissionPanel } from "../src/components/permission-panel.js";
import { ToolCard } from "../src/components/tool-card.js";
import { parseTaskPanel, renderTaskPanel, TaskPanel } from "../src/components/task-panel.js";
import { createInitialState } from "../src/state.js";
import { setInputText } from "../src/input-buffer.js";
import {
  dismissSlashCompletion,
  moveSlashCompletionSelection,
  syncSlashCompletion,
} from "../src/slash-completion.js";
import { detachTimeline, jumpTimelineToLatest, markTimelineOutput } from "../src/timeline-follow.js";

test("component core composes nested stacks and boxes within width", () => {
  const lines = renderComponent(
    boxComponent("group", [
      line("第一行"),
      (ctx) => stack([line("第二行"), line("第三行")], ctx),
    ]),
    { width: 48 },
  );

  assert(lines.some((item) => stripAnsi(item).includes("group")));
  assert(lines.some((item) => stripAnsi(item).includes("第一行")));
  assert(lines.every((item) => visibleWidth(item) <= 48));
});

test("interaction card and footer expose choices, custom input, and answered state", () => {
  const payload = {
    header: "实现策略",
    question: "请选择持久化范围",
    options: [
      { value: "workspace", label: "工作区", description: "同一仓库共享" },
      { value: "session", label: "当前会话", description: "仅本会话" },
    ],
    allow_custom: true,
    custom_label: "其他方案",
    status: "needs_input",
  };
  const card = renderComponent(InteractionCard({ interaction: payload }), { width: 52 })
    .map(stripAnsi)
    .join("\n");
  assert.match(card, /等待你的选择/);
  assert.match(card, /工作区.*同一仓库共享/);
  assert.match(card, /其他方案/);

  const interaction = {
    payload,
    selectedIndex: 1,
    customMode: false,
    input: "",
    inputCursor: 0,
  };
  const footer = InteractionFooter({ interaction }).render({ width: 64 }).map(stripAnsi).join("\n");
  assert.match(footer, /› 2\. 当前会话/);
  assert.match(footer, /↑\/↓ 选择/);

  const answered = renderComponent(InteractionCard({ interaction: {
    ...payload,
    status: "answered",
    kind: "custom",
    custom_text: "由配置决定",
  } }), { width: 52 }).map(stripAnsi).join("\n");
  assert.match(answered, /已回答/);
  assert.match(answered, /由配置决定/);
});

test("completion receipt card exposes evidence, risk, and recovery in Chinese", () => {
  const rendered = renderComponent(CompletionReceiptCard({
    receipt: {
      outcome: "partial",
      summary: "实现已落盘，但验证尚未通过。",
      changes: [{ path: "src/naumi_agent/example.py", status: "modified", additions: 8, deletions: 2 }],
      validations: [{ command: "pytest tests/unit/test_example.py -q", status: "failed", exit_code: 1, passed: 3, failed: 1, skipped: 0 }],
      unverified: ["尚未执行端到端真实场景。"],
      approvals: [{ tool_name: "bash_run", decision: "allowed_once" }],
      risks: [{ level: "high", message: "1 项验证失败。" }],
      git_state: { available: true, branch: "codex/receipt", dirty: true, ahead: 1, behind: 0 },
      next_actions: [{ label: "重试失败验证", kind: "retry_validation" }],
      duration_ms: 1530,
    },
  }), { width: 72 }).map(stripAnsi);
  const text = rendered.join("\n");

  assert.match(text, /完成回执/);
  assert.match(text, /部分完成/);
  assert.match(text, /影响.*修改 1 个文件/);
  assert.match(text, /pytest tests\/unit\/test_example.py -q/);
  assert.match(text, /风险.*1 项验证失败/);
  assert.doesNotMatch(text, /审批.*bash_run/);
  assert.match(text, /下一步.*重试失败验证/);
  assert(rendered.every((line) => visibleWidth(line) <= 72));
});

test("completion receipt card states when Git and validation evidence are absent", () => {
  const rendered = renderComponent(CompletionReceiptCard({
    receipt: {
      outcome: "failed",
      summary: "运行失败。",
      changes: [],
      validations: [],
      unverified: [],
      approvals: [],
      risks: [],
      git_state: { available: false, dirty: false },
      next_actions: [],
      duration_ms: 0,
    },
  }), { width: 48 }).map(stripAnsi).join("\n");

  assert.match(rendered, /Git 未核查/);
  assert.doesNotMatch(rendered, /未记录验证命令/);
});

test("completed delete receipt stays compact and separates runtime changes", () => {
  const changes = Array.from({ length: 6 }, (_, index) => ({
    path: `test/file-${index}.txt`,
    status: "removed_untracked",
    scope: "task",
  }));
  changes.push({
    path: ".naumi/terminal-ui-debug.jsonl",
    status: "modified",
    scope: "background",
  });
  const rendered = renderComponent(CompletionReceiptCard({
    receipt: {
      outcome: "completed",
      summary: "已删除 /workspace/test 目录及其所有内容。",
      changes,
      validations: [{
        command: "路径已不存在: /workspace/test",
        scope: "文件系统",
        status: "passed",
        exit_code: 0,
      }],
      unverified: [],
      approvals: [{ tool_name: "bash_run", decision: "bypass" }],
      risks: [],
      git_state: { available: true, branch: "main", dirty: true },
      next_actions: [],
      duration_ms: 6300,
    },
  }), { width: 72 }).map(stripAnsi).join("\n");

  assert.match(rendered, /已完成.*6\.3s/);
  assert.match(rendered, /验证 通过.*路径已不存在: \/workspace\/test/);
  assert.match(rendered, /影响.*删除 6 个文件/);
  assert.match(rendered, /工作区另有 1 项运行时变化/);
  assert.doesNotMatch(rendered, /验证 1\/1/);
  assert.doesNotMatch(rendered, /bash_run/);
  assert.doesNotMatch(rendered, /test\/file-0\.txt/);
});

test("semantic message component delegates tool cards and markdown text", () => {
  const toolLines = renderComponent(
    Message({
      message: {
        kind: "tool",
        name: "file_edit",
        primary: "demo.py",
        status: "success",
        output: "@@\n-old\n+new",
      },
    }),
    { width: 72 },
  );
  const assistantLines = renderComponent(
    Message({ message: { kind: "assistant", content: "```python\nreturn True\n```" } }),
    { width: 72 },
  );

  assert(toolLines.some((item) => item.includes(`${ANSI.green}+new${ANSI.reset}`)));
  assert(assistantLines.some((item) => item.includes(`${ANSI.cyan}return${ANSI.reset}`)));
});

test("user delivery card is right indented and exposes failure recovery", () => {
  const rendered = renderComponent(Message({
    message: {
      kind: "user",
      content: "请修复测试",
      deliveryStatus: "failed",
      errorMessage: "Bridge 已断开",
    },
  }), { width: 80 }).map(stripAnsi);

  assert(rendered.some((line) => line.startsWith(" ".repeat(20)) && line.includes("你  请修复测试")));
  assert.match(rendered.join("\n"), /发送失败.*\/retry/);
  assert(rendered.every((line) => visibleWidth(line) <= 80));
});

test("queued and uncertain user deliveries have distinct text status", () => {
  const queued = renderComponent(Message({
    message: { kind: "user", content: "第一条", deliveryStatus: "queued" },
  }), { width: 60 }).map(stripAnsi).join("\n");
  const uncertain = renderComponent(Message({
    message: { kind: "user", content: "第二条", deliveryStatus: "uncertain" },
  }), { width: 60 }).map(stripAnsi).join("\n");

  assert.match(queued, /发送中/);
  assert.match(uncertain, /发送状态待确认.*可能重复发送/);
});

test("task user message exposes task identity and lifecycle without color", () => {
  const running = renderComponent(Message({
    message: {
      kind: "user",
      content: "实现任务联动",
      intent: "task",
      taskId: "7",
      taskStatus: "running",
      deliveryStatus: "accepted",
    },
  }), { width: 72 }).map(stripAnsi).join("\n");
  const queued = renderComponent(Message({
    message: {
      kind: "user",
      content: "创建任务",
      intent: "task",
      deliveryStatus: "queued",
    },
  }), { width: 72 }).map(stripAnsi).join("\n");

  assert.match(running, /任务 #7 · 进行中/);
  assert.match(queued, /任务 · 创建中/);
  assert.match(queued, /发送中/);
});

test("footer components can render independently or as a full footer", () => {
  const state = createInitialState();
  state.permission = { requestId: "p1", payload: { tool_name: "bash_run", reason: "需要确认" } };
  state.todo = { total: 2, completed: 1, current: { id: "2", subject: "验证", status: "in_progress" } };

  const permission = renderComponent(PermissionFooter({ permission: state.permission }), { width: 80 });
  const todo = renderComponent(TodoFooter({ todo: state.todo }), { width: 80 });
  const full = renderComponent(Footer({ state, env: { cwd: "/tmp", home: "/Users/lv" } }), { width: 80 });

  assert(stripAnsi(permission.join("\n")).includes("permission: bash_run"));
  assert(stripAnsi(todo.join("\n")).includes("todo: 1/2 完成"));
  assert(stripAnsi(full.join("\n")).includes("Shift+Tab 模式"));
  assert(full.every((item) => visibleWidth(item) <= 80));
});

test("composer prompt states chat or task intent in text", () => {
  const state = createInitialState();
  state.mode = "bypass";

  const chat = PromptFooter({ state }).render({ width: 60 }).map(stripAnsi).join("\n");
  state.composerIntent = "task";
  const task = PromptFooter({ state }).render({ width: 60 }).map(stripAnsi).join("\n");

  assert.match(chat, /^chat > /);
  assert.match(task, /^task > /);
});

test("status footer states cancellation without relying on color", () => {
  const state = createInitialState();
  state.running = true;
  state.cancelPending = true;

  const rendered = Footer({ state }).render({ width: 120 }).map(stripAnsi).join("\n");

  assert.match(rendered, /运行: 正在停止/);
});

test("new output footer appears only while detached with unread output", () => {
  const state = createInitialState();
  detachTimeline(state, 10);
  markTimelineOutput(state, {
    type: "ui/message",
    seq: 1,
    payload: { type: "assistant_stream", phase: "token", content: "继续" },
  }, "assistant-1");

  const detached = renderComponent(NewOutputFooter({ state }), { width: 80 }).map(stripAnsi);
  assert.equal(detached.length, 1);
  assert.match(detached[0], /有 1 条新输出/);
  assert.match(detached[0], /End\/Ctrl\+L 跳到最新/);

  jumpTimelineToLatest(state);
  assert.deepEqual(renderComponent(NewOutputFooter({ state }), { width: 80 }), []);
});

test("prompt renders multiline text without flattening logical newlines", () => {
  const state = createInitialState();
  setInputText(state, "检查 API\n然后修复测试");

  const lines = renderComponent(
    PromptFooter({ state }),
    createRenderContext({ width: 40, state }),
  ).map(stripAnsi);

  assert.equal(lines.length, 2);
  assert.match(lines[0], /检查 API/);
  assert.doesNotMatch(lines[0], /然后修复测试/);
  assert.match(lines[1], /然后修复测试.*▌/);
  assert(lines.every((line) => visibleWidth(line) <= 40));
});

test("prompt shows at most six wrapped rows around the cursor", () => {
  const state = createInitialState();
  setInputText(
    state,
    Array.from({ length: 10 }, (_, index) => `line-${index}`).join("\n"),
  );

  const lines = renderComponent(
    PromptFooter({ state }),
    createRenderContext({ width: 24, state }),
  ).map(stripAnsi);

  assert.equal(lines.length, 6);
  assert(lines.at(-1).includes("line-9▌"));
  assert(!lines.some((line) => line.includes("line-0")));
});

test("history search footer shows query, selection, and flattened preview within width", () => {
  const state = createInitialState();
  state.historySearch = {
    open: true,
    query: "测试",
    matches: ["修复测试\n然后运行验证", "补充测试"],
    selectedIndex: 0,
    draftText: "",
    draftCursor: 0,
  };

  const lines = renderComponent(
    HistorySearchFooter({ state }),
    createRenderContext({ width: 48, state }),
  ).map(stripAnsi);

  assert.match(lines.join("\n"), /历史搜索/);
  assert.match(lines.join("\n"), /测试/);
  assert.match(lines.join("\n"), /1\/2/);
  assert.match(lines.join("\n"), /修复测试 然后运行验证/);
  assert(lines.every((line) => visibleWidth(line) <= 48));
});

test("history search owns the footer instead of slash completion", () => {
  const state = createInitialState();
  setInputText(state, "/d");
  state.historySearch.open = true;
  state.historySearch.matches = [];

  const completion = renderComponent(
    CommandCompletionFooter({ state }),
    createRenderContext({ width: 80, state }),
  );
  const full = renderComponent(
    Footer({ state }),
    createRenderContext({ width: 80, state }),
  ).map(stripAnsi).join("\n");

  assert.deepEqual(completion, []);
  assert.match(full, /没有匹配记录/);
  assert.doesNotMatch(full, /命令补全/);
  assert.match(full, /Ctrl\+R/);
});

test("slash completion footer marks keyboard selection and respects dismissal", () => {
  const state = createInitialState();
  state.slashCommands = [
    { command: "/debug", description: "调试" },
    { command: "/delete", description: "删除" },
    { command: "/doctor", description: "诊断" },
  ];
  setInputText(state, "/d");
  syncSlashCompletion(state);
  moveSlashCompletionSelection(state, "next");

  const selected = renderComponent(
    CommandCompletionFooter({ state }),
    createRenderContext({ width: 52, state }),
  ).map(stripAnsi);
  assert.match(selected.join("\n"), /> 02\./);
  assert(selected.every((line) => visibleWidth(line) <= 52));

  dismissSlashCompletion(state);
  assert.deepEqual(renderComponent(
    CommandCompletionFooter({ state }),
    createRenderContext({ width: 52, state }),
  ), []);
});

test("tool card component preserves existing diff folding behavior", () => {
  const card = renderComponent(
    ToolCard({
      tool: {
        name: "file_write",
        primary: "large.md",
        status: "success",
        prepareTitle: "准备 file_write",
        preparePhase: "snapshot",
        prepareMetrics: { argumentChars: 4096, contentLines: 80, elapsedMs: 1800 },
        output: Array.from({ length: 80 }, (_, index) => (index % 2 === 0 ? `+line ${index}` : ` line ${index}`)).join("\n"),
      },
    }),
    { width: 90 },
  );

  assert(card.some((item) => stripAnsi(item).includes("file_write large.md")));
  assert(card.some((item) => stripAnsi(item).includes("生成中 [")));
  assert(card.some((item) => stripAnsi(item).includes("80 lines")));
  assert(!stripAnsi(card.join("\n")).includes("+line 78"));
});

test("activity card renders live operation details within width", () => {
  const card = renderComponent(
    ActivityCard({
      activity: {
        status: "running",
        title: "准备 file_write",
        phase: "snapshot",
        metrics: {
          argumentChars: 4096,
          contentChars: 12000,
          contentLines: 88,
          elapsedMs: 2400,
        },
        details: ["路径: showcase/index.html", "内容: 88 行", "参数: 4096 字符"],
      },
    }),
    { width: 72 },
  );
  const plain = stripAnsi(card.join("\n"));

  assert(plain.includes("activity"));
  assert(plain.includes("running 准备 file_write"));
  assert(plain.includes("生成中 ["));
  assert(plain.includes("参数 4.1K chars"));
  assert(plain.includes("88 lines"));
  assert(plain.includes("2.4s"));
  assert(plain.includes("路径: showcase/index.html"));
  assert(card.every((item) => visibleWidth(item) <= 72));
});

test("run activity card states phase, tool counts, and outcome within width", () => {
  const activity = {
    status: "running",
    phase: "awaiting_permission",
    phaseLabel: "等待权限",
    turn: 2,
    model: "model-a",
    permissionCount: 1,
    toolCalls: {
      a: { status: "success" },
      b: { status: "error" },
      c: { status: "running" },
    },
    perfPhases: [{ label: "记忆召回", durationMs: 42 }],
  };

  const running = RunActivityCard({ activity }).render({ width: 64 }).map(stripAnsi);
  activity.status = "completed";
  activity.phase = "completed";
  activity.phaseLabel = "执行完成";
  activity.durationMs = 1250;
  const completed = RunActivityCard({ activity }).render({ width: 64 }).map(stripAnsi);
  activity.status = "failed";
  activity.phase = "failed";
  activity.phaseLabel = "执行失败";
  const failed = RunActivityCard({ activity }).render({ width: 64 }).map(stripAnsi);
  activity.status = "cancelled";
  activity.phase = "cancelled";
  activity.phaseLabel = "运行取消";
  activity.durationMs = 1200;
  const cancelled = RunActivityCard({ activity }).render({ width: 64 }).map(stripAnsi);

  assert.match(running.join("\n"), /等待权限/);
  assert.match(running.join("\n"), /工具 2\/3/);
  assert.match(running.join("\n"), /失败 1/);
  assert.match(running.join("\n"), /权限请求 1/);
  assert.match(completed.join("\n"), /已完成/);
  assert.match(completed.join("\n"), /耗时 1\.3s/);
  assert.match(failed.join("\n"), /失败 · 执行失败/);
  assert.match(cancelled.join("\n"), /已取消/);
  assert(running.every((line) => visibleWidth(line) <= 64));
  assert(completed.every((line) => visibleWidth(line) <= 64));
  assert(failed.every((line) => visibleWidth(line) <= 64));
  assert(cancelled.every((line) => visibleWidth(line) <= 64));
});

test("permission card renders confirmation path as a structured dialog", () => {
  const card = renderComponent(
    PermissionCard({
      permission: {
        message: {
          tool_name: "bash_run",
          status: "needs_confirmation",
          reason: "需要启动本地预览服务。",
          requires_confirmation: true,
          choices: ["allow_once", "deny", "grant_session"],
        },
      },
    }),
    { width: 78 },
  );
  const plain = stripAnsi(card.join("\n"));

  assert(plain.includes("+ permission"));
  assert(plain.includes("需要确认 permission: bash_run"));
  assert(plain.includes("原因: 需要启动本地预览服务。"));
  assert(plain.includes("y=允许一次"));
  assert(plain.includes("g=本会话授权"));
  assert(plain.includes("b/Shift+Tab=全权限"));
  assert(card.every((item) => visibleWidth(item) <= 78));
});

test("permission messages delegate to the structured permission card", () => {
  const rendered = renderComponent(
    Message({
      message: {
        kind: "permission",
        requestId: "perm-1",
        message: {
          tool_name: "file_write",
          status: "allowed",
          choice: "allow_once",
          reason: "写入 demo 文件。",
          requires_confirmation: false,
        },
      },
    }),
    { width: 80 },
  );
  const plain = stripAnsi(rendered.join("\n"));

  assert(plain.includes("+ permission"));
  assert(plain.includes("已允许 permission: file_write"));
  assert(plain.includes("结果: 允许"));
});

test("permission panel summarizes pending and history sections", () => {
  const content = [
    "权限面板",
    "mode: bypass | permission: bypass",
    "Pending",
    "  - perm-1 main -> bash_run [needs_confirmation] 风险:high · 来源:TOOL_PERMISSIONS:bash_run · 模式:bypass/permissive/moderate · 确认:需要确认 · bypass 全权限放行；不执行确认、路径、命令与次数检查 | 需要确认",
    "History",
    "  - hist-1 coder -> file_write [confirmed] 用户已允许",
  ].join("\n");
  const model = parsePermissionPanel(content);
  const rendered = renderComponent(PermissionPanel({ content }), { width: 90 });
  const plain = stripAnsi(rendered.join("\n"));

  assert(model.summary.includes("pending 1"));
  assert(model.summary.includes("history 1"));
  assert(plain.includes("+ permissions"));
  assert(plain.includes("perm-1 main -> bash_run"));
  assert(plain.includes("来源:TOOL_PERMISSIONS:bash_run"));
  assert(plain.includes("确认:需要确认"));
  assert(plain.includes("bypass 全权限放行"));
  assert(rendered.every((item) => visibleWidth(item) <= 90));
});

test("semantic event messages render as structured cards instead of JSON fallback", () => {
  const messages = [
    {
      kind: "runtime_notification",
      message: { source: "background", title: "后台任务完成", count: 1, preview: "server ready" },
    },
    {
      kind: "subagent_event",
      message: { agent_name: "reviewer", task_id: "task-1", status: "completed", message: "审查完成" },
    },
    {
      kind: "team_event",
      message: { event_type: "handoff", sender: "planner", recipient: "coder", priority: "high", message: "交接实现" },
    },
    {
      kind: "context_compact",
      message: { before: 260000, after: 90000, archived_tool_results: 3, preserved_sections: ["todo"], warnings: ["接近上限"] },
    },
    {
      kind: "recovery",
      message: { phase: "completed", action: "继续输出", reason: "模型输出中断", before: 1, after: 2, unit: "chunk" },
    },
  ];

  const rendered = messages.flatMap((message) => renderComponent(Message({ message }), { width: 88 }));
  const plain = stripAnsi(rendered.join("\n"));

  assert(plain.includes("background"));
  assert(plain.includes("后台任务完成"));
  assert(plain.includes("subagent"));
  assert(plain.includes("reviewer"));
  assert(plain.includes("team"));
  assert(plain.includes("planner -> coder"));
  assert(plain.includes("compact 260000 -> 90000"));
  assert(plain.includes("recovery"));
  assert(!plain.includes('{"source"'));
  assert(rendered.every((item) => visibleWidth(item) <= 88));
});

test("task panel component structures task sections like a dedicated UI surface", () => {
  const content = [
    "\x1b[1m任务面板\x1b[0m",
    "\x1b[2mfilter: source=background status=running detail=bg_0001\x1b[0m",
    "\x1b[2m📋 1/3 [██░░░░]\x1b[0m",
    "",
    "\x1b[1mTimeline\x1b[0m",
    "  - bg_0001 [running] npm run dev | time=-; source=background; event=background:bg_0001; cwd=/tmp/project",
    "  - run_hidden [needs_input] 浏览器时间线事件 | time=2026-06-01T12:00:00; source=browser; event=browser:run_hidden; records=/tmp/browser.zip",
    "",
    "\x1b[1mDetail\x1b[0m",
    "  类型: Background",
    "  ID: bg_0001",
    "  命令: npm run dev",
    "",
    "\x1b[1mTodo\x1b[0m",
    "  ▶ 编写 CSS",
    "  - #2 [in_progress] 编写 CSS | owner=coder; blocked_by=1; blocks=3",
    "  ○ 浏览器验证",
    "",
    "\x1b[1mSubagent\x1b[0m",
    "  当前没有可见子 Agent 活动",
    "",
    "\x1b[1mBackground\x1b[0m",
    "  ▶ ⏱12s npm run dev",
    "  - bg_0001 [running] npm run dev | cwd=/tmp/project; pid=4242; ports=5173; output=/tmp/bg.log",
    "",
    "\x1b[1mBrowser Runs\x1b[0m",
    "  - run_1 [needs_input] 打开页面 | steps=3; created=2026-06-01T12:00:00; current=等待用户选择页面元素",
  ].join("\n");
  const model = parseTaskPanel(content);
  const rendered = renderComponent(
    TaskPanel({
      content,
      taskPanel: {
        selectedId: "bg_0001",
        expandedIds: { bg_0001: true },
        collapsedTimelineSources: { browser: true },
        focused: true,
      },
    }),
    { width: 84 },
  );
  const plain = stripAnsi(rendered.join("\n"));

  assert.equal(model.summary, "filter source=background status=running detail=bg_0001 | todo 1/3 | timeline 2 | background 1 | browser 1");
  assert(plain.includes("filter source=background status=running detail=bg_0001"));
  assert(plain.includes("Timeline"));
  assert(plain.includes("sources: background 1"));
  assert(plain.includes("browser 1 folded"));
  assert(!plain.includes("浏览器时间线事件"));
  assert(plain.includes("event=background:bg_0001"));
  assert(plain.includes("Detail"));
  assert(plain.includes("类型: Background"));
  assert(plain.includes("ID: bg_0001"));
  assert(plain.includes("Todo"));
  assert(plain.includes("编写 CSS"));
  assert(plain.includes("owner=coder"));
  assert(plain.includes("blocked_by=1"));
  assert(plain.includes("Background"));
  assert(plain.includes("> - bg_0001 [running] npm run dev"));
  assert(plain.includes("event flow"));
  assert(plain.includes("cwd=/tmp/project"));
  assert(plain.includes("ports=5173"));
  assert(plain.includes("current=等待用户选择页面元素"));
  assert(rendered.every((item) => visibleWidth(item) <= 84));
});

test("system task notices use the dedicated task panel renderer", () => {
  const rendered = renderComponent(
    Message({
      message: {
        kind: "system",
        title: "tasks",
        level: "info",
        content: "任务面板\nTodo\n  - #1 [running] 写入页面\nBackground\n  暂无后台任务",
      },
    }),
    { width: 80 },
  );
  const plain = stripAnsi(rendered.join("\n"));

  assert(plain.includes("+ tasks"));
  assert(plain.includes("tasks todo 1"));
  assert(plain.includes("#1 [running] 写入页面"));
  assert(!plain.includes("tasks: 任务面板"));
});

test("task panel enriches todo rows with workbench issue metadata", () => {
  const content = ["任务面板", "Todo", "  ● #1 实现任务市场"].join("\n");
  const rendered = renderTaskPanel(content, 90, {
    width: 90,
    state: {
      workbench: {
        issues: [
          {
            task_id: "1",
            risk_level: "high",
            parallel_mode: "exclusive",
            related_worktree: "issue-1-backend",
          },
        ],
      },
    },
  });
  const plain = stripAnsi(rendered.join("\n"));

  assert(plain.includes("risk:high"));
  assert(plain.includes("exclusive"));
  assert(plain.includes("issue-1-backend"));
});

test("task panel off notice renders as plain system text", () => {
  const rendered = renderComponent(
    Message({
      message: {
        kind: "system",
        title: "任务面板",
        level: "info",
        content: "已取消钉住。",
      },
    }),
    { width: 80 },
  );
  const plain = stripAnsi(rendered.join("\n"));

  assert(!plain.includes("+ tasks"));
  assert(plain.includes("任务面板: 已取消钉住。"));
});
