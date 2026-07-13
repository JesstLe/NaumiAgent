import process from "node:process";
import {
  ANSI,
  color,
  compactText,
  formatContext,
  formatMoney,
  shortPath,
  truncateAnsi,
  visibleWidth,
  wrapAnsiLine,
} from "../ansi.js";
import { boxLines } from "./core.js";
import { renderInputLinesWithCursor } from "../input-buffer.js";
import { getSlashCompletionItems } from "../slash-completion.js";

export function Footer({ state, env = {} }) {
  return {
    render(ctx) {
      return renderFooter(state, ctx.width, env);
    },
  };
}

export function PermissionFooter({ permission }) {
  return {
    render(ctx) {
      if (!permission) return [];
      const tool = permission.payload.tool_name ?? "tool";
      const reason = compactText(permission.payload.reason ?? "");
      return wrapAnsiLine(
        color(ANSI.yellow, `permission: ${tool}  y=允许 n=拒绝 b/Shift+Tab=bypass  ${reason}`),
        ctx.width,
      );
    },
  };
}

export function TodoFooter({ todo }) {
  return {
    render(ctx) {
      if (!todo) return [];
      const current = todo.current;
      const currentText = current ? `#${current.id} ${current.subject}` : "有未完成任务";
      return wrapAnsiLine(color(ANSI.cyan, `todo: ${todo.completed}/${todo.total} 完成 | ${currentText}`), ctx.width);
    },
  };
}

export function TaskSelectionFooter({ taskPanel }) {
  return {
    render(ctx) {
      const items = taskPanel?.items ?? [];
      if (!items.length) return [];
      const index = Math.max(0, Math.min(items.length - 1, Number(taskPanel.selectedIndex) || 0));
      const item = items[index];
      if (!item) return [];
      if (!taskPanel.focused) {
        return wrapAnsiLine(
          color(ANSI.dim, `task: ${index + 1}/${items.length} ${item.id} · /tasks focus 聚焦 · /tasks open 详情`),
          ctx.width,
        );
      }
      return wrapAnsiLine(
        color(ANSI.magenta, `task: ${index + 1}/${items.length} ${item.id} · Tab/n 选择 · Enter/o 详情 · e/c 展开 · j 记录 · x 取消 · /tasks timeline 折叠来源 · Esc 退出`),
        ctx.width,
      );
    },
  };
}

export function NewOutputFooter({ state }) {
  return {
    render(ctx) {
      if (state.followTail || Number(state.unreadOutputCount) <= 0) return [];
      return wrapAnsiLine(
        color(ANSI.cyan, `↓ 有 ${state.unreadOutputCount} 条新输出 · End/Ctrl+L 跳到最新`),
        ctx.width,
      );
    },
  };
}

export function StatusFooter({ state, env = {} }) {
  return {
    render(ctx) {
      const status = state.status ?? {};
      const context = status.context ?? {};
      const budget = status.budget ?? {};
      const git = status.git ?? {};
      const lastFirstTokenLatencyMs = Number(state.lastFirstTokenLatencyMs ?? 0);
      const tasks = formatTaskActivity(status.tasks);
      const time = new Date().toLocaleTimeString("zh-CN", { hour12: false });
      const session = state.currentSessionId ? `会话:${state.currentSessionId.slice(0, 8)}` : "会话:-";
      const firstToken =
        Number.isFinite(lastFirstTokenLatencyMs) && lastFirstTokenLatencyMs > 0
          ? `首字: ${(lastFirstTokenLatencyMs / 1000).toFixed(1)}s`
          : null;
      const parts = [
        time,
        `mode: ${state.mode}`,
        `reasoning: ${state.showReasoning ? "on" : "off"}`,
        `运行: ${state.cancelPending ? "正在停止" : state.running ? "进行中" : "空闲"}`,
        session,
        ...(tasks ? [`tasks: ${tasks}`] : []),
        status.model || "model: -",
        `工作区: ${shortPath(status.workspace_root || env.cwd || process.cwd(), env.home ?? process.env.HOME)}`,
        `Token: ${status.usage?.total_tokens ?? 0}`,
        ...(firstToken ? [firstToken] : []),
        `上下文: ${formatContext(context)}`,
        `预算: ${formatMoney(budget.used_usd)}/${formatMoney(budget.max_usd)}`,
      ];
      if (git.branch) parts.push(`${git.branch}${git.dirty ? "*" : ""}`);
      return wrapAnsiLine(color(ANSI.dim, truncateAnsi(parts.join(" | "), ctx.width)), ctx.width);
    },
  };
}

