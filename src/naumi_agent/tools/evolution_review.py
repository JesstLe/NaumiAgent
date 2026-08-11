"""Agent-facing Evolution Candidate review and explicit queue tools."""

from __future__ import annotations

import asyncio
from typing import Any

from naumi_agent.daemons.permission_context import current_permission_receipt
from naumi_agent.evolution.adversarial_batch_requests import (
    EvolutionAdversarialBatchRequest,
)
from naumi_agent.evolution.approval_decisions import (
    EvolutionPromotionApprovalDecisionError,
    render_evolution_promotion_approval_decision,
)
from naumi_agent.evolution.approval_principals import (
    EvolutionApprovalPrincipalError,
    parse_approval_roles,
    render_evolution_approval_principal,
)
from naumi_agent.evolution.approval_requests import (
    EvolutionPromotionApprovalRequestError,
    render_evolution_promotion_approval_response,
)
from naumi_agent.evolution.approval_requirements import (
    EvolutionPromotionApprovalRequirementError,
    render_evolution_promotion_approval_requirement,
)
from naumi_agent.evolution.approval_signatures import (
    EvolutionApprovalSignatureError,
    render_evolution_approval_signature,
)
from naumi_agent.evolution.counterfactual_evidence import (
    EvolutionCounterfactualEvidenceError,
    render_counterfactual_evidence,
)
from naumi_agent.evolution.decision_inputs import (
    EvolutionDecisionInputError,
    render_decision_input,
)
from naumi_agent.evolution.decision_resolutions import (
    EvolutionDecisionResolutionError,
    render_evolution_decision_resolution,
)
from naumi_agent.evolution.decision_states import (
    EvolutionDecisionStateError,
    render_evolution_decision_state,
)
from naumi_agent.evolution.evaluation_aggregation_contracts import (
    EvolutionEvaluationAggregationContractError,
    render_evaluation_aggregation_contract,
)
from naumi_agent.evolution.evaluation_lane_receipts import (
    EvolutionEvaluationLaneReceiptError,
    render_evaluation_lane_receipt,
)
from naumi_agent.evolution.experiments import (
    EvolutionExperimentContractStoreError,
    default_experiment_seed,
    render_experiment_contract_authority,
)
from naumi_agent.evolution.final_evaluation_receipts import (
    EvolutionFinalEvaluationReceiptError,
    render_final_evaluation_receipt,
)
from naumi_agent.evolution.independent_reviews import (
    EvolutionIndependentReviewError,
    render_independent_review,
)
from naumi_agent.evolution.mechanical_gates import (
    EvolutionMechanicalGateError,
    render_mechanical_gate,
)
from naumi_agent.evolution.opportunity_discovery import (
    EvolutionOutcomeOpportunityError,
    render_outcome_opportunity,
)
from naumi_agent.evolution.post_rollback_behavioral_coverage import (
    EvolutionPostRollbackBehavioralCoverageError,
    render_post_rollback_behavioral_coverage,
)
from naumi_agent.evolution.post_rollback_behavioral_lanes import (
    EvolutionPostRollbackBehavioralLaneError,
    render_post_rollback_behavioral_lane,
)
from naumi_agent.evolution.post_rollback_behavioral_matrix import (
    EvolutionPostRollbackBehavioralMatrixError,
    render_post_rollback_behavioral_matrix,
)
from naumi_agent.evolution.post_rollback_long_term_observation_assessments import (
    EvolutionPostRollbackLongTermObservationAssessmentError,
    render_post_rollback_long_term_observation_assessment,
)
from naumi_agent.evolution.post_rollback_long_term_observation_contracts import (
    EvolutionPostRollbackLongTermObservationContractError,
    render_post_rollback_long_term_observation_contract,
)
from naumi_agent.evolution.post_rollback_long_term_outcomes import (
    EvolutionPostRollbackLongTermOutcomeError,
    render_post_rollback_long_term_outcome,
)
from naumi_agent.evolution.post_rollback_remote_claims import (
    EvolutionPostRollbackRemoteClaimError,
    render_post_rollback_remote_claim,
    render_post_rollback_remote_claim_challenge,
)
from naumi_agent.evolution.post_rollback_remote_deliveries import (
    EvolutionPostRollbackRemoteDeliveryError,
    render_post_rollback_remote_delivery,
    render_post_rollback_remote_delivery_offer,
)
from naumi_agent.evolution.post_rollback_remote_dispatches import (
    EvolutionPostRollbackRemoteDispatchError,
    render_post_rollback_remote_dispatch,
)
from naumi_agent.evolution.post_rollback_remote_execution_authorizations import (
    EvolutionPostRollbackRemoteExecutionAuthorizationError,
    render_post_rollback_remote_execution_authorization,
    render_post_rollback_remote_start_challenge,
)
from naumi_agent.evolution.post_rollback_remote_lane_placements import (
    EvolutionPostRollbackRemoteLanePlacementError,
    render_post_rollback_remote_lane_placement,
)
from naumi_agent.evolution.post_rollback_remote_results import (
    MAX_REMOTE_RESULT_MANIFEST_BYTES,
    EvolutionPostRollbackRemoteResultError,
    EvolutionPostRollbackRemoteResultManifest,
    render_post_rollback_remote_result,
)
from naumi_agent.evolution.post_rollback_runtime_observation_admissions import (
    EvolutionPostRollbackRuntimeObservationAdmissionError,
    render_post_rollback_runtime_observation_admission,
)
from naumi_agent.evolution.post_rollback_runtime_verifications import (
    EvolutionPostRollbackRuntimeVerificationError,
    render_post_rollback_runtime_verification,
)
from naumi_agent.evolution.post_rollback_target_baselines import (
    EvolutionPostRollbackTargetBaselineError,
    render_post_rollback_target_baseline,
)
from naumi_agent.evolution.promotion_package_inputs import (
    EvolutionPromotionPackageInputError,
    render_evolution_promotion_package_input,
)
from naumi_agent.evolution.promotion_packages import (
    EvolutionPromotionPackageError,
    render_evolution_promotion_package,
)
from naumi_agent.evolution.proposal_before_after_evidence import (
    EvolutionProposalBeforeAfterEvidenceError,
    render_proposal_before_after_evidence,
)
from naumi_agent.evolution.queue import render_queue_result
from naumi_agent.evolution.reflection_memories import (
    EvolutionReflectionMemoryError,
    EvolutionReflectionRevocationReason,
    render_evolution_reflection_memory,
)
from naumi_agent.evolution.revalidation_evaluation_plans import (
    EvolutionRevalidationEvaluationPlanError,
    render_evolution_revalidation_evaluation_plan,
)
from naumi_agent.evolution.revalidation_evaluation_sources import (
    EvolutionRevalidationEvaluationSourceError,
    render_evolution_revalidation_evaluation_source,
)
from naumi_agent.evolution.revalidation_execution import (
    render_evolution_revalidation_execution,
)
from naumi_agent.evolution.revalidation_outcomes import (
    EvolutionRevalidationOutcomeError,
    render_evolution_revalidation_outcome,
)
from naumi_agent.evolution.revalidation_rebases import EvolutionRevalidationRebaseError
from naumi_agent.evolution.revalidation_replays import EvolutionRevalidationReplayError
from naumi_agent.evolution.revalidation_requests import (
    EvolutionRevalidationRequestError,
    render_evolution_revalidation_request,
)
from naumi_agent.evolution.revalidation_rollback_executions import (
    EvolutionRevalidationRollbackExecutionError,
    render_revalidation_rollback_execution,
)
from naumi_agent.evolution.revalidation_rollback_outcomes import (
    EvolutionRevalidationRollbackOutcomeError,
    render_revalidation_rollback_outcome,
)
from naumi_agent.evolution.revalidation_runtime_contracts import (
    EvolutionRevalidationRuntimeContractError,
    render_evolution_revalidation_runtime_contract,
)
from naumi_agent.evolution.revalidation_validation_plans import (
    EvolutionRevalidationValidationPlanError,
    render_evolution_revalidation_validation_plan,
)
from naumi_agent.evolution.revalidation_validations import (
    EvolutionRevalidationValidationError,
    render_evolution_revalidation_validation,
)
from naumi_agent.evolution.review import (
    EvolutionReviewFilter,
    EvolutionReviewService,
    render_evolution_review,
)
from naumi_agent.evolution.reward_hacking_evidence import (
    EvolutionRewardHackingEvidenceError,
    render_reward_hacking_evidence,
)
from naumi_agent.evolution.stable_population_candidate_previews import (
    EvolutionStablePopulationCandidatePreviewError,
    render_stable_population_candidate_preview,
)
from naumi_agent.evolution.stable_population_completions import (
    EvolutionStablePopulationCompletionError,
    render_stable_population_completion,
)
from naumi_agent.evolution.stable_promotion_observation_contracts import (
    EvolutionStablePromotionObservationContractError,
    render_stable_promotion_observation_contract,
)
from naumi_agent.evolution.stable_remote_finalization_authorizations import (
    EvolutionStableRemoteFinalizationAuthorizationError,
    render_stable_remote_finalization_authorization,
)
from naumi_agent.evolution.stable_remote_finalization_deliveries import (
    EvolutionStableRemoteFinalizationDeliveryError,
    EvolutionStableRemoteFinalizationTargetJournal,
    decode_stable_remote_finalization_delivery_package,
    encode_stable_remote_finalization_delivery_ack,
    render_stable_remote_finalization_delivery,
)
from naumi_agent.evolution.stable_remote_finalization_delivery_worker import (
    render_stable_remote_finalization_delivery_pass,
    render_stable_remote_finalization_delivery_worker,
)
from naumi_agent.evolution.stable_remote_finalization_installation_daemon import (
    render_stable_remote_finalization_installation_daemon,
)
from naumi_agent.evolution.stable_remote_finalization_result_return_worker import (
    render_stable_remote_finalization_result_return_pass,
    render_stable_remote_finalization_result_return_worker,
)
from naumi_agent.evolution.stable_remote_finalizations import (
    EvolutionStableRemoteFinalizationError,
    decode_stable_remote_finalization_execution_package,
    execute_stable_remote_finalization,
    render_stable_remote_finalization,
    render_stable_remote_finalization_submission,
)
from naumi_agent.evolution.stable_remote_population_finalizations import (
    EvolutionStableRemotePopulationFinalizationError,
    render_stable_remote_population_finalization,
)
from naumi_agent.evolution.stable_remote_readiness_claims import (
    EvolutionStableRemoteReadinessClaimError,
    render_stable_remote_readiness_challenge,
    render_stable_remote_readiness_claim,
)
from naumi_agent.evolution.stable_remote_readiness_probes import (
    EvolutionStableRemoteReadinessProbeError,
    decode_stable_remote_readiness_probe_challenge,
    execute_stable_remote_readiness_probe,
    render_stable_remote_readiness_probe,
    render_stable_remote_readiness_probe_challenge,
    render_stable_remote_readiness_probe_submission,
)
from naumi_agent.evolution.stable_rollback_readiness import (
    EvolutionStableRollbackReadinessError,
    render_stable_rollback_readiness,
)
from naumi_agent.evolution.stable_rollout_authorizations import (
    EvolutionStableRolloutAuthorizationError,
    render_stable_rollout_authorization,
)
from naumi_agent.evolution.stable_rollout_finalizations import (
    EvolutionStableRolloutFinalizationError,
    render_stable_rollout_finalization,
)
from naumi_agent.evolution.store import EvolutionStoreError
from naumi_agent.release.installation_keys import (
    ReleaseInstallationKeyError,
    render_release_installation_key,
)
from naumi_agent.release.rollout_control_keys import (
    ReleaseRolloutControlKeyError,
    load_release_rollout_control_trust_policy,
    render_release_rollout_control_key,
)
from naumi_agent.tools.base import Tool, ToolMetadata


class EvolutionCandidatesTool(Tool):
    def __init__(self, engine: Any, service: EvolutionReviewService) -> None:
        self._engine = engine
        self._service = service

    @property
    def name(self) -> str:
        return "evolution_candidates"

    @property
    def description(self) -> str:
        return (
            "只读列出或查看当前工作区的 Evolution Candidate。"
            "显示来源、风险、频次、机械指标和审计链，不批准实验或修改代码。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "detail"],
                    "default": "list",
                },
                "candidate_id": {"type": "string"},
                "query": {"type": "string"},
                "risk": {"type": "string", "enum": ["", "low", "medium", "high", "critical"]},
                "source_kind": {
                    "type": "string",
                    "enum": [
                        "",
                        "harness_failure",
                        "self_review_static",
                        "user_feedback",
                        "agent_interpreted_feedback",
                        "rollback_outcome",
                    ],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            },
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="Evolution 候选审查",
            search_hint="evolution candidates review evidence risk 自进化 候选 审查",
        )

    async def execute(
        self,
        action: str = "list",
        candidate_id: str = "",
        query: str = "",
        risk: str = "",
        source_kind: str = "",
        limit: int = 50,
    ) -> str:
        try:
            if action == "detail":
                if not candidate_id.strip():
                    return "用法：evolution_candidates(action='detail', candidate_id='<id>')"
                snapshot = await self._service.detail_snapshot(
                    self._engine.workspace_root,
                    candidate_id.strip(),
                )
            elif action == "list":
                snapshot = await self._service.list_snapshot(
                    self._engine.workspace_root,
                    filters=EvolutionReviewFilter(
                        query=query.strip(),
                        risk=risk.strip(),
                        source_kind=source_kind.strip(),
                        limit=limit,
                    ),
                )
            else:
                return "action 仅支持 list 或 detail。"
        except (EvolutionStoreError, OSError, ValueError):
            return "Evolution Candidate 状态库不可读，或过滤条件无效。请运行 /doctor。"
        return render_evolution_review(snapshot)


class EvolutionOutcomeOpportunityTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_discover_outcome_opportunity"

    @property
    def description(self) -> str:
        return (
            "把一个当前有效的 rolled_back Outcome 确定性回注为下一轮 "
            "Evolution Candidate。实时重验来源、去重并保留审计引用；"
            "不读取源码，不生成补丁，也不授予实验或推广权限。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "outcome_id": {
                    "type": "string",
                    "pattern": "^evrerollbackout_[0-9a-f]{24}$",
                },
            },
            "required": ["outcome_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            user_facing_name="Outcome 机会发现",
            search_hint=(
                "evolution outcome opportunity discovery feedback loop "
                "自进化 结果 回注 机会发现"
            ),
        )

    async def execute(self, outcome_id: str) -> str:
        try:
            result = await self._engine.evolution_outcome_opportunity_service.discover(
                outcome_id=outcome_id
            )
        except (
            EvolutionOutcomeOpportunityError,
            EvolutionStoreError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "outcome_opportunity_failed")
            return f"Outcome 机会发现未完成（`{code}`）：{exc}"
        return render_outcome_opportunity(result)


class EvolutionExperimentContractAuthorityTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_experiment_contract_authority"

    @property
    def description(self) -> str:
        return (
            "从 durable Store 只读重载当前工作区的 Experiment Contract Authority，"
            "查看已批准 scope、文件、预算、检查与禁用能力；不签发执行或推广许可。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "contract_id": {
                    "type": "string",
                    "pattern": "^evx_[0-9a-f]{24}$",
                },
            },
            "required": ["contract_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="Evolution 实验约束 Authority",
            search_hint=(
                "evolution experiment contract authority scope budget constraints "
                "自进化 实验 合同 约束 预算"
            ),
        )

    async def execute(self, contract_id: str) -> str:
        try:
            authority = await self._engine.evolution_experiment_contract_store.get(
                self._engine.workspace_root,
                contract_id.strip(),
            )
        except (
            EvolutionExperimentContractStoreError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            return "Experiment Contract Authority 不可读取；请运行 /doctor 后重试。"
        if authority is None:
            return "当前工作区不存在该 Experiment Contract Authority。"
        return render_experiment_contract_authority(authority)


class EvolutionExperimentContractIssueTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_issue_experiment_contract"

    @property
    def description(self) -> str:
        return (
            "把当前会话中一个已由用户批准的 Evolution Proposal 显式转换为 durable "
            "Experiment Contract。该操作只冻结 baseline、scope、预算和验证约束，"
            "不修改代码、不执行实验，也不授予发布权限。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "proposal_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
            },
            "required": ["proposal_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="签发 Evolution 实验契约",
            search_hint=(
                "evolution issue experiment contract approved proposal scope budget "
                "自进化 签发 实验 契约"
            ),
        )

    async def execute(self, proposal_id: str) -> str:
        session = getattr(self._engine, "_session", None)
        session_id = str(getattr(session, "id", "") or "").strip()
        if not session_id:
            return "当前没有活动会话，无法绑定 Experiment Contract。"
        clean_proposal = proposal_id.strip()
        try:
            contract = await self._engine.evolution_experiment_contract_issuer.issue(
                self._engine.workspace_root,
                session_id=session_id,
                proposal_id=clean_proposal,
                seed=default_experiment_seed(clean_proposal),
            )
            authority = await self._engine.evolution_experiment_contract_store.get(
                self._engine.workspace_root,
                contract.contract_id,
            )
        except (EvolutionExperimentContractStoreError, OSError, TypeError, ValueError):
            return (
                "Experiment Contract 未签发：Proposal 未批准、会话绑定无效，"
                "或 authority 状态库不可用。"
            )
        if authority is None:
            return "Experiment Contract 未签发：authority 未持久化，请运行 /doctor。"
        return render_experiment_contract_authority(authority)


class EvolutionProposalQueueTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_proposal_queue"

    @property
    def description(self) -> str:
        return (
            "把一个 review-ready Evolution Candidate 显式加入 Workbench 审阅队列。"
            "该操作只创建等待人工决定的 Proposal，不执行实验或修改代码。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string"},
                "mission_id": {"type": "string"},
                "task_id": {"type": "string"},
                "agent_id": {"type": "string", "default": "Evolution-Agent"},
            },
            "required": ["candidate_id", "mission_id", "task_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution Proposal 入队",
            search_hint="evolution proposal queue workbench review 自进化 提案 入队",
        )

    async def execute(
        self,
        candidate_id: str,
        mission_id: str,
        task_id: str,
        agent_id: str = "Evolution-Agent",
    ) -> str:
        try:
            session = getattr(self._engine, "_session", None)
            if session is None:
                return "当前没有活动会话，无法绑定 Workbench Proposal。"
            result = await self._engine.evolution_proposal_queue.enqueue(
                self._engine.workspace_root,
                session_id=session.id,
                mission_id=mission_id,
                task_id=task_id,
                agent_id=agent_id,
                candidate_id=candidate_id,
            )
        except (EvolutionStoreError, OSError, ValueError):
            return "Proposal 未入队：Candidate 未就绪、绑定无效或用户状态库不可用。"
        return render_queue_result(result)


class EvolutionEvaluationReceiptTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_evaluation_receipt"

    @property
    def description(self) -> str:
        return (
            "根据当前工作区已有的 H5c Comparison 与 Failure Attribution，"
            "签发并显示一个防篡改 Evaluation Lane Receipt。"
            "该收据明确保持非最终状态，不能代替跨 lane 与跨平台聚合。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "comparison_id": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
            },
            "required": ["comparison_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution 单 Lane 回执",
            search_hint=(
                "evolution evaluation receipt H5c attribution before after "
                "自进化 评测 回执"
            ),
        )

    async def execute(self, comparison_id: str) -> str:
        try:
            receipt = await (
                self._engine.evolution_evaluation_lane_receipt_executor.execute_by_id(
                    workspace_root=self._engine.workspace_root,
                    comparison_id=comparison_id.strip(),
                )
            )
        except (EvolutionEvaluationLaneReceiptError, OSError, ValueError) as exc:
            return f"Evaluation Lane Receipt 未签发：{exc}"
        return render_evaluation_lane_receipt(receipt)


class EvolutionEvaluationAggregationContractTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_evaluation_contract"

    @property
    def description(self) -> str:
        return (
            "把一个防篡改 Adversarial Batch Request 注册为最终 Evaluation 的覆盖合同。"
            "合同冻结必需平台和 RED/GREEN lane，但不会签发最终回执或批准候选。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "batch_request": {
                    "type": "object",
                    "description": "完整 EvolutionAdversarialBatchRequest JSON。",
                },
            },
            "required": ["batch_request"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution 最终评测覆盖合同",
            search_hint=(
                "evolution evaluation aggregation contract platforms lanes "
                "自进化 最终评测 聚合 合同"
            ),
        )

    async def execute(self, batch_request: dict[str, Any]) -> str:
        try:
            request = EvolutionAdversarialBatchRequest.model_validate(batch_request)
            artifact = await (
                self._engine.evolution_evaluation_aggregation_contract_issuer.issue(
                    workspace_root=self._engine.workspace_root,
                    batch_request=request,
                )
            )
        except (
            EvolutionEvaluationAggregationContractError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evaluation Aggregation Contract 未签发：{exc}"
        return render_evaluation_aggregation_contract(artifact)


class EvolutionFinalEvaluationReceiptTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_final_evaluation_receipt"

    @property
    def description(self) -> str:
        return (
            "从 durable Store 重读 Aggregation Contract、一个 Interventional lane、"
            "全部必需平台的 Adversarial lane 与 completion receipts，签发最终评测回执。"
            "回执只完成证据聚合，不接受候选或批准发布。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "aggregation_contract_id": {
                    "type": "string",
                    "pattern": "^evagg_[0-9a-f]{24}$",
                },
                "interventional_comparison_id": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "adversarial_comparison_ids": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "minItems": 1,
                    "maxItems": 3,
                    "uniqueItems": True,
                },
            },
            "required": [
                "aggregation_contract_id",
                "interventional_comparison_id",
                "adversarial_comparison_ids",
            ],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution 最终评测回执",
            search_hint=(
                "evolution final evaluation receipt aggregate platforms lanes "
                "自进化 最终评测 回执 聚合"
            ),
        )

    async def execute(
        self,
        aggregation_contract_id: str,
        interventional_comparison_id: str,
        adversarial_comparison_ids: list[str],
    ) -> str:
        try:
            receipt = await (
                self._engine.evolution_final_evaluation_receipt_executor.execute(
                    aggregation_contract_id=aggregation_contract_id.strip(),
                    interventional_comparison_id=interventional_comparison_id.strip(),
                    adversarial_comparison_ids=tuple(
                        item.strip() for item in adversarial_comparison_ids
                    ),
                )
            )
        except (EvolutionFinalEvaluationReceiptError, OSError, TypeError, ValueError) as exc:
            return f"Final Evaluation Receipt 未签发：{exc}"
        return render_final_evaluation_receipt(receipt)


class EvolutionDecisionInputTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_decision_input"

    @property
    def description(self) -> str:
        return (
            "仅凭 Final Evaluation Receipt ID，从四个 durable Store 重读 Candidate、"
            "Mutation、Experiment constraints 与完整评测证据，冻结防篡改 Decision Input。"
            "该工具不执行 mechanical gate，不接受候选，也不批准发布。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "final_evaluation_receipt_id": {
                    "type": "string",
                    "pattern": "^evfinal_[0-9a-f]{24}$",
                },
            },
            "required": ["final_evaluation_receipt_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution 决策输入",
            search_hint=(
                "evolution decision input candidate mutation constraints final receipt "
                "自进化 决策 输入 证据 约束"
            ),
        )

    async def execute(self, final_evaluation_receipt_id: str) -> str:
        try:
            artifact = await self._engine.evolution_decision_input_executor.execute(
                workspace_root=self._engine.workspace_root,
                final_evaluation_receipt_id=final_evaluation_receipt_id.strip(),
            )
        except (EvolutionDecisionInputError, OSError, TypeError, ValueError) as exc:
            return f"Evolution Decision Input 未冻结：{exc}"
        return render_decision_input(artifact)


class EvolutionMechanicalGateTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_mechanical_gate"

    @property
    def description(self) -> str:
        return (
            "从 durable Store 重读一个 Decision Input 及其签名引用的 Mutation Trace，"
            "机械复核 scope、guardrails、files/lines/tool calls/duration/attempt "
            "预算与完整评测事实，"
            "产生不可被 LLM 覆盖的 pass/veto。该结果仍不是 Candidate 接受或发布决定。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "decision_input_id": {
                    "type": "string",
                    "pattern": "^evdin_[0-9a-f]{24}$",
                },
            },
            "required": ["decision_input_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution 机械门禁",
            search_hint=(
                "evolution mechanical gate pass veto scope budget rerun fault "
                "自进化 机械 门禁 否决"
            ),
        )

    async def execute(self, decision_input_id: str) -> str:
        try:
            gate = await self._engine.evolution_mechanical_gate_executor.execute(
                workspace_root=self._engine.workspace_root,
                decision_input_id=decision_input_id.strip(),
            )
        except (EvolutionMechanicalGateError, OSError, TypeError, ValueError) as exc:
            return f"Evolution Mechanical Gate 未签发：{exc}"
        return render_mechanical_gate(gate)


class EvolutionIndependentReviewTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_independent_review"

    @property
    def description(self) -> str:
        return (
            "仅凭 Mechanical Gate ID 重读 Gate 与 Trace-bound Mutation Author Receipt。"
            "机械通过时使用与 author canonical identity 不同的模型产生严格结构化 advisory；"
            "机械否决时不调用模型，只返回不可覆盖的 veto 与 required actions。"
            "该工具不接受 Candidate，也不批准发布。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "gate_id": {
                    "type": "string",
                    "pattern": "^evgate_[0-9a-f]{24}$",
                },
                "reviewer_model": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "default": "",
                    "description": "可选独立 Reviewer model；空值使用 reasoning tier。",
                },
            },
            "required": ["gate_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution 独立审查",
            search_hint=(
                "evolution independent reviewer author identity advisory gate "
                "自进化 独立 审查 模型 隔离"
            ),
        )

    async def execute(self, gate_id: str, reviewer_model: str = "") -> str:
        try:
            review = await self._engine.evolution_independent_review_executor.execute(
                workspace_root=self._engine.workspace_root,
                gate_id=gate_id.strip(),
                reviewer_model=reviewer_model.strip() or None,
            )
        except (EvolutionIndependentReviewError, OSError, TypeError, ValueError) as exc:
            return f"Evolution Independent Review 未完成：{exc}"
        return render_independent_review(review)


class EvolutionCounterfactualEvidenceTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_counterfactual_evidence"

    @property
    def description(self) -> str:
        return (
            "仅凭 completed Independent Review ID，从 durable Store 重读 Review、Gate、"
            "Decision Input、Mutation Receipt、Experiment Contract 与 Lease，验证真实"
            "受管 worktree 的 baseline/candidate 字节和 diff 摘要，并机械寻找更小 scope、"
            "删测试、修改 metric、放宽阈值、skip/mock 与评测泄漏。"
            "不调用 LLM，不接受 Candidate，也不批准发布。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "review_id": {
                    "type": "string",
                    "pattern": "^evreview_[0-9a-f]{24}$",
                },
            },
            "required": ["review_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution 反事实证据",
            search_hint=(
                "evolution counterfactual smaller scope test deletion metric threshold "
                "skip mock leakage 自进化 反事实 替代解释"
            ),
        )

    async def execute(self, review_id: str) -> str:
        try:
            artifact = await (
                self._engine.evolution_counterfactual_evidence_executor.execute(
                    workspace_root=self._engine.workspace_root,
                    review_id=review_id.strip(),
                )
            )
        except (
            EvolutionCounterfactualEvidenceError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Counterfactual Evidence 未完成：{exc}"
        return render_counterfactual_evidence(artifact)


class EvolutionRewardHackingEvidenceTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_reward_hacking_evidence"

    @property
    def description(self) -> str:
        return (
            "仅凭 Counterfactual Evidence ID，从 durable Store 重读 Counterfactual、"
            "Final Evaluation、全部 Lane 与 Adversarial Cohort authority，确定性检查"
            "真实任务退化、proxy divergence、平台选择性以及 duration/token/cost 资源"
            "换分。证据缺失返回 inconclusive；不调用 LLM，不接受 Candidate。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "counterfactual_evidence_id": {
                    "type": "string",
                    "pattern": "^evcounter_[0-9a-f]{24}$",
                },
            },
            "required": ["counterfactual_evidence_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution 奖励投机证据",
            search_hint=(
                "evolution reward hacking proxy gaming task degradation platform "
                "selectivity resource inflation 自进化 奖励投机 资源换分"
            ),
        )

    async def execute(self, counterfactual_evidence_id: str) -> str:
        try:
            artifact = await (
                self._engine.evolution_reward_hacking_evidence_executor.execute(
                    workspace_root=self._engine.workspace_root,
                    counterfactual_evidence_id=counterfactual_evidence_id.strip(),
                )
            )
        except (
            AttributeError,
            EvolutionRewardHackingEvidenceError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Reward-hacking Evidence 未完成：{exc}"
        return render_reward_hacking_evidence(artifact)


class EvolutionDecisionStateTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_decision_state"

    @property
    def description(self) -> str:
        return (
            "仅凭 Decision Input ID，从 durable Store 重读 Mechanical Gate、Independent "
            "Review、Counterfactual 与 Reward-hacking authority，按固定优先级形成 "
            "accepted_experiment/revise/rejected/escalated。Reviewer 仅为 advisory；"
            "escalated 生成兼容 New UI/TUI 的选项与自定义输入，不执行 promotion。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "decision_input_id": {
                    "type": "string",
                    "pattern": "^evdin_[0-9a-f]{24}$",
                },
            },
            "required": ["decision_input_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution 最终决策状态",
            search_hint=(
                "evolution decision state accept revise reject escalate user choice "
                "自进化 最终决策 用户选择"
            ),
        )

    async def execute(self, decision_input_id: str) -> str:
        try:
            artifact = await self._engine.evolution_decision_state_executor.execute(
                workspace_root=self._engine.workspace_root,
                decision_input_id=decision_input_id.strip(),
            )
        except (
            AttributeError,
            EvolutionDecisionStateError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Decision State 未完成：{exc}"
        return render_evolution_decision_state(artifact)


class EvolutionDecisionResolutionTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_decision_resolution"

    @property
    def description(self) -> str:
        return (
            "仅对 escalated Evolution Decision State 发起持久用户交互。问题先写入 "
            "Harness authority，option/custom 答案经 owner/epoch/sequence fencing 后才形成"
            "不可变 Resolution。结果只能要求补证据、人工审查、修订、拒绝或受约束的"
            "自定义后续动作，不接受 Candidate，也不执行 promotion。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "decision_input_id": {
                    "type": "string",
                    "pattern": "^evdin_[0-9a-f]{24}$",
                },
            },
            "required": ["decision_input_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution 用户决策回执",
            search_hint=(
                "evolution escalation resolution durable user interaction answer "
                "自进化 用户选择 决策回执"
            ),
        )

    async def execute(self, decision_input_id: str) -> str:
        try:
            artifact = await self._engine.evolution_decision_resolution_service.execute(
                workspace_root=self._engine.workspace_root,
                decision_input_id=decision_input_id.strip(),
            )
        except (
            AttributeError,
            EvolutionDecisionResolutionError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Escalation Resolution 未完成：{exc}"
        return render_evolution_decision_resolution(artifact)


class EvolutionReflectionMemoryTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_reflection_memory"

    @property
    def description(self) -> str:
        return (
            "从不可变 Evolution Decision State 和可选用户 Resolution 派生结构化反思记忆。"
            "只保存枚举信号与 authority ID/digest，不保存 Reviewer 叙事、用户自定义文本或源码，"
            "不写向量库、不自动召回、不注入系统 Prompt，也不执行 promotion。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "decision_input_id": {
                    "type": "string",
                    "pattern": "^evdin_[0-9a-f]{24}$",
                },
            },
            "required": ["decision_input_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution 结构化反思记忆",
            search_hint=(
                "evolution reflection memory structured lesson evidence refs "
                "自进化 反思 结构化经验"
            ),
        )

    async def execute(self, decision_input_id: str) -> str:
        try:
            view = await self._engine.evolution_reflection_memory_executor.execute(
                workspace_root=self._engine.workspace_root,
                decision_input_id=decision_input_id.strip(),
            )
        except (
            AttributeError,
            EvolutionReflectionMemoryError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Reflection Memory 未完成：{exc}"
        return render_evolution_reflection_memory(view)


class EvolutionReflectionMemoryRevokeTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_revoke_reflection_memory"

    @property
    def description(self) -> str:
        return (
            "以 append-only 撤销回执停用一条 Evolution Reflection Memory。"
            "不会删除原证据；revoked 记录只保留审计，不参与后续 policy learning 或 promotion。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "reflection_id": {
                    "type": "string",
                    "pattern": "^evreflection_[0-9a-f]{24}$",
                },
                "reason": {
                    "type": "string",
                    "enum": [item.value for item in EvolutionReflectionRevocationReason],
                },
            },
            "required": ["reflection_id", "reason"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="撤销 Evolution 反思记忆",
            search_hint="evolution revoke reflection memory 撤销 反思记忆",
        )

    async def execute(self, reflection_id: str, reason: str) -> str:
        try:
            view = await self._engine.evolution_reflection_memory_revoker.execute(
                workspace_root=self._engine.workspace_root,
                reflection_id=reflection_id.strip(),
                reason=reason.strip(),
            )
        except (
            AttributeError,
            EvolutionReflectionMemoryError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Reflection Memory 撤销未完成：{exc}"
        return render_evolution_reflection_memory(view)


class EvolutionPromotionPackageInputTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_promotion_package_input"

    @property
    def description(self) -> str:
        return (
            "从 accepted_experiment 且仍 active 的 Reflection 冻结 Promotion Package 输入。"
            "包含 patch manifest、baseline、完整 receipt 引用、迁移评估和 rollback plan；"
            "不审批、不写 Git、不合并、不推送也不发布。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "reflection_id": {
                    "type": "string",
                    "pattern": "^evreflection_[0-9a-f]{24}$",
                },
            },
            "required": ["reflection_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution Promotion 输入",
            search_hint=(
                "evolution promotion package input patch baseline receipts migration "
                "rollback 提升 发布 输入 回滚"
            ),
        )

    async def execute(self, reflection_id: str) -> str:
        try:
            view = await self._engine.evolution_promotion_package_input_executor.execute(
                workspace_root=self._engine.workspace_root,
                reflection_id=reflection_id.strip(),
            )
        except (
            AttributeError,
            EvolutionPromotionPackageInputError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Promotion Package Input 未完成：{exc}"
        return render_evolution_promotion_package_input(view)


class EvolutionPromotionPackageTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_promotion_package"

    @property
    def description(self) -> str:
        return (
            "从仍 eligible 的 Promotion Package Input 冻结完整审查 Package，"
            "绑定 exact local target branch、审批事实与可签名摘要域。"
            "只读 Git，不审批、不签名、不 rebase、不合并、不推送也不发布。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "promotion_input_id": {
                    "type": "string",
                    "pattern": "^evpromoin_[0-9a-f]{24}$",
                },
                "target_branch": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 255,
                    "default": "main",
                },
            },
            "required": ["promotion_input_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution Promotion Package",
            search_hint=(
                "evolution promotion package target branch signature approval "
                "提升 发布 审批 签名 目标分支"
            ),
        )

    async def execute(
        self,
        promotion_input_id: str,
        target_branch: str = "main",
    ) -> str:
        try:
            view = await self._engine.evolution_promotion_package_executor.execute(
                workspace_root=self._engine.workspace_root,
                promotion_input_id=promotion_input_id.strip(),
                target_branch=target_branch.strip(),
            )
        except (
            AttributeError,
            EvolutionPromotionPackageError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Promotion Package 未完成：{exc}"
        return render_evolution_promotion_package(view)


class EvolutionPromotionApprovalRequirementTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_promotion_approval_requirement"

    @property
    def description(self) -> str:
        return (
            "从 still-current Promotion Package 计算并冻结审批角色、签名门、"
            "技术前置条件与有效期。该工具不创建用户交互、不批准、不签名、"
            "不写 Git，也不执行 Promotion。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "package_id": {
                    "type": "string",
                    "pattern": "^evpromopkg_[0-9a-f]{24}$",
                },
            },
            "required": ["package_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution 审批要求",
            search_hint=(
                "evolution promotion approval requirement roles signatures expiry "
                "提升 审批 要求 角色 签名 有效期"
            ),
        )

    async def execute(self, package_id: str) -> str:
        try:
            view = await (
                self._engine.evolution_promotion_approval_requirement_executor.execute(
                    workspace_root=self._engine.workspace_root,
                    package_id=package_id.strip(),
                )
            )
        except (
            AttributeError,
            EvolutionPromotionApprovalRequirementError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Approval Requirement 未完成：{exc}"
        return render_evolution_promotion_approval_requirement(view)


class EvolutionPromotionApprovalRequestTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_promotion_approval_request"

    @property
    def description(self) -> str:
        return (
            "为 still-eligible Approval Requirement 的一个必需角色创建 HAR-10.6 "
            "持久审批交互，并把 fenced 选项答案冻结为角色回执。该工具不聚合最终审批、"
            "不代替身份或签名验证、不写 Git，也不执行 Promotion。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "requirement_id": {
                    "type": "string",
                    "pattern": "^evapprovalreq_[0-9a-f]{24}$",
                },
                "role": {
                    "type": "string",
                    "enum": [
                        "user",
                        "independent_reviewer",
                        "security_reviewer",
                        "data_owner",
                        "release_manager",
                    ],
                },
            },
            "required": ["requirement_id", "role"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution 角色审批请求",
            search_hint=(
                "evolution promotion approval request response role interaction "
                "提升 审批 请求 回答 角色 交互"
            ),
        )

    async def execute(self, requirement_id: str, role: str) -> str:
        try:
            view = await self._engine.evolution_promotion_approval_request_service.execute(
                workspace_root=self._engine.workspace_root,
                requirement_id=requirement_id.strip(),
                role=role.strip(),
            )
        except (
            AttributeError,
            EvolutionPromotionApprovalRequestError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Approval Request 未完成：{exc}"
        return render_evolution_promotion_approval_response(view)


class EvolutionApprovalPrincipalTool(Tool):
    """Govern trusted approval identities without ever accepting private keys."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_approval_principal"

    @property
    def description(self) -> str:
        return (
            "通过 HAR 持久人工确认注册、轮换、更新角色或撤销 Evolution 审批主体。"
            "仅接收 Ed25519 公钥，绝不请求或保存私钥；"
            "该工具不产生审批决定、Promotion 或 Git 写权限。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["register", "rotate_key", "update_roles", "revoke"],
                },
                "principal_name": {"type": "string", "pattern": "^[a-z][a-z0-9._-]{1,63}$"},
                "principal_id": {"type": "string", "pattern": "^evprincipal_[0-9a-f]{24}$"},
                "public_key_base64": {"type": "string", "minLength": 44, "maxLength": 44},
                "roles": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "user",
                            "independent_reviewer",
                            "security_reviewer",
                            "data_owner",
                            "release_manager",
                        ],
                    },
                    "minItems": 1,
                    "maxItems": 5,
                    "uniqueItems": True,
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution 审批主体治理",
            search_hint=(
                "evolution approval principal identity public key ed25519 rotate revoke "
                "自进化 审批 主体 身份 公钥 轮换 撤销"
            ),
        )

    async def execute(
        self,
        action: str,
        principal_name: str = "",
        principal_id: str = "",
        public_key_base64: str = "",
        roles: list[str] | None = None,
    ) -> str:
        service = self._engine.evolution_approval_principal_service
        try:
            if action == "register":
                if not principal_name or not public_key_base64 or not roles:
                    return "register 需要 principal_name、public_key_base64 和 roles。"
                result = await service.register(
                    workspace_root=self._engine.workspace_root,
                    principal_name=principal_name,
                    public_key_base64=public_key_base64,
                    roles=parse_approval_roles(roles),
                )
            elif action == "rotate_key":
                if not principal_id or not public_key_base64:
                    return "rotate_key 需要 principal_id 和 public_key_base64。"
                result = await service.rotate_key(
                    workspace_root=self._engine.workspace_root,
                    principal_id=principal_id,
                    public_key_base64=public_key_base64,
                )
            elif action == "update_roles":
                if not principal_id or not roles:
                    return "update_roles 需要 principal_id 和 roles。"
                result = await service.update_roles(
                    workspace_root=self._engine.workspace_root,
                    principal_id=principal_id,
                    roles=parse_approval_roles(roles),
                )
            elif action == "revoke":
                if not principal_id:
                    return "revoke 需要 principal_id。"
                result = await service.revoke(
                    workspace_root=self._engine.workspace_root,
                    principal_id=principal_id,
                )
            else:
                return "action 仅支持 register、rotate_key、update_roles 或 revoke。"
        except (
            AttributeError,
            EvolutionApprovalPrincipalError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Approval Principal 未完成：{exc}"
        return render_evolution_approval_principal(result)


class EvolutionApprovalPrincipalAuthorityTool(Tool):
    """Read one current approval principal projection without changing authority."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_approval_principal_authority"

    @property
    def description(self) -> str:
        return (
            "只读重载当前工作区的 Evolution Approval Principal、角色、当前 Ed25519 "
            "公钥指纹、generation 与撤销状态；不请求私钥、不创建交互、不修改 authority。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "principal_id": {
                    "type": "string",
                    "pattern": "^evprincipal_[0-9a-f]{24}$",
                },
            },
            "required": ["principal_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="Evolution 审批主体 Authority",
            search_hint=(
                "evolution approval principal authority identity public key status "
                "自进化 审批 主体 身份 公钥 状态"
            ),
        )

    async def execute(self, principal_id: str) -> str:
        try:
            view = await self._engine.evolution_approval_principal_service.inspect(
                workspace_root=self._engine.workspace_root,
                principal_id=principal_id.strip(),
            )
        except (
            AttributeError,
            EvolutionApprovalPrincipalError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Approval Principal Authority 不可读取：{exc}"
        return render_evolution_approval_principal(view)


class EvolutionApprovalSignatureTool(Tool):
    """Prepare and verify exact external Ed25519 approval signatures."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_approval_signature"

    @property
    def description(self) -> str:
        return (
            "为需要签名的 approve Role Response 创建带 nonce/expiry 的 canonical Challenge，"
            "或提交外部 Ed25519 signature 并形成验证回执。Naumi 不接收私钥；"
            "回执不聚合最终审批、不执行 Promotion 或 Git。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["prepare", "submit"]},
                "approval_response_id": {
                    "type": "string",
                    "pattern": "^evapprovalresp_[0-9a-f]{24}$",
                },
                "principal_id": {
                    "type": "string",
                    "pattern": "^evprincipal_[0-9a-f]{24}$",
                },
                "challenge_id": {
                    "type": "string",
                    "pattern": "^evsigchallenge_[0-9a-f]{24}$",
                },
                "signature_base64": {
                    "type": "string",
                    "minLength": 88,
                    "maxLength": 88,
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution 审批签名",
            search_hint=(
                "evolution approval signature ed25519 challenge verify response "
                "自进化 审批 签名 挑战 验证 回执"
            ),
        )

    async def execute(
        self,
        action: str,
        approval_response_id: str = "",
        principal_id: str = "",
        challenge_id: str = "",
        signature_base64: str = "",
    ) -> str:
        service = self._engine.evolution_approval_signature_service
        try:
            if action == "prepare":
                if not approval_response_id or not principal_id:
                    return "prepare 需要 approval_response_id 和 principal_id。"
                result = await service.prepare(
                    workspace_root=self._engine.workspace_root,
                    approval_response_id=approval_response_id,
                    principal_id=principal_id,
                )
            elif action == "submit":
                if not challenge_id or not signature_base64:
                    return "submit 需要 challenge_id 和 signature_base64。"
                result = await service.submit(
                    workspace_root=self._engine.workspace_root,
                    challenge_id=challenge_id,
                    signature_base64=signature_base64,
                )
            else:
                return "action 仅支持 prepare 或 submit。"
        except (
            AttributeError,
            EvolutionApprovalSignatureError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Approval Signature 未完成：{exc}"
        return render_evolution_approval_signature(result)


class EvolutionApprovalSignatureAuthorityTool(Tool):
    """Read one verified signature receipt without changing authority."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_approval_signature_authority"

    @property
    def description(self) -> str:
        return (
            "只读重载已验证的 Approval Signature Receipt，并动态显示 Requirement、target、"
            "Principal/key/role 当前是否仍可进入未来聚合；不修改 authority。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "receipt_id": {
                    "type": "string",
                    "pattern": "^evsigreceipt_[0-9a-f]{24}$",
                },
            },
            "required": ["receipt_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="Evolution 审批签名 Authority",
            search_hint=(
                "evolution approval signature receipt authority current eligibility "
                "自进化 审批 签名 回执 状态"
            ),
        )

    async def execute(self, receipt_id: str) -> str:
        try:
            view = await self._engine.evolution_approval_signature_service.inspect(
                workspace_root=self._engine.workspace_root,
                receipt_id=receipt_id.strip(),
            )
        except (
            AttributeError,
            EvolutionApprovalSignatureError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Approval Signature Authority 不可读取：{exc}"
        return render_evolution_approval_signature(view)


class EvolutionPromotionApprovalDecisionTool(Tool):
    """Aggregate exact current approval evidence without promotion authority."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_promotion_approval_decision"

    @property
    def description(self) -> str:
        return (
            "重读 exact Approval Requirement、Package、所有 required Role Response、"
            "Ed25519 Signature Receipt 与当前身份/target authority，形成 append-only "
            "非执行型审批决定。即使 approved 也只允许进入未来 rebase/revalidate，"
            "不授予 Git、merge、push、publish 或 Promotion 权限。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "requirement_id": {
                    "type": "string",
                    "pattern": "^evapprovalreq_[0-9a-f]{24}$",
                },
            },
            "required": ["requirement_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution 审批决策聚合",
            search_hint=(
                "evolution promotion approval decision aggregate quorum signature "
                "自进化 提升 审批 决策 聚合 法定人数 签名"
            ),
        )

    async def execute(self, requirement_id: str) -> str:
        try:
            view = await (
                self._engine.evolution_promotion_approval_decision_service.execute(
                    workspace_root=self._engine.workspace_root,
                    requirement_id=requirement_id.strip(),
                )
            )
        except (
            AttributeError,
            EvolutionPromotionApprovalDecisionError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Approval Decision 未完成：{exc}"
        return render_evolution_promotion_approval_decision(view)


class EvolutionApprovalDecisionAuthorityTool(Tool):
    """Inspect one historical decision against current upstream authority."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_approval_decision_authority"

    @property
    def description(self) -> str:
        return (
            "只读重载 Approval Decision Receipt，并重新检查 Requirement 有效期、"
            "Package、target、Principal/key/role 与 Signature 是否仍 current。"
            "不修改 authority，不执行 Git 或 Promotion。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "decision_id": {
                    "type": "string",
                    "pattern": "^evapprovaldecision_[0-9a-f]{24}$",
                },
            },
            "required": ["decision_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="Evolution 审批决定 Authority",
            search_hint=(
                "evolution approval decision authority current stale receipt "
                "自进化 审批 决定 当前 过期 回执"
            ),
        )

    async def execute(self, decision_id: str) -> str:
        try:
            view = await (
                self._engine.evolution_promotion_approval_decision_service.inspect(
                    workspace_root=self._engine.workspace_root,
                    decision_id=decision_id.strip(),
                )
            )
        except (
            AttributeError,
            EvolutionPromotionApprovalDecisionError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Approval Decision Authority 不可读取：{exc}"
        return render_evolution_promotion_approval_decision(view)


class EvolutionRevalidationRequestTool(Tool):
    """Freeze approved authority as a non-executing revalidation request."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_revalidation_request"

    @property
    def description(self) -> str:
        return (
            "消费 current approved Approval Decision 与 exact Promotion Package，"
            "冻结未来隔离 rebase/revalidate 所需的确定性输入。只创建可动态失效的请求，"
            "不执行 Git write、rebase、测试、merge、push、publish 或 Promotion。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "decision_id": {
                    "type": "string",
                    "pattern": "^evapprovaldecision_[0-9a-f]{24}$",
                },
            },
            "required": ["decision_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution 再验证请求",
            search_hint=(
                "evolution revalidation request approved decision rebase sandbox "
                "自进化 再验证 请求 批准 决定 隔离"
            ),
        )

    async def execute(self, decision_id: str) -> str:
        try:
            view = await self._engine.evolution_revalidation_request_service.issue(
                workspace_root=self._engine.workspace_root,
                decision_id=decision_id.strip(),
            )
        except (
            AttributeError,
            EvolutionRevalidationRequestError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Revalidation Request 未签发：{exc}"
        return render_evolution_revalidation_request(view)


class EvolutionRevalidationRequestAuthorityTool(Tool):
    """Inspect one request against current approval, package, and target authority."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_revalidation_request_authority"

    @property
    def description(self) -> str:
        return (
            "只读重载 Revalidation Request，并重新检查 Approval Decision、Promotion "
            "Package 与 target 是否仍 current。不执行 Git、验证命令或 Promotion。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^evrevalidation_[0-9a-f]{24}$",
                },
            },
            "required": ["request_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="Evolution 再验证请求 Authority",
            search_hint=(
                "evolution revalidation request authority ready stale current "
                "自进化 再验证 请求 当前 失效"
            ),
        )

    async def execute(self, request_id: str) -> str:
        try:
            view = await self._engine.evolution_revalidation_request_service.inspect(
                workspace_root=self._engine.workspace_root,
                request_id=request_id.strip(),
            )
        except (
            AttributeError,
            EvolutionRevalidationRequestError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Revalidation Request Authority 不可读取：{exc}"
        return render_evolution_revalidation_request(view)


class EvolutionRevalidationReplayTool(Tool):
    """Replay exact approved candidate bytes in an isolated detached worktree."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_revalidation_replay"

    @property
    def description(self) -> str:
        return (
            "消费 current Revalidation Request、Promotion Input 与 active Experiment "
            "Lease，在 disposable detached worktree 中真实重放 exact Candidate 字节并"
            "持久化回执。不会执行验证、merge、push、publish 或 Promotion。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^evrevalidation_[0-9a-f]{24}$",
                },
            },
            "required": ["request_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            user_facing_name="Evolution 隔离源码重放",
            search_hint=(
                "evolution revalidation replay detached worktree exact source "
                "自进化 再验证 隔离 源码 重放"
            ),
        )

    async def execute(self, request_id: str) -> str:
        try:
            receipt = await self._engine.evolution_revalidation_replay_service.execute(
                workspace_root=self._engine.workspace_root,
                request_id=request_id.strip(),
            )
        except (
            AttributeError,
            EvolutionRevalidationRebaseError,
            EvolutionRevalidationReplayError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Revalidation Replay 未完成：{exc}"
        return render_evolution_revalidation_execution(receipt)


class EvolutionRevalidationValidationTool(Tool):
    """Run trusted Profile checks over one exact replay/rebase overlay."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_revalidation_validate"

    @property
    def description(self) -> str:
        return (
            "重新构造成功 Replay/Rebase 的精确源码 overlay，经 Harness Sandbox 与 "
            "ARC-04 Worker 执行当前受信任 Profile 的匹配检查并签发新证据。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^evrevalidation_[0-9a-f]{24}$",
                },
            },
            "required": ["request_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            command_argument_names=(),
            user_facing_name="Evolution Harness 再验证",
            search_hint=(
                "evolution revalidation harness profile checks ARC-04 worker "
                "自进化 再验证 新证据"
            ),
            delegated_tool_names=("bash_run",),
        )

    async def execute(self, request_id: str) -> str:
        try:
            receipt = await self._engine.evolution_revalidation_validation_service.execute(
                workspace_root=self._engine.workspace_root,
                request_id=request_id.strip(),
            )
        except (
            AttributeError,
            EvolutionRevalidationValidationError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Harness Revalidation 未完成：{exc}"
        return render_evolution_revalidation_validation(receipt)


class EvolutionRevalidationOutcomeTool(Tool):
    """Invalidate old promotion evidence and issue one durable outcome."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_revalidation_outcome"

    @property
    def description(self) -> str:
        return (
            "消费持久化 Harness Revalidation Receipt，原子失效旧 promotion evidence，"
            "并签发要求重新评估与重新审批的 Revalidation Outcome。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^evrevalidation_[0-9a-f]{24}$",
                },
            },
            "required": ["request_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            command_argument_names=(),
            user_facing_name="Evolution 再验证结论",
            search_hint="evolution revalidation outcome stale invalidation 自进化 证据失效",
        )

    async def execute(self, request_id: str) -> str:
        try:
            view = await self._engine.evolution_revalidation_outcome_service.issue(
                workspace_root=self._engine.workspace_root,
                request_id=request_id.strip(),
            )
        except (
            AttributeError,
            EvolutionRevalidationOutcomeError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Revalidation Outcome 未完成：{exc}"
        return render_evolution_revalidation_outcome(view)


class EvolutionRevalidationEvaluationPlanTool(Tool):
    """Plan the complete fresh evaluation matrix required after revalidation."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_revalidation_evaluation_plan"

    @property
    def description(self) -> str:
        return (
            "从 current validated Revalidation Outcome 重建完整 Interventional 与"
            "跨平台 Adversarial 重评覆盖，签发 fresh evaluation authority。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "outcome_id": {
                    "type": "string",
                    "pattern": "^evrevalout_[0-9a-f]{24}$",
                },
            },
            "required": ["outcome_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            command_argument_names=(),
            user_facing_name="Evolution Fresh Evaluation Plan",
            search_hint=(
                "evolution revalidation fresh evaluation lanes matrix 重新评估 覆盖"
            ),
        )

    async def execute(self, outcome_id: str) -> str:
        try:
            view = await self._engine.evolution_revalidation_evaluation_plan_service.issue(
                workspace_root=self._engine.workspace_root,
                outcome_id=outcome_id.strip(),
            )
        except (
            AttributeError,
            EvolutionRevalidationEvaluationPlanError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Fresh Evaluation Plan 未完成：{exc}"
        return render_evolution_revalidation_evaluation_plan(view)


class EvolutionRevalidationEvaluationSourceTool(Tool):
    """Capture immutable exact source blobs for fresh evaluation execution."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_revalidation_evaluation_source"

    @property
    def description(self) -> str:
        return (
            "重新物化 Fresh Evaluation Plan 的 exact target + overlay，持久化为"
            "content-addressed immutable blobs，供完整评测执行器消费。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "plan_id": {
                    "type": "string",
                    "pattern": "^evrevalplan_[0-9a-f]{24}$",
                },
            },
            "required": ["plan_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            command_argument_names=(),
            user_facing_name="Evolution Evaluation Source Snapshot",
            search_hint=(
                "evolution revalidation immutable source snapshot overlay content addressed"
            ),
        )

    async def execute(self, plan_id: str) -> str:
        try:
            snapshot = (
                await self._engine.evolution_revalidation_evaluation_source_service.capture(
                    workspace_root=self._engine.workspace_root,
                    plan_id=plan_id.strip(),
                )
            )
        except (
            AttributeError,
            EvolutionRevalidationEvaluationSourceError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Evaluation Source Snapshot 未完成：{exc}"
        return render_evolution_revalidation_evaluation_source(snapshot)


class EvolutionRevalidationValidationPlanTool(Tool):
    """Bind fresh evaluation policy to current RED and immutable GREEN sources."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_revalidation_validation_plan"

    @property
    def description(self) -> str:
        return (
            "将旧 Experiment Contract 的 seed、预算、指标和样本策略重新绑定到"
            "current-target RED 与 immutable-overlay GREEN，签发不可执行的重评计划。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "source_snapshot_id": {
                    "type": "string",
                    "pattern": "^evrevalsrc_[0-9a-f]{24}$",
                },
            },
            "required": ["source_snapshot_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            command_argument_names=(),
            user_facing_name="Evolution Revalidation Validation Plan",
            search_hint=(
                "evolution revalidation validation plan RED GREEN immutable source"
            ),
        )

    async def execute(self, source_snapshot_id: str) -> str:
        try:
            view = (
                await self._engine.evolution_revalidation_validation_plan_service.issue(
                    workspace_root=self._engine.workspace_root,
                    source_snapshot_id=source_snapshot_id.strip(),
                )
            )
        except (
            AttributeError,
            EvolutionRevalidationValidationPlanError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Revalidation Validation Plan 未完成：{exc}"
        return render_evolution_revalidation_validation_plan(view)


class EvolutionRevalidationRuntimeContractTool(Tool):
    """Bind fresh metric runners and adversarial probes without execution."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_revalidation_runtime_contract"

    @property
    def description(self) -> str:
        return (
            "为 current-target Fresh Evaluation 绑定可执行 metric runner、timeout、"
            "预算与 adversarial probe coverage；缺口会形成机械 blocker。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "validation_plan_id": {
                    "type": "string",
                    "pattern": "^evrevalvplan_[0-9a-f]{24}$",
                },
            },
            "required": ["validation_plan_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            concurrency_safe=True,
            command_argument_names=(),
            user_facing_name="Evolution Fresh Runtime Contract",
            search_hint="evolution fresh metric runner adversarial probe runtime contract",
        )

    async def execute(self, validation_plan_id: str) -> str:
        try:
            view = (
                await self._engine.evolution_revalidation_runtime_contract_service.issue(
                    workspace_root=self._engine.workspace_root,
                    validation_plan_id=validation_plan_id.strip(),
                )
            )
        except (
            AttributeError,
            EvolutionRevalidationRuntimeContractError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Fresh Runtime Contract 未完成：{exc}"
        return render_evolution_revalidation_runtime_contract(view)


class EvolutionRevalidationRollbackExecutionTool(Tool):
    """Execute one exact-source, authority-bound installed-slot rollback."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_revalidation_rollback_execute"

    @property
    def description(self) -> str:
        return (
            "消费 exact Rollback Request 与 immutable Source，在 paused kill switch 下"
            "以 expected-pointer CAS 原子切回已验证 baseline slot，并返回 durable Receipt。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^evrerollbackreq_[0-9a-f]{24}$",
                },
            },
            "required": ["request_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=True,
            concurrency_safe=True,
            requires_confirmation=True,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="Evolution 受控版本槽回滚",
            search_hint=(
                "evolution revalidation rollback installed slot baseline receipt "
                "自进化 回滚 版本槽"
            ),
        )

    async def execute(self, request_id: str) -> str:
        normalized = str(request_id or "").strip()
        try:
            view = (
                await self._engine.evolution_revalidation_rollback_execution_service.execute(
                    request_id=normalized,
                )
            )
        except (
            AttributeError,
            EvolutionRevalidationRollbackExecutionError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "rollback_execution_failed")
            return f"Evolution 受控版本槽回滚未完成（`{code}`）：{exc}"
        return render_revalidation_rollback_execution(view)


