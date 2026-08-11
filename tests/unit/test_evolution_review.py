from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.evolution.queue import EvolutionProposalQueueResult
from naumi_agent.evolution.review import (
    EvolutionReviewFilter,
    EvolutionReviewService,
    render_evolution_review,
)
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.harness.feedback import (
    FeedbackIntakeService,
    build_direct_user_feedback,
)
from naumi_agent.tools.evolution_review import (
    EvolutionApprovalDecisionAuthorityTool,
    EvolutionApprovalPrincipalAuthorityTool,
    EvolutionApprovalPrincipalTool,
    EvolutionApprovalSignatureAuthorityTool,
    EvolutionApprovalSignatureTool,
    EvolutionCandidatesTool,
    EvolutionCounterfactualEvidenceTool,
    EvolutionDecisionInputTool,
    EvolutionDecisionResolutionTool,
    EvolutionDecisionStateTool,
    EvolutionEvaluationAggregationContractTool,
    EvolutionEvaluationReceiptTool,
    EvolutionExperimentContractAuthorityTool,
    EvolutionExperimentContractIssueTool,
    EvolutionFinalEvaluationReceiptTool,
    EvolutionIndependentReviewTool,
    EvolutionInstallationKeyTool,
    EvolutionMechanicalGateTool,
    EvolutionOutcomeOpportunityTool,
    EvolutionPostRollbackBehavioralCoverageTool,
    EvolutionPostRollbackBehavioralLaneTool,
    EvolutionPostRollbackBehavioralMatrixTool,
    EvolutionPostRollbackLongTermObservationAssessmentTool,
    EvolutionPostRollbackLongTermObservationContractTool,
    EvolutionPostRollbackLongTermOutcomeTool,
    EvolutionPostRollbackRemoteClaimTool,
    EvolutionPostRollbackRemoteDeliveryTool,
    EvolutionPostRollbackRemoteDispatchTool,
    EvolutionPostRollbackRemoteExecutionAuthorizationTool,
    EvolutionPostRollbackRemoteLanePlacementTool,
    EvolutionPostRollbackRemoteResultTool,
    EvolutionPostRollbackRuntimeObservationAdmissionTool,
    EvolutionPostRollbackRuntimeVerificationTool,
    EvolutionPostRollbackTargetBaselineTool,
    EvolutionPromotionApprovalDecisionTool,
    EvolutionPromotionApprovalRequestTool,
    EvolutionPromotionApprovalRequirementTool,
    EvolutionPromotionPackageInputTool,
    EvolutionPromotionPackageTool,
    EvolutionProposalBeforeAfterEvidenceTool,
    EvolutionProposalQueueTool,
    EvolutionReflectionMemoryRevokeTool,
    EvolutionReflectionMemoryTool,
    EvolutionRevalidationEvaluationPlanTool,
    EvolutionRevalidationEvaluationSourceTool,
    EvolutionRevalidationOutcomeTool,
    EvolutionRevalidationReplayTool,
    EvolutionRevalidationRequestAuthorityTool,
    EvolutionRevalidationRequestTool,
    EvolutionRevalidationRollbackExecutionTool,
    EvolutionRevalidationRollbackOutcomeTool,
    EvolutionRevalidationRuntimeContractTool,
    EvolutionRevalidationValidationPlanTool,
    EvolutionRevalidationValidationTool,
    EvolutionRewardHackingEvidenceTool,
    EvolutionRolloutControlKeyTool,
    EvolutionStablePopulationCandidatePreviewTool,
    EvolutionStablePopulationCompletionTool,
    EvolutionStablePromotionObservationContractTool,
    EvolutionStablePromotionRuntimeObservationAdmissionTool,
    EvolutionStableRemoteFinalizationAuthorizationTool,
    EvolutionStableRemoteFinalizationTool,
    EvolutionStableRemotePopulationFinalizationTool,
    EvolutionStableRemoteReadinessClaimTool,
    EvolutionStableRemoteReadinessProbeTool,
    EvolutionStableRollbackReadinessTool,
    EvolutionStableRolloutAuthorizationTool,
    EvolutionStableRolloutFinalizationTool,
    create_evolution_review_tools,
)

NOW = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)


