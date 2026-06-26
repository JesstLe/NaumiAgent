import { ANSI, color, compactText, stripAnsi, visibleWidth } from "../ansi.js";
import { boxComponent, line, renderComponent } from "./core.js";

const SECTION_NAMES = new Set(["Timeline", "Detail", "Todo", "Subagent", "Background", "Browser Runs", "面板警告"]);
const DEFAULT_MAX_RENDER_LINES = 48;

export function TaskPanel({ content, taskPanel = null }) {
  return {
    render(ctx) {
      return renderTaskPanel(content, ctx.width, { ...ctx, taskPanel });
    },
  };
}

export function renderTaskPanel(content, width, ctx = { width }) {
  const model = parseTaskPanel(content);
  const taskPanel = ctx.taskPanel ?? ctx.state?.taskPanel ?? {};
  const workbenchIssues = ctx.state?.workbench?.issues ?? [];
  const issuesByTaskId = issueByTaskId(workbenchIssues);
  const children = [
    line(`${color(ANSI.cyan, "tasks")} ${model.summary}`),
    ...renderSection("Timeline", model.sections.Timeline, ANSI.green, taskPanel),
    ...renderSection("Detail", model.sections.Detail, ANSI.green, taskPanel),
    ...renderSection("Todo", model.sections.Todo, ANSI.cyan, taskPanel, issuesByTaskId),
    ...renderSection("Subagent", model.sections.Subagent, ANSI.magenta, taskPanel),
    ...renderSection("Background", model.sections.Background, ANSI.yellow, taskPanel),
    ...renderSection("Browser Runs", model.sections["Browser Runs"], ANSI.blue, taskPanel),
    ...renderSection("面板警告", model.sections["面板警告"], ANSI.red, taskPanel),
  ];
  const rendered = renderComponent(boxComponent("tasks", children), ctx);
  const maxRenderLines = taskPanel.maxRenderLines ?? ctx.bodyHeight ?? DEFAULT_MAX_RENDER_LINES;
  return clampTaskPanelLines(rendered, width, maxRenderLines);
}

export function parseTaskPanel(content) {
  const sections = {
    Timeline: [],
    Detail: [],
    Todo: [],
    Subagent: [],
    Background: [],
    "Browser Runs": [],
    "面板警告": [],
  };
  let current = "Todo";
  let filterLine = "";
  const lines = String(content ?? "")
    .split("\n")
    .map((item) => stripAnsi(item).trimEnd())
    .filter((item) => item.trim() && item.trim() !== "任务面板");

  for (const raw of lines) {
    const text = raw.trim();
    if (SECTION_NAMES.has(text)) {
      current = text;
      continue;
    }
    if (text.startsWith("filter:")) {
      filterLine = text;
      continue;
    }
    if (/^📋\s+\d+\/\d+/.test(text) && !sections.Todo.includes(text)) {
      sections.Todo.unshift(text);
      continue;
    }
    sections[current] ??= [];
    sections[current].push(text);
  }

  return {
    sections,
    filterLine,
    summary: summarizeSections(sections, filterLine),
  };
}

function issueByTaskId(issues = []) {
  return new Map(issues.map((issue) => [String(issue.task_id), issue]));
}

function renderSection(title, rows = [], style = ANSI.dim, taskPanel = {}, issuesByTaskId = new Map()) {
  if (!rows.length) return [];
  if (title === "Timeline") {
    return renderTimelineSection(rows, style, taskPanel);
  }
  const visible = rows.slice(0, 6);
  const hidden = rows.length - visible.length;
  return [
    line(color(style, title)),
    ...visible.flatMap((item) => renderTaskRow(title, item, taskPanel, issuesByTaskId)),
    hidden > 0 ? line(color(ANSI.dim, `  ... 还有 ${hidden} 项`)) : null,
  ].filter(Boolean);
}

