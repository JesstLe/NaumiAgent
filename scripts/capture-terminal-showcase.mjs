import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { color, ANSI } from "../frontend/terminal-ui/src/ansi.js";
import { openCommandQuickOpen } from "../frontend/terminal-ui/src/command-quick-open.js";
import { renderScreen } from "../frontend/terminal-ui/src/render.js";
import {
  createInitialState,
  reduceServerEvent,
  submitUserMessage,
} from "../frontend/terminal-ui/src/state.js";

const OUTPUT_DIR = resolve("docs/showcase/screenshots");
const WIDTH = 132;
const HEIGHT = 36;

mkdirSync(OUTPUT_DIR, { recursive: true });

function readyState() {
  const state = createInitialState();
  reduceServerEvent(state, {
    type: "ready",
    payload: {
      version: "0.1.214",
      workspace_root: "E:\\Workspace\\NaumiAgent",
      model: "opencode/kimi-k3",
      provider: "opencode",
      api_format: "anthropic_messages",
      upstream_model: "kimi-k3",
      reasoning_effort: { configured: "auto", effective: "auto" },
      mode: "default",
      permission_mode: "moderate",
      usage: { total_tokens: 0 },
      context: { used: 0, window: 256000, percentage: 0 },
      budget: { enabled: false, used_usd: 0, max_usd: null },
      git: { branch: "main", dirty: true },
    },
  });
  return state;
}

function welcomeScreen() {
  return renderScreen(readyState(), WIDTH, HEIGHT, terminalEnvironment());
}

function onboardingScreen() {
  const cyan = (value) => color(ANSI.cyan, value);
  const green = (value) => color(ANSI.green, value);
  const dim = (value) => color(ANSI.dim, value);
  const panelWidth = 64;
  const lines = [
    "",
    cyan(`╭${"─".repeat(panelWidth - 2)}╮`),
    `${cyan("│")}${centerAnsi("🚀 首次启动", panelWidth - 2)}${cyan("│")}`,
    `${cyan("│")}${" ".repeat(panelWidth - 2)}${cyan("│")}`,
    `${cyan("│")}${padAnsi("  欢迎使用 NaumiAgent — 通用智能 Agent", panelWidth - 2)}${cyan("│")}`,
    `${cyan("│")}${padAnsi("  接下来需要配置模型密钥和基本偏好。", panelWidth - 2)}${cyan("│")}`,
    cyan(`╰${"─".repeat(panelWidth - 2)}╯`),
    "",
    "选择模型提供商:",
    `${green("  › 1. OpenCode Zen Go")}    ${dim("当前工作区配置")}`,
    "    2. Kimi Coding API",
    "    3. OpenAI",
    "    4. Anthropic",
    "    5. 自定义 API",
    "",
    `${cyan("输入编号或名称")} [opencode]: ${green("opencode")}`,
    "",
    `${green("✓")} 检测到模型凭据已保存在系统安全存储`,
    `${green("✓")} 默认模型  opencode/kimi-k3`,
    `${green("✓")} 快速模型  opencode/qwen3.8-flash`,
    "",
    "权限模式:",
    "    strict    严格 — 每次文件/Shell 操作都需确认",
    `${green("  › moderate  适中 — 写操作和敏感命令需确认")}`,
    "    relaxed   宽松 — mostly 自动执行",
    "",
    `${green("✓")} 配置将写入 .naumi/config.yaml（不包含密钥）`,
    `${green("✓")} 网络搜索可用（零配置，无需搜索 API Key）`,
    "",
    dim("↑/↓ 选择 · Enter 确认 · Ctrl+C 退出"),
  ];
  return centerBlock(lines, WIDTH, HEIGHT);
}

function commandGuideScreen() {
  const state = readyState();
  state.welcome.dismissed = true;
  state.welcome.phase = "dismissed";
  state.slashCommands = state.slashCommands.map((item) => ({ ...item }));
  state.commandQuickOpen.recentCommands = ["/todo", "/runtime", "/tasks"];
  openCommandQuickOpen(state);
  state.commandQuickOpen.query = "";
  return renderScreen(state, WIDTH, HEIGHT, terminalEnvironment());
}

