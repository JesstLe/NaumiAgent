import process from "node:process";
import {
  ANSI,
  color,
  compactText,
  formatContext,
  shortPath,
  visibleWidth,
  wrapAnsiLine,
} from "../ansi.js";
import { boxLines } from "./core.js";
import { renderInputLinesWithCursor } from "../input-buffer.js";
import { getSlashCompletionItems } from "../slash-completion.js";
import { formatBudgetStatus } from "./budget-status.js";
import { formatProviderIdentity } from "./provider-identity.js";

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
      const grant = permission.payload.choices?.includes("grant_session")
        ? " g=本会话授权"
        : "";
      return wrapAnsiLine(
        color(ANSI.yellow, `permission: ${tool}  y=允许 n=拒绝${grant} b/Shift+Tab=全权限  ${reason}`),
        ctx.width,
      );
    },
  };
}

export function InteractionFooter({ interaction }) {
  return {
    render(ctx) {
      if (!interaction) return [];
      const payload = interaction.payload ?? {};
      const options = Array.isArray(payload.options) ? payload.options : [];
      const rows = [
        color(ANSI.cyan, compactText(payload.header || "需要你的选择", 80)),
        compactText(payload.question || "请选择一个选项。", 500),
      ];
      if (interaction.customMode) {
        rows.push(color(ANSI.yellow, payload.custom_label || "其他"));
        rows.push(...renderInputLinesWithCursor(interaction, Math.max(1, ctx.width - 8), 4));
        rows.push(color(ANSI.dim, "Enter 提交 · Esc 返回选项 · Ctrl+C 取消运行"));
      } else {
        options.forEach((option, index) => {
          const marker = interaction.selectedIndex === index ? "›" : " ";
          const description = option.description ? ` · ${compactText(option.description, 180)}` : "";
          rows.push(`${color(interaction.selectedIndex === index ? ANSI.yellow : ANSI.dim, marker)} ${index + 1}. ${compactText(option.label, 80)}${description}`);
        });
        if (payload.allow_custom) {
          const index = options.length;
          const marker = interaction.selectedIndex === index ? "›" : " ";
          rows.push(`${color(interaction.selectedIndex === index ? ANSI.yellow : ANSI.dim, marker)} ${index + 1}. ${payload.custom_label || "其他"}`);
        }
        rows.push(color(ANSI.dim, interaction.submitting
          ? "正在提交回答..."
          : "↑/↓ 选择 · 数字定位 · Enter 确认 · Ctrl+C 取消运行"));
      }
      return boxLines("需要你的选择", rows, ctx.width);
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

export function AgentControlFooter({ agents }) {
  return {
    render(ctx) {
      if (!agents?.open) return [];
      const message = agents.stopConfirmationTaskId
        ? `agents: 确认停止 ${agents.stopConfirmationTaskId} · y 确认 · n/Esc 取消`
        : "agents: Tab/Shift+Tab 标签 · ↑/↓ 选择 · Enter 详情 · r 刷新 · x 停止 · Esc 返回";
      return wrapAnsiLine(
        color(agents.stopConfirmationTaskId ? ANSI.yellow : ANSI.cyan, message),
        ctx.width,
      );
    },
  };
}

export function WorkbenchFooter({ state }) {
  return {
    render(ctx) {
      if (state.route?.name !== "workbench") return [];
      const message = state.workbench?.loading
        ? "workbench: 正在刷新 · r 重试 · Esc 返回"
        : "workbench: r 刷新 · Esc 返回";
      return wrapAnsiLine(color(ANSI.cyan, message), ctx.width);
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
      const providerIdentity = status.provider || status.api_format
        ? formatProviderIdentity(status)
        : null;
      const reasoningEffort = String(status.reasoning_effort?.effective || "auto");
      const heartbeatWarning = state.bridgeHeartbeat?.status === "stale"
        ? "Bridge: 无响应"
        : null;
      const modelContractWarning = formatModelContractWarning(status.model_contract);
      const parts = [
        time,
        `mode: ${state.mode}`,
        `思考文本: ${state.showReasoning ? "on" : "off"}`,
        `强度: ${reasoningEffort}`,
        `运行: ${state.cancelPending ? "正在停止" : state.running ? "进行中" : "空闲"}`,
        ...(heartbeatWarning ? [heartbeatWarning] : []),
        ...(modelContractWarning ? [modelContractWarning] : []),
        session,
        ...(tasks ? [`tasks: ${tasks}`] : []),
        ...(providerIdentity ? [`提供方: ${providerIdentity}`] : []),
        status.model || "model: -",
        `工作区: ${shortPath(status.workspace_root || env.cwd || process.cwd(), env.home ?? process.env.HOME)}`,
        `Token: ${status.usage?.total_tokens ?? 0}`,
        ...(firstToken ? [firstToken] : []),
        `上下文: ${formatContext(context)}`,
        formatBudgetStatus(budget),
      ];
      if (git.branch) parts.push(`${git.branch}${git.dirty ? "*" : ""}`);
      return packStatusParts(parts, ctx.width).map((line) => color(ANSI.dim, line));
    },
  };
}

function formatModelContractWarning(contract) {
  const status = String(contract?.status || "").toLowerCase();
  const label = {
    partial: "模型契约: 部分可信",
    unverified: "模型契约: 未验证",
    incompatible: "模型契约: 不兼容",
  }[status];
  return label ? color(ANSI.yellow, label) : null;
}

function packStatusParts(parts, width) {
  const safeWidth = Math.max(1, Math.floor(Number(width) || 1));
  const lines = [];
  let current = "";
  for (const value of parts) {
    const part = String(value ?? "").trim();
    if (!part) continue;
    const candidate = current ? `${current} | ${part}` : part;
    if (visibleWidth(candidate) <= safeWidth) {
      current = candidate;
      continue;
    }
    if (current) {
      lines.push(current);
      current = "";
    }
    const wrapped = wrapAnsiLine(part, safeWidth);
    if (wrapped.length <= 1) {
      current = wrapped[0] ?? part;
      continue;
    }
    lines.push(...wrapped.slice(0, -1));
    current = wrapped.at(-1) ?? "";
  }
  if (current) lines.push(current);
  return lines;
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
    { name: "interaction", lines: state.permission ? [] : InteractionFooter({ interaction: state.interaction }).render(ctx) },
    { name: "agents", lines: AgentControlFooter({ agents: state.agents }).render(ctx) },
    { name: "workbench", lines: WorkbenchFooter({ state }).render(ctx) },
    { name: "todo", lines: TodoFooter({ todo: state.todo }).render(ctx) },
    { name: "task-selection", lines: TaskSelectionFooter({ taskPanel: state.taskPanel }).render(ctx) },
    { name: "history-search", lines: HistorySearchFooter({ state }).render(ctx) },
    { name: "command-completion", lines: CommandCompletionFooter({ state }).render(ctx) },
    { name: "new-output", lines: NewOutputFooter({ state }).render(ctx) },
    { name: "status", lines: StatusFooter({ state, env }).render(ctx) },
    {
      name: "prompt",
      lines: state.agents?.open || state.interaction || state.route?.name === "workbench"
        ? []
        : PromptFooter({ state }).render(ctx),
    },
    {
      name: "help",
      lines: state.agents?.open || state.interaction || state.route?.name === "workbench"
        ? []
        : HelpFooter().render(ctx),
    },
  ].filter((section) => section.lines.length > 0);
}

function formatTaskActivity(tasks) {
  if (!tasks || typeof tasks !== "object") return "";
  const parts = [];
  if (Number(tasks.background_running) > 0) parts.push(`bg ${Number(tasks.background_running)}`);
  if (Number(tasks.background_attention) > 0) parts.push(`bg! ${Number(tasks.background_attention)}`);
  if (Number(tasks.subagents_active) > 0) parts.push(`agent ${Number(tasks.subagents_active)}`);
  if (Number(tasks.browser_active) > 0) parts.push(`browser ${Number(tasks.browser_active)}`);
  if (Number(tasks.queued_conversations) > 0) parts.push(`queue ${Number(tasks.queued_conversations)}`);
  if (Number(tasks.permissions_pending) > 0) parts.push(`perm ${Number(tasks.permissions_pending)}`);
  if (Number(tasks.interactions_pending) > 0) parts.push(`ask ${Number(tasks.interactions_pending)}`);
  return parts.join(" ");
}
