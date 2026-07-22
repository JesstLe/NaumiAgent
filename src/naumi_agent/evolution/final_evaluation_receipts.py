"""Final, coverage-complete Evolution evaluation receipts."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.adversarial_cohort import (
    EvolutionAdversarialCohortReceipt,
)
from naumi_agent.evolution.adversarial_cohort_receipts import (
    EvolutionAdversarialCohortReceiptStore,
    EvolutionAdversarialCohortReceiptStoreError,
)
from naumi_agent.evolution.evaluation_aggregation_contracts import (
    EvolutionEvaluationAggregationContract,
    EvolutionEvaluationAggregationContractError,
    EvolutionEvaluationAggregationContractStore,
)
from naumi_agent.evolution.evaluation_lane_receipts import (
    EvaluationLaneKind,
    EvolutionEvaluationCohortSummary,
    EvolutionEvaluationLaneReceipt,
    EvolutionEvaluationLaneReceiptError,
    EvolutionEvaluationLaneReceiptStore,
)
from naumi_agent.evolution.failure_attribution import (
    FailureAttributionAction,
    FailureAttributionCategory,
)

FINAL_EVALUATION_RECEIPT_POLICY = "evolution-final-evaluation-receipt-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionFinalEvaluationResourceSummary(_StrictModel):
    samples: int = Field(ge=1, le=40_000)
    duration_ms: float = Field(ge=0)
    observed_tokens: float | None = Field(default=None, ge=0)
    token_samples: int = Field(ge=0, le=40_000)
    observed_cost_usd: float | None = Field(default=None, ge=0)
    cost_samples: int = Field(ge=0, le=40_000)

    @model_validator(mode="after")
    def _coverage_is_consistent(self) -> Self:
        if (self.observed_tokens is None) != (self.token_samples == 0):
            raise ValueError("Final Evaluation token coverage 状态不一致。")
        if (self.observed_cost_usd is None) != (self.cost_samples == 0):
            raise ValueError("Final Evaluation cost coverage 状态不一致。")
        if self.token_samples > self.samples or self.cost_samples > self.samples:
            raise ValueError("Final Evaluation resource coverage 越界。")
        return self


class EvolutionFinalAdversarialLaneEvidence(_StrictModel):
    order: int = Field(ge=1, le=3)
    platform: Literal["linux", "macos", "windows"]
    lane_receipt: EvolutionEvaluationLaneReceipt
    red_completion: EvolutionAdversarialCohortReceipt
    green_completion: EvolutionAdversarialCohortReceipt


class EvolutionFinalEvaluationReceipt(_StrictModel):
    """Complete evidence input for EVO-04; never an acceptance decision itself."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-final-evaluation-receipt-v1"] = (
        FINAL_EVALUATION_RECEIPT_POLICY
    )
    receipt_id: str = Field(pattern=r"^evfinal_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    aggregation_contract_id: str = Field(pattern=r"^evagg_[0-9a-f]{24}$")
    aggregation_contract_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    required_platforms: tuple[Literal["linux", "macos", "windows"], ...] = Field(
        min_length=1,
        max_length=3,
    )
    lane_count: int = Field(ge=2, le=4)
    aggregation_contract: EvolutionEvaluationAggregationContract
    interventional_lane: EvolutionEvaluationLaneReceipt
    adversarial_lanes: tuple[EvolutionFinalAdversarialLaneEvidence, ...] = Field(
        min_length=1,
        max_length=3,
    )
    baseline_resources: EvolutionFinalEvaluationResourceSummary
    candidate_resources: EvolutionFinalEvaluationResourceSummary
    comparison_ids: tuple[str, ...] = Field(min_length=2, max_length=4)
    failure_categories: tuple[FailureAttributionCategory, ...] = Field(
        min_length=1,
        max_length=7,
    )
    failure_actions: tuple[FailureAttributionAction, ...] = Field(
        min_length=1,
        max_length=4,
    )
    any_candidate_fault: bool
    any_requires_rerun: bool
    all_reflection_eligible: bool
    evidence_first_at: str
    evidence_last_at: str
    candidate_evaluation_complete: Literal[True] = True
    aggregation_required: Literal[False] = False
    mechanical_gate_input_ready: Literal[True] = True
    candidate_acceptance_decided: Literal[False] = False
    promotion_ready: Literal[False] = False
    created_at: str

    @model_validator(mode="after")
    def _complete_and_tamper_evident(self) -> Self:
        contract = self.aggregation_contract
        if not (
            self.workspace_root == contract.workspace_root
            and self.aggregation_contract_id == contract.contract_id
            and self.aggregation_contract_sha256 == contract.contract_sha256
            and self.validation_plan_id == contract.validation_plan_id
            and self.validation_plan_sha256 == contract.validation_plan_sha256
            and self.candidate_id == contract.candidate_id
            and self.candidate_revision == contract.candidate_revision
            and self.required_platforms == contract.required_platforms
        ):
            raise ValueError("Final Evaluation projection 与 Aggregation Contract 不一致。")
        lanes = (self.interventional_lane,) + tuple(
            item.lane_receipt for item in self.adversarial_lanes
        )
        if self.lane_count != len(lanes) or self.lane_count != 1 + len(
            contract.required_platforms
        ):
            raise ValueError("Final Evaluation lane 数量不完整。")
        _validate_interventional_lane(contract, self.interventional_lane)
        if tuple(item.order for item in self.adversarial_lanes) != tuple(
            range(1, len(self.adversarial_lanes) + 1)
        ):
            raise ValueError("Final Evaluation adversarial lane 顺序不连续。")
        if tuple(item.platform for item in self.adversarial_lanes) != tuple(
            contract.required_platforms
        ):
            raise ValueError("Final Evaluation adversarial platform 覆盖不完整。")
        for expected, evidence in zip(
            contract.adversarial_lanes,
            self.adversarial_lanes,
            strict=True,
        ):
            _validate_adversarial_lane(contract, expected, evidence)
        receipt_ids = tuple(item.receipt_id for item in lanes)
        comparison_ids = tuple(item.comparison_id for item in lanes)
        if len(set(receipt_ids)) != len(receipt_ids) or len(set(comparison_ids)) != len(
            comparison_ids
        ):
            raise ValueError("Final Evaluation lane evidence 不得重复。")
        if self.comparison_ids != comparison_ids:
            raise ValueError("Final Evaluation comparison ids 与 lane 不一致。")
        baseline = _resource_summary(tuple(item.baseline for item in lanes))
        candidate = _resource_summary(tuple(item.candidate for item in lanes))
        if self.baseline_resources != baseline or self.candidate_resources != candidate:
            raise ValueError("Final Evaluation resource summary 与 lane 不一致。")
        categories = tuple(sorted({item.failure_category for item in lanes}))
        actions = tuple(sorted({item.failure_action for item in lanes}))
        if self.failure_categories != categories or self.failure_actions != actions:
            raise ValueError("Final Evaluation failure summary 与 lane 不一致。")
        if self.any_candidate_fault != any(item.candidate_fault for item in lanes):
            raise ValueError("Final Evaluation candidate fault 聚合不一致。")
        if self.any_requires_rerun != any(item.requires_rerun for item in lanes):
            raise ValueError("Final Evaluation rerun 聚合不一致。")
        if self.all_reflection_eligible != all(
            item.reflection_eligible for item in lanes
        ):
            raise ValueError("Final Evaluation reflection eligibility 聚合不一致。")
        parsed_first = tuple(datetime.fromisoformat(item.evidence_first_at) for item in lanes)
        parsed_last = tuple(datetime.fromisoformat(item.evidence_last_at) for item in lanes)
        expected_first = min(parsed_first).isoformat()
        expected_last = max(parsed_last).isoformat()
        if not (
            self.evidence_first_at == expected_first
            and self.evidence_last_at == expected_last
            and self.created_at == expected_last
        ):
            raise ValueError("Final Evaluation evidence 时间范围不一致。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )
        if not hmac.compare_digest(self.receipt_sha256, digest):
            raise ValueError("Final Evaluation Receipt 摘要不一致。")
        if self.receipt_id != f"evfinal_{digest[:24]}":
            raise ValueError("Final Evaluation Receipt identity 不一致。")
        return self


class EvolutionFinalEvaluationReceiptError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionFinalEvaluationReceiptBuilder:
    def build(
        self,
        *,
        contract: EvolutionEvaluationAggregationContract,
        interventional_lane: EvolutionEvaluationLaneReceipt,
        adversarial_evidence: tuple[EvolutionFinalAdversarialLaneEvidence, ...],
    ) -> EvolutionFinalEvaluationReceipt:
        try:
            authority = EvolutionEvaluationAggregationContract.model_validate(
                contract.model_dump(mode="json")
            )
            interventional = EvolutionEvaluationLaneReceipt.model_validate(
                interventional_lane.model_dump(mode="json")
            )
            adversarial = tuple(
                EvolutionFinalAdversarialLaneEvidence.model_validate(
                    item.model_dump(mode="json")
                )
                for item in adversarial_evidence
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionFinalEvaluationReceiptError(
                "final_evaluation_authority_invalid",
                "Final Evaluation authority 无效或已被篡改。",
            ) from exc
        lanes = (interventional,) + tuple(item.lane_receipt for item in adversarial)
        parsed_first = tuple(datetime.fromisoformat(item.evidence_first_at) for item in lanes)
        parsed_last = tuple(datetime.fromisoformat(item.evidence_last_at) for item in lanes)
        payload = {
            "schema_version": 1,
            "policy_version": FINAL_EVALUATION_RECEIPT_POLICY,
            "workspace_root": authority.workspace_root,
            "aggregation_contract_id": authority.contract_id,
            "aggregation_contract_sha256": authority.contract_sha256,
            "validation_plan_id": authority.validation_plan_id,
            "validation_plan_sha256": authority.validation_plan_sha256,
            "candidate_id": authority.candidate_id,
            "candidate_revision": authority.candidate_revision,
            "required_platforms": list(authority.required_platforms),
            "lane_count": len(lanes),
            "aggregation_contract": authority.model_dump(mode="json"),
            "interventional_lane": interventional.model_dump(mode="json"),
            "adversarial_lanes": [item.model_dump(mode="json") for item in adversarial],
            "baseline_resources": _resource_summary(
                tuple(item.baseline for item in lanes)
            ).model_dump(mode="json"),
            "candidate_resources": _resource_summary(
                tuple(item.candidate for item in lanes)
            ).model_dump(mode="json"),
            "comparison_ids": [item.comparison_id for item in lanes],
            "failure_categories": sorted({item.failure_category.value for item in lanes}),
            "failure_actions": sorted({item.failure_action.value for item in lanes}),
            "any_candidate_fault": any(item.candidate_fault for item in lanes),
            "any_requires_rerun": any(item.requires_rerun for item in lanes),
            "all_reflection_eligible": all(item.reflection_eligible for item in lanes),
            "evidence_first_at": min(parsed_first).isoformat(),
            "evidence_last_at": max(parsed_last).isoformat(),
            "candidate_evaluation_complete": True,
            "aggregation_required": False,
            "mechanical_gate_input_ready": True,
            "candidate_acceptance_decided": False,
            "promotion_ready": False,
            "created_at": max(parsed_last).isoformat(),
        }
        digest = _sha256_payload(payload)
        try:
            return EvolutionFinalEvaluationReceipt.model_validate({
                **payload,
                "receipt_id": f"evfinal_{digest[:24]}",
                "receipt_sha256": digest,
            })
        except ValueError as exc:
            raise EvolutionFinalEvaluationReceiptError(
                "final_evaluation_coverage_invalid",
                "Final Evaluation lane 覆盖或 authority 不完整。",
            ) from exc


class EvolutionFinalEvaluationReceiptStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(
        self,
        receipt: EvolutionFinalEvaluationReceipt,
    ) -> EvolutionFinalEvaluationReceipt:
        artifact = EvolutionFinalEvaluationReceipt.model_validate(
            receipt.model_dump(mode="json")
        )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_final_evaluation_receipts "
                        "WHERE aggregation_contract_id = ?",
                        (artifact.aggregation_contract_id,),
                    )
                ).fetchone()
                if row is not None:
                    restored = _from_row(row)
                    if restored != artifact:
                        await db.rollback()
                        raise EvolutionFinalEvaluationReceiptError(
                            "final_evaluation_store_conflict",
                            "同一 Aggregation Contract 不可覆盖为不同最终回执。",
                        )
                    await db.rollback()
                    return restored
                await db.execute(
                    "INSERT INTO evolution_final_evaluation_receipts "
                    "(aggregation_contract_id, receipt_id, receipt_sha256, receipt_json, "
                    "created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        artifact.aggregation_contract_id,
                        artifact.receipt_id,
                        artifact.receipt_sha256,
                        _json_dumps(artifact.model_dump(mode="json")),
                        artifact.created_at,
                    ),
                )
                await db.commit()
        except EvolutionFinalEvaluationReceiptError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionFinalEvaluationReceiptError(
                "final_evaluation_store_error",
                "Final Evaluation Receipt 无法持久化。",
            ) from exc
        restored = await self.get(artifact.aggregation_contract_id)
        assert restored is not None
        return restored

    async def get(
        self,
        aggregation_contract_id: str,
    ) -> EvolutionFinalEvaluationReceipt | None:
        if not isinstance(aggregation_contract_id, str) or re.fullmatch(
            r"evagg_[0-9a-f]{24}", aggregation_contract_id
        ) is None:
            raise ValueError("aggregation_contract_id 格式无效。")
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_final_evaluation_receipts "
                        "WHERE aggregation_contract_id = ?",
                        (aggregation_contract_id,),
                    )
                ).fetchone()
                return _from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionFinalEvaluationReceiptError(
                "final_evaluation_store_corrupt",
                "Final Evaluation Receipt 损坏或无法读取。",
            ) from exc


