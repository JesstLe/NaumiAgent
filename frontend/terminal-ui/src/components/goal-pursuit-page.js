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
  const logical = [
    color(ANSI.cyan, "Goal / Pursuit"),
    color(ANSI.dim, "r 刷新 · ↑/↓ 滚动 · Esc 返回 · /goal interaction cancel <id> 取消等待"),
  ];
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
    for (const goal of snapshot.goals) {
      logical.push(...renderGoal(goal, snapshot.current_goal_id, snapshot.interactions ?? []));
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
  const wrapped = logical.flatMap((line) => wrapAnsiLine(line, safeWidth));
  const maximum = Math.max(0, wrapped.length - safeHeight);
  const offset = Math.min(Math.max(0, Number(value.scrollOffset) || 0), maximum);
  const lines = wrapped.slice(offset, offset + safeHeight);
  while (lines.length < safeHeight) lines.push("");
  return lines.map((line) => padRight(fit(line, safeWidth), safeWidth));
}

function renderGoal(goal, currentGoalId, interactions) {
  const current = goal.goal_id === currentGoalId;
  const lines = [
    color(
      current ? ANSI.cyan : ANSI.dim,
      `── ${current ? "当前目标" : "历史目标"} · ${goal.goal_id}`,
    ),
    color(goalColor(goal.status), `${goalLabel(goal.status)} · ${compactText(goal.objective, 4_000)}`),
    color(
      ANSI.dim,
      `会话 ${goal.session_id || "未绑定"} · 更新 ${goal.updated_at || "-"}`,
    ),
  ];
  if (goal.note) lines.push(color(ANSI.dim, `说明 · ${compactText(goal.note, 2_000)}`));
  if (goal.pursuit) {
    lines.push(...renderPursuit(goal.pursuit));
    lines.push(...renderInteractions(
      interactions.filter((item) => item.pursuit_run_id === goal.pursuit.run_id),
    ));
  } else if (goal.pursuit_link_status === "missing") {
    lines.push(color(ANSI.red, `Pursuit ${goal.pursuit_run_id} · 追踪记录不可用`));
  } else {
    lines.push(color(ANSI.dim, "Pursuit · 未启动"));
  }
  return lines;
}

function renderInteractions(interactions) {
  if (!interactions.length) return [];
  const lines = [color(ANSI.cyan, `用户交互 · ${interactions.length}`)];
  for (const item of interactions) {
    const style = item.state === "pending"
      ? ANSI.yellow
      : item.state === "answered"
        ? ANSI.green
        : ANSI.dim;
    const label = {
      pending: "等待回答", answered: "已回答", expired: "已超时", cancelled: "已取消",
    }[item.state] || item.state;
    lines.push(color(
      style,
      `  ${item.interaction_id} · ${label} · ${compactText(item.header, 40)} · ${compactText(item.question, 2_000)}`,
    ));
    if (item.can_cancel) {
      lines.push(color(ANSI.dim, `    取消 · /goal interaction cancel ${item.interaction_id}`));
    }
  }
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
  if (run.recovery) {
    lines.push(...renderRecovery(run.recovery));
  }
  if (run.waits?.length) {
    lines.push(color(ANSI.yellow, `等待任务 · ${run.waits.length}`));
    for (const wait of run.waits) {
      lines.push(color(ANSI.dim, `  ${wait.task_id} · ${compactText(wait.command, 2_000)}`));
    }
  }
  if (run.evidence?.length) {
    lines.push(color(ANSI.cyan, `最近证据 · ${run.evidence.length}`));
    for (const evidence of run.evidence.slice(-5)) {
      const style = evidence.is_hard ? ANSI.green : ANSI.dim;
      lines.push(color(style, `  ${evidence.kind} · ${evidence.source} · ${compactText(evidence.summary, 1_000)}`));
    }
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
  return lines;
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
