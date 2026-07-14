"""Bounded and side-effect-free Harness profile loading."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from pathlib import Path

import yaml
from pydantic import ValidationError

from naumi_agent.harness.models import (
    HarnessProfile,
    HarnessProfileError,
    HarnessProfileSnapshot,
    HarnessProfileStatus,
)

MAX_PROFILE_BYTES = 256 * 1024
DEFAULT_PROFILE_PATH = Path(".naumi/harness.yaml")


def load_harness_profile(
    workspace_root: str | Path,
    profile_path: str | Path | None = None,
) -> HarnessProfileSnapshot:
    """Load one workspace profile without executing or trusting its contents."""
    workspace = Path(workspace_root).expanduser().resolve()
    requested = Path(profile_path).expanduser() if profile_path is not None else None
    candidate = requested or workspace / DEFAULT_PROFILE_PATH
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved_profile = candidate.resolve(strict=False)

    if not _is_relative_to(resolved_profile, workspace):
        return _invalid_snapshot(
            workspace,
            resolved_profile,
            "profile_outside_workspace",
            "Harness 配置文件必须位于当前工作区内。",
            "下一步：将配置移动到工作区的 .naumi/harness.yaml。",
        )
    if not resolved_profile.is_file():
        return HarnessProfileSnapshot(
            workspace_root=workspace,
            profile_path=resolved_profile,
            status=HarnessProfileStatus.MISSING,
        )

    try:
        profile_size = resolved_profile.stat().st_size
    except OSError:
        return _invalid_snapshot(
            workspace,
            resolved_profile,
            "profile_unreadable",
            "Harness 配置文件无法读取。",
            "下一步：检查文件权限后运行 /harness doctor。",
        )

    if profile_size > MAX_PROFILE_BYTES:
        return _invalid_snapshot(
            workspace,
            resolved_profile,
            "profile_too_large",
            f"Harness 配置超过 {MAX_PROFILE_BYTES // 1024} KiB 上限。",
            "下一步：删除非机械配置内容，将说明文字移入 docs/harness/。",
        )

    try:
        with resolved_profile.open("rb") as stream:
            raw = stream.read(MAX_PROFILE_BYTES + 1)
    except OSError:
        return _invalid_snapshot(
            workspace,
            resolved_profile,
            "profile_unreadable",
            "Harness 配置文件无法读取。",
            "下一步：检查文件权限后运行 /harness doctor。",
        )
    if len(raw) > MAX_PROFILE_BYTES:
        return _invalid_snapshot(
            workspace,
            resolved_profile,
            "profile_too_large",
            f"Harness 配置超过 {MAX_PROFILE_BYTES // 1024} KiB 上限。",
            "下一步：删除非机械配置内容，将说明文字移入 docs/harness/。",
        )
    digest = hashlib.sha256(raw).hexdigest()

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _invalid_snapshot(
            workspace,
            resolved_profile,
            "invalid_encoding",
            "Harness 配置必须使用 UTF-8 编码。",
            "下一步：将文件转换为 UTF-8 后重新诊断。",
            digest=digest,
        )
    if not text.strip():
        return _invalid_snapshot(
            workspace,
            resolved_profile,
            "empty_profile",
            "Harness 配置文件为空。",
            "下一步：至少写入 schema_version: 1。",
            digest=digest,
        )

    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError:
        return _invalid_snapshot(
            workspace,
            resolved_profile,
            "invalid_yaml",
            "Harness 配置不是安全、有效的 YAML。",
            "下一步：修复 YAML 语法；不允许使用 Python object tag。",
            digest=digest,
        )
    if not isinstance(payload, Mapping):
        return _invalid_snapshot(
            workspace,
            resolved_profile,
            "invalid_root",
            "Harness 配置的根节点必须是键值对象。",
            "下一步：以 schema_version: 1 作为根字段。",
            digest=digest,
        )

    try:
        profile = HarnessProfile.model_validate(payload)
    except ValidationError:
        return _invalid_snapshot(
            workspace,
            resolved_profile,
            "invalid_profile",
            "Harness 配置字段、类型或取值不符合 schema version 1。",
            "下一步：运行 /harness doctor，并按字段摘要修复配置。",
            digest=digest,
        )

    escaped = _find_escaped_path(workspace, profile)
    if escaped is not None:
        return _invalid_snapshot(
            workspace,
            resolved_profile,
            "path_outside_workspace",
            f"Harness 路径越过工作区边界：{escaped}",
            "下一步：改用工作区内的相对路径，并移除 .. 或越界符号链接。",
            digest=digest,
        )

    return HarnessProfileSnapshot(
        workspace_root=workspace,
        profile_path=resolved_profile,
        status=HarnessProfileStatus.VALID,
        digest=digest,
        profile=profile,
    )


def _find_escaped_path(workspace: Path, profile: HarnessProfile) -> str | None:
    concrete = (*profile.knowledge.entrypoints, *profile.evals.suites)
    patterns = (
        *profile.knowledge.include,
        *profile.knowledge.exclude,
        *(pattern for check in profile.checks for pattern in check.when_changed),
    )
    for value in concrete:
        if not _relative_path_is_safe(workspace, value, resolve=True):
            return value
    for value in patterns:
        if not _relative_path_is_safe(workspace, value, resolve=False):
            return value
    return None


def _relative_path_is_safe(workspace: Path, value: str, *, resolve: bool) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    path = Path(value.strip())
    if path.is_absolute() or ".." in path.parts:
        return False
    if not resolve:
        prefix_parts: list[str] = []
        for part in path.parts:
            if any(marker in part for marker in ("*", "?", "[")):
                break
            prefix_parts.append(part)
        prefix = workspace.joinpath(*prefix_parts).resolve(strict=False)
        return _is_relative_to(prefix, workspace)
    return _is_relative_to((workspace / path).resolve(strict=False), workspace)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _invalid_snapshot(
    workspace: Path,
    profile_path: Path,
    code: str,
    message: str,
    hint: str,
    *,
    digest: str | None = None,
    errors: Iterable[HarnessProfileError] = (),
) -> HarnessProfileSnapshot:
    profile_errors = tuple(errors) or (
        HarnessProfileError(code=code, message=message, hint=hint),
    )
    return HarnessProfileSnapshot(
        workspace_root=workspace,
        profile_path=profile_path,
        status=HarnessProfileStatus.INVALID,
        digest=digest,
        errors=profile_errors,
    )
