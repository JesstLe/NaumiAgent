"""Deterministic, source-free release artifact assembly."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ArchiveFormat = Literal["tar.gz", "zip"]

_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FORBIDDEN_COMPONENTS = frozenset(
    {".git", "__pycache__", "docs", "frontend", "src", "tests"}
)
_FORBIDDEN_NAMES = frozenset(
    {"package.json", "pyproject.toml", "manifest.in", "uv.lock"}
)
_FORBIDDEN_SUFFIXES = (
    ".py",
    ".pyc",
    ".pyo",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".map",
    ".ipynb",
)


class ArtifactError(ValueError):
    """Raised when a release input or assembled bundle is unsafe."""


@dataclass(frozen=True)
class ReleaseArtifact:
    bundle_dir: Path
    archive: Path
    checksum: Path
    manifest: Path


def assemble_release_artifact(
    *,
    backend_dir: Path,
    ui_binary: Path,
    config_example: Path,
    output_dir: Path,
    version: str,
    target: str,
    archive_format: ArchiveFormat,
) -> ReleaseArtifact:
    """Build and validate one platform bundle without replacing existing output."""
    version = _safe_label(version, "version")
    target = _safe_label(target, "target")
    if archive_format not in {"tar.gz", "zip"}:
        raise ArtifactError("archive_format 仅支持 tar.gz 或 zip。")
    backend_dir = backend_dir.resolve()
    ui_binary = ui_binary.resolve()
    config_example = config_example.resolve()
    if not backend_dir.is_dir():
        raise ArtifactError(f"后端目录不存在：{backend_dir}")
    if not ui_binary.is_file() or ui_binary.stat().st_size <= 0:
        raise ArtifactError(f"Terminal UI 二进制不存在或为空：{ui_binary}")
    if not config_example.is_file():
        raise ArtifactError(f"示例配置不存在：{config_example}")
    _validate_tree_symlinks(backend_dir)
    _validate_source_free_tree(backend_dir)

    windows = target.casefold().startswith("windows-")
    backend_name = "naumi.exe" if windows else "naumi"
    ui_name = "naumi-ui.exe" if windows else "naumi-ui"
    backend_binary = backend_dir / backend_name
    if not backend_binary.is_file() or backend_binary.stat().st_size <= 0:
        raise ArtifactError(f"冻结后端缺少入口：{backend_binary}")

    bundle_name = f"naumi-{version}-{target}"
    archive_name = f"{bundle_name}.{archive_format}"
    output_dir.mkdir(parents=True, exist_ok=True)
    final_bundle = output_dir / bundle_name
    final_archive = output_dir / archive_name
    final_checksum = output_dir / f"{archive_name}.sha256"

    transaction = Path(
        tempfile.mkdtemp(prefix=f".{bundle_name}.tmp-", dir=output_dir)
    )
    staged_bundle = transaction / bundle_name
    staged_archive = transaction / archive_name
    staged_checksum = transaction / final_checksum.name
    try:
        shutil.copytree(backend_dir, staged_bundle, symlinks=True)
        if (staged_bundle / ui_name).exists():
            raise ArtifactError(f"后端目录意外占用 UI 入口：{ui_name}")
        shutil.copy2(ui_binary, staged_bundle / ui_name)
        shutil.copy2(config_example, staged_bundle / "config.yaml.example")
        if os.name != "nt":
            (staged_bundle / backend_name).chmod(
                (staged_bundle / backend_name).stat().st_mode | stat.S_IXUSR
            )
            (staged_bundle / ui_name).chmod(
                (staged_bundle / ui_name).stat().st_mode | stat.S_IXUSR
            )
        _validate_tree_symlinks(staged_bundle)
        _validate_source_free_tree(staged_bundle)
        manifest_path = staged_bundle / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "product": "NaumiAgent",
                    "version": version,
                    "target": target,
                    "files": _manifest_files(staged_bundle),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        _write_archive(
            staged_bundle,
            staged_archive,
            bundle_name=bundle_name,
            archive_format=archive_format,
        )
        archive_hash = _sha256_file(staged_archive)
        staged_checksum.write_text(
            f"{archive_hash}  {archive_name}\n",
            encoding="utf-8",
        )
        collisions = [
            path
            for path in (final_bundle, final_archive, final_checksum)
            if path.exists()
        ]
        if collisions:
            raise ArtifactError(
                "拒绝覆盖现有发行产物：" + "、".join(str(path) for path in collisions)
            )
        staged_bundle.replace(final_bundle)
        staged_archive.replace(final_archive)
        staged_checksum.replace(final_checksum)
    finally:
        shutil.rmtree(transaction, ignore_errors=True)

    return ReleaseArtifact(
        bundle_dir=final_bundle,
        archive=final_archive,
        checksum=final_checksum,
        manifest=final_bundle / "manifest.json",
    )


def _safe_label(value: str, name: str) -> str:
    normalized = value.strip()
    if not _SAFE_LABEL.fullmatch(normalized):
        raise ArtifactError(f"{name} 含不安全字符。")
    return normalized


def _validate_tree_symlinks(root: Path) -> None:
    resolved_root = root.resolve()
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(current) / name
            if not path.is_symlink():
                continue
            try:
                path.resolve(strict=True).relative_to(resolved_root)
            except (FileNotFoundError, ValueError) as exc:
                raise ArtifactError(f"发行目录含符号链接越界或失效：{path}") from exc


def _validate_source_free_tree(root: Path) -> None:
    for current, dirs, files in os.walk(root, followlinks=False):
        relative_root = Path(current).relative_to(root)
        for name in [*dirs, *files]:
            relative = relative_root / name
            lowered_parts = {part.casefold() for part in relative.parts}
            lowered_name = name.casefold()
            inside_third_party_runtime = (
                len(relative.parts) >= 2
                and relative.parts[0].casefold() == "_internal"
                and relative.parts[1].casefold() != "naumi_agent"
            )
            if (
                (
                    lowered_parts & _FORBIDDEN_COMPONENTS
                    or lowered_name in _FORBIDDEN_NAMES
                    or lowered_name.endswith(_FORBIDDEN_SUFFIXES)
                )
                and not inside_third_party_runtime
            ):
                raise ArtifactError(f"发行产物检测到源码泄漏：{relative.as_posix()}")


def _manifest_files(root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            target = os.readlink(path)
            records.append(
                {
                    "path": relative,
                    "kind": "symlink",
                    "target": target,
                    "sha256": hashlib.sha256(target.encode("utf-8")).hexdigest(),
                }
            )
        elif path.is_file():
            records.append(
                {
                    "path": relative,
                    "kind": "file",
                    "size": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
            )
    return records


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_archive(
    bundle: Path,
    destination: Path,
    *,
    bundle_name: str,
    archive_format: ArchiveFormat,
) -> None:
    paths = [bundle, *sorted(bundle.rglob("*"), key=lambda item: item.as_posix())]
    if archive_format == "tar.gz":
        with destination.open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as archive:
                    for path in paths:
                        relative = path.relative_to(bundle.parent)
                        archive.add(
                            path,
                            arcname=relative.as_posix(),
                            recursive=False,
                            filter=_normalize_tar_info,
                        )
        return
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            relative = path.relative_to(bundle.parent).as_posix()
            if path.is_dir():
                relative += "/"
                data = b""
            elif path.is_symlink():
                data = os.readlink(path).encode("utf-8")
            else:
                data = path.read_bytes()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (path.lstat().st_mode & 0xFFFF) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)


def _normalize_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    return info