function toolExecutionScreen() {
  const state = readyState();
  submitUserMessage(state, "检查项目结构，读取配置并执行前端验证", () => "submit-showcase");
  reduceServerEvent(state, {
    type: "run/started",
    request_id: "submit-showcase",
    payload: { run_id: "run-showcase", task: "检查项目结构，读取配置并执行前端验证" },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    request_id: "submit-showcase",
    payload: { type: "assistant_stream", phase: "start" },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    request_id: "submit-showcase",
    payload: {
      type: "assistant_stream",
      phase: "token",
      content: "我先读取前端配置与关键入口，再执行构建验证。",
    },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    request_id: "submit-showcase",
    payload: {
      type: "tool_use",
      tool_call_id: "tool-read-package",
      tool_name: "file_read",
      file_path: "frontend/web/package.json",
      primary_arg: "frontend/web/package.json",
    },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    request_id: "submit-showcase",
    payload: {
      type: "tool_result",
      tool_call_id: "tool-read-package",
      tool_name: "file_read",
      status: "success",
      duration_ms: 18,
      content_preview: "React 19 · Tailwind CSS 4 · Vite 8 · Tauri 2",
      content_length: 45,
    },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    request_id: "submit-showcase",
    payload: {
      type: "tool_use",
      tool_call_id: "tool-build-web",
      tool_name: "bash_run",
      command: "pnpm run build",
      primary_arg: "pnpm run build",
      args_summary: "{\"command\":\"pnpm run build\"}",
    },
  });
  state.workingAnimationFrame = 2;
  return renderScreen(state, WIDTH, HEIGHT, terminalEnvironment());
}

function todoScreen() {
  const state = readyState();
  submitUserMessage(state, "升级 Agent 工作台并完成验证", () => "submit-todo");
  reduceServerEvent(state, {
    type: "run/started",
    request_id: "submit-todo",
    payload: { run_id: "run-todo", task: "升级 Agent 工作台并完成验证" },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "todo_status",
      total_count: 5,
      completed_count: 2,
      open_count: 3,
      items: [
        { id: 1, subject: "核对现有界面与状态协议", status: "completed" },
        { id: 2, subject: "统一浅色视觉令牌", status: "completed" },
        { id: 3, subject: "升级聊天与工具卡片", status: "in_progress" },
        { id: 4, subject: "完善运行时侧边栏", status: "pending" },
        { id: 5, subject: "执行端到端验收", status: "pending" },
      ],
    },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "assistant_stream",
      phase: "token",
      content: "当前正在升级聊天与工具卡片；Todo 会常驻底部并随执行状态实时更新。",
    },
  });
  reduceServerEvent(state, {
    type: "ui/message",
    payload: {
      type: "tool_use",
      tool_call_id: "tool-edit-chat",
      tool_name: "file_edit",
      file_path: "frontend/web/src/components/Chat/ChatPage.tsx",
      primary_arg: "frontend/web/src/components/Chat/ChatPage.tsx",
    },
  });
  state.workingAnimationFrame = 1;
  return renderScreen(state, WIDTH, HEIGHT, terminalEnvironment());
}

