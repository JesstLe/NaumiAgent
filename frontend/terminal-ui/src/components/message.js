import { ANSI, color, compactText, visibleWidth, wrapAnsiLine } from "../ansi.js";
import { MarkdownExcerpt } from "./markdown.js";
import { renderComponent } from "./core.js";
import { ActivityCard } from "./activity-card.js";
import { EventCard } from "./event-card.js";
import { PermissionCard } from "./permission-card.js";
import { PermissionPanel } from "./permission-panel.js";
import { TaskPanel } from "./task-panel.js";
import { ToolCard } from "./tool-card.js";

export function Message({ message }) {
  return {
    render(ctx) {
      return renderMessage(message, ctx.width, ctx);
    },
  };
}

export function renderMessage(message, width, ctx = { width }) {
  if (message.kind === "user") {
    return renderUserMessage(message, width);
  }
  if (message.kind === "assistant") {
    return ["", ...renderComponent(MarkdownExcerpt({ text: message.content, foldKey: `message:${message.id ?? ""}` }), ctx)];
  }
  if (message.kind === "thinking") {
    const content = compactText(message.content || "");
    const suffix = content ? `: ${content}` : "";
    return ["", color(ANSI.dim, `thinking${message.done ? "" : "..."}${suffix}`)];
  }
  if (message.kind === "tool") {
    return renderComponent(ToolCard({ tool: message }), ctx);
  }
  if (message.kind === "activity") {
    return renderComponent(ActivityCard({ activity: message }), ctx);
  }
  if (message.kind === "permission") {
    return renderComponent(PermissionCard({ permission: message }), ctx);
  }
  if (message.kind === "system") {
    if (message.title === "tasks") {
      return renderComponent(TaskPanel({ content: message.content, taskPanel: ctx.state?.taskPanel }), ctx);
    }
    if (message.title === "permissions") {
      return renderComponent(PermissionPanel({ content: message.content }), ctx);
    }
    const style = message.level === "error" ? ANSI.red : message.level === "warning" ? ANSI.yellow : ANSI.dim;
    const contentLines = String(message.content ?? "").split("\n");
    const first = `${message.title}: ${contentLines.shift() ?? ""}`;
    return ["", color(style, first), ...contentLines.map((item) => color(style, item))];
  }
  if ([
    "runtime_notification",
    "subagent_event",
    "team_event",
    "hook_trace",
    "context_compact",
    "recovery",
    "error",
  ].includes(message.kind)) {
    return renderComponent(EventCard({ message }), ctx);
  }
  return ["", color(ANSI.dim, `${message.kind}: ${JSON.stringify(message.message ?? {})}`)];
}

function renderUserMessage(message, width) {
  const safeWidth = Math.max(1, Number(width) || 1);
  const blockWidth = Math.min(safeWidth, Math.max(12, Math.floor(safeWidth * 0.72)));
  const textWidth = Math.max(1, blockWidth - 4);
  const contentLines = String(message.content ?? "")
    .split("\n")
    .flatMap((line) => wrapAnsiLine(line, textWidth));
  const rendered = contentLines.map((line, index) => {
    const role = index === 0 ? `${color(ANSI.green, "你")}  ` : "    ";
    return rightAlign(`${role}${line}`, width);
  });
  const status = userDeliveryStatus(message);
  if (status) {
    rendered.push(...wrapAnsiLine(status.text, blockWidth).map(
      (line) => rightAlign(color(status.style, line), width),
    ));
  }
  return ["", ...rendered];
}

function userDeliveryStatus(message) {
  if (message.deliveryStatus === "queued") {
    return { text: "发送中...", style: ANSI.dim };
  }
  if (message.deliveryStatus === "failed") {
    const reason = compactText(message.errorMessage || "发送未完成");
    return { text: `发送失败: ${reason} · /retry 重试`, style: ANSI.red };
  }
  if (message.deliveryStatus === "uncertain") {
    return { text: "发送状态待确认 · /retry 可能重复发送", style: ANSI.yellow };
  }
  return null;
}

function rightAlign(line, width) {
  return `${" ".repeat(Math.max(0, width - visibleWidth(line)))}${line}`;
}
