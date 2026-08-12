"""Sealed, non-executable descriptors for offline Capability shadow routing."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.capability_artifact import (
    EvolutionCapabilityArtifactService,
)
from naumi_agent.evolution.capability_registry_leases import (
    CapabilityRegistryLeaseView,
    EvolutionCapabilityRegistryLeaseService,
)
from naumi_agent.evolution.capability_specification import (
    EvolutionCapabilitySpecificationService,
)

_POLICY_VERSION = "evolution-capability-shadow-descriptor-v1"
_SAFE_TOOL_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_-]+")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ABSOLUTE_TEXT_PATH_RE = re.compile(
    r"(?:^|[\s`(\"':=,\[])"
    r"(?:/(?:Users|home|tmp|var)/[^\s\"']+|[A-Za-z]:[\\/][^\s\"']+)",
)
_PERMISSION_FAMILIES = frozenset({
    "workspace_read",
    "workspace_write",
    "process",
    "network",
    "browser",
    "secrets",
})
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


class EvolutionCapabilityShadowDescriptor(_StrictModel):
    """Public routing semantics only; never an executable Tool registration."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-capability-shadow-descriptor-v1"] = (
        _POLICY_VERSION
    )
    descriptor_id: str = Field(pattern=r"^evcsd_[0-9a-f]{24}$")
    descriptor_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lease_id: str = Field(pattern=r"^evcrl_[0-9a-f]{24}$")
    lease_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lease_expires_at: str = Field(min_length=20, max_length=64)
    artifact_id: str = Field(pattern=r"^evcia_[0-9a-f]{24}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    specification_id: str = Field(pattern=r"^evcs_[0-9a-f]{24}$")
    specification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    declared_tool_name: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    evaluation_tool_name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    routing_description: str = Field(min_length=1, max_length=2_000)
    parameters_schema: dict[str, Any]
    parameters_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    error_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    permission_families: tuple[str, ...] = Field(max_length=6)
    scenario_names: tuple[str, ...] = Field(min_length=1, max_length=8)
    evaluation_mode: Literal["counterfactual_recommendation_only"] = (
        "counterfactual_recommendation_only"
    )
    offline_shadow_input_authorized: Literal[False] = False
    production_model_visible: Literal[False] = False
    registry_resolvable: Literal[False] = False
    side_effects_allowed: Literal[False] = False
    execution_authorized: Literal[False] = False
    activation_authorized: Literal[False] = False
    created_at: str = Field(min_length=20, max_length=64)

    @field_validator("routing_description")
    @classmethod
    def _routing_description_is_safe(cls, value: str) -> str:
        normalized = value.strip()
        if (
            normalized != value
            or any(ord(character) < 32 and character not in {"\n", "\t"} for character in value)
            or _SECRET_RE.search(value)
            or _ABSOLUTE_TEXT_PATH_RE.search(value)
        ):
            raise ValueError("Shadow routing description 必须已脱敏且不含控制字符。")
        return value

    @model_validator(mode="after")
    def _descriptor_is_exact(self) -> EvolutionCapabilityShadowDescriptor:
        created = _aware(self.created_at, field="created_at")
        expires = _aware(self.lease_expires_at, field="lease_expires_at")
        if created >= expires:
            raise ValueError("Shadow descriptor 必须在 Registry lease 到期前形成。")
        if _digest(self.parameters_schema) != self.parameters_schema_sha256:
            raise ValueError("Shadow parameters schema 摘要不一致。")
        encoded_schema = _canonical(self.parameters_schema)
        if (
            self.parameters_schema.get("type") != "object"
            or len(encoded_schema) > 12_000
            or _SECRET_RE.search(encoded_schema)
        ):
            raise ValueError("Shadow parameters schema 必须是有界、脱敏的 object schema。")
        if (
            len(set(self.error_codes)) != len(self.error_codes)
            or any(_ERROR_CODE_RE.fullmatch(code) is None for code in self.error_codes)
        ):
            raise ValueError("Shadow error codes 必须唯一且格式有效。")
        if (
            len(set(self.permission_families)) != len(self.permission_families)
            or not set(self.permission_families).issubset(_PERMISSION_FAMILIES)
        ):
            raise ValueError("Shadow permission families 必须唯一且属于受支持集合。")
        if (
            len(set(self.scenario_names)) != len(self.scenario_names)
            or any(
                not name.strip()
                or name != name.strip()
                or len(name) > 80
                or any(ord(character) < 32 for character in name)
                for name in self.scenario_names
            )
        ):
            raise ValueError("Shadow scenario names 必须唯一、安全且不超过 80 字符。")
        if self.evaluation_tool_name != _evaluation_tool_name(
            self.candidate_id,
            self.declared_tool_name,
        ):
            raise ValueError("Shadow evaluation tool name 与来源不一致。")
        payload = self.model_dump(
            mode="json",
            exclude={"descriptor_id", "descriptor_sha256"},
        )
        digest = _digest(payload)
        if not hmac.compare_digest(self.descriptor_sha256, digest):
            raise ValueError("Shadow descriptor 摘要不一致。")
        if self.descriptor_id != f"evcsd_{digest[:24]}":
            raise ValueError("Shadow descriptor identity 不一致。")
        return self

    def canonical_json(self) -> str:
        return _canonical(self.model_dump(mode="json"))

class CapabilityShadowDescriptorView(_StrictModel):
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    descriptor: EvolutionCapabilityShadowDescriptor | None
    state: Literal[
        "missing",
        "ready",
        "detached",
        "released",
        "revoked",
        "expired",
        "source_revoked",
    ]
    lease_state: str = Field(max_length=32)
    source_current: bool
    offline_shadow_input_eligible: bool
    production_model_visible: Literal[False] = False
    registry_resolvable: Literal[False] = False
    side_effects_allowed: Literal[False] = False
    execution_authorized: Literal[False] = False
    activation_authorized: Literal[False] = False

    @model_validator(mode="after")
    def _authority_matches_state(self) -> CapabilityShadowDescriptorView:
        expected = self.state == "ready"
        if self.offline_shadow_input_eligible != expected:
            raise ValueError("Shadow input eligibility 与动态状态不一致。")
        if self.descriptor is None and self.state != "missing":
            raise ValueError("无 Shadow descriptor 时状态只能是 missing。")
        return self


class CapabilityShadowDescriptorError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionCapabilityShadowDescriptorStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def latest(
        self,
        workspace_root: str | Path,
        candidate_id: str,
    ) -> EvolutionCapabilityShadowDescriptor | None:
        workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        if not self._db_path.is_file():
            return None
        await self._ensure_schema()
        try:
            async with aiosqlite.connect(self._db_path) as db:
                row = await (
                    await db.execute(
                        "SELECT payload_json, payload_sha256 "
                        "FROM evolution_capability_shadow_descriptors "
                        "WHERE workspace_root = ? AND candidate_id = ? "
                        "ORDER BY rowid DESC LIMIT 1",
                        (workspace, candidate_id),
                    )
                ).fetchone()
        except (aiosqlite.Error, OSError) as exc:
            raise CapabilityShadowDescriptorError(
                "shadow_descriptor_store_read_failed",
                "无法读取 Capability Shadow descriptor。",
            ) from exc
        return None if row is None else _restore(row)

    async def record(
        self,
        workspace_root: str | Path,
        descriptor: EvolutionCapabilityShadowDescriptor,
    ) -> EvolutionCapabilityShadowDescriptor:
        workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        payload = descriptor.canonical_json()
        payload_sha256 = hashlib.sha256(payload.encode()).hexdigest()
        await self._ensure_schema()
        try:
            async with self._write_lock, aiosqlite.connect(self._db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                existing = await (
                    await db.execute(
                        "SELECT payload_json, payload_sha256 "
                        "FROM evolution_capability_shadow_descriptors "
                        "WHERE workspace_root = ? AND lease_id = ?",
                        (workspace, descriptor.lease_id),
                    )
                ).fetchone()
                if existing is not None:
                    restored = _restore(existing)
                    await db.rollback()
                    if restored.descriptor_sha256 != descriptor.descriptor_sha256:
                        raise CapabilityShadowDescriptorError(
                            "shadow_descriptor_lease_conflict",
                            "Registry lease 已绑定不同 Shadow descriptor，拒绝覆盖。",
                        )
                    return restored
                await db.execute(
                    "INSERT INTO evolution_capability_shadow_descriptors "
                    "(workspace_root, descriptor_id, candidate_id, lease_id, "
                    "payload_json, payload_sha256, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        workspace,
                        descriptor.descriptor_id,
                        descriptor.candidate_id,
                        descriptor.lease_id,
                        payload,
                        payload_sha256,
                        descriptor.created_at,
                    ),
                )
                await db.commit()
        except CapabilityShadowDescriptorError:
            raise
        except (aiosqlite.Error, OSError) as exc:
            raise CapabilityShadowDescriptorError(
                "shadow_descriptor_store_write_failed",
                "无法保存 Capability Shadow descriptor。",
            ) from exc
        return descriptor

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
                raise CapabilityShadowDescriptorError(
                    "shadow_descriptor_store_init_failed",
                    "无法初始化 Capability Shadow descriptor Store。",
                ) from exc
            self._schema_ready = True


class EvolutionCapabilityShadowDescriptorService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        review_service: Any,
        specification_service: EvolutionCapabilitySpecificationService,
        artifact_service: EvolutionCapabilityArtifactService,
        registry_lease_service: EvolutionCapabilityRegistryLeaseService,
        store: EvolutionCapabilityShadowDescriptorStore,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.review_service = review_service
        self.specification_service = specification_service
        self.artifact_service = artifact_service
        self.registry_lease_service = registry_lease_service
        self.store = store
        self.now = now or (lambda: datetime.now(UTC).isoformat())
        self._lock = asyncio.Lock()

    async def inspect(self, candidate_id: str) -> CapabilityShadowDescriptorView:
        candidate = candidate_id.strip()
        async with self._lock:
            descriptor = await self.store.latest(self.workspace_root, candidate)
            if descriptor is None:
                return _view(candidate, None, state="missing", lease_state="missing")
            return await self._evaluate(descriptor)

    async def compile(self, candidate_id: str) -> CapabilityShadowDescriptorView:
        candidate = candidate_id.strip()
        async with self._lock:
            lease_view = await self.registry_lease_service.inspect(candidate)
            if not _lease_is_locally_active(lease_view):
                raise self._error(
                    "lease_not_active",
                    "Shadow descriptor 需要当前 Runtime 持有 active Registry lease。",
                )
            assert lease_view.lease is not None
            sources = await self._current_sources(candidate)
            if sources is None:
                raise self._error(
                    "source_not_current",
                    "Shadow descriptor 需要 current Candidate、Specification 与 Artifact。",
                )
            proposal, specification, artifact, hypothesis = sources
            lease = lease_view.lease
            if (
                artifact.artifact_id != lease.artifact_id
                or not hmac.compare_digest(artifact.artifact_sha256, lease.artifact_sha256)
                or artifact.specification_id != specification.specification_id
                or not hmac.compare_digest(
                    artifact.specification_sha256,
                    specification.digest(),
                )
            ):
                raise self._error(
                    "lease_source_mismatch",
                    "Registry lease 与当前 Specification/Artifact 不一致。",
                )
            created_at = self._now()
            descriptor = _descriptor(
                lease=lease,
                proposal=proposal,
                specification=specification,
                artifact=artifact,
                hypothesis=hypothesis,
                created_at=created_at,
            )
            stored = await self.store.record(self.workspace_root, descriptor)
            return await self._evaluate(stored, lease_view=lease_view)

    async def _evaluate(
        self,
        descriptor: EvolutionCapabilityShadowDescriptor,
        *,
        lease_view: CapabilityRegistryLeaseView | None = None,
    ) -> CapabilityShadowDescriptorView:
        current_lease = lease_view or await self.registry_lease_service.inspect(
            descriptor.candidate_id
        )
        if current_lease.state != "active":
            state = (
                current_lease.state
                if current_lease.state in {
                    "detached",
                    "released",
                    "revoked",
                    "expired",
                }
                else "source_revoked"
            )
            return _view(
                descriptor.candidate_id,
                descriptor,
                state=state,
                lease_state=current_lease.state,
            )
        lease = current_lease.lease
        if (
            lease is None
            or lease.lease_id != descriptor.lease_id
            or not hmac.compare_digest(lease.lease_sha256, descriptor.lease_sha256)
        ):
            return _view(
                descriptor.candidate_id,
                descriptor,
                state="source_revoked",
                lease_state=current_lease.state,
            )
        sources = await self._current_sources(descriptor.candidate_id)
        if sources is None:
            return _view(
                descriptor.candidate_id,
                descriptor,
                state="source_revoked",
                lease_state=current_lease.state,
            )
        proposal, specification, artifact, hypothesis = sources
        current = (
            proposal.source.candidate_revision == descriptor.candidate_revision
            and hmac.compare_digest(
                proposal.source.candidate_sha256,
                descriptor.candidate_sha256,
            )
            and specification.specification_id == descriptor.specification_id
            and hmac.compare_digest(
                specification.digest(),
                descriptor.specification_sha256,
            )
            and artifact.artifact_id == descriptor.artifact_id
            and hmac.compare_digest(
                artifact.artifact_sha256,
                descriptor.artifact_sha256,
            )
            and hypothesis == descriptor.routing_description
        )
        return _view(
            descriptor.candidate_id,
            descriptor,
            state="ready" if current else "source_revoked",
            lease_state=current_lease.state,
            source_current=current,
        )

    async def _current_sources(self, candidate_id: str) -> tuple[Any, Any, Any, str] | None:
        try:
            snapshot = await self.review_service.detail_snapshot(
                self.workspace_root,
                candidate_id,
                include_capability_extensions=False,
            )
            selected = snapshot.selected
            proposal = selected.capability_proposal if selected is not None else None
            hypothesis = selected.hypothesis if selected is not None else ""
            specification_view = await self.specification_service.inspect(
                self.workspace_root,
                candidate_id,
            )
            artifact_view = await self.artifact_service.inspect(
                self.workspace_root,
                candidate_id,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        specification = specification_view.specification
        artifact = artifact_view.artifact
        if (
            proposal is None
            or not hypothesis
            or specification is None
            or specification_view.state != "complete"
            or specification.state != "complete"
            or specification.interface is None
            or specification.permissions is None
            or specification.verification is None
            or artifact is None
            or artifact_view.state != "preview_ready"
        ):
            return None
        return proposal, specification, artifact, hypothesis

    def _now(self) -> str:
        return _aware(self.now(), field="clock").astimezone(UTC).isoformat()

    @staticmethod
    def _error(code: str, message: str) -> CapabilityShadowDescriptorError:
        return CapabilityShadowDescriptorError(f"capability_shadow_{code}", message)


def _descriptor(
    *,
    lease: Any,
    proposal: Any,
    specification: Any,
    artifact: Any,
    hypothesis: str,
    created_at: str,
) -> EvolutionCapabilityShadowDescriptor:
    interface = specification.interface
    permissions = specification.permissions
    verification = specification.verification
    assert interface is not None and permissions is not None and verification is not None
    payload = {
        "schema_version": 1,
        "policy_version": _POLICY_VERSION,
        "candidate_id": lease.candidate_id,
        "candidate_revision": proposal.source.candidate_revision,
        "candidate_sha256": proposal.source.candidate_sha256,
        "lease_id": lease.lease_id,
        "lease_sha256": lease.lease_sha256,
        "lease_expires_at": lease.expires_at,
        "artifact_id": artifact.artifact_id,
        "artifact_sha256": artifact.artifact_sha256,
        "specification_id": specification.specification_id,
        "specification_sha256": specification.digest(),
        "declared_tool_name": interface.tool_name,
        "evaluation_tool_name": _evaluation_tool_name(
            lease.candidate_id,
            interface.tool_name,
        ),
        "routing_description": hypothesis,
        "parameters_schema": interface.parameters_schema,
        "parameters_schema_sha256": _digest(interface.parameters_schema),
        "result_schema_sha256": _digest(interface.result_schema),
        "error_codes": tuple(item.code for item in interface.errors),
        "permission_families": tuple(item.family for item in permissions.requirements),
        "scenario_names": tuple(item.name for item in verification.scenarios),
        "evaluation_mode": "counterfactual_recommendation_only",
        "offline_shadow_input_authorized": False,
        "production_model_visible": False,
        "registry_resolvable": False,
        "side_effects_allowed": False,
        "execution_authorized": False,
        "activation_authorized": False,
        "created_at": created_at,
    }
    digest = _digest(payload)
    return EvolutionCapabilityShadowDescriptor.model_validate({
        **payload,
        "descriptor_id": f"evcsd_{digest[:24]}",
        "descriptor_sha256": digest,
    })


def _evaluation_tool_name(candidate_id: str, declared_name: str) -> str:
    suffix = _SAFE_TOOL_COMPONENT_RE.sub("_", declared_name).strip("_") or "capability"
    prefix = f"shadow_{candidate_id.removeprefix('evc_')}_"
    return f"{prefix}{suffix[:64 - len(prefix)]}"


def _lease_is_locally_active(view: CapabilityRegistryLeaseView) -> bool:
    return (
        view.state == "active"
        and view.lease is not None
        and view.locally_reserved
        and not view.model_visible
        and not view.shadow_authorized
        and not view.executable
    )


def _view(
    candidate_id: str,
    descriptor: EvolutionCapabilityShadowDescriptor | None,
    *,
    state: str,
    lease_state: str,
    source_current: bool = False,
) -> CapabilityShadowDescriptorView:
    return CapabilityShadowDescriptorView(
        candidate_id=candidate_id,
        descriptor=descriptor,
        state=state,
        lease_state=lease_state,
        source_current=source_current or state == "ready",
        offline_shadow_input_eligible=state == "ready",
    )


def render_capability_shadow_descriptor(view: CapabilityShadowDescriptorView) -> str:
    lines = ["# Capability Shadow Descriptor", ""]
    if view.descriptor is None:
        lines.extend([
            "尚未形成 Shadow descriptor。",
            "",
            "- 离线 Shadow 输入：否 · 生产模型可见：否 · 可解析：否 · 可执行：否",
        ])
        return "\n".join(lines)
    descriptor = view.descriptor
    lines.extend([
        f"- Descriptor：`{descriptor.descriptor_id}`",
        f"- 状态：`{view.state}` · Registry lease：`{view.lease_state}`",
        f"- 声明名称：`{descriptor.declared_tool_name}`",
        f"- 评价名称：`{descriptor.evaluation_tool_name}`",
        f"- 参数 Schema：`{descriptor.parameters_schema_sha256}`",
        f"- 来源当前：{'是' if view.source_current else '否'}",
        (
            "- 离线 Shadow 输入：是 · 生产模型可见：否 · 可解析：否 · 可执行：否"
            if view.offline_shadow_input_eligible
            else "- 离线 Shadow 输入：否 · 生产模型可见：否 · 可解析：否 · 可执行：否"
        ),
        "- 说明：本阶段只冻结反事实路由输入，尚未调用模型或记录推荐结果。",
    ])
    return "\n".join(lines)


def _restore(row: Any) -> EvolutionCapabilityShadowDescriptor:
    payload, stored_digest = map(str, row)
    if not hmac.compare_digest(
        hashlib.sha256(payload.encode()).hexdigest(),
        stored_digest,
    ):
        raise CapabilityShadowDescriptorError(
            "shadow_descriptor_store_tampered",
            "Capability Shadow descriptor 持久摘要不一致。",
        )
    try:
        return EvolutionCapabilityShadowDescriptor.model_validate_json(payload)
    except (TypeError, ValueError) as exc:
        raise CapabilityShadowDescriptorError(
            "shadow_descriptor_store_invalid",
            "Capability Shadow descriptor 持久内容损坏。",
        ) from exc


def _aware(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 ISO-8601 时间。") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区。")
    return parsed


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS evolution_capability_shadow_descriptors (
    workspace_root TEXT NOT NULL,
    descriptor_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_root, descriptor_id),
    UNIQUE (workspace_root, lease_id)
);
CREATE INDEX IF NOT EXISTS evolution_capability_shadow_descriptor_candidate
ON evolution_capability_shadow_descriptors(workspace_root, candidate_id, created_at);
"""


__all__ = [
    "CapabilityShadowDescriptorError",
    "CapabilityShadowDescriptorView",
    "EvolutionCapabilityShadowDescriptor",
    "EvolutionCapabilityShadowDescriptorService",
    "EvolutionCapabilityShadowDescriptorStore",
    "render_capability_shadow_descriptor",
]
