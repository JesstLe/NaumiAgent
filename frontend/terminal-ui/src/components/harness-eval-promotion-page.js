import {
  ANSI,
  color,
  compactText,
  padRight,
  visibleWidth,
  wrapAnsiLine,
} from "../ansi.js";

const STAGES = Object.freeze({
  awaiting_reason: [ANSI.cyan, "等待晋升理由"],
  awaiting_confirmation: [ANSI.yellow, "等待最终确认"],
  promoted: [ANSI.green, "晋升完成"],
  already_active: [ANSI.green, "已经是 Active"],
  not_selected: [ANSI.yellow, "历史版本未回拨"],
  cancelled: [ANSI.dim, "用户已取消"],
  error: [ANSI.red, "晋升失败"],
});

export function renderHarnessEvalPromotionPage(view, width, height) {
  const safeWidth = Math.max(1, Number(width) || 1);
  const safeHeight = Math.max(1, Number(height) || 1);
  const value = object(view);
  const snapshot = object(value.snapshot);
  const [tone, label] = STAGES[snapshot.stage] || [ANSI.dim, "准备交互"];
  const logical = [
    color(ANSI.cyan, "Harness Baseline 晋升"),
    color(ANSI.dim, `Suite · ${text(value.suiteId) || "-"} · Batch · ${text(value.batchId) || "-"} · Esc 返回`),
    section("状态"),
    color(tone, label),
    ...(snapshot.message ? [text(snapshot.message)] : []),
    ...(snapshot.promotion_reason
      ? [section("理由"), text(snapshot.promotion_reason)]
      : []),
    ...resultLines(snapshot, tone),
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

function resultLines(snapshot, tone) {
  if (!["promoted", "already_active", "not_selected", "cancelled", "error"].includes(snapshot.stage)) {
    return [color(ANSI.dim, "请在下方交互卡片中选择或输入。")];
  }
  if (["cancelled", "error"].includes(snapshot.stage)) {
    return [
      section("Selector"),
      color(tone, "未改变"),
      ...(snapshot.code ? [color(ANSI.dim, `Code · ${text(snapshot.code)}`)] : []),
    ];
  }
  return [
    section("权威结果"),
    `版本 · v${Number(snapshot.version) || 0} · 样本 ${Number(snapshot.sample_count) || 0}`,
    `Baseline · ${short(snapshot.baseline_id)}`,
    ...(snapshot.active_baseline_id ? [`当前 Active · ${short(snapshot.active_baseline_id)}`] : []),
    ...(snapshot.previous_baseline_id ? [`上一版本 · ${short(snapshot.previous_baseline_id)}`] : []),
    `操作者 · ${text(snapshot.promoted_by) || "-"} · ${text(snapshot.created_at) || "-"}`,
  ];
}

function section(label) {
  return color(ANSI.cyan, `── ${label}`);
}

function object(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function text(value) {
  return compactText(value ?? "", 500);
}

function short(value) {
  const normalized = text(value);
  return normalized ? normalized.slice(0, 12) : "-";
}

function fit(line, width) {
  if (visibleWidth(line) <= width) return line;
  return wrapAnsiLine(line, width)[0] ?? "";
}
