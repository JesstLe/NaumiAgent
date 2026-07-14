import { ANSI, color, compactText, formatMoney } from "../ansi.js";
import { isFoldExpanded } from "./folds.js";
import { boxComponent, line, renderComponent } from "./core.js";

export function SubagentCard({ activity }) {
  return {
    render(ctx) {
      return renderSubagentCard(activity, ctx);
    },
  };
}

export function renderSubagentCard(activity, ctx) {
  const foldKey = `subagent:${activity.id || activity.taskId || activity.agentName || "activity"}`;
  const expanded = isFoldExpanded(ctx.state?.folds, foldKey);
  const label = subagentStatusLabel(activity.status);
  const style = subagentStatusStyle(activity.status);
  const agent = compactText(activity.agentName || "未匹配", 120);
  const description = compactText(activity.description || activity.latestMessage || "未提供任务描述", 500);
  const marker = expanded ? "▾" : "▸";
  const rows = [line(color(style, `${marker} ${label} · ${agent} · ${description}`))];

  if (expanded) {
    if (activity.taskId) rows.push(line(color(ANSI.dim, `任务 ID · ${compactText(activity.taskId, 200)}`)));
    if (activity.description) rows.push(line(`任务 · ${compactText(activity.description, 4000)}`));
    if (activity.latestMessage) rows.push(line(`最新 · ${compactText(activity.latestMessage, 2000)}`));

    const resources = [];
    if (Number(activity.tokens) > 0) resources.push(`${Math.round(Number(activity.tokens)).toLocaleString("en-US")} tokens`);
    if (Number(activity.cost) > 0) resources.push(formatMoney(activity.cost));
    if (Number(activity.durationMs) > 0) resources.push(formatDuration(activity.durationMs));
    if (resources.length) rows.push(line(color(ANSI.dim, `资源 · ${resources.join(" · ")}`)));
    if (Number(activity.startedAtMs) > 0) {
      const end = Number(activity.updatedAtMs) > 0 ? formatClock(activity.updatedAtMs) : "进行中";
      rows.push(line(color(ANSI.dim, `时间 · ${formatClock(activity.startedAtMs)} → ${end}`)));
    }

    const events = Array.isArray(activity.events) ? activity.events.slice(-8) : [];
    if (events.length) {
      rows.push(line(color(ANSI.dim, "最近事件")));
      if ((activity.events || []).length > events.length) {
        rows.push(line(color(ANSI.dim, `  ... 省略 ${(activity.events || []).length - events.length} 条较早事件`)));
      }
      for (const event of events) {
        const eventStatus = compactText(event.status || "event", 80);
        const eventMessage = compactText(event.message || "状态更新", 1000);
        const clock = Number(event.timestampMs) > 0 ? `${formatClock(event.timestampMs)} · ` : "";
        rows.push(line(`  ${clock}${eventStatus} · ${eventMessage}`));
      }
    }
  }

  return renderComponent(boxComponent("子智能体", rows), ctx);
}

function subagentStatusLabel(status) {
  const normalized = String(status || "").toLowerCase();
  if (["started", "running"].includes(normalized)) return "进行中";
  if (normalized === "completed") return "已完成";
  if (["failed", "error"].includes(normalized)) return "失败";
  if (["cancelled", "canceled"].includes(normalized)) return "已取消";
  return normalized || "状态更新";
}

function subagentStatusStyle(status) {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "completed") return ANSI.green;
  if (["failed", "error"].includes(normalized)) return ANSI.red;
  if (["cancelled", "canceled"].includes(normalized)) return ANSI.yellow;
  return ANSI.cyan;
}

function formatDuration(value) {
  const ms = Math.max(0, Number(value) || 0);
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms)}ms`;
}

function formatClock(value) {
  const date = new Date(Number(value));
  if (Number.isNaN(date.getTime())) return "--:--:--";
  return date.toLocaleTimeString("zh-CN", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}
