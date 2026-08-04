import {
  ANSI,
  color,
  compactText,
  padRight,
  visibleWidth,
  wrapAnsiLine,
} from "../ansi.js";

const STAGES = Object.freeze({
  preparing: [ANSI.cyan, "准备评测"],
  evaluating: [ANSI.cyan, "正在评测"],
  persisting: [ANSI.blue, "正在保存"],
  completed: [ANSI.green, "评测完成"],
  partial: [ANSI.yellow, "部分完成"],
  error: [ANSI.red, "执行失败"],
  queued: [ANSI.yellow, "等待容量"],
  admitted: [ANSI.cyan, "已取得容量"],
  recovering: [ANSI.cyan, "恢复检查点"],
  acquiring: [ANSI.cyan, "申请隔离 Worker"],
  executing: [ANSI.blue, "隔离执行"],
  failed: [ANSI.red, "执行失败"],
  cancelled: [ANSI.yellow, "已取消"],
  expired: [ANSI.red, "租约过期"],
});

export function renderHarnessEvalBatchPage(view, width, height) {
  const safeWidth = Math.max(1, Number(width) || 1);
  const safeHeight = Math.max(1, Number(height) || 1);
  const value = object(view);
  const snapshot = object(value.snapshot);
  if (snapshot.kind === "sandbox") {
    return renderSandboxEvalBatchPage(value, snapshot, safeWidth, safeHeight);
  }
  if (snapshot.kind === "live") {
    return renderLiveEvalBatchPage(value, snapshot, safeWidth, safeHeight);
  }
  const [tone, label] = STAGES[snapshot.stage] || [ANSI.dim, "等待后端"];
  const requested = Number(snapshot.requested) || 0;
  const completed = Number(snapshot.completed) || 0;
  const persisted = Number(snapshot.persisted) || 0;
  const progress = phaseProgress(snapshot.stage, completed, persisted, requested);
  const logical = [
    color(ANSI.cyan, "Harness Eval Batch"),
    color(ANSI.dim, `Suite · ${text(value.suiteId) || "-"} · ↑/↓ 滚动 · Esc 返回`),
    section("状态"),
    color(tone, `${label} · ${progress}%`),
    `Batch · ${text(snapshot.batch_id || value.batchId) || "等待分配"}`,
    `评测 · ${completed}/${requested || "-"} · 已保存 ${persisted}`,
    section("Case 汇总"),
    `${color(ANSI.green, `通过 ${Number(snapshot.passed_cases) || 0}`)}`
      + ` · ${color(ANSI.red, `实现回归 ${Number(snapshot.implementation_failures) || 0}`)}`
      + ` · ${color(ANSI.yellow, `评测错误 ${Number(snapshot.evaluation_errors) || 0}`)}`
      + ` · 跳过 ${Number(snapshot.skipped) || 0}`,
    section("Baseline"),
    snapshot.identity_sha256
      ? `Identity · ${text(snapshot.identity_sha256).slice(0, 12)} · ${snapshot.baseline_eligible ? color(ANSI.green, "可晋升") : color(ANSI.yellow, "不可晋升")}`
      : color(ANSI.dim, "Identity 将在完整 source boundary 复核后生成。"),
    `耗时 · ${formatDuration(snapshot.duration_ms)}`,
    ...(snapshot.message ? [section("说明"), color(tone, text(snapshot.message))] : []),
    ...(snapshot.code ? [color(ANSI.dim, `Code · ${text(snapshot.code)}`)] : []),
    ...(snapshot.stage === "completed"
      ? [color(ANSI.dim, `下一步 · /harness baseline promote ${text(snapshot.suite_id)} ${text(snapshot.batch_id)} --reason <原因>`)]
      : []),
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

function renderLiveEvalBatchPage(value, snapshot, width, height) {
  const [tone, label] = STAGES[snapshot.stage] || [ANSI.dim, "等待后端"];
  const requested = Number(snapshot.requested) || 0;
  const completed = Number(snapshot.completed) || 0;
  const persisted = Number(snapshot.persisted) || 0;
  const progress = phaseProgress(snapshot.stage, completed, persisted, requested);
  const logical = [
    color(ANSI.cyan, "Harness Live Eval"),
    color(ANSI.dim, "真实 Provider · 有界成本 · 不显示 Prompt/输出/思考 · ↑/↓ 滚动 · Esc 返回"),
    section("状态"),
    color(tone, `${label} · ${progress}%`),
    `Batch · ${text(snapshot.batch_id || value.batchId) || "等待分配"}`,
    `Suite · ${text(snapshot.suite_id || value.suiteId) || "-"}`,
    `样本 · 完成 ${completed}/${requested || "-"} · 已保存 ${persisted}`,
    section("模型与调用"),
    `请求模型 · ${text(snapshot.model) || "-"}`,
    `Provider 实际模型 · ${text(snapshot.provider_model) || "等待回执"}`,
    `回执确认调用 · ${Number(snapshot.total_calls) || 0} · Token ${Number(snapshot.total_tokens) || 0}`,
    section("预算"),
    color(
      snapshot.actual_cost_exceeded ? ANSI.red : ANSI.green,
      `成本 · $${formatCost(snapshot.total_cost_usd)} / $${formatCost(snapshot.max_total_cost_usd)}`,
    ),
    `耗时 · ${formatDuration(snapshot.duration_ms)} / ${formatDuration(Number(snapshot.max_total_duration_seconds) * 1000)}`,
    section("证据"),
    `Request · ${shortSha(snapshot.request_sha256)}`,
    snapshot.identity_sha256
      ? `Identity · ${shortSha(snapshot.identity_sha256)} · ${snapshot.baseline_eligible ? color(ANSI.green, "可晋升") : color(ANSI.yellow, "不可晋升")}`
      : color(ANSI.dim, "Identity 将在 source/model/provider 终态复核后生成。"),
    ...(snapshot.message ? [section("说明"), color(tone, text(snapshot.message))] : []),
    ...(snapshot.code ? [color(ANSI.dim, `Code · ${text(snapshot.code)}`)] : []),
  ];
  const wrapped = logical.flatMap((line) => wrapAnsiLine(line, width));
  const offset = Math.min(
    Math.max(0, Number(value.scrollOffset) || 0),
    Math.max(0, wrapped.length - 1),
  );
  const lines = wrapped.slice(offset, offset + height);
  while (lines.length < height) lines.push("");
  return lines.slice(0, height).map((line) => padRight(fit(line, width), width));
}

function renderSandboxEvalBatchPage(value, snapshot, width, height) {
  const [tone, label] = STAGES[snapshot.stage] || [ANSI.dim, "等待后端"];
  const requested = Number(snapshot.requested) || 0;
  const persisted = Number(snapshot.persisted) || 0;
  const progress = requested > 0 ? Math.round((persisted / requested) * 100) : 0;
  const checks = Array.isArray(snapshot.check_ids) ? snapshot.check_ids : [];
  const canCancel = ["queued", "active"].includes(snapshot.admission_state);
  const cancelReceipt = object(value.cancelReceipt);
  const retryResult = object(value.retryResult);
  const canRetry = cancelReceipt.decision === "accepted"
    && Boolean(cancelReceipt.receipt_id)
    && retryResult.decision !== "accepted";
  const logical = [
    color(ANSI.cyan, "Harness Sandbox Eval"),
    color(
      ANSI.dim,
      "精确 Git revision · 隔离 Worker · C 取消 · R 恢复 · ↑/↓ 滚动 · Esc 返回",
    ),
    section("状态"),
    color(tone, `${label} · ${snapshot.stage === "completed" ? 100 : progress}%`),
    `Batch · ${text(snapshot.batch_id || value.batchId) || "等待分配"}`,
    `样本 · 已持久化 ${persisted}/${requested || "-"}`,
    `Checks · ${checks.length ? checks.map(text).join(" · ") : "-"}`,
    ...(snapshot.admission_ticket_id
      ? [
          section("容量权威"),
          `Ticket · ${text(snapshot.admission_ticket_id)}`,
          snapshot.admission_state === "queued"
            ? color(
                ANSI.yellow,
                `排队 · 第 ${Number(snapshot.queue_position) || "-"} 位`
                  + ` · active ${Number(snapshot.active_count) || 0}/${Number(snapshot.max_active) || 0}`
                  + ` · queued ${Number(snapshot.queued_count) || 0}/${Number(snapshot.max_queued) || 0}`,
              )
            : `状态 · ${admissionStateLabel(snapshot.admission_state)}`
              + ` · active ${Number(snapshot.active_count) || 0}/${Number(snapshot.max_active) || 0}`
              + ` · queued ${Number(snapshot.queued_count) || 0}/${Number(snapshot.max_queued) || 0}`,
          `Epoch · ${Number(snapshot.admission_epoch) || "-"}`,
          ...(canCancel
            ? [
                value.cancelPending
                  ? color(ANSI.yellow, "取消请求正在由容量权威裁决…")
                  : color(ANSI.dim, "操作 · C 取消当前精确 ticket（无需二次确认）"),
              ]
            : []),
        ]
      : []),
    ...(cancelReceipt.receipt_id || cancelReceipt.code
      ? [
          section("取消回执"),
          color(
            cancelReceipt.decision === "accepted" ? ANSI.green : ANSI.yellow,
            cancelReceipt.decision === "accepted"
              ? "已接受 · 执行将在安全边界停止"
              : "未接受 · 页面状态已刷新，请核对后再操作",
          ),
          `Code · ${text(cancelReceipt.code)}`,
          ...(cancelReceipt.receipt_id
            ? [`Receipt · ${text(cancelReceipt.receipt_id)}`]
            : []),
          ...(cancelReceipt.receipt_sha256
            ? [`SHA-256 · ${shortSha(cancelReceipt.receipt_sha256)}`]
            : []),
          ...(canRetry
            ? [
                value.retryPending
                  ? color(ANSI.yellow, "恢复请求正在通过新执行权威调度…")
                  : color(ANSI.dim, "操作 · R 使用该回执恢复原请求（无需重填参数）"),
              ]
            : []),
        ]
      : []),
    ...(value.retryPending && !cancelReceipt.receipt_id
      ? [
          section("恢复"),
          color(ANSI.yellow, "恢复请求正在通过新执行权威调度…"),
        ]
      : []),
    ...(retryResult.receipt_id || retryResult.code
      ? [
          section("恢复回执"),
          color(
            retryTone(retryResult.outcome),
            retryOutcomeLabel(retryResult.outcome),
          ),
          `Code · ${text(retryResult.code)}`,
          ...(retryResult.receipt_id
            ? [`Receipt · ${text(retryResult.receipt_id)}`]
            : []),
          ...(retryResult.dispatch?.ticket_id
            ? [
                `新 Ticket · ${text(retryResult.dispatch.ticket_id)}`
                  + ` · epoch ${Number(retryResult.dispatch.epoch) || "-"}`,
              ]
            : []),
          ...(retryResult.persisted !== undefined
            ? [
                `H5a · ${Number(retryResult.persisted) || 0}/`
                  + `${Number(retryResult.requested) || "-"}`,
              ]
            : []),
          ...(retryResult.message
            ? [color(ANSI.dim, text(retryResult.message))]
            : []),
        ]
      : []),
    section("执行权威"),
    `Lane · ${text(snapshot.lane) || "-"}`,
    `Checkpoint · ${text(snapshot.checkpoint_id) || "-"}`,
    `Authority · ${shortSha(snapshot.authority_key)}`,
    `Run · ${text(snapshot.run_id) || "尚未获取"}`,
    `Grant · ${shortSha(snapshot.run_grant_sha256)}`,
    `结果摘要 · ${Array.isArray(snapshot.sample_result_sha256) ? snapshot.sample_result_sha256.length : 0} 个`,
    ...(snapshot.code
      ? [section("失败"), color(ANSI.red, `Code · ${text(snapshot.code)}`)]
      : []),
    color(ANSI.dim, `更新时间 · ${text(snapshot.updated_at) || "-"}`),
  ];
  const wrapped = logical.flatMap((line) => wrapAnsiLine(line, width));
  const offset = Math.min(
    Math.max(0, Number(value.scrollOffset) || 0),
    Math.max(0, wrapped.length - 1),
  );
  const lines = wrapped.slice(offset, offset + height);
  while (lines.length < height) lines.push("");
  return lines.slice(0, height).map((line) => padRight(fit(line, width), width));
}

function admissionStateLabel(value) {
  return {
    queued: "排队",
    active: "已准入",
    completed: "已释放",
    cancelled: "已取消",
    failed: "失败",
    expired: "已过期",
  }[text(value)] || "-";
}

function retryOutcomeLabel(value) {
  return {
    completed: "恢复完成 · 原请求证据连续",
    failed: "恢复失败 · 已关闭本次执行权威",
    cancelled: "恢复已取消 · 可使用新的取消回执再次恢复",
    rejected: "恢复未获授权 · 原取消回执未被本次执行消费",
    blocked: "恢复受阻 · 已有 owner 或权限状态需要刷新",
  }[text(value)] || "恢复状态未知";
}

function retryTone(value) {
  return {
    completed: ANSI.green,
    failed: ANSI.red,
    cancelled: ANSI.yellow,
    rejected: ANSI.yellow,
    blocked: ANSI.yellow,
  }[text(value)] || ANSI.dim;
}

function shortSha(value) {
  const normalized = text(value);
  return normalized ? normalized.slice(0, 12) : "-";
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

function formatDuration(value) {
  const milliseconds = Math.max(0, Number(value) || 0);
  return milliseconds >= 1000 ? `${(milliseconds / 1000).toFixed(1)}s` : `${Math.round(milliseconds)}ms`;
}

function formatCost(value) {
  return Math.max(0, Number(value) || 0).toFixed(6);
}

function phaseProgress(stage, completed, persisted, requested) {
  if (stage === "completed") return 100;
  if (requested <= 0) return 0;
  if (stage === "persisting") return Math.round((persisted / requested) * 100);
  return Math.round((completed / requested) * 100);
}

function fit(line, width) {
  if (visibleWidth(line) <= width) return line;
  return wrapAnsiLine(line, width)[0] ?? "";
}