async def _seed(
    root: Path,
    store: EvolutionCandidateStore,
) -> tuple[str, str]:
    intake = FeedbackIntakeService(store)
    first = await intake.ingest(
        root,
        build_direct_user_feedback(
            session_id="session-review",
            category="defect",
            scope="ui:footer",
            topic="truncation",
            summary="底栏内容被截断 secret-never-render",
            provider="openai",
            model="openai/kimi-for-coding",
            platform="darwin",
            now=NOW,
        ),
    )
    await intake.ingest(
        root,
        build_direct_user_feedback(
            session_id="session-review",
            category="defect",
            scope="ui:footer",
            topic="truncation",
            summary="底栏仍然被截断",
            provider="openai",
            model="openai/kimi-for-coding",
            platform="darwin",
            now=NOW + timedelta(minutes=1),
        ),
    )
    second = await intake.ingest(
        root,
        build_direct_user_feedback(
            session_id="session-review",
            category="correction",
            scope="ui:task_panel",
            topic="subagent_status",
            summary="子智能体状态不正确",
            provider="anthropic",
            model="anthropic/claude",
            platform="linux",
            now=NOW + timedelta(minutes=2),
        ),
    )
    return first.candidate_id, second.candidate_id


@pytest.mark.asyncio
async def test_review_list_empty_and_filtered_state(tmp_path: Path) -> None:
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    service = EvolutionReviewService(store)
    empty = await service.list_snapshot(tmp_path)
    assert "没有 Candidate" in render_evolution_review(empty)

    footer_id, _task_id = await _seed(tmp_path, store)
    snapshot = await service.list_snapshot(
        tmp_path,
        filters=EvolutionReviewFilter(
            query="footer",
            risk="medium",
            source_kind="user_feedback",
            limit=1,
        ),
    )

    assert [item.candidate_id for item in snapshot.items] == [footer_id]
    item = snapshot.items[0]
    assert item.occurrence_count == 2
    assert item.providers == ("openai",)
    assert item.models == ("openai/kimi-for-coding",)
    assert item.platforms == ("darwin",)
    rendered = render_evolution_review(snapshot)
    assert "ui:footer" in rendered
    assert "secret-never-render" not in rendered


@pytest.mark.asyncio
async def test_review_detail_contains_verified_evidence_and_audit_chain(
    tmp_path: Path,
) -> None:
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    footer_id, _task_id = await _seed(tmp_path, store)
    service = EvolutionReviewService(store)

    snapshot = await service.detail_snapshot(tmp_path, footer_id)
    rendered = render_evolution_review(snapshot)

    assert snapshot.selected is not None
    assert snapshot.selected.experiment_eligible is False
    assert snapshot.selected.revision == 2
    assert len(snapshot.events) == 2
    assert "feedback_recurrence" in rendered
    assert "candidate-eligibility-v3" in rendered
    assert "candidate-aggregation-v1" in rendered
    assert "24h/7d/30d" in rendered
    assert "review_ready" in rendered
    assert "cooldown_gate" in rendered
    assert snapshot.selected.proposal is not None
    assert snapshot.selected.proposal.proposal_kind == "code"
    assert "Proposal Preview" in rendered
    assert "不可执行：否" not in rendered
    assert "可执行：否 · 已入队：否" in rendered
    assert "实验资格" in rendered
    assert "artifact://feedback/" in rendered
    assert "r1 `created`" in rendered
    assert "r2 `evidence_merged`" in rendered
    assert "治理动作由 Workbench 执行" in rendered
    assert "secret-never-render" not in rendered

    missing = await service.detail_snapshot(tmp_path, "evc_" + "0" * 24)
    assert "不存在" in render_evolution_review(missing)


def test_review_filter_rejects_unbounded_or_unknown_values() -> None:
    assert EvolutionReviewFilter(source_kind="rollback_outcome").source_kind == (
        "rollback_outcome"
    )
    with pytest.raises(ValueError, match="risk"):
        EvolutionReviewFilter(risk="urgent")
    with pytest.raises(ValueError, match="source"):
        EvolutionReviewFilter(source_kind="model_claim")
    with pytest.raises(ValueError, match="1..100"):
        EvolutionReviewFilter(limit=101)
    with pytest.raises(ValueError, match="控制字符"):
        EvolutionReviewFilter(query="bad\nquery")
    with pytest.raises(ValueError, match="256"):
        EvolutionReviewFilter(query="x" * 257)


