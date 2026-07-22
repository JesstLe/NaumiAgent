import {
  ANSI,
  color,
  compactText,
  padRight,
  visibleWidth,
  wrapAnsiLine,
} from "../ansi.js";

const DECISIONS = Object.freeze({
  passed: [ANSI.green, "通过"],
  failed: [ANSI.red, "未通过"],
  flaky: [ANSI.yellow, "波动"],
  inconclusive: [ANSI.yellow, "证据不足"],
  incompatible: [ANSI.magenta, "不可比较"],
});

const VERDICTS = Object.freeze({
  improved: [ANSI.green, "改善"],
  unchanged: [ANSI.dim, "不变"],
  regressed: [ANSI.red, "退化"],
  flaky: [ANSI.yellow, "波动"],
  inconclusive: [ANSI.yellow, "证据不足"],
  incompatible: [ANSI.magenta, "不可比较"],
});

export function renderEvaluationLaneReceiptPage(view, width, height) {
  const safeWidth = Math.max(1, Number(width) || 1);
  const safeHeight = Math.max(1, Number(height) || 1);
  const value = object(view);
  const snapshot = object(value.snapshot);
  const logical = [
    color(ANSI.cyan, "Evaluation Lane Receipt"),
    color(ANSI.dim, "单条 RED/GREEN lane · R 刷新 · ↑/↓ 滚动 · Esc 返回"),
    ...snapshotLines(value, snapshot),
  ];
  const wrapped = logical.flatMap((line) => wrapAnsiLine(line, safeWidth));
  const offset = Math.min(
    Math.max(0, Number(value.scrollOffset) || 0),
    Math.max(0, wrapped.length - 1),
  );
  const lines = wrapped.slice(offset, offset + safeHeight);
  while (lines.length < safeHeight) lines.push("");
  return lines.slice(0, safeHeight).map((line) => padRight(fit(line, safeWidth), safeWidth));
}

function snapshotLines(view, snapshot) {
  if (view.loading) return [section("状态"), color(ANSI.cyan, "正在重读并校验权威 Evaluation 证据…")];
  if (view.error) return [section("状态"), color(ANSI.red, "回执不可用"), color(ANSI.yellow, text(view.error))];
  if (!snapshot.receipt_id) return [section("状态"), color(ANSI.yellow, "尚未收到 Evaluation Lane Receipt。")];
  const decision = DECISIONS[snapshot.comparison_decision] || [ANSI.dim, text(snapshot.comparison_decision)];
  const verdict = VERDICTS[snapshot.statistical_verdict] || [ANSI.dim, text(snapshot.statistical_verdict)];
  const baseline = object(snapshot.baseline);
  const candidate = object(snapshot.candidate);
  return [
    section("结论"),
    `${color(decision[0], decision[1])} · 统计 ${color(verdict[0], verdict[1])} · ${text(snapshot.statistical_code)}`,
    color(ANSI.yellow, "非最终结论：本页仅证明一条 lane；Candidate Evaluation 尚未完成，仍需跨 lane 聚合。"),
    `Lane ${text(snapshot.lane_kind)} · 平台 ${text(snapshot.platform)} · Suite ${text(snapshot.suite_id)}`,
    `Candidate ${text(snapshot.candidate_id)} · revision ${Number(snapshot.candidate_revision) || 0}`,
    section("RED → GREEN"),
    cohortLine("RED", baseline, ANSI.red),
    cohortLine("GREEN", candidate, ANSI.green),
    deltaLine(baseline, candidate),
    resourceLine("RED", baseline, ANSI.red),
    resourceLine("GREEN", candidate, ANSI.green),
    section("失败归因"),
    `类别 ${failureTone(snapshot.failure_category)} · 原因 ${text(snapshot.failure_reason_code)}`,
    `动作 ${text(snapshot.failure_action)} · candidate fault ${yesNo(snapshot.candidate_fault)}`,
    `retry ${yesNo(snapshot.retryable)} · rerun ${yesNo(snapshot.requires_rerun)} · reflection ${yesNo(snapshot.reflection_eligible)}`,
    section("证据链"),
    ...objects(snapshot.artifacts, 6).map((item) => (
      `${Number(item.order) || 0}. ${text(item.kind)} · ${short(item.artifact_id)} · sha ${short(item.sha256)}`
    )),
    `时间 ${text(snapshot.evidence_first_at)} → ${text(snapshot.evidence_last_at)}`,
    section("Authority"),
    `Receipt ${text(snapshot.receipt_id)} · sha ${short(snapshot.receipt_sha256)}`,
    `Comparison ${short(snapshot.comparison_id)} · Attribution ${text(snapshot.attribution_id)}`,
    color(ANSI.dim, `创建 ${text(snapshot.created_at)}`),
  ];
}

