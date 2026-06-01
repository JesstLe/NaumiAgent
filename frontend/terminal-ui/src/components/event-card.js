import { ANSI, color, compactText } from "../ansi.js";
import { boxComponent, line, renderComponent } from "./core.js";

export function EventCard({ message }) {
  return {
    render(ctx) {
      return renderEventCard(message, ctx.width, ctx);
    },
  };
}

export function renderEventCard(message, width, ctx = { width }) {
  const view = eventView(message);
  const children = [line(`${color(view.style, view.label)} ${view.title}`)];
  for (const detail of view.details) {
    if (detail) children.push(line(color(ANSI.dim, compactText(detail, 180))));
  }
  return renderComponent(boxComponent(view.boxTitle, children), ctx);
}

function eventView(message) {
  const payload = message.message ?? message;
  if (message.kind === "runtime_notification") {
    return {
      boxTitle: payload.source || "runtime",
      label: "通知",
      title: compactText(payload.title || "运行时通知", 120),
      style: ANSI.cyan,
      details: [
        payload.count ? `数量: ${payload.count}` : "",
        payload.preview ? `预览: ${payload.preview}` : "",
      ],
    };
  }
  if (message.kind === "subagent_event") {
    return {
      boxTitle: "subagent",
      label: payload.status || "event",
      title: compactText(payload.agent_name || "subagent", 120),
      style: payload.status === "completed" ? ANSI.green : payload.status === "error" || payload.status === "failed" ? ANSI.red : ANSI.cyan,
      details: [
        payload.task_id ? `任务: ${payload.task_id}` : "",
        payload.message ? `消息: ${payload.message}` : "",
      ],
    };
  }
  if (message.kind === "team_event") {
    return {
      boxTitle: "team",
      label: payload.priority || "normal",
      title: compactText(`${payload.sender || "unknown"} -> ${payload.recipient || "广播"}`, 120),
      style: payload.priority === "critical" ? ANSI.red : payload.priority === "high" ? ANSI.yellow : ANSI.cyan,
      details: [
        payload.event_type ? `类型: ${payload.event_type}` : "",
        payload.message ? `消息: ${payload.message}` : "",
      ],
    };
  }
  if (message.kind === "hook_trace") {
    const failed = payload.aborted || payload.error;
    return {
      boxTitle: "hook",
      label: payload.aborted ? "aborted" : payload.error ? "error" : "ok",
      title: compactText(`${payload.point || "hook"} -> ${payload.callback || "callback"}`, 120),
      style: failed ? ANSI.red : ANSI.green,
      details: [
        payload.duration_ms ? `耗时: ${payload.duration_ms}ms` : "",
        payload.error ? `错误: ${payload.error}` : "",
      ],
    };
  }
  if (message.kind === "context_compact") {
    const preserved = listText(payload.preserved_sections);
    const warnings = listText(payload.warnings, "; ");
    return {
      boxTitle: "context",
      label: "compact",
      title: `${payload.before ?? 0} -> ${payload.after ?? 0}`,
      style: ANSI.yellow,
      details: [
        payload.archived_tool_results ? `归档工具结果: ${payload.archived_tool_results}` : "",
        preserved ? `保留: ${preserved}` : "",
        warnings ? `警告: ${warnings}` : "",
      ],
    };
  }
  if (message.kind === "recovery") {
    return {
      boxTitle: "recovery",
      label: payload.phase || "recovery",
      title: compactText(payload.action || payload.reason || "恢复流程", 120),
      style: payload.phase === "failed" ? ANSI.red : ANSI.yellow,
      details: [
        payload.reason ? `原因: ${payload.reason}` : "",
        payload.unit ? `变化: ${payload.before ?? "?"} -> ${payload.after ?? "?"} ${payload.unit}` : "",
      ],
    };
  }
  if (message.kind === "error") {
    return {
      boxTitle: "error",
      label: "error",
      title: compactText(payload.message || "未知错误", 140),
      style: ANSI.red,
      details: [],
    };
  }
  return {
    boxTitle: message.kind || "event",
    label: "event",
    title: compactText(JSON.stringify(payload), 160),
    style: ANSI.dim,
    details: [],
  };
}

function listText(value, separator = ", ") {
  if (Array.isArray(value)) return value.map(String).filter(Boolean).join(separator);
  if (value == null) return "";
  return String(value);
}
