"""Proposal-bound implementation before/after evidence for durable Outcomes."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.final_evaluation_receipts import (
    EvolutionFinalEvaluationReceipt,
    EvolutionFinalEvaluationReceiptError,
    EvolutionFinalEvaluationReceiptStore,
)
from naumi_agent.evolution.promotion_package_inputs import (
    EvolutionPromotionPackageInput,
)
from naumi_agent.evolution.revalidation_promotion_inputs import (
    EvolutionRevalidationPromotionInput,
    EvolutionRevalidationPromotionInputError,
    EvolutionRevalidationPromotionInputStore,
)
from naumi_agent.evolution.revalidation_rollback_outcomes import (
    EvolutionRevalidationRollbackOutcome,
    EvolutionRevalidationRollbackOutcomeError,
    EvolutionRevalidationRollbackOutcomeService,
)
from naumi_agent.harness.eval_receipt import (
    EvalComparisonDecision,
    HarnessEvalComparisonReceipt,
)
from naumi_agent.harness.eval_statistics import EvalStatisticalVerdict
from naumi_agent.harness.store import HarnessStore, HarnessStoreError

EVOLUTION_PROPOSAL_BEFORE_AFTER_EVIDENCE_POLICY = (
    "evolution-proposal-before-after-evidence-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_SAFE_BINDING_RE = r"^[^\x00\r\n]{1,128}$"
_MAX_ARTIFACT_BYTES = 512 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionProposalBeforeAfterCohort(_StrictModel):
    batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    identity_sha256: str = Field(pattern=_SHA256_RE)
    samples: int = Field(ge=1, le=10_000)
    samples_sha256: str = Field(pattern=_SHA256_RE)
    passed_samples: int = Field(ge=0, le=10_000)
    failed_samples: int = Field(ge=0, le=10_000)
    evaluation_error_samples: int = Field(ge=0, le=10_000)
    passed_cases: int = Field(ge=0, le=1_000_000)
    implementation_failures: int = Field(ge=0, le=1_000_000)
    evaluation_errors: int = Field(ge=0, le=1_000_000)
    skipped_cases: int = Field(ge=0, le=1_000_000)
    duration_ms: float = Field(ge=0)
    observed_tokens: float | None = Field(default=None, ge=0)
    token_samples: int = Field(ge=0, le=10_000)
    observed_cost_usd: float | None = Field(default=None, ge=0)
    cost_samples: int = Field(ge=0, le=10_000)

    @model_validator(mode="after")
    def _coverage_is_complete(self) -> Self:
        if (
            self.passed_samples + self.failed_samples + self.evaluation_error_samples
            != self.samples
        ):
            raise ValueError("Before/After cohort sample 状态计数不完整。")
        if (self.observed_tokens is None) != (self.token_samples == 0):
            raise ValueError("Before/After cohort token coverage 不一致。")
        if (self.observed_cost_usd is None) != (self.cost_samples == 0):
            raise ValueError("Before/After cohort cost coverage 不一致。")
        if self.token_samples > self.samples or self.cost_samples > self.samples:
            raise ValueError("Before/After cohort resource coverage 越界。")
        return self


class EvolutionProposalBeforeAfterLane(_StrictModel):
    order: int = Field(ge=1, le=4)
    lane_kind: Literal["interventional", "adversarial"]
    platform: Literal["linux", "macos", "windows", "unknown"]
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    comparison_id: str = Field(pattern=_SHA256_RE)
    comparison_receipt_sha256: str = Field(pattern=_SHA256_RE)
    baseline_id: str = Field(pattern=_SHA256_RE)
    decision: EvalComparisonDecision
    statistical_verdict: EvalStatisticalVerdict
    statistical_code: str = Field(max_length=128)
    before: EvolutionProposalBeforeAfterCohort
    after: EvolutionProposalBeforeAfterCohort


class EvolutionProposalBeforeAfterEvidence(_StrictModel):
    """Immutable implementation comparison; not post-rollback or learning evidence."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-proposal-before-after-evidence-v1"] = (
        EVOLUTION_PROPOSAL_BEFORE_AFTER_EVIDENCE_POLICY
    )
    evidence_id: str = Field(pattern=r"^evbeforeafter_[0-9a-f]{24}$")
    evidence_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    evidence_kind: Literal["implementation_before_after"] = (
        "implementation_before_after"
    )
    outcome_id: str = Field(pattern=r"^evrerollbackout_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^evrerollbackreq_[0-9a-f]{24}$")
    workbench_session_id: str = Field(pattern=_SAFE_BINDING_RE)
    workbench_proposal_id: str = Field(pattern=_SAFE_BINDING_RE)
    experiment_contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    experiment_contract_sha256: str = Field(pattern=_SHA256_RE)
    fresh_promotion_input_id: str = Field(pattern=r"^evrevalpromoin_[0-9a-f]{24}$")
    fresh_promotion_input_sha256: str = Field(pattern=_SHA256_RE)
    prior_promotion_input_id: str = Field(pattern=r"^evpromoin_[0-9a-f]{24}$")
    prior_promotion_input_sha256: str = Field(pattern=_SHA256_RE)
    final_evaluation_id: str = Field(pattern=r"^evfinal_[0-9a-f]{24}$")
    final_evaluation_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_sha256: str = Field(pattern=_SHA256_RE)
    lane_count: int = Field(ge=2, le=4)
    lanes: tuple[EvolutionProposalBeforeAfterLane, ...] = Field(
        min_length=2,
        max_length=4,
    )
    baseline_evidence_complete: Literal[True] = True
    candidate_evidence_complete: Literal[True] = True
    before_after_recorded: Literal[True] = True
    post_rollback_evaluation_recorded: Literal[False] = False
    long_term_metrics_recorded: Literal[False] = False
    promoted: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    recorded_at: str = Field(min_length=1, max_length=100)

    @field_validator("recorded_at")
    @classmethod
    def _aware_time(cls, value: str) -> str:
        return _aware(value).isoformat()

    @model_validator(mode="after")
    def _exact_and_tamper_evident(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Before/After workspace 必须是 canonical 绝对路径。")
        if any(char in self.workspace_root for char in ("\x00", "\r", "\n")):
            raise ValueError("Before/After workspace 包含不安全字符。")
        if self.lane_count != len(self.lanes):
            raise ValueError("Before/After lane_count 与 evidence 不一致。")
        if tuple(item.order for item in self.lanes) != tuple(
            range(1, self.lane_count + 1)
        ):
            raise ValueError("Before/After lanes 必须连续排序。")
        if self.lanes[0].lane_kind != "interventional" or any(
            item.lane_kind != "adversarial" for item in self.lanes[1:]
        ):
            raise ValueError("Before/After lane 类型或顺序不完整。")
        comparison_ids = tuple(item.comparison_id for item in self.lanes)
        if len(set(comparison_ids)) != len(comparison_ids):
            raise ValueError("Before/After comparison receipt 不得重复。")
        digest = _digest(
            self.model_dump(mode="json", exclude={"evidence_id", "evidence_sha256"})
        )
        if not hmac.compare_digest(self.evidence_sha256, digest):
            raise ValueError("Before/After Evidence 摘要不一致。")
        if self.evidence_id != f"evbeforeafter_{digest[:24]}":
            raise ValueError("Before/After Evidence identity 不一致。")
        return self


class EvolutionProposalBeforeAfterEvidenceView(_StrictModel):
    evidence: EvolutionProposalBeforeAfterEvidence
    durable_dependencies_valid: bool
    outcome_authority: bool
    comparison_authority: bool
    before_after_authority: bool
    post_rollback_evaluation_authority: Literal[False] = False
    long_term_metrics_authority: Literal[False] = False
    learning_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _authority_is_exact(self) -> Self:
        expected = bool(
            self.durable_dependencies_valid
            and self.outcome_authority
            and self.comparison_authority
        )
        if self.before_after_authority is not expected:
            raise ValueError("Before/After authority 投影不一致。")
        return self


class EvolutionProposalBeforeAfterEvidenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionProposalBeforeAfterEvidenceBuilder:
    def build(
        self,
        *,
        outcome: EvolutionRevalidationRollbackOutcome,
        fresh_input: EvolutionRevalidationPromotionInput,
        prior_input: EvolutionPromotionPackageInput,
        final_evaluation: EvolutionFinalEvaluationReceipt,
        recorded_at: str,
    ) -> EvolutionProposalBeforeAfterEvidence:
        try:
            rollback = EvolutionRevalidationRollbackOutcome.model_validate_json(
                outcome.model_dump_json()
            )
            fresh = EvolutionRevalidationPromotionInput.model_validate_json(
                fresh_input.model_dump_json()
            )
            prior = EvolutionPromotionPackageInput.model_validate_json(
                prior_input.model_dump_json()
            )
            final = EvolutionFinalEvaluationReceipt.model_validate_json(
                final_evaluation.model_dump_json()
            )
            _validate_lineage(rollback, fresh, prior, final)
            recorded = _aware(recorded_at)
            if recorded < _aware(rollback.recorded_at):
                raise ValueError("recorded_at 早于 Outcome。")
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionProposalBeforeAfterEvidenceError(
                "proposal_before_after_lineage_invalid",
                "Proposal Before/After 的 Outcome、Promotion Input 或 Final Evaluation 绑定无效。",
            ) from exc
        lanes = _lanes(final)
        payload = {
            "schema_version": 1,
            "policy_version": EVOLUTION_PROPOSAL_BEFORE_AFTER_EVIDENCE_POLICY,
            "workspace_root": rollback.workspace_root,
            "evidence_kind": "implementation_before_after",
            "outcome_id": rollback.outcome_id,
            "outcome_sha256": rollback.outcome_sha256,
            "request_id": rollback.request_id,
            "workbench_session_id": rollback.workbench_session_id,
            "workbench_proposal_id": rollback.workbench_proposal_id,
            "experiment_contract_id": rollback.experiment_contract_id,
            "experiment_contract_sha256": rollback.experiment_contract_sha256,
            "fresh_promotion_input_id": fresh.input_id,
            "fresh_promotion_input_sha256": fresh.input_sha256,
            "prior_promotion_input_id": prior.input_id,
            "prior_promotion_input_sha256": prior.input_sha256,
            "final_evaluation_id": final.receipt_id,
            "final_evaluation_sha256": final.receipt_sha256,
            "candidate_id": rollback.candidate_id,
            "candidate_revision": rollback.candidate_revision,
            "candidate_sha256": rollback.candidate_sha256,
            "lane_count": len(lanes),
            "lanes": [item.model_dump(mode="json") for item in lanes],
            "baseline_evidence_complete": True,
            "candidate_evidence_complete": True,
            "before_after_recorded": True,
            "post_rollback_evaluation_recorded": False,
            "long_term_metrics_recorded": False,
            "promoted": False,
            "learning_authority": False,
            "promotion_authority": False,
            "recorded_at": recorded.isoformat(),
        }
        digest = _digest(payload)
        try:
            return EvolutionProposalBeforeAfterEvidence.model_validate({
                **payload,
                "evidence_id": f"evbeforeafter_{digest[:24]}",
                "evidence_sha256": digest,
            })
        except ValueError as exc:
            raise EvolutionProposalBeforeAfterEvidenceError(
                "proposal_before_after_artifact_invalid",
                "Proposal Before/After Evidence 无法验证。",
            ) from exc


class EvolutionProposalBeforeAfterEvidenceStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def record(
        self,
        evidence: EvolutionProposalBeforeAfterEvidence,
    ) -> EvolutionProposalBeforeAfterEvidence:
        try:
            item = EvolutionProposalBeforeAfterEvidence.model_validate_json(
                evidence.model_dump_json()
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionProposalBeforeAfterEvidenceError(
                "proposal_before_after_artifact_invalid",
                "Proposal Before/After Evidence artifact 无效。",
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionProposalBeforeAfterEvidenceError(
                "proposal_before_after_artifact_oversized",
                "Proposal Before/After Evidence 超过 512 KiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                await _require_dependencies(db, item)
                row = await (
                    await db.execute(
                        "SELECT evidence_json FROM evolution_proposal_before_after_evidence "
                        "WHERE outcome_id = ?",
                        (item.outcome_id,),
                    )
                ).fetchone()
                if row is not None:
                    restored = _restore(row["evidence_json"])
                    await db.rollback()
                    if restored != item:
                        raise EvolutionProposalBeforeAfterEvidenceError(
                            "proposal_before_after_conflict",
                            "同一 Outcome 不可覆盖为不同 Before/After Evidence。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_proposal_before_after_evidence "
                    "(evidence_id, evidence_sha256, outcome_id, request_id, "
                    "workbench_session_id, workbench_proposal_id, evidence_json, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.evidence_id,
                        item.evidence_sha256,
                        item.outcome_id,
                        item.request_id,
                        item.workbench_session_id,
                        item.workbench_proposal_id,
                        encoded,
                        item.recorded_at,
                    ),
                )
                await db.commit()
        except EvolutionProposalBeforeAfterEvidenceError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionProposalBeforeAfterEvidenceError(
                "proposal_before_after_store_error",
                "Proposal Before/After Evidence 无法持久化。",
            ) from exc
        restored = await self.get_by_outcome(item.outcome_id)
        assert restored is not None
        return restored

    async def get_by_outcome(
        self,
        outcome_id: str,
    ) -> EvolutionProposalBeforeAfterEvidence | None:
        if re.fullmatch(r"evrerollbackout_[0-9a-f]{24}", str(outcome_id)) is None:
            raise ValueError("Outcome ID 格式无效。")
        if not self.db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        "SELECT evidence_json FROM evolution_proposal_before_after_evidence "
                        "WHERE outcome_id = ?",
                        (outcome_id,),
                    )
                ).fetchone()
            return None if row is None else _restore(row["evidence_json"])
        except EvolutionProposalBeforeAfterEvidenceError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionProposalBeforeAfterEvidenceError(
                "proposal_before_after_store_corrupt",
                "Proposal Before/After Evidence 损坏或无法读取。",
            ) from exc


class EvolutionProposalBeforeAfterEvidenceService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        outcome_service: EvolutionRevalidationRollbackOutcomeService,
        fresh_input_store: EvolutionRevalidationPromotionInputStore,
        final_evaluation_store: EvolutionFinalEvaluationReceiptStore,
        harness_store: HarnessStore,
        evidence_store: EvolutionProposalBeforeAfterEvidenceStore,
        builder: EvolutionProposalBeforeAfterEvidenceBuilder | None = None,
        now=None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.outcome_service = outcome_service
        self.fresh_input_store = fresh_input_store
        self.final_evaluation_store = final_evaluation_store
        self.harness_store = harness_store
        self.evidence_store = evidence_store
        self.builder = builder or EvolutionProposalBeforeAfterEvidenceBuilder()
        self.now = now or (lambda: datetime.now(UTC).isoformat())

    async def record(
        self,
        *,
        request_id: str,
    ) -> EvolutionProposalBeforeAfterEvidenceView:
        outcome, fresh, prior, final, comparison_valid = await self._sources(request_id)
        if not comparison_valid:
            raise EvolutionProposalBeforeAfterEvidenceError(
                "proposal_before_after_comparison_stale",
                "Final Evaluation 引用的 HAR-08 Comparison authority 已变化。",
            )
        evidence = self.builder.build(
            outcome=outcome,
            fresh_input=fresh,
            prior_input=prior,
            final_evaluation=final,
            recorded_at=self.now(),
        )
        recorded = await self.evidence_store.record(evidence)
        view = await self._view(recorded)
        if not view.before_after_authority:
            raise EvolutionProposalBeforeAfterEvidenceError(
                "proposal_before_after_authority_changed",
                "Proposal Before/After authority 在持久化期间发生变化，已失败关闭。",
            )
        return view

    async def inspect(
        self,
        *,
        evidence: EvolutionProposalBeforeAfterEvidence,
    ) -> EvolutionProposalBeforeAfterEvidenceView:
        return await self._view(
            EvolutionProposalBeforeAfterEvidence.model_validate_json(
                evidence.model_dump_json()
            )
        )

    async def _sources(self, request_id: str):
        normalized = str(request_id or "").strip()
        if re.fullmatch(r"evrerollbackreq_[0-9a-f]{24}", normalized) is None:
            raise EvolutionProposalBeforeAfterEvidenceError(
                "proposal_before_after_request_id_invalid",
                "Rollback Request ID 格式无效。",
            )
        try:
            outcome_view = await self.outcome_service.inspect(request_id=normalized)
            outcome = outcome_view.outcome
            fresh = await self.fresh_input_store.get(outcome.runtime_contract_id)
            if fresh is None:
                raise ValueError("fresh input missing")
            prior = fresh.prior_input
            final = await self.final_evaluation_store.get_by_receipt_id(
                prior.final_evaluation_receipt_id
            )
            if final is None:
                raise ValueError("final evaluation missing")
            _validate_lineage(outcome, fresh, prior, final)
            comparisons = await _reload_comparisons(
                self.harness_store,
                self.workspace_root,
                final,
            )
        except EvolutionProposalBeforeAfterEvidenceError:
            raise
        except (
            EvolutionFinalEvaluationReceiptError,
            EvolutionRevalidationPromotionInputError,
            EvolutionRevalidationRollbackOutcomeError,
            HarnessStoreError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise EvolutionProposalBeforeAfterEvidenceError(
                "proposal_before_after_source_invalid",
                "Proposal Before/After 的 durable source 不完整、越界或已损坏。",
            ) from exc
        if not outcome_view.outcome_authority:
            raise EvolutionProposalBeforeAfterEvidenceError(
                "proposal_before_after_outcome_stale",
                "Rollback Outcome authority 当前无效。",
            )
        comparison_valid = _comparisons_match(final, comparisons)
        return outcome, fresh, prior, final, comparison_valid

    async def _view(
        self,
        item: EvolutionProposalBeforeAfterEvidence,
    ) -> EvolutionProposalBeforeAfterEvidenceView:
        durable = outcome_authority = comparison_authority = False
        try:
            outcome, fresh, prior, final, comparison_authority = await self._sources(
                item.request_id
            )
            rebuilt = self.builder.build(
                outcome=outcome,
                fresh_input=fresh,
                prior_input=prior,
                final_evaluation=final,
                recorded_at=item.recorded_at,
            )
            durable = rebuilt == item
            outcome_authority = True
        except EvolutionProposalBeforeAfterEvidenceError:
            pass
        authority = bool(durable and outcome_authority and comparison_authority)
        return EvolutionProposalBeforeAfterEvidenceView(
            evidence=item,
            durable_dependencies_valid=durable,
            outcome_authority=outcome_authority,
            comparison_authority=comparison_authority,
            before_after_authority=authority,
            post_rollback_evaluation_authority=False,
            long_term_metrics_authority=False,
            learning_authority=False,
            promotion_authority=False,
        )


def render_proposal_before_after_evidence(
    view: EvolutionProposalBeforeAfterEvidenceView,
) -> str:
    item = view.evidence
    lines = [
        f"# Proposal Before/After Evidence `{item.evidence_id}`",
        "",
        (
            "**已登记 Proposal 实施前 RED baseline 与实施后 GREEN candidate 的 "
            "HAR-08 比较；这不是回滚后评测或长期成效证明。**"
        ),
        "",
        f"- Proposal：`{item.workbench_proposal_id}`",
        f"- Outcome：`{item.outcome_id}` · rolled_back",
        f"- Candidate：`{item.candidate_id}` · revision {item.candidate_revision}",
        f"- Final Evaluation：`{item.final_evaluation_id}`",
        f"- Authority：{'有效' if view.before_after_authority else '无效'}",
        f"- Lane：{item.lane_count}",
        "",
        "## Implementation comparisons",
        "",
    ]
    for lane in item.lanes:
        lines.append(
            f"- {lane.order}. {lane.lane_kind} · {lane.platform} · `{lane.suite_id}` · "
            f"{lane.decision.value} / {lane.statistical_verdict.value} · "
            f"before {lane.before.passed_samples}/{lane.before.samples} → "
            f"after {lane.after.passed_samples}/{lane.after.samples}"
        )
    lines.extend([
        "",
        "- 回滚后评测：尚未记录",
        "- 长期指标：尚未记录",
        "- 权限：不授予 policy learning 或 promotion authority",
    ])
    return "\n".join(lines)


def _validate_lineage(
    outcome: EvolutionRevalidationRollbackOutcome,
    fresh: EvolutionRevalidationPromotionInput,
    prior: EvolutionPromotionPackageInput,
    final: EvolutionFinalEvaluationReceipt,
) -> None:
    if not (
        outcome.workspace_root
        == fresh.workspace_root
        == prior.workspace_root
        == final.workspace_root
        and outcome.promotion_input_id == fresh.input_id
        and outcome.promotion_input_sha256 == fresh.input_sha256
        and outcome.runtime_contract_id == fresh.contract_id
        and outcome.runtime_contract_sha256 == fresh.contract_sha256
        and outcome.prior_input_id == fresh.prior_input_id == prior.input_id
        and outcome.prior_input_sha256 == fresh.prior_input_sha256 == prior.input_sha256
        and fresh.prior_input == prior
        and outcome.experiment_contract_id == prior.experiment_contract_id
        and outcome.experiment_contract_sha256 == prior.experiment_contract_sha256
        and prior.final_evaluation_receipt_id == final.receipt_id
        and prior.final_evaluation_receipt_sha256 == final.receipt_sha256
        and outcome.candidate_id == fresh.candidate_id == prior.candidate_id == final.candidate_id
        and outcome.candidate_revision
        == fresh.candidate_revision
        == prior.candidate_revision
        == final.candidate_revision
        and outcome.candidate_sha256 == prior.candidate_sha256
        and prior.evaluation_lane_count == final.lane_count
        and prior.required_platforms == final.required_platforms
    ):
        raise ValueError("Proposal Before/After lineage 不一致。")
    if not (
        _aware(final.created_at) <= _aware(prior.created_at) <= _aware(outcome.recorded_at)
    ):
        raise ValueError("Proposal Before/After evidence 时间顺序无效。")


def _lanes(
    final: EvolutionFinalEvaluationReceipt,
) -> tuple[EvolutionProposalBeforeAfterLane, ...]:
    source_lanes = (final.interventional_lane,) + tuple(
        item.lane_receipt for item in final.adversarial_lanes
    )
    return tuple(
        EvolutionProposalBeforeAfterLane(
            order=index,
            lane_kind=lane.lane_kind.value,
            platform=lane.platform,
            suite_id=lane.suite_id,
            comparison_id=lane.comparison_id,
            comparison_receipt_sha256=lane.comparison_receipt_sha256,
            baseline_id=lane.comparison_receipt.baseline_id,
            decision=lane.comparison_decision,
            statistical_verdict=lane.statistical_verdict,
            statistical_code=lane.statistical_code,
            before=EvolutionProposalBeforeAfterCohort.model_validate(
                lane.baseline.model_dump(mode="json")
            ),
            after=EvolutionProposalBeforeAfterCohort.model_validate(
                lane.candidate.model_dump(mode="json")
            ),
        )
        for index, lane in enumerate(source_lanes, start=1)
    )


async def _reload_comparisons(
    store: HarnessStore,
    workspace: Path,
    final: EvolutionFinalEvaluationReceipt,
) -> tuple[HarnessEvalComparisonReceipt | None, ...]:
    lanes = (final.interventional_lane,) + tuple(
        item.lane_receipt for item in final.adversarial_lanes
    )
    records = []
    for lane in lanes:
        stored = await store.get_eval_comparison_receipt_by_id(
            workspace,
            lane.comparison_id,
        )
        records.append(None if stored is None else stored.receipt)
    return tuple(records)


def _comparisons_match(
    final: EvolutionFinalEvaluationReceipt,
    comparisons: tuple[HarnessEvalComparisonReceipt | None, ...],
) -> bool:
    lanes = (final.interventional_lane,) + tuple(
        item.lane_receipt for item in final.adversarial_lanes
    )
    return len(comparisons) == len(lanes) and all(
        stored is not None and stored == lane.comparison_receipt
        for lane, stored in zip(lanes, comparisons, strict=True)
    )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_proposal_before_after_evidence ("
        "evidence_id TEXT PRIMARY KEY, evidence_sha256 TEXT NOT NULL UNIQUE, "
        "outcome_id TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL UNIQUE, "
        "workbench_session_id TEXT NOT NULL, workbench_proposal_id TEXT NOT NULL UNIQUE, "
        "evidence_json TEXT NOT NULL, recorded_at TEXT NOT NULL)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_evolution_before_after_session "
        "ON evolution_proposal_before_after_evidence(workbench_session_id, recorded_at)"
    )
    await db.commit()


async def _require_dependencies(
    db: aiosqlite.Connection,
    item: EvolutionProposalBeforeAfterEvidence,
) -> None:
    checks = (
        (
            "SELECT outcome_sha256 FROM evolution_revalidation_rollback_outcomes "
            "WHERE outcome_id = ?",
            item.outcome_id,
            item.outcome_sha256,
        ),
        (
            "SELECT input_sha256 FROM evolution_revalidation_promotion_inputs "
            "WHERE input_id = ?",
            item.fresh_promotion_input_id,
            item.fresh_promotion_input_sha256,
        ),
        (
            "SELECT input_sha256 FROM evolution_promotion_package_inputs "
            "WHERE input_id = ?",
            item.prior_promotion_input_id,
            item.prior_promotion_input_sha256,
        ),
        (
            "SELECT receipt_sha256 FROM evolution_final_evaluation_receipts "
            "WHERE receipt_id = ?",
            item.final_evaluation_id,
            item.final_evaluation_sha256,
        ),
    )
    for statement, identity, expected in checks:
        row = await (await db.execute(statement, (identity,))).fetchone()
        if row is None or not hmac.compare_digest(str(row[0]), expected):
            raise EvolutionProposalBeforeAfterEvidenceError(
                "proposal_before_after_dependency_mismatch",
                "Proposal Before/After Evidence 的 durable dependency 缺失或不一致。",
            )


def _restore(raw: object) -> EvolutionProposalBeforeAfterEvidence:
    encoded = str(raw)
    if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise EvolutionProposalBeforeAfterEvidenceError(
            "proposal_before_after_artifact_oversized",
            "Proposal Before/After Evidence 超过 512 KiB。",
        )
    try:
        return EvolutionProposalBeforeAfterEvidence.model_validate_json(encoded)
    except (TypeError, ValueError) as exc:
        raise EvolutionProposalBeforeAfterEvidenceError(
            "proposal_before_after_store_corrupt",
            "Proposal Before/After Evidence 损坏或无法验证。",
        ) from exc


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区。")
    return parsed


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "EVOLUTION_PROPOSAL_BEFORE_AFTER_EVIDENCE_POLICY",
    "EvolutionProposalBeforeAfterCohort",
    "EvolutionProposalBeforeAfterEvidence",
    "EvolutionProposalBeforeAfterEvidenceBuilder",
    "EvolutionProposalBeforeAfterEvidenceError",
    "EvolutionProposalBeforeAfterEvidenceService",
    "EvolutionProposalBeforeAfterEvidenceStore",
    "EvolutionProposalBeforeAfterEvidenceView",
    "EvolutionProposalBeforeAfterLane",
    "render_proposal_before_after_evidence",
]