class EvolutionStablePopulationCandidatePreviewTool(Tool):
    """Preview durable cross-installation stable completion candidates."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_stable_population_candidate_preview"

    @property
    def description(self) -> str:
        return (
            "有界扫描 durable Stable Stage Completion receipts，按 Population Snapshot "
            "与安装成员去重，动态核验 current signed Population trust，并通过默认惰性 "
            "stable read graph 或显式 5f5r inspection port 逐成员重验，显示 "
            "passing、breached、insufficient、缺失、撤权和冲突；"
            "只生成防篡改候选预演，不授予 stable rollout 或 promotion 权限。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "snapshot_id": {
                    "type": ["string", "null"],
                    "pattern": "^relpopsnapshot_[0-9a-f]{24}$",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="Stable Population 候选预演",
            search_hint=(
                "evolution stable population candidate completion rollout preview "
                "自进化 稳定发布 成员 候选 预演"
            ),
        )

    async def execute(
        self,
        snapshot_id: str | None = None,
        limit: int = 50,
    ) -> str:
        try:
            preview = (
                await self._engine.evolution_stable_population_candidate_preview_service.preview(
                    snapshot_id=snapshot_id,
                    limit=limit,
                )
            )
        except (
            AttributeError,
            EvolutionStablePopulationCandidatePreviewError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "stable_population_candidate_preview_failed")
            return f"Stable Population 候选预演不可用（`{code}`）：{exc}"
        return render_stable_population_candidate_preview(preview)


class EvolutionStablePopulationCompletionTool(Tool):
    """Issue or dynamically re-inspect one exact stable population completion."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_stable_population_completion"

    @property
    def description(self) -> str:
        return (
            "对 exact current Population 和全部 5f5r member Views 执行动态重验，"
            "在 SQLite writer fence 内签发幂等、可防篡改的 Stable Population "
            "Completion Receipt，或重新检查既有 Receipt 是否因成员 source、"
            "Population trust 或新 Snapshot 而撤权；不授予 rollout/promotion 权限。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["complete", "inspect"]},
                "snapshot_id": {
                    "type": ["string", "null"],
                    "pattern": "^relpopsnapshot_[0-9a-f]{24}$",
                },
                "receipt_id": {
                    "type": ["string", "null"],
                    "pattern": "^evstablepopcomplete_[0-9a-f]{24}$",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="Stable Population 完成权威",
            search_hint=(
                "evolution stable population completion receipt authority "
                "自进化 稳定发布 群体 完成 回执 撤权"
            ),
        )

    async def execute(
        self,
        action: str,
        snapshot_id: str | None = None,
        receipt_id: str | None = None,
    ) -> str:
        try:
            service = self._engine.evolution_stable_population_completion_service
            if action == "complete":
                if receipt_id is not None:
                    raise ValueError("complete 不接受 receipt_id。")
                view = await service.complete(snapshot_id=snapshot_id)
            elif action == "inspect":
                if snapshot_id is not None or receipt_id is None:
                    raise ValueError("inspect 需要且仅接受 receipt_id。")
                view = await service.inspect(receipt_id=receipt_id)
            else:
                raise ValueError("action 必须是 complete 或 inspect。")
        except (
            AttributeError,
            EvolutionStablePopulationCompletionError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "stable_population_completion_failed")
            return f"Stable Population Completion 未完成（`{code}`）：{exc}"
        return render_stable_population_completion(view)


class EvolutionStableRollbackReadinessTool(Tool):
    """Verify exact ARC-07 rollback readiness for one stable member."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_stable_rollback_readiness"

    @property
    def description(self) -> str:
        return (
            "把 current 5f5w Population Completion 中的一个 Stable Intent 与"
            "真实 Active Pointer、上一代 activation event、仍保留的 previous slot "
            "及其原 Boot Receipt 精确对账，证明 binary rollback 的 expected-pointer "
            "CAS 前置条件；只读且不授予 rollout/promotion 权限。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "completion_receipt_id": {
                    "type": "string",
                    "pattern": "^evstablepopcomplete_[0-9a-f]{24}$",
                },
                "intent_id": {
                    "type": "string",
                    "pattern": "^evrestableintent_[0-9a-f]{24}$",
                },
            },
            "required": ["completion_receipt_id", "intent_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="Stable 回滚就绪检查",
            search_hint=(
                "evolution stable rollback readiness active pointer previous slot "
                "自进化 稳定发布 回滚 就绪 版本槽"
            ),
        )

    async def execute(
        self,
        completion_receipt_id: str,
        intent_id: str,
    ) -> str:
        try:
            readiness = (
                await self._engine.evolution_stable_rollback_readiness_service.inspect(
                    completion_receipt_id=completion_receipt_id,
                    intent_id=intent_id,
                )
            )
        except (
            AttributeError,
            EvolutionStableRollbackReadinessError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "stable_rollback_readiness_failed")
            return f"Stable Rollback Readiness 不可用（`{code}`）：{exc}"
        return render_stable_rollback_readiness(readiness)


class EvolutionInstallationKeyTool(Tool):
    """Explicitly provision or publicly inspect one installation signing key."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_installation_key"

    @property
    def description(self) -> str:
        return (
            "显式初始化或只读检查 managed installation Ed25519 identity；私钥仅写入"
            "系统凭据库，启动 AgentEngine 或只读 inspect 均不会访问 keyring。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["provision", "inspect"]},
                "channel": {
                    "type": "string",
                    "pattern": "^[a-z0-9][a-z0-9._-]{0,63}$",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="安装身份密钥",
            search_hint=(
                "release installation key provision inspect keyring ed25519 "
                "发行 安装 身份 密钥 系统凭据库"
            ),
        )

    async def execute(self, action: str, channel: str = "stable") -> str:
        normalized = str(action or "").strip().lower()
        try:
            service = self._engine.release_installation_key_service
            if normalized == "provision":
                handle = await asyncio.to_thread(
                    service.provision,
                    channel=channel,
                )
            elif normalized == "inspect":
                if channel not in {"", "stable"}:
                    raise ValueError("inspect 不接受 channel 覆盖。")
                handle = await asyncio.to_thread(service.inspect)
            else:
                raise ValueError("action 必须是 provision 或 inspect。")
        except (
            AttributeError,
            OSError,
            ReleaseInstallationKeyError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "release_installation_key_failed")
            return f"安装身份密钥操作未完成（`{code}`）：{exc}"
        return render_release_installation_key(handle)


class EvolutionRolloutControlKeyTool(Tool):
    """Explicitly provision or publicly inspect the rollout-control signer."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_rollout_control_key"

    @property
    def description(self) -> str:
        return (
            "显式初始化或只读检查独立 Rollout Control Ed25519 signing identity；"
            "私钥只进入 OS keyring，不自动安装客户端 Trust Policy，也不直接授予 rollout。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["provision", "inspect"]},
                "control_plane_id": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9._-]{2,63}$",
                },
                "key_generation": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000000,
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="Rollout Control 签名密钥",
            search_hint=(
                "release rollout control signing key provision inspect trust "
                "发行 稳定发布 控制面 签名 密钥 信任根"
            ),
        )

    async def execute(
        self,
        action: str,
        control_plane_id: str = "naumi-control-plane",
        key_generation: int = 1,
    ) -> str:
        normalized = str(action or "").strip().lower()
        try:
            service = self._engine.release_rollout_control_key_service
            if normalized == "provision":
                handle = await asyncio.to_thread(
                    service.provision,
                    control_plane_id=control_plane_id,
                    key_generation=key_generation,
                )
            elif normalized == "inspect":
                handle = await asyncio.to_thread(service.inspect)
            else:
                raise ValueError("action 必须是 provision 或 inspect。")
        except (
            AttributeError,
            OSError,
            ReleaseRolloutControlKeyError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "release_rollout_control_key_failed")
            return f"Rollout Control signing key 操作未完成（`{code}`）：{exc}"
        return render_release_rollout_control_key(handle)


class EvolutionStableRemoteReadinessClaimTool(Tool):
    """Issue, ingest, or inspect an authenticated installation readiness claim."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_stable_remote_readiness_claim"

    @property
    def description(self) -> str:
        return (
            "为 current Stable Population member 签发短期 challenge，验证该安装使用 "
            "Population Credential 对 pointer/slot/rollback assertion 的 Ed25519 签名，"
            "并持久化 authenticated claim；不授予 readiness、execution 或 promotion 权限。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["challenge", "ingest", "inspect"],
                },
                "completion_receipt_id": {"type": "string"},
                "installation_member_id": {"type": "string"},
                "validity_seconds": {"type": "integer", "minimum": 60, "maximum": 900},
                "challenge_id": {"type": "string"},
                "assertion_base64": {"type": "string", "maxLength": 131072},
                "signature_base64": {"type": "string", "maxLength": 128},
                "receipt_id": {"type": "string"},
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="Stable 远端就绪身份声明",
            search_hint=(
                "evolution stable remote readiness challenge claim signature "
                "自进化 稳定发布 远端 就绪 身份 签名"
            ),
        )

    async def execute(
        self,
        action: str,
        completion_receipt_id: str = "",
        installation_member_id: str = "",
        validity_seconds: int = 300,
        challenge_id: str = "",
        assertion_base64: str = "",
        signature_base64: str = "",
        receipt_id: str = "",
    ) -> str:
        try:
            service = self._engine.evolution_stable_remote_readiness_claim_service
            if action == "challenge":
                challenge = await service.issue_challenge(
                    completion_receipt_id=completion_receipt_id,
                    installation_member_id=installation_member_id,
                    validity_seconds=validity_seconds,
                )
                return render_stable_remote_readiness_challenge(challenge)
            if action == "ingest":
                view = await service.ingest(
                    challenge_id=challenge_id,
                    assertion_base64=assertion_base64,
                    signature_base64=signature_base64,
                )
            elif action == "inspect":
                view = await service.inspect(receipt_id=receipt_id)
            else:
                raise ValueError("action 必须是 challenge、ingest 或 inspect。")
        except (
            AttributeError,
            EvolutionStableRemoteReadinessClaimError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "stable_remote_readiness_claim_failed")
            return f"Stable Remote Readiness Claim 未完成（`{code}`）：{exc}"
        return render_stable_remote_readiness_claim(view)


class EvolutionStableRemoteReadinessProbeTool(Tool):
    """Prepare, execute locally, ingest, or inspect a fresh remote store probe."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_stable_remote_readiness_probe"

    @property
    def description(self) -> str:
        return (
            "基于 current authenticated remote claim 签发短期 Probe；远端本机机械"
            "重验 active/candidate/previous/rollback slot 与 Boot Receipt 后使用安装"
            "私钥签名，Control Plane ingest 后仅授予短期 binary rollback readiness。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["prepare", "execute-local", "ingest", "inspect"],
                },
                "claim_receipt_id": {"type": "string"},
                "validity_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 300,
                },
                "challenge_base64": {"type": "string", "maxLength": 524288},
                "challenge_id": {"type": "string"},
                "submission_base64": {"type": "string", "maxLength": 524288},
                "receipt_id": {"type": "string"},
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="Stable 远端 Release Store 新鲜探测",
            search_hint=(
                "evolution stable remote release store fresh probe rollback "
                "自进化 稳定发布 远端 重验 回滚"
            ),
        )

    async def execute(
        self,
        action: str,
        claim_receipt_id: str = "",
        validity_seconds: int = 180,
        challenge_base64: str = "",
        challenge_id: str = "",
        submission_base64: str = "",
        receipt_id: str = "",
    ) -> str:
        try:
            service = self._engine.evolution_stable_remote_readiness_probe_service
            if action == "prepare":
                challenge = await service.prepare(
                    claim_receipt_id=claim_receipt_id,
                    validity_seconds=validity_seconds,
                )
                return render_stable_remote_readiness_probe_challenge(challenge)
            if action == "execute-local":
                challenge = decode_stable_remote_readiness_probe_challenge(
                    challenge_base64
                )
                snapshot = await (
                    self._engine.evolution_release_population_snapshot_store.inspect(
                        snapshot_id=challenge.population_snapshot_id
                    )
                )
                if not snapshot.population_snapshot_authority:
                    raise EvolutionStableRemoteReadinessProbeError(
                        "stable_remote_probe_population_snapshot_stale",
                        "本机 Population Snapshot 已失效。",
                    )
                credential = next(
                    (
                        item
                        for item in snapshot.snapshot.payload.credentials
                        if item.payload.member_id
                        == challenge.installation_member_id
                    ),
                    None,
                )
                if credential is None:
                    raise EvolutionStableRemoteReadinessProbeError(
                        "stable_remote_probe_member_not_local",
                        "本机 Population Snapshot 不包含 Probe 目标 member。",
                    )
                submission = await asyncio.to_thread(
                    execute_stable_remote_readiness_probe,
                    challenge=challenge,
                    credential=credential,
                    release_slot_store=self._engine.evolution_release_slot_store,
                    installation_key_service=(
                        self._engine.release_installation_key_service
                    ),
                    clock=self._engine.release_installation_key_service.clock,
                )
                return render_stable_remote_readiness_probe_submission(submission)
            if action == "ingest":
                view = await service.ingest(
                    challenge_id=challenge_id,
                    submission_base64=submission_base64,
                )
            elif action == "inspect":
                view = await service.inspect(receipt_id=receipt_id)
            else:
                raise ValueError(
                    "action 必须是 prepare、execute-local、ingest 或 inspect。"
                )
        except (
            AttributeError,
            EvolutionStableRemoteReadinessProbeError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "stable_remote_readiness_probe_failed")
            return f"Stable Remote Readiness Probe 未完成（`{code}`）：{exc}"
        return render_stable_remote_readiness_probe(view)


class EvolutionStableRemoteFinalizationAuthorizationTool(Tool):
    """Issue, inspect, or export one signed remote finalization capability."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_stable_remote_finalization_authorization"

    @property
    def description(self) -> str:
        return (
            "消费 current installation-signed Remote Readiness Probe 与 rollout "
            "kill-switch，为 exact member 签发独立 Rollout Control key 签名的短期、"
            "single-use、binary-only finalization envelope；不执行远端写入。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["issue", "inspect", "export"],
                },
                "probe_receipt_id": {
                    "type": "string",
                    "pattern": "^evstableremoteprobereceipt_[0-9a-f]{24}$",
                },
                "authorization_id": {
                    "type": "string",
                    "pattern": "^evstableremotefinalauth_[0-9a-f]{24}$",
                },
                "validity_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 300,
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="Stable 远端最终化签名授权",
            search_hint=(
                "evolution stable remote finalization signed authorization envelope "
                "自进化 稳定发布 远端 最终化 签名 授权"
            ),
        )

    async def execute(
        self,
        action: str,
        probe_receipt_id: str = "",
        authorization_id: str = "",
        validity_seconds: int = 180,
    ) -> str:
        normalized = str(action or "").strip().lower()
        try:
            service = (
                self._engine.evolution_stable_remote_finalization_authorization_service
            )
            if normalized == "issue":
                view = await service.issue(
                    probe_receipt_id=probe_receipt_id,
                    validity_seconds=validity_seconds,
                )
            elif normalized in {"inspect", "export"}:
                view = await service.inspect(authorization_id=authorization_id)
                if normalized == "export" and not view.remote_finalization_authority:
                    raise EvolutionStableRemoteFinalizationAuthorizationError(
                        "stable_remote_finalization_authorization_not_current",
                        "只有 current Authorization 可以导出。",
                    )
            else:
                raise ValueError("action 必须是 issue、inspect 或 export。")
        except (
            AttributeError,
            EvolutionStableRemoteFinalizationAuthorizationError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(
                exc,
                "code",
                "stable_remote_finalization_authorization_failed",
            )
            return f"Signed Remote Finalization Authorization 未完成（`{code}`）：{exc}"
        return render_stable_remote_finalization_authorization(
            view,
            include_envelope=normalized == "export",
        )


class EvolutionStableRemoteFinalizationTool(Tool):
    """Coordinate, execute, and ingest one remote stable-member finalization."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_stable_remote_finalization"

    @property
    def description(self) -> str:
        return (
            "把 signed remote Authorization 原子消费为 Execution Grant；目标端重验 "
            "Release Store 并执行 expected-pointer CAS，以 installation key 签署结果，"
            "再由 Control Plane 重验并形成 durable Receipt。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "prepare", "export", "execute-local", "ingest", "inspect",
                        "queue-delivery", "claim-delivery", "retry-delivery",
                        "receive-delivery-local", "execute-delivery-local",
                        "recover-delivery-local", "ack-delivery",
                        "ingest-delivery", "ingest-delivery-late", "inspect-delivery",
                        "run-delivery-worker", "inspect-delivery-worker",
                        "run-result-return-worker", "inspect-result-return-worker",
                        "inspect-installation-daemon",
                    ],
                },
                "authorization_id": {"type": "string"},
                "grant_id": {"type": "string"},
                "package_base64": {"type": "string", "maxLength": 699052},
                "submission_base64": {"type": "string", "maxLength": 699052},
                "receipt_id": {"type": "string"},
                "delivery_id": {"type": "string"},
                "delivery_package_base64": {"type": "string", "maxLength": 1048576},
                "ack_base64": {"type": "string", "maxLength": 1048576},
                "owner_id": {"type": "string", "maxLength": 256},
                "claim_epoch": {"type": "integer", "minimum": 1},
                "failure_code": {"type": "string", "maxLength": 128},
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="Stable 远端成员最终化",
            search_hint=(
                "evolution stable remote finalization execution grant signed result "
                "自进化 稳定发布 远端 最终化 CAS"
            ),
        )

    async def execute(
        self,
        action: str,
        authorization_id: str = "",
        grant_id: str = "",
        package_base64: str = "",
        submission_base64: str = "",
        receipt_id: str = "",
        delivery_id: str = "",
        delivery_package_base64: str = "",
        ack_base64: str = "",
        owner_id: str = "",
        claim_epoch: int = 0,
        failure_code: str = "",
    ) -> str:
        normalized = str(action or "").strip().lower()
        try:
            service = self._engine.evolution_stable_remote_finalization_service
            delivery_service = delivery_store = None
            if "delivery" in normalized:
                delivery_service = (
                    self._engine.evolution_stable_remote_finalization_delivery_service
                )
                delivery_store = (
                    self._engine.evolution_stable_remote_finalization_delivery_store
                )
            if normalized == "queue-delivery":
                delivery = await delivery_service.queue(grant_id=grant_id)
                worker = getattr(
                    self._engine,
                    "evolution_stable_remote_finalization_delivery_worker",
                    None,
                )
                if worker is not None:
                    worker.wake()
                return render_stable_remote_finalization_delivery(delivery)
            if normalized == "run-delivery-worker":
                result = await (
                    self._engine.run_stable_remote_finalization_delivery_once()
                )
                return render_stable_remote_finalization_delivery_pass(result)
            if normalized == "inspect-delivery-worker":
                snapshot = (
                    self._engine.stable_remote_finalization_delivery_worker_snapshot()
                )
                return render_stable_remote_finalization_delivery_worker(snapshot)
            if normalized == "run-result-return-worker":
                result = await (
                    self._engine.run_stable_remote_finalization_result_return_once()
                )
                return render_stable_remote_finalization_result_return_pass(result)
            if normalized == "inspect-result-return-worker":
                snapshot = (
                    self._engine
                    .stable_remote_finalization_result_return_worker_snapshot()
                )
                return render_stable_remote_finalization_result_return_worker(snapshot)
            if normalized == "inspect-installation-daemon":
                snapshot = await (
                    self._engine
                    .inspect_stable_remote_finalization_installation_daemon()
                )
                return render_stable_remote_finalization_installation_daemon(snapshot)
            if normalized == "claim-delivery":
                delivery = await delivery_store.claim(
                    owner_id=owner_id,
                    now=delivery_service.clock().isoformat(),
                )
                if delivery is None:
                    return "## Remote Stable Finalization Delivery\n\n- 状态：**暂无到期任务**"
                return render_stable_remote_finalization_delivery(
                    delivery, include_package=True
                )
            if normalized == "retry-delivery":
                delivery = await delivery_store.retry(
                    delivery_id=delivery_id,
                    owner_id=owner_id,
                    claim_epoch=claim_epoch,
                    failure_code=failure_code,
                    now=delivery_service.clock().isoformat(),
                )
                return render_stable_remote_finalization_delivery(delivery)
            if normalized in {
                "receive-delivery-local",
                "execute-delivery-local",
                "recover-delivery-local",
            }:
                delivery_package = decode_stable_remote_finalization_delivery_package(
                    delivery_package_base64
                )
                auth = delivery_package.execution_package.authorization.authorization
                snapshot = await (
                    self._engine.evolution_release_population_snapshot_store.inspect(
                        snapshot_id=auth.population_snapshot_id
                    )
                )
                credential = next(
                    (
                        item for item in snapshot.snapshot.payload.credentials
                        if item.payload.member_id == auth.installation_member_id
                    ),
                    None,
                )
                if not snapshot.population_snapshot_authority or credential is None:
                    raise EvolutionStableRemoteFinalizationError(
                        "stable_remote_finalization_member_not_local",
                        "本机 current Population Snapshot 不包含目标 member。",
                    )
                trust_policy = load_release_rollout_control_trust_policy(
                    self._engine.evolution_release_rollout_control_trust_policy_path
                )
                journal = EvolutionStableRemoteFinalizationTargetJournal(
                    self._engine.evolution_release_slot_store.release_root
                )
                if normalized == "receive-delivery-local":
                    ack = await asyncio.to_thread(
                        journal.receive,
                        package=delivery_package,
                        trust_policy=trust_policy,
                        credential=credential,
                        installation_key_service=(
                            self._engine.release_installation_key_service
                        ),
                        clock=self._engine.release_installation_key_service.clock,
                    )
                    result_worker = getattr(
                        self._engine,
                        "evolution_stable_remote_finalization_result_return_worker",
                        None,
                    )
                    if result_worker is not None:
                        result_worker.wake()
                    return "\n".join((
                        "## Remote Stable Finalization Delivery ACK",
                        "",
                        "- 状态：**已验证并写入目标 journal**",
                        f"- Delivery：`{ack.payload.delivery_id}`",
                        "- Writer executed：`false`",
                        "",
                        "### Delivery ACK Base64",
                        "",
                        encode_stable_remote_finalization_delivery_ack(ack),
                    ))
                submission = await asyncio.to_thread(
                    journal.execute,
                    package=delivery_package,
                    trust_policy=trust_policy,
                    credential=credential,
                    release_slot_store=self._engine.evolution_release_slot_store,
                    installation_key_service=(
                        self._engine.release_installation_key_service
                    ),
                    recover_existing_only=normalized == "recover-delivery-local",
                    clock=self._engine.release_installation_key_service.clock,
                )
                result_worker = getattr(
                    self._engine,
                    "evolution_stable_remote_finalization_result_return_worker",
                    None,
                )
                if result_worker is not None:
                    result_worker.wake()
                return render_stable_remote_finalization_submission(submission)
            if normalized == "ack-delivery":
                delivery = await delivery_service.acknowledge(
                    delivery_id=delivery_id, ack_base64=ack_base64
                )
                return render_stable_remote_finalization_delivery(delivery)
            if normalized in {"ingest-delivery", "ingest-delivery-late"}:
                delivery = await delivery_service.ingest_result(
                    delivery_id=delivery_id,
                    submission_base64=submission_base64,
                    late_recovery=normalized == "ingest-delivery-late",
                )
                return render_stable_remote_finalization_delivery(delivery)
            if normalized == "inspect-delivery":
                delivery = await delivery_store.get(delivery_id)
                if delivery is None:
                    raise EvolutionStableRemoteFinalizationDeliveryError(
                        "stable_remote_delivery_missing", "Delivery 不存在。"
                    )
                return render_stable_remote_finalization_delivery(delivery)
            if normalized == "prepare":
                package = await service.prepare(authorization_id=authorization_id)
                return render_stable_remote_finalization(package)
            if normalized == "export":
                package = await service.current_package(grant_id=grant_id)
                return render_stable_remote_finalization(
                    package, include_package=True
                )
            if normalized == "execute-local":
                package = decode_stable_remote_finalization_execution_package(
                    package_base64
                )
                auth = package.authorization.authorization
                snapshot = await (
                    self._engine.evolution_release_population_snapshot_store.inspect(
                        snapshot_id=auth.population_snapshot_id
                    )
                )
                if not snapshot.population_snapshot_authority:
                    raise EvolutionStableRemoteFinalizationError(
                        "stable_remote_finalization_population_snapshot_stale",
                        "本机 Population Snapshot 已失效。",
                    )
                credential = next(
                    (
                        item
                        for item in snapshot.snapshot.payload.credentials
                        if item.payload.member_id == auth.installation_member_id
                    ),
                    None,
                )
                if credential is None:
                    raise EvolutionStableRemoteFinalizationError(
                        "stable_remote_finalization_member_not_local",
                        "本机 Population Snapshot 不包含目标 member。",
                    )
                trust_policy = load_release_rollout_control_trust_policy(
                    self._engine.evolution_release_rollout_control_trust_policy_path
                )
                submission = await asyncio.to_thread(
                    execute_stable_remote_finalization,
                    package=package,
                    trust_policy=trust_policy,
                    credential=credential,
                    release_slot_store=self._engine.evolution_release_slot_store,
                    installation_key_service=(
                        self._engine.release_installation_key_service
                    ),
                    clock=self._engine.release_installation_key_service.clock,
                )
                return render_stable_remote_finalization_submission(submission)
            if normalized == "ingest":
                view = await service.ingest(
                    grant_id=grant_id,
                    submission_base64=submission_base64,
                )
            elif normalized == "inspect":
                view = await service.inspect(receipt_id=receipt_id)
            else:
                raise ValueError(
                    "action 不是受支持的 Stable Remote Finalization 操作。"
                )
        except (
            AttributeError,
            EvolutionStableRemoteFinalizationError,
            EvolutionStableRemoteFinalizationDeliveryError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "stable_remote_finalization_failed")
            return f"Stable Remote Finalization 未完成（`{code}`）：{exc}"
        return render_stable_remote_finalization(view)


class EvolutionStableRemotePopulationFinalizationTool(Tool):
    """Complete or re-inspect the exact remote finalization Population."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_stable_remote_population_finalization"

    @property
    def description(self) -> str:
        return (
            "聚合 exact current Stable Population 的全部远端 member Finalization "
            "Receipt，在 SQLite writer fence 内重验 Grant、Consumption、Control、"
            "Trust、Population Credential 和安装签名，签发 durable Population "
            "Finalization Receipt；缺员、重复、篡改或 authority 漂移立即撤权，"
            "不授予配置/数据 finalization 或 promotion 权限。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["complete", "inspect"]},
                "snapshot_id": {
                    "type": ["string", "null"],
                    "pattern": "^relpopsnapshot_[0-9a-f]{24}$",
                },
                "receipt_id": {
                    "type": ["string", "null"],
                    "pattern": "^evstableremotepopfinal_[0-9a-f]{24}$",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="Remote Population Finalization",
            search_hint=(
                "evolution stable remote population finalization receipt aggregation "
                "自进化 稳定发布 群体 远端 完成 回执 聚合"
            ),
        )

    async def execute(
        self,
        action: str,
        snapshot_id: str | None = None,
        receipt_id: str | None = None,
    ) -> str:
        try:
            service = (
                self._engine.evolution_stable_remote_population_finalization_service
            )
            if action == "complete":
                if receipt_id is not None:
                    raise ValueError("complete 不接受 receipt_id。")
                view = await service.complete(snapshot_id=snapshot_id)
            elif action == "inspect":
                if snapshot_id is not None or receipt_id is None:
                    raise ValueError("inspect 需要且仅接受 receipt_id。")
                view = await service.inspect(receipt_id=receipt_id)
            else:
                raise ValueError("action 必须是 complete 或 inspect。")
        except (
            AttributeError,
            EvolutionStableRemotePopulationFinalizationError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(
                exc,
                "code",
                "stable_remote_population_finalization_failed",
            )
            return f"Remote Population Finalization 未完成（`{code}`）：{exc}"
        return render_stable_remote_population_finalization(view)


class EvolutionStablePromotionObservationContractTool(Tool):
    """Freeze successful stable-rollout lineage for later long-term observation."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_stable_promotion_observation_contract"

    @property
    def description(self) -> str:
        return (
            "为已完成且当前仍有效的 Stable Population 冻结 Approval、Proposal、"
            "Runtime 与长期观察规则；不读取长期指标，不签发 promoted Outcome。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "finalization_receipt_id": {
                    "type": "string",
                    "pattern": "^evstableremotepopfinal_[0-9a-f]{24}$",
                },
            },
            "required": ["finalization_receipt_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="稳定推广长期观察契约",
            search_hint=(
                "evolution stable promotion observation contract promoted outcome "
                "自进化 稳定 推广 长期观察 契约"
            ),
        )

    async def execute(self, finalization_receipt_id: str) -> str:
        try:
            view = await (
                self._engine.evolution_stable_promotion_observation_contract_service.record(
                    finalization_receipt_id=str(finalization_receipt_id or "").strip()
                )
            )
        except (
            AttributeError,
            EvolutionStablePromotionObservationContractError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(
                exc, "code", "stable_promotion_observation_contract_failed"
            )
            return f"稳定推广长期观察契约未完成（`{code}`）：{exc}"
        return render_stable_promotion_observation_contract(view)


class EvolutionStableRolloutAuthorizationTool(Tool):
    """Issue or inspect one member-scoped stable rollout capability."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_stable_rollout_authorization"

    @property
    def description(self) -> str:
        return (
            "为 exact current Population Completion 中的单个 member 签发或重验短期、"
            "single-use、binary-only Stable Rollout Authorization；绑定 rollback "
            "readiness 与 kill-switch generation，不授予配置/数据或 promotion 权限。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["issue", "inspect"]},
                "completion_receipt_id": {"type": "string"},
                "intent_id": {"type": "string"},
                "authorization_id": {"type": "string"},
                "validity_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 900,
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="Stable Rollout 授权",
            search_hint=(
                "evolution stable rollout authorization 自进化 稳定发布 授权"
            ),
        )

    async def execute(
        self,
        action: str,
        completion_receipt_id: str = "",
        intent_id: str = "",
        authorization_id: str = "",
        validity_seconds: int = 300,
    ) -> str:
        try:
            service = self._engine.evolution_stable_rollout_authorization_service
            if action == "issue":
                view = await service.issue(
                    completion_receipt_id=completion_receipt_id,
                    intent_id=intent_id,
                    validity_seconds=validity_seconds,
                )
            elif action == "inspect":
                view = await service.inspect(authorization_id=authorization_id)
            else:
                raise ValueError("action 必须是 issue 或 inspect。")
        except (
            AttributeError,
            EvolutionStableRolloutAuthorizationError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "stable_rollout_authorization_failed")
            return f"Stable Rollout Authorization 不可用（`{code}`）：{exc}"
        return render_stable_rollout_authorization(view)


class EvolutionStableRolloutFinalizationTool(Tool):
    """Execute or inspect one authority-bound stable member finalization."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_stable_rollout_finalization"

    @property
    def description(self) -> str:
        return (
            "消费 exact Stable Rollout Authorization，以 release-store pointer CAS "
            "完成单 installation member 的 binary-only stable finalization；支持崩溃恢复，"
            "不执行配置/数据迁移，也不授予 Population rollout 或 promotion 权限。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["execute", "inspect"]},
                "authorization_id": {"type": "string"},
            },
            "required": ["action", "authorization_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="Stable Rollout 成员最终化",
            search_hint="evolution stable rollout finalization completion 稳定发布 最终化",
        )

    async def execute(self, action: str, authorization_id: str) -> str:
        try:
            service = self._engine.evolution_stable_rollout_finalization_service
            if action == "execute":
                view = await service.execute(authorization_id=authorization_id)
            elif action == "inspect":
                view = await service.inspect(authorization_id=authorization_id)
            else:
                raise ValueError("action 必须是 execute 或 inspect。")
        except (
            AttributeError,
            EvolutionStableRolloutFinalizationError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "stable_rollout_finalization_failed")
            return f"Stable Rollout Finalization 不可用（`{code}`）：{exc}"
        return render_stable_rollout_finalization(view)


class EvolutionRevalidationRollbackOutcomeTool(Tool):
    """Record one proposal-bound historical rollback Outcome."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_revalidation_rollback_outcome"

    @property
    def description(self) -> str:
        return (
            "把可验证 Rollback Receipt 反向绑定到原始 Experiment Contract 与 "
            "Workbench Proposal，幂等记录 rolled_back Outcome。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^evrerollbackreq_[0-9a-f]{24}$",
                },
            },
            "required": ["request_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="Evolution 回滚 Outcome",
            search_hint=(
                "evolution rollback outcome proposal contract receipt rolled back "
                "自进化 回滚 结果"
            ),
        )

    async def execute(self, request_id: str) -> str:
        try:
            view = await self._engine.evolution_revalidation_rollback_outcome_service.record(
                request_id=str(request_id or "").strip(),
            )
        except (
            AttributeError,
            EvolutionRevalidationRollbackOutcomeError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "rollback_outcome_failed")
            return f"Evolution 回滚 Outcome 未完成（`{code}`）：{exc}"
        return render_revalidation_rollback_outcome(view)


class EvolutionProposalBeforeAfterEvidenceTool(Tool):
    """Record Proposal-bound HAR-08 implementation comparisons."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_proposal_before_after_evidence"

    @property
    def description(self) -> str:
        return (
            "把 rolled_back Outcome 绑定到原 Promotion Input 的 Final Evaluation 与 "
            "HAR-08 RED/GREEN Comparison，幂等登记实施前后证据。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^evrerollbackreq_[0-9a-f]{24}$",
                },
            },
            "required": ["request_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="Evolution 实施前后证据",
            search_hint=(
                "evolution proposal before after final evaluation comparison "
                "自进化 实施 前后 评测"
            ),
        )

    async def execute(self, request_id: str) -> str:
        try:
            view = (
                await self._engine.evolution_proposal_before_after_evidence_service.record(
                    request_id=str(request_id or "").strip(),
                )
            )
        except (
            AttributeError,
            EvolutionProposalBeforeAfterEvidenceError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "proposal_before_after_failed")
            return f"Evolution 实施前后证据未完成（`{code}`）：{exc}"
        return render_proposal_before_after_evidence(view)


class EvolutionPostRollbackRuntimeVerificationTool(Tool):
    """Run fresh installed-runtime probes after an authority-bound rollback."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_post_rollback_runtime_verification"

    @property
    def description(self) -> str:
        return (
            "在 rolled_back Outcome 的 active baseline slot 上重新执行受控 "
            "--version boot probe，并重新解析 launcher identity。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^evrerollbackreq_[0-9a-f]{24}$",
                },
            },
            "required": ["request_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="回滚后 Runtime 验证",
            search_hint=(
                "evolution post rollback runtime verification boot launch "
                "自进化 回滚 恢复 验证"
            ),
        )

    async def execute(self, request_id: str) -> str:
        try:
            view = await (
                self._engine.evolution_post_rollback_runtime_verification_service.record(
                    request_id=str(request_id or "").strip(),
                )
            )
        except (
            AttributeError,
            EvolutionPostRollbackRuntimeVerificationError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "post_rollback_verification_failed")
            return f"回滚后 Runtime 验证未完成（`{code}`）：{exc}"
        return render_post_rollback_runtime_verification(view)


class EvolutionPostRollbackBehavioralLaneTool(Tool):
    """Run one fresh native H5c lane against the exact installed baseline."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_post_rollback_behavioral_lane"

    @property
    def description(self) -> str:
        return (
            "在 rolled_back Outcome 的 exact installed baseline 上，按原 H5c "
            "repetitions 重新执行一个 Final Evaluation lane；不聚合全部平台，"
            "不授予学习或推广权限。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^evrerollbackreq_[0-9a-f]{24}$",
                },
                "comparison_id": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
            },
            "required": ["request_id", "comparison_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="回滚后行为验证 Lane",
            search_hint=(
                "evolution post rollback installed runtime behavioral h5c lane "
                "自进化 回滚 行为 评测"
            ),
        )

    async def execute(self, request_id: str, comparison_id: str) -> str:
        try:
            view = await (
                self._engine.evolution_post_rollback_behavioral_lane_service.record(
                    request_id=str(request_id or "").strip(),
                    comparison_id=str(comparison_id or "").strip(),
                )
            )
        except (
            AttributeError,
            EvolutionPostRollbackBehavioralLaneError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "post_rollback_behavioral_lane_failed")
            return f"回滚后行为验证 Lane 未完成（`{code}`）：{exc}"
        return render_post_rollback_behavioral_lane(view)


class EvolutionPostRollbackBehavioralCoverageTool(Tool):
    """Freeze and inspect the exact post-rollback lane coverage requirement."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_post_rollback_behavioral_coverage"

    @property
    def description(self) -> str:
        return (
            "冻结 rolled_back Outcome 的完整 Final Evaluation lane 集，区分本机 "
            "installed baseline 可执行 lane 与必须由目标主机执行的 lane，并动态检查覆盖状态。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^evrerollbackreq_[0-9a-f]{24}$",
                },
            },
            "required": ["request_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="回滚后行为覆盖契约",
            search_hint=(
                "evolution post rollback behavioral coverage matrix lanes target host "
                "自进化 回滚 行为 覆盖 平台"
            ),
        )

    async def execute(self, request_id: str) -> str:
        try:
            view = await (
                self._engine.evolution_post_rollback_behavioral_coverage_service.record(
                    request_id=str(request_id or "").strip(),
                )
            )
        except (
            AttributeError,
            EvolutionPostRollbackBehavioralCoverageError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "post_rollback_coverage_failed")
            return f"回滚后行为覆盖契约未完成（`{code}`）：{exc}"
        return render_post_rollback_behavioral_coverage(view)


