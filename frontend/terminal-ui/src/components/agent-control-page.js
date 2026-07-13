import {
  ANSI,
  charWidth,
  color,
  compactText,
  padRight,
  stripAnsi,
  visibleWidth,
  wrapAnsiLine,
} from "../ansi.js";

const TABS = [
  { id: "agents", label: "Agent" },
  { id: "executions", label: "执行" },
  { id: "team", label: "协作" },
];

export function renderAgentControlPage(view, width, height) {
  const safeWidth = Math.max(1, Number(width) || 1);
  const safeHeight = Math.max(1, Number(height) || 1);
  const snapshot = view?.snapshot;
  const header = [
    color(ANSI.cyan, fitAnsiWidth("Agent Control Center", safeWidth)),
    fitAnsiWidth(renderSummary(view, snapshot), safeWidth),
    fitAnsiWidth(renderTabs(view?.selectedTab), safeWidth),
    fitAnsiWidth(renderPageState(view), safeWidth),
  ];
  const bodyHeight = Math.max(0, safeHeight - header.length);
  let body;
  if (!snapshot) {
    body = [view?.error
      ? color(ANSI.red, `加载失败 · ${compactText(view.error, 500)}`)
      : color(ANSI.cyan, "正在加载 Agent 权威快照…")];
  } else if (safeWidth >= 100) {
    body = renderWideBody(view, snapshot, safeWidth, bodyHeight);
  } else if (view?.detailId) {
    body = renderDetail(view, snapshot, safeWidth).map((line) => fitAnsiWidth(line, safeWidth));
  } else {
    body = renderList(view, snapshot, safeWidth, bodyHeight).map((line) => fitAnsiWidth(line, safeWidth));
  }
  const lines = [...header, ...body.slice(0, bodyHeight)];
  while (lines.length < safeHeight) lines.push("");
  return lines.slice(0, safeHeight).map((line) => padRight(fitAnsiWidth(line, safeWidth), safeWidth));
}

function renderSummary(view, snapshot) {
  const summary = snapshot?.summary || {};
  const revision = Number(snapshot?.revision ?? view?.revision ?? 0) || 0;
  return [
    `rev ${revision}`,
    `Agent ${number(summary.total_agents)}`,
    `运行 ${number(summary.active_agents)}`,
    `需注意 ${number(summary.attention_agents)}`,
    `可停止 ${number(summary.stoppable_executions)}`,
    `消息 ${number(summary.pending_messages)}`,
    snapshot?.generated_at ? `更新 ${compactText(snapshot.generated_at, 40)}` : "",
  ].filter(Boolean).join(" · ");
}

function renderTabs(selected) {
  return TABS.map((tab) => (
    tab.id === selected
      ? color(ANSI.cyan, `[${tab.label}]`)
      : color(ANSI.dim, tab.label)
  )).join("  ");
}

function renderPageState(view) {
  if (view?.stopConfirmationTaskId) {
    return color(ANSI.yellow, `确认停止 ${view.stopConfirmationTaskId}？y 确认，n/Esc 取消`);
  }
  if (view?.actionPendingTaskId) {
    return color(ANSI.yellow, view.actionMessage || "正在请求停止…");
  }
  if (view?.stale) {
    return color(ANSI.yellow, `状态 · 已过期${view.error ? ` · ${compactText(view.error, 300)}` : ""}`);
  }
  if (view?.error && !view?.snapshot) return color(ANSI.red, `状态 · 加载失败 · ${compactText(view.error, 300)}`);
  if (view?.loading) return color(ANSI.cyan, "状态 · 正在加载");
  if (array(view?.snapshot?.warnings).length) {
    return color(ANSI.yellow, `警告 · ${compactText(view.snapshot.warnings[0], 300)}`);
  }
  return color(ANSI.dim, "Tab 切换 · ↑/↓ 选择 · Enter 详情 · r 刷新 · x 停止 · Esc 返回");
}

function renderWideBody(view, snapshot, width, height) {
  const listWidth = Math.max(42, Math.min(Math.floor(width * 0.44), width - 43));
  const detailWidth = Math.max(1, width - listWidth - 1);
  const list = renderList(view, snapshot, listWidth, height);
  const detail = renderDetail(view, snapshot, detailWidth);
  return Array.from({ length: height }, (_, index) => {
    const left = padRight(fitAnsiWidth(list[index] || "", listWidth), listWidth);
    const right = padRight(fitAnsiWidth(detail[index] || "", detailWidth), detailWidth);
    return `${left}${color(ANSI.blue, "│")}${right}`;
  });
}

function renderList(view, snapshot, width, maxLines = 100) {
  const selected = String(view?.selectedByTab?.[view?.selectedTab] || "");
  if (view?.selectedTab === "executions") {
    const items = array(snapshot.executions);
    if (!items.length) return [color(ANSI.dim, "暂无执行记录")];
    return visibleListItems(view, items, maxLines).map((item) => {
      const marker = item.task_id === selected ? color(ANSI.cyan, "›") : " ";
      const stop = item.stop_supported ? " · 可停止" : "";
      return `${marker} ${executionStatus(item.status)} ${compactText(item.task_id, 120)} · ${compactText(item.agent_name, 80)}${stop}`;
    }).flatMap((line) => wrapAnsiLine(line, Math.max(1, width))).slice(0, maxLines);
  }
  if (view?.selectedTab === "team") {
    const messages = array(snapshot.team_messages).map((item) => ({
      id: `message:${item.timestamp}:${item.sender}:${item.topic}`,
      text: `消息 · ${item.sender} → ${item.recipient || "all"} · ${item.topic}`,
    }));
    const entries = array(snapshot.blackboard).map((item) => ({
      id: `blackboard:${item.key}`,
      text: `黑板 · ${item.key} · v${item.version}`,
    }));
    const items = [...messages, ...entries];
    if (!items.length) return [color(ANSI.dim, "暂无团队消息或黑板记录")];
    return visibleListItems(view, items, maxLines)
      .map((item) => `${item.id === selected ? color(ANSI.cyan, "›") : " "} ${compactText(item.text, 500)}`)
      .flatMap((line) => wrapAnsiLine(line, Math.max(1, width)))
      .slice(0, maxLines);
  }
  const items = array(snapshot.agents);
  if (!items.length) return [color(ANSI.dim, "暂无 Agent")];
  return visibleListItems(view, items, maxLines).map((item) => {
    const marker = item.name === selected ? color(ANSI.cyan, "›") : " ";
    return `${marker} ${agentState(item.state)} ${compactText(item.name, 100)} · ${item.kind} · 任务 ${number(item.task_count)}`;
  }).flatMap((line) => wrapAnsiLine(line, Math.max(1, width))).slice(0, maxLines);
}

