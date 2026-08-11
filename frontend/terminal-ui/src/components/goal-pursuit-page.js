import {
  ANSI,
  color,
  compactText,
  padRight,
  visibleWidth,
  wrapAnsiLine,
} from "../ansi.js";

export function renderGoalPursuitPage(view, width, height) {
  const safeWidth = Math.max(1, Number(width) || 1);
  const safeHeight = Math.max(1, Number(height) || 1);
  const value = view && typeof view === "object" ? view : {};
  const snapshot = value.snapshot && typeof value.snapshot === "object"
    ? value.snapshot
    : null;
  const help = safeWidth >= 140
    ? "r 刷新 · ←/→ 选择 Goal · m 暂停/恢复 · x 恢复当前 Pursuit · o 恢复终态队列 · d/u/a/z 死信 · {/} 处置历史 · ↑/↓ 滚动 · j/k 交互 · Enter 详情 · f 筛选 · n/p 交互翻页 · Esc 返回"
    : "r 刷新 · m 暂停/恢复 · x 恢复当前 Pursuit · o 队列 · d/u/a/z 死信 · {/} 历史 · Esc";
  const logical = [
    color(ANSI.cyan, "Goal / Pursuit"),
    color(ANSI.dim, help),
  ];
  if (value.lifecycleActionPending) {
    logical.push(color(ANSI.yellow, "正在通过 ToolExecution 校验权限并更新 Goal…"));
  }
  if (value.lifecycleActionNotice) {
    logical.push(color(ANSI.green, compactText(value.lifecycleActionNotice, 4_000)));
  }
  if (value.lifecycleActionError) {
    logical.push(color(ANSI.red, compactText(value.lifecycleActionError, 4_000)));
  }
  if (value.recoveryActionPending) {
    logical.push(color(ANSI.yellow, "正在通过 ToolExecution 校验恢复权限与持久边界…"));
  }
  if (value.recoveryActionNotice) {
    logical.push(color(ANSI.green, compactText(value.recoveryActionNotice, 4_000)));
  }
  if (value.recoveryActionError) {
    logical.push(color(ANSI.red, compactText(value.recoveryActionError, 4_000)));
  }
  if (value.terminalOutboxActionPending) {
    logical.push(color(ANSI.yellow, "正在通过 ToolExecution 恢复到期的终态队列记录…"));
  }
  if (value.terminalOutboxActionNotice) {
    logical.push(color(ANSI.green, compactText(value.terminalOutboxActionNotice, 4_000)));
  }
  if (value.terminalOutboxActionError) {
    logical.push(color(ANSI.red, compactText(value.terminalOutboxActionError, 4_000)));
  }
  if (value.loading && !snapshot) {
    logical.push(color(ANSI.cyan, "正在读取 Goal / Pursuit 权威状态…"));
  } else if (!snapshot) {
    logical.push(color(ANSI.yellow, compactText(value.error || "Goal 快照暂不可用。", 500)));
  } else if (!snapshot.goals?.length) {
    logical.push(
      color(
        ANSI.dim,
        snapshot.include_finished
          ? "当前没有持久目标记录。使用 /goal <目标> 创建。"
          : "当前没有未完成目标。使用 /goal <目标> 创建。",
      ),
    );
  } else {
    logical.push(...renderGoalDirectory(
      snapshot.goals,
      snapshot.current_goal_id,
      snapshot.selected_goal_id,
    ));
    const selectedGoal = snapshot.goals.find(
      (goal) => goal.goal_id === snapshot.selected_goal_id,
    ) ?? snapshot.goals[0];
    logical.push(...renderGoal(selectedGoal, snapshot.current_goal_id));
    logical.push(...renderInteractionLedger(
      snapshot,
      Math.max(0, Number(value.selectedInteractionIndex) || 0),
    ));
    if (snapshot.selected_interaction) {
      logical.push(...renderInteractionDetail(snapshot.selected_interaction));
    }
    if (snapshot.truncated) {
      logical.push(color(ANSI.yellow, "目标历史已按当前视图上限截断。"));
    }
    if (snapshot.warnings?.length) {
      logical.push(
        color(ANSI.cyan, `── 警告 · ${snapshot.warnings.length}`),
        ...snapshot.warnings.map((item) => color(ANSI.yellow, compactText(item, 500))),
      );
    }
  }
  if (snapshot?.terminal_outbox) {
    logical.push(...renderTerminalOutbox(
      snapshot.terminal_outbox,
      Math.max(0, Number(value.selectedDeadLetterIndex) || 0),
      Math.max(0, Number(value.selectedAbandonReasonIndex) || 0),
    ));
  }
  const wrapped = logical.flatMap((line) => wrapAnsiLine(line, safeWidth));
  const maximum = Math.max(0, wrapped.length - safeHeight);
  const offset = Math.min(Math.max(0, Number(value.scrollOffset) || 0), maximum);
  const lines = wrapped.slice(offset, offset + safeHeight);
  while (lines.length < safeHeight) lines.push("");
  return lines.map((line) => padRight(fit(line, safeWidth), safeWidth));
}

