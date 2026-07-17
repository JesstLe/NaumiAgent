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
  inconclusive: [ANSI.yellow, "无法判断"],
  incompatible: [ANSI.magenta, "不可比较"],
});

export function renderHarnessEvalBaselinePage(view, width, height) {
  const safeWidth = Math.max(1, Number(width) || 1);
  const safeHeight = Math.max(1, Number(height) || 1);
  const value = object(view);
  const snapshot = object(value.snapshot);
  const logical = [
    color(ANSI.cyan, "Harness Eval Baseline"),
    color(ANSI.dim, `Suite · ${text(value.suiteId) || "-"} · ↑/↓ 滚动 · Esc 返回`),
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
  if (view.loading) return [section("状态"), color(ANSI.cyan, "正在读取权威 Baseline 快照…")];
  if (!snapshot.status) return [section("状态"), color(ANSI.yellow, "尚未收到 Baseline 快照。")];
  if (snapshot.status === "unavailable") {
    return [
      section("状态"),
      color(ANSI.red, "状态库不可用"),
      color(ANSI.yellow, text(snapshot.message) || "请运行 /harness doctor 检查状态库。"),
    ];
  }
  if (snapshot.status === "empty" || !snapshot.active) {
    return [
      section("状态"),
      color(ANSI.yellow, "尚无 Active Baseline"),
      text(snapshot.message) || "请先生成稳定的重复 Eval cohort，再显式晋升。",
    ];
  }
  const active = object(snapshot.active);
  const comparisons = objects(snapshot.comparisons, 20);
  return [
    section("Active"),
    `${color(ANSI.green, `v${Number(active.version) || 0}`)} · Batch ${text(active.batch_id)} · 样本 ${Number(active.sample_count) || 0}`,
    `Baseline ${short(active.id)} · Identity ${short(active.identity_sha256)}`,
    `晋升 ${text(active.promoted_by)} · ${text(active.created_at)}`,
    `原因 · ${text(active.promotion_reason) || "未记录"}`,
    section(`最近比较 · ${comparisons.length}`),
    ...(comparisons.length
      ? comparisons.map((item) => {
        const [tone, label] = DECISIONS[item.decision] || [ANSI.dim, text(item.decision) || "未知"];
        return `${color(tone, label)} · Candidate ${text(item.current_batch_id)}`
          + ` · 统计 ${text(item.statistical_verdict)} · 样本 ${Number(item.current_samples) || 0}`
          + ` · Receipt ${short(item.id)}`;
      })
      : [color(ANSI.dim, "尚无引用当前 Active Baseline 的 Comparison receipt。")]),
    section("快照"),
    color(ANSI.dim, `SHA-256 · ${short(snapshot.snapshot_sha256)}`),
  ];
}

function section(label) {
  return color(ANSI.cyan, `── ${label}`);
}

function object(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function objects(value, limit) {
  return Array.isArray(value) ? value.slice(0, limit).filter((item) => item && typeof item === "object") : [];
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
