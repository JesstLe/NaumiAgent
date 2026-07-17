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