function runtimeSidebarScreen() {
  const state = readyState();
  state.welcome.dismissed = true;
  state.welcome.phase = "dismissed";
  state.messages = [
    { kind: "user", id: "sidebar-user", content: "升级工作台 UI，并保留完整运行证据。" },
    {
      kind: "assistant",
      id: "sidebar-assistant",
      content: "已完成界面审计，右侧 Runtime Inspector 汇总计划、工具、上下文、改动和测试。",
    },
  ];
  state.inspector.open = true;
  state.inspector.focused = true;
  state.inspector.selectedTab = "plan";
  state.inspector.selectionByTab.plan = 2;
  state.inspector.expandedByTab.plan = { "2": true };
  state.inspector.revision = 12;
  state.inspector.snapshot = {
    session_id: "session-showcase",
    revision: 12,
    active_run_id: "run-ui-upgrade",
    plan: {
      state: "ready",
      items: [
        { id: "1", subject: "审计当前 UI 与交互协议", status: "completed", owner: "Naumi" },
        { id: "2", subject: "建立统一浅色视觉令牌", status: "completed", owner: "Naumi" },
        { id: "3", subject: "升级聊天、工具与审批体验", status: "in_progress", owner: "Naumi", active_form: "正在适配真实运行事件", blocked_by: [] },
        { id: "4", subject: "验证窗口尺寸与键盘操作", status: "pending", owner: "Naumi" },
        { id: "5", subject: "生成验收证据并提交", status: "pending", owner: "Naumi" },
      ],
      next_actions: [{ kind: "validate", label: "完成构建与端到端验证" }],
      warnings: [],
    },
    tools: {
      state: "ready",
      items: [
        { name: "file_read", status: "success", duration_ms: 18, call_id: "read-1" },
        { name: "file_edit", status: "running", duration_ms: 0, call_id: "edit-1" },
      ],
      approvals: [],
      warnings: [],
    },
    context: {
      state: "ready",
      workspace_root: "E:\\Workspace\\NaumiAgent",
      branch: "main",
      git_dirty: true,
      commit: "a384b9e0",
      model: "opencode/kimi-k3",
      runtime_mode: "default",
      permission_mode: "moderate",
      context_used: 18400,
      context_window: 256000,
      context_percentage: 7.2,
      input_tokens: 15200,
      output_tokens: 3200,
      turns: 4,
      budget_enabled: false,
      warnings: [],
    },
    changes: {
      state: "ready",
      summary: "3 个界面文件已更新",
      items: [
        { path: "frontend/web/src/index.css", status: "modified", additions: 88, deletions: 31 },
        { path: "frontend/web/src/components/Chat/ChatPage.tsx", status: "modified", additions: 142, deletions: 79 },
      ],
      warnings: [],
    },
    tests: {
      state: "ready",
      validations: [
        { command: "pnpm run build", scope: "frontend", status: "passed", exit_code: 0 },
        { command: "pnpm test", scope: "unit", status: "passed", passed: 24, failed: 0 },
      ],
      unverified: ["真实桌面窗口视觉回归待最终确认"],
      next_actions: [],
      warnings: [],
    },
  };
  return renderScreen(state, WIDTH, HEIGHT, terminalEnvironment());
}

function taskPanelScreen() {
  const state = readyState();
  state.welcome.dismissed = true;
  state.welcome.phase = "dismissed";
  const snapshot = {
    schema_version: 1,
    generated_at: "2026-09-11T09:30:00+08:00",
    filters: { source: "all", status: "all" },
    items: [
      { view_id: "todo:1", task_id: "1", source: "todo", status: "completed", title: "审计现有 UI", owner: "Naumi", age_seconds: 210, detail: "已核对 React、Tailwind 与 Tauri 入口" },
      { view_id: "todo:2", task_id: "2", source: "todo", status: "running", title: "升级聊天界面", owner: "Naumi", age_seconds: 84, detail: "正在适配工具事件与流式消息" },
      { view_id: "subagent:design", task_id: "design", source: "subagent", status: "running", title: "视觉一致性复核", owner: "design-reviewer", age_seconds: 42, detail: "检查间距、层级与响应式布局" },
      { view_id: "background:build", task_id: "build-web", source: "background", status: "running", title: "前端生产构建", owner: "background", age_seconds: 17, detail: "pnpm run build" },
      { view_id: "browser:acceptance", task_id: "acceptance", source: "browser", status: "pending", title: "桌面端视觉验收", owner: "browser", age_seconds: 0, detail: "等待构建完成后启动" },
    ],
    timeline: [
      { source: "todo", task_id: "1", status: "completed", title: "审计现有 UI" },
      { source: "todo", task_id: "2", status: "running", title: "升级聊天界面" },
      { source: "subagent", task_id: "design", status: "running", title: "视觉一致性复核" },
      { source: "background", task_id: "build-web", status: "running", title: "前端生产构建" },
      { source: "browser", task_id: "acceptance", status: "pending", title: "桌面端视觉验收" },
    ],
    warnings: [],
  };
  state.taskPanel.snapshot = snapshot;
  state.taskPanel.pinned = true;
  state.taskPanel.focused = true;
  state.taskPanel.items = snapshot.items.map((item) => ({
    id: item.view_id,
    taskId: item.task_id,
    source: item.source,
  }));
  state.taskPanel.selectedId = "todo:2";
  state.taskPanel.selectedIndex = 1;
  state.taskPanel.expandedIds = { "todo:2": true };
  state.messages = [{
    kind: "system",
    id: "tasks-showcase",
    title: "tasks",
    content: "",
    taskSnapshot: snapshot,
  }];
  state.taskPanel.messageId = "tasks-showcase";
  return renderScreen(state, WIDTH, HEIGHT, terminalEnvironment());
}

