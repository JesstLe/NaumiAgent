import test from "node:test";
import assert from "node:assert/strict";
import { ANSI, stripAnsi, visibleWidth } from "../src/ansi.js";
import { boxComponent, createRenderContext, line, renderComponent, stack } from "../src/components/core.js";
import {
  CommandCompletionFooter,
  Footer,
  HistorySearchFooter,
  NewOutputFooter,
  PermissionFooter,
  PromptFooter,
  TodoFooter,
} from "../src/components/footer.js";
import { Message } from "../src/components/message.js";
import { ActivityCard } from "../src/components/activity-card.js";
import { PermissionCard } from "../src/components/permission-card.js";
import { parsePermissionPanel, PermissionPanel } from "../src/components/permission-panel.js";
import { ToolCard } from "../src/components/tool-card.js";
import { parseTaskPanel, renderTaskPanel, TaskPanel } from "../src/components/task-panel.js";
import { createInitialState } from "../src/state.js";
import { setInputText } from "../src/input-buffer.js";
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

test("permission card renders confirmation path as a structured dialog", () => {
  const card = renderComponent(
    PermissionCard({
      permission: {
        message: {
          tool_name: "bash_run",
          status: "needs_confirmation",
          reason: "需要启动本地预览服务。",
          requires_confirmation: true,
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
          choice: "allow",
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
    "  - perm-1 main -> bash_run [needs_confirmation] 风险:high · 来源:TOOL_PERMISSIONS:bash_run · 模式:bypass/permissive/moderate · 确认:需要确认 · bypass 允许；跳过逐次确认和路径沙箱，危险命令仍拦截 | 需要确认",
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
  assert(plain.includes("bypass 允许"));
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
