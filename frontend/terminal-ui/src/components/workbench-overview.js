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

export function renderWorkbenchOverview(view, width, height) {
  const safeWidth = Math.max(1, Number(width) || 1);
  const safeHeight = Math.max(1, Number(height) || 1);
  const snapshot = view && typeof view === "object" ? view : {};
  const header = [
    fitAnsiWidth(renderTabs(snapshot), safeWidth),
    fitAnsiWidth(renderSummary(snapshot), safeWidth),
    fitAnsiWidth(renderPageState(snapshot), safeWidth),
  ];
  const bodyHeight = Math.max(0, safeHeight - header.length);
  let body;
  if (snapshot.error && Number(snapshot.revision) < 1) {
    body = [color(ANSI.red, `加载失败 · ${compactText(snapshot.error, 500)}`)];
  } else if (snapshot.loading && Number(snapshot.revision) < 1) {
    body = [color(ANSI.cyan, "正在加载 Workbench 权威快照…")];
  } else if (snapshot.selected_tab === "worktrees") {
    body = renderWorktrees(snapshot, safeWidth, bodyHeight);
  } else if (snapshot.selected_tab === "reviews") {
    body = renderReviews(snapshot, safeWidth, bodyHeight);
  } else if (!array(snapshot.missions).length && !array(snapshot.tasks).length) {
    body = [
      color(ANSI.dim, "暂无 Workbench 任务。"),
      color(ANSI.dim, "可先使用 /task 创建任务，或按 r 刷新当前会话。"),
    ];
  } else if (safeWidth >= 120) {
    body = renderWideBody(snapshot, safeWidth, bodyHeight);
  } else {
    body = renderNarrowBody(snapshot, safeWidth, bodyHeight);
  }
  const lines = [...header, ...body.slice(0, bodyHeight)];
  while (lines.length < safeHeight) lines.push("");
  return lines.slice(0, safeHeight).map((line) => (
    padRight(fitAnsiWidth(line, safeWidth), safeWidth)
  ));
}

function renderTabs(snapshot) {
  const selected = ["overview", "worktrees", "reviews"].includes(snapshot.selected_tab)
    ? snapshot.selected_tab
    : "overview";
  const overview = selected === "overview" ? color(ANSI.cyan, "[1 概览]") : color(ANSI.dim, "1 概览");
  const worktrees = selected === "worktrees" ? color(ANSI.cyan, "[2 Worktrees]") : color(ANSI.dim, "2 Worktrees");
  const reviews = selected === "reviews" ? color(ANSI.cyan, "[3 Reviews]") : color(ANSI.dim, "3 Reviews");
  return `Workbench Overview · ${overview} · ${worktrees} · ${reviews}`;
}

function renderSummary(snapshot) {
  const counts = snapshot.counts || {};
  return [
    `rev ${number(snapshot.revision)}`,
    `任务 ${number(counts.tasks)}`,
    `worktree ${number(counts.worktrees)}`,
    `待审 ${number(counts.reviews)}`,
    `失败 ${number(counts.failures)}`,
    snapshot.generated_at ? `更新 ${compactText(snapshot.generated_at, 40)}` : "",
  ].filter(Boolean).join(" · ");
}

function renderPageState(snapshot) {
  if (snapshot.error) {
    return color(ANSI.yellow, `刷新警告 · ${compactText(snapshot.error, 300)} · r 重试 · Esc 返回`);
  }
  if (snapshot.loading) return color(ANSI.cyan, "状态 · 正在刷新 · r 重试 · Esc 返回");
  if (["worktrees", "reviews"].includes(snapshot.selected_tab)) {
    return color(ANSI.dim, "Tab/Shift+Tab 标签 · ↑/↓ 选择 · PgUp/PgDn 翻页 · r 刷新 · Esc 返回");
  }
  return color(ANSI.dim, "Tab/Shift+Tab 标签 · r 刷新 · Esc 返回对话");
}

