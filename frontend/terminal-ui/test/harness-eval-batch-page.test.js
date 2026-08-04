import test from "node:test";
import assert from "node:assert/strict";
import { stripAnsi, visibleWidth } from "../src/ansi.js";
import { renderHarnessEvalBatchPage } from "../src/components/harness-eval-batch-page.js";

function snapshot(stage) {
  return {
    stage,
    batch_id: "candidate-1",
    suite_id: "surface-protocol",
    requested: 5,
    completed: stage === "completed" ? 5 : 2,
    persisted: stage === "completed" ? 5 : 0,
    passed_cases: 4,
    implementation_failures: 0,
    evaluation_errors: 0,
    skipped: 0,
    duration_ms: 1250,
    baseline_eligible: stage === "completed",
    identity_sha256: stage === "completed" ? "a".repeat(64) : "",
  };
}

test("Harness Eval Batch page renders real progress and terminal promotion hint", () => {
  for (const width of [80, 120, 200]) {
    const progress = renderHarnessEvalBatchPage({
      suiteId: "surface-protocol",
      batchId: "candidate-1",
      snapshot: snapshot("evaluating"),
    }, width, 16).map(stripAnsi).join("\n");
    const completed = renderHarnessEvalBatchPage({
      suiteId: "surface-protocol",
      batchId: "candidate-1",
      snapshot: snapshot("completed"),
    }, width, 16);
    const plainCompleted = completed.map(stripAnsi).join("\n");
    assert(progress.includes("正在评测 · 40%"));
    assert(progress.includes("评测 · 2/5 · 已保存 0"));
    assert(plainCompleted.includes("评测完成 · 100%"));
    assert(plainCompleted.includes("可晋升"));
    assert(plainCompleted.includes("baseline promote"));
    assert(completed.every((line) => visibleWidth(line) <= width));
  }
});

test("Harness Eval Batch page never invents identity before source review", () => {
  const plain = renderHarnessEvalBatchPage({
    suiteId: "surface-protocol",
    snapshot: snapshot("persisting"),
  }, 100, 12).map(stripAnsi).join("\n");
  assert(plain.includes("Identity 将在完整 source boundary 复核后生成"));
  assert(plain.includes("正在保存 · 0%"));
  assert(!plain.includes("可晋升"));
});

test("Harness Live Eval page renders provider cost and bounded evidence", () => {
  const lines = renderHarnessEvalBatchPage({
    batchId: "live-batch-1",
    suiteId: "live-transport-core",
    snapshot: {
      kind: "live",
      stage: "evaluating",
      request_sha256: "b".repeat(64),
      batch_id: "live-batch-1",
      suite_id: "live-transport-core",
      model: "provider/model",
      provider_model: "model-20260805",
      requested: 5,
      completed: 2,
      persisted: 0,
      total_calls: 2,
      total_tokens: 56,
      total_cost_usd: 0.002,
      max_total_cost_usd: 0.1,
      duration_ms: 1250,
      max_total_duration_seconds: 30,
      actual_cost_exceeded: false,
      identity_sha256: "",
      baseline_eligible: false,
      code: "",
      message: "",
    },
  }, 110, 24);
  const plain = lines.map(stripAnsi).join("\n");

  assert(plain.includes("Harness Live Eval"));
  assert(plain.includes("Provider 实际模型 · model-20260805"));
  assert(plain.includes("成本 · $0.002000 / $0.100000"));
  assert(plain.includes("Request · bbbbbbbbbbbb"));
  assert(!plain.includes("NAUMI_LIVE_OK"));
  assert(!plain.includes("reasoning_content"));
  assert(lines.every((line) => visibleWidth(line) <= 110));
});