def test_agent_tools_keep_read_and_write_authority_separate(tmp_path: Path) -> None:
    service = EvolutionReviewService(EvolutionCandidateStore(tmp_path / "evolution.db"))
    tools = create_evolution_review_tools(_FakeEngine(tmp_path, service), service)

    assert [tool.name for tool in tools] == [
        "evolution_candidates",
        "evolution_experiment_contract_authority",
        "evolution_issue_experiment_contract",
        "evolution_evaluation_receipt",
        "evolution_evaluation_contract",
        "evolution_final_evaluation_receipt",
        "evolution_decision_input",
        "evolution_mechanical_gate",
        "evolution_independent_review",
        "evolution_counterfactual_evidence",
        "evolution_reward_hacking_evidence",
        "evolution_decision_state",
        "evolution_decision_resolution",
        "evolution_reflection_memory",
        "evolution_revoke_reflection_memory",
        "evolution_promotion_package_input",
        "evolution_promotion_package",
        "evolution_promotion_approval_requirement",
        "evolution_promotion_approval_request",
        "evolution_approval_principal_authority",
        "evolution_approval_principal",
        "evolution_approval_signature_authority",
        "evolution_approval_signature",
        "evolution_approval_decision_authority",
        "evolution_promotion_approval_decision",
        "evolution_revalidation_request_authority",
        "evolution_revalidation_request",
        "evolution_revalidation_replay",
        "evolution_revalidation_validate",
        "evolution_revalidation_outcome",
        "evolution_revalidation_evaluation_plan",
        "evolution_revalidation_evaluation_source",
        "evolution_revalidation_validation_plan",
        "evolution_revalidation_runtime_contract",
        "evolution_revalidation_rollback_execute",
        "evolution_revalidation_rollback_outcome",
        "evolution_proposal_before_after_evidence",
        "evolution_post_rollback_runtime_verification",
        "evolution_post_rollback_behavioral_lane",
        "evolution_post_rollback_behavioral_coverage",
        "evolution_post_rollback_behavioral_matrix",
        "evolution_post_rollback_observation_contract",
        "evolution_post_rollback_runtime_admission",
        "evolution_post_rollback_long_term_assessment",
        "evolution_post_rollback_long_term_outcome",
        "evolution_post_rollback_remote_lane_placement",
        "evolution_post_rollback_target_baseline",
        "evolution_post_rollback_remote_dispatch",
        "evolution_post_rollback_remote_claim",
        "evolution_post_rollback_remote_delivery",
        "evolution_post_rollback_remote_execution",
        "evolution_post_rollback_remote_result",
        "evolution_stable_population_candidate_preview",
        "evolution_stable_population_completion",
        "evolution_stable_rollback_readiness",
        "evolution_installation_key",
        "evolution_rollout_control_key",
        "evolution_stable_remote_readiness_claim",
        "evolution_stable_remote_readiness_probe",
        "evolution_stable_remote_finalization_authorization",
        "evolution_stable_remote_finalization",
        "evolution_stable_remote_population_finalization",
        "evolution_stable_promotion_observation_contract",
        "evolution_stable_promotion_runtime_observation_admission",
        "evolution_stable_rollout_authorization",
        "evolution_stable_rollout_finalization",
        "evolution_discover_outcome_opportunity",
        "evolution_proposal_queue",
    ]
    assert {tool.name for tool in tools if tool.metadata.read_only} == {
        "evolution_candidates",
        "evolution_experiment_contract_authority",
        "evolution_approval_principal_authority",
        "evolution_approval_signature_authority",
        "evolution_approval_decision_authority",
        "evolution_revalidation_request_authority",
        "evolution_stable_population_candidate_preview",
        "evolution_stable_rollback_readiness",
    }
    assert isinstance(tools[1], EvolutionExperimentContractAuthorityTool)
    assert isinstance(tools[2], EvolutionExperimentContractIssueTool)
    assert isinstance(tools[3], EvolutionEvaluationReceiptTool)
    assert isinstance(tools[4], EvolutionEvaluationAggregationContractTool)
    assert isinstance(tools[5], EvolutionFinalEvaluationReceiptTool)
    assert isinstance(tools[6], EvolutionDecisionInputTool)
    assert isinstance(tools[7], EvolutionMechanicalGateTool)
    assert isinstance(tools[8], EvolutionIndependentReviewTool)
    assert isinstance(tools[9], EvolutionCounterfactualEvidenceTool)
    assert isinstance(tools[10], EvolutionRewardHackingEvidenceTool)
    assert isinstance(tools[11], EvolutionDecisionStateTool)
    assert isinstance(tools[12], EvolutionDecisionResolutionTool)
    assert isinstance(tools[13], EvolutionReflectionMemoryTool)
    assert isinstance(tools[14], EvolutionReflectionMemoryRevokeTool)
    assert isinstance(tools[15], EvolutionPromotionPackageInputTool)
    assert isinstance(tools[16], EvolutionPromotionPackageTool)
    assert isinstance(tools[17], EvolutionPromotionApprovalRequirementTool)
    assert isinstance(tools[18], EvolutionPromotionApprovalRequestTool)
    assert isinstance(tools[19], EvolutionApprovalPrincipalAuthorityTool)
    assert isinstance(tools[20], EvolutionApprovalPrincipalTool)
    assert isinstance(tools[21], EvolutionApprovalSignatureAuthorityTool)
    assert isinstance(tools[22], EvolutionApprovalSignatureTool)
    assert isinstance(tools[23], EvolutionApprovalDecisionAuthorityTool)
    assert isinstance(tools[24], EvolutionPromotionApprovalDecisionTool)
    assert isinstance(tools[25], EvolutionRevalidationRequestAuthorityTool)
    assert isinstance(tools[26], EvolutionRevalidationRequestTool)
    assert isinstance(tools[27], EvolutionRevalidationReplayTool)
    assert isinstance(tools[28], EvolutionRevalidationValidationTool)
    assert isinstance(tools[29], EvolutionRevalidationOutcomeTool)
    assert isinstance(tools[30], EvolutionRevalidationEvaluationPlanTool)
    assert isinstance(tools[31], EvolutionRevalidationEvaluationSourceTool)
    assert isinstance(tools[32], EvolutionRevalidationValidationPlanTool)
    assert isinstance(tools[33], EvolutionRevalidationRuntimeContractTool)
    assert isinstance(tools[34], EvolutionRevalidationRollbackExecutionTool)
    assert isinstance(tools[35], EvolutionRevalidationRollbackOutcomeTool)
    assert isinstance(tools[36], EvolutionProposalBeforeAfterEvidenceTool)
    assert isinstance(tools[37], EvolutionPostRollbackRuntimeVerificationTool)
    assert isinstance(tools[38], EvolutionPostRollbackBehavioralLaneTool)
    assert isinstance(tools[39], EvolutionPostRollbackBehavioralCoverageTool)
    assert isinstance(tools[40], EvolutionPostRollbackBehavioralMatrixTool)
    assert isinstance(tools[41], EvolutionPostRollbackLongTermObservationContractTool)
    assert isinstance(tools[42], EvolutionPostRollbackRuntimeObservationAdmissionTool)
    assert isinstance(tools[43], EvolutionPostRollbackLongTermObservationAssessmentTool)
    assert isinstance(tools[44], EvolutionPostRollbackLongTermOutcomeTool)
    assert isinstance(tools[45], EvolutionPostRollbackRemoteLanePlacementTool)
    assert isinstance(tools[46], EvolutionPostRollbackTargetBaselineTool)
    assert isinstance(tools[47], EvolutionPostRollbackRemoteDispatchTool)
    assert isinstance(tools[48], EvolutionPostRollbackRemoteClaimTool)
    assert isinstance(tools[49], EvolutionPostRollbackRemoteDeliveryTool)
    assert isinstance(tools[50], EvolutionPostRollbackRemoteExecutionAuthorizationTool)
    assert isinstance(tools[51], EvolutionPostRollbackRemoteResultTool)
    assert isinstance(tools[52], EvolutionStablePopulationCandidatePreviewTool)
    assert isinstance(tools[53], EvolutionStablePopulationCompletionTool)
    assert isinstance(tools[54], EvolutionStableRollbackReadinessTool)
    assert isinstance(tools[55], EvolutionInstallationKeyTool)
    assert isinstance(tools[56], EvolutionRolloutControlKeyTool)
    assert isinstance(tools[57], EvolutionStableRemoteReadinessClaimTool)
    assert isinstance(tools[58], EvolutionStableRemoteReadinessProbeTool)
    assert isinstance(
        tools[59], EvolutionStableRemoteFinalizationAuthorizationTool
    )
    assert isinstance(tools[60], EvolutionStableRemoteFinalizationTool)
    assert isinstance(tools[61], EvolutionStableRemotePopulationFinalizationTool)
    assert isinstance(tools[62], EvolutionStablePromotionObservationContractTool)
    assert isinstance(
        tools[63], EvolutionStablePromotionRuntimeObservationAdmissionTool
    )
    assert isinstance(tools[64], EvolutionStableRolloutAuthorizationTool)
    assert isinstance(tools[65], EvolutionStableRolloutFinalizationTool)
    assert isinstance(tools[66], EvolutionOutcomeOpportunityTool)
    assert isinstance(tools[67], EvolutionProposalQueueTool)


