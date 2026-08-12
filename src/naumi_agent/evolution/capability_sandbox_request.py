"""Immutable ARC-04 execution requests for sealed Capability scenarios."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import re
import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.capability_artifact import (
    EvolutionCapabilityImplementationArtifact,
)
from naumi_agent.evolution.capability_scenario_binding import (
    CapabilityScenarioBindingView,
    EvolutionCapabilityScenarioBinding,
    EvolutionCapabilityScenarioBindingService,
)
from naumi_agent.evolution.capability_specification import (
    EvolutionCapabilitySpecification,
    EvolutionCapabilitySpecificationStore,
)
from naumi_agent.harness.sandbox_request import (
    HarnessSandboxEvalRequestError,
    capture_clean_revision,
)

_POLICY_VERSION = "evolution-capability-sandbox-request-v1"
_DRIVER_POLICY_VERSION = "evolution-capability-driver-v1"
_MAX_OVERLAY_BYTES = 512 * 1024
CapabilitySandboxOverlayKind = Literal[
    "candidate",
    "driver",
    "permission_manifest",
    "scenario_input",
]
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


class CapabilitySandboxPermissionRequirement(_StrictModel):
    family: Literal[
        "workspace_read",
        "workspace_write",
        "process",
        "network",
        "browser",
        "secrets",
    ]
    scopes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _scopes_are_canonical(self) -> CapabilitySandboxPermissionRequirement:
        if self.scopes != tuple(sorted(set(self.scopes))):
            raise ValueError("Sandbox permission scopes 必须去重排序。")
        return self


class CapabilitySandboxRequestOverlay(_StrictModel):
    order: int = Field(ge=1, le=10)
    kind: Literal["candidate", "driver", "permission_manifest", "scenario_input"]
    path: str = Field(min_length=1, max_length=512)
    content_utf8: str = Field(min_length=1, max_length=_MAX_OVERLAY_BYTES, repr=False)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executable: Literal[False] = False

    @model_validator(mode="after")
    def _overlay_is_exact(self) -> CapabilitySandboxRequestOverlay:
        pure = PurePosixPath(self.path)
        if (
            pure.is_absolute()
            or pure.as_posix() != self.path
            or ".." in pure.parts
            or not self.path.startswith(".naumi/evolution-sandbox/")
        ):
            raise ValueError("Capability Sandbox overlay path 无效。")
        encoded = self.content_utf8.encode("utf-8")
        if len(encoded) > _MAX_OVERLAY_BYTES:
            raise ValueError("Capability Sandbox 单个 overlay 超过 512 KiB。")
        if hashlib.sha256(encoded).hexdigest() != self.sha256:
            raise ValueError("Capability Sandbox overlay 摘要不一致。")
        return self


class CapabilitySandboxScenarioCheck(_StrictModel):
    order: int = Field(ge=1, le=8)
    check_id: str = Field(pattern=r"^capability_scenario_0[1-8]$")
    scenario_name: str = Field(min_length=1, max_length=80)
    argv: tuple[str, ...] = Field(min_length=6, max_length=6)
    timeout_ms: int = Field(ge=100, le=300_000)
    timeout_seconds: int = Field(ge=1, le=300)
    input_overlay_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expectation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _check_is_exact(self) -> CapabilitySandboxScenarioCheck:
        if self.check_id != f"capability_scenario_{self.order:02d}":
            raise ValueError("Capability Sandbox check id 与顺序不一致。")
        if self.timeout_seconds != math.ceil(self.timeout_ms / 1000):
            raise ValueError("Capability Sandbox check timeout 汇总不一致。")
        if any(not item or "\x00" in item for item in self.argv):
            raise ValueError("Capability Sandbox check argv 无效。")
        return self


class EvolutionCapabilitySandboxExecutionRequest(_StrictModel):
    """Content-addressed request; it is not an execution authorization."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-capability-sandbox-request-v1"] = (
        _POLICY_VERSION
    )
    request_id: str = Field(pattern=r"^evcsr_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    specification_id: str = Field(pattern=r"^evcs_[0-9a-f]{24}$")
    specification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(pattern=r"^evcia_[0-9a-f]{24}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_id: str = Field(pattern=r"^evcsb_[0-9a-f]{24}$")
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    permission_specification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    permissions: tuple[CapabilitySandboxPermissionRequirement, ...] = Field(
        max_length=6
    )
    source_revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    source_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    python_executable: str = Field(min_length=1, max_length=4096)
    driver_policy_version: Literal["evolution-capability-driver-v1"] = (
        _DRIVER_POLICY_VERSION
    )
    overlay_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    overlays: tuple[CapabilitySandboxRequestOverlay, ...] = Field(
        min_length=4,
        max_length=11,
        repr=False,
    )
    checks: tuple[CapabilitySandboxScenarioCheck, ...] = Field(
        min_length=1,
        max_length=8,
    )
    source_materialization: Literal[
        "arc04_ephemeral_git_revision_with_overlays"
    ] = "arc04_ephemeral_git_revision_with_overlays"
    isolation_runtime: Literal["arc04_shell_worker"] = "arc04_shell_worker"
    runtime_identity_required: Literal[True] = True
    network_access_authorized: Literal[False] = False
    dependency_installation: Literal[False] = False
    permission_observation_required: Literal[True] = True
    execution_authority_required: Literal[True] = True
    sandbox_execution_authorized: Literal[False] = False
    registration_authorized: Literal[False] = False
    shadow_authorized: Literal[False] = False
    executable: Literal[False] = False
    request_ready: Literal[True] = True
    created_at: str = Field(min_length=20, max_length=64)

    @model_validator(mode="after")
    def _identity_and_manifests_are_exact(
        self,
    ) -> EvolutionCapabilitySandboxExecutionRequest:
        if tuple(item.order for item in self.overlays) != tuple(
            range(1, len(self.overlays) + 1)
        ):
            raise ValueError("Capability Sandbox overlays 必须连续排序。")
        if tuple(item.order for item in self.checks) != tuple(
            range(1, len(self.checks) + 1)
        ):
            raise ValueError("Capability Sandbox checks 必须连续排序。")
        paths = tuple(item.path for item in self.overlays)
        if len(paths) != len(set(paths)):
            raise ValueError("Capability Sandbox overlay path 不得重复。")
        if tuple(item.family for item in self.permissions) != tuple(
            sorted(item.family for item in self.permissions)
        ):
            raise ValueError("Capability Sandbox permissions 必须按 family 排序。")
        overlay_digest = _digest([
            {
                "order": item.order,
                "kind": item.kind,
                "path": item.path,
                "sha256": item.sha256,
                "executable": item.executable,
            }
            for item in self.overlays
        ])
        if not hmac.compare_digest(self.overlay_source_sha256, overlay_digest):
            raise ValueError("Capability Sandbox overlay source 摘要不一致。")
        by_sha = {item.sha256: item for item in self.overlays}
        if any(item.input_overlay_sha256 not in by_sha for item in self.checks):
            raise ValueError("Capability Sandbox check 未绑定 input overlay。")
        payload = self.model_dump(
            mode="json",
            exclude={"request_id", "request_sha256"},
        )
        digest = _digest(payload)
        if not hmac.compare_digest(self.request_sha256, digest):
            raise ValueError("Capability Sandbox Request 摘要不一致。")
        if self.request_id != f"evcsr_{digest[:24]}":
            raise ValueError("Capability Sandbox Request identity 不一致。")
        return self

    def canonical_json(self) -> str:
        return _canonical(self.model_dump(mode="json"))


class CapabilitySandboxRequestView(_StrictModel):
    schema_version: Literal[1] = 1
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    request: EvolutionCapabilitySandboxExecutionRequest | None
    state: Literal["missing", "ready", "revoked"]
    binding_current: bool
    source_current: bool
    sandbox_execution_authorized: Literal[False] = False
    registration_authorized: Literal[False] = False
    shadow_authorized: Literal[False] = False
    executable: Literal[False] = False


class CapabilitySandboxRequestError(RuntimeError):
    pass


class EvolutionCapabilitySandboxRequestStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def latest(
        self,
        workspace_root: str | Path,
        binding_id: str,
    ) -> EvolutionCapabilitySandboxExecutionRequest | None:
        workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        if not self._db_path.is_file():
            return None
        await self._ensure_schema()
        try:
            async with aiosqlite.connect(self._db_path) as db:
                row = await (
                    await db.execute(
                        "SELECT payload_json, payload_sha256 FROM "
                        "evolution_capability_sandbox_requests "
                        "WHERE workspace_root = ? AND binding_id = ? "
                        "ORDER BY rowid DESC LIMIT 1",
                        (workspace, binding_id),
                    )
                ).fetchone()
        except (aiosqlite.Error, OSError) as exc:
            raise CapabilitySandboxRequestError(
                "无法读取 Capability Sandbox Request。"
            ) from exc
        return None if row is None else _restore_request(row[0], row[1])

    async def record(
        self,
        workspace_root: str | Path,
        request: EvolutionCapabilitySandboxExecutionRequest,
    ) -> EvolutionCapabilitySandboxExecutionRequest:
        workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        payload = request.canonical_json()
        payload_sha256 = hashlib.sha256(payload.encode()).hexdigest()
        await self._ensure_schema()
        try:
            async with self._write_lock, aiosqlite.connect(self._db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "INSERT OR IGNORE INTO evolution_capability_sandbox_requests "
                    "(workspace_root, binding_id, request_id, source_revision, "
                    "payload_json, payload_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        workspace,
                        request.binding_id,
                        request.request_id,
                        request.source_revision,
                        payload,
                        payload_sha256,
                        request.created_at,
                    ),
                )
                row = await (
                    await db.execute(
                        "SELECT payload_json, payload_sha256 FROM "
                        "evolution_capability_sandbox_requests "
                        "WHERE workspace_root = ? AND binding_id = ? "
                        "AND source_revision = ?",
                        (
                            workspace,
                            request.binding_id,
                            request.source_revision,
                        ),
                    )
                ).fetchone()
                await db.commit()
        except (aiosqlite.Error, OSError) as exc:
            raise CapabilitySandboxRequestError(
                "无法保存 Capability Sandbox Request。"
            ) from exc
        if row is None:
            raise CapabilitySandboxRequestError(
                "Capability Sandbox Request 未形成持久记录。"
            )
        return _restore_request(row[0], row[1])

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
                raise CapabilitySandboxRequestError(
                    "无法初始化 Capability Sandbox Request Store。"
                ) from exc
            self._schema_ready = True


class EvolutionCapabilitySandboxRequestService:
    def __init__(
        self,
        *,
        binding_service: EvolutionCapabilityScenarioBindingService,
        specification_store: EvolutionCapabilitySpecificationStore,
        store: EvolutionCapabilitySandboxRequestStore,
        now: Callable[[], str],
    ) -> None:
        self.binding_service = binding_service
        self.specification_store = specification_store
        self.store = store
        if not callable(now):
            raise TypeError("now 必须可调用。")
        self.now = now

    async def inspect(
        self,
        workspace_root: str | Path,
        candidate_id: str,
    ) -> CapabilitySandboxRequestView:
        workspace = Path(workspace_root).expanduser().resolve(strict=True)
        binding_view = await self.binding_service.inspect(workspace, candidate_id)
        binding = binding_view.binding
        if binding is None:
            return _request_view(
                candidate_id=str(candidate_id),
                request=None,
                binding_current=False,
                source_current=False,
            )
        request = await self.store.latest(workspace, binding.binding_id)
        source_current = await _request_source_current(workspace, request)
        return _request_view(
            candidate_id=binding.candidate_id,
            request=request,
            binding_current=binding_view.state == "ready",
            source_current=source_current,
        )

    async def prepare(
        self,
        workspace_root: str | Path,
        *,
        candidate_id: str,
    ) -> CapabilitySandboxRequestView:
        workspace = Path(workspace_root).expanduser().resolve(strict=True)
        binding_view = await self.binding_service.inspect(workspace, candidate_id)
        binding = _ready_binding(binding_view)
        existing = await self.store.latest(workspace, binding.binding_id)
        if await _request_source_current(workspace, existing):
            return _request_view(
                candidate_id=binding.candidate_id,
                request=existing,
                binding_current=True,
                source_current=True,
            )
        specification = await self.specification_store.latest(
            workspace,
            binding.specification_id,
        )
        if (
            specification is None
            or specification.digest() != binding.specification_sha256
            or specification.permissions is None
        ):
            raise CapabilitySandboxRequestError(
                "Capability Specification 或权限契约已失效。"
            )
        try:
            _, revision, tree_sha256 = await asyncio.to_thread(
                capture_clean_revision,
                workspace,
            )
        except HarnessSandboxEvalRequestError as exc:
            raise CapabilitySandboxRequestError(str(exc)) from exc
        artifact_view = await self.binding_service.artifact_service.inspect(
            workspace,
            candidate_id,
        )
        if artifact_view.state != "preview_ready" or artifact_view.artifact is None:
            raise CapabilitySandboxRequestError(
                "Capability Artifact 在 Request 编译前已失效。"
            )
        request = build_capability_sandbox_execution_request(
            specification=specification,
            binding_view=binding_view,
            artifact=artifact_view.artifact,
            source_revision=revision,
            source_tree_sha256=tree_sha256,
            python_executable=str(Path(sys.executable).resolve(strict=True)),
            created_at=self.now(),
        )
        refreshed = await self.binding_service.inspect(workspace, candidate_id)
        if (
            refreshed.state != "ready"
            or refreshed.binding is None
            or refreshed.binding.binding_id != binding.binding_id
            or refreshed.binding.binding_sha256 != binding.binding_sha256
        ):
            raise CapabilitySandboxRequestError(
                "Sandbox Request 编译期间 Binding 已失效。"
            )
        if not await _request_source_current(workspace, request):
            raise CapabilitySandboxRequestError(
                "Sandbox Request 编译期间 Git source 已漂移。"
            )
        stored = await self.store.record(workspace, request)
        return _request_view(
            candidate_id=binding.candidate_id,
            request=stored,
            binding_current=True,
            source_current=True,
        )


def build_capability_sandbox_execution_request(
    *,
    specification: EvolutionCapabilitySpecification,
    binding_view: CapabilityScenarioBindingView,
    artifact: EvolutionCapabilityImplementationArtifact | None,
    source_revision: str,
    source_tree_sha256: str,
    python_executable: str,
    created_at: str,
) -> EvolutionCapabilitySandboxExecutionRequest:
    binding = _ready_binding(binding_view)
    if artifact is None:
        raise CapabilitySandboxRequestError("Capability Artifact 不存在。")
    return _build_request_from_artifact(
        specification=specification,
        binding=binding,
        artifact=artifact,
        source_revision=source_revision,
        source_tree_sha256=source_tree_sha256,
        python_executable=python_executable,
        created_at=created_at,
    )


def _build_request_from_artifact(
    *,
    specification: EvolutionCapabilitySpecification,
    binding: EvolutionCapabilityScenarioBinding,
    artifact: EvolutionCapabilityImplementationArtifact,
    source_revision: str,
    source_tree_sha256: str,
    python_executable: str,
    created_at: str,
) -> EvolutionCapabilitySandboxExecutionRequest:
    if binding.artifact_id != artifact.artifact_id:
        raise CapabilitySandboxRequestError("Binding 与 Artifact identity 不一致。")
    permissions = tuple(
        CapabilitySandboxPermissionRequirement(
            family=item.family,
            scopes=tuple(sorted(item.scopes)),
        )
        for item in sorted(specification.permissions.requirements, key=lambda item: item.family)
    )
    root = f".naumi/evolution-sandbox/{artifact.artifact_id}"
    candidate_path = f"{root}/candidate.py"
    driver_path = f"{root}/driver.py"
    permission_path = f"{root}/permissions.json"
    permission_text = _canonical({
        "schema_version": 1,
        "requirements": [item.model_dump(mode="json") for item in permissions],
    })
    overlays = [
        _overlay(1, "candidate", candidate_path, artifact.source_text),
        _overlay(2, "driver", driver_path, _DRIVER_SOURCE),
        _overlay(3, "permission_manifest", permission_path, permission_text),
    ]
    checks: list[CapabilitySandboxScenarioCheck] = []
    for index, scenario in enumerate(binding.scenarios, start=1):
        input_path = f"{root}/scenario-{index:02d}.json"
        input_text = _canonical({
            "schema_version": 1,
            "scenario_index": index,
            "arguments": scenario.arguments,
            "timeout_ms": scenario.timeout_ms,
        })
        overlay = _overlay(index + 3, "scenario_input", input_path, input_text)
        overlays.append(overlay)
        checks.append(CapabilitySandboxScenarioCheck(
            order=index,
            check_id=f"capability_scenario_{index:02d}",
            scenario_name=scenario.name,
            argv=(
                python_executable,
                "-I",
                driver_path,
                input_path,
                candidate_path,
                artifact.class_name,
            ),
            timeout_ms=scenario.timeout_ms,
            timeout_seconds=math.ceil(scenario.timeout_ms / 1000),
            input_overlay_sha256=overlay.sha256,
            expectation_sha256=_digest(scenario.expectation.model_dump(mode="json")),
        ))
    overlay_models = tuple(overlays)
    overlay_source_sha256 = _digest([
        {
            "order": item.order,
            "kind": item.kind,
            "path": item.path,
            "sha256": item.sha256,
            "executable": item.executable,
        }
        for item in overlay_models
    ])
    payload = {
        "schema_version": 1,
        "policy_version": _POLICY_VERSION,
        "candidate_id": binding.candidate_id,
        "specification_id": binding.specification_id,
        "specification_sha256": binding.specification_sha256,
        "artifact_id": binding.artifact_id,
        "artifact_sha256": binding.artifact_sha256,
        "binding_id": binding.binding_id,
        "binding_sha256": binding.binding_sha256,
        "permission_specification_sha256": binding.permission_specification_sha256,
        "permissions": [item.model_dump(mode="json") for item in permissions],
        "source_revision": source_revision,
        "source_tree_sha256": source_tree_sha256,
        "python_executable": python_executable,
        "driver_policy_version": _DRIVER_POLICY_VERSION,
        "overlay_source_sha256": overlay_source_sha256,
        "overlays": [item.model_dump(mode="json") for item in overlay_models],
        "checks": [item.model_dump(mode="json") for item in checks],
        "source_materialization": "arc04_ephemeral_git_revision_with_overlays",
        "isolation_runtime": "arc04_shell_worker",
        "runtime_identity_required": True,
        "network_access_authorized": False,
        "dependency_installation": False,
        "permission_observation_required": True,
        "execution_authority_required": True,
        "sandbox_execution_authorized": False,
        "registration_authorized": False,
        "shadow_authorized": False,
        "executable": False,
        "request_ready": True,
        "created_at": created_at,
    }
    digest = _digest(payload)
    return EvolutionCapabilitySandboxExecutionRequest.model_validate({
        **payload,
        "request_id": f"evcsr_{digest[:24]}",
        "request_sha256": digest,
    })


def render_capability_sandbox_request(view: CapabilitySandboxRequestView) -> str:
    lines = ["# Capability Sandbox Execution Request", ""]
    if view.request is None:
        lines.extend([
            "尚未形成执行请求。",
            "",
            "- Sandbox 执行授权：否 · Registry：否 · Shadow：否 · 可执行：否",
        ])
        return "\n".join(lines)
    request = view.request
    lines.extend([
        f"- Request：`{request.request_id}`",
        f"- 状态：`{view.state}`",
        f"- Git revision：`{request.source_revision[:12]}`",
        f"- 场景：{len(request.checks)} · overlays：{len(request.overlays)}",
        f"- Binding 当前：{'是' if view.binding_current else '否'}",
        f"- Git source 当前：{'是' if view.source_current else '否'}",
        "- 隔离运行时：ARC-04 Shell Worker · 网络授权：否",
        "- Sandbox 执行授权：否 · Registry：否 · Shadow：否 · 可执行：否",
        "",
        "> Request 已封存执行输入，但尚未签发 Run Grant 或运行候选代码。",
    ])
    return "\n".join(lines)


def _overlay(
    order: int,
    kind: CapabilitySandboxOverlayKind,
    path: str,
    content: str,
) -> CapabilitySandboxRequestOverlay:
    if _SECRET_RE.search(content) and kind != "candidate":
        raise CapabilitySandboxRequestError("Sandbox overlay 疑似包含 secret。")
    return CapabilitySandboxRequestOverlay(
        order=order,
        kind=kind,
        path=path,
        content_utf8=content,
        sha256=hashlib.sha256(content.encode()).hexdigest(),
    )


def _ready_binding(
    view: CapabilityScenarioBindingView,
) -> EvolutionCapabilityScenarioBinding:
    if (
        view.state != "ready"
        or not view.sandbox_execution_eligible
        or view.binding is None
    ):
        raise CapabilitySandboxRequestError(
            "Capability Scenario Binding 尚未形成当前 Sandbox 执行资格。"
        )
    return view.binding


def _request_view(
    *,
    candidate_id: str,
    request: EvolutionCapabilitySandboxExecutionRequest | None,
    binding_current: bool,
    source_current: bool,
) -> CapabilitySandboxRequestView:
    state = "missing" if request is None else (
        "ready" if binding_current and source_current else "revoked"
    )
    return CapabilitySandboxRequestView(
        candidate_id=candidate_id,
        request=request,
        state=state,
        binding_current=binding_current,
        source_current=source_current,
    )


async def _request_source_current(
    workspace: Path,
    request: EvolutionCapabilitySandboxExecutionRequest | None,
) -> bool:
    if request is None:
        return False
    try:
        _, revision, tree_sha256 = await asyncio.to_thread(
            capture_clean_revision,
            workspace,
        )
    except (HarnessSandboxEvalRequestError, OSError, TypeError, ValueError):
        return False
    return hmac.compare_digest(revision, request.source_revision) and hmac.compare_digest(
        tree_sha256,
        request.source_tree_sha256,
    )


def _restore_request(
    payload: str,
    payload_sha256: str,
) -> EvolutionCapabilitySandboxExecutionRequest:
    if not hmac.compare_digest(hashlib.sha256(payload.encode()).hexdigest(), payload_sha256):
        raise CapabilitySandboxRequestError("Capability Sandbox Request 持久摘要不一致。")
    try:
        return EvolutionCapabilitySandboxExecutionRequest.model_validate_json(payload)
    except (TypeError, ValueError) as exc:
        raise CapabilitySandboxRequestError("Capability Sandbox Request JSON 损坏。") from exc


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


_DRIVER_SOURCE = r'''from __future__ import annotations

import asyncio
import fnmatch
import importlib.util
import json
import os
import sys
from pathlib import Path


class _PermissionObserver:
    def __init__(self, workspace: Path, requirements: list[dict[str, object]]):
        self.workspace = workspace.resolve()
        self.allowed = {
            str(item["family"]): tuple(str(scope) for scope in item["scopes"])
            for item in requirements
        }
        self.events: list[dict[str, str]] = []
        self.violations: list[dict[str, str]] = []
        self.runtime_roots = tuple({
            Path(sys.base_prefix).resolve(),
            Path(sys.prefix).resolve(),
        })

    def install(self) -> None:
        sys.addaudithook(self._audit)

    def _audit(self, event: str, args: tuple[object, ...]) -> None:
        if event == "open" and args:
            self._observe_open(args)
        elif event.startswith("subprocess.") or event in {
            "os.system", "os.exec", "os.posix_spawn", "os.spawn",
        }:
            executable = self._executable(args)
            self._record("process", executable, self._allowed("process", executable))
        elif event.startswith("socket."):
            self._record("network", "network_access", False)

    def _observe_open(self, args: tuple[object, ...]) -> None:
        raw_path = args[0]
        if isinstance(raw_path, int):
            return
        try:
            value = os.fsdecode(os.fspath(raw_path))
        except TypeError:
            self._record("workspace_read", "invalid_path", False)
            return
        mode = args[1] if len(args) > 1 else "r"
        flags = args[2] if len(args) > 2 else 0
        write = (
            isinstance(mode, str) and any(token in mode for token in "wax+")
        ) or (
            isinstance(flags, int)
            and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        )
        family = "workspace_write" if write else "workspace_read"
        absolute = Path(os.path.abspath(value))
        try:
            relative = absolute.relative_to(self.workspace).as_posix()
        except ValueError:
            if any(_is_relative_to(absolute, root) for root in self.runtime_roots):
                return
            self._record(family, "outside_workspace", False)
            return
        self._record(family, relative, self._allowed(family, relative))

    def _allowed(self, family: str, scope: str) -> bool:
        return any(
            fnmatch.fnmatchcase(scope, pattern)
            for pattern in self.allowed.get(family, ())
        )

    def _record(self, family: str, scope: str, allowed: bool) -> None:
        item = {"family": family, "scope": scope, "decision": "allow" if allowed else "deny"}
        target = self.events if allowed else self.violations
        if item not in target:
            if len(target) >= 128:
                overflow = {"family": "observation", "scope": "event_limit", "decision": "deny"}
                if overflow not in self.violations:
                    self.violations.append(overflow)
                raise PermissionError("candidate_permission_observation_limit")
            target.append(item)
        if not allowed:
            raise PermissionError("candidate_permission_denied")

    @staticmethod
    def _executable(args: tuple[object, ...]) -> str:
        if not args:
            return "unknown_process"
        raw = args[0]
        if isinstance(raw, (str, bytes, os.PathLike)):
            return Path(os.fsdecode(os.fspath(raw))).name or "unknown_process"
        return "unknown_process"

    def result(self) -> dict[str, object]:
        return {
            "events": self.events,
            "violations": self.violations,
            "complete": not any(
                item["scope"] == "event_limit" for item in self.violations
            ),
        }


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _load(candidate_path: Path, class_name: str):
    workspace = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(workspace / "src"))
    spec = importlib.util.spec_from_file_location("naumi_evolution_candidate", candidate_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("candidate_module_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, class_name)()


async def _run(input_path: Path, candidate_path: Path, class_name: str) -> dict[str, object]:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if set(payload) != {"schema_version", "scenario_index", "arguments", "timeout_ms"}:
        raise RuntimeError("scenario_input_invalid")
    if payload["schema_version"] != 1 or not isinstance(payload["arguments"], dict):
        raise RuntimeError("scenario_input_invalid")
    tool = _load(candidate_path, class_name)
    from naumi_agent.tools.base import Tool, ToolExecutionError

    if not isinstance(tool, Tool):
        raise RuntimeError("candidate_tool_invalid")
    permission_payload = json.loads(
        Path(__file__).with_name("permissions.json").read_text(encoding="utf-8")
    )
    if (
        set(permission_payload) != {"schema_version", "requirements"}
        or permission_payload["schema_version"] != 1
        or not isinstance(permission_payload["requirements"], list)
    ):
        raise RuntimeError("permission_manifest_invalid")
    observer = _PermissionObserver(
        Path(__file__).resolve().parents[3],
        permission_payload["requirements"],
    )
    observer.install()
    try:
        content = await asyncio.wait_for(
            tool.execute(**payload["arguments"]),
            timeout=payload["timeout_ms"] / 1000,
        )
    except ToolExecutionError as error:
        safe = ToolExecutionError(error.code, str(error), retryable=error.retryable)
        return {
            "kind": "error",
            "error_code": safe.code,
            "retryable": safe.retryable,
            "permission_observation": observer.result(),
        }
    except TimeoutError:
        return {"kind": "timeout", "permission_observation": observer.result()}
    except PermissionError:
        return {
            "kind": "permission_violation",
            "permission_observation": observer.result(),
        }
    except Exception:
        return {
            "kind": "infrastructure_error",
            "error_code": "candidate_execution_failed",
            "permission_observation": observer.result(),
        }
    observation = observer.result()
    if observation["violations"]:
        return {
            "kind": "permission_violation",
            "permission_observation": observation,
        }
    if not isinstance(content, str):
        raise RuntimeError("candidate_result_not_string")
    return {
        "kind": "result",
        "value": json.loads(content),
        "permission_observation": observation,
    }


def main() -> int:
    if len(sys.argv) != 4:
        return 64
    try:
        result = asyncio.run(_run(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]))
    except Exception:
        result = {
            "kind": "infrastructure_error",
            "error_code": "candidate_setup_failed",
        }
    print(json.dumps(
        result,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


_SCHEMA = """
CREATE TABLE IF NOT EXISTS evolution_capability_sandbox_requests (
    workspace_root TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_root, request_id)
);
CREATE INDEX IF NOT EXISTS idx_evolution_capability_sandbox_request_binding
ON evolution_capability_sandbox_requests (
    workspace_root, binding_id, created_at, request_id
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_evolution_capability_sandbox_request_authority
ON evolution_capability_sandbox_requests (
    workspace_root, binding_id, source_revision
);
"""


__all__ = [
    "CapabilitySandboxPermissionRequirement",
    "CapabilitySandboxRequestError",
    "CapabilitySandboxRequestOverlay",
    "CapabilitySandboxRequestView",
    "CapabilitySandboxScenarioCheck",
    "EvolutionCapabilitySandboxExecutionRequest",
    "EvolutionCapabilitySandboxRequestService",
    "EvolutionCapabilitySandboxRequestStore",
    "build_capability_sandbox_execution_request",
    "render_capability_sandbox_request",
]