function visibleListItems(view, items, maxLines) {
  const count = Math.max(1, Number(maxLines) || 1);
  const cursor = Math.max(0, Number(view?.scrollByTab?.[view?.selectedTab]) || 0);
  const start = Math.max(0, Math.min(items.length - count, cursor - Math.floor(count / 2)));
  return items.slice(start, start + count);
}

function renderDetail(view, snapshot, width) {
  const id = String(view?.detailId || view?.selectedByTab?.[view?.selectedTab] || "");
  if (view?.selectedTab === "executions") {
    const item = array(snapshot.executions).find((entry) => entry.task_id === id);
    if (!item) return [color(ANSI.dim, "选择一条执行查看详情")];
    return [
      color(ANSI.cyan, "执行详情"),
      `任务 · ${item.task_id}`,
      `Agent · ${item.agent_name}`,
      `状态 · ${item.status} / ${item.phase}`,
      `当前工具 · ${item.current_tool || "-"}`,
      `最近工具 · ${array(item.recent_tools).join(", ") || "-"}`,
      `耗时 · ${number(item.elapsed_ms)}ms · 心跳 ${number(item.heartbeat_age_ms)}ms`,
      `Token · ${number(item.total_tokens)} · $${Number(item.total_cost_usd || 0).toFixed(4)} · ${number(item.turns)} 轮`,
      `描述 · ${item.description || "-"}`,
      item.error ? color(ANSI.red, `错误 · ${item.error}`) : "",
      item.stop_supported ? color(ANSI.yellow, "按 x 请求停止") : color(ANSI.dim, "当前不可停止"),
    ].filter(Boolean).flatMap((line) => wrapAnsiLine(line, Math.max(1, width)));
  }
  if (view?.selectedTab === "team") {
    if (id.startsWith("blackboard:")) {
      const item = array(snapshot.blackboard).find((entry) => `blackboard:${entry.key}` === id);
      if (!item) return [color(ANSI.dim, "选择团队记录查看详情")];
      return [color(ANSI.cyan, "黑板详情"), `键 · ${item.key}`, `作者 · ${item.author}`, `版本 · ${item.version}`, `值摘要 · ${item.value_summary}`]
        .flatMap((line) => wrapAnsiLine(line, Math.max(1, width)));
    }
    const item = array(snapshot.team_messages).find(
      (entry) => `message:${entry.timestamp}:${entry.sender}:${entry.topic}` === id,
    );
    if (!item) return [color(ANSI.dim, "选择团队记录查看详情")];
    return [color(ANSI.cyan, "消息详情"), `来自 · ${item.sender}`, `发送给 · ${item.recipient || "all"}`, `主题 · ${item.topic}`, `优先级 · ${item.priority}`, `内容 · ${item.content}`]
      .flatMap((line) => wrapAnsiLine(line, Math.max(1, width)));
  }
  const item = array(snapshot.agents).find((entry) => entry.name === id);
  if (!item) return [color(ANSI.dim, "选择 Agent 查看详情")];
  return [
    color(ANSI.cyan, "Agent 详情"),
    `名称 · ${item.name}`,
    `描述 · ${item.description || "-"}`,
    `类型 · ${item.kind} · 状态 ${item.state}`,
    `模型 · ${item.model_tier || "-"}`,
    `权限 · ${item.permission_level || "-"}`,
    `能力 · ${array(item.capabilities).join(", ") || "-"}`,
    `工具 · ${array(item.tools).join(", ") || "-"}`,
    `年龄 · ${number(item.age_ms)}ms · 心跳 ${number(item.heartbeat_age_ms)}ms`,
  ].flatMap((line) => wrapAnsiLine(line, Math.max(1, width)));
}

function executionStatus(status) {
  if (["completed"].includes(status)) return color(ANSI.green, "✓");
  if (["error", "failed", "timeout", "max_turns"].includes(status)) return color(ANSI.red, "!");
  if (status === "cancelled") return color(ANSI.yellow, "×");
  return color(ANSI.cyan, "●");
}

function agentState(state) {
  if (["running", "spawned"].includes(state)) return color(ANSI.cyan, "●");
  if (["destroyed"].includes(state)) return color(ANSI.dim, "×");
  return color(ANSI.green, "●");
}

function array(value) {
  return Array.isArray(value) ? value : [];
}

function number(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

function fitAnsiWidth(line, width) {
  const safeWidth = Math.max(1, Number(width) || 1);
  if (visibleWidth(line) <= safeWidth) return line;
  const target = Math.max(0, safeWidth - 1);
  let used = 0;
  let result = "";
  for (const character of Array.from(stripAnsi(line))) {
    const next = charWidth(character);
    if (used + next > target) break;
    result += character;
    used += next;
  }
  return `${result}…`;
}