class EvolutionPostRollbackBehavioralMatrixTool(Tool):
    """Record the exact complete matrix over local and signed remote lanes."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_post_rollback_behavioral_matrix"

    @property
    def description(self) -> str:
        return (
            "聚合 rolled_back Outcome 的完整本机与远端签名 Behavioral Lane，"
            "动态复验 H5c authority 并签发总体恢复判定；不授予长期、学习或推广权限。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^evrerollbackreq_[0-9a-f]{24}$",
                },
            },
            "required": ["request_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="回滚后行为矩阵",
            search_hint=(
                "evolution post rollback behavioral matrix verdict local remote "
                "自进化 回滚 行为 矩阵 判定"
            ),
        )

    async def execute(self, request_id: str) -> str:
        try:
            view = await (
                self._engine.evolution_post_rollback_behavioral_matrix_service.record(
                    request_id=str(request_id or "").strip(),
                )
            )
        except (
            AttributeError,
            EvolutionPostRollbackBehavioralMatrixError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "post_rollback_behavioral_matrix_failed")
            return f"回滚后行为矩阵未完成（`{code}`）：{exc}"
        return render_post_rollback_behavioral_matrix(view)


class EvolutionPostRollbackLongTermObservationContractTool(Tool):
    """Freeze exact lineage and policy for a future long-term window."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_post_rollback_observation_contract"

    @property
    def description(self) -> str:
        return (
            "为 recovered Behavioral Matrix 冻结长期观察 lineage、窗口、样本、"
            "间隙与 censoring 规则；不计算指标，不授予学习、推广或执行权限。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^evrerollbackreq_[0-9a-f]{24}$",
                },
            },
            "required": ["request_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="回滚后长期观察契约",
            search_hint=(
                "evolution post rollback long term observation contract window "
                "自进化 回滚 长期 观察 契约"
            ),
        )

    async def execute(self, request_id: str) -> str:
        try:
            view = await (
                self._engine.evolution_post_rollback_observation_contract_service.record(
                    request_id=str(request_id or "").strip(),
                )
            )
        except (
            AttributeError,
            EvolutionPostRollbackLongTermObservationContractError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "post_rollback_observation_contract_failed")
            return f"回滚后长期观察契约未完成（`{code}`）：{exc}"
        return render_post_rollback_long_term_observation_contract(view)


