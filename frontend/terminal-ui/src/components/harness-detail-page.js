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
  verified: "已验证",
  reproduced: "已复现",
  changed: "已变化",
  digest_mismatch: "摘要不一致",
  missing: "缺失",
});

export function renderHarnessDetailPage(detail, width, height) {
  const safeWidth = Math.max(1, Number(width) || 1);
  const safeHeight = Math.max(1, Number(height) || 1);
  const value = detail && typeof detail === "object" ? detail : {};
  const evidenceFocused = value.focus === "evidence";
  const logical = [
    color(ANSI.cyan, evidenceFocused ? "Harness 证据焦点" : "Harness 运行详情"),
    color(
      ANSI.dim,
      evidenceFocused
        ? `Run · ${text(value.runId) || "-"} · v 返回全部 · e 刷新 Evidence · ↑/↓ 滚动 · Esc 返回`
        : `Run · ${text(value.runId) || "-"} · v 聚焦 Evidence · e 刷新 Explain · r 刷新 Replay · ↑/↓ 滚动 · Esc 返回`,
    ),
    ...(evidenceFocused
      ? evidenceFocusLines(value)
      : [...explainLines(value), ...replayLines(value)]),
  ];
  const wrapped = logical.flatMap((line) => wrapAnsiLine(line, safeWidth));
  const offset = Math.min(Math.max(0, Number(value.scrollOffset) || 0), Math.max(0, wrapped.length - 1));
  const lines = wrapped.slice(offset, offset + safeHeight);
  while (lines.length < safeHeight) lines.push("");
  return lines.slice(0, safeHeight).map((line) => padRight(fit(line, safeWidth), safeWidth));
}

function evidenceFocusLines(detail) {
  const payload = object(detail.explain);
  if (detail.explainLoading) {
    return [
      section("权威证据记录"),
      color(ANSI.cyan, "正在加载 Evidence 权威详情…"),
      ...(payload.lookup_status && payload.lookup_status !== "ok"
        ? [color(ANSI.yellow, text(payload.message) || "Harness 证据详情不可用。")] : []),
    ];
  }
  if (payload.lookup_status !== "ok" || !payload.explanation) {
    return [
      section("权威证据记录"),
      color(ANSI.yellow, text(payload.message) || "Harness 证据详情不可用。"),
    ];
  }
  const value = object(payload.explanation);
  const criteria = objects(value.criteria, 100);
  const findings = objects(value.findings, 20);
  const evidence = objects(value.evidence, 100);
  const references = evidenceReferences(criteria, findings);
  const knownIds = new Set(evidence.map((item) => text(item.id)).filter(Boolean));
  const missingIds = references.order.filter((item) => !knownIds.has(item));
  const lines = [
    section("概览"),
    `${status(value.status)} · ${text(value.objective) || "未记录目标"}`,
    text(value.summary) || color(ANSI.dim, "无摘要"),
    section("权威证据记录"),
  ];
  if (!evidence.length) lines.push(color(ANSI.dim, "未记录证据"));
  for (const item of evidence) {
    const evidenceId = text(item.id);
    const relatedCriteria = references.criteria.get(evidenceId) || [];
    const relatedFindings = references.findings.get(evidenceId) || [];
    lines.push(section(`证据 ${evidenceId || "未命名"}`));
    lines.push(
      `${text(item.kind) || "unknown"} · ${status(item.status)}`
      + (item.digest_prefix ? ` · digest ${text(item.digest_prefix)}` : "")
      + (item.uri ? ` · ${text(item.uri)}` : ""),
    );
    for (const criterion of relatedCriteria) {
      lines.push(
        `准则 · ${status(criterion.status)} · ${text(criterion.id) || "未命名准则"}`
        + ` · ${text(criterion.description) || "未记录描述"}`,
      );
    }
    for (const finding of relatedFindings) {
      const checks = uniqueTexts(finding.check_ids, 50);
      lines.push(color(
        ANSI.yellow,
        `发现 · ${FAILURE_LABELS[finding.failure_class] || text(finding.failure_class) || "未分类"}`
        + ` · ${text(finding.message) || "无说明"}`
        + (finding.source ? ` · 来源 ${text(finding.source)}` : "")
        + (checks.length ? ` · 检查 ${checks.join(", ")}` : "")
        + (finding.next_step ? ` → ${text(finding.next_step)}` : ""),
      ));
    }
    if (!relatedCriteria.length && !relatedFindings.length) {
      lines.push(color(ANSI.dim, "关联 · 未被准则或发现引用"));
    }
  }
  if (missingIds.length) {
    lines.push(section("引用缺口"));
    lines.push(...missingIds.map((item) => color(
      ANSI.red,
      `${item} · 引用存在但权威证据记录缺失`,
    )));
  }
  return lines;
}

