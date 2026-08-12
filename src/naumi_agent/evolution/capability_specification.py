"""Durable, interaction-backed specification completion for Capability Proposals."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.capability_proposal import EvolutionCapabilityProposal
from naumi_agent.harness.interaction import HarnessInteractionRecord
from naumi_agent.harness.store import HarnessStoreError
from naumi_agent.user_interaction import (
    UserInteractionOption,
    UserInteractionRequest,
    UserInteractionUnavailableError,
)

CapabilitySpecificationStep = Literal[
    "interface",
    "permissions",
    "data",
    "verification",
    "operations",
]
_STEPS: tuple[CapabilitySpecificationStep, ...] = (
    "interface",
    "permissions",
    "data",
    "verification",
    "operations",
)
_GENERATOR_VERSION = "evolution-capability-specification-v1"
_SPEC_ID_RE = re.compile(r"^evcs_[0-9a-f]{24}$")
_PROPOSAL_ID_RE = re.compile(r"^evcp_[0-9a-f]{24}$")
_CANDIDATE_ID_RE = re.compile(r"^evc_[0-9a-f]{24}$")
_INTERACTION_ID_RE = re.compile(
    r"^ask-evcpspec-([0-9a-f]{24})-"
    r"(interface|permissions|data|verification|operations)-(\d{1,3})$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DOMAIN_RE = re.compile(
    r"^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_SECRET_RE = re.compile(
    r"(?:\b(?:api[_-]?key|password|secret|token|authorization|cookie)\b\s*[:=]\s*\S+)"
    r"|(?:\bbearer\s+\S+)|(?:\bsk-[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)
_ABSOLUTE_PATH_RE = re.compile(r"^(?:/|[A-Za-z]:[\\/])")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class CapabilityErrorContract(_StrictModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    message: str = Field(min_length=1, max_length=300)
    retryable: bool

    @field_validator("message")
    @classmethod
    def _safe_message(cls, value: str) -> str:
        return _safe_text(value, field="error.message", maximum=300)


class CapabilityInterfaceSpecification(_StrictModel):
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    parameters_schema: dict[str, Any]
    result_schema: dict[str, Any]
    errors: tuple[CapabilityErrorContract, ...] = Field(min_length=1, max_length=16)
    version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

    @model_validator(mode="after")
    def _schemas_are_bounded(self) -> CapabilityInterfaceSpecification:
        _validate_json_schema(self.parameters_schema, field="parameters_schema", object_root=True)
        _validate_json_schema(self.result_schema, field="result_schema", object_root=False)
        if len({item.code for item in self.errors}) != len(self.errors):
            raise ValueError("errors.code 不得重复。")
        return self


class CapabilityPermissionRequirement(_StrictModel):
    family: Literal[
        "workspace_read",
        "workspace_write",
        "process",
        "network",
        "browser",
        "secrets",
    ]
    scopes: tuple[str, ...] = Field(min_length=1, max_length=16)
    justification: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _scope_matches_family(self) -> CapabilityPermissionRequirement:
        if len(set(self.scopes)) != len(self.scopes):
            raise ValueError("permission scopes 不得重复。")
        for scope in self.scopes:
            _validate_permission_scope(self.family, scope)
        _safe_text(self.justification, field="permission.justification", maximum=500)
        return self


class CapabilityPermissionSpecification(_StrictModel):
    requirements: tuple[CapabilityPermissionRequirement, ...] = Field(max_length=6)

    @model_validator(mode="after")
    def _families_are_unique(self) -> CapabilityPermissionSpecification:
        if len({item.family for item in self.requirements}) != len(self.requirements):
            raise ValueError("每个 permission family 最多声明一次。")
        return self


class CapabilityDataSpecification(_StrictModel):
    input_classes: tuple[
        Literal[
            "public",
            "user_input",
            "workspace_content",
            "generated_content",
            "network_content",
            "credential_reference",
        ],
        ...,
    ] = Field(min_length=1, max_length=6)
    output_classes: tuple[
        Literal[
            "public",
            "user_input",
            "workspace_content",
            "generated_content",
            "network_content",
            "credential_reference",
        ],
        ...,
    ] = Field(min_length=1, max_length=6)
    retention: Literal["none", "turn", "session", "durable_reference_only"]
    sensitive_handling: Literal["deny", "reference_only"]

    @model_validator(mode="after")
    def _classes_are_unique_and_safe(self) -> CapabilityDataSpecification:
        if (
            len(set(self.input_classes)) != len(self.input_classes)
            or len(set(self.output_classes)) != len(self.output_classes)
        ):
            raise ValueError("data classes 不得重复。")
        if "credential_reference" in {*self.input_classes, *self.output_classes}:
            if self.sensitive_handling != "reference_only":
                raise ValueError("credential_reference 必须使用 reference_only。")
        elif self.sensitive_handling != "deny":
            raise ValueError("没有 credential_reference 时 sensitive_handling 必须为 deny。")
        return self


class CapabilityScenarioSpecification(_StrictModel):
    name: str = Field(min_length=1, max_length=80)
    fixture: str = Field(min_length=1, max_length=500)
    expected: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _scenario_text_is_safe(self) -> CapabilityScenarioSpecification:
        _safe_text(self.name, field="scenario.name", maximum=80)
        _safe_text(self.fixture, field="scenario.fixture", maximum=500, multiline=True)
        _safe_text(self.expected, field="scenario.expected", maximum=500, multiline=True)
        return self


class CapabilityVerificationSpecification(_StrictModel):
    scenarios: tuple[CapabilityScenarioSpecification, ...] = Field(
        min_length=1,
        max_length=8,
    )

    @model_validator(mode="after")
    def _scenario_names_are_unique(self) -> CapabilityVerificationSpecification:
        if len({item.name.casefold() for item in self.scenarios}) != len(self.scenarios):
            raise ValueError("verification scenario name 不得重复。")
        return self


class CapabilityOperationsSpecification(_StrictModel):
    owner: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
    latency_p95_ms: int = Field(ge=1, le=3_600_000)
    success_rate_percent: float = Field(ge=0.01, le=100)
    maintenance: str = Field(min_length=1, max_length=1_000)
    additional_retirement_criteria: tuple[str, ...] = Field(max_length=8)

    @model_validator(mode="after")
    def _operations_are_safe(self) -> CapabilityOperationsSpecification:
        _safe_text(self.maintenance, field="operations.maintenance", maximum=1_000, multiline=True)
        if len(set(self.additional_retirement_criteria)) != len(
            self.additional_retirement_criteria
        ):
            raise ValueError("additional retirement criteria 不得重复。")
        for value in self.additional_retirement_criteria:
            _safe_text(value, field="retirement criterion", maximum=300)
        return self


class CapabilitySpecificationInteractionSource(_StrictModel):
    step: CapabilitySpecificationStep
    interaction_id: str = Field(
        pattern=(
            r"^ask-evcpspec-[0-9a-f]{24}-"
            r"(interface|permissions|data|verification|operations)-\d{1,3}$"
        )
    )
    interaction_sequence: int = Field(ge=2)
    interaction_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    answered_at: str = Field(min_length=20, max_length=64)


class EvolutionCapabilitySpecification(_StrictModel):
    schema_version: Literal[1] = 1
    specification_id: str = Field(pattern=r"^evcs_[0-9a-f]{24}$")
    generator_version: Literal["evolution-capability-specification-v1"] = (
        _GENERATOR_VERSION
    )
    revision: int = Field(ge=1, le=5)
    state: Literal["drafting", "complete"]
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_ids: tuple[str, ...] = Field(min_length=1, max_length=16)
    interface: CapabilityInterfaceSpecification | None = None
    permissions: CapabilityPermissionSpecification | None = None
    data: CapabilityDataSpecification | None = None
    verification: CapabilityVerificationSpecification | None = None
    operations: CapabilityOperationsSpecification | None = None
    completed_steps: tuple[CapabilitySpecificationStep, ...]
    pending_step: CapabilitySpecificationStep | None
    unresolved_requirements: tuple[str, ...]
    interaction_sources: tuple[CapabilitySpecificationInteractionSource, ...] = Field(
        min_length=1,
        max_length=5,
    )
    sandbox_eligible: Literal[False] = False
    shadow_eligible: Literal[False] = False
    executable: Literal[False] = False
    registry_mutation_allowed: Literal[False] = False
    created_at: str = Field(min_length=20, max_length=64)

    @model_validator(mode="after")
    def _revision_is_complete_and_stable(self) -> EvolutionCapabilitySpecification:
        expected_id = _specification_id(
            self.candidate_id,
            self.candidate_revision,
            self.candidate_sha256,
        )
        if self.specification_id != expected_id:
            raise ValueError("specification_id 与 Candidate source 不一致。")
        if len(set(self.proposal_ids)) != len(self.proposal_ids) or any(
            _PROPOSAL_ID_RE.fullmatch(value) is None for value in self.proposal_ids
        ):
            raise ValueError("proposal_ids 必须是唯一有效 ID。")
        populated = tuple(
            step for step in _STEPS if getattr(self, step) is not None
        )
        if self.completed_steps != populated or self.revision != len(populated):
            raise ValueError("Specification revision 必须等于连续完成步骤数。")
        if populated != _STEPS[: len(populated)]:
            raise ValueError("Capability Specification 必须按固定步骤完成。")
        expected_pending = _STEPS[len(populated)] if len(populated) < len(_STEPS) else None
        if self.pending_step != expected_pending:
            raise ValueError("pending_step 与已完成步骤不一致。")
        expected_unresolved = _requirements_from_pending(expected_pending)
        if self.unresolved_requirements != expected_unresolved:
            raise ValueError("unresolved requirements 与进度不一致。")
        expected_state = "complete" if expected_pending is None else "drafting"
        if self.state != expected_state:
            raise ValueError("Specification state 与进度不一致。")
        if len(self.interaction_sources) != self.revision:
            raise ValueError("每个 Specification revision 必须有一个 interaction source。")
        if tuple(item.step for item in self.interaction_sources) != populated:
            raise ValueError("interaction source 顺序与 completed_steps 不一致。")
        if self.permissions is not None and self.data is not None:
            secret_permission = any(
                item.family == "secrets" for item in self.permissions.requirements
            )
            credential_data = "credential_reference" in {
                *self.data.input_classes,
                *self.data.output_classes,
            }
            if secret_permission != credential_data:
                raise ValueError(
                    "secrets permission 与 credential_reference data class 必须同时声明。"
                )
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


class CapabilitySpecificationView(_StrictModel):
    schema_version: Literal[1] = 1
    specification_id: str = Field(pattern=r"^evcs_[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    proposal_id: str = Field(pattern=r"^evcp_[0-9a-f]{24}$")
    revision: int = Field(ge=0, le=5)
    state: Literal["not_started", "drafting", "complete"]
    completed_steps: tuple[CapabilitySpecificationStep, ...]
    pending_step: CapabilitySpecificationStep | None
    unresolved_requirements: tuple[str, ...]
    specification: EvolutionCapabilitySpecification | None
    specification_sha256: str = Field(pattern=r"^(?:|[0-9a-f]{64})$")
    pending_interaction_id: str = Field(max_length=160)
    sandbox_eligible: Literal[False] = False
    shadow_eligible: Literal[False] = False
    executable: Literal[False] = False


class CapabilitySpecificationStoreError(RuntimeError):
    pass


class CapabilitySpecificationInteractionStore(Protocol):
    async def get_interaction(self, **kwargs: Any) -> HarnessInteractionRecord | None: ...
    async def list_interactions(self, **kwargs: Any) -> tuple[HarnessInteractionRecord, ...]: ...


class EvolutionCapabilitySpecificationStore:
    """Append-only specification snapshots keyed by stable Candidate revision."""

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
    ) -> EvolutionCapabilitySpecification | None:
        workspace = _workspace(workspace_root)
        identifier = _spec_id(specification_id)
        if not self._db_path.is_file():
            return None
        await self._ensure_schema()
        try:
            async with self._connection() as db:
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_capability_specifications "
                        "WHERE workspace_root = ? AND specification_id = ? "
                        "ORDER BY revision DESC LIMIT 1",
                        (workspace, identifier),
                    )
                ).fetchone()
                return None if row is None else _specification_from_row(row)
        except CapabilitySpecificationStoreError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise CapabilitySpecificationStoreError(
                "无法读取 Capability Specification。"
            ) from exc

    async def record_step(
        self,
        workspace_root: str | Path,
        *,
        proposal: EvolutionCapabilityProposal,
        step: CapabilitySpecificationStep,
        value: _StrictModel,
        interaction: HarnessInteractionRecord,
        created_at: str,
    ) -> EvolutionCapabilitySpecification:
        workspace = _workspace(workspace_root)
        timestamp = _timestamp(created_at)
        specification_id = _specification_id(
            proposal.source.candidate_id,
            proposal.source.candidate_revision,
            proposal.source.candidate_sha256,
        )
        source = _interaction_source(step, interaction)
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                existing_interaction = await (
                    await db.execute(
                        "SELECT * FROM evolution_capability_specifications "
                        "WHERE workspace_root = ? AND source_interaction_id = ?",
                        (workspace, interaction.interaction_id),
                    )
                ).fetchone()
                if existing_interaction is not None:
                    restored = _specification_from_row(existing_interaction)
                    await db.rollback()
                    return restored
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_capability_specifications "
                        "WHERE workspace_root = ? AND specification_id = ? "
                        "ORDER BY revision DESC LIMIT 1",
                        (workspace, specification_id),
                    )
                ).fetchone()
                current = None if row is None else _specification_from_row(row)
                expected = "interface" if current is None else current.pending_step
                if expected != step:
                    await db.rollback()
                    raise CapabilitySpecificationStoreError(
                        f"Specification 当前步骤是 {expected or 'complete'}，拒绝写入 {step}。"
                    )
                updated = _advance_specification(
                    proposal=proposal,
                    current=current,
                    step=step,
                    value=value,
                    source=source,
                    created_at=timestamp,
                )
                payload = updated.canonical_json()
                digest = updated.digest()
                await db.execute(
                    """
                    INSERT INTO evolution_capability_specifications (
                        workspace_root, specification_id, revision, candidate_id,
                        candidate_revision, candidate_sha256, source_proposal_id,
                        source_interaction_id, payload_json, payload_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace,
                        updated.specification_id,
                        updated.revision,
                        updated.candidate_id,
                        updated.candidate_revision,
                        updated.candidate_sha256,
                        proposal.proposal_id,
                        interaction.interaction_id,
                        payload,
                        digest,
                        updated.created_at,
                    ),
                )
                await db.commit()
                return updated
        except CapabilitySpecificationStoreError:
            raise
        except aiosqlite.IntegrityError as exc:
            raise CapabilitySpecificationStoreError(
                "Capability Specification 并发 revision 冲突，请重读后重试。"
            ) from exc
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise CapabilitySpecificationStoreError(
                "无法保存 Capability Specification。"
            ) from exc

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            try:
                self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                async with self._connection() as db:
                    await db.executescript(_SCHEMA)
                    await db.commit()
                self._db_path.chmod(0o600)
            except (aiosqlite.Error, OSError) as exc:
                raise CapabilitySpecificationStoreError(
                    "无法初始化 Capability Specification Store。"
                ) from exc
            self._schema_ready = True

    def _connection(self):
        return _connection(self._db_path)