class _FakeEngine:
    def __init__(self, root: Path, service: EvolutionReviewService) -> None:
        self.workspace_root = root
        self.evolution_review_service = service
        self.router = SimpleNamespace(current_model="openai/test")
        self._session = SimpleNamespace(id="session-review")
        self.evolution_proposal_queue = _FakeQueue()
        self.evolution_promotion_package_input_executor = _FakePromotionInputExecutor()
        self.evolution_promotion_package_executor = _FakePromotionPackageExecutor()
        self.evolution_promotion_approval_requirement_executor = (
            _FakeApprovalRequirementExecutor()
        )


class _FakeQueue:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def enqueue(self, workspace_root: Path, **kwargs):
        self.calls.append({"workspace_root": workspace_root, **kwargs})
        candidate_id = kwargs["candidate_id"]
        return EvolutionProposalQueueResult(
            proposal={
                "id": "proposal-1",
                "source_proposal_id": "evp_" + "a" * 24,
                "source_id": candidate_id,
                "source_revision": 2,
                "proposal_kind": "code",
                "state": "open",
            },
            created=True,
        )


class _FakePromotionInputExecutor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return object()


class _FakePromotionPackageExecutor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return object()


class _FakeApprovalRequirementExecutor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return object()


@pytest.mark.asyncio
async def test_tool_and_slash_share_review_service(tmp_path: Path) -> None:
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    footer_id, _task_id = await _seed(tmp_path, store)
    service = EvolutionReviewService(store)
    engine = _FakeEngine(tmp_path, service)
    tool = EvolutionCandidatesTool(engine, service)

    tool_list = await tool.execute(action="list", query="footer")
    tool_detail = await tool.execute(action="detail", candidate_id=footer_id)
    slash_list = await execute_slash_command(
        engine,
        "/evolution list --source user_feedback --limit 10",
    )
    slash_detail = await execute_slash_command(
        engine,
        f"/evolution detail {footer_id}",
    )

    assert footer_id in tool_list
    assert footer_id in tool_detail
    assert footer_id in slash_list
    assert "审计链" in slash_detail
    assert "Proposal Preview" in tool_detail
    assert "Proposal Preview" in slash_detail
    assert "secret-never-render" not in "\n".join(
        (tool_list, tool_detail, slash_list, slash_detail)
    )


