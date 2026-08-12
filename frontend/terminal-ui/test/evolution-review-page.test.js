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
  priority: {
    policy_version: "evolution-priority-v1",
    formula: "severity*frequency*confidence*5/(cost*change_risk)",
    domain: "correctness", rankable: true, rank: 1, score: 50,
    severity: 4, frequency: 2, qualifying_observations: 2, confidence: 75,
    confidence_lanes: ["explicit_user"], implementation_cost: 3,
    change_risk: 2, exclusion_reasons: [],
  },
};

const portfolio = {
  policy_version: "evolution-priority-v1",
  formula: "severity*frequency*confidence*5/(cost*change_risk)",
  anchor_at: "2026-07-18T18:01:00+00:00",
  window_start_at: "2026-06-18T18:01:00+00:00",
  window_days: 30, considered_count: 1, ranked_count: 1, excluded_count: 0,
  clusters: [{
    cluster_id: `eoc_${"b".repeat(24)}`, rank: 1, domain: "correctness", score: 50,
    primary_candidate_id: candidate.candidate_id, candidate_ids: [candidate.candidate_id],
    source_kinds: ["user_feedback"],
    impact: { candidate_count: 1, source_kind_count: 1, scope_count: 1, provider_count: 1, model_count: 1, platform_count: 1 },
  }],
};

test("evolution review list and detail stay bounded at common widths", () => {
  for (const width of [80, 120, 200]) {
    const list = renderEvolutionReviewPage({ snapshot: { mode: "list", filters: {}, items: [candidate], selected: null, events: [], portfolio }, selectedIndex: 0 }, width, 20);
    assert.equal(list.length, 20);
    assert(list.every((line) => visibleWidth(line) <= width));
    assert(list.map(stripAnsi).join("\n").includes("可人工"));
    assert(list.map(stripAnsi).join("\n").includes("P1 50"));
    assert(list.map(stripAnsi).join("\n").includes("全局 30d 优先级"));
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
      capability_proposal: {
        proposal_id: `evcp_${"c".repeat(24)}`,
        status: "needs_specification",
        title: "能力提案：browser.trace_compare",
        source: { priority_rank: 1, priority_score_basis_points: 165 },
        interface: { requested_name: "browser.trace_compare" },
        unresolved_requirements: ["api.parameters_schema", "permissions.required_families"],
      },
      capability_specification: {
        specification_id: `evcs_${"d".repeat(24)}`,
        state: "drafting",
        revision: 2,
        completed_steps: ["interface", "permissions"],
        pending_step: "data",
        pending_interaction_id: "",
      },
      capability_governance: {
        state: "awaiting_decision",
        decision_effective: false,
        sandbox_design_eligible: false,
        pending_interaction_id: "",
        assessment: {
          assessment_id: `evcsa_${"e".repeat(24)}`,
          checks: [
            "specification_complete", "candidate_lineage", "interaction_cardinality",
            "interaction_integrity", "answer_replay", "authority_closed",
          ].map((code) => ({ code, passed: true })),
        },
        decision: null,
      },
      capability_scenario_binding: {
        state: "ready",
        artifact_current: true,
        binding_current: true,
        sandbox_execution_eligible: true,
        sandbox_execution_authorized: false,
        registration_authorized: false,
        shadow_authorized: false,
        executable: false,
        pending_interaction_id: "",
        binding: {
          binding_id: `evcsb_${"f".repeat(24)}`,
          source_interaction_id: `ask-evcsbind-${"e".repeat(24)}-1`,
          scenarios: [{
            name: "比较两份真实轨迹",
            expectation_kind: "result",
            timeout_ms: 1500,
          }],
        },
      },
    };
    const detail = renderEvolutionReviewPage({ snapshot: { mode: "detail", filters: {}, items: [], selected, events: [] }, scrollOffset: 0 }, width, 80);
    const plain = detail.map(stripAnsi).join("\n");
    assert(detail.every((line) => visibleWidth(line) <= width));
    assert(plain.includes("Eligibility Gates"));
    assert(plain.includes("聚合趋势"));
    assert(plain.includes("24h/7d/30d 1/4/6"));
    assert(plain.includes("实验资格 否"));
    assert(plain.includes("Proposal Preview"));
    assert(plain.includes("不可执行 · 未入队 · 必须人工审阅"));
    assert(plain.includes("Capability Proposal"));
    assert(plain.includes("可注册 否 · 可执行 否 · Shadow 否"));
    assert(plain.includes("api.parameters_schema"));
    assert(plain.includes("Capability Specification · drafting"));
    assert(plain.includes("revision 2/5"));
    assert(plain.includes(`/evolution capability-spec ${candidate.candidate_id}`));
    assert(plain.includes("Capability Governance · awaiting_decision"));
    assert(plain.includes("Registry 注册 否 · Shadow 否 · 可执行 否"));
    assert(plain.includes(`/evolution capability-govern ${candidate.candidate_id}`));
    assert(plain.includes("Capability 可执行场景绑定 · ready"));
    assert(plain.includes("比较两份真实轨迹 · result · 1500ms"));
    assert(plain.includes("Workbench 治理"));
    assert(plain.includes("可解释优先级"));
    assert(plain.includes("严重度 4 × 频次 2"));
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

test("evolution review keeps the selected ranked candidate visible with cluster summary", () => {
  const items = Array.from({ length: 20 }, (_, index) => ({
    ...candidate,
    candidate_id: `evc_${index.toString(16).padStart(24, "0")}`,
    priority: { ...candidate.priority, rank: index + 1 },
  }));
  const lines = renderEvolutionReviewPage({
    snapshot: { mode: "list", filters: {}, items, selected: null, events: [], portfolio },
    selectedIndex: 19,
  }, 100, 20).map(stripAnsi).join("\n");

  assert(lines.includes(items[19].candidate_id));
  assert(lines.includes("全局 30d 优先级"));
});