test("Harness Sandbox Eval page renders only coordinator facts", () => {
  const lines = renderHarnessEvalBatchPage({
    batchId: "sandbox-1",
    snapshot: {
      kind: "sandbox",
      stage: "executing",
      batch_id: "sandbox-1",
      check_ids: ["unit", "lint"],
      requested: 5,
      persisted: 2,
      checkpoint_id: `hsbatch_${"a".repeat(24)}`,
      authority_key: "b".repeat(64),
      lane: "sandbox",
      run_id: "manual:session-1",
      run_grant_sha256: "c".repeat(64),
      sample_result_sha256: ["d".repeat(64), "e".repeat(64)],
      code: "",
      updated_at: "2026-07-23T10:00:00+08:00",
    },
  }, 100, 18);
  const plain = lines.map(stripAnsi).join("\n");

  assert(plain.includes("Harness Sandbox Eval"));
  assert(plain.includes("隔离执行 · 40%"));
  assert(plain.includes("unit · lint"));
  assert(plain.includes("结果摘要 · 2 个"));
  assert(!plain.includes("Baseline"));
  assert(!plain.includes("实现回归"));
  assert(lines.every((line) => visibleWidth(line) <= 100));
});

test("Harness Sandbox Eval page renders durable queue position and capacity", () => {
  const lines = renderHarnessEvalBatchPage({
    batchId: "sandbox-queued",
    snapshot: {
      kind: "sandbox",
      stage: "queued",
      batch_id: "sandbox-queued",
      check_ids: ["unit"],
      requested: 5,
      persisted: 0,
      admission_ticket_id: `hsadm_${"f".repeat(24)}`,
      admission_epoch: 2,
      admission_state: "queued",
      queue_position: 2,
      max_active: 1,
      max_queued: 4,
      active_count: 1,
      queued_count: 3,
      checkpoint_id: `hsbatch_${"a".repeat(24)}`,
      authority_key: "b".repeat(64),
      lane: "sandbox",
      run_id: "",
      run_grant_sha256: "",
      sample_result_sha256: [],
      code: "",
      updated_at: "2026-07-23T10:00:00+08:00",
    },
  }, 110, 20);
  const plain = lines.map(stripAnsi).join("\n");

  assert(plain.includes("等待容量 · 0%"));
  assert(plain.includes(`Ticket · hsadm_${"f".repeat(24)}`));
  assert(plain.includes("排队 · 第 2 位"));
  assert(plain.includes("active 1/1 · queued 3/4"));
  assert(lines.every((line) => visibleWidth(line) <= 110));
});

test("Harness Sandbox Eval page renders retry action and durable result", () => {
  const base = {
    batchId: "sandbox-retry",
    cancelReceipt: {
      receipt_id: `hsacr_${"a".repeat(24)}`,
      receipt_sha256: "a".repeat(64),
      decision: "accepted",
      code: "sandbox_batch_cancelled_by_user",
    },
    snapshot: {
      kind: "sandbox",
      stage: "cancelled",
      batch_id: "sandbox-retry",
      check_ids: ["unit"],
      requested: 5,
      persisted: 2,
      checkpoint_id: `hsbatch_${"b".repeat(24)}`,
      authority_key: "b".repeat(64),
      lane: "sandbox",
      sample_result_sha256: ["c".repeat(64), "d".repeat(64)],
      code: "sandbox_batch_cancelled_by_user",
      updated_at: "2026-07-23T10:00:00+08:00",
    },
  };
  const action = renderHarnessEvalBatchPage(base, 110, 30)
    .map(stripAnsi)
    .join("\n");
  const completed = renderHarnessEvalBatchPage({
    ...base,
    retryResult: {
      receipt_id: `hsarr_${"e".repeat(24)}`,
      decision: "accepted",
      outcome: "completed",
      code: "sandbox_batch_retry_authorized",
      requested: 5,
      persisted: 5,
      message: "原 H5a 已恢复完成。",
      dispatch: {
        ticket_id: `hsadm_${"f".repeat(24)}`,
        epoch: 1,
      },
    },
  }, 110, 30).map(stripAnsi).join("\n");

  assert(action.includes("R 使用该回执恢复原请求"));
  assert(action.includes("SHA-256 · aaaaaaaaaaaa"));
  assert(completed.includes("恢复完成 · 原请求证据连续"));
  assert(completed.includes(`新 Ticket · hsadm_${"f".repeat(24)}`));
  assert(completed.includes("H5a · 5/5"));
});
