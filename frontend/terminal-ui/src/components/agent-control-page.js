import {
  ANSI,
  color,
  compactText,
  padRight,
  truncateAnsi,
  visibleWidth,
  wrapAnsiLine,
} from "../ansi.js";

const TABS = [
  { id: "agents", label: "Agent" },
  { id: "executions", label: "执行" },
  { id: "results", label: "结果" },
  { id: "recovery", label: "恢复" },
  { id: "team", label: "协作" },
];

export function renderAgentControlPage(view, width, height) {
  const safeWidth = Math.max(1, Number(width) || 1);
  const safeHeight = Math.max(1, Number(height) || 1);
  const snapshot = view?.snapshot;
  const header = [
    color(ANSI.cyan, fitAnsiWidth("Agent Control Center", safeWidth)),
    fitAnsiWidth(renderSummary(view, snapshot), safeWidth),
    fitAnsiWidth(renderDurableSummary(snapshot), safeWidth),
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
    `结果 ${number(summary.durable_results_visible)}`,
    number(summary.durable_unread_results) > 0
      ? color(ANSI.yellow, `未读 ${number(summary.durable_unread_results)}`)
      : color(ANSI.green, "未读 0"),
    snapshot?.generated_at ? `更新 ${compactText(snapshot.generated_at, 40)}` : "",
  ].filter(Boolean).join(" · ");
}

function renderDurableSummary(snapshot) {
  const summary = snapshot?.summary || {};
  const durable = summary.durable_capacity_configured
    ? durableCapacitySummary(summary)
    : "";
  return [
    durablePublicationSummary(summary),
    recoveryCatalogSummary(snapshot?.recovery_catalog),
    durable,
  ].filter(Boolean).join(" · ");
}

function recoveryCatalogSummary(catalog) {
  const items = array(catalog?.items);
  if (!items.length) return "";
  const attention = items.filter((item) => [
    "recovery_required",
    "outcome_unknown",
    "publication_claim_expired",
    "publication_quarantined",
  ].includes(item.recovery_state)).length;
  const label = `恢复目录 ${items.length}${catalog?.truncated ? "+" : ""}`;
  return color(attention > 0 ? ANSI.red : ANSI.yellow, label);
}

function durablePublicationSummary(summary) {
  const pending = number(summary.durable_publications_pending);
  const claimed = number(summary.durable_publications_claimed);
  const expired = number(summary.durable_publications_expired);
  const quarantined = number(summary.durable_publications_quarantined);
  if (quarantined > 0) {
    return color(
      ANSI.red,
      `发布待处理 ${pending} · 已认领 ${claimed} · 已隔离 ${quarantined}`,
    );
  }
  if (expired > 0) {
    return color(
      ANSI.red,
      `发布待处理 ${pending} · 已认领 ${claimed} · 过期 ${expired}`,
    );
  }
  if (pending > 0 || claimed > 0) {
    return color(
      ANSI.yellow,
      `发布待处理 ${pending} · 已认领 ${claimed}`,
    );
  }
  return "";
}

