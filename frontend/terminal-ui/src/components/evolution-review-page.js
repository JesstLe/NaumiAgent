import { ANSI, color, compactText, padRight, visibleWidth, wrapAnsiLine } from "../ansi.js";

export function renderEvolutionReviewPage(view, width, height) {
  const safeWidth = Math.max(1, Number(width) || 1);
  const safeHeight = Math.max(1, Number(height) || 1);
  const value = view && typeof view === "object" ? view : {};
  const snapshot = value.snapshot && typeof value.snapshot === "object" ? value.snapshot : null;
  const logical = [
    color(ANSI.cyan, "Evolution Candidate 审阅"),
    color(ANSI.dim, snapshot?.mode === "detail" ? "b 返回列表 · r 刷新 · ↑/↓ 滚动 · Esc 返回对话" : "↑/↓ 选择 · Enter 详情 · r 刷新 · Esc 返回对话"),
  ];
  if (value.loading && !snapshot) logical.push(color(ANSI.cyan, "正在加载 Candidate 权威快照…"));
  else if (!snapshot) logical.push(color(ANSI.yellow, compactText(value.error || "Candidate 快照暂不可用。", 500)));
  else if (snapshot.mode === "detail") logical.push(...detailLines(snapshot.selected, snapshot.events));
  else logical.push(...listLines(
    snapshot.items,
    value.selectedIndex,
    snapshot.filters,
    snapshot.portfolio,
    safeHeight,
  ));
  const wrapped = logical.flatMap((line) => wrapAnsiLine(line, safeWidth));
  const offset = snapshot?.mode === "detail"
    ? Math.min(Math.max(0, Number(value.scrollOffset) || 0), Math.max(0, wrapped.length - 1))
    : 0;
  const lines = wrapped.slice(offset, offset + safeHeight);
  while (lines.length < safeHeight) lines.push("");
  return lines.slice(0, safeHeight).map((line) => padRight(fit(line, safeWidth), safeWidth));
}

function listLines(rawItems, selectedIndex, filters, portfolio, height) {
  const items = Array.isArray(rawItems) ? rawItems : [];
  const filterText = [filters?.query && `query=${filters.query}`, filters?.risk && `risk=${filters.risk}`, filters?.source_kind && `source=${filters.source_kind}`].filter(Boolean).join(" · ");
  const lines = [color(ANSI.dim, filterText || "过滤 · 无")];
  if (portfolio) {
    lines.push(
      color(ANSI.cyan, `全局 30d 优先级 · ${portfolio.ranked_count} 已排序 / ${portfolio.excluded_count} 未排序 · ${portfolio.clusters.length} 个机会簇`),
    );
    for (const cluster of portfolio.clusters.slice(0, 3)) {
      lines.push(color(
        ANSI.dim,
        compactText(`C${cluster.rank} ${cluster.domain} · ${cluster.score} · 候选 ${cluster.impact.candidate_count} · 来源 ${cluster.impact.source_kind_count} · Scope ${cluster.impact.scope_count}`, 1000),
      ));
    }
  }
  lines.push(`候选 · ${items.length} · 只读`);
  if (!items.length) {
    const next = portfolio?.considered_count
      ? "当前过滤条件下没有 Candidate。请调整 query/risk/source；全局 Portfolio 保持不变。"
      : "当前还没有 Candidate。可运行 /feedback 或 /self-review 产生真实证据。";
    return [...lines, color(ANSI.dim, next)];
  }
  const selected = Math.min(Math.max(0, Number(selectedIndex) || 0), items.length - 1);
  const pageSize = Math.max(1, Math.min(
    14,
    Math.floor((Math.max(1, Number(height) || 1) - 2 - lines.length) / 2),
  ));
  const start = Math.max(
    0,
    Math.min(selected - Math.floor(pageSize / 2), Math.max(0, items.length - pageSize)),
  );
  for (let index = start; index < Math.min(items.length, start + pageSize); index += 1) {
    const item = items[index];
    const marker = index === selected ? "›" : " ";
    const priority = item.priority?.rankable ? `P${item.priority.rank} ${item.priority.score}` : "未排序";
    const primary = `${marker} ${priority} · ${item.candidate_id} · ${item.finding_code} · ${item.risk} · ${decisionLabel(item.decision)}`;
    lines.push(color(index === selected ? ANSI.cyan : riskStyle(item.risk), compactText(primary, 1000)));
    lines.push(color(ANSI.dim, compactText(`  ${item.scope} · 证据 ${item.occurrence_count} · r${item.revision} · ${item.source_kinds.join(", ")}`, 1000)));
  }
  return lines;
}

