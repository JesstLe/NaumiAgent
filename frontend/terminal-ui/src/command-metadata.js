import { ANSI } from "./ansi.js";

const CATEGORY_LABELS = Object.freeze({
  basic: "基础",
  session: "会话",
  analysis: "分析",
  orchestration: "编排",
  navigation: "导航",
  control: "控制",
});

const RISK_LABELS = Object.freeze({
  read_only: "只读",
  session_state: "会话状态",
  permission_change: "权限变更",
  workspace_write: "工作区写入",
  tool_execution: "工具执行",
  destructive: "破坏性",
});

export function commandCategoryLabel(category) {
  return CATEGORY_LABELS[String(category || "")] || "未分类";
}

export function commandRiskLabel(risk) {
  return RISK_LABELS[String(risk || "")] || "风险待确认";
}

export function commandRiskColor(risk, selected = false) {
  if (selected) return ANSI.yellow;
  if (risk === "read_only") return ANSI.green;
  if (risk === "session_state") return ANSI.cyan;
  if (risk === "permission_change" || risk === "workspace_write") return ANSI.yellow;
  if (risk === "tool_execution" || risk === "destructive") return ANSI.red;
  return ANSI.dim;
}

export function commandTemplate(entry) {
  const command = String(entry?.command || "").trim();
  const syntax = String(entry?.arguments?.syntax || "").trim();
  return syntax ? `${command} ${syntax}` : command;
}