export function PromptFooter({ state }) {
  return {
    render(ctx) {
      const intent = state.composerIntent === "task" ? "task" : "chat";
      const prefix = `${color(intent === "task" ? ANSI.cyan : ANSI.green, intent)} ${state.running ? color(ANSI.dim, "running") : ">"} `;
      const indent = " ".repeat(visibleWidth(prefix));
      const inputWidth = Math.max(1, ctx.width - visibleWidth(prefix));
      return renderInputLinesWithCursor(state, inputWidth, 6).map(
        (line, index) => `${index === 0 ? prefix : indent}${line}`,
      );
    },
  };
}

export function HelpFooter() {
  return {
    render(ctx) {
      return wrapAnsiLine(color(ANSI.dim, "Ctrl+I Inspector · Ctrl+T 对话/任务 · Shift+Tab 模式 · Enter 发送 · Shift+Enter 换行 · Ctrl+R 历史 · ↑/↓ 导航 · PgUp/PgDn 滚动 · Ctrl+C 取消/退出"), ctx.width);
    },
  };
}

export function HistorySearchFooter({ state }) {
  return {
    render(ctx) {
      const search = state.historySearch;
      if (!search?.open) return [];
      const matches = Array.isArray(search.matches) ? search.matches : [];
      const index = Math.max(0, Math.min(matches.length - 1, Number(search.selectedIndex) || 0));
      const query = search.query ? compactText(search.query) : "全部记录";
      const rows = [color(ANSI.cyan, `查询: ${query}`)];
      if (matches.length) {
        rows.push(`${index + 1}/${matches.length}  ${compactText(matches[index])}`);
      } else {
        rows.push(color(ANSI.yellow, "没有匹配记录"));
      }
      rows.push(color(ANSI.dim, "Ctrl+R/↓/Tab 更早 · ↑ 更新 · Enter 使用 · Esc 取消"));
      return boxLines("历史搜索", rows, ctx.width);
    },
  };
}

export function CommandCompletionFooter({ state }) {
  return {
    render(ctx) {
      if (state.historySearch?.open) return [];
      const completions = getSlashCompletionItems(state);
      if (!completions.length) return [];
      const rows = completions.map((item, index) => {
        const alias = item.aliases.length ? `(${item.aliases.join(", ")})` : "";
        const marker = item.selected ? ">" : " ";
        const text = `${marker} ${String(index + 1).padStart(2, "0")}. ${item.command} ${alias} ${item.description}`;
        return color(ANSI.cyan, text.trim());
      });
      return boxLines("命令补全", rows, ctx.width);
    },
  };
}

export function renderFooter(state, width, env = {}) {
  return renderFooterSections(state, width, env).flatMap((section) => section.lines);
}

export function renderFooterSections(state, width, env = {}) {
  const ctx = { width };
  return [
    { name: "permission", lines: PermissionFooter({ permission: state.permission }).render(ctx) },
    { name: "todo", lines: TodoFooter({ todo: state.todo }).render(ctx) },
    { name: "task-selection", lines: TaskSelectionFooter({ taskPanel: state.taskPanel }).render(ctx) },
    { name: "history-search", lines: HistorySearchFooter({ state }).render(ctx) },
    { name: "command-completion", lines: CommandCompletionFooter({ state }).render(ctx) },
    { name: "new-output", lines: NewOutputFooter({ state }).render(ctx) },
    { name: "status", lines: StatusFooter({ state, env }).render(ctx) },
    { name: "prompt", lines: PromptFooter({ state }).render(ctx) },
    { name: "help", lines: HelpFooter().render(ctx) },
  ].filter((section) => section.lines.length > 0);
}

function formatTaskActivity(tasks) {
  if (!tasks || typeof tasks !== "object") return "";
  const parts = [];
  if (Number(tasks.background_running) > 0) parts.push(`bg ${Number(tasks.background_running)}`);
  if (Number(tasks.background_attention) > 0) parts.push(`bg! ${Number(tasks.background_attention)}`);
  if (Number(tasks.subagents_active) > 0) parts.push(`agent ${Number(tasks.subagents_active)}`);
  if (Number(tasks.browser_active) > 0) parts.push(`browser ${Number(tasks.browser_active)}`);
  if (Number(tasks.permissions_pending) > 0) parts.push(`perm ${Number(tasks.permissions_pending)}`);
  return parts.join(" ");
}