function renderReviews(snapshot, width, height) {
  const reviews = array(snapshot.approvals);
  if (!reviews.length) {
    return [
      color(ANSI.green, "当前没有待审请求。"),
      color(ANSI.dim, "Review 只读取权威审批与证据；审批动作将在 UI-10.6 接入。"),
    ];
  }
  const selectedIndex = selectedReviewIndex(snapshot, reviews);
  const selected = reviews[selectedIndex];
  if (width >= 120) {
    const leftWidth = Math.max(44, Math.min(Math.floor(width * 0.42), width - 55));
    const rightWidth = Math.max(1, width - leftWidth - 1);
    const left = renderReviewList(reviews, selectedIndex, leftWidth, height);
    const right = renderReviewDetail(snapshot, selected, rightWidth);
    return Array.from({ length: height }, (_, index) => {
      const leftLine = padRight(fitAnsiWidth(left[index] || "", leftWidth), leftWidth);
      const rightLine = padRight(fitAnsiWidth(right[index] || "", rightWidth), rightWidth);
      return `${leftLine}${color(ANSI.blue, "│")}${rightLine}`;
    });
  }
  const listHeight = Math.max(4, Math.min(7, Math.floor(height * 0.38)));
  return [
    ...renderReviewList(reviews, selectedIndex, width, listHeight),
    ...renderReviewDetail(snapshot, selected, width),
  ].slice(0, height);
}

function renderReviewList(reviews, selectedIndex, width, height) {
  const rowCount = Math.max(1, height - 1);
  const start = Math.max(
    0,
    Math.min(reviews.length - rowCount, selectedIndex - Math.floor(rowCount / 2)),
  );
  const rows = reviews.slice(start, start + rowCount).map((item, offset) => {
    const index = start + offset;
    const marker = index === selectedIndex ? color(ANSI.cyan, "›") : " ";
    const title = compactText(item.title || item.id || "未命名审查", 500);
    const requester = compactText(item.requester || "未知发起者", 160);
    return fitAnsiWidth(`${marker} ${color(ANSI.yellow, "待审")} ${title} · ${requester}`, width);
  });
  return [color(ANSI.cyan, `Reviews · ${reviews.length} · 当前 ${selectedIndex + 1}`), ...rows];
}

function renderReviewDetail(snapshot, selected, width) {
  if (!selected) return [color(ANSI.dim, "未选择审查请求")];
  if (snapshot.review_error) {
    return [
      color(ANSI.red, `证据不可用 · ${compactText(snapshot.review_error, 500)}`),
      color(ANSI.dim, "按 r 重新读取当前会话权威证据。"),
    ];
  }
  const detail = snapshot.review_detail;
  if (snapshot.review_loading || !detail || String(detail.review_id || "") !== String(selected.id || "")) {
    return [
      color(ANSI.cyan, `审查 · ${compactText(selected.title || selected.id, 500)}`),
      color(ANSI.dim, "正在读取 diff、验证与阻塞证据…"),
    ];
  }
  if (detail.status !== "ready" || !detail.evidence) {
    return [color(ANSI.yellow, "该审查已不可用，请刷新 Review 列表。")];
  }
  const evidence = detail.evidence;
  const approval = evidence.approval || selected;
  const worktree = evidence.worktree || {};
  const runs = array(evidence.validation_runs);
  const files = array(evidence.changed_files);
  const hunks = array(evidence.diff_hunks);
  const failed = runs.filter((run) => ["failed", "error"].includes(String(run.status)));
  const passed = runs.filter((run) => ["passed", "success", "completed"].includes(String(run.status)));
  const lines = [
    color(ANSI.cyan, `审查 · ${compactText(approval.title || approval.id, 600)}`),
    `发起者 · ${compactText(approval.requester || "未知", 200)} · Task ${compactText(approval.task_id || "-", 120)}`,
    `说明 · ${compactText(approval.detail || "未填写", 1_500)}`,
    reviewGateLine(worktree, runs, failed),
    `${worktreeLabel(worktree.status)} · ${compactText(worktree.name || "未绑定", 300)}`,
    `${passed.length ? color(ANSI.green, `通过 ${passed.length}`) : color(ANSI.yellow, "通过 0")} · ${failed.length ? color(ANSI.red, `失败 ${failed.length}`) : color(ANSI.green, "失败 0")} · 变更 ${files.length}`,
  ];
  if (!runs.length) lines.push(color(ANSI.yellow, "验证 · 尚未记录真实验证命令"));
  else {
    const latest = runs.at(-1);
    const command = Array.isArray(latest.command) ? latest.command.join(" ") : String(latest.command || "-");
    lines.push(`${validationStatus(latest.status)} · ${compactText(command, 900)} · exit ${latest.exit_code ?? "-"}`);
  }
  if (hunks.length) {
    lines.push(color(ANSI.cyan, `Diff · ${compactText(hunks[0].path || "-", 500)}`));
    for (const line of String(hunks[0].patch || "").split("\n").slice(0, 12)) {
      lines.push(renderDiffLine(line));
    }
    if (hunks.length > 1) lines.push(color(ANSI.dim, `另有 ${hunks.length - 1} 个 diff 文件`));
  } else {
    lines.push(color(ANSI.dim, "Diff · 当前没有可展示的已跟踪文件差异"));
  }
  if (files.length) {
    lines.push(color(ANSI.cyan, "文件"));
    for (const file of files.slice(0, 8)) lines.push(renderChangedFile(file));
    if (files.length > 8) lines.push(color(ANSI.dim, `另有 ${files.length - 8} 个文件`));
  } else {
    lines.push(color(ANSI.yellow, "文件 · 未检测到工作区变更"));
  }
  return lines.flatMap((line) => wrapAnsiLine(line, Math.max(1, width)));
}

