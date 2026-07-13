import {
  ANSI,
  color,
  compactText,
  visibleWidth,
  wrapAnsiLine,
} from "../ansi.js";
import { formatBudgetStatus } from "./budget-status.js";

export const RUNTIME_INSPECTOR_TABS = Object.freeze([
  { id: "plan", label: "Plan" },
  { id: "tools", label: "Tools" },
  { id: "context", label: "Context" },
  { id: "changes", label: "Changes" },
  { id: "tests", label: "Tests" },
]);

export function renderRuntimeInspector(inspector, width, height = Number.POSITIVE_INFINITY) {
  const safeWidth = Math.max(1, Math.floor(Number(width) || 1));
  const safeHeight = Number.isFinite(Number(height))
    ? Math.max(1, Math.floor(Number(height) || 1))
    : Number.POSITIVE_INFINITY;
  const innerWidth = Math.max(1, safeWidth - 4);
  const view = inspector && typeof inspector === "object" ? inspector : {};
  const selectedTab = RUNTIME_INSPECTOR_TABS.some((tab) => tab.id === view.selectedTab)
    ? view.selectedTab
    : "plan";
  const snapshot = view.snapshot && typeof view.snapshot === "object" ? view.snapshot : null;
  const logical = [renderTabs(selectedTab)];
  if (view.focused) logical.push(color(ANSI.cyan, "Inspector 已聚焦 · Esc 返回 Composer"));

  if (view.error) logical.push(color(ANSI.yellow, `刷新警告 · ${compactText(view.error, 500)}`));
  if (view.loading && snapshot) logical.push(color(ANSI.dim, "正在刷新，当前展示上一次完整快照。"));
  if (view.stale) logical.push(color(ANSI.yellow, "快照已过期，等待后端确认最新状态。"));

  if (!snapshot) {
    logical.push("");
    logical.push(view.loading ? "正在加载运行快照…" : "尚未产生运行数据");
  } else {
    const run = snapshot.active_run_id ? ` · run ${compactText(snapshot.active_run_id, 24)}` : "";
    logical.push(color(ANSI.dim, `rev ${Number(snapshot.revision) || 0}${run}`));
    logical.push(...renderSelectedTab(view, selectedTab, snapshot[selectedTab]));
  }

  const wrapped = logical.flatMap((line) => wrapAnsiLine(line, innerWidth));
  if (safeWidth < 5) {
    const visible = wrapped.slice(0, safeHeight);
    while (visible.length < safeHeight && Number.isFinite(safeHeight)) visible.push("");
    return visible;
  }
  const top = inspectorBorder("Runtime Inspector", safeWidth, "top");
  const bottom = inspectorBorder("", safeWidth, "bottom");
  if (safeHeight === 1) return [top];
  if (safeHeight === 2) return [top, bottom];
  const availableBody = Math.max(1, safeHeight - 2);
  const visibleBody = wrapped.slice(0, availableBody);
  const framed = visibleBody.map((line) => {
    const padding = Math.max(0, innerWidth - visibleWidth(line));
    return `${color(ANSI.blue, "│")} ${line}${" ".repeat(padding)} ${color(ANSI.blue, "│")}`;
  });
  while (framed.length < availableBody && Number.isFinite(height)) {
    framed.push(`${color(ANSI.blue, "│")} ${" ".repeat(innerWidth)} ${color(ANSI.blue, "│")}`);
  }
  return [top, ...framed, bottom].slice(0, safeHeight);
}

function renderTabs(selectedTab) {
  return RUNTIME_INSPECTOR_TABS.map((tab) => (
    tab.id === selectedTab
      ? color(ANSI.cyan, `[${tab.label}]`)
      : color(ANSI.dim, tab.label)
  )).join(" ");
}

function renderSelectedTab(inspector, tabName, rawTab) {
  const tab = rawTab && typeof rawTab === "object"
    ? rawTab
    : { state: "empty", warnings: [] };
  const lines = [stateLine(tab.state)];
  if (tabName === "plan") lines.push(...renderPlan(inspector, tab));
  if (tabName === "tools") lines.push(...renderTools(inspector, tab));
  if (tabName === "context") lines.push(...renderContext(tab));
  if (tabName === "changes") lines.push(...renderChanges(inspector, tab));
  if (tabName === "tests") lines.push(...renderTests(inspector, tab));
  for (const warning of array(tab.warnings).slice(0, 5)) {
    lines.push(color(ANSI.yellow, `警告 · ${compactText(warning, 500)}`));
  }
  return lines;
}