function durableCapacitySummary(summary) {
  const active = `${number(summary.durable_active_jobs)}/${number(summary.durable_max_active_jobs)}`;
  const waiting = `${number(summary.durable_waiting_jobs)}/${number(summary.durable_max_waiters)}`;
  const recovery = number(summary.durable_recovery_required_jobs);
  const reclaimable = number(summary.durable_reclaimable_jobs);
  if (recovery > 0) {
    return color(
      ANSI.red,
      `共享容量 ${active} · 等待 ${waiting} · 待恢复 ${recovery}`,
    );
  }
  if (Number(summary.durable_waiting_jobs) > 0 || reclaimable > 0) {
    return color(
      ANSI.yellow,
      `共享容量 ${active} · 等待 ${waiting} · 可接管 ${reclaimable}`,
    );
  }
  return color(ANSI.green, `共享容量 ${active} · 等待 ${waiting}`);
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
  if (view?.recoveryActionPendingId) {
    return color(ANSI.yellow, view.actionMessage || "正在提交精确恢复裁决…");
  }
  if (view?.resultAckPendingId) {
    return color(ANSI.yellow, view.actionMessage || "正在签发结果已读回执…");
  }
  if (view?.stale) {
    return color(ANSI.yellow, `状态 · 已过期${view.error ? ` · ${compactText(view.error, 300)}` : ""}`);
  }
  if (view?.error && !view?.snapshot) return color(ANSI.red, `状态 · 加载失败 · ${compactText(view.error, 300)}`);
  if (view?.loading) return color(ANSI.cyan, "状态 · 正在加载");
  if (view?.actionMessage) {
    return color(ANSI.cyan, compactText(view.actionMessage, 500));
  }
  if (array(view?.snapshot?.warnings).length) {
    return color(ANSI.yellow, `警告 · ${compactText(view.snapshot.warnings[0], 300)}`);
  }
  if (view?.selectedTab === "recovery") {
    return color(ANSI.dim, "恢复目录 · ↑/↓ 选择 · Enter 详情 · u 精确裁决 · r 刷新 · Esc 返回");
  }
  if (view?.selectedTab === "results") {
    return color(ANSI.dim, "结果 · ↑/↓ 选择 · Enter 详情 · v 标记已读 · r 刷新 · Esc 返回");
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
  if (view?.selectedTab === "results") {
    const items = array(snapshot.results);
    if (!items.length) return [color(ANSI.dim, "当前会话暂无持久结果")];
    return visibleListItems(view, items, maxLines).map((item) => {
      const marker = item.delivery_id === selected ? color(ANSI.cyan, "›") : " ";
      const truncated = item.content_truncated ? color(ANSI.yellow, " · 已脱敏/截断") : "";
      const readState = item.acknowledged
        ? color(ANSI.green, "已读")
        : color(ANSI.yellow, "未读");
      return `${marker} ${readState} ${executionStatus(item.status)} ${compactText(item.task_id, 120)} · ${compactText(item.agent_name, 80)}${truncated}`;
    }).flatMap((line) => wrapAnsiLine(line, Math.max(1, width))).slice(0, maxLines);
  }
  if (view?.selectedTab === "recovery") {
    const catalog = snapshot.recovery_catalog || {};
    const items = array(catalog.items);
    if (!items.length) return [color(ANSI.green, "暂无 Agent 恢复条目")];
    const selectedItems = visibleListItems(view, items, maxLines);
    const lines = selectedItems.map((item) => {
      const id = `recovery:${item.kind}:${item.item_id}`;
      const marker = id === selected ? color(ANSI.cyan, "›") : " ";
      return `${marker} ${recoveryStatus(item.recovery_state)} ${compactText(item.item_id, 120)} · ${compactText(item.agent_name, 80)} · ${sessionScope(item.session_scope)}`;
    });
    if (catalog.truncated) {
      lines.push(color(ANSI.yellow, "  展示已达到 50 项上限"));
    }
    return lines
      .flatMap((line) => wrapAnsiLine(line, Math.max(1, width)))
      .slice(0, maxLines);
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
      `执行后端 · ${item.worker_backend === "independent" ? "独立 Agent Worker" : "内嵌降级"}`,
      `当前工具 · ${item.current_tool || "-"}`,
      `最近工具 · ${array(item.recent_tools).join(", ") || "-"}`,
      `Worker 工具范围 · ${workerToolScope(item.worker_tool_scope)}`,
      item.worker_contract_failure_code
        ? color(ANSI.yellow, `Worker 合同降级 · ${item.worker_contract_failure_code}`)
        : `Worker 合同 · 请求 ${shortDigest(item.worker_request_sha256)} · 结果 ${shortDigest(item.worker_result_sha256)}`,
      item.worker_job_failure_code
        ? color(
          ANSI.yellow,
          `持久任务降级 · ${item.worker_job_failure_code} · 状态 ${item.worker_job_state || "未知"} · epoch ${number(item.worker_claim_epoch)}`,
        )
        : `持久任务 · ${shortDigest(item.worker_job_id)} · 状态 ${item.worker_job_state || "未接入"} · epoch ${number(item.worker_claim_epoch)}`,
      item.heartbeat_failure_code
        ? color(
          ANSI.yellow,
          `耗时 · ${number(item.elapsed_ms)}ms · 持久心跳降级 ${item.heartbeat_failure_code}`,
        )
        : `耗时 · ${number(item.elapsed_ms)}ms · 心跳 ${number(item.heartbeat_age_ms)}ms · 持久 ${item.heartbeat_phase || "未启用"}`,
      `Token · ${number(item.total_tokens)} · $${Number(item.total_cost_usd || 0).toFixed(4)} · ${number(item.turns)} 轮`,
      `描述 · ${item.description || "-"}`,
      item.error ? color(ANSI.red, `错误 · ${item.error}`) : "",
      item.stop_supported ? color(ANSI.yellow, "按 x 请求停止") : color(ANSI.dim, "当前不可停止"),
    ].filter(Boolean).flatMap((line) => wrapAnsiLine(line, Math.max(1, width)));
  }
  if (view?.selectedTab === "results") {
    const item = array(snapshot.results).find((entry) => entry.delivery_id === id);
    if (!item) return [color(ANSI.dim, "选择一条持久结果查看详情")];
    return [
      color(ANSI.cyan, "持久结果详情"),
      `任务 · ${item.task_id}`,
      `Agent · ${item.agent_name}`,
      `状态 · ${item.status} · ${item.reason_code || "-"}`,
      `投递时间 · ${item.delivered_at}`,
      `Token · ${number(item.total_tokens)} · $${Number(item.total_cost_usd || 0).toFixed(4)} · ${number(item.turns)} 轮`,
      `响应大小 · ${number(item.response_bytes)} bytes`,
      `结果摘要 · ${shortDigest(item.result_sha256)}`,
      `投递摘要 · ${shortDigest(item.delivery_sha256)}`,
      item.acknowledged
        ? color(ANSI.green, `已读 · ${item.acknowledged_at}`)
        : color(ANSI.yellow, "未读 · 按 v 签发不可变已读回执"),
      item.acknowledged
        ? `已读回执 · ${shortDigest(item.acknowledgement_receipt_sha256)}`
        : "",
      item.content_truncated
        ? color(ANSI.yellow, "展示内容已经脱敏或截断；原始结果仍保留在加密持久层。")
        : "",
      color(ANSI.blue, `任务摘录 · ${compactText(item.task_excerpt || "-", 2000)}`),
      item.response_excerpt
        ? color(ANSI.green, `回复摘录 · ${compactText(item.response_excerpt, 2000)}`)
        : color(ANSI.dim, "回复摘录 · -"),
      item.error_excerpt
        ? color(ANSI.red, `错误摘录 · ${compactText(item.error_excerpt, 2000)}`)
        : "",
    ].filter(Boolean).flatMap((line) => wrapAnsiLine(line, Math.max(1, width)));
  }
  if (view?.selectedTab === "recovery") {
    const item = array(snapshot.recovery_catalog?.items).find(
      (entry) => `recovery:${entry.kind}:${entry.item_id}` === id,
    );
    if (!item) return [color(ANSI.dim, "选择一条恢复事实查看详情")];
    return [
      color(ANSI.cyan, "Agent 恢复事实"),
      `${recoveryStatus(item.recovery_state)} · ${recoveryStateLabel(item.recovery_state)}`,
      `类型 · ${item.kind} · Agent ${item.agent_name}`,
      `Job · ${item.job_id} · 状态 ${item.job_state}`,
      item.publication_id ? `Publication · ${item.publication_id}` : "",
      `会话范围 · ${sessionScope(item.session_scope)}`,
      `claim epoch · ${number(item.claim_epoch)}${item.claim_expires_at ? ` · 到期 ${item.claim_expires_at}` : ""}`,
      item.kind === "publication" ? `投递尝试 · ${number(item.attempt_count)}` : "",
      `发生时间 · ${item.occurred_at}`,
      `请求摘要 · ${shortDigest(item.request_sha256)}`,
      `回执摘要 · ${shortDigest(item.receipt_sha256)}`,
      `原因码 · ${item.reason_code}`,
      (
        item.kind === "job"
        && item.recovery_state === "recovery_required"
        && item.session_scope === "current"
      )
        ? color(ANSI.yellow, "按 u 将该过期 running Job 精确收口为 unknown。")
        : color(ANSI.dim, "当前条目没有可用的人工恢复动作。"),
      color(ANSI.dim, "恢复裁决不会自动重放模型，也不会删除持久证据。"),
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

function shortDigest(value) {
  const digest = String(value || "");
  return digest ? digest.slice(0, 12) : "待生成";
}

function workerToolScope(value) {
  const tools = array(value);
  if (!tools.length) return "-";
  const visible = tools.slice(0, 8).join(", ");
  return tools.length > 8 ? `${visible}，另 ${tools.length - 8} 项` : visible;
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

function recoveryStatus(state) {
  if (["recovery_required", "outcome_unknown", "publication_claim_expired", "publication_quarantined"].includes(state)) {
    return color(ANSI.red, "!");
  }
  if (["reclaimable_prestart", "publication_pending"].includes(state)) {
    return color(ANSI.yellow, "◆");
  }
  return color(ANSI.cyan, "●");
}

function recoveryStateLabel(state) {
  return ({
    claim_active: "claim 仍有效",
    worker_active: "worker 仍在运行",
    reclaimable_prestart: "启动前 claim 可接管",
    recovery_required: "running Job 需要恢复裁决",
    outcome_unknown: "执行结果未知",
    publication_pending: "终态结果等待发布",
    publication_claim_expired: "发布 claim 已过期",
    publication_quarantined: "发布失败已隔离",
  })[state] || state;
}

function sessionScope(scope) {
  return ({ current: "当前会话", other: "其他会话", unknown: "会话未知" })[scope] || "会话未知";
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
  return visibleWidth(line) <= safeWidth ? line : truncateAnsi(line, safeWidth);
}
