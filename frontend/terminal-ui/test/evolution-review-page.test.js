import test from "node:test";
import assert from "node:assert/strict";
import { stripAnsi, visibleWidth } from "../src/ansi.js";
import { renderEvolutionReviewPage } from "../src/components/evolution-review-page.js";

const candidate = {
  candidate_id: `evc_${"a".repeat(24)}`,
  finding_code: "user_reported_defect",
  kind: "correctness",
  scope: "ui:footer",
  risk: "medium",
  occurrence_count: 2,
  source_kinds: ["user_feedback"],
  last_observed_at: "2026-07-18T18:01:00+00:00",
  revision: 2,
  decision: "review_ready",
  review_ready: true,
  human_review_required: false,
  experiment_eligible: false,
};

test("evolution review list and detail stay bounded at common widths", () => {
  for (const width of [80, 120, 200]) {
    const list = renderEvolutionReviewPage({ snapshot: { mode: "list", filters: {}, items: [candidate], selected: null, events: [] }, selectedIndex: 0 }, width, 20);
    assert.equal(list.length, 20);
    assert(list.every((line) => visibleWidth(line) <= width));
    assert(list.map(stripAnsi).join("\n").includes("可人工审阅"));
    const selected = {
      ...candidate,
      status: "draft",
      hypothesis: "用机械指标验证修复。",
      providers: ["openai"], models: ["model"], platforms: ["darwin"],
      first_observed_at: "2026-07-18T18:00:00+00:00",
      expected_metrics: ["feedback.recurrence decrease 0"], evidence_refs: [],
      policy_version: "candidate-eligibility-v2",
      checks: [{ code: "cooldown_gate", passed: false, hard_block: false, detail: "等待冷却记录。" }],
      governance: {
        policy_version: "proposal-governance-v1", allowed: false,
        reason: "cooldown_active", proposal_state: "rejected", proposal_revision: 2,
        cooldown_until: "2026-08-17T18:00:00+00:00", significant_new_evidence: false,
      },
      aggregation: {
        policy_version: "candidate-aggregation-v1", trend: "increasing",
        count_24h: 1, count_7d: 4, count_30d: 6, previous_7d_count: 2,
        provider_counts: [{ value: "openai", count: 4, percentage: 66.7 }],
        provider_unique_count: 1, model_counts: [], model_unique_count: 0,
        platform_counts: [], platform_unique_count: 0, source_counts: [], source_unique_count: 0,
      },
      proposal: {
        proposal_id: `evp_${"b".repeat(24)}`, proposal_kind: "code",
        title: "代码改进建议：user_reported_defect", risk_level: "medium",
        classification_reason: "fallback:code", impact_scope: "ui:footer",
        intended_files: [], validation_plan: [{
          metric_name: "feedback.recurrence", direction: "decrease", target: 0,
          verifier: "feedback_recurrence", procedure: "比较后续反馈复发率。",
        }],
      },
    };
    const detail = renderEvolutionReviewPage({ snapshot: { mode: "detail", filters: {}, items: [], selected, events: [] }, scrollOffset: 0 }, width, 60);
    const plain = detail.map(stripAnsi).join("\n");
    assert(detail.every((line) => visibleWidth(line) <= width));
    assert(plain.includes("Eligibility Gates"));
    assert(plain.includes("聚合趋势"));
    assert(plain.includes("24h/7d/30d 1/4/6"));
    assert(plain.includes("实验资格 否"));
    assert(plain.includes("Proposal Preview"));
    assert(plain.includes("不可执行 · 未入队 · 必须人工审阅"));
    assert(plain.includes("Workbench 治理"));
    assert(plain.includes("冷却阻断 · cooldown_active"));
    assert(plain.includes("rejected / r2"));
  }
});

test("evolution review distinguishes loading empty and missing detail", () => {
  const loading = renderEvolutionReviewPage({ snapshot: null, loading: true }, 90, 10).map(stripAnsi).join("\n");
  const empty = renderEvolutionReviewPage({ snapshot: { mode: "list", filters: {}, items: [] } }, 90, 10).map(stripAnsi).join("\n");
  const missing = renderEvolutionReviewPage({ snapshot: { mode: "detail", selected: null, events: [] } }, 90, 10).map(stripAnsi).join("\n");
  assert(loading.includes("正在加载"));
  assert(empty.includes("没有 Candidate"));
  assert(missing.includes("不存在"));
});
