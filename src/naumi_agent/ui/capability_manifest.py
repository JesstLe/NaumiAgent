"""Strict product capability manifests for terminal frontend parity."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    model_validator,
)

FrontendSurface = Literal["new_ui", "tui"]
CapabilityState = Literal["supported", "degraded", "unsupported"]

CAPABILITY_MANIFEST_SCHEMA_VERSION = 1
REQUIRED_TERMINAL_CAPABILITIES = (
    "agent_task_control",
    "budget_context_status",
    "conversation_submit_streaming",
    "doctor_debug",
    "goal_pursuit",
    "harness_receipt_explain",
    "history_resume",
    "model_provider_identity",
    "permission_bypass",
    "queued_send_now",
    "run_cancel",
    "terminal_runtime_health",
    "tool_lifecycle",
    "user_interaction",
)

_PROTOCOL_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_EVIDENCE_PATH = re.compile(r"^(?:src|frontend|tests)/[A-Za-z0-9_./-]{1,240}$")


class CapabilityManifestError(ValueError):
    """A shipped frontend capability declaration is missing or invalid."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FrontendProtocolDeclaration(_StrictModel):
    name: str = Field(min_length=1, max_length=64)
    transport: Literal["jsonl", "in_process"]
    minimum_version: StrictInt = Field(ge=1)
    maximum_version: StrictInt = Field(ge=1)
    negotiated: StrictBool

    @model_validator(mode="after")
    def _validate_protocol(self) -> FrontendProtocolDeclaration:
        if not _PROTOCOL_NAME.fullmatch(self.name):
            raise ValueError("protocol name 必须是稳定的小写标识符。")
        if self.maximum_version < self.minimum_version:
            raise ValueError("maximum_version 不能小于 minimum_version。")
        if self.transport == "in_process" and self.negotiated:
            raise ValueError("in_process frontend 不得伪造协议协商。")
        return self


class FrontendCapabilityDeclaration(_StrictModel):
    state: CapabilityState
    evidence: tuple[str, ...] = Field(min_length=2, max_length=8)
    note: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def _validate_evidence(self) -> FrontendCapabilityDeclaration:
        if len(self.evidence) != len(set(self.evidence)):
            raise ValueError("capability evidence 不得重复。")
        if any(
            not _EVIDENCE_PATH.fullmatch(path)
            or ".." in Path(path).parts
            for path in self.evidence
        ):
            raise ValueError("capability evidence 必须是受限的仓库相对路径。")
        if self.state != "supported" and not self.note.strip():
            raise ValueError("degraded/unsupported capability 必须说明原因。")
        return self


class FrontendCapabilityManifest(_StrictModel):
    schema_version: StrictInt = Field(ge=1)
    surface: FrontendSurface
    protocol: FrontendProtocolDeclaration
    capabilities: Mapping[str, FrontendCapabilityDeclaration]

    @model_validator(mode="after")
    def _validate_capability_coverage(self) -> FrontendCapabilityManifest:
        actual = set(self.capabilities)
        expected = set(REQUIRED_TERMINAL_CAPABILITIES)
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        if missing or unknown:
            raise ValueError(
                f"capability 覆盖不完整：missing={missing} unknown={unknown}"
            )
        object.__setattr__(
            self,
            "capabilities",
            MappingProxyType(dict(self.capabilities)),
        )
        return self


def load_frontend_capability_manifest(
    surface: FrontendSurface,
    manifest_path: str | Path | None = None,
) -> FrontendCapabilityManifest:
    """Load one exact shipped surface declaration without inferring support."""
    path = (
        Path(manifest_path).expanduser()
        if manifest_path is not None
        else _manifest_path(surface)
    )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilityManifestError(
            f"无法读取 {surface} capability manifest：{type(exc).__name__}"
        ) from exc
    try:
        manifest = FrontendCapabilityManifest.model_validate(raw)
    except ValidationError as exc:
        messages = sorted({str(error["msg"]) for error in exc.errors()})
        detail = "；".join(messages[:5])
        raise CapabilityManifestError(
            f"{surface} capability manifest 校验失败：{detail}"
        ) from exc
    if manifest.schema_version != CAPABILITY_MANIFEST_SCHEMA_VERSION:
        raise CapabilityManifestError(
            f"不支持的 capability manifest schema：{manifest.schema_version}"
        )
    if manifest.surface != surface:
        raise CapabilityManifestError(
            f"capability manifest surface 不匹配：期望 {surface}，实际 {manifest.surface}"
        )
    return manifest


def assert_required_terminal_parity(
    manifests: tuple[FrontendCapabilityManifest, ...],
) -> None:
    """Fail closed unless every required capability is supported by every surface."""
    surfaces = [manifest.surface for manifest in manifests]
    if len(manifests) != 2 or set(surfaces) != {"new_ui", "tui"}:
        raise CapabilityManifestError("parity 校验必须同时提供 new_ui 与 tui。")
    for manifest in manifests:
        incomplete = sorted(
            capability
            for capability, declaration in manifest.capabilities.items()
            if declaration.state != "supported"
        )
        if incomplete:
            raise CapabilityManifestError(
                f"{manifest.surface} 必需能力未完全支持：{incomplete}"
            )


def missing_capability_evidence(
    manifest: FrontendCapabilityManifest,
    *,
    project_root: str | Path,
) -> tuple[str, ...]:
    """Return declared evidence paths that do not exist in a source checkout."""
    root = Path(project_root).expanduser().resolve()
    missing = {
        path
        for declaration in manifest.capabilities.values()
        for path in declaration.evidence
        if not (root / path).is_file()
    }
    return tuple(sorted(missing))


def _manifest_path(surface: FrontendSurface) -> Path:
    package_root = Path(__file__).resolve().parents[1]
    if surface == "tui":
        candidate = package_root / "tui" / "capability-manifest.json"
        if candidate.is_file():
            return candidate
    source_root = Path(__file__).resolve().parents[3]
    source = source_root / "frontend" / "terminal-ui" / "capability-manifest.json"
    installed = package_root / "frontend" / "terminal-ui" / "capability-manifest.json"
    for candidate in (source, installed):
        if candidate.is_file():
            return candidate
    raise CapabilityManifestError(
        f"未找到随程序发布的 {surface} capability manifest。"
    )


__all__ = [
    "CAPABILITY_MANIFEST_SCHEMA_VERSION",
    "REQUIRED_TERMINAL_CAPABILITIES",
    "CapabilityManifestError",
    "FrontendCapabilityDeclaration",
    "FrontendCapabilityManifest",
    "FrontendProtocolDeclaration",
    "assert_required_terminal_parity",
    "load_frontend_capability_manifest",
    "missing_capability_evidence",
]
