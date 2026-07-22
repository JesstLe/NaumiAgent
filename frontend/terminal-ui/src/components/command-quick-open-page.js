import { ANSI, color, compactText } from "../ansi.js";
import {
  commandCategoryLabel,
  commandRiskColor,
  commandRiskLabel,
  commandTemplate,
} from "../command-metadata.js";
import { getCommandQuickOpenItems, taskTemplate } from "../command-quick-open.js";
import { boxLines } from "./core.js";

export function renderCommandQuickOpenPage(state, width, height) {
  const items = getCommandQuickOpenItems(state);
  const selectedIndex = Math.max(0, items.findIndex((item) => item.selected));
  const visibleCount = Math.max(1, Math.min(items.length || 1, height - 9));
  const start = Math.max(0, Math.min(
    Math.max(0, items.length - visibleCount),
    selectedIndex - Math.floor(visibleCount / 2),
  ));
  const visible = items.slice(start, start + visibleCount);
  const query = String(state.commandQuickOpen?.query || "");
  const provider = state.commandQuickOpen?.provider === "tasks" ? "tasks" : "commands";
  const taskLoading = Boolean(state.commandQuickOpen?.taskLoading);
  const taskError = String(state.commandQuickOpen?.taskError || "");
  const rows = [
    `${color(ANSI.cyan, "搜索:")} ${query || color(ANSI.dim, provider === "tasks" ? "输入任务 ID、标题、Owner、来源或状态" : "输入命令、别名、说明、类别或风险")}${color(ANSI.yellow, "█")}`,
    color(ANSI.dim, `Provider ${provider === "tasks" ? "任务" : "命令"} · 结果 ${items.length} · 选择只填入输入框`),
    "",
  ];
  if (!visible.length) {
    if (provider === "tasks" && taskLoading) {
      rows.push(color(ANSI.cyan, "正在读取权威任务快照…"));
    } else if (provider === "tasks" && taskError) {
      rows.push(color(ANSI.yellow, compactText(taskError, 160)));
    } else if (provider === "tasks" && state.commandQuickOpen?.taskWarnings?.length) {
      rows.push(color(ANSI.yellow, "没有匹配任务。部分来源不可用，可运行 /doctor 检查。"));
    } else {
      rows.push(color(ANSI.yellow, `没有匹配${provider === "tasks" ? "任务" : "命令"}。`));
    }
  } else {
    for (const item of visible) {
      const marker = item.selected ? "›" : " ";
      if (provider === "tasks") {
        const taskText = `${marker} ${compactText(item.title || item.task_id, 100)} · ${taskSourceLabel(item.source)} · ${taskStatusLabel(item.status)} · ${compactText(item.task_id, 80)}${item.owner ? ` · ${compactText(item.owner, 60)}` : ""}`;
        rows.push(color(taskStatusColor(item.status, item.selected), taskText));
      } else {
        const category = commandCategoryLabel(item.category);
        const risk = commandRiskLabel(item.permission_risk);
        const recent = item.recent ? " · 最近" : "";
        const text = `${marker} ${commandTemplate(item)} · ${category} · ${risk}${recent} · ${compactText(item.description, 120)}`;
        rows.push(color(commandRiskColor(item.permission_risk, item.selected), text));
      }
    }
    const selected = items[selectedIndex];
    rows.push("");
    rows.push(color(
      ANSI.dim,
      `将填入: ${provider === "tasks" ? taskTemplate(selected) : commandTemplate(selected)} · 不会自动发送或执行`,
    ));
  }
  rows.push(color(ANSI.dim, "Tab 切换命令/任务 · ↑/↓ 选择 · Enter 填入 · Esc/Ctrl+P 取消"));
  const boundedHeight = Math.max(1, height);
  const output = boxLines(provider === "tasks" ? "任务 QuickOpen" : "命令 QuickOpen", rows, width).slice(0, boundedHeight);
  while (output.length < boundedHeight) output.push("");
  return output;
}

function taskSourceLabel(source) {
  return ({ todo: "待办", subagent: "子智能体", background: "后台任务", browser: "浏览器" })[source] || source;
}

function taskStatusLabel(status) {
  return ({ pending: "等待", running: "运行中", blocked: "阻塞", completed: "已完成", failed: "失败", cancelled: "已取消" })[status] || status;
}

function taskStatusColor(status, selected) {
  if (selected) return ANSI.cyan;
  if (status === "completed") return ANSI.green;
  if (["failed", "cancelled"].includes(status)) return ANSI.red;
  if (status === "blocked") return ANSI.yellow;
  if (status === "running") return ANSI.cyan;
  return ANSI.dim;
}