class EvolutionPostRollbackRuntimeObservationAdmissionTool(Tool):
    """Admit one exact managed runtime startup chain for observation."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_post_rollback_runtime_admission"

    @property
    def description(self) -> str:
        return (
            "把长期观察契约逐字段绑定到 exact managed New UI/TUI runtime identity "
            "与 startup origin sample；不聚合长期指标，不启动任何进程。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^evrerollbackreq_[0-9a-f]{24}$",
                },
                "subject_id": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9_-]{0,95}$",
                },
            },
            "required": ["request_id", "subject_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="回滚后 Runtime 观察准入",
            search_hint=(
                "evolution post rollback runtime observation admission binding "
                "startup 自进化 回滚 运行时 观察 准入"
            ),
        )

    async def execute(self, request_id: str, subject_id: str) -> str:
        try:
            view = await (
                self._engine.evolution_post_rollback_runtime_admission_service.record(
                    request_id=str(request_id or "").strip(),
                    subject_id=str(subject_id or "").strip(),
                )
            )
        except (
            AttributeError,
            EvolutionPostRollbackRuntimeObservationAdmissionError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "post_rollback_runtime_admission_failed")
            return f"回滚后 Runtime 观察准入未完成（`{code}`）：{exc}"
        return render_post_rollback_runtime_observation_admission(view)


class EvolutionPostRollbackLongTermObservationAssessmentTool(Tool):
    """Assess the current admitted runtime ledger under the frozen contract."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_post_rollback_long_term_assessment"

    @property
    def description(self) -> str:
        return (
            "分页复验 admitted runtime 的 append-only heartbeat ledger，并按冻结规则"
            "机械判定 insufficient、passing、breached 或 censored；不授予学习或推广权限。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^evrerollbackreq_[0-9a-f]{24}$",
                },
                "subject_id": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9_-]{0,95}$",
                },
            },
            "required": ["request_id", "subject_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="回滚后长期观察评估",
            search_hint=(
                "evolution post rollback long term observation assessment health "
                "自进化 回滚 长期 观察 健康 评估"
            ),
        )

    async def execute(self, request_id: str, subject_id: str) -> str:
        try:
            view = await (
                self._engine.evolution_post_rollback_long_term_assessment_service.assess(
                    request_id=str(request_id or "").strip(),
                    subject_id=str(subject_id or "").strip(),
                )
            )
        except (
            AttributeError,
            EvolutionPostRollbackLongTermObservationAssessmentError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "post_rollback_long_term_assessment_failed")
            return f"回滚后长期观察评估未完成（`{code}`）：{exc}"
        return render_post_rollback_long_term_observation_assessment(view)


