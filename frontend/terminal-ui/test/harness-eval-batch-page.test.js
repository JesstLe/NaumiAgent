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
