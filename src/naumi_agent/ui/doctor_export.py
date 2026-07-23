"""Deterministic, privacy-bounded Doctor export bundles."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from naumi_agent import __version__
from naumi_agent.config.state_paths import resolve_naumi_state_home
from naumi_agent.safety.guardrails import OutputGuardrail
from naumi_agent.ui.doctor_health import DoctorHealthSnapshot

_BUNDLE_FILES = ("health.json", "README.txt", "manifest.json")
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_MAX_BUNDLE_BYTES = 512 * 1024
_EXPORT_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b(api[_-]?key|token|secret|password|authorization)"
            r"(\s*[:=]\s*)[^\s,;]{8,}"
        ),
        r"\1\2[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)\b(?:sk|rk|pk)-(?:proj-)?[A-Za-z0-9_-]{20,}"
        ),
        "[REDACTED_API_KEY]",
    ),
    (
        re.compile(r"(?i)\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}"),
        "[REDACTED_GITHUB_TOKEN]",
    ),
    (
        re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"),
        "[REDACTED_AUTHORIZATION]",
    ),
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DoctorExportFile(_StrictModel):
    path: Literal["health.json", "README.txt", "manifest.json"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0, le=_MAX_BUNDLE_BYTES)
    description: str = Field(min_length=1, max_length=120)


class DoctorExportPreview(_StrictModel):
    schema_version: Literal[1] = 1
    bundle_format: Literal["zip"] = "zip"
    source_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    total_bytes: int = Field(ge=1, le=_MAX_BUNDLE_BYTES)
    files: tuple[DoctorExportFile, ...] = Field(min_length=3, max_length=3)
    privacy_notice: str = Field(min_length=1, max_length=500)


class DoctorExportReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    output_path: str = Field(min_length=1, max_length=2_000)
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=_MAX_BUNDLE_BYTES)
    reused_existing: bool


@dataclass(frozen=True)
class DoctorExportPlan:
    """Internal immutable bundle bytes plus their public preview."""

    preview: DoctorExportPreview
    archive_bytes: bytes


def build_doctor_export_plan(
    snapshot: DoctorHealthSnapshot,
    *,
    workspace_root: str | Path,
    state_home: str | Path | None = None,
) -> DoctorExportPlan:
    """Build a deterministic archive from one already-bounded Health snapshot."""
    if not isinstance(snapshot, DoctorHealthSnapshot):
        raise TypeError("snapshot 必须是 DoctorHealthSnapshot。")
    workspace_input = Path(workspace_root).expanduser()
    workspace = workspace_input.resolve()
    state_home_input = (
        Path(state_home).expanduser()
        if state_home is not None
        else resolve_naumi_state_home()
    )
    resolved_state_home = state_home_input.resolve()
    replacements = _private_path_replacements(
        workspace_root=workspace,
        state_home=resolved_state_home,
        aliases=(
            (workspace_input, "<workspace>"),
            (state_home_input, "<naumi-state>"),
        ),
    )
    health_payload = {
        "schema_version": 1,
        "source_snapshot_sha256": snapshot.snapshot_sha256,
        "status": snapshot.status,
        "generated_at": snapshot.generated_at,
        "live_probe": snapshot.live_probe,
        "redaction": {
            "secrets": True,
            "workspace_path": "<workspace>",
            "state_path": "<naumi-state>",
            "home_path": "~",
            "raw_trace_included": False,
            "conversation_included": False,
            "reasoning_included": False,
        },
        "items": [
            {
                **item.model_dump(mode="json"),
                "label": _sanitize_text(item.label, replacements),
                "detail": _sanitize_text(item.detail, replacements),
                "suggestion": _sanitize_text(item.suggestion, replacements),
            }
            for item in snapshot.items
        ],
    }
    health_bytes = _canonical_json_bytes(health_payload)
    readme_bytes = (
        "NaumiAgent 脱敏诊断包\n"
        "\n"
        "本包只包含 typed Doctor Health 状态、稳定诊断码和 manifest。\n"
        "不包含聊天正文、模型 reasoning、原始 trace、环境变量全集、API key 或源码。\n"
        "绝对工作区、Naumi 状态目录和用户主目录已替换为占位符。\n"
        "\n"
        f"来源快照: {snapshot.snapshot_sha256}\n"
    ).encode()
    manifest_payload = {
        "schema_version": 1,
        "kind": "naumi_doctor_export",
        "product": "NaumiAgent",
        "product_version": __version__,
        "platform": platform.system().lower() or "unknown",
        "source_snapshot_sha256": snapshot.snapshot_sha256,
        "generated_at": snapshot.generated_at,
        "files": [
            _manifest_file("health.json", health_bytes, "脱敏后的 typed Health 快照"),
            _manifest_file("README.txt", readme_bytes, "诊断包隐私与用途说明"),
        ],
        "excludes": [
            "chat_content",
            "model_reasoning",
            "raw_debug_trace",
            "environment_dump",
            "credentials",
            "source_code",
        ],
    }
    manifest_bytes = _canonical_json_bytes(manifest_payload)
    archive_bytes = _build_zip({
        "health.json": health_bytes,
        "README.txt": readme_bytes,
        "manifest.json": manifest_bytes,
    })
    if len(archive_bytes) > _MAX_BUNDLE_BYTES:
        raise ValueError("诊断包超过 512 KiB 安全上限。")

    file_descriptions = {
        "health.json": "脱敏后的 typed Health 快照",
        "README.txt": "诊断包隐私与用途说明",
        "manifest.json": "产品、平台、文件摘要与排除项",
    }
    file_bytes = {
        "health.json": health_bytes,
        "README.txt": readme_bytes,
        "manifest.json": manifest_bytes,
    }
    preview = DoctorExportPreview(
        source_snapshot_sha256=snapshot.snapshot_sha256,
        manifest_sha256=_sha256(manifest_bytes),
        bundle_sha256=_sha256(archive_bytes),
        total_bytes=len(archive_bytes),
        files=tuple(
            DoctorExportFile(
                path=path,
                sha256=_sha256(file_bytes[path]),
                size_bytes=len(file_bytes[path]),
                description=file_descriptions[path],
            )
            for path in _BUNDLE_FILES
        ),
        privacy_notice=(
            "仅包含脱敏 Health、manifest 与说明；不包含聊天、reasoning、"
            "原始 trace、环境变量全集、凭据或源码。"
        ),
    )
    return DoctorExportPlan(preview=preview, archive_bytes=archive_bytes)


def write_doctor_export(
    plan: DoctorExportPlan,
    *,
    state_home: str | Path | None = None,
    now: datetime | None = None,
) -> DoctorExportReceipt:
    """Atomically write one previewed bundle under the platform state directory."""
    if not isinstance(plan, DoctorExportPlan):
        raise TypeError("plan 必须是 DoctorExportPlan。")
    if _sha256(plan.archive_bytes) != plan.preview.bundle_sha256:
        raise ValueError("诊断包内容与预览摘要不一致，拒绝写入。")
    root = (
        Path(state_home).expanduser().resolve()
        if state_home is not None
        else resolve_naumi_state_home()
    ) / "diagnostics"
    if root.exists() and root.is_symlink():
        raise OSError("诊断导出目录不能是符号链接。")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(root, 0o700)

    current = (now or datetime.now(UTC)).astimezone(UTC)
    stamp = current.strftime("%Y%m%dT%H%M%SZ")
    target = root / (
        f"naumi-diagnostics-{stamp}-{plan.preview.bundle_sha256[:16]}.zip"
    )
    if target.exists():
        if target.is_symlink() or _sha256(target.read_bytes()) != plan.preview.bundle_sha256:
            raise FileExistsError("同名诊断包已存在但摘要不一致，拒绝覆盖。")
        return _receipt(plan, target, reused_existing=True)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".naumi-diagnostics-",
        suffix=".tmp",
        dir=root,
    )
    temporary = Path(temporary_name)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(plan.archive_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        if os.name == "posix":
            os.chmod(target, 0o600)
            _fsync_directory(root)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return _receipt(plan, target, reused_existing=False)


def doctor_export_preview_payload(preview: DoctorExportPreview) -> dict[str, object]:
    return {"status": "preview", **preview.model_dump(mode="json")}


def doctor_export_receipt_payload(
    preview: DoctorExportPreview,
    receipt: DoctorExportReceipt,
) -> dict[str, object]:
    return {
        "status": "written",
        **preview.model_dump(mode="json"),
        "receipt": receipt.model_dump(mode="json"),
    }


def render_doctor_export_preview(preview: DoctorExportPreview) -> str:
    """Render the exact bounded file list before any write occurs."""
    lines = [
        "## 脱敏诊断包预览",
        "",
        f"- 来源快照：`{preview.source_snapshot_sha256}`",
        f"- Bundle：`{preview.bundle_sha256}`",
        f"- 大小：{preview.total_bytes} bytes",
        "- 文件：",
    ]
    lines.extend(
        f"  - `{item.path}` · {item.size_bytes} bytes · {item.description}"
        for item in preview.files
    )
    lines.extend([
        "",
        f"隐私边界：{preview.privacy_notice}",
        "",
        "确认清单无误后，使用上述来源快照摘要执行写入。",
    ])
    return "\n".join(lines)


def render_doctor_export_receipt(receipt: DoctorExportReceipt) -> str:
    """Render one local write receipt without claiming it was uploaded."""
    action = "复用已有文件" if receipt.reused_existing else "已原子写入"
    return "\n".join([
        "## 诊断包导出完成",
        "",
        f"- 状态：{action}",
        f"- 路径：`{receipt.output_path}`",
        f"- Bundle：`{receipt.bundle_sha256}`",
        f"- 大小：{receipt.size_bytes} bytes",
        "",
        "文件仅保存在本机 Naumi 状态目录，尚未上传或发送给任何人。",
    ])


def _private_path_replacements(
    *,
    workspace_root: Path,
    state_home: Path,
    aliases: tuple[tuple[Path, str], ...] = (),
) -> tuple[tuple[str, str], ...]:
    values = [
        (str(workspace_root), "<workspace>"),
        (str(state_home), "<naumi-state>"),
        (str(Path.home().resolve()), "~"),
    ]
    for alias, replacement in aliases:
        if not alias.is_absolute():
            continue
        values.append((str(alias), replacement))
    expanded: list[tuple[str, str]] = []
    for source, replacement in values:
        if source and source not in {"/", "\\"}:
            expanded.append((source, replacement))
            expanded.append((source.replace("\\", "/"), replacement))
    expanded.sort(key=lambda item: len(item[0]), reverse=True)
    return tuple(dict.fromkeys(expanded))


def _sanitize_text(value: object, replacements: tuple[tuple[str, str], ...]) -> str:
    text = OutputGuardrail.redact(str(value or ""))
    for pattern, replacement in _EXPORT_SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    for source, replacement in replacements:
        text = text.replace(source, replacement)
    return " ".join(text.split())[:500]


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _manifest_file(path: str, content: bytes, description: str) -> dict[str, object]:
    return {
        "path": path,
        "sha256": _sha256(content),
        "size_bytes": len(content),
        "description": description,
    }


def _build_zip(files: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in _BUNDLE_FILES:
            info = zipfile.ZipInfo(path, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, files[path])
    return buffer.getvalue()


def _receipt(
    plan: DoctorExportPlan,
    path: Path,
    *,
    reused_existing: bool,
) -> DoctorExportReceipt:
    return DoctorExportReceipt(
        output_path=str(path.resolve()),
        bundle_sha256=plan.preview.bundle_sha256,
        source_snapshot_sha256=plan.preview.source_snapshot_sha256,
        size_bytes=len(plan.archive_bytes),
        reused_existing=reused_existing,
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "DoctorExportPlan",
    "DoctorExportPreview",
    "DoctorExportReceipt",
    "build_doctor_export_plan",
    "doctor_export_preview_payload",
    "doctor_export_receipt_payload",
    "render_doctor_export_preview",
    "render_doctor_export_receipt",
    "write_doctor_export",
]
