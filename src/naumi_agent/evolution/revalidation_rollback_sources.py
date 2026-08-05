"""Freeze exact Git baseline bytes for a guarded rollback request."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import stat
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.promotion_package_inputs import (
    EvolutionPromotionRollbackOperation,
)
from naumi_agent.evolution.revalidation_rollback_requests import (
    EvolutionRevalidationRollbackRequestStore,
)
from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlState,
    EvolutionRevalidationRolloutControlStore,
)

EVOLUTION_REVALIDATION_ROLLBACK_SOURCE_POLICY = (
    "evolution-revalidation-rollback-source-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_GIT_OBJECT_RE = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_MAX_BLOB_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_BYTES = 16 * _MAX_BLOB_BYTES
_MAX_ARTIFACT_BYTES = 512 * 1024
_GIT_TIMEOUT_SECONDS = 15


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationRollbackSourceFile(_StrictModel):
    order: int = Field(ge=1, le=16)
    path: str = Field(min_length=1, max_length=1024)
    operation: EvolutionPromotionRollbackOperation
    baseline_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    candidate_sha256: str = Field(pattern=_SHA256_RE)
    size_bytes: int = Field(ge=0, le=_MAX_BLOB_BYTES)
    executable: bool
    storage_key: str | None = Field(
        default=None, pattern=r"^blobs/[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _exact(self) -> Self:
        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in self.path
            or any(char in self.path for char in ("\x00", "\r", "\n"))
        ):
            raise ValueError("Rollback Source path 不安全。")
        restoring = (
            self.operation
            is EvolutionPromotionRollbackOperation.RESTORE_BASELINE_BLOB
        )
        if not (
            (self.baseline_sha256 is not None) is restoring
            and (self.storage_key is not None) is restoring
        ):
            raise ValueError("Rollback Source operation/blob 投影不一致。")
        if restoring and self.storage_key != f"blobs/{self.baseline_sha256}":
            raise ValueError("Rollback Source storage key 与 baseline digest 不一致。")
        if not restoring and (self.size_bytes != 0 or self.executable):
            raise ValueError("Remove-created-file source 不得声明 bytes/executable。")
        return self


class EvolutionRevalidationRollbackSource(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-rollback-source-v1"] = (
        EVOLUTION_REVALIDATION_ROLLBACK_SOURCE_POLICY
    )
    source_id: str = Field(pattern=r"^evrerollbacksrc_[0-9a-f]{24}$")
    source_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4096)
    request_id: str = Field(pattern=r"^evrerollbackreq_[0-9a-f]{24}$")
    request_sha256: str = Field(pattern=_SHA256_RE)
    rollback_plan_sha256: str = Field(pattern=_SHA256_RE)
    pause_event_id: str = Field(pattern=r"^evrerolloutctrl_[0-9a-f]{24}$")
    pause_event_sha256: str = Field(pattern=_SHA256_RE)
    baseline_commit: str = Field(pattern=_GIT_OBJECT_RE)
    baseline_tree_sha256: str = Field(pattern=_SHA256_RE)
    files: tuple[EvolutionRevalidationRollbackSourceFile, ...] = Field(
        min_length=1, max_length=16
    )
    file_set_sha256: str = Field(pattern=_SHA256_RE)
    total_bytes: int = Field(ge=0, le=_MAX_TOTAL_BYTES)
    restore_file_count: int = Field(ge=0, le=16)
    remove_file_count: int = Field(ge=0, le=16)
    data_restore_required: bool
    data_restore_input_ready: Literal[False] = False
    source_verified: Literal[True] = True
    content_addressed: Literal[True] = True
    rollback_execution_authority: Literal[False] = False
    workspace_write_executed: Literal[False] = False
    git_write_executed: Literal[False] = False
    created_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Rollback Source workspace 必须 canonical。")
        if tuple(item.order for item in self.files) != tuple(
            range(1, len(self.files) + 1)
        ):
            raise ValueError("Rollback Source files 顺序不连续。")
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("Rollback Source files 必须按 path 排序且唯一。")
        restore = sum(item.storage_key is not None for item in self.files)
        if not (
            self.total_bytes == sum(item.size_bytes for item in self.files)
            and self.restore_file_count == restore
            and self.remove_file_count == len(self.files) - restore
            and self.file_set_sha256
            == _digest([item.model_dump(mode="json") for item in self.files])
        ):
            raise ValueError("Rollback Source file projection 不一致。")
        if datetime.fromisoformat(self.created_at).utcoffset() is None:
            raise ValueError("Rollback Source created_at 必须包含 offset。")
        core = self.model_dump(mode="json", exclude={"source_id", "source_sha256"})
        digest = _digest(core)
        if self.source_sha256 != digest or self.source_id != (
            f"evrerollbacksrc_{digest[:24]}"
        ):
            raise ValueError("Rollback Source identity 不一致。")
        return self


class EvolutionRevalidationRollbackSourceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationRollbackSourceStore:
    def __init__(self, db_path: str | Path, *, storage_dir: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.storage_dir = Path(storage_dir).expanduser().resolve()

    async def get_by_request(self, request_id: str):
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (await db.execute(
                "SELECT source_json FROM evolution_revalidation_rollback_sources "
                "WHERE request_id = ?", (request_id,)
            )).fetchone()
        if row is None:
            return None
        return _restore_source(row["source_json"])

    async def record(self, source, contents: dict[str, bytes]):
        item = EvolutionRevalidationRollbackSource.model_validate_json(
            source.model_dump_json()
        )
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionRevalidationRollbackSourceError(
                "rollback_source_artifact_too_large",
                "Rollback Source 超过 512 KiB 持久化上限。",
            )
        expected = {
            file.baseline_sha256
            for file in item.files
            if file.baseline_sha256 is not None
        }
        if set(contents) != expected:
            raise EvolutionRevalidationRollbackSourceError(
                "rollback_source_content_set_mismatch",
                "Rollback Source blob 集合不完整。",
            )
        await self._require_request_dependency(item)
        for digest, content in contents.items():
            if len(content) > _MAX_BLOB_BYTES or not hmac.compare_digest(
                hashlib.sha256(content).hexdigest(), digest
            ):
                raise EvolutionRevalidationRollbackSourceError(
                    "rollback_source_content_invalid",
                    "Rollback Source blob 大小或摘要不一致。",
                )
            _persist_blob(self.storage_dir / "blobs" / digest, content)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            dependency = await (await db.execute(
                "SELECT request_sha256 FROM evolution_revalidation_rollback_requests "
                "WHERE request_id = ?", (item.request_id,)
            )).fetchone()
            if dependency is None or dependency["request_sha256"] != item.request_sha256:
                await db.rollback()
                raise EvolutionRevalidationRollbackSourceError(
                    "rollback_source_request_mismatch",
                    "Rollback Source 的 Request 依赖不一致。",
                )
            existing = await (await db.execute(
                "SELECT source_json FROM evolution_revalidation_rollback_sources "
                "WHERE request_id = ?", (item.request_id,)
            )).fetchone()
            if existing is not None:
                restored = _restore_source(existing["source_json"])
                await db.rollback()
                await self.verify(restored)
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_rollback_sources "
                "(source_id, source_sha256, request_id, source_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (item.source_id, item.source_sha256, item.request_id, encoded, item.created_at),
            )
            await db.commit()
        await self.verify(item)
        return item

    async def _require_request_dependency(self, item) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            dependency = await (await db.execute(
                "SELECT request_sha256 FROM evolution_revalidation_rollback_requests "
                "WHERE request_id = ?", (item.request_id,)
            )).fetchone()
        if dependency is None or dependency["request_sha256"] != item.request_sha256:
            raise EvolutionRevalidationRollbackSourceError(
                "rollback_source_request_mismatch",
                "Rollback Source 的 Request 依赖不一致。",
            )

    async def verify(self, source) -> None:
        for item in source.files:
            if item.storage_key is None:
                continue
            content = _read_blob(self.storage_dir / item.storage_key)
            if len(content) != item.size_bytes or not hmac.compare_digest(
                hashlib.sha256(content).hexdigest(), item.baseline_sha256 or ""
            ):
                raise EvolutionRevalidationRollbackSourceError(
                    "rollback_source_blob_corrupt",
                    f"Rollback Source blob 损坏：{item.path}",
                )


class EvolutionRevalidationRollbackSourceService:
    def __init__(
        self,
        *,
        workspace_root,
        request_store,
        control_store,
        store,
        now=None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.request_store: EvolutionRevalidationRollbackRequestStore = request_store
        self.control_store: EvolutionRevalidationRolloutControlStore = control_store
        self.store: EvolutionRevalidationRollbackSourceStore = store
        self.now = now or (lambda: datetime.now(UTC).isoformat())

    async def freeze(self, *, request_id: str):
        existing = await self.store.get_by_request(request_id)
        if existing is not None:
            await self.store.verify(existing)
            return existing
        request = await self.request_store.get(request_id)
        if request is None or request.workspace_root != str(self.workspace_root):
            raise EvolutionRevalidationRollbackSourceError(
                "rollback_source_request_missing",
                "Rollback Request 不存在或不属于当前 workspace。",
            )
        await self._require_current_pause(request)
        files, contents = await asyncio.to_thread(self._load_files, request)
        await self._require_current_pause(request)
        core = {
            "schema_version": 1,
            "policy_version": EVOLUTION_REVALIDATION_ROLLBACK_SOURCE_POLICY,
            "workspace_root": str(self.workspace_root),
            "request_id": request.request_id,
            "request_sha256": request.request_sha256,
            "rollback_plan_sha256": request.rollback_plan_sha256,
            "pause_event_id": request.pause_event_id,
            "pause_event_sha256": request.pause_event_sha256,
            "baseline_commit": request.rollback_plan.baseline_commit,
            "baseline_tree_sha256": request.rollback_plan.baseline_tree_sha256,
            "files": [item.model_dump(mode="json") for item in files],
            "file_set_sha256": _digest(
                [item.model_dump(mode="json") for item in files]
            ),
            "total_bytes": sum(item.size_bytes for item in files),
            "restore_file_count": sum(item.storage_key is not None for item in files),
            "remove_file_count": sum(item.storage_key is None for item in files),
            "data_restore_required": request.data_restore_required,
            "data_restore_input_ready": False,
            "source_verified": True,
            "content_addressed": True,
            "rollback_execution_authority": False,
            "workspace_write_executed": False,
            "git_write_executed": False,
            "created_at": _aware(self.now()).isoformat(),
        }
        digest = _digest(core)
        source = EvolutionRevalidationRollbackSource.model_validate(
            {
                **core,
                "source_id": f"evrerollbacksrc_{digest[:24]}",
                "source_sha256": digest,
            }
        )
        return await self.store.record(source, contents)

    async def _require_current_pause(self, request) -> None:
        control = await self.control_store.latest(self.workspace_root)
        if not (
            control is not None
            and control.state is EvolutionRevalidationRolloutControlState.PAUSED
            and control.event_id == request.pause_event_id
            and control.event_sha256 == request.pause_event_sha256
        ):
            raise EvolutionRevalidationRollbackSourceError(
                "rollback_source_pause_stale",
                "Rollback Request 绑定的 kill switch 已不再 current。",
            )

    def _load_files(self, request):
        plan = request.rollback_plan
        resolved = _git(
            self.workspace_root,
            "rev-parse",
            "--verify",
            f"{plan.baseline_commit}^{{commit}}",
        )
        if resolved.decode("ascii").strip().lower() != plan.baseline_commit:
            raise EvolutionRevalidationRollbackSourceError(
                "rollback_source_commit_mismatch", "Rollback baseline commit 无法精确解析。"
            )
        listing = _git(
            self.workspace_root, "ls-tree", "-r", "-z", "--full-tree", plan.baseline_commit
        )
        if not hmac.compare_digest(
            hashlib.sha256(listing).hexdigest(), plan.baseline_tree_sha256
        ):
            raise EvolutionRevalidationRollbackSourceError(
                "rollback_source_tree_mismatch", "Rollback baseline tree 已漂移或损坏。"
            )
        files = []
        contents: dict[str, bytes] = {}
        for step in plan.steps:
            entry = _git(
                self.workspace_root,
                "ls-tree",
                "-z",
                "--full-tree",
                plan.baseline_commit,
                "--",
                step.path,
            )
            if step.operation is EvolutionPromotionRollbackOperation.REMOVE_CREATED_FILE:
                if entry:
                    raise EvolutionRevalidationRollbackSourceError(
                        "rollback_source_created_path_exists",
                        f"Rollback create path 已存在于 baseline：{step.path}",
                    )
                files.append(EvolutionRevalidationRollbackSourceFile(
                    order=step.order,
                    path=step.path,
                    operation=step.operation,
                    baseline_sha256=None,
                    candidate_sha256=step.candidate_sha256,
                    size_bytes=0,
                    executable=False,
                    storage_key=None,
                ))
                continue
            mode, blob = _parse_tree_entry(entry, step.path)
            content = _git(self.workspace_root, "cat-file", "blob", blob)
            if len(content) > _MAX_BLOB_BYTES or not hmac.compare_digest(
                hashlib.sha256(content).hexdigest(), step.baseline_sha256 or ""
            ):
                raise EvolutionRevalidationRollbackSourceError(
                    "rollback_source_baseline_digest_mismatch",
                    f"Rollback baseline blob 摘要不一致：{step.path}",
                )
            contents[step.baseline_sha256 or ""] = content
            files.append(EvolutionRevalidationRollbackSourceFile(
                order=step.order,
                path=step.path,
                operation=step.operation,
                baseline_sha256=step.baseline_sha256,
                candidate_sha256=step.candidate_sha256,
                size_bytes=len(content),
                executable=mode == "100755",
                storage_key=f"blobs/{step.baseline_sha256}",
            ))
        if sum(item.size_bytes for item in files) > _MAX_TOTAL_BYTES:
            raise EvolutionRevalidationRollbackSourceError(
                "rollback_source_total_oversized", "Rollback Source 总大小超过 32 MiB。"
            )
        return tuple(files), contents


def _parse_tree_entry(entry: bytes, expected_path: str) -> tuple[str, str]:
    try:
        metadata, path = entry.rstrip(b"\0").split(b"\t", 1)
        mode, kind, blob = metadata.decode("ascii").split(" ", 2)
        decoded_path = path.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise EvolutionRevalidationRollbackSourceError(
            "rollback_source_tree_entry_invalid", "Rollback baseline tree entry 无效。"
        ) from exc
    if (
        decoded_path != expected_path
        or kind != "blob"
        or mode not in {"100644", "100755"}
        or len(blob) not in {40, 64}
        or any(char not in "0123456789abcdef" for char in blob)
    ):
        raise EvolutionRevalidationRollbackSourceError(
            "rollback_source_tree_entry_invalid", "Rollback baseline tree entry 不匹配。"
        )
    return mode, blob


def _git(root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvolutionRevalidationRollbackSourceError(
            "rollback_source_git_failed", "Rollback Source Git 操作无法执行。"
        ) from exc
    if completed.returncode != 0 or len(completed.stdout) > _MAX_TOTAL_BYTES:
        raise EvolutionRevalidationRollbackSourceError(
            "rollback_source_git_failed", "Rollback Source Git 操作失败或输出过大。"
        )
    return completed.stdout


def _persist_blob(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if _read_blob(path) != content:
            raise EvolutionRevalidationRollbackSourceError(
                "rollback_source_blob_conflict", "Rollback blob 地址已存在不同内容。"
            )
        return
    fd, temp_name = tempfile.mkstemp(prefix=".rollback-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o400)
        os.replace(temp_name, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _read_blob(path: Path) -> bytes:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise EvolutionRevalidationRollbackSourceError(
            "rollback_source_blob_unreadable", "Rollback blob 不存在或不可读取。"
        ) from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size > _MAX_BLOB_BYTES
    ):
        raise EvolutionRevalidationRollbackSourceError(
            "rollback_source_blob_unsafe", "Rollback blob 不是安全 regular file。"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        current = os.fstat(fd)
        try:
            after = os.lstat(path)
        except OSError as exc:
            raise EvolutionRevalidationRollbackSourceError(
                "rollback_source_blob_replaced", "Rollback blob 在读取期间被替换。"
            ) from exc
        if not (
            stat.S_ISREG(current.st_mode)
            and os.path.samestat(info, current)
            and os.path.samestat(after, current)
        ):
            raise EvolutionRevalidationRollbackSourceError(
                "rollback_source_blob_replaced", "Rollback blob 在读取期间被替换。"
            )
        chunks = []
        remaining = current.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                raise EvolutionRevalidationRollbackSourceError(
                    "rollback_source_blob_truncated", "Rollback blob 被截断。"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _restore_source(encoded: str) -> EvolutionRevalidationRollbackSource:
    if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
        raise EvolutionRevalidationRollbackSourceError(
            "rollback_source_artifact_too_large", "持久化 Rollback Source 超过上限。"
        )
    return EvolutionRevalidationRollbackSource.model_validate_json(encoded)


def _aware(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.utcoffset() is None:
        raise ValueError("Rollback Source 时间必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(payload):
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


async def _ensure_schema(db):
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_rollback_sources ("
        "source_id TEXT PRIMARY KEY, source_sha256 TEXT NOT NULL UNIQUE, "
        "request_id TEXT NOT NULL UNIQUE, source_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL)"
    )
    await db.commit()


__all__ = [
    "EVOLUTION_REVALIDATION_ROLLBACK_SOURCE_POLICY",
    "EvolutionRevalidationRollbackSource",
    "EvolutionRevalidationRollbackSourceError",
    "EvolutionRevalidationRollbackSourceFile",
    "EvolutionRevalidationRollbackSourceService",
    "EvolutionRevalidationRollbackSourceStore",
]