function selectedReviewIndex(snapshot, reviews) {
  const byId = reviews.findIndex(
    (item) => String(item.id || "") === String(snapshot.selected_review_id || ""),
  );
  if (byId >= 0) return byId;
  const byAuthority = reviews.findIndex(
    (item) => String(item.id || "") === String(snapshot.active_selection?.review_id || ""),
  );
  return byAuthority >= 0 ? byAuthority : 0;
}

function reviewGateLine(worktree, runs, failed) {
  if (worktree.status !== "present") return color(ANSI.red, "阻塞 · 变更载体不可用");
  if (!runs.length) return color(ANSI.yellow, "待补证据 · 尚未运行验证");
  if (failed.length) return color(ANSI.red, `阻塞 · ${failed.length} 项验证失败`);
  return color(ANSI.green, "证据就绪 · 可进入人工判断");
}

function worktreeLabel(status) {
  if (status === "present") return color(ANSI.green, "Worktree 可用");
  if (status === "missing") return color(ANSI.red, "Worktree 缺失");
  return color(ANSI.yellow, "Worktree 未绑定");
}

function renderChangedFile(file) {
  const status = String(file.status || "modified");
  const label = {
    added: color(ANSI.green, "+ 新增"),
    deleted: color(ANSI.red, "- 删除"),
    modified: color(ANSI.yellow, "~ 修改"),
    renamed: color(ANSI.cyan, "→ 重命名"),
    untracked: color(ANSI.green, "+ 未跟踪"),
  }[status] || color(ANSI.dim, status);
  return `${label} · ${compactText(file.path || "-", 900)}`;
}

function renderDiffLine(line) {
  if (line.startsWith("+") && !line.startsWith("+++")) return color(ANSI.green, line);
  if (line.startsWith("-") && !line.startsWith("---")) return color(ANSI.red, line);
  if (line.startsWith("@@")) return color(ANSI.cyan, line);
  if (line.startsWith("diff ") || line.startsWith("index ")) return color(ANSI.dim, line);
  return line;
}