function detailLines(item, rawEvents) {
  if (!item) return [color(ANSI.yellow, "Candidate 不存在，或不属于当前工作区。")];
  const events = Array.isArray(rawEvents) ? rawEvents : [];
  const lines = [
    color(riskStyle(item.risk), `${item.candidate_id} · ${item.finding_code}`),
    `${item.kind} / ${item.risk} · ${decisionLabel(item.decision)} · 实验资格 否`,
    `Scope · ${compactText(item.scope, 1000)}`,
    `证据 ${item.occurrence_count} · Revision ${item.revision} · 人工治理 ${item.human_review_required ? "必须" : "常规"}`,
    color(ANSI.dim, `来源 · ${item.source_kinds.join(", ") || "-"}`),
    color(ANSI.dim, `Provider/Model/Platform · ${item.providers.join(", ") || "-"} / ${item.models.join(", ") || "-"} / ${item.platforms.join(", ") || "-"}`),
  ];
  if (item.governance) {
    const governance = item.governance;
    lines.push(
      color(ANSI.cyan, `── Workbench 治理 · ${governance.policy_version}`),
      color(governance.allowed ? ANSI.green : ANSI.yellow, `${governance.allowed ? "可重新审阅" : "冷却阻断"} · ${governance.reason}`),
      color(ANSI.dim, `最近 Proposal · ${governance.proposal_state || "-"} / r${governance.proposal_revision || "-"} · 冷却截止 ${governance.cooldown_until || "-"}`),
    );
  }
  if (item.priority) {
    const priority = item.priority;
    lines.push(
      color(ANSI.cyan, `── 可解释优先级 · ${priority.policy_version}`),
      color(priority.rankable ? ANSI.green : ANSI.yellow, priority.rankable ? `P${priority.rank} · 分数 ${priority.score} · 机会域 ${priority.domain}` : `未参与排序 · 机会域 ${priority.domain}`),
      color(ANSI.dim, `严重度 ${priority.severity} × 频次 ${priority.frequency} × 置信度 ${priority.confidence}% ÷ 实现成本 ${priority.implementation_cost} ÷ 变更风险 ${priority.change_risk}`),
      color(ANSI.dim, `有效观测 ${priority.qualifying_observations} · 权威通道 ${priority.confidence_lanes.join(", ") || "-"}`),
    );
    if (priority.exclusion_reasons.length) {
      lines.push(color(ANSI.yellow, `排除原因 · ${priority.exclusion_reasons.join(", ")}`));
    }
  } else {
    lines.push(color(ANSI.dim, "── 可解释优先级 · 不在当前 500 条有界快照中"));
  }
  if (item.proposal) {
    const proposal = item.proposal;
    lines.push(
      color(ANSI.cyan, `── Proposal Preview · ${proposal.proposal_kind}`),
      color(ANSI.yellow, `${proposal.title} · ${proposal.risk_level}`),
      color(ANSI.dim, `${proposal.proposal_id} · ${proposal.classification_reason}`),
      `Scope · ${compactText(proposal.impact_scope, 1000)}`,
      color(ANSI.dim, `目标文件 · ${proposal.intended_files.join(", ") || "尚未确定"}`),
      color(ANSI.yellow, "不可执行 · 未入队 · 必须人工审阅"),
      color(ANSI.cyan, `验证计划 · ${proposal.validation_plan.length}`),
    );
    for (const step of proposal.validation_plan) {
      lines.push(
        color(ANSI.dim, `• ${step.metric_name} ${step.direction} ${step.target} via ${step.verifier}`),
        color(ANSI.dim, `  ${compactText(step.procedure, 1000)}`),
      );
    }
  } else {
    lines.push(color(ANSI.yellow, "── Proposal Preview · 当前证据或安全 Gate 不允许生成"));
  }
  if (item.capability_proposal) {
    const capability = item.capability_proposal;
    lines.push(
      color(ANSI.cyan, `── Capability Proposal · ${capability.status}`),
      color(ANSI.yellow, capability.title),
      color(ANSI.dim, `${capability.proposal_id} · Portfolio P${capability.source.priority_rank} · score ${capability.source.priority_score_basis_points / 100}`),
      `Tool 名 · ${capability.interface.requested_name || "待用户定义"}`,
      color(ANSI.yellow, "API / 权限 / 数据 / Owner / SLO 尚未补齐"),
      color(ANSI.red, "可注册 否 · 可执行 否 · Shadow 否"),
      color(ANSI.cyan, `进入 Sandbox 前必须补齐 · ${capability.unresolved_requirements.length}`),
    );
    for (const requirement of capability.unresolved_requirements) {
      lines.push(color(ANSI.dim, `• ${requirement}`));
    }
  }
  if (item.capability_specification) {
    const specification = item.capability_specification;
    lines.push(
      color(ANSI.cyan, `── Capability Specification · ${specification.state}`),
      `${specification.specification_id} · revision ${specification.revision}/5`,
      color(ANSI.dim, `已完成 · ${specification.completed_steps.join(", ") || "-"}`),
      color(specification.pending_step ? ANSI.yellow : ANSI.green, `下一步 · ${specification.pending_step || "无"}`),
      color(ANSI.red, "Sandbox 否 · Shadow 否 · 可执行 否"),
    );
    if (specification.pending_interaction_id) {
      lines.push(color(ANSI.yellow, `待回答交互 · ${specification.pending_interaction_id}`));
    }
    if (specification.pending_step) {
      lines.push(color(ANSI.cyan, `继续 · /evolution capability-spec ${item.candidate_id}`));
    }
  }
  const aggregation = item.aggregation;
  if (aggregation) {
    lines.push(
      color(ANSI.cyan, `── 聚合趋势 · ${aggregation.policy_version}`),
      color(trendStyle(aggregation.trend), `${trendLabel(aggregation.trend)} · 24h/7d/30d ${aggregation.count_24h}/${aggregation.count_7d}/${aggregation.count_30d} · 前一7d ${aggregation.previous_7d_count}`),
      color(ANSI.dim, `Provider · ${dimensionText(aggregation.provider_counts, aggregation.provider_unique_count)}`),
      color(ANSI.dim, `Model · ${dimensionText(aggregation.model_counts, aggregation.model_unique_count)}`),
      color(ANSI.dim, `Platform · ${dimensionText(aggregation.platform_counts, aggregation.platform_unique_count)}`),
      color(ANSI.dim, `来源 · ${dimensionText(aggregation.source_counts, aggregation.source_unique_count)}`),
    );
  }
  lines.push(color(ANSI.cyan, `── Eligibility Gates · ${item.policy_version}`));
  for (const check of item.checks || []) {
    const label = check.passed ? "通过" : check.hard_block ? "硬阻断" : "待补齐";
    const style = check.passed ? ANSI.green : check.hard_block ? ANSI.red : ANSI.yellow;
    lines.push(color(style, `${label} · ${check.code}`), color(ANSI.dim, `  ${compactText(check.detail, 1000)}`));
  }
  lines.push(color(ANSI.cyan, "── 假设"), compactText(item.hypothesis, 2000));
  lines.push(color(ANSI.cyan, `── 机械指标 · ${(item.expected_metrics || []).length}`));
  lines.push(...(item.expected_metrics || []).map((metric) => color(ANSI.dim, `• ${compactText(metric, 1000)}`)));
  lines.push(color(ANSI.cyan, `── 审计链 · ${events.length}`));
  lines.push(...events.map((event) => color(ANSI.dim, `r${event.revision} · ${event.event_type} · +${event.added_evidence_count} evidence · ${event.occurred_at}`)));
  lines.push(color(ANSI.yellow, "只读 · 治理动作在 Workbench 执行 · approved 不授予实验资格"));
  return lines;
}

function decisionLabel(value) {
  return value === "review_ready" ? "可人工审阅" : value === "blocked" ? "已阻断" : "需要证据";
}

function riskStyle(value) {
  if (["high", "critical"].includes(value)) return ANSI.red;
  if (value === "medium") return ANSI.yellow;
  return ANSI.green;
}

function dimensionText(values, uniqueCount) {
  const entries = Array.isArray(values) ? values : [];
  const rendered = entries.map((item) => `${item.value} ${item.count} (${item.percentage}%)`).join(", ") || "-";
  const omitted = Math.max(0, Number(uniqueCount) - entries.length);
  return omitted ? `${rendered}，另有 ${omitted} 项` : rendered;
}

function trendLabel(value) {
  return value === "increasing" ? "上升" : value === "decreasing" ? "下降" : value === "stable" ? "稳定" : value === "new" ? "新出现" : "数据不足";
}

function trendStyle(value) {
  return value === "increasing" ? ANSI.red : value === "decreasing" ? ANSI.green : value === "stable" ? ANSI.cyan : ANSI.yellow;
}

function fit(line, width) {
  if (visibleWidth(line) <= width) return line;
  return wrapAnsiLine(line, width)[0] ?? "";
}
