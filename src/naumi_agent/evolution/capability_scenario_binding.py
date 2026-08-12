"""Interaction-backed executable scenarios for sealed capability artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

import aiosqlite
from jsonschema import Draft202012Validator, FormatChecker, SchemaError, ValidationError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.capability_artifact import (
    CapabilityArtifactView,
    EvolutionCapabilityArtifactService,
    EvolutionCapabilityImplementationArtifact,
)
from naumi_agent.evolution.capability_specification import (
    EvolutionCapabilitySpecification,
    EvolutionCapabilitySpecificationService,
)
from naumi_agent.harness.interaction import HarnessInteractionRecord
from naumi_agent.harness.store import HarnessStoreError
from naumi_agent.user_interaction import (
    UserInteractionOption,
    UserInteractionRequest,
    UserInteractionUnavailableError,
)

_POLICY_VERSION = "evolution-capability-scenario-binding-v1"
_INTERACTION_RE = re.compile(r"^ask-evcsbind-([0-9a-f]{24})-(\d{1,3})$")
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


class CapabilityScenarioExpectation(_StrictModel):
    kind: Literal["result", "error"]
    value: Any = None
    error_code: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> CapabilityScenarioExpectation:
        if self.kind == "result" and self.error_code:
            raise ValueError("result expectation 不得包含 error_code。")
        if self.kind == "error" and (
            not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.error_code)
            or self.value is not None
        ):
            raise ValueError("error expectation 必须只包含有效 error_code。")
        _bounded_json(self.model_dump(mode="json"), field="expectation", maximum=8_000)
        return self


class CapabilityExecutableScenario(_StrictModel):
    name: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any]
    expectation: CapabilityScenarioExpectation
    timeout_ms: int = Field(ge=100, le=300_000)

    @model_validator(mode="after")
    def _scenario_is_bounded(self) -> CapabilityExecutableScenario:
        _safe_text(self.name, field="scenario.name", maximum=80)
        _bounded_json(self.arguments, field="scenario.arguments", maximum=12_000)
        return self


class EvolutionCapabilityScenarioBinding(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-capability-scenario-binding-v1"] = (
        _POLICY_VERSION
    )
    binding_id: str = Field(pattern=r"^evcsb_[0-9a-f]{24}$")
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    specification_id: str = Field(pattern=r"^evcs_[0-9a-f]{24}$")
    specification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(pattern=r"^evcia_[0-9a-f]{24}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    permission_specification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_specification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_interaction_id: str = Field(
        pattern=r"^ask-evcsbind-[0-9a-f]{24}-\d{1,3}$"
    )
    source_interaction_sequence: int = Field(ge=2)
    source_interaction_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    answered_by: Literal["user"] = "user"
    scenarios: tuple[CapabilityExecutableScenario, ...] = Field(
        min_length=1,
        max_length=8,
    )
    schema_dialect: Literal["https://json-schema.org/draft/2020-12"] = (
        "https://json-schema.org/draft/2020-12"
    )
    sandbox_execution_eligible: Literal[True] = True
    sandbox_execution_authorized: Literal[False] = False
    registration_authorized: Literal[False] = False
    shadow_authorized: Literal[False] = False
    executable: Literal[False] = False
    created_at: str = Field(min_length=20, max_length=64)

    @model_validator(mode="after")
    def _identity_is_content_addressed(self) -> EvolutionCapabilityScenarioBinding:
        names = tuple(item.name for item in self.scenarios)
        if len(names) != len(set(names)):
            raise ValueError("Executable scenario name 不得重复。")
        payload = self.model_dump(
            mode="json",
            exclude={"binding_id", "binding_sha256"},
        )
        digest = _digest(payload)
        if not hmac.compare_digest(self.binding_sha256, digest):
            raise ValueError("Capability scenario binding 摘要不一致。")
        if self.binding_id != f"evcsb_{digest[:24]}":
            raise ValueError("Capability scenario binding identity 不一致。")
        return self

    def canonical_json(self) -> str:
        return _canonical(self.model_dump(mode="json"))


class CapabilityScenarioBindingView(_StrictModel):
    schema_version: Literal[1] = 1
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    artifact_id: str = Field(pattern=r"^evcia_[0-9a-f]{24}$")
    binding: EvolutionCapabilityScenarioBinding | None
    state: Literal["missing", "awaiting_input", "ready", "revoked"]
    pending_interaction_id: str = Field(max_length=160)
    artifact_current: bool
    binding_current: bool
    sandbox_execution_eligible: bool
    sandbox_execution_authorized: Literal[False] = False
    registration_authorized: Literal[False] = False
    shadow_authorized: Literal[False] = False
    executable: Literal[False] = False

    @model_validator(mode="after")
    def _state_matches_authority(self) -> CapabilityScenarioBindingView:
        if self.binding is None:
            if self.state not in {"missing", "awaiting_input"}:
                raise ValueError("无 binding 时状态无效。")
            if self.binding_current or self.sandbox_execution_eligible:
                raise ValueError("无 binding 时不得声明 Sandbox eligibility。")
        else:
            expected = "ready" if self.artifact_current and self.binding_current else "revoked"
            if self.state != expected:
                raise ValueError("Scenario binding current 状态不一致。")
            if self.sandbox_execution_eligible != (expected == "ready"):
                raise ValueError("Sandbox execution eligibility 与状态不一致。")
        return self


class CapabilityScenarioBindingError(RuntimeError):
    pass


class CapabilityScenarioInteractionStore(Protocol):
    async def get_interaction(self, **kwargs: Any) -> HarnessInteractionRecord | None: ...
    async def list_interactions(self, **kwargs: Any) -> tuple[HarnessInteractionRecord, ...]: ...


class EvolutionCapabilityScenarioBindingStore:
    """One immutable user-authored executable binding per sealed artifact."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def get(
        self,
        workspace_root: str | Path,
        artifact_id: str,
    ) -> EvolutionCapabilityScenarioBinding | None:
        workspace = _workspace(workspace_root)
        if not self._db_path.is_file():
            return None
        await self._ensure_schema()
        try:
            async with aiosqlite.connect(self._db_path) as db:
                row = await (
                    await db.execute(
                        "SELECT payload_json, payload_sha256 FROM "
                        "evolution_capability_scenario_bindings "
                        "WHERE workspace_root = ? AND artifact_id = ?",
                        (workspace, artifact_id),
                    )
                ).fetchone()
        except (aiosqlite.Error, OSError) as exc:
            raise CapabilityScenarioBindingError(
                "无法读取 Capability Scenario Binding。"
            ) from exc
        return None if row is None else _restore(row[0], row[1])

    async def record(
        self,
        workspace_root: str | Path,
        binding: EvolutionCapabilityScenarioBinding,
    ) -> EvolutionCapabilityScenarioBinding:
        workspace = _workspace(workspace_root)
        await self._ensure_schema()
        payload = binding.canonical_json()
        payload_sha256 = hashlib.sha256(payload.encode()).hexdigest()
        try:
            async with self._write_lock, aiosqlite.connect(self._db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                existing = await (
                    await db.execute(
                        "SELECT payload_json, payload_sha256 FROM "
                        "evolution_capability_scenario_bindings "
                        "WHERE workspace_root = ? AND artifact_id = ?",
                        (workspace, binding.artifact_id),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(existing[0], existing[1])
                    await db.rollback()
                    if restored.source_interaction_id == binding.source_interaction_id:
                        return restored
                    raise CapabilityScenarioBindingError(
                        f"Artifact 已由 {restored.binding_id} 绑定，拒绝覆盖。"
                    )
                await db.execute(
                    "INSERT INTO evolution_capability_scenario_bindings "
                    "(workspace_root, artifact_id, binding_id, source_interaction_id, "
                    "payload_json, payload_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        workspace,
                        binding.artifact_id,
                        binding.binding_id,
                        binding.source_interaction_id,
                        payload,
                        payload_sha256,
                        binding.created_at,
                    ),
                )
                await db.commit()
                return binding
        except CapabilityScenarioBindingError:
            raise
        except (aiosqlite.Error, OSError) as exc:
            raise CapabilityScenarioBindingError(
                "无法保存 Capability Scenario Binding。"
            ) from exc

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                async with aiosqlite.connect(self._db_path) as db:
                    await db.executescript(_SCHEMA)
                    await db.commit()
                self._db_path.chmod(0o600)
            except (aiosqlite.Error, OSError) as exc:
                raise CapabilityScenarioBindingError(
                    "无法初始化 Capability Scenario Binding Store。"
                ) from exc
            self._schema_ready = True


class EvolutionCapabilityScenarioBindingService:
    def __init__(
        self,
        *,
        review_service: Any,
        specification_service: EvolutionCapabilitySpecificationService,
        artifact_service: EvolutionCapabilityArtifactService,
        store: EvolutionCapabilityScenarioBindingStore,
        interaction_store: CapabilityScenarioInteractionStore,
        request_user_input: Callable[[dict[str, Any]], Any],
    ) -> None:
        self.review_service = review_service
        self.specification_service = specification_service
        self.artifact_service = artifact_service
        self.store = store
        self.interaction_store = interaction_store
        self.request_user_input = request_user_input

    async def inspect(
        self,
        workspace_root: str | Path,
        candidate_id: str,
    ) -> CapabilityScenarioBindingView:
        workspace, specification, artifact_view = await self._current_source(
            workspace_root,
            candidate_id,
        )
        artifact = artifact_view.artifact
        if artifact is None:
            raise CapabilityScenarioBindingError("当前没有 Capability Artifact。")
        binding = await self.store.get(workspace, artifact.artifact_id)
        artifact_current = artifact_view.state == "preview_ready"
        pending = (
            await self._pending(workspace, artifact.artifact_id, binding)
            if artifact_current
            else None
        )
        return _view(
            candidate_id=specification.candidate_id,
            artifact=artifact,
            binding=binding,
            pending=pending,
            artifact_current=artifact_current,
            interaction_current=await self._binding_interaction_current(
                workspace,
                binding,
            ),
        )

    async def advance(
        self,
        workspace_root: str | Path,
        *,
        candidate_id: str,
    ) -> CapabilityScenarioBindingView:
        workspace, specification, artifact_view = await self._current_source(
            workspace_root,
            candidate_id,
        )
        artifact = _ready_artifact(artifact_view)
        existing = await self.store.get(workspace, artifact.artifact_id)
        if existing is not None:
            return _view(
                candidate_id=specification.candidate_id,
                artifact=artifact,
                binding=existing,
                pending=None,
                artifact_current=True,
                interaction_current=await self._binding_interaction_current(
                    workspace,
                    existing,
                ),
            )
        recovered = await self._recover(workspace, specification, artifact)
        if recovered is not None:
            return _view(
                candidate_id=specification.candidate_id,
                artifact=artifact,
                binding=recovered,
                pending=None,
                artifact_current=True,
                interaction_current=True,
            )
        pending = await self._pending(workspace, artifact.artifact_id, None)
        if pending is not None:
            raise CapabilityScenarioBindingError(
                f"场景绑定交互 {pending.interaction_id} 仍待回答。"
            )
        history = await self._history(workspace, artifact.artifact_id)
        attempts = tuple(
            attempt
            for item in history
            if (attempt := _interaction_attempt(
                item.interaction_id,
                artifact.artifact_id,
            )) is not None
        )
        attempt = 1 + max(attempts, default=0)
        if len(attempts) >= 100 or attempt > 100:
            raise CapabilityScenarioBindingError("单个 Artifact 最多创建 100 次绑定交互。")
        interaction_id = f"ask-evcsbind-{artifact.artifact_id[6:]}-{attempt}"
        payload = {
            **_interaction_request(specification).to_public_dict(),
            "_interaction_id": interaction_id,
            "_durable_subject_kind": "tool",
            "_durable_subject_id": artifact.artifact_id,
        }
        try:
            await self.request_user_input(payload)
        except (HarnessStoreError, UserInteractionUnavailableError) as exc:
            raise CapabilityScenarioBindingError(
                "当前界面无法创建持久 Scenario Binding 交互。"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise CapabilityScenarioBindingError(
                "Scenario Binding 交互未通过运行时协议校验。"
            ) from exc
        interaction = await self._get_interaction(workspace, interaction_id)
        if interaction is None or interaction.state != "answered":
            raise CapabilityScenarioBindingError("场景绑定答案尚未提交到 Harness authority。")
        if interaction.answer_kind == "option":
            return _view(
                candidate_id=specification.candidate_id,
                artifact=artifact,
                binding=None,
                pending=None,
                artifact_current=True,
                interaction_current=False,
            )
        binding = build_capability_scenario_binding(
            specification=specification,
            artifact=artifact,
            interaction=interaction,
        )
        _, refreshed_specification, refreshed_artifact_view = await self._current_source(
            workspace,
            candidate_id,
        )
        refreshed_artifact = _ready_artifact(refreshed_artifact_view)
        if (
            refreshed_specification.digest() != specification.digest()
            or refreshed_artifact.artifact_id != artifact.artifact_id
            or refreshed_artifact.artifact_sha256 != artifact.artifact_sha256
        ):
            raise CapabilityScenarioBindingError(
                "回答期间 Specification 或 Artifact 已变化；旧答案不会生效。"
            )
        stored = await self.store.record(workspace, binding)
        return _view(
            candidate_id=specification.candidate_id,
            artifact=artifact,
            binding=stored,
            pending=None,
            artifact_current=True,
            interaction_current=True,
        )

    async def inspect_capability_scenario_binding(
        self,
        workspace_root: str | Path,
        candidate_id: str,
        artifact_view: CapabilityArtifactView,
    ) -> CapabilityScenarioBindingView | None:
        if artifact_view.artifact is None:
            return None
        artifact = artifact_view.artifact
        binding = await self.store.get(workspace_root, artifact.artifact_id)
        current = artifact_view.state == "preview_ready"
        pending = (
            await self._pending(workspace_root, artifact.artifact_id, binding)
            if current
            else None
        )
        workspace = Path(workspace_root).expanduser().resolve(strict=True)
        return _view(
            candidate_id=candidate_id,
            artifact=artifact,
            binding=binding,
            pending=pending,
            artifact_current=current,
            interaction_current=await self._binding_interaction_current(
                workspace,
                binding,
            ),
        )

    async def _current_source(
        self,
        workspace_root: str | Path,
        candidate_id: str,
    ) -> tuple[Path, EvolutionCapabilitySpecification, CapabilityArtifactView]:
        workspace = Path(workspace_root).expanduser().resolve(strict=True)
        snapshot = await self.review_service.detail_snapshot(
            workspace,
            str(candidate_id).strip(),
            include_capability_extensions=False,
        )
        proposal = (
            snapshot.selected.capability_proposal
            if snapshot.selected is not None
            else None
        )
        if proposal is None:
            raise CapabilityScenarioBindingError("Candidate 当前没有有效 Capability Proposal。")
        specification_view = (
            await self.specification_service.inspect_capability_specification(
                workspace,
                proposal,
            )
        )
        specification = specification_view.specification
        if specification is None or specification.state != "complete":
            raise CapabilityScenarioBindingError("Capability Specification 尚未完成。")
        artifact_view = await self.artifact_service.inspect(workspace, candidate_id)
        return workspace, specification, artifact_view

    async def _get_interaction(
        self,
        workspace: Path,
        interaction_id: str,
    ) -> HarnessInteractionRecord | None:
        try:
            return await self.interaction_store.get_interaction(
                workspace_root=workspace,
                interaction_id=interaction_id,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise CapabilityScenarioBindingError(
                "无法读取 Scenario Binding interaction authority。"
            ) from exc

    async def _history(
        self,
        workspace: Path,
        artifact_id: str,
    ) -> tuple[HarnessInteractionRecord, ...]:
        try:
            return await self.interaction_store.list_interactions(
                workspace_root=workspace,
                subject_kind="tool",
                subject_ids=(artifact_id,),
                limit=100,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise CapabilityScenarioBindingError(
                "无法读取 Scenario Binding 交互历史。"
            ) from exc

    async def _binding_interaction_current(
        self,
        workspace: Path,
        binding: EvolutionCapabilityScenarioBinding | None,
    ) -> bool:
        if binding is None:
            return False
        interaction = await self._get_interaction(
            workspace,
            binding.source_interaction_id,
        )
        source_current = bool(
            interaction is not None
            and interaction.subject_kind == "tool"
            and interaction.subject_id == binding.artifact_id
            and interaction.state == "answered"
            and interaction.answer_kind == "custom"
            and interaction.answered_by == "user"
            and interaction.sequence == binding.source_interaction_sequence
            and hmac.compare_digest(
                interaction.digest(),
                binding.source_interaction_sha256,
            )
        )
        if not source_current or interaction is None:
            return False
        try:
            return _parse_scenarios(interaction.custom_text) == binding.scenarios
        except CapabilityScenarioBindingError:
            return False

    async def _pending(
        self,
        workspace_root: str | Path,
        artifact_id: str,
        binding: EvolutionCapabilityScenarioBinding | None,
    ) -> HarnessInteractionRecord | None:
        if binding is not None:
            return None
        workspace = Path(workspace_root).expanduser().resolve(strict=True)
        history = await self._history(workspace, artifact_id)
        return next(
            (
                item
                for item in history
                if item.state == "pending"
                and _interaction_matches(item.interaction_id, artifact_id)
            ),
            None,
        )

    async def _recover(
        self,
        workspace: Path,
        specification: EvolutionCapabilitySpecification,
        artifact: EvolutionCapabilityImplementationArtifact,
    ) -> EvolutionCapabilityScenarioBinding | None:
        history = await self._history(workspace, artifact.artifact_id)
        answered = tuple(
            item
            for item in history
            if item.state == "answered"
            and item.answer_kind == "custom"
            and _interaction_matches(item.interaction_id, artifact.artifact_id)
        )
        valid: list[EvolutionCapabilityScenarioBinding] = []
        for interaction in answered:
            try:
                valid.append(build_capability_scenario_binding(
                    specification=specification,
                    artifact=artifact,
                    interaction=interaction,
                ))
            except CapabilityScenarioBindingError:
                continue
        if not valid:
            return None
        if len(valid) != 1:
            raise CapabilityScenarioBindingError(
                "存在多个有效但未对账 Scenario Binding 答案，拒绝猜测。"
            )
        return await self.store.record(workspace, valid[0])


def build_capability_scenario_binding(
    *,
    specification: EvolutionCapabilitySpecification,
    artifact: EvolutionCapabilityImplementationArtifact,
    interaction: HarnessInteractionRecord,
) -> EvolutionCapabilityScenarioBinding:
    if not artifact.admission_ready:
        raise CapabilityScenarioBindingError("Artifact admission preview 未通过。")
    if (
        artifact.specification_id != specification.specification_id
        or artifact.specification_sha256 != specification.digest()
        or artifact.candidate_id != specification.candidate_id
    ):
        raise CapabilityScenarioBindingError("Artifact 未绑定当前 Specification。")
    if (
        interaction.state != "answered"
        or interaction.answer_kind != "custom"
        or interaction.answered_by != "user"
        or interaction.subject_kind != "tool"
        or interaction.subject_id != artifact.artifact_id
        or not _interaction_matches(interaction.interaction_id, artifact.artifact_id)
    ):
        raise CapabilityScenarioBindingError("Scenario Binding 必须来自当前人工 Harness 答案。")
    if len(interaction.custom_text) > 4_000 or _SECRET_RE.search(interaction.custom_text):
        raise CapabilityScenarioBindingError("Scenario Binding 答案过长或疑似包含 secret。")
    scenarios = _parse_scenarios(interaction.custom_text)
    verification = specification.verification
    interface = specification.interface
    permissions = specification.permissions
    if verification is None or interface is None or permissions is None:
        raise CapabilityScenarioBindingError("Specification 缺少验证、接口或权限契约。")
    expected_names = tuple(item.name for item in verification.scenarios)
    if tuple(item.name for item in scenarios) != expected_names:
        raise CapabilityScenarioBindingError("Executable scenarios 必须按规格顺序完整覆盖。")
    error_codes = {item.code for item in interface.errors}
    for scenario in scenarios:
        _validate_instance(
            scenario.arguments,
            interface.parameters_schema,
            field=f"{scenario.name}.arguments",
        )
        if scenario.expectation.kind == "result":
            _validate_instance(
                scenario.expectation.value,
                interface.result_schema,
                field=f"{scenario.name}.expectation.value",
            )
        elif scenario.expectation.error_code not in error_codes:
            raise CapabilityScenarioBindingError(
                f"{scenario.name} 的 error_code 未在接口错误契约声明。"
            )
    payload = {
        "schema_version": 1,
        "policy_version": _POLICY_VERSION,
        "candidate_id": specification.candidate_id,
        "specification_id": specification.specification_id,
        "specification_sha256": specification.digest(),
        "artifact_id": artifact.artifact_id,
        "artifact_sha256": artifact.artifact_sha256,
        "permission_specification_sha256": artifact.permission_specification_sha256,
        "verification_specification_sha256": artifact.verification_specification_sha256,
        "source_interaction_id": interaction.interaction_id,
        "source_interaction_sequence": interaction.sequence,
        "source_interaction_sha256": interaction.digest(),
        "answered_by": "user",
        "scenarios": [item.model_dump(mode="json") for item in scenarios],
        "schema_dialect": "https://json-schema.org/draft/2020-12",
        "sandbox_execution_eligible": True,
        "sandbox_execution_authorized": False,
        "registration_authorized": False,
        "shadow_authorized": False,
        "executable": False,
        "created_at": interaction.answered_at,
    }
    digest = _digest(payload)
    return EvolutionCapabilityScenarioBinding(
        **payload,
        binding_id=f"evcsb_{digest[:24]}",
        binding_sha256=digest,
    )


def render_capability_scenario_binding(view: CapabilityScenarioBindingView) -> str:
    lines = [
        "# Capability 可执行场景绑定",
        "",
        f"- Artifact：`{view.artifact_id}`",
        f"- 状态：`{view.state}`",
        f"- Artifact 当前：{'是' if view.artifact_current else '否'}",
        f"- Sandbox 执行资格：{'是' if view.sandbox_execution_eligible else '否'}",
        "- Sandbox 执行授权：否 · Registry：否 · Shadow：否 · 可执行：否",
    ]
    if view.binding is not None:
        lines.extend([
            f"- Binding：`{view.binding.binding_id}`",
            f"- 人工来源：`{view.binding.source_interaction_id}`",
            "",
            "## 场景",
            "",
        ])
        lines.extend(
            f"- `{item.name}` · {item.expectation.kind} · {item.timeout_ms}ms"
            for item in view.binding.scenarios
        )
    elif view.pending_interaction_id:
        lines.append(f"- 待回答交互：`{view.pending_interaction_id}`")
    else:
        lines.append("- 尚未提交机械可执行的参数与预期 JSON。")
    lines.extend([
        "",
        "> 本阶段只绑定测试输入与 oracle，不运行候选代码，也不注册 Tool。",
    ])
    return "\n".join(lines)


def _interaction_request(
    specification: EvolutionCapabilitySpecification,
) -> UserInteractionRequest:
    verification = specification.verification
    interface = specification.interface
    if verification is None or interface is None:
        raise CapabilityScenarioBindingError("Specification 缺少验证或接口契约。")
    names = "、".join(item.name for item in verification.scenarios)
    example = (
        '{"scenarios":[{"name":"场景名","arguments":{},"expectation":'
        '{"kind":"result","value":{}},"timeout_ms":1000}]}'
    )
    return UserInteractionRequest(
        header="能力场景绑定",
        question=(
            f"请按顺序完整绑定场景：{names}。arguments 必须满足参数 schema；"
            f"expectation 使用 result/value 或 error/error_code。示例：{example}"
        ),
        options=(
            UserInteractionOption(
                value="defer",
                label="稍后绑定",
                description="保持未绑定，不获得 Sandbox 执行资格。",
            ),
            UserInteractionOption(
                value="inspect",
                label="仅查看",
                description="不提交测试输入，返回当前状态。",
            ),
        ),
        allow_custom=True,
        custom_label="粘贴场景 JSON",
        timeout_seconds=None,
        priority="normal",
    )


def _validate_instance(value: Any, schema: Mapping[str, Any], *, field: str) -> None:
    try:
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        error = next(iter(validator.iter_errors(value)), None)
    except SchemaError as exc:
        raise CapabilityScenarioBindingError(f"{field} 对应 JSON Schema 无效。") from exc
    except ValidationError as exc:
        raise CapabilityScenarioBindingError(f"{field} JSON Schema 校验失败。") from exc
    if error is not None:
        location = "/".join(str(item) for item in error.absolute_path)
        suffix = f"（{location}）" if location else ""
        raise CapabilityScenarioBindingError(
            f"{field} 不满足 JSON Schema{suffix}：{error.message[:240]}"
        )


def _ready_artifact(
    view: CapabilityArtifactView,
) -> EvolutionCapabilityImplementationArtifact:
    if view.state != "preview_ready" or view.artifact is None:
        raise CapabilityScenarioBindingError("当前没有有效 preview_ready Artifact。")
    return view.artifact


def _view(
    *,
    candidate_id: str,
    artifact: EvolutionCapabilityImplementationArtifact,
    binding: EvolutionCapabilityScenarioBinding | None,
    pending: HarnessInteractionRecord | None,
    artifact_current: bool,
    interaction_current: bool,
) -> CapabilityScenarioBindingView:
    binding_current = bool(
        binding is not None
        and artifact_current
        and interaction_current
        and binding.artifact_id == artifact.artifact_id
        and binding.artifact_sha256 == artifact.artifact_sha256
        and binding.specification_id == artifact.specification_id
        and binding.specification_sha256 == artifact.specification_sha256
        and binding.permission_specification_sha256
        == artifact.permission_specification_sha256
        and binding.verification_specification_sha256
        == artifact.verification_specification_sha256
    )
    if binding is not None:
        state = "ready" if binding_current else "revoked"
    elif pending is not None:
        state = "awaiting_input"
    else:
        state = "missing"
    return CapabilityScenarioBindingView(
        candidate_id=candidate_id,
        artifact_id=artifact.artifact_id,
        binding=binding,
        state=state,
        pending_interaction_id=pending.interaction_id if pending is not None else "",
        artifact_current=artifact_current,
        binding_current=binding_current,
        sandbox_execution_eligible=state == "ready",
    )


def _interaction_matches(interaction_id: str, artifact_id: str) -> bool:
    return _interaction_attempt(interaction_id, artifact_id) is not None


def _interaction_attempt(interaction_id: str, artifact_id: str) -> int | None:
    match = _INTERACTION_RE.fullmatch(interaction_id)
    if match is None or match.group(1) != artifact_id[6:]:
        return None
    attempt = int(match.group(2))
    return attempt if 1 <= attempt <= 100 else None


def _parse_scenarios(custom_text: str) -> tuple[CapabilityExecutableScenario, ...]:
    try:
        raw = json.loads(custom_text)
    except json.JSONDecodeError as exc:
        raise CapabilityScenarioBindingError("Scenario Binding 必须是有效 JSON。") from exc
    if not isinstance(raw, Mapping) or set(raw) != {"scenarios"}:
        raise CapabilityScenarioBindingError("Scenario Binding 根对象只能包含 scenarios。")
    values = raw.get("scenarios")
    if not isinstance(values, list):
        raise CapabilityScenarioBindingError("Scenario Binding scenarios 必须是数组。")
    try:
        return tuple(CapabilityExecutableScenario.model_validate(item) for item in values)
    except (TypeError, ValueError) as exc:
        raise CapabilityScenarioBindingError("Executable scenario 字段无效。") from exc


def _bounded_json(value: Any, *, field: str, maximum: int) -> str:
    try:
        encoded = _canonical(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是有限 JSON。") from exc
    if len(encoded) > maximum or _SECRET_RE.search(encoded):
        raise ValueError(f"{field} 过大或疑似包含 secret。")
    return encoded


def _safe_text(value: Any, *, field: str, maximum: int) -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > maximum
        or any(ord(char) < 32 and char not in "\t\n\r" for char in text)
        or _SECRET_RE.search(text)
    ):
        raise ValueError(f"{field} 为空、过长、含控制字符或疑似 secret。")
    return text


def _workspace(value: str | Path) -> str:
    return str(Path(value).expanduser().resolve(strict=True))


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _restore(payload: str, digest: str) -> EvolutionCapabilityScenarioBinding:
    if not hmac.compare_digest(hashlib.sha256(payload.encode()).hexdigest(), digest):
        raise CapabilityScenarioBindingError("Scenario Binding 持久摘要不一致。")
    try:
        return EvolutionCapabilityScenarioBinding.model_validate_json(payload)
    except (TypeError, ValueError) as exc:
        raise CapabilityScenarioBindingError("Scenario Binding JSON 损坏。") from exc


_SCHEMA = """
CREATE TABLE IF NOT EXISTS evolution_capability_scenario_bindings (
    workspace_root TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    source_interaction_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_root, artifact_id),
    UNIQUE (workspace_root, binding_id),
    UNIQUE (workspace_root, source_interaction_id)
);
"""


__all__ = [
    "CapabilityExecutableScenario",
    "CapabilityScenarioBindingError",
    "CapabilityScenarioBindingView",
    "CapabilityScenarioExpectation",
    "EvolutionCapabilityScenarioBinding",
    "EvolutionCapabilityScenarioBindingService",
    "EvolutionCapabilityScenarioBindingStore",
    "build_capability_scenario_binding",
    "render_capability_scenario_binding",
]
