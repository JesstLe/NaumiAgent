"""Deterministic EVO-04 mechanical gates over immutable Decision Input authority."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.decision_inputs import (
    EvolutionDecisionInput,
    EvolutionDecisionInputError,
    EvolutionDecisionInputStore,
)
from naumi_agent.evolution.failure_attribution import (
    FailureAttributionAction,
    FailureAttributionCategory,
)
from naumi_agent.evolution.mutation_generation import (
    EvolutionMutationGenerationError,
    EvolutionMutationGenerationTrace,
    EvolutionMutationGenerationTraceStore,
)

MECHANICAL_GATE_POLICY = "evolution-mechanical-gate-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 12 * 1_024 * 1_024


class MechanicalGateRule(StrEnum):
    AUTHORITY_INTEGRITY = "authority_integrity"
    GENERATION_TRACE_BOUND = "generation_trace_bound"
    SCOPE_EXACT = "scope_exact"
    GUARDRAIL_CHAIN_COMPLETE = "guardrail_chain_complete"
    REQUIRED_METRICS_EXACT = "required_metrics_exact"
    CHANGED_FILES_WITHIN_BUDGET = "changed_files_within_budget"
    CHANGED_LINES_WITHIN_BUDGET = "changed_lines_within_budget"
    TOOL_CALLS_WITHIN_BUDGET = "tool_calls_within_budget"
    DURATION_WITHIN_BUDGET = "duration_within_budget"
    ATTEMPTS_WITHIN_BUDGET = "attempts_within_budget"
    EVALUATION_COVERAGE_COMPLETE = "evaluation_coverage_complete"
    NO_CANDIDATE_FAULT = "no_candidate_fault"
    NO_RERUN_REQUIRED = "no_rerun_required"
    ALL_LANES_REFLECTION_ELIGIBLE = "all_lanes_reflection_eligible"
    FAILURE_CATEGORIES_CLEAR = "failure_categories_clear"
    FAILURE_ACTIONS_CONTINUE = "failure_actions_continue"


class MechanicalGateRequiredAction(StrEnum):
    CONTINUE_TO_INDEPENDENT_REVIEW = "continue_to_independent_review"
    REVISE_CANDIDATE = "revise_candidate"
    RERUN_EVALUATION = "rerun_evaluation"
    REBUILD_ENVIRONMENT = "rebuild_environment"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionMechanicalGateCheck(_StrictModel):
    order: int = Field(ge=1, le=32)
    rule: MechanicalGateRule
    passed: bool
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _refs_are_stable(self) -> Self:
        if self.evidence_refs != tuple(dict.fromkeys(self.evidence_refs)):
            raise ValueError("Mechanical Gate evidence refs 不得重复。")
        if any(
            not value
            or len(value) > 256
            or any(char in value for char in ("\x00", "\r", "\n"))
            for value in self.evidence_refs
        ):
            raise ValueError("Mechanical Gate evidence ref 无效。")
        return self


class EvolutionMechanicalGate(_StrictModel):
    """A mechanical pass/veto authority, never a Candidate acceptance decision."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-mechanical-gate-v1"] = MECHANICAL_GATE_POLICY
    gate_id: str = Field(pattern=r"^evgate_[0-9a-f]{24}$")
    gate_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    decision_input_id: str = Field(pattern=r"^evdin_[0-9a-f]{24}$")
    decision_input_sha256: str = Field(pattern=_SHA256_RE)
    mutation_trace_id: str = Field(pattern=r"^evmgt_[0-9a-f]{24}$")
    mutation_trace_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    checks: tuple[EvolutionMechanicalGateCheck, ...] = Field(
        min_length=16,
        max_length=16,
    )
    veto_codes: tuple[MechanicalGateRule, ...] = Field(max_length=16)
    required_actions: tuple[MechanicalGateRequiredAction, ...] = Field(
        min_length=1,
        max_length=3,
    )
    changed_files: int = Field(ge=1, le=16)
    changed_lines: int = Field(ge=0, le=134_217_728)
    observed_tool_calls: int = Field(ge=1, le=200)
    observed_duration_ms: float = Field(ge=0)
    outcome: Literal["pass", "veto"]
    mechanical_gate_decided: Literal[True] = True
    mechanical_gate_passed: bool
    mechanical_veto_applied: bool
    independent_review_ready: bool
    llm_override_allowed: Literal[False] = False
    candidate_acceptance_decided: Literal[False] = False
    promotion_ready: Literal[False] = False
    decision_input: EvolutionDecisionInput
    mutation_trace: EvolutionMutationGenerationTrace
    created_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _gate_is_exact_and_tamper_evident(self) -> Self:
        decision = self.decision_input
        trace = self.mutation_trace
        _require_trace_binding(decision, trace)
        expected_checks = _evaluate_checks(decision, trace)
        expected_veto = tuple(item.rule for item in expected_checks if not item.passed)
        expected_outcome: Literal["pass", "veto"] = (
            "pass" if not expected_veto else "veto"
        )
        expected_actions = _required_actions(decision, expected_veto)
        changed_lines = (
            decision.mutation.total_added_lines
            + decision.mutation.total_deleted_lines
        )
        duration_ms = _observed_duration_ms(decision, trace)
        if not (
            self.workspace_root == decision.workspace_root
            and self.decision_input_id == decision.decision_input_id
            and self.decision_input_sha256 == decision.decision_input_sha256
            and self.mutation_trace_id == trace.trace_id
            and self.mutation_trace_sha256 == trace.trace_sha256
            and self.candidate_id == decision.candidate_id
            and self.candidate_revision == decision.candidate_revision
            and self.checks == expected_checks
            and self.veto_codes == expected_veto
            and self.required_actions == expected_actions
            and self.changed_files == len(decision.mutation.files)
            and self.changed_lines == changed_lines
            and self.observed_tool_calls == trace.total_tool_calls
            and math.isclose(
                self.observed_duration_ms,
                duration_ms,
                rel_tol=0,
                abs_tol=1e-9,
            )
            and self.outcome == expected_outcome
            and self.mechanical_gate_passed == (expected_outcome == "pass")
            and self.mechanical_veto_applied == (expected_outcome == "veto")
            and self.independent_review_ready == (expected_outcome == "pass")
            and self.created_at == decision.created_at
        ):
            raise ValueError("Mechanical Gate projection 或确定性规则不一致。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"gate_id", "gate_sha256"})
        )
        if not hmac.compare_digest(self.gate_sha256, digest):
            raise ValueError("Mechanical Gate 摘要不一致。")
        if self.gate_id != f"evgate_{digest[:24]}":
            raise ValueError("Mechanical Gate identity 不一致。")
        return self