function renderPlan(inspector, tab) {
  const items = array(tab.items);
  const actions = array(tab.next_actions);
  if (!items.length && !actions.length) return [emptyLine(tab.state, "尚未产生计划")];
  const lines = [];
  for (const [index, item] of items.slice(0, 20).entries()) {
    lines.push(`${selectionPrefix(inspector, "plan", index)}${todoStatus(item.status)} ${compactText(item.subject || item.id, 400)}`);
    if (isExpanded(inspector, "plan", index)) {
      const details = [
        item.active_form ? `进行态 ${compactText(item.active_form, 300)}` : "",
        item.owner ? `负责人 ${compactText(item.owner, 120)}` : "",
        array(item.blocked_by).length ? `阻塞于 ${array(item.blocked_by).join(", ")}` : "",
      ].filter(Boolean).join(" · ");
      lines.push(color(ANSI.dim, `    ${details || `ID ${compactText(item.id, 120)}`}`));
    }
  }
  for (const action of actions.slice(0, 5)) {
    lines.push(color(ANSI.cyan, `下一步 · ${compactText(action.label || action.kind, 400)}`));
  }
  return lines;
}

function renderTools(inspector, tab) {
  const items = array(tab.items);
  const approvals = array(tab.approvals);
  if (!items.length && !approvals.length) return [emptyLine(tab.state, "尚未调用工具")];
  const lines = [];
  for (const [index, item] of items.slice(0, 20).entries()) {
    const duration = Number(item.duration_ms) > 0 ? ` · ${Number(item.duration_ms)}ms` : "";
    lines.push(`${selectionPrefix(inspector, "tools", index)}${toolStatus(item.status)} ${compactText(item.name || "未知工具", 120)}${duration}`);
    if (isExpanded(inspector, "tools", index)) {
      const details = [
        item.summary ? compactText(item.summary, 400) : "",
        item.call_id ? `call ${compactText(item.call_id, 120)}` : "",
        item.run_id ? `run ${compactText(item.run_id, 120)}` : "",
      ].filter(Boolean).join(" · ");
      lines.push(color(ANSI.dim, `    ${details || "暂无更多工具证据"}`));
    }
  }
  for (const approval of approvals.slice(0, 8)) {
    lines.push(`审批 · ${compactText(approval.tool_name, 120)} · ${approvalLabel(approval.decision)}`);
  }
  return lines;
}

function renderContext(tab) {
  if (tab.state === "empty") return [emptyLine(tab.state, "尚未产生运行上下文")];
  const lines = [];
  if (tab.workspace_root) lines.push(`工作区 · ${compactText(tab.workspace_root, 500)}`);
  if (tab.branch || tab.commit) {
    lines.push(`Git · ${compactText(tab.branch || "detached", 120)}${tab.git_dirty ? " · 有改动" : " · 干净"}${tab.commit ? ` · ${compactText(tab.commit, 16)}` : ""}`);
  } else if (tab.git_available === false) {
    lines.push(color(ANSI.yellow, "Git · 不可用"));
  }
  if (tab.model) lines.push(`模型 · ${compactText(tab.model, 200)}`);
  if (tab.runtime_mode || tab.permission_mode) {
    lines.push(`模式 · ${compactText(tab.runtime_mode || "default", 80)} · 权限 ${compactText(tab.permission_mode || "-", 80)}`);
  }
  lines.push(`上下文 · ${formatCount(tab.context_used)}/${formatCount(tab.context_window)} · ${formatPercent(tab.context_percentage)}`);
  lines.push(formatBudgetStatus({
    enabled: tab.budget_enabled,
    used_usd: tab.budget_used_usd,
    max_usd: tab.budget_max_usd,
    cost_percentage: tab.budget_percentage,
    input_tokens: tab.input_tokens,
    max_input_tokens: tab.budget_max_input_tokens,
    output_tokens: tab.output_tokens,
    max_output_tokens: tab.budget_max_output_tokens,
  }));
  lines.push(`Token · 输入 ${formatCount(tab.input_tokens)} · 输出 ${formatCount(tab.output_tokens)} · 轮次 ${number(tab.turns)}`);
  return lines;
}

function renderChanges(inspector, tab) {
  const items = array(tab.items);
  if (!items.length) {
    const git = tab.git_state && typeof tab.git_state === "object" ? tab.git_state : {};
    const suffix = git.available ? ` · Git ${git.dirty ? "有改动" : "干净"}` : "";
    return [emptyLine(tab.state, `尚未记录文件改动${suffix}`)];
  }
  const lines = [];
  if (tab.summary) lines.push(compactText(tab.summary, 500));
  for (const [index, item] of items.slice(0, 30).entries()) {
    const stats = [
      Number(item.additions) > 0 ? `+${Number(item.additions)}` : "",
      Number(item.deletions) > 0 ? `-${Number(item.deletions)}` : "",
    ].filter(Boolean).join(" ");
    lines.push(`${selectionPrefix(inspector, "changes", index)}${changeStatus(item.status)} ${compactText(item.path, 400)}${stats ? ` · ${stats}` : ""}`);
    if (isExpanded(inspector, "changes", index)) {
      lines.push(color(
        ANSI.dim,
        `    ${item.source_tool ? `来源 ${compactText(item.source_tool, 120)}` : "暂无更多变更证据"}`,
      ));
    }
  }
  return lines;
}