function renderWorktrees(snapshot, width, height) {
  if (snapshot.worktrees_status !== "ready") {
    return [
      color(ANSI.yellow, "Worktree 状态暂不可用。"),
      color(ANSI.dim, "Overview 仍可使用；按 r 重试，或运行 /doctor 检查环境。"),
    ];
  }
  const worktrees = array(snapshot.worktrees);
  if (!worktrees.length) {
    return [
      color(ANSI.dim, "暂无由 NaumiAgent 管理的 worktree。"),
      color(ANSI.dim, "可通过 /worktree create 创建隔离执行区。"),
    ];
  }
  const selectedIndex = selectedWorktreeIndex(snapshot, worktrees);
  const selected = worktrees[selectedIndex];
  if (width >= 120) {
    const leftWidth = Math.max(48, Math.min(Math.floor(width * 0.52), width - 43));
    const rightWidth = Math.max(1, width - leftWidth - 1);
    const left = renderWorktreeList(snapshot, worktrees, selectedIndex, leftWidth, height);
    const right = renderWorktreeDetail(selected, rightWidth);
    return Array.from({ length: height }, (_, index) => {
      const leftLine = padRight(fitAnsiWidth(left[index] || "", leftWidth), leftWidth);
      const rightLine = padRight(fitAnsiWidth(right[index] || "", rightWidth), rightWidth);
      return `${leftLine}${color(ANSI.blue, "│")}${rightLine}`;
    });
  }
  const listHeight = Math.max(4, Math.min(7, Math.floor(height * 0.42)));
  return [
    ...renderWorktreeList(snapshot, worktrees, selectedIndex, width, listHeight),
    ...renderWorktreeDetail(selected, width),
  ].slice(0, height);
}

function renderWorktreeList(snapshot, worktrees, selectedIndex, width, height) {
  const total = Math.max(worktrees.length, number(snapshot.worktrees_total));
  const heading = `Worktrees · ${worktrees.length}/${total} · 当前 ${selectedIndex + 1}`;
  const rowCount = Math.max(1, height - 1);
  const start = Math.max(
    0,
    Math.min(worktrees.length - rowCount, selectedIndex - Math.floor(rowCount / 2)),
  );
  const rows = worktrees.slice(start, start + rowCount).map((item, offset) => {
    const index = start + offset;
    const marker = index === selectedIndex ? color(ANSI.cyan, "›") : " ";
    const status = worktreeStatus(item.status);
    const task = compactText(item.task?.subject || item.task_id || "未绑定任务", 180);
    const agent = compactText(item.agent_id || "未占用", 120);
    return fitAnsiWidth(`${marker} ${status} ${compactText(item.name || "-", 180)} · ${task} · ${agent}`, width);
  });
  return [color(ANSI.cyan, heading), ...rows];
}

function renderWorktreeDetail(item, width) {
  if (!item) return [color(ANSI.dim, "未选择 worktree")];
  const lease = item.lease || {};
  const task = item.task || {};
  const safeRemoval = item.removable
    ? color(ANSI.green, "可安全移除")
    : color(ANSI.yellow, "不可安全移除");
  const lines = [
    color(ANSI.cyan, `详情 · ${compactText(item.name || "-", 300)}`),
    `${worktreeStatus(item.status)} · 未提交 ${number(item.dirty_files)} · 领先 ${number(item.commits_ahead)}`,
    safeRemoval,
    `路径 · ${compactText(item.path || "-", 1_500)}`,
    `分支 · ${compactText(item.branch || "-", 500)}`,
    `任务 · ${compactText(task.subject || item.task_id || "未绑定", 500)}`,
    `Agent · ${compactText(item.agent_id || "未占用", 200)}`,
    `Lease · ${compactText(lease.state || "无", 80)}${lease.expires_at ? ` · 到期 ${compactText(lease.expires_at, 100)}` : ""}`,
  ];
  if (item.kept_reason) lines.push(`保留原因 · ${compactText(item.kept_reason, 600)}`);
  return lines.flatMap((line) => wrapAnsiLine(line, Math.max(1, width)));
}

function selectedWorktreeIndex(snapshot, worktrees) {
  const byName = worktrees.findIndex(
    (item) => String(item.name || "") === String(snapshot.selected_worktree_name || ""),
  );
  if (byName >= 0) return byName;
  const byAuthority = worktrees.findIndex(
    (item) => String(item.name || "") === String(snapshot.active_selection?.worktree || ""),
  );
  return byAuthority >= 0 ? byAuthority : 0;
}

