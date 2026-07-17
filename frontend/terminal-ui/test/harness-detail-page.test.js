import test from "node:test";
import assert from "node:assert/strict";
import { stripAnsi, visibleWidth } from "../src/ansi.js";
import { renderHarnessDetailPage } from "../src/components/harness-detail-page.js";

function detail() {
  return {
    runId: "detail-run",
    explainLoading: false,
    replayLoading: false,
    explain: {
      lookup_status: "ok",
      explanation: {
        status: "completed_unverified",
        objective: "修复并验证详情页",
        summary: "发现验证问题",
        criteria: [{ id: "tests", description: "定向测试通过", status: "unsatisfied", evidence_ids: ["ev-1"] }],
        failure_classes: ["verification_failure"],
        findings: [{ failure_class: "verification_failure", source: "check:unit", message: "单元测试失败", next_step: "修复后重试", check_ids: ["unit"], evidence_ids: ["ev-1"] }],
        checks: [{ id: "unit", status: "failed", duration_ms: 42 }],
        evidence: [{ id: "ev-1", kind: "test_report", status: "recorded", digest_prefix: "abcdef123456", uri: "artifact://report" }],
      },
    },
    replay: {
      lookup_status: "ok",
      result: {
        status: "changed",
        anomalies: ["tree_changed"],
        differences: [{ field: "tree", baseline: "before", current: "after" }],
        artifacts: [{ id: "artifact-1", kind: "test_report", reference: "artifact://report", status: "digest_mismatch" }],
        timeline: [{ kind: "check", id: "unit", status: "failed" }],
      },
    },
    scrollOffset: 0,
  };
}

test("Harness detail page renders bounded semantic sections at common widths", () => {
  for (const width of [80, 120, 200]) {
    const lines = renderHarnessDetailPage(detail(), width, 24);
    const plain = lines.map(stripAnsi).join("\n");
    assert.equal(lines.length, 24);
    assert(lines.every((line) => visibleWidth(line) <= width));
    for (const expected of ["Harness 运行详情", "准则", "失败分类", "检查", "证据", "Replay", "差异", "Artifact"]) {
      assert(plain.includes(expected));
    }
  }
});

test("Harness detail page reports loading and unavailable state without invented success", () => {
  const value = detail();
  value.explainLoading = true;
  value.explain = { lookup_status: "unavailable", message: "状态库暂不可用" };
  value.replay = { lookup_status: "not_found", message: "Replay 不存在" };
  const plain = renderHarnessDetailPage(value, 100, 16).map(stripAnsi).join("\n");

  assert(plain.includes("正在加载"));
  assert(plain.includes("状态库暂不可用"));
  assert(plain.includes("Replay 不存在"));
  assert(!plain.includes("已验证"));
});