class EvolutionMechanicalGateError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionMechanicalGateBuilder:
    def build(
        self,
        *,
        decision_input: EvolutionDecisionInput,
        mutation_trace: EvolutionMutationGenerationTrace,
    ) -> EvolutionMechanicalGate:
        try:
            decision = EvolutionDecisionInput.model_validate(
                decision_input.model_dump(mode="json")
            )
            trace = EvolutionMutationGenerationTrace.model_validate(
                mutation_trace.model_dump(mode="json")
            )
            _require_trace_binding(decision, trace)
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionMechanicalGateError(
                "mechanical_gate_authority_mismatch",
                "Decision Input 与 Mutation Trace authority 不一致。",
            ) from exc
        checks = _evaluate_checks(decision, trace)
        veto_codes = tuple(item.rule for item in checks if not item.passed)
        outcome: Literal["pass", "veto"] = "pass" if not veto_codes else "veto"
        payload = {
            "schema_version": 1,
            "policy_version": MECHANICAL_GATE_POLICY,
            "workspace_root": decision.workspace_root,
            "decision_input_id": decision.decision_input_id,
            "decision_input_sha256": decision.decision_input_sha256,
            "mutation_trace_id": trace.trace_id,
            "mutation_trace_sha256": trace.trace_sha256,
            "candidate_id": decision.candidate_id,
            "candidate_revision": decision.candidate_revision,
            "checks": [item.model_dump(mode="json") for item in checks],
            "veto_codes": [item.value for item in veto_codes],
            "required_actions": [
                item.value for item in _required_actions(decision, veto_codes)
            ],
            "changed_files": len(decision.mutation.files),
            "changed_lines": (
                decision.mutation.total_added_lines
                + decision.mutation.total_deleted_lines
            ),
            "observed_tool_calls": trace.total_tool_calls,
            "observed_duration_ms": _observed_duration_ms(decision, trace),
            "outcome": outcome,
            "mechanical_gate_decided": True,
            "mechanical_gate_passed": outcome == "pass",
            "mechanical_veto_applied": outcome == "veto",
            "independent_review_ready": outcome == "pass",
            "llm_override_allowed": False,
            "candidate_acceptance_decided": False,
            "promotion_ready": False,
            "decision_input": decision.model_dump(mode="json"),
            "mutation_trace": trace.model_dump(mode="json"),
            "created_at": decision.created_at,
        }
        digest = _sha256_payload(payload)
        try:
            return EvolutionMechanicalGate.model_validate({
                **payload,
                "gate_id": f"evgate_{digest[:24]}",
                "gate_sha256": digest,
            })
        except ValueError as exc:
            raise EvolutionMechanicalGateError(
                "mechanical_gate_invalid",
                "Mechanical Gate 无法形成确定性 authority。",
            ) from exc


