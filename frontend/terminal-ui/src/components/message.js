import { ANSI, color, compactText } from "../ansi.js";
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
    return ["", `${color(ANSI.green, ">")} ${message.content}`];
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