function renderTerminalOutbox(value, selectedDeadLetterIndex, selectedAbandonReasonIndex) {
  const labels = {
    idle: "空闲",
    recovering: "正在恢复",
    backoff: "等待重试",
    degraded: "部分失败",
    disabled: "已关闭",
    unavailable: "状态不可用",
  };
  const workerLabels = {
    running: "运行中",
    waiting: "等待中",
    stopping: "正在停止",
    stopped: "已停止",
    disabled: "已关闭",
    unavailable: "不可用",
  };
  const style = value.status === "idle"
    ? ANSI.green
    : value.status === "recovering"
      ? ANSI.cyan
      : value.status === "backoff" || value.status === "disabled"
        ? ANSI.yellow
        : ANSI.red;
  const counts = value.counts;
  const lines = [
    color(ANSI.cyan, "── 终态自动恢复"),
    color(
      style,
      `${labels[value.status] || value.status} · Worker ${workerLabels[value.worker_state] || value.worker_state}`,
    ),
    color(
      ANSI.dim,
      `队列 ${counts.total_pending} · 到期 ${counts.due} · 退避 ${counts.backoff} · 认领 ${counts.live_claimed} · 过期认领 ${counts.expired_claimed} · 死信 ${counts.dead_letter || 0}`,
    ),
    color(
      ANSI.dim,
      `累计 · 轮次 ${value.pass_count} · 已收口 ${value.delivered_count} · 已退避 ${value.retry_scheduled_count} · 死信 ${value.dead_lettered_count || 0} · 失败 ${value.failure_count}`,
    ),
  ];
  if (value.enabled && ["running", "waiting"].includes(value.worker_state)) {
    lines.push(color(ANSI.dim, `下次检查 · 约 ${value.next_delay_seconds.toFixed(1)}s`));
  }
  if (value.failure_codes?.length) {
    lines.push(color(ANSI.red, `最近失败 · ${value.failure_codes.join(", ")}`));
  }
  for (const [index, item] of (value.dead_letters || []).entries()) {
    const disposition = item.disposition === "retry_exhausted"
      ? "重试预算耗尽"
      : "机械不变量破坏";
    lines.push(color(
      index === selectedDeadLetterIndex ? ANSI.yellow : ANSI.red,
      `${index === selectedDeadLetterIndex ? "▶" : " "} 死信 ${item.dead_letter_id} · ${disposition} · ${item.failure_code} · 失败 ${item.failure_attempts} 次 / 总认领 ${item.total_claim_attempts} 次 · ${item.occurred_at}`,
    ));
    if (index === selectedDeadLetterIndex) {
      const reasons = [
        ["no_longer_required", "不再需要"],
        ["superseded", "已被替代"],
        ["external_resolution", "外部已解决"],
        ["invalid_target", "目标无效"],
      ];
      const [reason, reasonLabel] = reasons[selectedAbandonReasonIndex % reasons.length];
      lines.push(color(
        ANSI.dim,
        `  重入队命令 · /pursue outbox requeue ${item.dead_letter_id}`,
      ));
      lines.push(color(
        ANSI.yellow,
        `  放弃原因 · ${reasonLabel} (${reason}) · a 切换 · z 执行`,
      ));
      lines.push(color(
        ANSI.dim,
        `  放弃命令 · /pursue outbox abandon ${item.dead_letter_id} ${reason}`,
      ));
    }
  }
  if (value.dead_letters_truncated) {
    lines.push(color(ANSI.yellow, "死信目录已按当前视图上限截断。"));
  }
  if (value.disposed_count > 0) {
    lines.push(color(ANSI.cyan, `── 已处置历史 · ${value.disposed_count}`));
    const reasons = {
      no_longer_required: "不再需要",
      superseded: "已被替代",
      external_resolution: "外部已解决",
      invalid_target: "目标无效",
    };
    for (const item of value.disposed || []) {
      lines.push(color(
        ANSI.green,
        `✓ 已放弃 ${item.dead_letter_id} · ${reasons[item.reason] || item.reason} · ${item.failure_code}`,
      ));
      lines.push(color(
        ANSI.dim,
        `  effective-state ${item.effective_state} · failure seq ${item.failure_sequence} · 回执 ${item.receipt_id} · ${item.abandoned_at}`,
      ));
    }
  }
  if (value.disposed_truncated) {
    lines.push(color(ANSI.yellow, "已处置历史当前只显示一页。"));
  }
  if (value.disposed_has_more) {
    lines.push(color(ANSI.cyan, "按 } 查看更早的已处置历史。"));
  }
  if (value.disposed_cursor) {
    lines.push(color(ANSI.dim, "当前为历史后续页；按 { 返回上一页。"));
  }
  if (value.disposed_warning) {
    lines.push(color(ANSI.yellow, `⚠ ${compactText(value.disposed_warning, 500)}`));
  }
  if (value.warning) {
    lines.push(color(ANSI.yellow, `⚠ ${compactText(value.warning, 500)}`));
  }
  return lines;
}

