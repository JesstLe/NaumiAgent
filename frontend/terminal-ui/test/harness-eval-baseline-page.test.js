import test from "node:test";
import assert from "node:assert/strict";
import { stripAnsi, visibleWidth } from "../src/ansi.js";
import { renderHarnessEvalBaselinePage } from "../src/components/harness-eval-baseline-page.js";

function snapshot(status = "ok") {
  return {
    schema_version: 1,
    snapshot_sha256: "f".repeat(64),
    status,
    suite_id: "surface-protocol",
    message: status === "ok" ? "" : "状态说明",
    active: status === "ok" ? {
      id: "a".repeat(64),
      version: 2,
      batch_id: "baseline-2",
      sample_count: 5,
      identity_sha256: "b".repeat(64),
      promoted_by: "user",
      promotion_reason: "真实验证完成",
      created_at: "2026-07-18T10:00:00+00:00",
    } : null,
    comparisons: status === "ok" ? [{
      id: "d".repeat(64),
      baseline_id: "a".repeat(64),
      current_batch_id: "candidate-2",
      decision: "passed",
      statistical_verdict: "unchanged",
      current_samples: 5,
    }] : [],
  };
}

test("Harness Eval Baseline page renders authoritative status at common widths", () => {
  for (const width of [80, 120, 200]) {
    const lines = renderHarnessEvalBaselinePage({
      suiteId: "surface-protocol",
      loading: false,
      snapshot: snapshot(),
      scrollOffset: 0,
    }, width, 16);
    const plain = lines.map(stripAnsi).join("\n");
    assert.equal(lines.length, 16);
    assert(lines.every((line) => visibleWidth(line) <= width));
    for (const expected of ["Harness Eval Baseline", "Active", "baseline-2", "最近比较", "通过", "candidate-2"]) {
      assert(plain.includes(expected));
    }
  }
});

test("Harness Eval Baseline page distinguishes loading empty and unavailable", () => {
  const loading = renderHarnessEvalBaselinePage({ loading: true }, 100, 8).map(stripAnsi).join("\n");
  const empty = renderHarnessEvalBaselinePage({ snapshot: snapshot("empty") }, 100, 8).map(stripAnsi).join("\n");
  const unavailable = renderHarnessEvalBaselinePage({ snapshot: snapshot("unavailable") }, 100, 8).map(stripAnsi).join("\n");
  assert(loading.includes("正在读取"));
  assert(empty.includes("尚无 Active Baseline"));
  assert(unavailable.includes("状态库不可用"));
  assert(!unavailable.includes("v2"));
});
