import { ANSI, color, compactText } from "../ansi.js";
import { boxComponent, line, renderComponent } from "./core.js";

export function RunActivityCard({ activity }) {
  return {
    render(ctx) {
      return renderRunActivityCard(activity, ctx);
    },
  };
}

export function renderRunActivityCard(activity, ctx) {
  const status = runStatusLabel(activity);
  const statusColor = activity.status === "completed"
    ? ANSI.green
    : activity.status === "failed" || activity.status === "cancelled"
      ? ANSI.yellow
      : ANSI.cyan;
  const rows = [line(color(statusColor, `${status} · ${phaseLabel(activity)}`))];
  const context = [];
  if (Number(activity.turn) > 0) context.push(`回合 ${Number(activity.turn)}`);
  if (activity.model) context.push(`模型 ${compactText(activity.model, 60)}`);
  if (Number(activity.durationMs) >= 0 && activity.status !== "running") {
    context.push(`耗时 ${formatDuration(activity.durationMs)}`);
  }
  if (context.length) rows.push(line(color(ANSI.dim, context.join(" · "))));

  const tools = Object.values(activity.toolCalls ?? {});
  if (tools.length) {
    const completed = tools.filter((tool) => !["prepared", "running"].includes(tool.status)).length;
    const success = tools.filter((tool) => ["success", "succeeded"].includes(tool.status)).length;
    const failed = tools.filter((tool) => ["error", "failed", "cancelled"].includes(tool.status)).length;
    rows.push(line(`工具 ${completed}/${tools.length} · 成功 ${success} · 失败 ${failed}`));
  }
  if (Number(activity.permissionCount) > 0) {
    rows.push(line(`权限请求 ${Number(activity.permissionCount)}`));
  }
  for (const phase of (activity.perfPhases ?? []).slice(-3)) {
    rows.push(line(color(ANSI.dim, `${compactText(phase.label, 80)} · ${formatDuration(phase.durationMs)}`)));
  }
  return renderComponent(boxComponent("执行过程", rows), ctx);
}

function runStatusLabel(activity) {
  if (activity.status === "completed") return "已完成";
  if (activity.status === "failed") return "失败";
  if (activity.status === "cancelled") return "已取消";
  return "进行中";
}

function phaseLabel(activity) {
  if (activity.phaseLabel) return activity.phaseLabel;
  return {
    preparing: "准备运行",
    generating: "生成响应",
    executing: "执行工具",
    awaiting_permission: "等待权限",
    awaiting_input: "等待用户输入",
    summarizing: "整理结果",
    completed: "执行完成",
    failed: "执行失败",
    cancelled: "运行取消",
  }[activity.phase] ?? "处理中";
}

function formatDuration(value) {
  const ms = Math.max(0, Number(value) || 0);
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms)}ms`;
}