function renderGoal(goal, currentGoalId) {
  const current = goal.goal_id === currentGoalId;
  const lines = [
    color(
      current ? ANSI.cyan : ANSI.dim,
      `── 目标详情 · ${goal.goal_id}${current ? " · 当前" : ""}`,
    ),
    color(goalColor(goal.status), `${goalLabel(goal.status)} · ${compactText(goal.objective, 4_000)}`),
    color(
      ANSI.dim,
      `会话 ${goal.session_id || "未绑定"} · 更新 ${goal.updated_at || "-"}`,
    ),
  ];
  if (goal.note) lines.push(color(ANSI.dim, `说明 · ${compactText(goal.note, 2_000)}`));
  if (goal.status === "active") {
    lines.push(color(ANSI.yellow, "可用操作 · m 暂停 · fallback /goal pause"));
  } else if (goal.status === "paused") {
    lines.push(color(ANSI.green, "可用操作 · m 恢复 · fallback /goal resume"));
  } else {
    lines.push(color(ANSI.dim, "页内可逆操作 · 当前状态不可用"));
  }
  if (goal.pursuit) {
    lines.push(...renderPursuit(goal.pursuit));
  } else if (goal.pursuit_link_status === "missing") {
    lines.push(color(ANSI.red, `Pursuit ${goal.pursuit_run_id} · 追踪记录不可用`));
  } else {
    lines.push(color(ANSI.dim, "Pursuit · 未启动"));
  }
  return lines;
}

function renderGoalDirectory(goals, currentGoalId, selectedGoalId) {
  const lines = [color(ANSI.cyan, `── 目标目录 · ${goals.length} 项`)];
  for (const goal of goals) {
    const selected = goal.goal_id === selectedGoalId;
    const current = goal.goal_id === currentGoalId;
    lines.push(color(
      selected ? ANSI.cyan : goalColor(goal.status),
      `${selected ? "▶" : " "} ${goal.goal_id} · ${goalLabel(goal.status)}${current ? " · 当前" : ""} · ${compactText(goal.objective, 300)}`,
    ));
  }
  return lines;
}

