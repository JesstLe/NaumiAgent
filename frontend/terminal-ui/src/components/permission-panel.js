import { ANSI, color, compactText, stripAnsi } from "../ansi.js";
import { boxComponent, line, renderComponent } from "./core.js";

const SECTION_NAMES = new Set(["Pending", "History", "面板警告"]);

export function PermissionPanel({ content }) {
  return {
    render(ctx) {
      return renderPermissionPanel(content, ctx.width, ctx);
    },
  };
}

export function renderPermissionPanel(content, width, ctx = { width }) {
  const model = parsePermissionPanel(content);
  const children = [
    line(`${color(ANSI.yellow, "permissions")} ${model.summary}`),
    ...renderSection("Pending", model.sections.Pending, ANSI.yellow),
    ...renderSection("History", model.sections.History, ANSI.dim),
    ...renderSection("面板警告", model.sections["面板警告"], ANSI.red),
  ];
  return renderComponent(boxComponent("permissions", children), ctx);
}

export function parsePermissionPanel(content) {
  const sections = {
    Pending: [],
    History: [],
    "面板警告": [],
  };
  let current = "Pending";
  let modeLine = "";
  const lines = String(content ?? "")
    .split("\n")
    .map((item) => stripAnsi(item).trimEnd())
    .filter((item) => item.trim() && item.trim() !== "权限面板");

  for (const raw of lines) {
    const text = raw.trim();
    if (text.startsWith("mode:")) {
      modeLine = text;
      continue;
    }
    if (SECTION_NAMES.has(text)) {
      current = text;
      continue;
    }
    sections[current] ??= [];
    sections[current].push(text);
  }

  return {
    modeLine,
    sections,
    summary: summarizePermissionSections(sections, modeLine),
  };
}

function renderSection(title, rows = [], style = ANSI.dim) {
  if (!rows.length) return [];
  const visible = rows.slice(0, 6);
  const hidden = rows.length - visible.length;
  return [
    line(color(style, title)),
    ...visible.flatMap((item) => renderPermissionRow(title, item)),
    hidden > 0 ? line(color(ANSI.dim, `  ... 还有 ${hidden} 项`)) : null,
  ].filter(Boolean);
}

function renderPermissionRow(section, item) {
  const parsed = parsePermissionRow(item);
  if (!parsed.policy && !parsed.reason) {
    return [line(`  ${permissionLineStyle(section, item)}`)];
  }
  return [
    line(`  ${permissionLineStyle(section, parsed.primary)}`),
    parsed.policy ? line(color(ANSI.dim, `    ${compactText(parsed.policy, 180)}`)) : null,
    parsed.reason ? line(color(ANSI.dim, `    ${compactText(parsed.reason, 180)}`)) : null,
  ].filter(Boolean);
}

function parsePermissionRow(item) {
  const [left, reason = ""] = String(item ?? "").split(/\s+\|\s+/, 2);
  const policyIndex = left.indexOf(" 风险:");
  if (policyIndex < 0) {
    return { primary: left, policy: "", reason };
  }
  return {
    primary: left.slice(0, policyIndex),
    policy: left.slice(policyIndex + 1),
    reason,
  };
}

function permissionLineStyle(section, item) {
  const text = compactText(item, 180);
  if (section === "Pending" && !text.includes("暂无")) return color(ANSI.yellow, text);
  if (text.includes("[confirmed]") || text.includes("[allowed]")) return color(ANSI.green, text);
  if (text.includes("[denied]") || text.includes("[blocked]")) return color(ANSI.red, text);
  if (section === "面板警告") return color(ANSI.yellow, text);
  return color(ANSI.dim, text);
}

function summarizePermissionSections(sections, modeLine) {
  const pending = (sections.Pending ?? []).filter((item) => !item.includes("暂无"));
  const history = (sections.History ?? []).filter((item) => !item.includes("暂无"));
  const parts = [];
  if (pending.length) parts.push(`pending ${pending.length}`);
  if (history.length) parts.push(`history ${history.length}`);
  if (modeLine) parts.push(modeLine.replace("mode:", "mode"));
  return parts.length ? parts.join(" | ") : "暂无权限活动";
}
