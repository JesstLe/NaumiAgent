import { ANSI, color, compactText } from "../ansi.js";
import { MarkdownExcerpt } from "./markdown.js";
import { renderComponent } from "./core.js";
import { ActivityCard } from "./activity-card.js";
import { EventCard } from "./event-card.js";
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
    const content = compactText(message.content || "思考中...");
    const label = message.done ? "thinking" : "thinking...";
    return ["", color(ANSI.dim, `${label}: ${content}`)];
  }
  if (message.kind === "tool") {
    return renderComponent(ToolCard({ tool: message }), ctx);
  }
  if (message.kind === "activity") {
    return renderComponent(ActivityCard({ activity: message }), ctx);
  }
  if (message.kind === "permission") {
    return ["", color(ANSI.yellow, `permission: ${message.message.tool_name} · ${message.message.status}`)];
  }
  if (message.kind === "system") {
    const style = message.level === "error" ? ANSI.red : message.level === "warning" ? ANSI.yellow : ANSI.dim;
    return ["", color(style, `${message.title}: ${message.content}`)];
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