class EvolutionCapabilitySpecificationService:
    """Advance exactly one specification step through durable user interaction."""

    def __init__(
        self,
        *,
        review_service: Any,
        store: EvolutionCapabilitySpecificationStore,
        interaction_store: CapabilitySpecificationInteractionStore,
        request_user_input: Callable[[dict[str, Any]], Any],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.review_service = review_service
        self.store = store
        self.interaction_store = interaction_store
        self.request_user_input = request_user_input
        self.clock = clock or (lambda: datetime.now(UTC))

    async def inspect(
        self,
        workspace_root: str | Path,
        candidate_id: str,
    ) -> CapabilitySpecificationView:
        proposal = await self._current_proposal(workspace_root, candidate_id)
        return await self.inspect_capability_specification(workspace_root, proposal)

    async def inspect_capability_specification(
        self,
        workspace_root: str | Path,
        proposal: EvolutionCapabilityProposal,
    ) -> CapabilitySpecificationView:
        specification_id = _specification_id(
            proposal.source.candidate_id,
            proposal.source.candidate_revision,
            proposal.source.candidate_sha256,
        )
        current = await self.store.latest(workspace_root, specification_id)
        pending = await self._pending_interaction(workspace_root, specification_id, current)
        return _view(proposal, current, pending)

    async def advance(
        self,
        workspace_root: str | Path,
        *,
        candidate_id: str,
        session_id: str,
        agent_name: str,
    ) -> CapabilitySpecificationView:
        proposal = await self._current_proposal(workspace_root, candidate_id)
        specification_id = _specification_id(
            proposal.source.candidate_id,
            proposal.source.candidate_revision,
            proposal.source.candidate_sha256,
        )
        current = await self.store.latest(workspace_root, specification_id)
        if current is not None and current.state == "complete":
            return _view(proposal, current, None)
        recovered = await self._recover_answered(
            workspace_root,
            proposal=proposal,
            current=current,
        )
        if recovered is not None:
            return _view(proposal, recovered, None)
        pending = await self._pending_interaction(workspace_root, specification_id, current)
        if pending is not None:
            raise CapabilitySpecificationStoreError(
                f"规格交互 {pending.interaction_id} 仍待回答；请在当前界面完成或接管。"
            )
        step = "interface" if current is None else current.pending_step
        if step is None:
            return _view(proposal, current, None)
        history = await self._history(workspace_root, specification_id)
        attempt = 1 + sum(
            _interaction_step(item.interaction_id) == step for item in history
        )
        if attempt > 999:
            raise CapabilitySpecificationStoreError("单个规格步骤最多创建 999 次交互。")
        interaction_id = f"ask-evcpspec-{specification_id[5:]}-{step}-{attempt}"
        request = _interaction_request(step, proposal)
        payload = {
            **request.to_public_dict(),
            "_interaction_id": interaction_id,
            "_durable_subject_kind": "tool",
            "_durable_subject_id": specification_id,
        }
        try:
            await self.request_user_input(payload)
        except (HarnessStoreError, UserInteractionUnavailableError) as exc:
            raise CapabilitySpecificationStoreError(
                "当前界面无法创建持久 Capability Specification 交互。"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise CapabilitySpecificationStoreError(
                "Capability Specification 交互未通过运行时协议校验。"
            ) from exc
        try:
            interaction = await self.interaction_store.get_interaction(
                workspace_root=workspace_root,
                interaction_id=interaction_id,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise CapabilitySpecificationStoreError(
                "无法重读 Capability Specification interaction authority。"
            ) from exc
        if interaction is None or interaction.state != "answered":
            raise CapabilitySpecificationStoreError(
                "Capability Specification 答案尚未提交到 Harness authority。"
            )
        if interaction.answer_kind != "custom":
            return _view(proposal, current, None)
        refreshed = await self._current_proposal(workspace_root, candidate_id)
        refreshed_specification_id = _specification_id(
            refreshed.source.candidate_id,
            refreshed.source.candidate_revision,
            refreshed.source.candidate_sha256,
        )
        if refreshed_specification_id != specification_id:
            raise CapabilitySpecificationStoreError(
                "回答期间 Candidate revision 已变化；旧答案不会写入新规格。"
            )
        value = _parse_step(step, interaction.custom_text, refreshed)
        updated = await self.store.record_step(
            workspace_root,
            proposal=refreshed,
            step=step,
            value=value,
            interaction=interaction,
            created_at=self.clock().isoformat(),
        )
        return _view(proposal, updated, None)

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
            raise CapabilitySpecificationStoreError(
                "Candidate 当前没有 authority/cooldown/Portfolio 均有效的 Capability Proposal。"
            )
        return proposal

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
            raise CapabilitySpecificationStoreError(
                "无法读取 Capability Specification 交互历史。"
            ) from exc

    async def _pending_interaction(
        self,
        workspace_root: str | Path,
        specification_id: str,
        current: EvolutionCapabilitySpecification | None,
    ) -> HarnessInteractionRecord | None:
        expected = "interface" if current is None else current.pending_step
        if expected is None:
            return None
        history = await self._history(workspace_root, specification_id)
        return next(
            (
                item
                for item in history
                if item.state == "pending"
                and _interaction_step(item.interaction_id) == expected
            ),
            None,
        )

    async def _recover_answered(
        self,
        workspace_root: str | Path,
        *,
        proposal: EvolutionCapabilityProposal,
        current: EvolutionCapabilitySpecification | None,
    ) -> EvolutionCapabilitySpecification | None:
        expected = "interface" if current is None else current.pending_step
        if expected is None:
            return None
        specification_id = _specification_id(
            proposal.source.candidate_id,
            proposal.source.candidate_revision,
            proposal.source.candidate_sha256,
        )
        history = await self._history(workspace_root, specification_id)
        answered = tuple(
            item
            for item in history
            if item.state == "answered"
            and item.answer_kind == "custom"
            and _interaction_step(item.interaction_id) == expected
        )
        recorded_ids = {
            item.interaction_id
            for item in (current.interaction_sources if current is not None else ())
        }
        unrecorded = tuple(
            item for item in answered if item.interaction_id not in recorded_ids
        )
        if not unrecorded:
            return None
        valid: list[tuple[HarnessInteractionRecord, _StrictModel]] = []
        for interaction in unrecorded:
            try:
                value = _parse_step(expected, interaction.custom_text, proposal)
            except (TypeError, ValueError):
                continue
            valid.append((interaction, value))
        if not valid:
            return None
        if len(valid) != 1:
            raise CapabilitySpecificationStoreError(
                "同一步骤存在多个未对账答案，拒绝猜测哪一个有效。"
            )
        interaction, value = valid[0]
        return await self.store.record_step(
            workspace_root,
            proposal=proposal,
            step=expected,
            value=value,
            interaction=interaction,
            created_at=self.clock().isoformat(),
        )


def render_capability_specification(view: CapabilitySpecificationView) -> str:
    completed = ", ".join(view.completed_steps) or "-"
    pending = view.pending_step or "无"
    lines = [
        "# Capability Specification",
        "",
        f"- ID：`{view.specification_id}`",
        f"- Candidate：`{view.candidate_id}`",
        f"- Proposal：`{view.proposal_id}`",
        f"- 状态：`{view.state}` · revision {view.revision}/5",
        f"- 已完成：`{completed}`",
        f"- 下一步：`{pending}`",
        "- Sandbox：否 · Shadow：否 · 可执行：否",
    ]
    if view.pending_interaction_id:
        lines.append(f"- 待回答交互：`{view.pending_interaction_id}`")
    if view.unresolved_requirements:
        lines.extend(["", "## 未决项", ""])
        lines.extend(f"- `{item}`" for item in view.unresolved_requirements)
    if view.pending_step is not None:
        lines.extend([
            "",
            f"继续：`/evolution capability-spec {view.candidate_id}`",
        ])
    else:
        lines.extend([
            "",
            "> 规格完整只表示可进入人工治理；仍未授予 Sandbox 注册或执行权限。",
        ])
    return "\n".join(lines)


def _parse_step(
    step: CapabilitySpecificationStep,
    text: str,
    proposal: EvolutionCapabilityProposal,
) -> _StrictModel:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Capability Specification 自定义答案不能为空。")
    if len(text) > 4_000 or _SECRET_RE.search(text):
        raise ValueError("Capability Specification 答案过长或疑似包含 secret。")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Capability Specification 必须使用有效 JSON。") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("Capability Specification 每一步必须提交 JSON object。")
    if step == "interface":
        value = CapabilityInterfaceSpecification.model_validate(payload)
        known = proposal.interface.requested_name
        if known is not None and value.tool_name != known:
            raise ValueError("Tool Catalog miss 的 tool_name 不得改写。")
        return value
    if step == "permissions":
        return CapabilityPermissionSpecification.model_validate(payload)
    if step == "data":
        value = CapabilityDataSpecification.model_validate(payload)
        return value
    if step == "verification":
        return CapabilityVerificationSpecification.model_validate(payload)
    if step == "operations":
        return CapabilityOperationsSpecification.model_validate(payload)
    raise ValueError("未知 Capability Specification step。")


def validate_capability_specification_step(
    step: CapabilitySpecificationStep,
    text: str,
    proposal: EvolutionCapabilityProposal,
) -> _StrictModel:
    """Replay one durable answer through the authoritative step validator."""
    return _parse_step(step, text, proposal)


def _interaction_request(
    step: CapabilitySpecificationStep,
    proposal: EvolutionCapabilityProposal,
) -> UserInteractionRequest:
    examples = {
        "interface": (
            "提交 API JSON",
            '{"tool_name":"name","parameters_schema":{"type":"object",'
            '"properties":{},"additionalProperties":false},"result_schema":'
            '{"type":"object","properties":{},"additionalProperties":false},'
            '"errors":[{"code":"failed","message":"失败",'
            '"retryable":false}],"version":"1.0.0"}',
        ),
        "permissions": (
            "提交权限 JSON",
            '{"requirements":[{"family":"workspace_read","scopes":["src/**"],'
            '"justification":"读取工作区输入"}]}',
        ),
        "data": (
            "提交数据 JSON",
            '{"input_classes":["workspace_content"],"output_classes":'
            '["generated_content"],"retention":"none",'
            '"sensitive_handling":"deny"}',
        ),
        "verification": (
            "提交验收 JSON",
            '{"scenarios":[{"name":"真实场景","fixture":"输入说明",'
            '"expected":"可机械判断的预期"}]}',
        ),
        "operations": (
            "提交运维 JSON",
            '{"owner":"team-name","latency_p95_ms":1000,'
            '"success_rate_percent":99,"maintenance":"维护与升级责任",'
            '"additional_retirement_criteria":[]}',
        ),
    }
    label, example = examples[step]
    known = proposal.interface.requested_name
    suffix = f"；tool_name 必须为 {known}" if step == "interface" and known else ""
    return UserInteractionRequest(
        header=f"能力规格 · {step}",
        question=f"{label}{suffix}。请选择稍后处理，或使用“其他”粘贴 JSON。示例：{example}",
        options=(
            UserInteractionOption(
                value="defer",
                label="稍后填写",
                description="保持当前步骤未决，不获得 Sandbox 资格。",
            ),
            UserInteractionOption(
                value="inspect",
                label="仅查看状态",
                description="结束本次交互并显示仍待补齐的字段。",
            ),
        ),
        allow_custom=True,
        custom_label="粘贴 JSON",
        timeout_seconds=None,
        priority="normal",
    )


def _advance_specification(
    *,
    proposal: EvolutionCapabilityProposal,
    current: EvolutionCapabilitySpecification | None,
    step: CapabilitySpecificationStep,
    value: _StrictModel,
    source: CapabilitySpecificationInteractionSource,
    created_at: str,
) -> EvolutionCapabilitySpecification:
    values: dict[str, Any] = {
        name: (getattr(current, name) if current is not None else None)
        for name in _STEPS
    }
    values[step] = value
    completed = tuple(name for name in _STEPS if values[name] is not None)
    pending = _STEPS[len(completed)] if len(completed) < len(_STEPS) else None
    proposals = tuple(
        dict.fromkeys(
            (
                *(current.proposal_ids if current is not None else ()),
                proposal.proposal_id,
            )
        )
    )
    sources = (
        *(current.interaction_sources if current is not None else ()),
        source,
    )
    return EvolutionCapabilitySpecification(
        specification_id=_specification_id(
            proposal.source.candidate_id,
            proposal.source.candidate_revision,
            proposal.source.candidate_sha256,
        ),
        revision=len(completed),
        state="complete" if pending is None else "drafting",
        candidate_id=proposal.source.candidate_id,
        candidate_revision=proposal.source.candidate_revision,
        candidate_sha256=proposal.source.candidate_sha256,
        proposal_ids=proposals,
        interface=values["interface"],
        permissions=values["permissions"],
        data=values["data"],
        verification=values["verification"],
        operations=values["operations"],
        completed_steps=completed,
        pending_step=pending,
        unresolved_requirements=_requirements_from_pending(pending),
        interaction_sources=sources,
        created_at=created_at,
    )


def _view(
    proposal: EvolutionCapabilityProposal,
    current: EvolutionCapabilitySpecification | None,
    pending: HarnessInteractionRecord | None,
) -> CapabilitySpecificationView:
    specification_id = _specification_id(
        proposal.source.candidate_id,
        proposal.source.candidate_revision,
        proposal.source.candidate_sha256,
    )
    return CapabilitySpecificationView(
        specification_id=specification_id,
        candidate_id=proposal.source.candidate_id,
        proposal_id=proposal.proposal_id,
        revision=current.revision if current is not None else 0,
        state=current.state if current is not None else "not_started",
        completed_steps=current.completed_steps if current is not None else (),
        pending_step=current.pending_step if current is not None else "interface",
        unresolved_requirements=(
            current.unresolved_requirements
            if current is not None
            else _requirements_from_pending("interface")
        ),
        specification=current,
        specification_sha256=current.digest() if current is not None else "",
        pending_interaction_id=pending.interaction_id if pending is not None else "",
    )


def _requirements_from_pending(
    pending: CapabilitySpecificationStep | None,
) -> tuple[str, ...]:
    mapping: dict[CapabilitySpecificationStep, tuple[str, ...]] = {
        "interface": (
            "api.tool_name",
            "api.parameters_schema",
            "api.result_schema",
            "api.error_contract",
            "api.versioning",
        ),
        "permissions": ("permissions.required_families", "permissions.scopes"),
        "data": ("data.input_output_retention", "data.sensitive_handling"),
        "verification": ("verification.real_scenario",),
        "operations": (
            "operations.owner",
            "operations.slo",
            "operations.maintenance",
        ),
    }
    if pending is None:
        return ()
    index = _STEPS.index(pending)
    return tuple(item for step in _STEPS[index:] for item in mapping[step])


def _specification_id(candidate_id: str, revision: int, digest: str) -> str:
    if _CANDIDATE_ID_RE.fullmatch(candidate_id) is None or revision < 1:
        raise ValueError("Capability Specification Candidate identity 无效。")
    if _SHA256_RE.fullmatch(digest) is None:
        raise ValueError("Capability Specification Candidate digest 无效。")
    payload = json.dumps(
        {
            "candidate_id": candidate_id,
            "candidate_revision": revision,
            "candidate_sha256": digest,
            "generator_version": _GENERATOR_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"evcs_{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def _spec_id(value: str) -> str:
    normalized = str(value).strip().lower()
    if _SPEC_ID_RE.fullmatch(normalized) is None:
        raise ValueError("Capability Specification ID 格式无效。")
    return normalized


def _interaction_step(value: str) -> CapabilitySpecificationStep | None:
    match = _INTERACTION_ID_RE.fullmatch(str(value))
    return None if match is None else match.group(2)  # type: ignore[return-value]


def _interaction_source(
    step: CapabilitySpecificationStep,
    interaction: HarnessInteractionRecord,
) -> CapabilitySpecificationInteractionSource:
    if interaction.state != "answered" or interaction.answer_kind != "custom":
        raise ValueError("Specification revision 只能消费已提交的 custom interaction。")
    if _interaction_step(interaction.interaction_id) != step:
        raise ValueError("interaction identity 与 Specification step 不一致。")
    return CapabilitySpecificationInteractionSource(
        step=step,
        interaction_id=interaction.interaction_id,
        interaction_sequence=interaction.sequence,
        interaction_sha256=interaction.digest(),
        answered_at=interaction.answered_at,
    )


def _validate_json_schema(
    value: Mapping[str, Any],
    *,
    field: str,
    object_root: bool,
) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} 必须是 JSON Schema object。")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) > 12_000 or _SECRET_RE.search(encoded):
        raise ValueError(f"{field} 过大或疑似包含 secret。")
    node_count = 0

    def visit(node: Any, depth: int) -> None:
        nonlocal node_count
        node_count += 1
        if node_count > 256 or depth > 8:
            raise ValueError(f"{field} 超出 256 nodes / 8 层限制。")
        if isinstance(node, Mapping):
            if "$ref" in node or "$dynamicRef" in node:
                raise ValueError(f"{field} v1 不允许外部或递归 $ref。")
            for key, child in node.items():
                if not isinstance(key, str):
                    raise ValueError(f"{field} key 必须是字符串。")
                visit(child, depth + 1)
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            for child in node:
                visit(child, depth + 1)
        elif not isinstance(node, (str, int, float, bool, type(None))):
            raise ValueError(f"{field} 包含非 JSON 值。")

    visit(value, 0)
    allowed_types = {"object", "array", "string", "number", "integer", "boolean", "null"}
    if value.get("type") not in allowed_types:
        raise ValueError(f"{field}.type 必须是明确 JSON 类型。")
    if object_root and value.get("type") != "object":
        raise ValueError("parameters_schema 根节点必须为 object。")
    if value.get("type") == "object":
        properties = value.get("properties")
        required = value.get("required", [])
        if not isinstance(properties, Mapping) or not isinstance(required, list):
            raise ValueError(f"{field} object 必须声明 properties 和数组 required。")
        if value.get("additionalProperties") is not False:
            raise ValueError(f"{field} object 必须显式 additionalProperties=false。")
        if any(item not in properties for item in required) or len(set(required)) != len(required):
            raise ValueError(f"{field}.required 必须唯一且属于 properties。")


def _validate_permission_scope(family: str, value: str) -> None:
    scope = _safe_text(value, field=f"permission.{family}.scope", maximum=300)
    if family in {"workspace_read", "workspace_write"}:
        parts = scope.replace("\\", "/").split("/")
        if _ABSOLUTE_PATH_RE.match(scope) or ".." in parts:
            raise ValueError("workspace permission scope 必须是安全相对路径/glob。")
    elif family in {"network", "browser"}:
        if _DOMAIN_RE.fullmatch(scope.casefold()) is None:
            raise ValueError("network/browser scope 必须是精确域名或 *.domain。")
    elif family == "process":
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}", scope) is None:
            raise ValueError("process scope 必须是无参数 executable 名。")
    elif family == "secrets":
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:/-]{0,127}", scope) is None:
            raise ValueError("secrets scope 只能是逻辑 credential reference ID。")
    else:
        raise ValueError("未知 permission family。")


def _safe_text(
    value: object,
    *,
    field: str,
    maximum: int,
    multiline: bool = False,
) -> str:
    text = str(value or "").strip()
    if not multiline:
        text = " ".join(text.split())
    if not text or len(text) > maximum or "\x00" in text or _SECRET_RE.search(text):
        raise ValueError(f"{field} 为空、过长、含控制字符或疑似 secret。")
    return text


def _workspace(value: str | Path) -> str:
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError("workspace_root 必须是现有目录。")
    return str(path)


def _timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("Capability Specification 时间必须包含时区。")
    return parsed.isoformat()


def _specification_from_row(row: aiosqlite.Row) -> EvolutionCapabilitySpecification:
    payload = str(row["payload_json"])
    digest = str(row["payload_sha256"])
    if hashlib.sha256(payload.encode()).hexdigest() != digest:
        raise CapabilitySpecificationStoreError("Capability Specification 摘要不一致。")
    try:
        value = EvolutionCapabilitySpecification.model_validate_json(payload)
    except ValueError as exc:
        raise CapabilitySpecificationStoreError("Capability Specification JSON 损坏。") from exc
    if (
        str(row["specification_id"]) != value.specification_id
        or int(row["revision"]) != value.revision
        or str(row["candidate_id"]) != value.candidate_id
        or int(row["candidate_revision"]) != value.candidate_revision
        or str(row["candidate_sha256"]) != value.candidate_sha256
        or str(row["source_interaction_id"])
        != value.interaction_sources[-1].interaction_id
        or str(row["source_proposal_id"]) != value.proposal_ids[-1]
        or str(row["created_at"]) != value.created_at
    ):
        raise CapabilitySpecificationStoreError(
            "Capability Specification 投影列与 payload 不一致。"
        )
    return value


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
CREATE TABLE IF NOT EXISTS evolution_capability_specifications (
    workspace_root TEXT NOT NULL,
    specification_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 5),
    candidate_id TEXT NOT NULL,
    candidate_revision INTEGER NOT NULL,
    candidate_sha256 TEXT NOT NULL,
    source_proposal_id TEXT NOT NULL,
    source_interaction_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_root, specification_id, revision),
    UNIQUE (workspace_root, source_interaction_id)
);
CREATE INDEX IF NOT EXISTS idx_evolution_capability_specifications_candidate
ON evolution_capability_specifications (
    workspace_root, candidate_id, candidate_revision, revision DESC
);
"""


__all__ = [
    "CapabilityDataSpecification",
    "CapabilityInterfaceSpecification",
    "CapabilityOperationsSpecification",
    "CapabilityPermissionSpecification",
    "CapabilityScenarioSpecification",
    "CapabilitySpecificationStoreError",
    "CapabilitySpecificationView",
    "CapabilityVerificationSpecification",
    "EvolutionCapabilitySpecification",
    "EvolutionCapabilitySpecificationService",
    "EvolutionCapabilitySpecificationStore",
    "render_capability_specification",
    "validate_capability_specification_step",
]