function renderInteractionLedger(snapshot, selectedIndex) {
  const interactions = snapshot.interactions ?? [];
  if (!interactions.length) return [];
  const filterLabel = {
    all: "全部", pending: "等待回答", answered: "已回答",
    expired: "已超时", cancelled: "已取消",
  }[snapshot.interaction_filter] || snapshot.interaction_filter || "全部";
  const page = snapshot.interaction_cursor ? "后续页" : "第 1 页";
  const lines = [
    color(
      ANSI.cyan,
      `── 用户交互账本 · ${filterLabel} · ${page} · ${interactions.length} 项`,
    ),
  ];
  for (const [index, item] of interactions.entries()) {
    const style = item.state === "pending"
      ? ANSI.yellow
      : item.state === "answered"
        ? ANSI.green
        : ANSI.dim;
    const label = {
      pending: "等待回答", answered: "已回答", expired: "已超时", cancelled: "已取消",
    }[item.state] || item.state;
    const marker = index === selectedIndex ? "›" : " ";
    const priority = { critical: "紧急", high: "重要", normal: "常规", low: "可延后" }[item.priority] || "常规";
    const row = `${marker} ${item.interaction_id} · ${label} · ${priority} · ${compactText(item.header, 40)} · ${compactText(item.question, 2_000)}`;
    lines.push(color(index === selectedIndex ? ANSI.cyan : style, row));
    lines.push(color(ANSI.dim, `    Pursuit · ${item.pursuit_run_id}`));
    lines.push(color(ANSI.dim, `    详情 · /goal interaction detail ${item.interaction_id}`));
    if (item.can_cancel) {
      lines.push(color(ANSI.dim, `    取消 · /goal interaction cancel ${item.interaction_id}`));
    }
    if (item.can_takeover) {
      lines.push(color(ANSI.cyan, `    接管 · /goal interaction takeover ${item.interaction_id}`));
    }
  }
  lines.push(color(
    ANSI.dim,
    `${snapshot.interaction_cursor ? "p 上一页" : "p 上一页（不可用）"} · ${
      snapshot.interaction_has_more ? "n 下一页" : "n 下一页（已到底）"
    }`,
  ));
  return lines;
}

function renderInteractionDetail(item) {
  const lines = [
    color(ANSI.cyan, `── 交互详情 · ${item.interaction_id}`),
    color(ANSI.dim, `优先级 · ${{ critical: "紧急", high: "重要", normal: "常规", low: "可延后" }[item.priority] || "常规"}`),
    color(
      item.state === "pending" ? ANSI.yellow : item.state === "answered" ? ANSI.green : ANSI.dim,
      `${item.state} · ${compactText(item.header, 40)} · ${compactText(item.question, 2_000)}`,
    ),
  ];
  for (const [index, option] of (item.options ?? []).entries()) {
    const description = option.description ? ` · ${compactText(option.description, 300)}` : "";
    lines.push(`  ${index + 1}. ${option.label} (${option.value})${description}`);
  }
  if (item.allow_custom) {
    lines.push(color(ANSI.dim, `  自定义 · ${item.custom_label || "自定义回答"}`));
  }
  if (item.state === "answered") {
    const answer = item.answer_kind === "custom"
      ? item.custom_text
      : `${item.answer_label} (${item.answer_value})`;
    lines.push(color(ANSI.green, `回答 · ${compactText(answer || "-", 4_000)}`));
  } else {
    lines.push(color(ANSI.dim, item.state === "pending" ? "回答 · 尚未提交" : "回答 · 无"));
  }
  lines.push(
    color(ANSI.dim, `Fencing · sequence ${item.sequence} · owner epoch ${item.owner_epoch}`),
    color(
      item.question_expired ? ANSI.red : item.lease_expired ? ANSI.yellow : ANSI.dim,
      `期限 · ${item.question_expired ? "问题已到期" : "问题有效"} · ${
        item.lease_expired ? "owner 租约已过期" : "owner 租约生效"
      }`,
    ),
    color(ANSI.dim, "Esc 关闭详情"),
  );
  return lines;
}