@pytest.mark.asyncio
async def test_tool_errors_are_safe_and_non_mutating(tmp_path: Path) -> None:
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    service = EvolutionReviewService(store)
    tool = EvolutionCandidatesTool(_FakeEngine(tmp_path, service), service)

    assert "用法" in await tool.execute(action="detail")
    assert "仅支持" in await tool.execute(action="approve")
    assert "过滤条件无效" in await tool.execute(action="list", limit=0)
    assert not (tmp_path / "evolution.db").exists()


@pytest.mark.asyncio
async def test_tool_and_slash_share_explicit_queue_adapter(tmp_path: Path) -> None:
    store = EvolutionCandidateStore(tmp_path / "evolution.db")
    footer_id, _task_id = await _seed(tmp_path, store)
    service = EvolutionReviewService(store)
    engine = _FakeEngine(tmp_path, service)
    tool = EvolutionProposalQueueTool(engine)

    tool_result = await tool.execute(
        candidate_id=footer_id,
        mission_id="mission-1",
        task_id="task-1",
    )
    slash_result = await execute_slash_command(
        engine,
        f"/evolution enqueue {footer_id} --mission mission-1 --task task-1",
    )

    assert "仍需人工决定，不可执行" in tool_result
    assert "仍需人工决定，不可执行" in slash_result
    assert len(engine.evolution_proposal_queue.calls) == 2
    assert all(
        call["candidate_id"] == footer_id
        for call in engine.evolution_proposal_queue.calls
    )
    assert EvolutionCandidatesTool(engine, service).metadata.read_only is True
    assert tool.metadata.read_only is False