function cohortLine(label, cohort, tone) {
  return `${color(tone, label)} ${text(cohort.batch_id)} · 样本 ${number(cohort.samples)}`
    + ` · pass ${color(ANSI.green, number(cohort.passed_samples))}`
    + ` · fail ${color(ANSI.red, number(cohort.failed_samples))}`
    + ` · eval error ${color(ANSI.yellow, number(cohort.evaluation_error_samples))}`;
}

function deltaLine(baseline, candidate) {
  const passDelta = numberValue(candidate.passed_samples) - numberValue(baseline.passed_samples);
  const failDelta = numberValue(candidate.failed_samples) - numberValue(baseline.failed_samples);
  return `Δ pass ${signed(passDelta, passDelta >= 0 ? ANSI.green : ANSI.red)}`
    + ` · fail ${signed(failDelta, failDelta <= 0 ? ANSI.green : ANSI.red)}`
    + ` · duration ${signed(numberValue(candidate.duration_ms) - numberValue(baseline.duration_ms), ANSI.cyan)}ms`;
}

function resourceLine(label, cohort, tone) {
  const tokens = cohort.observed_tokens === null ? "未观测" : number(cohort.observed_tokens);
  const cost = cohort.observed_cost_usd === null ? "未观测" : `$${number(cohort.observed_cost_usd)}`;
  return `${color(tone, label)} 资源 · tokens ${tokens} (${number(cohort.token_samples)}/${number(cohort.samples)})`
    + ` · cost ${cost} (${number(cohort.cost_samples)}/${number(cohort.samples)})`
    + ` · ${number(cohort.duration_ms)}ms`;
}

function failureTone(category) {
  const value = text(category);
  if (value === "none") return color(ANSI.green, value);
  if (["evidence_incomplete", "flaky_evidence"].includes(value)) return color(ANSI.yellow, value);
  return color(ANSI.red, value);
}

function section(label) { return color(ANSI.cyan, `── ${label}`); }
function object(value) { return value && typeof value === "object" && !Array.isArray(value) ? value : {}; }
function objects(value, limit) { return Array.isArray(value) ? value.slice(0, limit).filter((item) => item && typeof item === "object") : []; }
function text(value) { return compactText(value ?? "", 500); }
function short(value) { const normalized = text(value); return normalized ? normalized.slice(0, 12) : "-"; }
function numberValue(value) { return Number.isFinite(Number(value)) ? Number(value) : 0; }
function number(value) { return Number.isInteger(numberValue(value)) ? String(numberValue(value)) : numberValue(value).toFixed(3).replace(/0+$/, "").replace(/\.$/, ""); }
function signed(value, tone) { const normalized = numberValue(value); return color(tone, `${normalized >= 0 ? "+" : ""}${number(normalized)}`); }
function yesNo(value) { return value ? color(ANSI.yellow, "是") : color(ANSI.green, "否"); }
function fit(line, width) { return visibleWidth(line) <= width ? line : (wrapAnsiLine(line, width)[0] ?? ""); }
