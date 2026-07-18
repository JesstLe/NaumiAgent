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
  else logical.push(...listLines(snapshot.items, value.selectedIndex, snapshot.filters));
  const wrapped = logical.flatMap((line) => wrapAnsiLine(line, safeWidth));
  const offset = snapshot?.mode === "detail"
    ? Math.min(Math.max(0, Number(value.scrollOffset) || 0), Math.max(0, wrapped.length - 1))
    : 0;
  const lines = wrapped.slice(offset, offset + safeHeight);
  while (lines.length < safeHeight) lines.push("");
  return lines.slice(0, safeHeight).map((line) => padRight(fit(line, safeWidth), safeWidth));
}

function listLines(rawItems, selectedIndex, filters) {
  const items = Array.isArray(rawItems) ? rawItems : [];
  const filterText = [filters?.query && `query=${filters.query}`, filters?.risk && `risk=${filters.risk}`, filters?.source_kind && `source=${filters.source_kind}`].filter(Boolean).join(" · ");
  const lines = [color(ANSI.dim, filterText || "过滤 · 无"), `候选 · ${items.length} · 只读`];
  if (!items.length) return [...lines, color(ANSI.dim, "当前过滤条件下没有 Candidate。可运行 /feedback 或 /self-review 产生证据。")];
  const selected = Math.min(Math.max(0, Number(selectedIndex) || 0), items.length - 1);
  const start = Math.max(0, Math.min(selected - 6, Math.max(0, items.length - 14)));
  for (let index = start; index < Math.min(items.length, start + 14); index += 1) {
    const item = items[index];
    const marker = index === selected ? "›" : " ";
    const primary = `${marker} ${item.candidate_id} · ${item.finding_code} · ${item.risk} · ${decisionLabel(item.decision)}`;
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
  lines.push(color(ANSI.yellow, "只读 · 完整实验 Eligibility、approve/reject/defer 尚未开放"));
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