@pytest.mark.asyncio
async def test_tool_and_slash_share_promotion_input_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = EvolutionReviewService(EvolutionCandidateStore(tmp_path / "evolution.db"))
    engine = _FakeEngine(tmp_path, service)
    reflection_id = f"evreflection_{'a' * 24}"
    monkeypatch.setattr(
        "naumi_agent.evolution.promotion_package_inputs."
        "render_evolution_promotion_package_input",
        lambda _view: "promotion-input-rendered",
    )
    monkeypatch.setattr(
        "naumi_agent.tools.evolution_review.render_evolution_promotion_package_input",
        lambda _view: "promotion-input-rendered",
    )

    tool_result = await EvolutionPromotionPackageInputTool(engine).execute(
        reflection_id=reflection_id
    )
    slash_result = await execute_slash_command(
        engine,
        f"/evolution promotion-input {reflection_id}",
    )

    assert tool_result == "promotion-input-rendered"
    assert "promotion-input-rendered" in slash_result
    assert engine.evolution_promotion_package_input_executor.calls == [
        {"workspace_root": tmp_path, "reflection_id": reflection_id},
        {"workspace_root": tmp_path, "reflection_id": reflection_id},
    ]


@pytest.mark.asyncio
async def test_tool_and_slash_share_promotion_package_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = EvolutionReviewService(EvolutionCandidateStore(tmp_path / "evolution.db"))
    engine = _FakeEngine(tmp_path, service)
    input_id = f"evpromoin_{'b' * 24}"
    monkeypatch.setattr(
        "naumi_agent.evolution.promotion_packages.render_evolution_promotion_package",
        lambda _view: "promotion-package-rendered",
    )
    monkeypatch.setattr(
        "naumi_agent.tools.evolution_review.render_evolution_promotion_package",
        lambda _view: "promotion-package-rendered",
    )

    tool_result = await EvolutionPromotionPackageTool(engine).execute(
        promotion_input_id=input_id,
        target_branch="release/1.0",
    )
    slash_result = await execute_slash_command(
        engine,
        f"/evolution promotion-package {input_id} release/1.0",
    )

    assert tool_result == "promotion-package-rendered"
    assert "promotion-package-rendered" in slash_result
    assert engine.evolution_promotion_package_executor.calls == [
        {
            "workspace_root": tmp_path,
            "promotion_input_id": input_id,
            "target_branch": "release/1.0",
        },
        {
            "workspace_root": tmp_path,
            "promotion_input_id": input_id,
            "target_branch": "release/1.0",
        },
    ]


@pytest.mark.asyncio
async def test_tool_and_slash_share_approval_requirement_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = EvolutionReviewService(EvolutionCandidateStore(tmp_path / "evolution.db"))
    engine = _FakeEngine(tmp_path, service)
    package_id = f"evpromopkg_{'c' * 24}"
    monkeypatch.setattr(
        "naumi_agent.evolution.approval_requirements."
        "render_evolution_promotion_approval_requirement",
        lambda _view: "approval-requirement-rendered",
    )
    monkeypatch.setattr(
        "naumi_agent.tools.evolution_review."
        "render_evolution_promotion_approval_requirement",
        lambda _view: "approval-requirement-rendered",
    )

    tool_result = await EvolutionPromotionApprovalRequirementTool(engine).execute(
        package_id=package_id
    )
    slash_result = await execute_slash_command(
        engine,
        f"/evolution approval-requirement {package_id}",
    )

    assert tool_result == "approval-requirement-rendered"
    assert "approval-requirement-rendered" in slash_result
    assert engine.evolution_promotion_approval_requirement_executor.calls == [
        {"workspace_root": tmp_path, "package_id": package_id},
        {"workspace_root": tmp_path, "package_id": package_id},
    ]