class EvolutionPostRollbackLongTermOutcomeTool(Tool):
    """Append a governed Outcome revision from current passing evidence."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_post_rollback_long_term_outcome"

    @property
    def description(self) -> str:
        return (
            "把 exact rolled_back Outcome、recovered Matrix 与当前 passing 长期评估"
            "组合为 append-only Outcome revision 和 supersede event；保留回滚事实，"
            "不授予学习、推广或执行权限。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^evrerollbackreq_[0-9a-f]{24}$",
                },
                "subject_id": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9_-]{0,95}$",
                },
            },
            "required": ["request_id", "subject_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="回滚后长期 Outcome",
            search_hint=(
                "evolution post rollback long term outcome supersede recovery "
                "自进化 回滚 长期 结果 修订 替代"
            ),
        )

    async def execute(self, request_id: str, subject_id: str) -> str:
        try:
            view = await (
                self._engine.evolution_post_rollback_long_term_outcome_service.record(
                    request_id=str(request_id or "").strip(),
                    subject_id=str(subject_id or "").strip(),
                )
            )
        except (
            AttributeError,
            EvolutionPostRollbackLongTermOutcomeError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "post_rollback_long_term_outcome_failed")
            return f"回滚后长期 Outcome 未完成（`{code}`）：{exc}"
        return render_post_rollback_long_term_outcome(view)


class EvolutionPostRollbackRemoteLanePlacementTool(Tool):
    """Bind a missing remote lane to one exact active worker incarnation."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_post_rollback_remote_lane_placement"

    @property
    def description(self) -> str:
        return (
            "把 Coverage Contract 中一个 missing remote lane 绑定到 exact active "
            "Worker incarnation 和 release target；不检查健康、不预留容量、不执行任务。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^evrerollbackreq_[0-9a-f]{24}$",
                },
                "comparison_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "worker_id": {
                    "type": "string",
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                },
            },
            "required": ["request_id", "comparison_id", "worker_id"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="回滚后远端 Lane Placement",
            search_hint=(
                "evolution post rollback remote lane placement worker target "
                "自进化 回滚 远端 平台 worker"
            ),
        )

    async def execute(
        self,
        request_id: str,
        comparison_id: str,
        worker_id: str,
    ) -> str:
        try:
            view = await (
                self._engine.evolution_post_rollback_remote_lane_placement_service.place(
                    request_id=str(request_id or "").strip(),
                    comparison_id=str(comparison_id or "").strip(),
                    worker_id=str(worker_id or "").strip(),
                )
            )
        except (
            AttributeError,
            EvolutionPostRollbackRemoteLanePlacementError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "post_rollback_placement_failed")
            return f"回滚后远端 Lane Placement 未完成（`{code}`）：{exc}"
        return render_post_rollback_remote_lane_placement(view)


class EvolutionPostRollbackTargetBaselineTool(Tool):
    """Resolve the exact trusted target build for one placed remote lane."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_post_rollback_target_baseline"

    @property
    def description(self) -> str:
        return (
            "从受信 Release Channel Catalog 为 placed remote lane 解析与本机 baseline "
            "同 version/source 的 target-specific Build Attestation；不下载、不安装、不执行。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^evrerollbackreq_[0-9a-f]{24}$",
                },
                "comparison_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "channel": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9._-]{0,63}$",
                },
            },
            "required": ["request_id", "comparison_id", "channel"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="回滚后目标 Baseline Resolution",
            search_hint=(
                "evolution post rollback target baseline release channel catalog "
                "自进化 回滚 目标平台 构建证明"
            ),
        )

    async def execute(
        self,
        request_id: str,
        comparison_id: str,
        channel: str,
    ) -> str:
        try:
            view = await (
                self._engine.evolution_post_rollback_target_baseline_service.resolve(
                    request_id=str(request_id or "").strip(),
                    comparison_id=str(comparison_id or "").strip(),
                    channel=str(channel or "").strip(),
                )
            )
        except (
            AttributeError,
            EvolutionPostRollbackTargetBaselineError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "post_rollback_target_baseline_failed")
            return f"回滚后目标 Baseline Resolution 未完成（`{code}`）：{exc}"
        return render_post_rollback_target_baseline(view)


class EvolutionPostRollbackRemoteDispatchTool(Tool):
    """Queue one exact remote evaluation after health and capacity admission."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_post_rollback_remote_dispatch"

    @property
    def description(self) -> str:
        return (
            "消费 current Target Baseline、durable Worker Health 与 exact incarnation "
            "capacity，为远端行为 lane 原子预留一个 queued job；不授予 claim 或执行权限。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "pattern": "^evrerollbackreq_[0-9a-f]{24}$",
                },
                "comparison_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "channel": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9._-]{0,63}$",
                },
            },
            "required": ["request_id", "comparison_id", "channel"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="回滚后远端 Dispatch",
            search_hint=(
                "evolution post rollback remote dispatch health capacity reservation "
                "自进化 回滚 远端 派发 容量"
            ),
        )

    async def execute(
        self,
        request_id: str,
        comparison_id: str,
        channel: str,
    ) -> str:
        try:
            view = await (
                self._engine.evolution_post_rollback_remote_dispatch_service.queue(
                    request_id=str(request_id or "").strip(),
                    comparison_id=str(comparison_id or "").strip(),
                    channel=str(channel or "").strip(),
                )
            )
        except (
            AttributeError,
            EvolutionPostRollbackRemoteDispatchError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "post_rollback_remote_dispatch_failed")
            return f"回滚后远端 Dispatch 未完成（`{code}`）：{exc}"
        return render_post_rollback_remote_dispatch(view)


