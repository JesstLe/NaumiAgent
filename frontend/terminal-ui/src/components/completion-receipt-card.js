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
  const taskChanges = changes.filter((item) => item.scope !== "background");
  const backgroundChanges = changes.filter((item) => item.scope === "background");
  const validations = array(view.validations);
  const approvals = array(view.approvals);
  const actionableApprovals = approvals.filter((item) => ["denied", "error"].includes(item.decision));
  const risks = array(view.risks);
  const unverified = array(view.unverified);
  const actions = array(view.next_actions);
  const outcome = outcomeView(view.outcome);
  const rows = [
    line(color(outcome.style, `${outcome.label} · ${formatDuration(view.duration_ms)}`)),
  ];
  if (view.summary) rows.push(line(compactText(view.summary, 500)));

  if (validations.length) {
    for (const validation of validations.slice(0, 2)) {
      const label = validation.status === "passed" ? "通过" : "失败";
      const style = validation.status === "passed" ? ANSI.green : ANSI.red;
      const counts = validationCounts(validation);
      rows.push(line(
        `${color(style, `验证 ${label}`)} · ${compactText(validation.command || "未知命令", 300)}`
        + `${counts ? ` · ${counts}` : ""}`,
      ));
    }
    if (validations.length > 2) rows.push(line(color(ANSI.dim, `另有 ${validations.length - 2} 项验证`)));
  } else if (taskChanges.length) {
    rows.push(line(color(ANSI.yellow, "未验证 · 本轮任务改动尚无验证证据")));
  }

  if (taskChanges.length) {
    rows.push(line(`影响 · ${changeSummary(taskChanges)}`));
  }
  if (backgroundChanges.length) {
    rows.push(line(color(ANSI.dim, `工作区另有 ${backgroundChanges.length} 项运行时变化`)));
  }

  const git = view.git_state && typeof view.git_state === "object" ? view.git_state : {};
  const hasReviewableChanges = taskChanges.some(
    (item) => !["removed_untracked", "restored"].includes(item.status),
  );
  if (!git.available && view.outcome !== "completed") {
    rows.push(line(gitSummary(git)));
  } else if (git.available && (hasReviewableChanges || Number(git.behind) > 0)) {
    rows.push(line(gitSummary(git)));
  }

  for (const approval of actionableApprovals.slice(0, 3)) {
    rows.push(line(color(
      ANSI.red,
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
  const parts = [
    color(ANSI.cyan, `Git ${git.branch || "detached"}`),
    color(git.dirty ? ANSI.yellow : ANSI.green, git.dirty ? "工作区有改动" : "工作区干净"),
  ];
  if (Number(git.ahead) > 0) parts.push(color(ANSI.green, `领先 ${Number(git.ahead)}`));
  if (Number(git.behind) > 0) parts.push(color(ANSI.red, `落后 ${Number(git.behind)}`));
  return parts.join(" · ");
}

function validationCounts(validation) {
  const counts = [];
  if (Number(validation.passed) > 0) counts.push(`通过 ${Number(validation.passed)}`);
  if (Number(validation.failed) > 0) counts.push(`失败 ${Number(validation.failed)}`);
  if (Number(validation.skipped) > 0) counts.push(`跳过 ${Number(validation.skipped)}`);
  if (!counts.length && validation.exit_code != null && validation.scope !== "文件系统") {
    counts.push(`退出码 ${validation.exit_code}`);
  }
  return counts.join(" · ");
}

function changeView(status) {
  return {
    modified: { label: "修改", style: ANSI.yellow },
    added: { label: "新增", style: ANSI.green },
    deleted: { label: "删除", style: ANSI.red },
    renamed: { label: "重命名", style: ANSI.cyan },
    untracked: { label: "新增", style: ANSI.green },
    copied: { label: "复制", style: ANSI.cyan },
    conflicted: { label: "冲突", style: `${ANSI.bold}${ANSI.red}` },
    restored: { label: "还原", style: ANSI.blue },
    removed_untracked: { label: "删除", style: ANSI.red },
  }[status] ?? { label: compactText(status || "变化", 40), style: ANSI.dim };
}

function changeSummary(changes) {
  const order = ["删除", "新增", "修改", "重命名", "复制", "还原", "冲突"];
  const counts = new Map();
  for (const change of changes) {
    const view = changeView(change.status);
    const current = counts.get(view.label) || { count: 0, style: view.style };
    current.count += 1;
    counts.set(view.label, current);
  }
  return [...counts.entries()]
    .sort(([left], [right]) => {
      const leftIndex = order.indexOf(left);
      const rightIndex = order.indexOf(right);
      return (leftIndex < 0 ? order.length : leftIndex) - (rightIndex < 0 ? order.length : rightIndex);
    })
    .map(([label, view]) => color(view.style, `${label} ${view.count} 个文件`))
    .join(" · ");
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
