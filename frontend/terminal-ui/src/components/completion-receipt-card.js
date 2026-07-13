import { ANSI, color, compactText } from "../ansi.js";
import { boxComponent, line, renderComponent } from "./core.js";

export function CompletionReceiptCard({ receipt }) {
  return {
    render(ctx) {
      return renderCompletionReceiptCard(receipt, ctx);
    },
  };
}

export function renderCompletionReceiptCard(receipt, ctx) {
  const view = receipt && typeof receipt === "object" ? receipt : {};
  const changes = array(view.changes);
  const validations = array(view.validations);
  const approvals = array(view.approvals);
  const risks = array(view.risks);
  const unverified = array(view.unverified);
  const actions = array(view.next_actions);
  const outcome = outcomeView(view.outcome);
  const validationPassed = validations.filter((item) => item.status === "passed").length;
  const validationFailed = validations.filter((item) => item.status === "failed").length;
  const rows = [
    line(color(outcome.style, `${outcome.label} · ${formatDuration(view.duration_ms)}`)),
  ];
  if (view.summary) rows.push(line(compactText(view.summary, 500)));
  rows.push(line(
    `改动 ${changes.length} · 验证 ${validationPassed}/${validations.length}`
    + `（失败 ${validationFailed}） · 审批 ${approvals.length} · 风险 ${risks.length}`,
  ));
  rows.push(line(gitSummary(view.git_state)));

  if (!validations.length) {
    rows.push(line(color(ANSI.dim, "验证 · 未记录验证命令")));
  } else {
    for (const validation of validations.slice(0, 4)) {
      const label = validation.status === "passed" ? "通过" : "失败";
      const style = validation.status === "passed" ? ANSI.green : ANSI.red;
      const counts = validationCounts(validation);
      rows.push(line(
        `${color(style, `验证 ${label}`)} · ${compactText(validation.command || "未知命令", 300)}`
        + `${counts ? ` · ${counts}` : ""}`,
      ));
    }
    if (validations.length > 4) rows.push(line(color(ANSI.dim, `另有 ${validations.length - 4} 项验证`)));
  }

  for (const change of changes.slice(0, 5)) {
    const stats = changeStats(change);
    const source = change.source_tool ? ` · 来源 ${change.source_tool}` : "";
    rows.push(line(
      `改动 ${changeStatus(change.status)} · ${compactText(change.path || "未知路径", 300)}`
      + `${stats ? ` · ${stats}` : ""}${source}`,
    ));
  }
  if (changes.length > 5) rows.push(line(color(ANSI.dim, `另有 ${changes.length - 5} 个文件改动`)));

  for (const approval of approvals.slice(0, 3)) {
    const style = ["denied", "error"].includes(approval.decision) ? ANSI.red : ANSI.cyan;
    rows.push(line(color(
      style,
      `审批 · ${compactText(approval.tool_name || "未知工具", 120)} · ${approvalLabel(approval.decision)}`,
    )));
  }

  for (const item of unverified.slice(0, 3)) {
    rows.push(line(color(ANSI.yellow, `未验证 · ${compactText(item, 300)}`)));
  }
  for (const risk of risks.slice(0, 3)) {
    const style = ["high", "critical"].includes(risk.level) ? ANSI.red : ANSI.yellow;
    rows.push(line(color(style, `风险 · ${compactText(risk.message || risk.code, 300)}`)));
  }
  for (const action of actions.slice(0, 3)) {
    rows.push(line(color(ANSI.cyan, `下一步 · ${compactText(action.label || action.kind, 300)}`)));
  }
  return renderComponent(boxComponent("完成回执", rows), ctx);
}

function outcomeView(outcome) {
  return {
    completed: { label: "已完成", style: ANSI.green },
    partial: { label: "部分完成", style: ANSI.yellow },
    failed: { label: "失败", style: ANSI.red },
    cancelled: { label: "已取消", style: ANSI.yellow },
  }[outcome] ?? { label: "状态未知", style: ANSI.dim };
}

function gitSummary(value) {
  const git = value && typeof value === "object" ? value : {};
  if (!git.available) return color(ANSI.yellow, "Git 未核查");
  const parts = [`Git ${git.branch || "detached"}`, git.dirty ? "工作区有改动" : "工作区干净"];
  if (Number(git.ahead) > 0) parts.push(`领先 ${Number(git.ahead)}`);
  if (Number(git.behind) > 0) parts.push(`落后 ${Number(git.behind)}`);
  return parts.join(" · ");
}

function validationCounts(validation) {
  const counts = [];
  if (Number(validation.passed) > 0) counts.push(`通过 ${Number(validation.passed)}`);
  if (Number(validation.failed) > 0) counts.push(`失败 ${Number(validation.failed)}`);
  if (Number(validation.skipped) > 0) counts.push(`跳过 ${Number(validation.skipped)}`);
  if (!counts.length && validation.exit_code != null) counts.push(`退出码 ${validation.exit_code}`);
  return counts.join(" · ");
}

function changeStats(change) {
  const stats = [];
  if (Number(change.additions) > 0) stats.push(`+${Number(change.additions)}`);
  if (Number(change.deletions) > 0) stats.push(`-${Number(change.deletions)}`);
  return stats.join(" ");
}

function changeStatus(status) {
  return {
    modified: "修改",
    added: "新增",
    deleted: "删除",
    renamed: "重命名",
    untracked: "未跟踪",
    conflicted: "冲突",
    restored: "还原",
  }[status] ?? compactText(status || "变化", 40);
}

function approvalLabel(decision) {
  return {
    allowed_once: "仅本次允许",
    allowed_session: "本会话允许",
    bypass: "已绕过确认",
    denied: "已拒绝",
    error: "确认失败",
  }[decision] ?? compactText(decision || "已记录", 80);
}

function formatDuration(value) {
  const ms = Math.max(0, Number(value) || 0);
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

function array(value) {
  return Array.isArray(value) ? value : [];
}