function completionScreen() {
  const state = readyState();
  submitUserMessage(state, "完成终端 UI 模块验证", () => "submit-complete");
  reduceServerEvent(state, {
    type: "run/started",
    request_id: "submit-complete",
    payload: { run_id: "run-complete", task: "完成终端 UI 模块验证" },
  });
  reduceServerEvent(state, {
    type: "completion/receipt",
    request_id: "submit-complete",
    payload: {
      schema_version: 1,
      receipt_id: "receipt-showcase",
      run_id: "run-complete",
      outcome: "completed",
      summary: "终端 UI 构建、单元测试与关键交互验证全部通过。",
      changes: [
        { path: "frontend/terminal-ui/src/render.js", status: "modified", additions: 36, deletions: 12 },
        { path: "frontend/terminal-ui/src/components/tool-card.js", status: "modified", additions: 48, deletions: 20 },
      ],
      validations: [
        { command: "npm run check", scope: "syntax", status: "passed", exit_code: 0, passed: 1, failed: 0, skipped: 0 },
        { command: "npm test", scope: "unit", status: "passed", exit_code: 0, passed: 214, failed: 0, skipped: 0 },
      ],
      unverified: [],
      approvals: [],
      risks: [],
      git_state: { available: true, branch: "main", dirty: true, commit: "a384b9e0", ahead: 0, behind: 0 },
      next_actions: [],
      evidence_refs: ["terminal-ui:test", "terminal-ui:golden-capture"],
      duration_ms: 4821,
    },
  });
  reduceServerEvent(state, {
    type: "run/completed",
    request_id: "submit-complete",
    payload: { run_id: "run-complete", receipt_id: "receipt-showcase", status: "completed" },
  });
  return renderScreen(state, WIDTH, HEIGHT, terminalEnvironment());
}

function terminalEnvironment() {
  return {
    cwd: "E:\\Workspace\\NaumiAgent",
    home: "C:\\Users\\13357",
    clockText: "09:30:12",
    term: "xterm-256color",
  };
}

function centerBlock(lines, width, height) {
  const top = Math.max(0, Math.floor((height - lines.length) / 2));
  const output = Array.from({ length: top }, () => "");
  for (const line of lines) output.push(line);
  while (output.length < height) output.push("");
  return output.slice(0, height).map((line) => `${line}${" ".repeat(Math.max(0, width - plainWidth(line)))}`);
}

function plainWidth(value) {
  return Array.from(stripAnsi(value)).reduce((total, char) => total + terminalCellWidth(char), 0);
}

function padAnsi(value, width) {
  return `${value}${" ".repeat(Math.max(0, width - plainWidth(value)))}`;
}

function centerAnsi(value, width) {
  const remaining = Math.max(0, width - plainWidth(value));
  const left = Math.floor(remaining / 2);
  return `${" ".repeat(left)}${value}${" ".repeat(remaining - left)}`;
}

function stripAnsi(value) {
  return String(value).replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, "");
}

const baseColors = {
  30: "#111827", 31: "#ff6b6b", 32: "#70d6a3", 33: "#ffd166",
  34: "#7aa2f7", 35: "#c099ff", 36: "#58d6e8", 37: "#e5e7eb",
  90: "#6b7280", 91: "#ff8787", 92: "#8ce3b5", 93: "#ffe08a",
  94: "#9ab7ff", 95: "#d1b0ff", 96: "#83e8f2", 97: "#ffffff",
};

