"""Agent-facing Evolution Candidate review and explicit queue tools."""

from __future__ import annotations

from typing import Any

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
from naumi_agent.evolution.promotion_package_inputs import (
    EvolutionPromotionPackageInputError,
    render_evolution_promotion_package_input,
)
from naumi_agent.evolution.promotion_packages import (
    EvolutionPromotionPackageError,
    render_evolution_promotion_package,
)
from naumi_agent.evolution.queue import render_queue_result
from naumi_agent.evolution.reflection_memories import (
    EvolutionReflectionMemoryError,
    EvolutionReflectionRevocationReason,
    render_evolution_reflection_memory,
)
from naumi_agent.evolution.revalidation_replays import (
    EvolutionRevalidationReplayError,
    render_evolution_revalidation_replay,
)
from naumi_agent.evolution.revalidation_requests import (
    EvolutionRevalidationRequestError,
    render_evolution_revalidation_request,
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
from naumi_agent.evolution.store import EvolutionStoreError
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
            EvolutionRevalidationReplayError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            return f"Evolution Revalidation Replay 未完成：{exc}"
        return render_evolution_revalidation_replay(receipt)


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
        EvolutionProposalQueueTool(engine),
    ]


__all__ = [
    "EvolutionApprovalDecisionAuthorityTool",
    "EvolutionApprovalPrincipalAuthorityTool",
    "EvolutionApprovalPrincipalTool",
    "EvolutionApprovalSignatureAuthorityTool",
    "EvolutionApprovalSignatureTool",
    "EvolutionCandidatesTool",
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
    "EvolutionMechanicalGateTool",
    "EvolutionProposalQueueTool",
    "EvolutionPromotionApprovalRequirementTool",
    "EvolutionPromotionApprovalDecisionTool",
    "EvolutionPromotionPackageInputTool",
    "EvolutionPromotionPackageTool",
    "EvolutionRewardHackingEvidenceTool",
    "EvolutionRevalidationRequestAuthorityTool",
    "EvolutionRevalidationRequestTool",
    "EvolutionRevalidationReplayTool",
    "EvolutionReflectionMemoryRevokeTool",
    "EvolutionReflectionMemoryTool",
    "create_evolution_review_tools",
]
