import { ANSI, color, compactText } from "../ansi.js";
import {
  commandCategoryLabel,
  commandRiskColor,
  commandRiskLabel,
  commandTemplate,
} from "../command-metadata.js";
import { getCommandQuickOpenItems } from "../command-quick-open.js";
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
  const rows = [
    `${color(ANSI.cyan, "搜索:")} ${query || color(ANSI.dim, "输入命令、别名、说明、类别或风险")}${color(ANSI.yellow, "█")}`,
    color(ANSI.dim, `结果 ${items.length} · 权威命令索引 · 选择只填入输入框`),
    "",
  ];
  if (!visible.length) {
    rows.push(color(ANSI.yellow, "没有匹配命令。"));
  } else {
    for (const item of visible) {
      const marker = item.selected ? "›" : " ";
      const category = commandCategoryLabel(item.category);
      const risk = commandRiskLabel(item.permission_risk);
      const text = `${marker} ${commandTemplate(item)} · ${category} · ${risk} · ${compactText(item.description, 120)}`;
      rows.push(color(commandRiskColor(item.permission_risk, item.selected), text));
    }
    const selected = items[selectedIndex];
    rows.push("");
    rows.push(color(ANSI.dim, `将填入: ${commandTemplate(selected)} · 不会自动发送或执行`));
  }
  rows.push(color(ANSI.dim, "↑/↓/Tab 选择 · Enter 填入 · Esc/Ctrl+P 取消"));
  const boundedHeight = Math.max(1, height);
  const output = boxLines("命令 QuickOpen", rows, width).slice(0, boundedHeight);
  while (output.length < boundedHeight) output.push("");
  return output;
}
