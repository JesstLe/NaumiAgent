"""Mechanical Harness completion contracts, gate decisions, and receipts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.harness.checks import HarnessCheckResult, HarnessCheckStatus
from naumi_agent.harness.fingerprint import TreeFingerprint
from naumi_agent.harness.models import (
    HarnessCompletionContract,
    HarnessTaskKind,
)


class _CompletionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HarnessEvidenceRef(_CompletionModel):
    id: str = Field(max_length=256)
    kind: str = Field(max_length=128)
    summary: str = Field(max_length=4_000)
    criterion_ids: tuple[str, ...] = Field(default=(), max_length=128)

    @field_validator("id", "kind", "summary")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("evidence 字段不能为空")
        return normalized

    @field_validator("criterion_ids")
    @classmethod
    def _criterion_ids_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("criterion_ids 不能包含空值")
        if len(normalized) != len(set(normalized)):
            raise ValueError("criterion_ids 中存在重复值")
        return normalized


@dataclass
class HarnessRunState:
    """Ephemeral completion state for one Agent run."""

    contract: HarnessCompletionContract
    initial_tree: TreeFingerprint
    available_check_ids: tuple[str, ...]
    context: str
    correction_attempt: int = 0
    mutating_tool_used: bool = False
    finalized: bool = False
    receipt: HarnessCompletionReceipt | None = None


class CompletionGateInput(_CompletionModel):
    current_tree_fingerprint: str
    current_profile_digest: str | None = None
    changed_paths: tuple[str, ...] = ()
    checks: tuple[HarnessCheckResult, ...] = ()
    evidence: tuple[HarnessEvidenceRef, ...] = ()
    pending_todo_ids: tuple[str, ...] = ()
    known_failure_ids: tuple[str, ...] = ()
    disclosed_failure_ids: tuple[str, ...] = ()
    infrastructure_errors: tuple[str, ...] = ()
    mutating_tool_used: bool = False

    @field_validator("current_tree_fingerprint")
    @classmethod
    def _fingerprint_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("current_tree_fingerprint 不能为空")
        return normalized

    @model_validator(mode="after")
    def _evidence_and_checks_are_unambiguous(self) -> CompletionGateInput:
        evidence_ids = [evidence.id for evidence in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("存在重复 evidence id")
        check_keys = [(check.run_id, check.check_id) for check in self.checks]
        if len(check_keys) != len(set(check_keys)):
            raise ValueError("存在重复 check 结果")
        for field_name in (
            "changed_paths",
            "pending_todo_ids",
            "known_failure_ids",
            "disclosed_failure_ids",
            "infrastructure_errors",
        ):
            values = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} 中存在重复值")
        return self


class HarnessReceiptCheck(_CompletionModel):
    id: str
    status: str
    tree_fingerprint: str | None = None


class HarnessReceiptCriterion(_CompletionModel):
    id: str
    status: Literal["satisfied", "unsatisfied"]
    evidence_ids: tuple[str, ...] = ()


class HarnessCompletionReceipt(_CompletionModel):
    run_id: str
    status: Literal["completed_verified", "completed_unverified", "blocked"]
    task_kind: HarnessTaskKind
    changed_files: tuple[str, ...]
    checks: tuple[HarnessReceiptCheck, ...]
    criteria: tuple[HarnessReceiptCriterion, ...]
    warnings: tuple[str, ...]
    tree_fingerprint: str


class CompletionGateResult(_CompletionModel):
    status: Literal[
        "needs_correction",
        "completed_verified",
        "completed_unverified",
        "blocked",
    ]
    correction_instruction: str = ""
    receipt: HarnessCompletionReceipt | None = None


class _GateIssue(_CompletionModel):
    code: str
    message: str
    hard_block: bool = False


class CompletionGate:
    """Require current mechanical evidence before a task may claim verification."""

    def evaluate(
        self,
        contract: HarnessCompletionContract,
        facts: CompletionGateInput,
        *,
        correction_attempt: int,
    ) -> CompletionGateResult:
        if correction_attempt < 0:
            raise ValueError("correction_attempt 不能为负数。")
        effective_kind = contract.effective_task_kind(
            mutating_tool_used=(facts.mutating_tool_used or bool(facts.changed_paths))
        )
        issues = self._issues(contract, facts)
        if not issues:
            receipt = self._receipt(
                contract,
                facts,
                task_kind=effective_kind,
                status="completed_verified",
                issues=(),
            )
            return CompletionGateResult(
                status="completed_verified",
                receipt=receipt,
            )

        if correction_attempt < contract.correction_attempts:
            return CompletionGateResult(
                status="needs_correction",
                correction_instruction=_correction_instruction(issues),
            )

        final_status: Literal["completed_unverified", "blocked"]
        if any(issue.hard_block for issue in issues):
            final_status = "blocked"
        else:
            final_status = contract.unverified_status
        return CompletionGateResult(
            status=final_status,
            receipt=self._receipt(
                contract,
                facts,
                task_kind=effective_kind,
                status=final_status,
                issues=issues,
            ),
        )

    def _issues(
        self,
        contract: HarnessCompletionContract,
        facts: CompletionGateInput,
    ) -> tuple[_GateIssue, ...]:
        issues: list[_GateIssue] = []
        issues.extend(
            _GateIssue(
                code="infrastructure_error",
                message=message,
                hard_block=True,
            )
            for message in facts.infrastructure_errors
        )
        if contract.profile_digest is not None and (
            facts.current_profile_digest != contract.profile_digest
        ):
            issues.append(
                _GateIssue(
                    code="profile_digest_changed",
                    message="Profile digest 已变化，必须重建完成契约并重新运行检查。",
                )
            )
        if contract.task_kind is not HarnessTaskKind.CHANGE and (
            facts.mutating_tool_used or facts.changed_paths
        ):
            issues.append(
                _GateIssue(
                    code="task_kind_upgraded",
                    message=(
                        "运行中检测到持久化变更，必须升级为 change 契约并重新选择"
                        "必需检查。"
                    ),
                )
            )
        for path in facts.changed_paths:
            normalized = _safe_changed_path(path)
            if normalized is None:
                issues.append(
                    _GateIssue(
                        code="scope_invalid",
                        message=f"scope 路径不是工作区相对路径：{path}",
                        hard_block=True,
                    )
                )
                continue
            if contract.prohibited_scope and _matches_patterns(
                normalized,
                contract.prohibited_scope,
            ):
                issues.append(
                    _GateIssue(
                        code="scope_prohibited",
                        message=f"scope 禁止修改：{normalized}",
                        hard_block=True,
                    )
                )
            elif contract.allowed_scope and not _matches_patterns(
                normalized,
                contract.allowed_scope,
            ):
                issues.append(
                    _GateIssue(
                        code="scope_outside",
                        message=f"scope 超出允许范围：{normalized}",
                        hard_block=True,
                    )
                )

        if facts.pending_todo_ids:
            issues.append(
                _GateIssue(
                    code="todo_pending",
                    message="仍有未对账 Todo：" + "、".join(facts.pending_todo_ids),
                )
            )

        check_map = {
            (result.run_id, result.check_id): result for result in facts.checks
        }
        for check_id in contract.required_checks:
            result = check_map.get((contract.run_id, check_id))
            if result is None:
                issues.append(
                    _GateIssue(
                        code="check_missing",
                        message=f"缺少必需检查 {check_id}。",
                    )
                )
                continue
            if (
                contract.profile_digest is not None
                and result.profile_digest != contract.profile_digest
            ):
                issues.append(
                    _GateIssue(
                        code="check_profile_stale",
                        message=(
                            f"必需检查 {check_id} 来自旧 Profile digest，必须重新运行。"
                        ),
                    )
                )
            elif result.status is not HarnessCheckStatus.PASSED:
                issues.append(
                    _GateIssue(
                        code="check_not_passed",
                        message=(
                            f"必需检查 {check_id} 状态为 {result.status.value}，"
                            "不能作为通过证据。"
                        ),
                    )
                )
            elif result.tree_fingerprint != facts.current_tree_fingerprint:
                issues.append(
                    _GateIssue(
                        code="check_stale",
                        message=(
                            f"必需检查 {check_id} 不对应当前工作树，必须重新运行。"
                        ),
                    )
                )

        evidence_kinds = {evidence.kind for evidence in facts.evidence}
        for evidence_kind in contract.required_evidence:
            if evidence_kind not in evidence_kinds:
                issues.append(
                    _GateIssue(
                        code="evidence_missing",
                        message=f"缺少必需证据类型 {evidence_kind}。",
                    )
                )
        for criterion in contract.acceptance_criteria:
            if not any(
                criterion.id in evidence.criterion_ids for evidence in facts.evidence
            ):
                issues.append(
                    _GateIssue(
                        code="criterion_unsatisfied",
                        message=f"验收标准 {criterion.id} 缺少关联证据。",
                    )
                )

        disclosed = set(facts.disclosed_failure_ids)
        for failure_id in facts.known_failure_ids:
            if failure_id not in disclosed:
                issues.append(
                    _GateIssue(
                        code="failure_undisclosed",
                        message=f"失败 {failure_id} 尚未在完成说明中披露。",
                    )
                )
        return tuple(issues)

    def _receipt(
        self,
        contract: HarnessCompletionContract,
        facts: CompletionGateInput,
        *,
        task_kind: HarnessTaskKind,
        status: Literal["completed_verified", "completed_unverified", "blocked"],
        issues: tuple[_GateIssue, ...],
    ) -> HarnessCompletionReceipt:
        check_map = {
            (result.run_id, result.check_id): result for result in facts.checks
        }
        checks: list[HarnessReceiptCheck] = []
        for check_id in contract.required_checks:
            result = check_map.get((contract.run_id, check_id))
            if result is None:
                checks.append(HarnessReceiptCheck(id=check_id, status="missing"))
            elif (
                result.status is HarnessCheckStatus.PASSED
                and result.tree_fingerprint != facts.current_tree_fingerprint
            ):
                checks.append(
                    HarnessReceiptCheck(
                        id=check_id,
                        status="stale",
                        tree_fingerprint=result.tree_fingerprint,
                    )
                )
            else:
                checks.append(
                    HarnessReceiptCheck(
                        id=check_id,
                        status=result.status.value,
                        tree_fingerprint=result.tree_fingerprint,
                    )
                )
        criteria = tuple(
            HarnessReceiptCriterion(
                id=criterion.id,
                status=("satisfied" if evidence_ids else "unsatisfied"),
                evidence_ids=evidence_ids,
            )
            for criterion in contract.acceptance_criteria
            for evidence_ids in [
                tuple(
                    evidence.id
                    for evidence in facts.evidence
                    if criterion.id in evidence.criterion_ids
                )
            ]
        )
        return HarnessCompletionReceipt(
            run_id=contract.run_id,
            status=status,
            task_kind=task_kind,
            changed_files=tuple(sorted(set(facts.changed_paths))),
            checks=tuple(checks),
            criteria=criteria,
            warnings=tuple(issue.message for issue in issues),
            tree_fingerprint=facts.current_tree_fingerprint,
        )


def build_completion_contract(**values: object) -> HarnessCompletionContract:
    """Build one strict contract without silently accepting unknown fields."""
    return HarnessCompletionContract.model_validate(values)


def render_completion_contract_context(
    contract: HarnessCompletionContract,
    *,
    available_check_ids: tuple[str, ...],
) -> str:
    """Render an ephemeral, non-persistent contract instruction for the model."""
    available = "、".join(available_check_ids) or "无"
    required = "、".join(contract.required_checks) or "尚未选择"
    return (
        "<naumi_harness_completion_contract>\n"
        f"run_id: {contract.run_id}\n"
        f"task_kind: {contract.task_kind.value}\n"
        f"objective: {contract.objective}\n"
        f"available_checks: {available}\n"
        f"required_checks: {required}\n"
        "在声称完成前，必须满足 Harness 完成门禁。若门禁要求检查，"
        "只能调用 harness_run_check，并原样传入上述 run_id 与 check_id；"
        "不得把旧运行、旧工作树或旧 Profile 的结果当作当前证据。\n"
        "</naumi_harness_completion_contract>"
    )


def _safe_changed_path(path: str) -> str | None:
    normalized = path.strip().replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or (len(normalized) >= 2 and normalized[1] == ":")
    ):
        return None
    parts = PurePosixPath(normalized).parts
    if ".." in parts:
        return None
    return PurePosixPath(normalized).as_posix()


def _matches_patterns(path: str, patterns: tuple[str, ...]) -> bool:
    candidate = PurePosixPath(path)
    for pattern in patterns:
        if candidate.match(pattern):
            return True
        if "**/" in pattern and candidate.match(pattern.replace("**/", "")):
            return True
    return False


def _correction_instruction(issues: tuple[_GateIssue, ...]) -> str:
    lines = [
        "Harness 完成门禁发现缺失项。本轮只允许纠正一次，然后必须诚实返回未验证或阻塞："
    ]
    lines.extend(f"- {issue.message}" for issue in issues)
    return "\n".join(lines)