function ansiToHtml(value) {
  const parts = String(value).split(/(\x1b\[[0-9;]*m)/g);
  const state = { fg: "#e7e9ee", bg: null, bold: false, dim: false, inverse: false };
  return parts.map((part) => {
    const match = part.match(/^\x1b\[([0-9;]*)m$/);
    if (match) {
      applySgr(state, match[1]);
      return "";
    }
    if (!part) return "";
    const fg = state.inverse ? (state.bg ?? "#11151d") : state.fg;
    const bg = state.inverse ? state.fg : state.bg;
    const styles = [
      `color:${fg}`,
      bg ? `background:${bg}` : "",
      state.bold ? "font-weight:700" : "",
      state.dim ? "opacity:.62" : "",
    ].filter(Boolean).join(";");
    return Array.from(part).map((char) => {
      const width = terminalCellWidth(char);
      const content = char === " " ? "&nbsp;" : escapeHtml(char);
      return `<span class="cell" style="width:${width}ch;${styles}">${content}</span>`;
    }).join("");
  }).join("");
}

function terminalCellWidth(char) {
  const codePoint = char.codePointAt(0) ?? 0;
  if (codePoint === 0x200d || (codePoint >= 0x300 && codePoint <= 0x36f)) return 0;
  if (
    codePoint >= 0x1100 && (
      codePoint <= 0x115f
      || codePoint === 0x2329
      || codePoint === 0x232a
      || (codePoint >= 0x2e80 && codePoint <= 0xa4cf && codePoint !== 0x303f)
      || (codePoint >= 0xac00 && codePoint <= 0xd7a3)
      || (codePoint >= 0xf900 && codePoint <= 0xfaff)
      || (codePoint >= 0xfe10 && codePoint <= 0xfe19)
      || (codePoint >= 0xfe30 && codePoint <= 0xfe6f)
      || (codePoint >= 0xff00 && codePoint <= 0xff60)
      || (codePoint >= 0xffe0 && codePoint <= 0xffe6)
      || (codePoint >= 0x1f300 && codePoint <= 0x1faff)
      || (codePoint >= 0x20000 && codePoint <= 0x3fffd)
    )
  ) return 2;
  return 1;
}

function applySgr(state, rawCodes) {
  const codes = rawCodes === "" ? [0] : rawCodes.split(";").map(Number);
  for (let index = 0; index < codes.length; index += 1) {
    const code = codes[index];
    if (code === 0) Object.assign(state, { fg: "#e7e9ee", bg: null, bold: false, dim: false, inverse: false });
    else if (code === 1) state.bold = true;
    else if (code === 2) state.dim = true;
    else if (code === 7) state.inverse = true;
    else if (code === 22) { state.bold = false; state.dim = false; }
    else if (code === 27) state.inverse = false;
    else if (code === 39) state.fg = "#e7e9ee";
    else if (code === 49) state.bg = null;
    else if (baseColors[code]) state.fg = baseColors[code];
    else if (code >= 40 && code <= 47) state.bg = baseColors[code - 10];
    else if (code >= 100 && code <= 107) state.bg = baseColors[code - 10];
    else if ((code === 38 || code === 48) && codes[index + 1] === 2) {
      const rgb = `rgb(${codes[index + 2]},${codes[index + 3]},${codes[index + 4]})`;
      if (code === 38) state.fg = rgb;
      else state.bg = rgb;
      index += 4;
    }
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function pageHtml(title, lines) {
  const renderedLines = lines.map((line) => `<div class="line">${ansiToHtml(line) || "&nbsp;"}</div>`).join("\n");
  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escapeHtml(title)}</title>
<style>
  * { box-sizing: border-box; }
  html, body { width: 100%; height: 100%; margin: 0; overflow: hidden; }
  body {
    display: grid;
    place-items: center;
    background: #07090d;
    color: #e7e9ee;
    font-family: "Cascadia Mono", "Microsoft YaHei UI", "Microsoft YaHei", monospace;
  }
  .window {
    width: 1360px;
    height: 820px;
    overflow: hidden;
    border: 1px solid #303642;
    border-radius: 10px;
    background: #10141b;
    box-shadow: 0 28px 90px rgba(0,0,0,.58), 0 0 0 1px rgba(255,255,255,.025) inset;
  }
  .titlebar {
    height: 42px;
    display: flex;
    align-items: center;
    padding: 0 16px;
    border-bottom: 1px solid #2a303a;
    background: #181d26;
    color: #aab1bd;
    font-family: "Segoe UI", sans-serif;
    font-size: 13px;
  }
  .app-icon {
    width: 18px;
    height: 18px;
    display: grid;
    place-items: center;
    margin-right: 10px;
    border: 1px solid #58d6e8;
    color: #58d6e8;
    font-size: 10px;
    transform: rotate(45deg);
  }
  .app-icon span { transform: rotate(-45deg); }
  .terminal {
    height: calc(100% - 42px);
    overflow: hidden;
    padding: 19px 22px;
    background: radial-gradient(circle at 50% -20%, rgba(88,214,232,.055), transparent 45%), #10141b;
    font-size: 14px;
    line-height: 1.42;
    letter-spacing: 0;
    font-variant-ligatures: none;
  }
  .line { min-height: 19.88px; height: 19.88px; white-space: nowrap; }
  .cell { display: inline-block; height: 19.88px; vertical-align: top; overflow: visible; }
</style>
</head>
<body>
  <section class="window" aria-label="NaumiAgent terminal screenshot">
    <header class="titlebar"><div class="app-icon"><span>N</span></div>${escapeHtml(title)} · PowerShell</header>
    <main class="terminal">${renderedLines}</main>
  </section>
</body>
</html>`;
}

function overviewHtml() {
  const cards = [
    ["启动欢迎与 ASCII Logo", "01-welcome-logo.png"],
    ["工具调用与执行状态", "04-tool-execution.png"],
    ["Todo 常驻进度", "05-todo-progress.png"],
    ["Runtime Inspector 侧边栏", "06-runtime-sidebar.png"],
  ];
  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NaumiAgent 终端交互一览</title>
<style>
  * { box-sizing: border-box; }
  html, body { width: 100%; min-height: 100%; margin: 0; }
  body { padding: 34px 42px; background: #090c11; color: #edf1f7; font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif; }
  header { display: flex; align-items: end; justify-content: space-between; margin-bottom: 22px; }
  h1 { margin: 0; font-size: 28px; letter-spacing: -.02em; }
  .meta { color: #8f9aaa; font-family: "Cascadia Mono", monospace; font-size: 12px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  figure { margin: 0; overflow: hidden; border: 1px solid #2b3340; border-radius: 9px; background: #11161e; box-shadow: 0 16px 44px rgba(0,0,0,.28); }
  img { display: block; width: 100%; aspect-ratio: 16 / 10; object-fit: cover; object-position: center; }
  figcaption { padding: 11px 14px 12px; border-top: 1px solid #29313c; color: #b8c1ce; font-size: 14px; }
  .accent { color: #58d6e8; }
</style>
</head>
<body>
  <header>
    <h1><span class="accent">NaumiAgent</span> 终端交互一览</h1>
    <div class="meta">v0.1.214 · main · 2026-09-11</div>
  </header>
  <main class="grid">
    ${cards.map(([label, src]) => `<figure><img src="${src}" alt="${label}"><figcaption>${label}</figcaption></figure>`).join("\n")}
  </main>
</body>
</html>`;
}

const captures = [
  ["01-welcome-logo", "NaumiAgent 启动欢迎页", welcomeScreen()],
  ["02-first-run-guide", "NaumiAgent 首次启动引导", onboardingScreen()],
  ["03-command-guide", "NaumiAgent 命令引导", commandGuideScreen()],
  ["04-tool-execution", "NaumiAgent 工具执行过程", toolExecutionScreen()],
  ["05-todo-progress", "NaumiAgent Todo 进度", todoScreen()],
  ["06-runtime-sidebar", "NaumiAgent Runtime Inspector", runtimeSidebarScreen()],
  ["07-task-panel", "NaumiAgent 任务面板", taskPanelScreen()],
  ["08-completion-receipt", "NaumiAgent 完成回执", completionScreen()],
];

for (const [slug, title, lines] of captures) {
  writeFileSync(resolve(OUTPUT_DIR, `${slug}.html`), pageHtml(title, lines), "utf8");
  writeFileSync(resolve(OUTPUT_DIR, `${slug}.txt`), `${lines.map(stripAnsi).join("\n")}\n`, "utf8");
}

writeFileSync(resolve(OUTPUT_DIR, "00-overview.html"), overviewHtml(), "utf8");

writeFileSync(resolve(OUTPUT_DIR, "manifest.json"), `${JSON.stringify({
  generated_at: new Date().toISOString(),
  renderer: "frontend/terminal-ui/src/render.js",
  viewport: { columns: WIDTH, rows: HEIGHT },
  captures: captures.map(([slug, title]) => ({ slug, title })),
}, null, 2)}\n`, "utf8");

process.stdout.write(`${OUTPUT_DIR}\n`);