function worktreeStatus(status) {
  if (status === "clean") return color(ANSI.green, "干净");
  if (status === "dirty") return color(ANSI.yellow, "有变更");
  if (status === "missing") return color(ANSI.red, "目录缺失");
  if (status === "kept") return color(ANSI.cyan, "已保留");
  return color(ANSI.dim, "未知");
}

function renderWideBody(snapshot, width, height) {
  const leftWidth = Math.max(42, Math.min(Math.floor(width * 0.48), width - 43));
  const rightWidth = Math.max(1, width - leftWidth - 1);
  const left = [
    ...renderSection("当前目标", renderMission(snapshot), leftWidth),
    ...renderSection("当前任务", renderTask(snapshot), leftWidth),
  ];
  const right = [
    ...renderSection("变更载体", renderChangeCarrier(snapshot), rightWidth),
    ...renderSection("验证", renderValidation(snapshot), rightWidth),
    ...renderSection("风险与待审", renderRisk(snapshot), rightWidth),
  ];
  return Array.from({ length: height }, (_, index) => {
    const leftLine = padRight(fitAnsiWidth(left[index] || "", leftWidth), leftWidth);
    const rightLine = padRight(fitAnsiWidth(right[index] || "", rightWidth), rightWidth);
    return `${leftLine}${color(ANSI.blue, "│")}${rightLine}`;
  });
}

function renderNarrowBody(snapshot, width, height) {
  return [
    ...renderSection("当前目标", renderMission(snapshot), width),
    ...renderSection("当前任务", renderTask(snapshot), width),
    ...renderSection("变更载体", renderChangeCarrier(snapshot), width),
    ...renderSection("验证", renderValidation(snapshot), width),
    ...renderSection("风险与待审", renderRisk(snapshot), width),
  ].slice(0, height);
}

function renderSection(title, lines, width) {
  const heading = color(ANSI.cyan, title);
  return [heading, ...lines].flatMap((line) => (
    wrapAnsiLine(line, Math.max(1, width)).map((wrapped) => fitAnsiWidth(wrapped, width))
  ));
}

function renderMission(snapshot) {
  const selection = snapshot.active_selection || {};
  const missions = array(snapshot.missions);
  const mission = missions.find((item) => String(item.id) === String(selection.mission_id))
    || missions.find((item) => ["active", "planning"].includes(String(item.status)))
    || missions[0];
  if (!mission) return [color(ANSI.dim, "尚未设置目标")];
  return [
    `${missionStatus(mission.status)} ${compactText(mission.title || mission.id, 500)}`,
    `目标 · ${compactText(mission.goal || "未填写", 800)}`,
    color(ANSI.dim, `Mission · ${compactText(mission.id || "-", 120)}`),
  ];
}

function renderTask(snapshot) {
  const { task, issue, lease } = activeRecords(snapshot);
  if (!task) return [color(ANSI.dim, "当前目标下暂无任务")];
  const owner = task.owner || lease?.agent_id || "未分配";
  return [
    `${taskStatus(task.status)} ${compactText(task.subject || task.id, 500)}`,
    `说明 · ${compactText(task.description || task.active_form || "未填写", 800)}`,
    `Owner · ${compactText(owner, 160)} · Risk ${riskLabel(issue?.risk_level)}`,
    array(task.blocked_by).length
      ? color(ANSI.red, `阻塞于 · ${compactText(array(task.blocked_by).join(", "), 500)}`)
      : color(ANSI.dim, `Task · ${compactText(task.id || "-", 120)}`),
  ];
}

function renderChangeCarrier(snapshot) {
  const { issue, lease } = activeRecords(snapshot);
  if (!issue && !lease) return [color(ANSI.dim, "尚未绑定分支或 worktree")];
  const artifacts = array(issue?.expected_artifacts);
  return [
    `分支 · ${compactText(issue?.related_branch || "尚未绑定", 300)}`,
    `Worktree · ${compactText(issue?.related_worktree || lease?.worktree_name || "尚未绑定", 300)}`,
    `PR · ${compactText(issue?.related_pr || "尚未绑定", 200)}`,
    artifacts.length
      ? `产物 · ${compactText(artifacts.slice(0, 3).join(", "), 500)}${artifacts.length > 3 ? ` · 另 ${artifacts.length - 3} 项` : ""}`
      : color(ANSI.dim, "产物 · 尚未登记"),
  ];
}