class EvolutionMechanicalGateStore:
    """Immutable one-gate-per-Decision-Input storage."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(self, gate: EvolutionMechanicalGate) -> EvolutionMechanicalGate:
        try:
            item = EvolutionMechanicalGate.model_validate(gate.model_dump(mode="json"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionMechanicalGateError(
                "mechanical_gate_invalid", "Mechanical Gate 无效或已被篡改。"
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionMechanicalGateError(
                "mechanical_gate_oversized", "Mechanical Gate 超过 12 MiB 上限。"
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_mechanical_gates "
                        "WHERE decision_input_id = ?",
                        (item.decision_input_id,),
                    )
                ).fetchone()
                if row is not None:
                    restored = _from_row(row)
                    if restored != item:
                        await db.rollback()
                        raise EvolutionMechanicalGateError(
                            "mechanical_gate_conflict",
                            "同一 Decision Input 不可覆盖为不同 Mechanical Gate。",
                        )
                    await db.rollback()
                    return restored
                await db.execute(
                    "INSERT INTO evolution_mechanical_gates "
                    "(gate_id, gate_sha256, workspace_root, decision_input_id, "
                    "outcome, gate_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.gate_id,
                        item.gate_sha256,
                        item.workspace_root,
                        item.decision_input_id,
                        item.outcome,
                        encoded,
                        item.created_at,
                    ),
                )
                await db.commit()
        except EvolutionMechanicalGateError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionMechanicalGateError(
                "mechanical_gate_store_error", "Mechanical Gate 无法持久化。"
            ) from exc
        restored = await self.get(item.gate_id)
        assert restored is not None
        return restored

    async def get(self, gate_id: str) -> EvolutionMechanicalGate | None:
        if not isinstance(gate_id, str) or re.fullmatch(
            r"evgate_[0-9a-f]{24}", gate_id
        ) is None:
            raise ValueError("gate_id 格式无效。")
        return await self._read("gate_id", gate_id)

    async def get_by_decision_input(
        self,
        decision_input_id: str,
    ) -> EvolutionMechanicalGate | None:
        if not isinstance(decision_input_id, str) or re.fullmatch(
            r"evdin_[0-9a-f]{24}", decision_input_id
        ) is None:
            raise ValueError("decision_input_id 格式无效。")
        return await self._read("decision_input_id", decision_input_id)

    async def _read(self, column: str, value: str) -> EvolutionMechanicalGate | None:
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        f"SELECT * FROM evolution_mechanical_gates WHERE {column} = ?",
                        (value,),
                    )
                ).fetchone()
                return _from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionMechanicalGateError(
                "mechanical_gate_store_corrupt",
                "Mechanical Gate 损坏或无法读取。",
            ) from exc


class EvolutionMechanicalGateExecutor:
    def __init__(
        self,
        *,
        decision_store: EvolutionDecisionInputStore,
        trace_store: EvolutionMutationGenerationTraceStore,
        gate_store: EvolutionMechanicalGateStore,
        builder: EvolutionMechanicalGateBuilder | None = None,
    ) -> None:
        if not isinstance(decision_store, EvolutionDecisionInputStore):
            raise TypeError("Mechanical Gate executor 需要 Decision Input Store。")
        if not isinstance(trace_store, EvolutionMutationGenerationTraceStore):
            raise TypeError("Mechanical Gate executor 需要 Mutation Trace Store。")
        if not isinstance(gate_store, EvolutionMechanicalGateStore):
            raise TypeError("Mechanical Gate executor 需要 Mechanical Gate Store。")
        if builder is not None and not isinstance(builder, EvolutionMechanicalGateBuilder):
            raise TypeError("Mechanical Gate executor 需要 Mechanical Gate Builder。")
        self._decision_store = decision_store
        self._trace_store = trace_store
        self._gate_store = gate_store
        self._builder = builder or EvolutionMechanicalGateBuilder()

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        decision_input_id: str,
    ) -> EvolutionMechanicalGate:
        try:
            workspace = Path(workspace_root).expanduser().resolve(strict=True)
        except OSError as exc:
            raise EvolutionMechanicalGateError(
                "mechanical_gate_workspace_missing", "Mechanical Gate 工作区不存在。"
            ) from exc
        if not workspace.is_dir():
            raise EvolutionMechanicalGateError(
                "mechanical_gate_workspace_missing", "Mechanical Gate 工作区不存在。"
            )
        try:
            decision = await self._decision_store.get(decision_input_id.strip())
        except (EvolutionDecisionInputError, TypeError, ValueError) as exc:
            raise EvolutionMechanicalGateError(
                "mechanical_gate_input_read_failed", "无法读取 Decision Input。"
            ) from exc
        if decision is None:
            raise EvolutionMechanicalGateError(
                "mechanical_gate_input_missing", "Decision Input 不存在。"
            )
        if decision.workspace_root != str(workspace):
            raise EvolutionMechanicalGateError(
                "mechanical_gate_workspace_mismatch",
                "Decision Input 不属于当前工作区。",
            )
        trace_id = decision.mutation.mutation_generation_trace_id
        if trace_id is None:
            raise EvolutionMechanicalGateError(
                "mechanical_gate_trace_missing",
                "Decision Input 的 Mutation Receipt 缺少 Generation Trace 引用。",
            )
        try:
            trace = await asyncio.to_thread(self._trace_store.get, trace_id)
        except (EvolutionMutationGenerationError, OSError, TypeError, ValueError) as exc:
            raise EvolutionMechanicalGateError(
                "mechanical_gate_trace_read_failed",
                "无法读取 Mutation Generation Trace。",
            ) from exc
        if trace is None:
            raise EvolutionMechanicalGateError(
                "mechanical_gate_trace_missing", "Mutation Generation Trace 不存在。"
            )
        gate = self._builder.build(decision_input=decision, mutation_trace=trace)
        return await self._gate_store.record(gate)


def render_mechanical_gate(gate: EvolutionMechanicalGate) -> str:
    item = EvolutionMechanicalGate.model_validate(gate.model_dump(mode="json"))
    passed = sum(check.passed for check in item.checks)
    outcome = "通过" if item.outcome == "pass" else "机械否决"
    lines = [
        f"# Evolution Mechanical Gate `{item.gate_id}`",
        "",
        f"**{outcome}；该结果不可被 LLM 覆盖，但仍不是 Candidate 接受或发布决定。**",
        "",
        f"- Decision Input：`{item.decision_input_id}`",
        f"- Candidate：`{item.candidate_id}` · revision {item.candidate_revision}",
        f"- 规则：{passed}/{len(item.checks)} 通过",
        f"- 文件/变更行：{item.changed_files} / {item.changed_lines}",
        f"- Mutation tool calls：{item.observed_tool_calls}",
        f"- 已观察总耗时：{item.observed_duration_ms:.3f} ms",
    ]
    if item.veto_codes:
        lines.extend((
            "- Veto：" + ", ".join(f"`{code.value}`" for code in item.veto_codes),
            "- Required actions："
            + ", ".join(f"`{action.value}`" for action in item.required_actions),
        ))
    else:
        lines.append("- 下一状态：`independent_review_ready`（尚未接受 Candidate）")
    lines.extend((
        f"- Gate SHA-256：`{item.gate_sha256}`",
        "",
        (
            "下一步：EVO-04.3 independent reviewer 只能读取本 gate；"
            "veto 时 reviewer 无权改写为通过。"
        ),
    ))
    return "\n".join(lines)


def _require_trace_binding(
    decision: EvolutionDecisionInput,
    trace: EvolutionMutationGenerationTrace,
) -> None:
    mutation = decision.mutation
    receipt_paths = tuple(item.path for item in mutation.files)
    trace_paths = tuple(item.path for item in trace.final_files)
    receipt_after = tuple(item.after_sha256 for item in mutation.files)
    trace_after = tuple(item.after_sha256 for item in trace.final_files)
    if not (
        mutation.mutation_generation_trace_id == trace.trace_id
        and mutation.mutation_generation_trace_sha256 == trace.trace_sha256
        and mutation.contract_id == trace.contract_id
        and mutation.contract_manifest_sha256 == trace.contract_manifest_sha256
        and mutation.lease_id == trace.lease_id
        and mutation.source_snapshot_id == trace.source_snapshot_id
        and mutation.source_snapshot_sha256 == trace.source_snapshot_sha256
        and mutation.mutation_plan_id == trace.mutation_plan_id
        and mutation.mutation_plan_sha256 == trace.mutation_plan_sha256
        and mutation.attempt == trace.attempt
        and mutation.max_attempts == trace.max_attempts
        and receipt_paths == trace_paths
        and receipt_after == trace_after
    ):
        raise ValueError("Mutation Receipt 与 Generation Trace 引用链不一致。")


def _evaluate_checks(
    decision: EvolutionDecisionInput,
    trace: EvolutionMutationGenerationTrace,
) -> tuple[EvolutionMechanicalGateCheck, ...]:
    mutation = decision.mutation
    final = decision.final_evaluation
    constraints = decision.constraints
    file_paths = tuple(item.path for item in mutation.files)
    trace_paths = tuple(item.path for item in trace.final_files)
    phases = tuple(item.phase for item in mutation.tool_evidence)
    expected_phases = (
        "mutation_generation",
        "static_guard",
        "patch_write",
        "postflight_guard",
    ) if mutation.schema_version == 2 else (
        "static_guard",
        "patch_write",
        "postflight_guard",
    )
    changed_lines = mutation.total_added_lines + mutation.total_deleted_lines
    duration_ms = _observed_duration_ms(decision, trace)
    expected_comparisons = 1 + len(final.required_platforms)
    facts: tuple[tuple[MechanicalGateRule, bool, tuple[str, ...]], ...] = (
        (
            MechanicalGateRule.AUTHORITY_INTEGRITY,
            True,
            (decision.decision_input_id, decision.final_evaluation_receipt_id),
        ),
        (
            MechanicalGateRule.GENERATION_TRACE_BOUND,
            mutation.mutation_generation_trace_id == trace.trace_id,
            (mutation.mutation_receipt_id, trace.trace_id),
        ),
        (
            MechanicalGateRule.SCOPE_EXACT,
            file_paths == constraints.allowed_files == trace_paths,
            (decision.experiment_contract_id, mutation.mutation_receipt_id, trace.trace_id),
        ),
        (
            MechanicalGateRule.GUARDRAIL_CHAIN_COMPLETE,
            phases == expected_phases,
            tuple(item.artifact_id for item in mutation.tool_evidence),
        ),
        (
            MechanicalGateRule.REQUIRED_METRICS_EXACT,
            mutation.required_metrics == constraints.required_metrics,
            (mutation.mutation_receipt_id, decision.experiment_contract_id),
        ),
        (
            MechanicalGateRule.CHANGED_FILES_WITHIN_BUDGET,
            len(file_paths) <= constraints.max_changed_files,
            (mutation.mutation_receipt_id, decision.experiment_contract_id),
        ),
        (
            MechanicalGateRule.CHANGED_LINES_WITHIN_BUDGET,
            changed_lines <= constraints.max_changed_lines,
            (mutation.mutation_receipt_id, decision.experiment_contract_id),
        ),
        (
            MechanicalGateRule.TOOL_CALLS_WITHIN_BUDGET,
            trace.total_tool_calls <= trace.max_tool_calls <= constraints.max_tool_calls,
            (trace.trace_id, decision.experiment_contract_id),
        ),
        (
            MechanicalGateRule.DURATION_WITHIN_BUDGET,
            duration_ms <= constraints.max_duration_seconds * 1_000,
            (trace.trace_id, decision.final_evaluation_receipt_id, decision.experiment_contract_id),
        ),
        (
            MechanicalGateRule.ATTEMPTS_WITHIN_BUDGET,
            trace.attempt <= trace.max_attempts <= constraints.max_attempts,
            (trace.trace_id, decision.experiment_contract_id),
        ),
        (
            MechanicalGateRule.EVALUATION_COVERAGE_COMPLETE,
            final.lane_count == expected_comparisons
            and len(final.comparison_ids) == expected_comparisons
            and final.candidate_evaluation_complete
            and not final.aggregation_required,
            (decision.final_evaluation_receipt_id,),
        ),
        (
            MechanicalGateRule.NO_CANDIDATE_FAULT,
            not final.any_candidate_fault,
            (decision.final_evaluation_receipt_id,),
        ),
        (
            MechanicalGateRule.NO_RERUN_REQUIRED,
            not final.any_requires_rerun,
            (decision.final_evaluation_receipt_id,),
        ),
        (
            MechanicalGateRule.ALL_LANES_REFLECTION_ELIGIBLE,
            final.all_reflection_eligible,
            (decision.final_evaluation_receipt_id,),
        ),
        (
            MechanicalGateRule.FAILURE_CATEGORIES_CLEAR,
            final.failure_categories == (FailureAttributionCategory.NONE,),
            (decision.final_evaluation_receipt_id,),
        ),
        (
            MechanicalGateRule.FAILURE_ACTIONS_CONTINUE,
            final.failure_actions == (
                FailureAttributionAction.CONTINUE_TO_REFLECTION,
            ),
            (decision.final_evaluation_receipt_id,),
        ),
    )
    return tuple(
        EvolutionMechanicalGateCheck(
            order=index,
            rule=rule,
            passed=passed,
            evidence_refs=refs,
        )
        for index, (rule, passed, refs) in enumerate(facts, start=1)
    )


def _required_actions(
    decision: EvolutionDecisionInput,
    veto_codes: tuple[MechanicalGateRule, ...],
) -> tuple[MechanicalGateRequiredAction, ...]:
    if not veto_codes:
        return (MechanicalGateRequiredAction.CONTINUE_TO_INDEPENDENT_REVIEW,)
    action_map = {
        FailureAttributionAction.REVISE_CANDIDATE: (
            MechanicalGateRequiredAction.REVISE_CANDIDATE
        ),
        FailureAttributionAction.RERUN_EVALUATION: (
            MechanicalGateRequiredAction.RERUN_EVALUATION
        ),
        FailureAttributionAction.REBUILD_ENVIRONMENT: (
            MechanicalGateRequiredAction.REBUILD_ENVIRONMENT
        ),
    }
    selected = {
        action_map[action]
        for action in decision.final_evaluation.failure_actions
        if action in action_map
    }
    budget_rules = {
        MechanicalGateRule.CHANGED_FILES_WITHIN_BUDGET,
        MechanicalGateRule.CHANGED_LINES_WITHIN_BUDGET,
        MechanicalGateRule.TOOL_CALLS_WITHIN_BUDGET,
        MechanicalGateRule.DURATION_WITHIN_BUDGET,
        MechanicalGateRule.ATTEMPTS_WITHIN_BUDGET,
    }
    if any(code in budget_rules for code in veto_codes):
        selected.add(MechanicalGateRequiredAction.REVISE_CANDIDATE)
    if not selected:
        selected.add(MechanicalGateRequiredAction.REVISE_CANDIDATE)
    order = tuple(MechanicalGateRequiredAction)
    return tuple(item for item in order if item in selected)


def _observed_duration_ms(
    decision: EvolutionDecisionInput,
    trace: EvolutionMutationGenerationTrace,
) -> float:
    started = datetime.fromisoformat(trace.created_at)
    completed = datetime.fromisoformat(trace.completed_at)
    generation_ms = (completed - started).total_seconds() * 1_000
    final = decision.final_evaluation
    return (
        generation_ms
        + final.baseline_resources.duration_ms
        + final.candidate_resources.duration_ms
    )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA busy_timeout = 10000")
    await db.execute(
        """CREATE TABLE IF NOT EXISTS evolution_mechanical_gates (
               gate_id TEXT PRIMARY KEY,
               gate_sha256 TEXT NOT NULL,
               workspace_root TEXT NOT NULL,
               decision_input_id TEXT NOT NULL UNIQUE,
               outcome TEXT NOT NULL CHECK(outcome IN ('pass', 'veto')),
               gate_json TEXT NOT NULL,
               created_at TEXT NOT NULL
           )"""
    )


def _from_row(row: aiosqlite.Row) -> EvolutionMechanicalGate:
    encoded = row["gate_json"]
    if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise ValueError("Mechanical Gate 持久化内容无效。")
    item = EvolutionMechanicalGate.model_validate_json(encoded)
    if not (
        row["gate_id"] == item.gate_id
        and row["gate_sha256"] == item.gate_sha256
        and row["workspace_root"] == item.workspace_root
        and row["decision_input_id"] == item.decision_input_id
        and row["outcome"] == item.outcome
        and row["created_at"] == item.created_at
    ):
        raise ValueError("Mechanical Gate 索引与内容不一致。")
    return item


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "EvolutionMechanicalGate",
    "EvolutionMechanicalGateBuilder",
    "EvolutionMechanicalGateError",
    "EvolutionMechanicalGateExecutor",
    "EvolutionMechanicalGateStore",
    "MechanicalGateRequiredAction",
    "MechanicalGateRule",
    "render_mechanical_gate",
]
