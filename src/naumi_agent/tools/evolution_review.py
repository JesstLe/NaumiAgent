"""Agent-facing Evolution Candidate review and explicit queue tools."""

from __future__ import annotations

from typing import Any

from naumi_agent.evolution.adversarial_batch_requests import (
    EvolutionAdversarialBatchRequest,
)
from naumi_agent.evolution.decision_inputs import (
    EvolutionDecisionInputError,
    render_decision_input,
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
from naumi_agent.evolution.queue import render_queue_result
from naumi_agent.evolution.review import (
    EvolutionReviewFilter,
    EvolutionReviewService,
    render_evolution_review,
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
        except (EvolutionExperimentContractStoreError, OSError, TypeError, ValueError):
            return "Experiment Contract Authority 不可读取；请运行 /doctor 后重试。"
        if authority is None:
            return "当前工作区不存在该 Experiment Contract Authority。"
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


def create_evolution_review_tools(
    engine: Any,
    service: EvolutionReviewService,
) -> list[Tool]:
    return [
        EvolutionCandidatesTool(engine, service),
        EvolutionExperimentContractAuthorityTool(engine),
        EvolutionEvaluationReceiptTool(engine),
        EvolutionEvaluationAggregationContractTool(engine),
        EvolutionFinalEvaluationReceiptTool(engine),
        EvolutionDecisionInputTool(engine),
        EvolutionMechanicalGateTool(engine),
        EvolutionIndependentReviewTool(engine),
        EvolutionProposalQueueTool(engine),
    ]


__all__ = [
    "EvolutionCandidatesTool",
    "EvolutionDecisionInputTool",
    "EvolutionExperimentContractAuthorityTool",
    "EvolutionEvaluationAggregationContractTool",
    "EvolutionEvaluationReceiptTool",
    "EvolutionFinalEvaluationReceiptTool",
    "EvolutionIndependentReviewTool",
    "EvolutionMechanicalGateTool",
    "EvolutionProposalQueueTool",
    "create_evolution_review_tools",
]