function renderPursuit(run) {
  const lines = [
    color(
      pursuitColor(run.status),
      `Pursuit ${run.run_id} · ${pursuitLabel(run.status)} · ${run.phase || "-"}`,
    ),
    `成功标准 ${run.criteria_verified}/${run.criteria_total} ${progressBar(run.criteria_verified, run.criteria_total)} · 轮次 ${run.iteration} · 失败 ${run.failure_count}`,
    `下一步 · ${compactText(run.next_action || "暂无", 2_000)}`,
  ];
  if (run.blocked_reason) {
    lines.push(color(ANSI.red, `阻塞 · ${compactText(run.blocked_reason, 2_000)}`));
  }
  if (run.boundary_decision) {
    const decision = run.boundary_decision;
    lines.push(
      color(
        pursuitColor(decision.status),
        `最近裁判 · ${decision.code} · ${decision.status} · ${decision.decision_id.slice(0, 12)}`,
      ),
      color(ANSI.dim, `  ${compactText(decision.reason, 300)}`),
    );
  }
  if (run.recovery) {
    lines.push(...renderRecovery(run.recovery));
  }
  lines.push(color(run.waits?.length ? ANSI.yellow : ANSI.dim, `等待任务 · ${run.waits?.length || 0}`));
  for (const wait of run.waits || []) {
    lines.push(color(
      ANSI.dim,
      `  ${wait.task_id} · ${wait.action_id || "无 action"} · ${compactText(wait.command, 2_000)} · ${wait.created_at || "-"}`,
    ));
  }
  lines.push(color(run.evidence?.length ? ANSI.cyan : ANSI.dim, `最近证据 · ${run.evidence?.length || 0} / 20（快照上限）`));
  for (const evidence of run.evidence || []) {
    const style = evidence.is_hard ? ANSI.green : ANSI.dim;
    lines.push(color(
      style,
      `  ${evidence.is_hard ? "强证据" : "辅助证据"} · ${evidence.kind} · ${evidence.timestamp || "-"} · ${evidence.source} · ${compactText(evidence.summary, 1_000)}`,
    ));
  }
  return lines;
}

function renderRecovery(recovery) {
  const style = recoveryColor(recovery.recovery_state);
  const lines = [
    color(
      style,
      `恢复健康 · ${recoveryLabel(recovery.recovery_state)} · 心跳 ${heartbeatLabel(recovery.heartbeat.health)} · 租约 ${leaseLabel(recovery.lease.status)}`,
    ),
    color(
      ANSI.dim,
      `Worker · ${recovery.heartbeat.instance_id || "未知"} · seq ${recovery.heartbeat.sequence} · age ${recovery.heartbeat.age_seconds}s`,
    ),
    color(
      ANSI.dim,
      `Lease · ${recovery.lease.owner_id || "无 owner"} · epoch ${recovery.lease.epoch} · ${recovery.lease.expired ? "已过期" : "未过期"}`,
    ),
    color(
      ANSI.dim,
      `Checkpoint · ${checkpointLabel(recovery.checkpoint.status)} · seq ${recovery.checkpoint.sequence} · ${recovery.checkpoint.phase || "-"}`,
    ),
  ];
  if (recovery.reconcile_required) {
    lines.push(color(ANSI.red, `Reconcile · ${recovery.reconcile_reason || "需要人工核对"}`));
  }
  for (const alert of recovery.alerts || []) {
    lines.push(color(ANSI.yellow, `恢复提醒 · ${compactText(alert, 500)}`));
  }
  if (recovery.resume_action) {
    const action = recovery.resume_action;
    const actionStyle = action.state === "available"
      ? ANSI.cyan
      : action.state === "busy"
        ? ANSI.yellow
        : action.state === "blocked"
          ? ANSI.red
          : ANSI.dim;
    lines.push(
      color(
        actionStyle,
        `恢复动作 · ${recoveryActionLabel(action.state)} · ${action.code} · ${compactText(action.reason, 300)}`,
      ),
      color(ANSI.dim, `  ${action.command}`),
    );
  }
  if (recovery.attempts?.length) {
    lines.push(color(ANSI.cyan, `恢复请求 · 最近 ${recovery.attempts.length} 项`));
    for (const attempt of recovery.attempts) {
      const attemptStyle = attempt.state === "resolved"
        ? ANSI.green
        : attempt.state === "failed"
          ? ANSI.red
          : ANSI.yellow;
      const result = attempt.result_code ? ` · ${attempt.result_code}` : "";
      lines.push(color(
        attemptStyle,
        `  ${attempt.attempt_id.slice(0, 20)}… · ${recoveryAttemptLabel(attempt.state)}${result} · ${attempt.updated_at}`,
      ));
      if (attempt.state === "admitted") {
        lines.push(color(
          ANSI.cyan,
          `    对账 · /pursue reconcile ${attempt.attempt_id}`,
        ));
      }
    }
  }
  return lines;
}