function renderTests(inspector, tab) {
  const validations = array(tab.validations);
  const unverified = array(tab.unverified);
  const actions = array(tab.next_actions);
  if (!validations.length && !unverified.length && !actions.length) {
    return [emptyLine(tab.state, "尚未记录验证")];
  }
  const lines = [];
  for (const [index, item] of validations.slice(0, 20).entries()) {
    const counts = validationCounts(item);
    lines.push(`${selectionPrefix(inspector, "tests", index)}${validationStatus(item.status)} ${compactText(item.command || item.scope, 400)}${counts ? ` · ${counts}` : ""}`);
    if (isExpanded(inspector, "tests", index)) {
      const details = [
        item.scope ? `范围 ${compactText(item.scope, 160)}` : "",
        item.log_ref ? `日志 ${compactText(item.log_ref, 300)}` : "",
      ].filter(Boolean).join(" · ");
      lines.push(color(ANSI.dim, `    ${details || "暂无更多验证证据"}`));
    }
  }
  for (const item of unverified.slice(0, 8)) {
    lines.push(color(ANSI.yellow, `未验证 · ${compactText(item, 400)}`));
  }
  for (const action of actions.slice(0, 5)) {
    lines.push(color(ANSI.cyan, `下一步 · ${compactText(action.label || action.kind, 400)}`));
  }
  return lines;
}

function stateLine(state) {
  const labels = {
    ready: [ANSI.green, "状态 · 已就绪"],
    empty: [ANSI.dim, "状态 · 暂无数据"],
    loading: [ANSI.cyan, "状态 · 加载中"],
    stale: [ANSI.yellow, "状态 · 已过期"],
    error: [ANSI.red, "状态 · 错误"],
  };
  const [style, label] = labels[state] ?? labels.empty;
  return color(style, label);
}

function emptyLine(state, message) {
  if (state === "loading") return color(ANSI.cyan, "正在等待后端数据…");
  if (state === "error") return color(ANSI.red, message);
  return color(ANSI.dim, message);
}

function selectionPrefix(inspector, tabName, index) {
  const selected = Number(inspector?.selectionByTab?.[tabName] ?? 0);
  return selected === index ? color(ANSI.cyan, "› ") : "  ";
}

function isExpanded(inspector, tabName, index) {
  return inspector?.expandedByTab?.[tabName]?.[String(index)] === true;
}

function todoStatus(status) {
  const view = {
    completed: [ANSI.green, "✓"],
    in_progress: [ANSI.cyan, "●"],
    blocked: [ANSI.red, "!"],
    pending: [ANSI.dim, "○"],
  }[status] ?? [ANSI.dim, "·"];
  return color(view[0], view[1]);
}

function toolStatus(status) {
  const view = {
    success: [ANSI.green, "✓"],
    running: [ANSI.cyan, "●"],
    prepared: [ANSI.dim, "○"],
    error: [ANSI.red, "!"],
  }[status] ?? [ANSI.dim, "·"];
  return color(view[0], view[1]);
}

function validationStatus(status) {
  return status === "passed"
    ? color(ANSI.green, "通过")
    : status === "failed"
      ? color(ANSI.red, "失败")
      : color(ANSI.yellow, compactText(status || "未知", 40));
}

function validationCounts(item) {
  const parts = [];
  if (number(item.passed)) parts.push(`通过 ${number(item.passed)}`);
  if (number(item.failed)) parts.push(`失败 ${number(item.failed)}`);
  if (number(item.skipped)) parts.push(`跳过 ${number(item.skipped)}`);
  if (!parts.length && item.exit_code != null) parts.push(`退出码 ${item.exit_code}`);
  return parts.join(" · ");
}

function approvalLabel(decision) {
  return {
    pending: "等待确认",
    allowed_once: "仅本次允许",
    allowed_session: "本会话允许",
    bypass: "已绕过确认",
    denied: "已拒绝",
    error: "确认失败",
  }[decision] ?? compactText(decision || "已记录", 80);
}

function changeStatus(status) {
  return {
    modified: "修改",
    added: "新增",
    deleted: "删除",
    renamed: "重命名",
    untracked: "未跟踪",
    conflicted: "冲突",
  }[status] ?? compactText(status || "变化", 40);
}

function formatCount(value) {
  const count = number(value);
  return count >= 1000 ? `${(count / 1000).toFixed(count >= 10000 ? 0 : 1)}K` : String(count);
}

function formatPercent(value) {
  return `${number(value).toFixed(1)}%`;
}

function number(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

function array(value) {
  return Array.isArray(value) ? value : [];
}

function inspectorBorder(title, width, position) {
  if (width === 1) return color(ANSI.blue, "─");
  if (position === "bottom") return color(ANSI.blue, `└${"─".repeat(width - 2)}┘`);
  const available = Math.max(0, width - 2);
  const fullLabel = ` ${title} `;
  const label = fullLabel.slice(0, available);
  return color(ANSI.blue, `┌${label}${"─".repeat(Math.max(0, available - label.length))}┐`);
}