class EvolutionPostRollbackRemoteClaimTool(Tool):
    """Prepare, submit or renew an authenticated remote Worker claim."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_post_rollback_remote_claim"

    @property
    def description(self) -> str:
        return (
            "为 queued post-rollback Dispatch 准备一次性 Ed25519 challenge、"
            "提交 Worker signature 或准备短 lease renewal；不传输 baseline、不执行。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["prepare", "submit", "renew"]},
                "dispatch_id": {
                    "type": "string",
                    "pattern": "^evpostdispatch_[0-9a-f]{24}$",
                },
                "challenge_id": {
                    "type": "string",
                    "pattern": "^evpostclaimchallenge_[0-9a-f]{24}$",
                },
                "claim_id": {
                    "type": "string",
                    "pattern": "^evpostclaim_[0-9a-f]{24}$",
                },
                "signature_base64": {
                    "type": "string",
                    "pattern": "^[A-Za-z0-9+/]{86}==$",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="回滚后远端 Worker Claim",
            search_hint=(
                "evolution post rollback remote worker claim ed25519 challenge "
                "lease renew 自进化 回滚 远端 认证 领取 续租"
            ),
        )

    async def execute(
        self,
        action: str,
        dispatch_id: str = "",
        challenge_id: str = "",
        claim_id: str = "",
        signature_base64: str = "",
    ) -> str:
        normalized = str(action or "").strip().lower()
        try:
            service = self._engine.evolution_post_rollback_remote_claim_service
            if normalized == "prepare":
                if challenge_id or claim_id or signature_base64:
                    raise ValueError("prepare 只接受 dispatch_id。")
                challenge = await service.prepare_claim(
                    dispatch_id=str(dispatch_id or "").strip(),
                )
                return render_post_rollback_remote_claim_challenge(challenge)
            if normalized == "submit":
                if dispatch_id or claim_id:
                    raise ValueError("submit 只接受 challenge_id 与 signature_base64。")
                view = await service.submit(
                    challenge_id=str(challenge_id or "").strip(),
                    signature_base64=str(signature_base64 or "").strip(),
                )
                return render_post_rollback_remote_claim(view)
            if normalized == "renew":
                if dispatch_id or challenge_id or signature_base64:
                    raise ValueError("renew 只接受 claim_id。")
                challenge = await service.prepare_renewal(
                    claim_id=str(claim_id or "").strip(),
                )
                return render_post_rollback_remote_claim_challenge(challenge)
            raise ValueError("action 仅支持 prepare、submit 或 renew。")
        except (
            AttributeError,
            EvolutionPostRollbackRemoteClaimError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "post_rollback_remote_claim_failed")
            return f"回滚后远端 Claim 未完成（`{code}`）：{exc}"


class EvolutionPostRollbackRemoteDeliveryTool(Tool):
    """Prepare, inspect or acknowledge one encrypted remote baseline delivery."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_post_rollback_remote_delivery"

    @property
    def description(self) -> str:
        return (
            "为 current Remote Claim 生成 X25519+HKDF+AES-GCM baseline descriptor，"
            "或验证 exact Worker Ed25519 ACK；不安装、不执行评测。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["prepare", "submit", "inspect"]},
                "claim_id": {
                    "type": "string",
                    "pattern": "^evpostclaim_[0-9a-f]{24}$",
                },
                "delivery_id": {
                    "type": "string",
                    "pattern": "^evpostdelivery_[0-9a-f]{24}$",
                },
                "worker_signature_base64": {
                    "type": "string",
                    "pattern": "^[A-Za-z0-9+/]{86}==$",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="回滚后远端 Baseline 加密交付",
            search_hint=(
                "evolution post rollback remote baseline encrypted delivery "
                "x25519 hkdf aes ack 自进化 回滚 远端 加密 交付"
            ),
        )

    async def execute(
        self,
        action: str,
        claim_id: str = "",
        delivery_id: str = "",
        worker_signature_base64: str = "",
    ) -> str:
        normalized = str(action or "").strip().lower()
        try:
            service = self._engine.evolution_post_rollback_remote_delivery_service
            if normalized == "prepare":
                if delivery_id or worker_signature_base64:
                    raise ValueError("prepare 只接受 claim_id。")
                view = await service.prepare(claim_id=str(claim_id or "").strip())
                return render_post_rollback_remote_delivery_offer(view)
            if normalized == "submit":
                if claim_id:
                    raise ValueError("submit 只接受 delivery_id 与 Worker signature。")
                view = await service.submit_ack(
                    delivery_id=str(delivery_id or "").strip(),
                    worker_signature_base64=str(
                        worker_signature_base64 or ""
                    ).strip(),
                )
                return render_post_rollback_remote_delivery(view)
            if normalized == "inspect":
                if claim_id or worker_signature_base64:
                    raise ValueError("inspect 只接受 delivery_id。")
                view = await service.inspect(
                    delivery_id=str(delivery_id or "").strip()
                )
                return (
                    render_post_rollback_remote_delivery(view)
                    if view.receipt is not None
                    else render_post_rollback_remote_delivery_offer(view)
                )
            raise ValueError("action 仅支持 prepare、submit 或 inspect。")
        except (
            AttributeError,
            EvolutionPostRollbackRemoteDeliveryError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "post_rollback_remote_delivery_failed")
            return f"回滚后远端交付未完成（`{code}`）：{exc}"


class EvolutionPostRollbackRemoteExecutionAuthorizationTool(Tool):
    """Prepare, sign or inspect one exact remote evaluation start authority."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_post_rollback_remote_execution"

    @property
    def description(self) -> str:
        return (
            "为 current Delivery 生成 Worker Ed25519 Start challenge，或在签名后"
            "获取 exact Runtime lease 与 bash_run Run Grant；不接收结果。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["prepare", "submit", "inspect"]},
                "delivery_id": {
                    "type": "string",
                    "pattern": "^evpostdelivery_[0-9a-f]{24}$",
                },
                "challenge_id": {
                    "type": "string",
                    "pattern": "^evpoststart_[0-9a-f]{24}$",
                },
                "reference_id": {
                    "type": "string",
                    "pattern": "^evpost(?:attempt|start|execauth)_[0-9a-f]{24}$",
                },
                "worker_signature_base64": {
                    "type": "string",
                    "pattern": "^[A-Za-z0-9+/]{86}==$",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="回滚后远端执行授权",
            search_hint=(
                "evolution post rollback remote execution start authorization "
                "worker signature run grant lease 自进化 回滚 远端 执行 授权"
            ),
            delegated_tool_names=("bash_run",),
            requires_persistent_authorization=True,
        )

    async def execute(
        self,
        action: str,
        delivery_id: str = "",
        challenge_id: str = "",
        reference_id: str = "",
        worker_signature_base64: str = "",
    ) -> str:
        normalized = str(action or "").strip().lower()
        try:
            service = (
                self._engine.evolution_post_rollback_remote_execution_authorization_service
            )
            if normalized == "prepare":
                if challenge_id or reference_id or worker_signature_base64:
                    raise ValueError("prepare 只接受 delivery_id。")
                permission = current_permission_receipt()
                if permission is None:
                    raise ValueError("prepare 缺少本次调用的持久父权限回执。")
                view = await service.prepare(
                    delivery_id=str(delivery_id or "").strip(),
                    parent_permission_receipt_id=permission.receipt_id,
                )
                return render_post_rollback_remote_start_challenge(view)
            if normalized == "submit":
                if delivery_id or reference_id:
                    raise ValueError("submit 只接受 challenge_id 与 Worker signature。")
                view = await service.submit(
                    challenge_id=str(challenge_id or "").strip(),
                    worker_signature_base64=str(
                        worker_signature_base64 or ""
                    ).strip(),
                )
                return render_post_rollback_remote_execution_authorization(view)
            if normalized == "inspect":
                if delivery_id or challenge_id or worker_signature_base64:
                    raise ValueError("inspect 只接受 reference_id。")
                view = await service.inspect(
                    reference_id=str(reference_id or "").strip()
                )
                return render_post_rollback_remote_execution_authorization(view)
            raise ValueError("action 仅支持 prepare、submit 或 inspect。")
        except (
            AttributeError,
            EvolutionPostRollbackRemoteExecutionAuthorizationError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "post_rollback_remote_execution_failed")
            return f"回滚后远端执行授权未完成（`{code}`）：{exc}"


class EvolutionPostRollbackRemoteResultTool(Tool):
    """Ingest or inspect one Worker-signed remote Runtime Eval cohort."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "evolution_post_rollback_remote_result"

    @property
    def description(self) -> str:
        return (
            "验证 exact Worker Ed25519 签名与 execution authorization，原子准入"
            "完整 Runtime Eval cohort，并写入 canonical H5a/H5c；不授予学习或晋升权。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["submit", "inspect"]},
                "manifest_json": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": MAX_REMOTE_RESULT_MANIFEST_BYTES,
                },
                "manifest_id": {
                    "type": "string",
                    "pattern": "^evpostresultmanifest_[0-9a-f]{24}$",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="回滚后远端结果接收",
            search_hint=(
                "evolution post rollback remote signed result ingestion h5a h5c "
                "自进化 回滚 远端 签名 结果 接收"
            ),
        )

    async def execute(
        self,
        action: str,
        manifest_json: str = "",
        manifest_id: str = "",
    ) -> str:
        normalized = str(action or "").strip().lower()
        try:
            service = self._engine.evolution_post_rollback_remote_result_service
            if normalized == "submit":
                if manifest_id:
                    raise ValueError("submit 只接受 manifest_json。")
                manifest = EvolutionPostRollbackRemoteResultManifest.model_validate_json(
                    str(manifest_json or "")
                )
                view = await service.submit(manifest=manifest)
                return render_post_rollback_remote_result(view)
            if normalized == "inspect":
                if manifest_json:
                    raise ValueError("inspect 只接受 manifest_id。")
                view = await service.inspect(manifest_id=str(manifest_id or "").strip())
                return render_post_rollback_remote_result(view)
            raise ValueError("action 仅支持 submit 或 inspect。")
        except (
            AttributeError,
            EvolutionPostRollbackRemoteResultError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "post_rollback_remote_result_failed")
            return f"回滚后远端结果接收未完成（`{code}`）：{exc}"


def create_evolution_review_tools(
    engine: Any,
    service: EvolutionReviewService,
) -> list[Tool]:
    return [
        EvolutionCandidatesTool(engine, service),
        EvolutionExperimentContractAuthorityTool(engine),
        EvolutionExperimentContractIssueTool(engine),
        EvolutionEvaluationReceiptTool(engine),
        EvolutionEvaluationAggregationContractTool(engine),
        EvolutionFinalEvaluationReceiptTool(engine),
        EvolutionDecisionInputTool(engine),
        EvolutionMechanicalGateTool(engine),
        EvolutionIndependentReviewTool(engine),
        EvolutionCounterfactualEvidenceTool(engine),
        EvolutionRewardHackingEvidenceTool(engine),
        EvolutionDecisionStateTool(engine),
        EvolutionDecisionResolutionTool(engine),
        EvolutionReflectionMemoryTool(engine),
        EvolutionReflectionMemoryRevokeTool(engine),
        EvolutionPromotionPackageInputTool(engine),
        EvolutionPromotionPackageTool(engine),
        EvolutionPromotionApprovalRequirementTool(engine),
        EvolutionPromotionApprovalRequestTool(engine),
        EvolutionApprovalPrincipalAuthorityTool(engine),
        EvolutionApprovalPrincipalTool(engine),
        EvolutionApprovalSignatureAuthorityTool(engine),
        EvolutionApprovalSignatureTool(engine),
        EvolutionApprovalDecisionAuthorityTool(engine),
        EvolutionPromotionApprovalDecisionTool(engine),
        EvolutionRevalidationRequestAuthorityTool(engine),
        EvolutionRevalidationRequestTool(engine),
        EvolutionRevalidationReplayTool(engine),
        EvolutionRevalidationValidationTool(engine),
        EvolutionRevalidationOutcomeTool(engine),
        EvolutionRevalidationEvaluationPlanTool(engine),
        EvolutionRevalidationEvaluationSourceTool(engine),
        EvolutionRevalidationValidationPlanTool(engine),
        EvolutionRevalidationRuntimeContractTool(engine),
        EvolutionRevalidationRollbackExecutionTool(engine),
        EvolutionRevalidationRollbackOutcomeTool(engine),
        EvolutionProposalBeforeAfterEvidenceTool(engine),
        EvolutionPostRollbackRuntimeVerificationTool(engine),
        EvolutionPostRollbackBehavioralLaneTool(engine),
        EvolutionPostRollbackBehavioralCoverageTool(engine),
        EvolutionPostRollbackBehavioralMatrixTool(engine),
        EvolutionPostRollbackLongTermObservationContractTool(engine),
        EvolutionPostRollbackRuntimeObservationAdmissionTool(engine),
        EvolutionPostRollbackLongTermObservationAssessmentTool(engine),
        EvolutionPostRollbackLongTermOutcomeTool(engine),
        EvolutionPostRollbackRemoteLanePlacementTool(engine),
        EvolutionPostRollbackTargetBaselineTool(engine),
        EvolutionPostRollbackRemoteDispatchTool(engine),
        EvolutionPostRollbackRemoteClaimTool(engine),
        EvolutionPostRollbackRemoteDeliveryTool(engine),
        EvolutionPostRollbackRemoteExecutionAuthorizationTool(engine),
        EvolutionPostRollbackRemoteResultTool(engine),
        EvolutionStablePopulationCandidatePreviewTool(engine),
        EvolutionStablePopulationCompletionTool(engine),
        EvolutionStableRollbackReadinessTool(engine),
        EvolutionInstallationKeyTool(engine),
        EvolutionRolloutControlKeyTool(engine),
        EvolutionStableRemoteReadinessClaimTool(engine),
        EvolutionStableRemoteReadinessProbeTool(engine),
        EvolutionStableRemoteFinalizationAuthorizationTool(engine),
        EvolutionStableRemoteFinalizationTool(engine),
        EvolutionStableRemotePopulationFinalizationTool(engine),
        EvolutionStablePromotionObservationContractTool(engine),
        EvolutionStableRolloutAuthorizationTool(engine),
        EvolutionStableRolloutFinalizationTool(engine),
        EvolutionOutcomeOpportunityTool(engine),
        EvolutionProposalQueueTool(engine),
    ]


__all__ = [
    "EvolutionApprovalDecisionAuthorityTool",
    "EvolutionApprovalPrincipalAuthorityTool",
    "EvolutionApprovalPrincipalTool",
    "EvolutionApprovalSignatureAuthorityTool",
    "EvolutionApprovalSignatureTool",
    "EvolutionCandidatesTool",
    "EvolutionOutcomeOpportunityTool",
    "EvolutionCounterfactualEvidenceTool",
    "EvolutionDecisionInputTool",
    "EvolutionDecisionResolutionTool",
    "EvolutionDecisionStateTool",
    "EvolutionExperimentContractAuthorityTool",
    "EvolutionExperimentContractIssueTool",
    "EvolutionEvaluationAggregationContractTool",
    "EvolutionEvaluationReceiptTool",
    "EvolutionFinalEvaluationReceiptTool",
    "EvolutionIndependentReviewTool",
    "EvolutionInstallationKeyTool",
    "EvolutionMechanicalGateTool",
    "EvolutionProposalQueueTool",
    "EvolutionRolloutControlKeyTool",
    "EvolutionPromotionApprovalRequirementTool",
    "EvolutionPromotionApprovalDecisionTool",
    "EvolutionPromotionPackageInputTool",
    "EvolutionPromotionPackageTool",
    "EvolutionPostRollbackBehavioralLaneTool",
    "EvolutionPostRollbackBehavioralCoverageTool",
    "EvolutionPostRollbackBehavioralMatrixTool",
    "EvolutionPostRollbackLongTermObservationContractTool",
    "EvolutionPostRollbackLongTermObservationAssessmentTool",
    "EvolutionPostRollbackLongTermOutcomeTool",
    "EvolutionPostRollbackRuntimeObservationAdmissionTool",
    "EvolutionPostRollbackRemoteLanePlacementTool",
    "EvolutionPostRollbackRemoteDispatchTool",
    "EvolutionPostRollbackRemoteClaimTool",
    "EvolutionPostRollbackRemoteDeliveryTool",
    "EvolutionPostRollbackRemoteExecutionAuthorizationTool",
    "EvolutionPostRollbackRemoteResultTool",
    "EvolutionPostRollbackTargetBaselineTool",
    "EvolutionPostRollbackRuntimeVerificationTool",
    "EvolutionProposalBeforeAfterEvidenceTool",
    "EvolutionRewardHackingEvidenceTool",
    "EvolutionRevalidationRequestAuthorityTool",
    "EvolutionRevalidationRequestTool",
    "EvolutionRevalidationValidationTool",
    "EvolutionRevalidationOutcomeTool",
    "EvolutionRevalidationEvaluationPlanTool",
    "EvolutionRevalidationEvaluationSourceTool",
    "EvolutionRevalidationValidationPlanTool",
    "EvolutionRevalidationRuntimeContractTool",
    "EvolutionStablePopulationCandidatePreviewTool",
    "EvolutionStablePopulationCompletionTool",
    "EvolutionStableRollbackReadinessTool",
    "EvolutionStableRemoteReadinessClaimTool",
    "EvolutionStableRemoteReadinessProbeTool",
    "EvolutionStableRemoteFinalizationAuthorizationTool",
    "EvolutionStableRemoteFinalizationTool",
    "EvolutionStableRemotePopulationFinalizationTool",
    "EvolutionStablePromotionObservationContractTool",
    "EvolutionStableRolloutAuthorizationTool",
    "EvolutionStableRolloutFinalizationTool",
    "EvolutionRevalidationRollbackExecutionTool",
    "EvolutionRevalidationRollbackOutcomeTool",
    "EvolutionRevalidationReplayTool",
    "EvolutionReflectionMemoryRevokeTool",
    "EvolutionReflectionMemoryTool",
    "create_evolution_review_tools",
]
