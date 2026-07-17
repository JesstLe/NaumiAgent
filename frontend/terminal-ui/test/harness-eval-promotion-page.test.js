import test from "node:test";
import assert from "node:assert/strict";
import { stripAnsi, visibleWidth } from "../src/ansi.js";
import { renderHarnessEvalPromotionPage } from "../src/components/harness-eval-promotion-page.js";

function snapshot(stage = "promoted") {
  const successful = ["promoted", "already_active", "not_selected"].includes(stage);
  return {
    schema_version: 1,
    stage,
    terminal: !["awaiting_reason", "awaiting_confirmation"].includes(stage),
    suite_id: "surface-protocol",
    batch_id: "candidate-1",
    code: stage === "cancelled" ? "user_cancelled" : "",
    message: stage === "cancelled" ? "用户取消晋升。" : "",
    baseline_id: successful ? "a".repeat(64) : "",
    active_baseline_id: successful ? "a".repeat(64) : "",
    previous_baseline_id: "",
    version: successful ? 1 : 0,
    sample_count: successful ? 5 : 0,
    promoted_by: successful ? "user" : "",
    promotion_reason: successful ? "完整回归已通过" : "",
    created_at: successful ? "2026-07-18T10:00:00+00:00" : "",
  };
}

test("Harness Eval promotion page renders authoritative result at common widths", () => {
  for (const width of [80, 120, 200]) {
    const lines = renderHarnessEvalPromotionPage({
      suiteId: "surface-protocol",
      batchId: "candidate-1",
      snapshot: snapshot(),
      scrollOffset: 0,
    }, width, 14);
    const plain = lines.map(stripAnsi).join("\n");
    assert.equal(lines.length, 14);
    assert(lines.every((line) => visibleWidth(line) <= width));
    for (const expected of ["Baseline 晋升", "晋升完成", "权威结果", "v1", "操作者", "完整回归已通过"]) {
      assert(plain.includes(expected));
    }
  }
});

test("Harness Eval promotion page makes cancellation and pending interaction explicit", () => {
  const cancelled = renderHarnessEvalPromotionPage({
    suiteId: "surface-protocol",
    batchId: "candidate-1",
    snapshot: snapshot("cancelled"),
  }, 100, 12).map(stripAnsi).join("\n");
  const waiting = renderHarnessEvalPromotionPage({
    suiteId: "surface-protocol",
    batchId: "candidate-1",
    snapshot: snapshot("awaiting_reason"),
  }, 100, 12).map(stripAnsi).join("\n");

  assert(cancelled.includes("用户已取消"));
  assert(cancelled.includes("Selector"));
  assert(cancelled.includes("未改变"));
  assert(waiting.includes("交互卡片"));
});