function recoveryActionLabel(state) {
  return {
    available: "可恢复",
    busy: "处理中",
    blocked: "已阻止",
    unavailable: "不适用",
  }[state] || state;
}

function recoveryAttemptLabel(state) {
  return {
    requested: "已记录",
    admitted: "已准入",
    resolved: "已完成",
    failed: "失败关闭",
  }[state] || state;
}

function recoveryColor(state) {
  if (["active", "terminal"].includes(state)) return ANSI.green;
  if (["orphaned", "inconsistent", "reconcile_required"].includes(state)) return ANSI.red;
  if (["waiting", "blocked"].includes(state)) return ANSI.yellow;
  return ANSI.dim;
}

function recoveryLabel(state) {
  return {
    active: "运行健康", waiting: "安全等待", blocked: "已阻塞",
    reconcile_required: "需要核对", orphaned: "疑似孤立",
    inconsistent: "状态不一致", terminal: "已终止", unknown: "未知",
  }[state] || state;
}

function heartbeatLabel(value) {
  return {
    starting: "启动中", healthy: "健康", draining: "排空中", stale: "陈旧",
    offline: "离线", stopped: "已停止", failed: "失败",
    clock_regression: "时钟倒退", missing: "缺失", error: "读取失败",
  }[value] || value;
}

function leaseLabel(value) {
  return { active: "生效", released: "已释放", missing: "缺失", error: "读取失败" }[value] || value;
}

function checkpointLabel(value) {
  return { ready: "可用", missing: "缺失", error: "校验失败" }[value] || value;
}

function progressBar(verified, total) {
  const size = 10;
  const ratio = total > 0 ? Math.min(1, Math.max(0, verified / total)) : 0;
  const filled = Math.round(ratio * size);
  return color(ANSI.cyan, `[${"█".repeat(filled)}${"░".repeat(size - filled)}]`);
}

function goalLabel(status) {
  return {
    active: "进行中",
    paused: "已暂停",
    blocked: "已阻塞",
    completed: "已完成",
    cancelled: "已取消",
  }[status] || status;
}

function goalColor(status) {
  if (status === "active" || status === "completed") return ANSI.green;
  if (status === "blocked") return ANSI.red;
  if (status === "paused") return ANSI.yellow;
  return ANSI.dim;
}

function pursuitLabel(status) {
  return {
    running: "运行中",
    waiting: "等待中",
    blocked: "已阻塞",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
    budget_exceeded: "预算耗尽",
  }[status] || status;
}

function pursuitColor(status) {
  if (status === "running" || status === "completed") return ANSI.green;
  if (["blocked", "failed"].includes(status)) return ANSI.red;
  if (["waiting", "budget_exceeded"].includes(status)) return ANSI.yellow;
  return ANSI.dim;
}

function fit(line, width) {
  if (visibleWidth(line) <= width) return line;
  return wrapAnsiLine(line, width)[0] ?? "";
}