function renderValidation(snapshot) {
  const { task } = activeRecords(snapshot);
  const runs = array(snapshot.validation_runs)
    .filter((run) => !task || String(run.task_id) === String(task.id));
  const run = runs.at(-1);
  if (!run) return [color(ANSI.yellow, "尚未记录验证")];
  const command = Array.isArray(run.command) ? run.command.join(" ") : String(run.command || "-");
  const duration = durationMs(run.started_at, run.completed_at);
  return [
    `${validationStatus(run.status)} · 退出码 ${run.exit_code ?? "-"}${duration ? ` · ${duration}ms` : ""}`,
    `命令 · ${compactText(command, 600)}`,
  ];
}

function renderRisk(snapshot) {
  const { task, issue } = activeRecords(snapshot);
  const failures = array(snapshot.failures)
    .filter((item) => !task || String(item.task_id) === String(task.id));
  const approvals = array(snapshot.approvals)
    .filter((item) => !task || String(item.task_id) === String(task.id));
  const lines = [riskLine(issue?.risk_level)];
  if (failures.length) {
    lines.push(color(ANSI.red, `失败 · ${compactText(failures[0].title || failures[0].kind, 500)}`));
    if (failures.length > 1) lines.push(color(ANSI.red, `另有 ${failures.length - 1} 项失败`));
  } else {
    lines.push(color(ANSI.green, "失败 · 当前任务无已登记失败"));
  }
  if (approvals.length) {
    lines.push(color(ANSI.yellow, `待审 · ${compactText(approvals[0].title || approvals[0].id, 500)}`));
    if (approvals.length > 1) lines.push(color(ANSI.yellow, `另有 ${approvals.length - 1} 项待审`));
  } else {
    lines.push(color(ANSI.dim, "待审 · 无"));
  }
  return lines;
}

function activeRecords(snapshot) {
  const selection = snapshot.active_selection || {};
  const tasks = array(snapshot.tasks);
  const task = tasks.find((item) => String(item.id) === String(selection.task_id))
    || tasks.find((item) => String(item.status) === "in_progress")
    || tasks[0];
  const taskId = String(task?.id || "");
  const issue = array(snapshot.issues).find((item) => String(item.task_id) === taskId);
  const lease = array(snapshot.leases).find((item) => String(item.task_id) === taskId);
  return { task, issue, lease };
}

function missionStatus(status) {
  if (status === "completed") return color(ANSI.green, "已完成");
  if (["blocked", "cancelled"].includes(status)) return color(ANSI.red, "已阻塞");
  if (status === "active") return color(ANSI.cyan, "进行中");
  return color(ANSI.yellow, compactText(status || "规划中", 40));
}

function taskStatus(status) {
  if (status === "completed") return color(ANSI.green, "✓");
  if (status === "blocked") return color(ANSI.red, "!");
  if (status === "in_progress") return color(ANSI.cyan, "●");
  return color(ANSI.dim, "○");
}

function validationStatus(status) {
  if (["passed", "success", "completed"].includes(status)) return color(ANSI.green, "验证通过");
  if (["failed", "error"].includes(status)) return color(ANSI.red, "验证失败");
  return color(ANSI.yellow, `验证${compactText(status || "未知", 40)}`);
}

function riskLine(risk) {
  const label = riskLabel(risk);
  if (["high", "critical"].includes(risk)) return color(ANSI.red, `风险 · ${label}`);
  if (risk === "medium") return color(ANSI.yellow, `风险 · ${label}`);
  return color(ANSI.green, `风险 · ${label}`);
}

function riskLabel(risk) {
  return {
    critical: "严重风险",
    high: "高风险",
    medium: "中风险",
    low: "低风险",
  }[risk] || "未评级";
}

function durationMs(startedAt, completedAt) {
  const started = Date.parse(startedAt || "");
  const completed = Date.parse(completedAt || "");
  if (!Number.isFinite(started) || !Number.isFinite(completed) || completed < started) return 0;
  return completed - started;
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
