import { ANSI, color, compactText } from "../ansi.js";
import { boxComponent, line, renderComponent } from "./core.js";

export function ActivityCard({ activity }) {
  return {
    render(ctx) {
      return renderActivityCard(activity, ctx.width, ctx);
    },
  };
}

export function renderActivityCard(activity, width, ctx = { width }) {
  const status = activity.status === "done" ? color(ANSI.green, "done") : color(ANSI.cyan, "running");
  const title = compactText(activity.title || "正在处理", 120);
  const children = [line(`${status} ${title}`)];
  const progress = renderActivityProgress(activity);
  if (progress) children.push(line(progress));

  for (const detail of activity.details ?? []) {
    if (detail) children.push(line(color(ANSI.dim, compactText(detail, 160))));
  }

  return renderComponent(boxComponent("activity", children), ctx);
}

export function renderActivityProgress(activity) {
  const metrics = activity.metrics ?? {};
  const argumentChars = Number(metrics.argumentChars ?? 0) || 0;
  const contentChars = Number(metrics.contentChars ?? 0) || 0;
  const contentLines = Number(metrics.contentLines ?? 0) || 0;
  const elapsedMs = Number(metrics.elapsedMs ?? 0) || 0;
  const activityUnits = Math.max(argumentChars, contentChars, contentLines * 80, elapsedMs);
  if (!activity.phase && activityUnits <= 0) return "";

  const label = phaseLabel(activity.phase, activity.status);
  const meter = activityMeter(activityUnits, activity.status);
  const parts = [];
  if (argumentChars) parts.push(`参数 ${formatCount(argumentChars)} chars`);
  if (contentLines) parts.push(`${contentLines} lines`);
  if (contentChars) parts.push(`内容 ${formatCount(contentChars)} chars`);
  if (elapsedMs) parts.push(formatDuration(elapsedMs));
  const suffix = parts.length ? ` · ${parts.join(" · ")}` : "";
  return color(ANSI.dim, `${label} ${meter}${suffix}`);
}

function phaseLabel(phase, status) {
  if (status === "done") return "完成";
  if (phase === "snapshot") return "生成中";
  if (phase === "end") return "收尾";
  return "启动";
}

function activityMeter(units, status) {
  if (status === "done") return "[##########]";
  const normalized = Math.max(1, Math.min(10, Math.ceil(Math.log10(Math.max(1, units)) * 2)));
  return `[${"#".repeat(normalized)}${"-".repeat(10 - normalized)}]`;
}

function formatCount(value) {
  const num = Number(value || 0);
  if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
  if (num >= 1000) return `${(num / 1000).toFixed(1)}K`;
  return String(num);
}

function formatDuration(ms) {
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${ms}ms`;
}
