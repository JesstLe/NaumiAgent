import { ANSI, color, compactText } from "../ansi.js";
import {
  commandCategoryLabel,
  commandRiskColor,
  commandRiskLabel,
  commandTemplate,
} from "../command-metadata.js";
import {
  fileTemplate,
  getCommandQuickOpenItems,
  sessionTemplate,
  taskTemplate,
} from "../command-quick-open.js";
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
  const provider = ["tasks", "sessions", "files"].includes(state.commandQuickOpen?.provider)
    ? state.commandQuickOpen.provider : "commands";
  const taskLoading = Boolean(state.commandQuickOpen?.taskLoading);
  const taskError = String(state.commandQuickOpen?.taskError || "");
  const rows = [
    `${color(ANSI.cyan, "搜索:")} ${query || color(ANSI.dim, provider === "tasks" ? "输入任务 ID、标题、Owner、来源或状态" : provider === "sessions" ? "输入会话标题、ID、模型或分支" : provider === "files" ? "输入相对路径、文件名或扩展名" : "输入命令、别名、说明、类别或风险")}${color(ANSI.yellow, "█")}`,
    color(ANSI.dim, `Provider ${{ commands: "命令", tasks: "任务", sessions: "会话", files: "文件" }[provider]} · 结果 ${items.length} · 选择只填入输入框`),
    "",
  ];
  if (!visible.length) {
    if (provider === "tasks" && taskLoading) {
      rows.push(color(ANSI.cyan, "正在读取权威任务快照…"));
    } else if (provider === "tasks" && taskError) {
      rows.push(color(ANSI.yellow, compactText(taskError, 160)));
    } else if (provider === "tasks" && state.commandQuickOpen?.taskWarnings?.length) {
      rows.push(color(ANSI.yellow, "没有匹配任务。部分来源不可用，可运行 /doctor 检查。"));
    } else if (provider === "sessions" && state.commandQuickOpen?.sessionLoading) {
      rows.push(color(ANSI.cyan, "正在读取当前工作区会话…"));
    } else if (provider === "sessions" && state.commandQuickOpen?.sessionError) {
      rows.push(color(ANSI.yellow, compactText(state.commandQuickOpen.sessionError, 160)));
    } else if (provider === "files" && state.commandQuickOpen?.fileLoading) {
      rows.push(color(ANSI.cyan, "正在后台建立 Workspace 文件索引，可按 Esc 取消…"));
    } else if (provider === "files" && state.commandQuickOpen?.fileError) {
      rows.push(color(ANSI.yellow, compactText(state.commandQuickOpen.fileError, 160)));
    } else {
      rows.push(color(ANSI.yellow, `没有匹配${{ commands: "命令", tasks: "任务", sessions: "会话", files: "文件" }[provider]}。`));
    }
  } else {
    for (const item of visible) {
      const marker = item.selected ? "›" : " ";
      if (provider === "tasks") {
        const taskText = `${marker} ${compactText(item.title || item.task_id, 100)} · ${taskSourceLabel(item.source)} · ${taskStatusLabel(item.status)} · ${compactText(item.task_id, 80)}${item.owner ? ` · ${compactText(item.owner, 60)}` : ""}`;
        rows.push(color(taskStatusColor(item.status, item.selected), taskText));
      } else if (provider === "sessions") {
        const current = item.is_current ? " · 当前" : "";
        rows.push(color(item.selected ? ANSI.cyan : ANSI.dim, `${marker} ${compactText(item.title, 100)} · ${compactText(item.session_id, 80)} · ${compactText(item.model, 80)}${current}`));
      } else if (provider === "files") {
        const directory = item.directory ? ` · ${compactText(item.directory, 100)}` : "";
        const extension = item.extension ? ` · ${compactText(item.extension, 30)}` : "";
        rows.push(color(item.selected ? ANSI.cyan : ANSI.dim, `${marker} ${compactText(item.name, 120)}${directory}${extension}`));
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
      `将填入: ${provider === "tasks" ? taskTemplate(selected) : provider === "sessions" ? sessionTemplate(selected) : provider === "files" ? fileTemplate(selected) : commandTemplate(selected)} · 不会自动发送或执行`,
    ));
  }
  if (provider === "files" && state.commandQuickOpen?.fileMeta) {
    const meta = state.commandQuickOpen.fileMeta;
    rows.push(color(
      ANSI.dim,
      `索引 ${meta.totalIndexed} 个文件 · ${meta.source === "git" ? "Git ignore-aware" : "文件系统"}${meta.truncated ? " · 已达边界" : ""} · revision ${meta.revision}`,
    ));
  }
  rows.push(color(ANSI.dim, "Tab 切换命令/任务/会话/文件 · ↑/↓ 选择 · Enter 填入 · Esc/Ctrl+P 取消"));
  const boundedHeight = Math.max(1, height);
  const output = boxLines({ commands: "命令 QuickOpen", tasks: "任务 QuickOpen", sessions: "会话 QuickOpen", files: "文件 QuickOpen" }[provider], rows, width).slice(0, boundedHeight);
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
