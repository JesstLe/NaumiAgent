"""Independent evidence replay and explicit governance for capability specifications."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.capability_proposal import EvolutionCapabilityProposal
from naumi_agent.evolution.capability_specification import (
    CapabilitySpecificationView,
    EvolutionCapabilitySpecificationService,
    validate_capability_specification_step,
)
from naumi_agent.harness.interaction import HarnessInteractionRecord
from naumi_agent.harness.store import HarnessStoreError
from naumi_agent.user_interaction import (
    UserInteractionOption,
    UserInteractionRequest,
    UserInteractionUnavailableError,
)

_POLICY_VERSION = "evolution-capability-governance-v1"
_SPECIFICATION_ID_RE = re.compile(r"^evcs_[0-9a-f]{24}$")
_INTERACTION_ID_RE = re.compile(r"^ask-evcgov-([0-9a-f]{24})-(\d{1,3})$")
_SECRET_RE = re.compile(
    r"(?:\b(?:api[_-]?key|password|secret|token|authorization|cookie)\b\s*[:=]\s*\S+)"
    r"|(?:\bbearer\s+\S+)|(?:\bsk-[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class CapabilityGovernanceCheck(_StrictModel):
    code: Literal[
        "specification_complete",
        "candidate_lineage",
        "interaction_cardinality",
        "interaction_integrity",
        "answer_replay",
        "authority_closed",
    ]
    passed: bool
    hard_block: Literal[True] = True
    detail: str = Field(min_length=1, max_length=500)


class CapabilitySpecificationAssessment(_StrictModel):
    schema_version: Literal[1] = 1
    assessment_id: str = Field(pattern=r"^evcsa_[0-9a-f]{24}$")
    policy_version: Literal["evolution-capability-governance-v1"] = _POLICY_VERSION
    specification_id: str = Field(pattern=r"^evcs_[0-9a-f]{24}$")
    specification_revision: Literal[5] = 5
    specification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checks: tuple[CapabilityGovernanceCheck, ...] = Field(min_length=6, max_length=6)
    eligible_for_decision: bool
    sandbox_design_eligible: Literal[False] = False
    registration_authorized: Literal[False] = False
    executable: Literal[False] = False

    @model_validator(mode="after")
    def _identity_and_checks_are_exact(self) -> CapabilitySpecificationAssessment:
        expected_codes = (
            "specification_complete",
            "candidate_lineage",
            "interaction_cardinality",
            "interaction_integrity",
            "answer_replay",
            "authority_closed",
        )
        if tuple(item.code for item in self.checks) != expected_codes:
            raise ValueError("Capability assessment checks 顺序或集合无效。")
        if self.eligible_for_decision != all(item.passed for item in self.checks):
            raise ValueError("eligible_for_decision 与机械 checks 不一致。")
        if self.assessment_id != _assessment_id(
            specification_id=self.specification_id,
            specification_sha256=self.specification_sha256,
            candidate_id=self.candidate_id,
            candidate_revision=self.candidate_revision,
            candidate_sha256=self.candidate_sha256,
            checks=self.checks,
        ):
            raise ValueError("assessment_id 与规格证据不一致。")
        return self

    def canonical_json(self) -> str:
        return _canonical(self.model_dump(mode="json"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


class CapabilityGovernanceDecision(_StrictModel):
    schema_version: Literal[1] = 1
    decision_id: str = Field(pattern=r"^evcgd_[0-9a-f]{24}$")
    policy_version: Literal["evolution-capability-governance-v1"] = _POLICY_VERSION
    assessment_id: str = Field(pattern=r"^evcsa_[0-9a-f]{24}$")
    assessment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    specification_id: str = Field(pattern=r"^evcs_[0-9a-f]{24}$")
    specification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    outcome: Literal["approved", "rejected"]
    reason: str = Field(min_length=1, max_length=1_000)
    source_interaction_id: str = Field(
        pattern=r"^ask-evcgov-[0-9a-f]{24}-\d{1,3}$"
    )
    source_interaction_sequence: int = Field(ge=2)
    source_interaction_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decided_by: Literal["user"] = "user"
    decided_at: str = Field(min_length=20, max_length=64)
    sandbox_design_eligible: bool
    registration_authorized: Literal[False] = False
    shadow_authorized: Literal[False] = False
    executable: Literal[False] = False

    @model_validator(mode="after")
    def _decision_identity_and_authority(self) -> CapabilityGovernanceDecision:
        _safe_reason(self.reason)
        if self.sandbox_design_eligible != (self.outcome == "approved"):
            raise ValueError("Sandbox design eligibility 与决策不一致。")
        expected = _decision_id(
            assessment_sha256=self.assessment_sha256,
            interaction_id=self.source_interaction_id,
            interaction_sha256=self.source_interaction_sha256,
            outcome=self.outcome,
            reason=self.reason,
        )
        if self.decision_id != expected:
            raise ValueError("decision_id 与 assessment/interaction 不一致。")
        return self

    def canonical_json(self) -> str:
        return _canonical(self.model_dump(mode="json"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


class CapabilityGovernanceView(_StrictModel):
    schema_version: Literal[1] = 1
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    specification_id: str = Field(pattern=r"^evcs_[0-9a-f]{24}$")
    assessment: CapabilitySpecificationAssessment
    decision: CapabilityGovernanceDecision | None
    state: Literal["blocked", "awaiting_decision", "approved", "rejected", "revoked"]
    decision_effective: bool
    pending_interaction_id: str = Field(max_length=160)
    sandbox_design_eligible: bool
    registration_authorized: Literal[False] = False
    shadow_authorized: Literal[False] = False
    executable: Literal[False] = False

    @model_validator(mode="after")
    def _state_matches_authority(self) -> CapabilityGovernanceView:
        if self.decision is None:
            expected = (
                "awaiting_decision"
                if self.assessment.eligible_for_decision
                else "blocked"
            )
            if self.state != expected or self.decision_effective:
                raise ValueError("无决策时 governance state 无效。")
        elif self.decision_effective:
            if self.state != self.decision.outcome:
                raise ValueError("有效决策与 governance state 不一致。")
        elif self.state != "revoked":
            raise ValueError("失效决策必须显示 revoked。")
        expected_design = bool(
            self.decision_effective
            and self.decision is not None
            and self.decision.outcome == "approved"
        )
        if self.sandbox_design_eligible != expected_design:
            raise ValueError("Sandbox design eligibility 与有效决策不一致。")
        return self


class CapabilityGovernanceError(RuntimeError):
    pass


class CapabilityGovernanceInteractionStore(Protocol):
    async def get_interaction(self, **kwargs: Any) -> HarnessInteractionRecord | None: ...
    async def list_interactions(self, **kwargs: Any) -> tuple[HarnessInteractionRecord, ...]: ...


class CapabilitySpecificationAssessor:
    """Replay all five durable answers without trusting the stored projection."""

    def __init__(self, interaction_store: CapabilityGovernanceInteractionStore) -> None:
        self.interaction_store = interaction_store

    async def assess(
        self,
        workspace_root: str | Path,
        *,
        proposal: EvolutionCapabilityProposal,
        specification_view: CapabilitySpecificationView,
    ) -> CapabilitySpecificationAssessment:
        specification = specification_view.specification
        if specification is None or specification.state != "complete":
            raise CapabilityGovernanceError(
                "Capability Specification 尚未完成，不能进入治理决策。"
            )
        specification_sha256 = specification.digest()
        complete = (
            specification.revision == 5
            and specification.pending_step is None
            and len(specification.completed_steps) == 5
        )
        lineage = (
            specification.specification_id == specification_view.specification_id
            and specification.candidate_id == proposal.source.candidate_id
            and specification.candidate_revision == proposal.source.candidate_revision
            and specification.candidate_sha256 == proposal.source.candidate_sha256
        )
        cardinality = len(specification.interaction_sources) == 5
        integrity = cardinality
        replay = cardinality
        for source in specification.interaction_sources:
            try:
                interaction = await self.interaction_store.get_interaction(
                    workspace_root=workspace_root,
                    interaction_id=source.interaction_id,
                )
            except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
                raise CapabilityGovernanceError(
                    "无法重读 Capability Specification interaction authority。"
                ) from exc
            if interaction is None:
                integrity = False
                replay = False
                continue
            source_integrity = (
                interaction.subject_kind == "tool"
                and interaction.subject_id == specification.specification_id
                and interaction.state == "answered"
                and interaction.answer_kind == "custom"
                and interaction.answered_by == "user"
                and interaction.sequence == source.interaction_sequence
                and interaction.digest() == source.interaction_sha256
                and interaction.answered_at == source.answered_at
            )
            integrity = integrity and source_integrity
            if not source_integrity:
                replay = False
                continue
            try:
                replayed = validate_capability_specification_step(
                    source.step,
                    interaction.custom_text,
                    proposal,
                )
            except (TypeError, ValueError):
                replay = False
                continue
            stored_value = getattr(specification, source.step)
            replay = replay and stored_value is not None and (
                replayed.model_dump(mode="json") == stored_value.model_dump(mode="json")
            )
        authority_closed = not any((
            specification.sandbox_eligible,
            specification.shadow_eligible,
            specification.executable,
            specification.registry_mutation_allowed,
            specification_view.sandbox_eligible,
            specification_view.shadow_eligible,
            specification_view.executable,
        ))
        checks = (
            _check("specification_complete", complete, "五个规格步骤已连续完成。"),
            _check("candidate_lineage", lineage, "规格绑定当前 Candidate revision/digest。"),
            _check("interaction_cardinality", cardinality, "每个步骤恰有一条持久答案来源。"),
            _check(
                "interaction_integrity",
                integrity,
                "Harness interaction 身份、摘要与人工来源一致。",
            ),
            _check("answer_replay", replay, "五条原始答案可重放为当前结构化规格。"),
            _check("authority_closed", authority_closed, "注册、Shadow 与执行 authority 仍关闭。"),
        )
        return CapabilitySpecificationAssessment(
            assessment_id=_assessment_id(
                specification_id=specification.specification_id,
                specification_sha256=specification_sha256,
                candidate_id=specification.candidate_id,
                candidate_revision=specification.candidate_revision,
                candidate_sha256=specification.candidate_sha256,
                checks=checks,
            ),
            specification_id=specification.specification_id,
            specification_sha256=specification_sha256,
            candidate_id=specification.candidate_id,
            candidate_revision=specification.candidate_revision,
            candidate_sha256=specification.candidate_sha256,
            checks=checks,
            eligible_for_decision=all(item.passed for item in checks),
        )


class EvolutionCapabilityGovernanceStore:
    """First-terminal-wins decision ledger for one exact specification."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def latest(
        self,
        workspace_root: str | Path,
        specification_id: str,
    ) -> CapabilityGovernanceDecision | None:
        workspace = _workspace(workspace_root)
        identifier = _specification_id(specification_id)
        if not self._db_path.is_file():
            return None
        await self._ensure_schema()
        try:
            async with _connection(self._db_path) as db:
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_capability_governance_decisions "
                        "WHERE workspace_root = ? AND specification_id = ?",
                        (workspace, identifier),
                    )
                ).fetchone()
                return None if row is None else _decision_from_row(row)
        except CapabilityGovernanceError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise CapabilityGovernanceError(
                "无法读取 Capability Governance Decision。"
            ) from exc

    async def record(
        self,
        workspace_root: str | Path,
        *,
        assessment: CapabilitySpecificationAssessment,
        interaction: HarnessInteractionRecord,
        outcome: Literal["approved", "rejected"],
        reason: str,
    ) -> CapabilityGovernanceDecision:
        workspace = _workspace(workspace_root)
        if not assessment.eligible_for_decision:
            raise CapabilityGovernanceError(
                "Capability Assessment 未通过，拒绝写入治理终态。"
            )
        decision = _build_decision(assessment, interaction, outcome, reason)
        await self._ensure_schema()
        try:
            async with self._write_lock, _connection(self._db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                existing = await (
                    await db.execute(
                        "SELECT * FROM evolution_capability_governance_decisions "
                        "WHERE workspace_root = ? AND specification_id = ?",
                        (workspace, assessment.specification_id),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _decision_from_row(existing)
                    await db.rollback()
                    if restored.source_interaction_id == interaction.interaction_id:
                        return restored
                    raise CapabilityGovernanceError(
                        f"规格已由 {restored.decision_id} 作出终态 {restored.outcome}，拒绝覆盖。"
                    )
                payload = decision.canonical_json()
                await db.execute(
                    """
                    INSERT INTO evolution_capability_governance_decisions (
                        workspace_root, specification_id, decision_id, assessment_id,
                        assessment_json, assessment_sha256, source_interaction_id,
                        outcome, payload_json, payload_sha256, decided_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace,
                        decision.specification_id,
                        decision.decision_id,
                        decision.assessment_id,
                        assessment.canonical_json(),
                        assessment.digest(),
                        decision.source_interaction_id,
                        decision.outcome,
                        payload,
                        decision.digest(),
                        decision.decided_at,
                    ),
                )
                await db.commit()
                return decision
        except CapabilityGovernanceError:
            raise
        except aiosqlite.IntegrityError as exc:
            raise CapabilityGovernanceError(
                "Capability Governance Decision 并发冲突，请重读后重试。"
            ) from exc
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise CapabilityGovernanceError(
                "无法保存 Capability Governance Decision。"
            ) from exc

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            try:
                self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                async with _connection(self._db_path) as db:
                    await db.executescript(_SCHEMA)
                    await db.commit()
                self._db_path.chmod(0o600)
            except (aiosqlite.Error, OSError) as exc:
                raise CapabilityGovernanceError(
                    "无法初始化 Capability Governance Store。"
                ) from exc
            self._schema_ready = True


class EvolutionCapabilityGovernanceService:
    """Reassess current evidence and collect one explicit human terminal decision."""

    def __init__(
        self,
        *,
        review_service: Any,
        specification_service: EvolutionCapabilitySpecificationService,
        assessor: CapabilitySpecificationAssessor,
        store: EvolutionCapabilityGovernanceStore,
        interaction_store: CapabilityGovernanceInteractionStore,
        request_user_input: Callable[[dict[str, Any]], Any],
    ) -> None:
        self.review_service = review_service
        self.specification_service = specification_service
        self.assessor = assessor
        self.store = store
        self.interaction_store = interaction_store
        self.request_user_input = request_user_input

    async def inspect(
        self,
        workspace_root: str | Path,
        candidate_id: str,
    ) -> CapabilityGovernanceView:
        proposal = await self._current_proposal(workspace_root, candidate_id)
        specification_view = await self.specification_service.inspect_capability_specification(
            workspace_root,
            proposal,
        )
        view = await self.inspect_capability_governance(
            workspace_root,
            proposal,
            specification_view,
        )
        if view is None:
            raise CapabilityGovernanceError(
                "Capability Specification 尚未完成，当前没有治理 Assessment。"
            )
        return view

    async def inspect_capability_governance(
        self,
        workspace_root: str | Path,
        proposal: EvolutionCapabilityProposal,
        specification_view: CapabilitySpecificationView,
    ) -> CapabilityGovernanceView | None:
        if specification_view.specification is None or specification_view.state != "complete":
            return None
        assessment = await self.assessor.assess(
            workspace_root,
            proposal=proposal,
            specification_view=specification_view,
        )
        decision = await self.store.latest(workspace_root, assessment.specification_id)
        pending = await self._pending_interaction(
            workspace_root,
            assessment.specification_id,
            decision,
        )
        return _view(assessment, decision, pending)

    async def decide(
        self,
        workspace_root: str | Path,
        *,
        candidate_id: str,
    ) -> CapabilityGovernanceView:
        proposal, specification_view, assessment = await self._current_assessment(
            workspace_root,
            candidate_id,
        )
        decision = await self.store.latest(workspace_root, assessment.specification_id)
        if decision is not None:
            return _view(assessment, decision, None)
        if not assessment.eligible_for_decision:
            raise CapabilityGovernanceError(
                "Capability Specification 独立机械 assessment 未通过，不能发起治理决策。"
            )
        recovered = await self._recover_answered(workspace_root, assessment)
        if recovered is not None:
            return _view(assessment, recovered, None)
        pending = await self._pending_interaction(
            workspace_root,
            assessment.specification_id,
            None,
        )
        if pending is not None:
            raise CapabilityGovernanceError(
                f"治理交互 {pending.interaction_id} 仍待回答；请在当前界面完成或接管。"
            )
        history = await self._history(workspace_root, assessment.specification_id)
        attempt = 1 + sum(
            _governance_interaction_matches(item.interaction_id, assessment.specification_id)
            for item in history
        )
        if attempt > 999:
            raise CapabilityGovernanceError("单份规格最多创建 999 次治理交互。")
        interaction_id = f"ask-evcgov-{assessment.specification_id[5:]}-{attempt}"
        payload = {
            **_decision_request().to_public_dict(),
            "_interaction_id": interaction_id,
            "_durable_subject_kind": "tool",
            "_durable_subject_id": assessment.specification_id,
        }
        try:
            await self.request_user_input(payload)
        except (HarnessStoreError, UserInteractionUnavailableError) as exc:
            raise CapabilityGovernanceError(
                "当前界面无法创建持久 Capability Governance 交互。"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise CapabilityGovernanceError(
                "Capability Governance 交互未通过运行时协议校验。"
            ) from exc
        interaction = await self._get_interaction(workspace_root, interaction_id)
        if interaction is None or interaction.state != "answered":
            raise CapabilityGovernanceError("治理答案尚未提交到 Harness authority。")
        try:
            parsed = _parse_decision(interaction)
        except ValueError as exc:
            raise CapabilityGovernanceError(
                "Capability Governance 拒绝原因无效；请重新发起治理交互。"
            ) from exc
        if parsed is None:
            return _view(assessment, None, None)
        refreshed_proposal, refreshed_specification, refreshed = (
            await self._current_assessment(workspace_root, candidate_id)
        )
        _ = refreshed_proposal, refreshed_specification
        if refreshed.assessment_id != assessment.assessment_id or (
            refreshed.digest() != assessment.digest()
        ):
            raise CapabilityGovernanceError(
                "回答期间规格或 authority 已变化；旧治理答案不会生效。"
            )
        outcome, reason = parsed
        if outcome == "approved" and not refreshed.eligible_for_decision:
            raise CapabilityGovernanceError("机械 assessment 未通过，不能批准。")
        decision = await self.store.record(
            workspace_root,
            assessment=refreshed,
            interaction=interaction,
            outcome=outcome,
            reason=reason,
        )
        return _view(refreshed, decision, None)

    async def _current_proposal(
        self,
        workspace_root: str | Path,
        candidate_id: str,
    ) -> EvolutionCapabilityProposal:
        snapshot = await self.review_service.detail_snapshot(
            workspace_root,
            str(candidate_id).strip(),
            include_capability_extensions=False,
        )
        selected = snapshot.selected
        proposal = selected.capability_proposal if selected is not None else None
        if proposal is None:
            raise CapabilityGovernanceError(
                "Candidate 当前没有 authority/cooldown/Portfolio 均有效的 Capability Proposal。"
            )
        return proposal

    async def _current_assessment(
        self,
        workspace_root: str | Path,
        candidate_id: str,
    ) -> tuple[
        EvolutionCapabilityProposal,
        CapabilitySpecificationView,
        CapabilitySpecificationAssessment,
    ]:
        proposal = await self._current_proposal(workspace_root, candidate_id)
        specification_view = await self.specification_service.inspect_capability_specification(
            workspace_root,
            proposal,
        )
        assessment = await self.assessor.assess(
            workspace_root,
            proposal=proposal,
            specification_view=specification_view,
        )
        return proposal, specification_view, assessment

    async def _get_interaction(
        self,
        workspace_root: str | Path,
        interaction_id: str,
    ) -> HarnessInteractionRecord | None:
        try:
            return await self.interaction_store.get_interaction(
                workspace_root=workspace_root,
                interaction_id=interaction_id,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise CapabilityGovernanceError(
                "无法重读 Capability Governance interaction authority。"
            ) from exc

    async def _history(
        self,
        workspace_root: str | Path,
        specification_id: str,
    ) -> tuple[HarnessInteractionRecord, ...]:
        try:
            return await self.interaction_store.list_interactions(
                workspace_root=workspace_root,
                subject_kind="tool",
                subject_ids=(specification_id,),
                limit=100,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise CapabilityGovernanceError(
                "无法读取 Capability Governance 交互历史。"
            ) from exc

    async def _pending_interaction(
        self,
        workspace_root: str | Path,
        specification_id: str,
        decision: CapabilityGovernanceDecision | None,
    ) -> HarnessInteractionRecord | None:
        if decision is not None:
            return None
        history = await self._history(workspace_root, specification_id)
        return next(
            (
                item
                for item in history
                if item.state == "pending"
                and _governance_interaction_matches(
                    item.interaction_id,
                    specification_id,
                )
            ),
            None,
        )

    async def _recover_answered(
        self,
        workspace_root: str | Path,
        assessment: CapabilitySpecificationAssessment,
    ) -> CapabilityGovernanceDecision | None:
        history = await self._history(workspace_root, assessment.specification_id)
        actionable: list[
            tuple[HarnessInteractionRecord, Literal["approved", "rejected"], str]
        ] = []
        for interaction in history:
            if interaction.state != "answered" or not _governance_interaction_matches(
                interaction.interaction_id,
                assessment.specification_id,
            ):
                continue
            try:
                parsed = _parse_decision(interaction)
            except (CapabilityGovernanceError, ValueError):
                continue
            if parsed is not None:
                actionable.append((interaction, *parsed))
        if not actionable:
            return None
        if len(actionable) != 1:
            raise CapabilityGovernanceError(
                "存在多个未对账治理答案，拒绝猜测终态。"
            )
        interaction, outcome, reason = actionable[0]
        if outcome == "approved" and not assessment.eligible_for_decision:
            raise CapabilityGovernanceError("机械 assessment 未通过，不能恢复批准。")
        return await self.store.record(
            workspace_root,
            assessment=assessment,
            interaction=interaction,
            outcome=outcome,
            reason=reason,
        )


def render_capability_governance(view: CapabilityGovernanceView) -> str:
    lines = [
        "# Capability Governance",
        "",
        f"- Specification：`{view.specification_id}`",
        f"- Assessment：`{view.assessment.assessment_id}`",
        f"- 状态：`{view.state}`",
        f"- 决策有效：{'是' if view.decision_effective else '否'}",
        f"- Sandbox 实现设计资格：{'是' if view.sandbox_design_eligible else '否'}",
        "- Registry 注册：否 · Shadow：否 · 可执行：否",
        "",
        "## 独立机械校验",
        "",
    ]
    lines.extend(
        f"- {'通过' if item.passed else '阻断'} · `{item.code}` · {item.detail}"
        for item in view.assessment.checks
    )
    if view.decision is not None:
        lines.extend([
            "",
            "## 人工决策",
            "",
            f"- ID：`{view.decision.decision_id}`",
            f"- 结果：`{view.decision.outcome}`",
            f"- 原因：{view.decision.reason}",
            f"- 来源交互：`{view.decision.source_interaction_id}`",
        ])
    elif view.pending_interaction_id:
        lines.append(f"- 待回答交互：`{view.pending_interaction_id}`")
    else:
        lines.extend([
            "",
            f"继续：`/evolution capability-govern {view.candidate_id}`",
        ])
    lines.extend([
        "",
        "> approved 只允许进入 Sandbox 实现设计；不授予注册、Shadow 或执行 authority。",
    ])
    return "\n".join(lines)


def _decision_request() -> UserInteractionRequest:
    return UserInteractionRequest(
        header="能力规格治理",
        question=(
            "独立校验已重放五条 Harness 答案。选择批准可进入 Sandbox 实现设计；"
            "选择稍后不产生决策；如需拒绝，请使用“其他”填写拒绝原因。"
        ),
        options=(
            UserInteractionOption(
                value="approve",
                label="批准规格",
                description="仅批准进入 Sandbox 实现设计，不注册也不执行 Tool。",
            ),
            UserInteractionOption(
                value="defer",
                label="稍后决定",
                description="不写入治理终态，可稍后重新审阅。",
            ),
        ),
        allow_custom=True,
        custom_label="填写拒绝原因",
        timeout_seconds=None,
        priority="high",
    )


def _parse_decision(
    interaction: HarnessInteractionRecord,
) -> tuple[Literal["approved", "rejected"], str] | None:
    if (
        interaction.state != "answered"
        or interaction.answered_by != "user"
        or interaction.subject_kind != "tool"
    ):
        raise CapabilityGovernanceError("治理决策必须来自已持久化的人工交互。")
    if interaction.answer_kind == "option":
        if interaction.answer_value == "defer":
            return None
        if interaction.answer_value == "approve":
            return (
                "approved",
                "用户明确批准该完整规格进入 Sandbox 实现设计阶段。",
            )
        raise CapabilityGovernanceError("治理交互 option 无效。")
    if interaction.answer_kind == "custom":
        return "rejected", _safe_reason(interaction.custom_text)
    raise CapabilityGovernanceError("治理交互答案类型无效。")


def _build_decision(
    assessment: CapabilitySpecificationAssessment,
    interaction: HarnessInteractionRecord,
    outcome: Literal["approved", "rejected"],
    reason: str,
) -> CapabilityGovernanceDecision:
    if not _governance_interaction_matches(
        interaction.interaction_id,
        assessment.specification_id,
    ) or interaction.subject_id != assessment.specification_id:
        raise CapabilityGovernanceError("治理 interaction 与 Specification 不一致。")
    if interaction.state != "answered" or interaction.answered_by != "user":
        raise CapabilityGovernanceError("治理 interaction 尚无有效人工终态。")
    normalized_reason = _safe_reason(reason)
    parsed = _parse_decision(interaction)
    if parsed is None or parsed != (outcome, normalized_reason):
        raise CapabilityGovernanceError(
            "治理 outcome/reason 与 Harness interaction 原始答案不一致。"
        )
    interaction_sha256 = interaction.digest()
    assessment_sha256 = assessment.digest()
    return CapabilityGovernanceDecision(
        decision_id=_decision_id(
            assessment_sha256=assessment_sha256,
            interaction_id=interaction.interaction_id,
            interaction_sha256=interaction_sha256,
            outcome=outcome,
            reason=normalized_reason,
        ),
        assessment_id=assessment.assessment_id,
        assessment_sha256=assessment_sha256,
        specification_id=assessment.specification_id,
        specification_sha256=assessment.specification_sha256,
        candidate_id=assessment.candidate_id,
        candidate_revision=assessment.candidate_revision,
        outcome=outcome,
        reason=normalized_reason,
        source_interaction_id=interaction.interaction_id,
        source_interaction_sequence=interaction.sequence,
        source_interaction_sha256=interaction_sha256,
        decided_at=interaction.answered_at,
        sandbox_design_eligible=outcome == "approved",
    )


def _view(
    assessment: CapabilitySpecificationAssessment,
    decision: CapabilityGovernanceDecision | None,
    pending: HarnessInteractionRecord | None,
) -> CapabilityGovernanceView:
    effective = bool(
        decision is not None
        and decision.assessment_id == assessment.assessment_id
        and decision.assessment_sha256 == assessment.digest()
        and decision.specification_sha256 == assessment.specification_sha256
    )
    if decision is None:
        state: Literal[
            "blocked",
            "awaiting_decision",
            "approved",
            "rejected",
            "revoked",
        ] = (
            "awaiting_decision" if assessment.eligible_for_decision else "blocked"
        )
    elif effective:
        state = decision.outcome
    else:
        state = "revoked"
    return CapabilityGovernanceView(
        candidate_id=assessment.candidate_id,
        specification_id=assessment.specification_id,
        assessment=assessment,
        decision=decision,
        state=state,
        decision_effective=effective,
        pending_interaction_id=pending.interaction_id if pending is not None else "",
        sandbox_design_eligible=bool(
            effective and decision is not None and decision.outcome == "approved"
        ),
    )


def _check(code: Any, passed: bool, success: str) -> CapabilityGovernanceCheck:
    return CapabilityGovernanceCheck(
        code=code,
        passed=passed,
        detail=success if passed else f"未通过：{success}",
    )


def _assessment_id(
    *,
    specification_id: str,
    specification_sha256: str,
    candidate_id: str,
    candidate_revision: int,
    candidate_sha256: str,
    checks: tuple[CapabilityGovernanceCheck, ...],
) -> str:
    payload = _canonical({
        "policy_version": _POLICY_VERSION,
        "specification_id": specification_id,
        "specification_sha256": specification_sha256,
        "candidate_id": candidate_id,
        "candidate_revision": candidate_revision,
        "candidate_sha256": candidate_sha256,
        "checks": [
            {"code": item.code, "passed": item.passed, "hard_block": item.hard_block}
            for item in checks
        ],
    })
    return f"evcsa_{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def _decision_id(
    *,
    assessment_sha256: str,
    interaction_id: str,
    interaction_sha256: str,
    outcome: str,
    reason: str,
) -> str:
    payload = _canonical({
        "policy_version": _POLICY_VERSION,
        "assessment_sha256": assessment_sha256,
        "interaction_id": interaction_id,
        "interaction_sha256": interaction_sha256,
        "outcome": outcome,
        "reason": reason,
    })
    return f"evcgd_{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def _governance_interaction_matches(value: str, specification_id: str) -> bool:
    match = _INTERACTION_ID_RE.fullmatch(str(value))
    return bool(match is not None and match.group(1) == specification_id[5:])


def _safe_reason(value: object) -> str:
    text = " ".join(str(value or "").strip().split())
    if (
        not text
        or len(text) > 1_000
        or "\x00" in text
        or _SECRET_RE.search(text)
        or "<redacted>" in text.casefold()
        or "[redacted" in text.casefold()
    ):
        raise ValueError("治理原因为空、过长、含控制字符或疑似 secret。")
    return text


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _workspace(value: str | Path) -> str:
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError("workspace_root 必须是现有目录。")
    return str(path)


def _specification_id(value: str) -> str:
    normalized = str(value).strip().lower()
    if _SPECIFICATION_ID_RE.fullmatch(normalized) is None:
        raise ValueError("Capability Specification ID 格式无效。")
    return normalized


def _decision_from_row(row: aiosqlite.Row) -> CapabilityGovernanceDecision:
    payload = str(row["payload_json"])
    digest = str(row["payload_sha256"])
    if hashlib.sha256(payload.encode()).hexdigest() != digest:
        raise CapabilityGovernanceError("Capability Governance Decision 摘要不一致。")
    try:
        decision = CapabilityGovernanceDecision.model_validate_json(payload)
        assessment = CapabilitySpecificationAssessment.model_validate_json(
            str(row["assessment_json"])
        )
    except ValueError as exc:
        raise CapabilityGovernanceError("Capability Governance Decision JSON 损坏。") from exc
    if (
        hashlib.sha256(str(row["assessment_json"]).encode()).hexdigest()
        != str(row["assessment_sha256"])
        or assessment.digest() != decision.assessment_sha256
        or assessment.assessment_id != decision.assessment_id
        or assessment.specification_id != decision.specification_id
        or assessment.specification_sha256 != decision.specification_sha256
    ):
        raise CapabilityGovernanceError(
            "Capability Governance Assessment 快照与 Decision 不一致。"
        )
    if (
        str(row["specification_id"]) != decision.specification_id
        or str(row["decision_id"]) != decision.decision_id
        or str(row["assessment_id"]) != decision.assessment_id
        or str(row["source_interaction_id"]) != decision.source_interaction_id
        or str(row["outcome"]) != decision.outcome
        or str(row["decided_at"]) != decision.decided_at
    ):
        raise CapabilityGovernanceError(
            "Capability Governance Decision 投影列与 payload 不一致。"
        )
    return decision


class _Connection:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def __aenter__(self) -> aiosqlite.Connection:
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await self.db.execute("PRAGMA busy_timeout = 5000")
        return self.db

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self.db is not None:
            await self.db.close()


def _connection(path: Path) -> _Connection:
    return _Connection(path)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS evolution_capability_governance_decisions (
    workspace_root TEXT NOT NULL,
    specification_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    assessment_id TEXT NOT NULL,
    assessment_json TEXT NOT NULL,
    assessment_sha256 TEXT NOT NULL,
    source_interaction_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('approved', 'rejected')),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (workspace_root, specification_id),
    UNIQUE (workspace_root, decision_id),
    UNIQUE (workspace_root, source_interaction_id)
);
"""


__all__ = [
    "CapabilityGovernanceDecision",
    "CapabilityGovernanceError",
    "CapabilityGovernanceView",
    "CapabilitySpecificationAssessment",
    "CapabilitySpecificationAssessor",
    "EvolutionCapabilityGovernanceService",
    "EvolutionCapabilityGovernanceStore",
    "render_capability_governance",
]
