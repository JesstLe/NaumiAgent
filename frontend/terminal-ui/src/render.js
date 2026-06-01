import process from "node:process";
import {
  ANSI,
  color,
  colorCodeLine,
  colorDiffLine,
  compactText,
  formatContext,
  formatMoney,
  looksLikeDiff,
  padRight,
  shortPath,
  truncateAnsi,
  visibleWidth,
  wrapAnsiLine,
} from "./ansi.js";

export function renderScreen(state, width, height, env = {}) {
  const footer = renderFooter(state, width, env);
  const footerHeight = footer.length;
  const bodyHeight = Math.max(1, height - footerHeight);
  const bodyLines = renderBody(state, width);
  const start = Math.max(0, bodyLines.length - bodyHeight - state.scrollOffset);
  const visible = bodyLines.slice(start, start + bodyHeight);
  while (visible.length < bodyHeight) visible.push("");
  return [
    ...visible.map((line) => padRight(line, width)),
    ...footer.map((line) => padRight(line, width)),
  ];
}

export function renderBody(state, width) {
  const lines = [];
  for (const message of state.messages) {
    lines.push(...renderMessage(message, width));
  }
  if (state.activeToolPrepare) {
    lines.push(color(ANSI.dim, `tool prepare: ${state.activeToolPrepare}`));
  }
  if (state.running) {
    lines.push(color(ANSI.dim, "运行中..."));
  }
  return lines.flatMap((line) => wrapAnsiLine(line, width));
}

export function renderMessage(message, width) {
  if (message.kind === "user") {
    return ["", `${color(ANSI.green, ">")} ${message.content}`];
  }
  if (message.kind === "assistant") {
    return ["", ...renderMarkdownExcerpt(message.content, width)];
  }
  if (message.kind === "thinking") {
    const content = compactText(message.content || "思考中...");
    const label = message.done ? "thinking" : "thinking...";
    return ["", color(ANSI.dim, `${label}: ${content}`)];
  }
  if (message.kind === "tool") {
    return renderToolCard(message, width);
  }
  if (message.kind === "permission") {
    return ["", color(ANSI.yellow, `permission: ${message.message.tool_name} · ${message.message.status}`)];
  }
  if (message.kind === "system") {
    const style = message.level === "error" ? ANSI.red : message.level === "warning" ? ANSI.yellow : ANSI.dim;
    return ["", color(style, `${message.title}: ${message.content}`)];
  }
  return ["", color(ANSI.dim, `${message.kind}: ${JSON.stringify(message.message ?? {})}`)];
}

export function renderToolCard(tool, width) {
  const title = `${tool.name}${tool.primary ? ` ${tool.primary}` : ""}`;
  const statusStyle = tool.status === "success" ? ANSI.green : tool.status === "running" ? ANSI.cyan : ANSI.red;
  const titleLine = `${color(statusStyle, tool.status === "running" ? "running" : tool.status)} ${title}`;
  const output = tool.output ? renderToolOutput(tool.output, width - 4) : [];
  const inner = [titleLine, ...output];
  if (tool.outputLength > (tool.output?.length ?? 0)) {
    inner.push(color(ANSI.dim, `... 已截断，完整输出 ${tool.outputLength} 字符`));
  }
  return boxLines("tool", inner, width);
}

export function renderToolOutput(text, width) {
  if (looksLikeDiff(text)) {
    return text.split("\n").slice(0, 60).map(colorDiffLine);
  }
  return renderMarkdownExcerpt(text, width).slice(0, 60);
}

export function renderMarkdownExcerpt(text, width) {
  const lines = [];
  const raw = String(text ?? "").split("\n");
  let inCode = false;
  let codeLineCount = 0;
  let omitted = 0;
  for (const line of raw) {
    if (line.startsWith("```")) {
      if (inCode && omitted) {
        lines.push(color(ANSI.dim, `... 已隐藏 ${omitted} 行代码`));
        omitted = 0;
      }
      inCode = !inCode;
      codeLineCount = 0;
      lines.push(color(ANSI.dim, line));
      continue;
    }
    if (inCode) {
      if (codeLineCount < 40) {
        lines.push(colorCodeLine(line));
      } else {
        omitted += 1;
      }
      codeLineCount += 1;
      continue;
    }
    lines.push(line);
  }
  if (omitted) {
    lines.push(color(ANSI.dim, `... 已隐藏 ${omitted} 行代码`));
  }
  return lines.flatMap((line) => wrapAnsiLine(line, width));
}

export function renderFooter(state, width, env = {}) {
  const lines = [];
  if (state.permission) {
    const tool = state.permission.payload.tool_name ?? "tool";
    const reason = compactText(state.permission.payload.reason ?? "");
    lines.push(color(ANSI.yellow, `permission: ${tool}  y=允许 n=拒绝 b=bypass  ${reason}`));
  }
  if (state.todo) {
    const current = state.todo.current;
    const currentText = current ? `#${current.id} ${current.subject}` : "有未完成任务";
    lines.push(color(ANSI.cyan, `todo: ${state.todo.completed}/${state.todo.total} 完成 | ${currentText}`));
  }
  const status = state.status ?? {};
  const context = status.context ?? {};
  const budget = status.budget ?? {};
  const git = status.git ?? {};
  const parts = [
    `mode: ${state.mode}`,
    status.model || "model: -",
    `工作区: ${shortPath(status.workspace_root || env.cwd || process.cwd(), env.home ?? process.env.HOME)}`,
    `Token: ${status.usage?.total_tokens ?? 0}`,
    `上下文: ${formatContext(context)}`,
    `预算: ${formatMoney(budget.used_usd)}/${formatMoney(budget.max_usd)}`,
  ];
  if (git.branch) parts.push(`${git.branch}${git.dirty ? "*" : ""}`);
  lines.push(color(ANSI.dim, truncateAnsi(parts.join(" | "), width)));
  lines.push(`${color(ANSI.green, state.mode)} ${state.running ? color(ANSI.dim, "running") : ">"} ${state.input}`);
  lines.push(color(ANSI.dim, "Shift+Tab 切换模式 · Enter 发送 · PageUp/PageDown 滚动 · Ctrl+C 退出"));
  return lines.flatMap((line) => wrapAnsiLine(line, width));
}

export function boxLines(title, inner, width) {
  const boxWidth = Math.max(30, width - 2);
  const top = `+ ${title} ${"-".repeat(Math.max(0, boxWidth - visibleWidth(title) - 4))}+`;
  const bottom = `+${"-".repeat(Math.max(0, boxWidth - 1))}+`;
  const body = inner.flatMap((line) => wrapAnsiLine(line, boxWidth - 4)).map((line) => {
    const rawPad = Math.max(0, boxWidth - 4 - visibleWidth(line));
    return `| ${line}${" ".repeat(rawPad)} |`;
  });
  return ["", color(ANSI.blue, top), ...body, color(ANSI.blue, bottom)];
}