function evidenceReferences(criteria, findings) {
  const criterionRefs = new Map();
  const findingRefs = new Map();
  const order = [];
  const seen = new Set();
  for (const [target, items] of [[criterionRefs, criteria], [findingRefs, findings]]) {
    for (const item of items) {
      for (const evidenceId of uniqueTexts(item.evidence_ids, 100)) {
        if (!target.has(evidenceId)) target.set(evidenceId, []);
        target.get(evidenceId).push(item);
        if (!seen.has(evidenceId)) {
          seen.add(evidenceId);
          order.push(evidenceId);
        }
      }
    }
  }
  return { criteria: criterionRefs, findings: findingRefs, order };
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
    ...(criteria.length
      ? criteria.map((item) => (
        `${status(item.status)} · ${text(item.id) || "未命名准则"} · ${text(item.description) || "未记录描述"}`
        + ` · 证据 ${texts(item.evidence_ids, 100).length}`
      ))
      : [color(ANSI.dim, "未记录验收准则")]),
    section("失败分类"),
    failures.length
      ? color(ANSI.red, failures.map((item) => FAILURE_LABELS[item] || item).join(" · "))
      : color(ANSI.green, "无已分类失败"),
    ...findings.map((item) => color(
      ANSI.yellow,
      `${FAILURE_LABELS[item.failure_class] || item.failure_class} · ${text(item.message)}`
      + (item.source ? ` · 来源 ${text(item.source)}` : "")
      + (item.next_step ? ` → ${text(item.next_step)}` : ""),
    )),
    section("检查"),
    ...(checks.length
      ? checks.map((item) => `${text(item.id)} ${status(item.status)} ${Number(item.duration_ms) || 0}ms`)
      : [color(ANSI.dim, "未记录检查")]),
    section("证据"),
    ...(evidence.length
      ? evidence.map((item) => (
        `${text(item.id)} ${text(item.kind)} ${status(item.status)}`
        + (item.digest_prefix ? ` digest ${text(item.digest_prefix)}` : "")
        + (item.uri ? ` ${text(item.uri)}` : "")
      ))
      : [color(ANSI.dim, "未记录证据")]),
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
    ...texts(value.anomalies, 50).map((item) => color(ANSI.yellow, `异常 · ${item}`)),
    section("差异"),
    ...(differences.length
      ? differences.map((item) => `${text(item.field)}: ${text(item.baseline)} → ${text(item.current)}`)
      : [color(ANSI.green, "无差异")]),
    section("Artifact"),
    ...(artifacts.length
      ? artifacts.map((item) => (
        `${text(item.id)} ${text(item.kind)} ${status(item.status)}`
        + (item.reference ? ` ${text(item.reference)}` : "")
      ))
      : [color(ANSI.dim, "无 Artifact")]),
  ];
}

function section(label) {
  return color(ANSI.cyan, `── ${label}`);
}

function status(value) {
  const raw = text(value);
  const label = STATUS_LABELS[raw] || raw || "未知";
  if (["completed_verified", "satisfied", "passed", "recorded", "verified", "reproduced"].includes(raw)) {
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

function uniqueTexts(value, limit) {
  return [...new Set(texts(value, limit))];
}

function text(value) {
  return compactText(value ?? "", 500);
}

function fit(line, width) {
  if (visibleWidth(line) <= width) return line;
  return wrapAnsiLine(line, width)[0] ?? "";
}
