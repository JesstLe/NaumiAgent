import test from "node:test";
import assert from "node:assert/strict";
import { stripAnsi, visibleWidth } from "../src/ansi.js";
import { renderEvaluationLaneReceiptPage } from "../src/components/evaluation-lane-receipt-page.js";
import GOLDEN from "../../../tests/fixtures/ui17/evaluation-lane-receipt-golden.json" with { type: "json" };

function snapshot() {
  return structuredClone(GOLDEN.typed_payload);
}

test("Evaluation Lane page renders RED/GREEN evidence and non-final boundary", () => {
  for (const width of [80, 120, 200]) {
    const lines = renderEvaluationLaneReceiptPage({ snapshot: snapshot(), scrollOffset: 0 }, width, 30);
    const plain = lines.map(stripAnsi).join("\n");
    assert(lines.every((line) => visibleWidth(line) <= width));
    for (const expected of GOLDEN.new_ui_markers) {
      assert(plain.includes(expected), `${width} columns should contain ${expected}`);
    }
  }
});

test("Evaluation Lane page distinguishes loading and error states", () => {
  const loading = renderEvaluationLaneReceiptPage({ loading: true }, 100, 8).map(stripAnsi).join("\n");
  const failed = renderEvaluationLaneReceiptPage({ error: "证据损坏" }, 100, 8).map(stripAnsi).join("\n");
  assert(loading.includes("正在重读"));
  assert(failed.includes("回执不可用"));
  assert(failed.includes("证据损坏"));
});