function renderTimelineSection(rows = [], style = ANSI.dim, taskPanel = {}) {
  const collapsedSources = taskPanel.collapsedTimelineSources ?? {};
  const visibleRows = rows.filter((row) => !collapsedSources[timelineSourceForRow(row)]);
  const visible = visibleRows.slice(0, 6);
  const hidden = visibleRows.length - visible.length;
  const collapsed = rows.length - visibleRows.length;
  return [
    line(color(style, "Timeline")),
    renderTimelineSourceSummary(rows, collapsedSources),
    ...visible.flatMap((item) => renderTaskRow("Timeline", item, taskPanel)),
    collapsed > 0 ? line(color(ANSI.dim, `  ... 已折叠 ${collapsed} 项来源事件`)) : null,
    hidden > 0 ? line(color(ANSI.dim, `  ... 还有 ${hidden} 项`)) : null,
  ].filter(Boolean);
}

function renderTimelineSourceSummary(rows = [], collapsedSources = {}) {
  const counts = new Map();
  for (const row of rows) {
    const source = timelineSourceForRow(row);
    if (!source || source === "-") continue;
    counts.set(source, (counts.get(source) ?? 0) + 1);
  }
  if (!counts.size) return null;
  const summary = Array.from(counts.entries())
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([source, count]) => `${source} ${count}${collapsedSources[source] ? " folded" : ""}`)
    .join(" · ");
  return line(color(ANSI.dim, `  sources: ${summary}`));
}

function timelineSourceForRow(row) {
  const { detail } = parseTaskRow(row);
  return detailField(detail, "source");
}

function renderTaskRow(section, item, taskPanel = {}, issuesByTaskId = new Map()) {
  const parsed = parseTaskRow(item);
  const rowId = taskRowId(section, parsed.primary);
  const selected = rowId && rowId === taskPanel.selectedId;
  const expanded = rowId && taskPanel.expandedIds?.[rowId];
  const prefix = selected ? color(ANSI.green, "> ") : "  ";
  const primary = section === "Todo" ? enrichTodoPrimary(parsed.primary, rowId, issuesByTaskId) : parsed.primary;
  if (!parsed.detail) {
    return [line(`${prefix}${taskLineStyle(section, primary)}`)];
  }
  const rows = [
    line(`${prefix}${taskLineStyle(section, primary)}`),
    line(color(ANSI.dim, `    ${compactText(parsed.detail, 180)}`)),
  ];
  if (expanded) {
    rows.push(...renderExpandedTaskDetail(parsed.detail));
  }
  return rows;
}

function enrichTodoPrimary(primary, rowId, issuesByTaskId) {
  const issue = issuesByTaskId.get(String(rowId));
  if (!issue) return primary;
  const parts = [];
  if (issue.risk_level) parts.push(`risk:${issue.risk_level}`);
  if (issue.parallel_mode) parts.push(issue.parallel_mode);
  if (issue.related_worktree) parts.push(issue.related_worktree);
  return parts.length ? `${primary} [${parts.join(" · ")}]` : primary;
}

function parseTaskRow(item) {
  const [primary, detail = ""] = String(item ?? "").split(/\s+\|\s+/, 2);
  return { primary, detail };
}

function renderExpandedTaskDetail(detail) {
  const fields = String(detail ?? "")
    .split(/\s*;\s*/)
    .map((item) => item.trim())
    .filter(Boolean);
  if (!fields.length) return [];
  return [
    line(color(ANSI.dim, "    event flow")),
    ...fields.slice(0, 8).map((field) => line(color(ANSI.dim, `      - ${compactText(field, 160)}`))),
  ];
}

function detailField(detail, key) {
  const field = String(detail ?? "")
    .split(/\s*;\s*/)
    .find((item) => item.startsWith(`${key}=`));
  if (!field) return "";
  return field.slice(key.length + 1).trim();
}

