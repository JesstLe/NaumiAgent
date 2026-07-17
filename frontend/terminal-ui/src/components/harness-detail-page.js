import {
  ANSI,
  color,
  compactText,
  padRight,
  visibleWidth,
  wrapAnsiLine,
} from "../ansi.js";

const FAILURE_LABELS = Object.freeze({
  specification_gap: "规格缺口",
  knowledge_gap: "知识缺口",
  context_overflow: "上下文溢出",
  tool_contract_error: "工具契约错误",
  permission_block: "权限阻断",
  environment_error: "环境异常",
  implementation_error: "实现错误",
  verification_failure: "验证失败",
  evaluation_error: "评测错误",
  agent_premature_finish: "Agent 过早结束",
  agent_repetition: "Agent 重复执行",
  human_judgment_required: "需要人工判断",
});

const STATUS_LABELS = Object.freeze({
  completed_verified: "已验证",
  completed_unverified: "未验证",
  satisfied: "已满足",
  unsatisfied: "未满足",
  passed: "通过",
  failed: "失败",
  recorded: "已记录",
  reproduced: "已复现",
  changed: "已变化",
  digest_mismatch: "摘要不一致",
  missing: "缺失",
});

export function renderHarnessDetailPage(detail, width, height) {
  const safeWidth = Math.max(1, Number(width) || 1);
  const safeHeight = Math.max(1, Number(height) || 1);
  const value = detail && typeof detail === "object" ? detail : {};
  const logical = [
    color(ANSI.cyan, "Harness 运行详情"),
    color(ANSI.dim, `Run · ${text(value.runId) || "-"} · ↑/↓ 滚动 · Esc 返回`),
    ...explainLines(value),
    ...replayLines(value),
  ];
  const wrapped = logical.flatMap((line) => wrapAnsiLine(line, safeWidth));
  const offset = Math.min(Math.max(0, Number(value.scrollOffset) || 0), Math.max(0, wrapped.length - 1));
  const lines = wrapped.slice(offset, offset + safeHeight);
  while (lines.length < safeHeight) lines.push("");
  return lines.slice(0, safeHeight).map((line) => padRight(fit(line, safeWidth), safeWidth));
}

function explainLines(detail) {
  const payload = object(detail.explain);
  if (detail.explainLoading) {
    return [
      section("Explain"),
      color(ANSI.cyan, "正在加载 Explain 权威详情…"),
      ...(payload.lookup_status && payload.lookup_status !== "ok"
        ? [color(ANSI.yellow, text(payload.message) || "Explain 详情不可用。")]
        : []),
    ];
  }
  if (payload.lookup_status !== "ok" || !payload.explanation) {
    return [section("Explain"), color(ANSI.yellow, text(payload.message) || "Explain 详情不可用。")];
  }
  const value = object(payload.explanation);
  const criteria = objects(value.criteria, 100);
  const failures = texts(value.failure_classes, 20);
  const findings = objects(value.findings, 20);
  const checks = objects(value.checks, 50);
  const evidence = objects(value.evidence, 100);
  return [
    section("概览"),
    `目标 · ${text(value.objective) || "未记录"}`,
    `${status(value.status)} · ${text(value.summary) || "无摘要"}`,
    section("准则"),
    criteria.length
      ? criteria.map((item) => (
        `${status(item.status)} · ${text(item.description) || text(item.id)}`
        + ` · 证据 ${texts(item.evidence_ids, 100).length}`
      )).join("；")
      : color(ANSI.dim, "未记录验收准则"),
    section("失败分类"),
    failures.length
      ? color(ANSI.red, failures.map((item) => FAILURE_LABELS[item] || item).join(" · "))
      : color(ANSI.green, "无已分类失败"),
    ...findings.slice(0, 2).map((item) => color(
      ANSI.yellow,
      `${FAILURE_LABELS[item.failure_class] || item.failure_class} · ${text(item.message)}`
      + (item.next_step ? ` → ${text(item.next_step)}` : ""),
    )),
    section("检查"),
    checks.length
      ? checks.map((item) => `${text(item.id)} ${status(item.status)} ${Number(item.duration_ms) || 0}ms`).join(" · ")
      : color(ANSI.dim, "未记录检查"),
    section("证据"),
    evidence.length
      ? evidence.slice(0, 4).map((item) => (
        `${text(item.id)} ${text(item.kind)} ${status(item.status)}`
        + (item.uri ? ` ${text(item.uri)}` : "")
      )).join(" · ")
      : color(ANSI.dim, "未记录证据"),
  ];
}

function replayLines(detail) {
  const payload = object(detail.replay);
  if (detail.replayLoading) {
    return [
      section("Replay"),
      color(ANSI.cyan, "正在加载 Replay 权威详情…"),
      ...(payload.lookup_status && payload.lookup_status !== "ok"
        ? [color(ANSI.yellow, text(payload.message) || "Replay 详情不可用。")]
        : []),
    ];
  }
  if (payload.lookup_status !== "ok" || !payload.result) {
    return [section("Replay"), color(ANSI.yellow, text(payload.message) || "Replay 详情不可用。")];
  }
  const value = object(payload.result);
  const differences = objects(value.differences, 50);
  const artifacts = objects(value.artifacts, 100);
  return [
    section("Replay"),
    `${status(value.status)} · Timeline ${objects(value.timeline, 200).length} · 异常 ${texts(value.anomalies, 50).length}`,
    section("差异"),
    differences.length
      ? differences.slice(0, 3).map((item) => `${text(item.field)}: ${text(item.baseline)} → ${text(item.current)}`).join("；")
      : color(ANSI.green, "无差异"),
    section("Artifact"),
    artifacts.length
      ? artifacts.slice(0, 4).map((item) => (
        `${text(item.id)} ${text(item.kind)} ${status(item.status)}`
        + (item.reference ? ` ${text(item.reference)}` : "")
      )).join(" · ")
      : color(ANSI.dim, "无 Artifact"),
  ];
}

function section(label) {
  return color(ANSI.cyan, `── ${label}`);
}

function status(value) {
  const raw = text(value);
  const label = STATUS_LABELS[raw] || raw || "未知";
  if (["completed_verified", "satisfied", "passed", "recorded", "reproduced"].includes(raw)) {
    return color(ANSI.green, label);
  }
  if (["failed", "digest_mismatch", "missing"].includes(raw)) return color(ANSI.red, label);
  if (["completed_unverified", "unsatisfied", "changed"].includes(raw)) return color(ANSI.yellow, label);
  return label;
}

function object(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function objects(value, limit) {
  return Array.isArray(value) ? value.slice(0, limit).filter((item) => item && typeof item === "object") : [];
}

function texts(value, limit) {
  return Array.isArray(value) ? value.slice(0, limit).map(text).filter(Boolean) : [];
}

function text(value) {
  return compactText(value ?? "", 500);
}

function fit(line, width) {
  if (visibleWidth(line) <= width) return line;
  return wrapAnsiLine(line, width)[0] ?? "";
}
