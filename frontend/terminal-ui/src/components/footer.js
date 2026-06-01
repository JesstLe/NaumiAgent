import process from "node:process";
import {
  ANSI,
  color,
  compactText,
  formatContext,
  formatMoney,
  shortPath,
  truncateAnsi,
  wrapAnsiLine,
} from "../ansi.js";

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
      return wrapAnsiLine(color(ANSI.yellow, `permission: ${tool}  y=允许 n=拒绝 b=bypass  ${reason}`), ctx.width);
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

export function StatusFooter({ state, env = {} }) {
  return {
    render(ctx) {
      const status = state.status ?? {};
      const context = status.context ?? {};
      const budget = status.budget ?? {};
      const git = status.git ?? {};
      const parts = [
        `mode: ${state.mode}`,
        status.model || "model: -",
        `工作区: ${shortPath(status.workspace_root || env.cwd || process.cwd(), env.home ?? process.env.HOME)}`,
        `Token: ${status.usage?.total_tokens ?? 0}`,
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
      return wrapAnsiLine(`${color(ANSI.green, state.mode)} ${state.running ? color(ANSI.dim, "running") : ">"} ${state.input}`, ctx.width);
    },
  };
}

export function HelpFooter() {
  return {
    render(ctx) {
      return wrapAnsiLine(color(ANSI.dim, "Shift+Tab 切换模式 · Enter 发送 · PageUp/PageDown 滚动 · Ctrl+C 退出"), ctx.width);
    },
  };
}

export function renderFooter(state, width, env = {}) {
  return [
    PermissionFooter({ permission: state.permission }),
    TodoFooter({ todo: state.todo }),
    StatusFooter({ state, env }),
    PromptFooter({ state }),
    HelpFooter(),
  ].flatMap((component) => component.render({ width }));
}