class EvolutionFinalEvaluationReceiptExecutor:
    def __init__(
        self,
        *,
        contract_store: EvolutionEvaluationAggregationContractStore,
        lane_store: EvolutionEvaluationLaneReceiptStore,
        cohort_store: EvolutionAdversarialCohortReceiptStore,
        receipt_store: EvolutionFinalEvaluationReceiptStore,
        builder: EvolutionFinalEvaluationReceiptBuilder | None = None,
    ) -> None:
        if not isinstance(contract_store, EvolutionEvaluationAggregationContractStore):
            raise TypeError("Final Evaluation executor 需要 Aggregation Contract Store。")
        if not isinstance(lane_store, EvolutionEvaluationLaneReceiptStore):
            raise TypeError("Final Evaluation executor 需要 Lane Receipt Store。")
        if not isinstance(cohort_store, EvolutionAdversarialCohortReceiptStore):
            raise TypeError("Final Evaluation executor 需要 Cohort Receipt Store。")
        if not isinstance(receipt_store, EvolutionFinalEvaluationReceiptStore):
            raise TypeError("Final Evaluation executor 需要 Final Receipt Store。")
        self._contract_store = contract_store
        self._lane_store = lane_store
        self._cohort_store = cohort_store
        self._receipt_store = receipt_store
        self._builder = builder or EvolutionFinalEvaluationReceiptBuilder()

    async def execute(
        self,
        *,
        aggregation_contract_id: str,
        interventional_comparison_id: str,
        adversarial_comparison_ids: tuple[str, ...],
    ) -> EvolutionFinalEvaluationReceipt:
        if len(set(adversarial_comparison_ids)) != len(adversarial_comparison_ids):
            raise EvolutionFinalEvaluationReceiptError(
                "final_evaluation_lane_duplicate",
                "Adversarial comparison ids 不得重复。",
            )
        try:
            contract = await self._contract_store.get(aggregation_contract_id)
            interventional = await self._lane_store.get(interventional_comparison_id)
            adversarial = tuple(
                await asyncio.gather(*(
                    self._lane_store.get(item) for item in adversarial_comparison_ids
                ))
            )
        except (
            EvolutionEvaluationAggregationContractError,
            EvolutionEvaluationLaneReceiptError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise EvolutionFinalEvaluationReceiptError(
                "final_evaluation_authority_read_failed",
                "无法读取 Final Evaluation authority。",
            ) from exc
        if contract is None:
            raise EvolutionFinalEvaluationReceiptError(
                "final_evaluation_contract_missing",
                "Aggregation Contract 不存在。",
            )
        if interventional is None:
            raise EvolutionFinalEvaluationReceiptError(
                "final_evaluation_interventional_missing",
                "Interventional Evaluation Lane Receipt 不存在。",
            )
        if any(item is None for item in adversarial):
            raise EvolutionFinalEvaluationReceiptError(
                "final_evaluation_adversarial_missing",
                "至少一个 Adversarial Evaluation Lane Receipt 不存在。",
            )
        typed_adversarial = tuple(item for item in adversarial if item is not None)
        if len(typed_adversarial) != len(contract.required_platforms):
            raise EvolutionFinalEvaluationReceiptError(
                "final_evaluation_platform_coverage_incomplete",
                "Adversarial comparison 数量未覆盖合同要求的全部平台。",
            )
        by_platform = {item.platform: item for item in typed_adversarial}
        if len(by_platform) != len(typed_adversarial) or tuple(sorted(by_platform)) != tuple(
            contract.required_platforms
        ):
            raise EvolutionFinalEvaluationReceiptError(
                "final_evaluation_platform_coverage_incomplete",
                "Adversarial comparison 平台未精确覆盖 Aggregation Contract。",
            )
        evidence = []
        for expected in contract.adversarial_lanes:
            lane = by_platform[expected.platform]
            try:
                red = await self._cohort_store.get(lane.red_completion_id)
                green = await self._cohort_store.get(lane.green_completion_id)
            except (
                EvolutionAdversarialCohortReceiptStoreError,
                OSError,
                TypeError,
                ValueError,
            ) as exc:
                raise EvolutionFinalEvaluationReceiptError(
                    "final_evaluation_completion_read_failed",
                    "无法读取 Adversarial completion receipt authority。",
                ) from exc
            if red is None or green is None:
                raise EvolutionFinalEvaluationReceiptError(
                    "final_evaluation_completion_missing",
                    "Adversarial lane 缺少 durable RED/GREEN completion receipt。",
                )
            evidence.append(EvolutionFinalAdversarialLaneEvidence(
                order=expected.order,
                platform=expected.platform,
                lane_receipt=lane,
                red_completion=red,
                green_completion=green,
            ))
        receipt = self._builder.build(
            contract=contract,
            interventional_lane=interventional,
            adversarial_evidence=tuple(evidence),
        )
        return await self._receipt_store.record(receipt)


def render_final_evaluation_receipt(receipt: EvolutionFinalEvaluationReceipt) -> str:
    artifact = EvolutionFinalEvaluationReceipt.model_validate(
        receipt.model_dump(mode="json")
    )
    lines = [
        f"# Final Evaluation Receipt `{artifact.receipt_id}`",
        "",
        "**评测证据已完整聚合；这不是 Candidate 接受或发布决定。**",
        "",
        f"- Candidate：`{artifact.candidate_id}` · revision {artifact.candidate_revision}",
        f"- Aggregation Contract：`{artifact.aggregation_contract_id}`",
        (
            f"- Lane：{artifact.lane_count}（Interventional 1 + "
            f"Adversarial {len(artifact.adversarial_lanes)}）"
        ),
        f"- 平台：{', '.join(f'`{item}`' for item in artifact.required_platforms)}",
        f"- Candidate fault：{'是' if artifact.any_candidate_fault else '否'}",
        f"- 需要重跑：{'是' if artifact.any_requires_rerun else '否'}",
        f"- 全部可进入 Reflection：{'是' if artifact.all_reflection_eligible else '否'}",
        "",
        "## Resource evidence",
        "",
        _render_resources("RED", artifact.baseline_resources),
        _render_resources("GREEN", artifact.candidate_resources),
        "",
        "## Mechanical facts",
        "",
        (
            "- Failure categories："
            + ", ".join(f"`{item.value}`" for item in artifact.failure_categories)
        ),
        f"- Required actions：{', '.join(f'`{item.value}`' for item in artifact.failure_actions)}",
        f"- Comparison receipts：{len(artifact.comparison_ids)}",
        f"- Receipt SHA-256：`{artifact.receipt_sha256}`",
        "",
        "下一步：EVO-04 mechanical gate 读取本回执；LLM 无权覆盖其中的失败与重跑事实。",
    ]
    return "\n".join(lines)


def _validate_interventional_lane(
    contract: EvolutionEvaluationAggregationContract,
    lane: EvolutionEvaluationLaneReceipt,
) -> None:
    origin = contract.batch_request.origin_platform
    platform_ok = (
        lane.platform == origin
        if origin != "unknown"
        else lane.platform in contract.required_platforms
    )
    if not (
        lane.lane_kind is EvaluationLaneKind.INTERVENTIONAL
        and lane.workspace_root == contract.workspace_root
        and lane.validation_plan_id == contract.validation_plan_id
        and lane.validation_plan_sha256 == contract.validation_plan_sha256
        and lane.candidate_id == contract.candidate_id
        and lane.candidate_revision == contract.candidate_revision
        and platform_ok
    ):
        raise ValueError("Final Evaluation Interventional lane authority 不一致。")


def _validate_adversarial_lane(contract, expected, evidence) -> None:
    lane = evidence.lane_receipt
    red = evidence.red_completion
    green = evidence.green_completion
    request = contract.batch_request
    common = (
        "request_id",
        "request_sha256",
        "validation_plan_id",
        "validation_plan_sha256",
        "candidate_id",
        "candidate_revision",
        "suite_id",
        "requested_samples",
    )
    if not (
        evidence.order == expected.order
        and evidence.platform == expected.platform
        and lane.lane_kind is EvaluationLaneKind.ADVERSARIAL
        and lane.platform == expected.platform
        and lane.workspace_root == contract.workspace_root
        and lane.validation_plan_id == contract.validation_plan_id
        and lane.validation_plan_sha256 == contract.validation_plan_sha256
        and lane.candidate_id == contract.candidate_id
        and lane.candidate_revision == contract.candidate_revision
        and lane.suite_id == contract.suite_id
        and lane.baseline.batch_id == expected.red_batch_id
        and lane.candidate.batch_id == expected.green_batch_id
        and lane.baseline.samples == lane.candidate.samples == contract.requested_samples
        and all(
            getattr(red, field) == getattr(green, field) == getattr(request, field)
            for field in common
        )
        and red.lane_order == expected.red_lane_order
        and green.lane_order == expected.green_lane_order
        and red.platform == green.platform == expected.platform
        and red.phase == "red"
        and green.phase == "green"
        and red.batch_id == expected.red_batch_id
        and green.batch_id == expected.green_batch_id
        and red.receipt_id == lane.red_completion_id
        and red.receipt_sha256 == lane.red_completion_sha256
        and green.receipt_id == lane.green_completion_id
        and green.receipt_sha256 == lane.green_completion_sha256
        and _sample_set_sha256(red.sample_result_sha256) == lane.baseline.samples_sha256
        and _sample_set_sha256(green.sample_result_sha256) == lane.candidate.samples_sha256
    ):
        raise ValueError("Final Evaluation Adversarial lane/request authority 不一致。")


def _resource_summary(
    summaries: tuple[EvolutionEvaluationCohortSummary, ...],
) -> EvolutionFinalEvaluationResourceSummary:
    tokens = tuple(item.observed_tokens for item in summaries if item.observed_tokens is not None)
    costs = tuple(
        item.observed_cost_usd for item in summaries if item.observed_cost_usd is not None
    )
    return EvolutionFinalEvaluationResourceSummary(
        samples=sum(item.samples for item in summaries),
        duration_ms=sum(item.duration_ms for item in summaries),
        observed_tokens=sum(tokens) if tokens else None,
        token_samples=sum(item.token_samples for item in summaries),
        observed_cost_usd=sum(costs) if costs else None,
        cost_samples=sum(item.cost_samples for item in summaries),
    )


def _render_resources(
    label: str,
    summary: EvolutionFinalEvaluationResourceSummary,
) -> str:
    tokens = (
        f"{summary.observed_tokens:g} ({summary.token_samples}/{summary.samples})"
        if summary.observed_tokens is not None
        else "未观测"
    )
    cost = (
        f"${summary.observed_cost_usd:g} ({summary.cost_samples}/{summary.samples})"
        if summary.observed_cost_usd is not None
        else "未观测"
    )
    return (
        f"- {label}：samples {summary.samples} · duration {summary.duration_ms:.0f}ms · "
        f"tokens {tokens} · cost {cost}"
    )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS evolution_final_evaluation_receipts (
            aggregation_contract_id TEXT PRIMARY KEY,
            receipt_id TEXT NOT NULL UNIQUE,
            receipt_sha256 TEXT NOT NULL,
            receipt_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def _from_row(row: aiosqlite.Row) -> EvolutionFinalEvaluationReceipt:
    artifact = EvolutionFinalEvaluationReceipt.model_validate_json(
        str(row["receipt_json"])
    )
    if not (
        row["aggregation_contract_id"] == artifact.aggregation_contract_id
        and row["receipt_id"] == artifact.receipt_id
        and row["receipt_sha256"] == artifact.receipt_sha256
        and row["created_at"] == artifact.created_at
    ):
        raise ValueError("Final Evaluation Store row 与 payload 不一致。")
    return artifact


def _sample_set_sha256(result_digests: tuple[str, ...]) -> str:
    return _sha256_payload([
        {"sample_index": index, "result_sha256": digest}
        for index, digest in enumerate(result_digests)
    ])


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(_json_dumps(payload).encode("utf-8")).hexdigest()


def _json_dumps(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "FINAL_EVALUATION_RECEIPT_POLICY",
    "EvolutionFinalAdversarialLaneEvidence",
    "EvolutionFinalEvaluationReceipt",
    "EvolutionFinalEvaluationReceiptBuilder",
    "EvolutionFinalEvaluationReceiptError",
    "EvolutionFinalEvaluationReceiptExecutor",
    "EvolutionFinalEvaluationReceiptStore",
    "EvolutionFinalEvaluationResourceSummary",
    "render_final_evaluation_receipt",
]