function taskRowId(section, primary) {
  const text = String(primary ?? "");
  if (section === "Todo") {
    return text.match(/#([^\s]+)/)?.[1] ?? "";
  }
  if (section === "Subagent") {
    return text.match(/\s\/\s([^\s]+)/)?.[1] ?? "";
  }
  if (section === "Timeline" || section === "Background" || section === "Browser Runs") {
    return text.match(/^-\s+([^\s#]+)/)?.[1] ?? "";
  }
  return "";
}

function taskLineStyle(section, item) {
  const text = compactText(item, 180);
  if (section === "Todo") {
    if (/[✓✔]/.test(text) || text.includes("completed")) return color(ANSI.green, text);
    if (/[▶●]/.test(text) || text.includes("in_progress") || text.includes("running")) return color(ANSI.cyan, text);
    if (text.includes("blocked") || text.includes("⚑")) return color(ANSI.yellow, text);
  }
  if (section === "面板警告") return color(ANSI.yellow, text);
  if (text.includes("暂无") || text.includes("当前没有")) return color(ANSI.dim, text);
  return color(ANSI.dim, text);
}

function summarizeSections(sections, filterLine = "") {
  const parts = [];
  if (filterLine) {
    parts.push(filterLine.replace("filter:", "filter"));
  }
  const todoRows = sections.Todo ?? [];
  const todoProgress = todoRows.find((item) => /\d+\/\d+/.test(item));
  if (todoProgress) {
    const match = todoProgress.match(/(\d+)\/(\d+)/);
    if (match) parts.push(`todo ${match[1]}/${match[2]}`);
  } else if (todoRows.length) {
    parts.push(`todo ${todoRows.length}`);
  }
  const timelineActive = (sections.Timeline ?? []).filter(
    (item) => !item.includes("暂无") && !item.includes("当前没有"),
  );
  if (timelineActive.length) parts.push(`timeline ${timelineActive.length}`);
  for (const [name, rows] of [
    ["subagent", sections.Subagent],
  ]) {
    const active = (rows ?? []).filter((item) => !item.includes("暂无") && !item.includes("当前没有"));
    if (active.length) parts.push(`${name} ${active.length}`);
  }
  const backgroundActive = countBackgroundRows(sections.Background ?? []);
  if (backgroundActive) parts.push(`background ${backgroundActive}`);
  const browserActive = (sections["Browser Runs"] ?? []).filter(
    (item) => !item.includes("暂无") && !item.includes("当前没有"),
  );
  if (browserActive.length) parts.push(`browser ${browserActive.length}`);
  if ((sections["面板警告"] ?? []).length) parts.push(`warnings ${sections["面板警告"].length}`);
  return parts.length ? parts.join(" | ") : "暂无活动";
}

function countBackgroundRows(rows) {
  const active = (rows ?? []).filter((item) => !item.includes("暂无") && !item.includes("当前没有"));
  const statusRows = active.filter((item) => !item.includes(" | "));
  return statusRows.length || active.length;
}

function clampTaskPanelLines(lines, width, maxLines) {
  const limit = Math.max(8, Number(maxLines) || DEFAULT_MAX_RENDER_LINES);
  if (lines.length <= limit) return lines;
  const hidden = lines.length - limit + 1;
  const kept = lines.slice(0, Math.max(2, limit - 2));
  return [
    ...kept,
    taskPanelOmittedRow(hidden, width),
    lines.at(-1),
  ];
}

function taskPanelOmittedRow(hidden, width) {
  const boxWidth = Math.max(30, width - 2);
  const innerWidth = Math.max(1, boxWidth - 4);
  const text = color(
    ANSI.dim,
    compactText(`... 还有 ${hidden} 行，使用 /tasks detail <id> 查看详情，或 /tasks timeline 折叠高噪声来源`, innerWidth),
  );
  const pad = Math.max(0, innerWidth - visibleWidth(text));
  return `| ${text}${" ".repeat(pad)} |`;
}
