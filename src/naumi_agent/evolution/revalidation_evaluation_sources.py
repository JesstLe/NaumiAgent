"""Immutable content-addressed source snapshots for post-revalidation evaluation."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_evaluation_plans import (
    EvolutionRevalidationEvaluationPlanService,
)
from naumi_agent.evolution.revalidation_outcomes import (
    EvolutionRevalidationOutcomeStore,
)
from naumi_agent.harness.evolution_revalidation import (
    HarnessEvolutionRevalidationPlan,
    HarnessEvolutionRevalidationSource,
)
from naumi_agent.harness.sandbox_checks import HarnessSandboxSourceOverlay

EVOLUTION_REVALIDATION_EVALUATION_SOURCE_POLICY = (
    "evolution-revalidation-evaluation-source-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_BLOB_BYTES = 2 * 1_024 * 1_024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionRevalidationEvaluationSourceBlob(_StrictModel):
    order: int = Field(ge=1, le=16)
    path: str = Field(min_length=1, max_length=1_024)
    sha256: str = Field(pattern=_SHA256_RE)
    size_bytes: int = Field(ge=0, le=_MAX_BLOB_BYTES)
    executable: bool
    storage_key: str = Field(pattern=r"^blobs/[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _blob_is_safe(self) -> Self:
        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in self.path
            or any(char in self.path for char in ("\x00", "\r", "\n"))
        ):
            raise ValueError("Evaluation Source blob path 不安全。")
        if self.storage_key != f"blobs/{self.sha256}":
            raise ValueError("Evaluation Source storage key 与 digest 不一致。")
        return self


class EvolutionRevalidationEvaluationSourceSnapshot(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-evaluation-source-v1"] = (
        EVOLUTION_REVALIDATION_EVALUATION_SOURCE_POLICY
    )
    snapshot_id: str = Field(pattern=r"^evrevalsrc_[0-9a-f]{24}$")
    snapshot_sha256: str = Field(pattern=_SHA256_RE)
    plan_id: str = Field(pattern=r"^evrevalplan_[0-9a-f]{24}$")
    plan_sha256: str = Field(pattern=_SHA256_RE)
    outcome_id: str = Field(pattern=r"^evrevalout_[0-9a-f]{24}$")
    outcome_sha256: str = Field(pattern=_SHA256_RE)
    request_id: str = Field(pattern=r"^evrevalidation_[0-9a-f]{24}$")
    validation_receipt_id: str = Field(pattern=r"^evrevalidate_[0-9a-f]{24}$")
    validation_receipt_sha256: str = Field(pattern=_SHA256_RE)
    target_head: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    target_tree_sha256: str = Field(pattern=_SHA256_RE)
    overlay_source_sha256: str = Field(pattern=_SHA256_RE)
    harness_profile_sha256: str = Field(pattern=_SHA256_RE)
    harness_plan_sha256: str = Field(pattern=_SHA256_RE)
    blobs: tuple[EvolutionRevalidationEvaluationSourceBlob, ...] = Field(
        min_length=1,
        max_length=16,
    )
    blob_set_sha256: str = Field(pattern=_SHA256_RE)
    total_bytes: int = Field(ge=0, le=16 * _MAX_BLOB_BYTES)
    content_addressed: Literal[True] = True
    immutable_source_complete: Literal[True] = True
    evaluation_execution_started: Literal[False] = False
    promotion_authority: Literal[False] = False
    created_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _snapshot_is_exact(self) -> Self:
        if tuple(item.order for item in self.blobs) != tuple(
            range(1, len(self.blobs) + 1)
        ):
            raise ValueError("Evaluation Source blobs 顺序不连续。")
        if tuple(item.path for item in self.blobs) != tuple(
            sorted(item.path for item in self.blobs)
        ):
            raise ValueError("Evaluation Source blobs 必须按 path 排序。")
        if len({item.path for item in self.blobs}) != len(self.blobs):
            raise ValueError("Evaluation Source blobs path 不得重复。")
        expected_set = _sha256_payload(
            [item.model_dump(mode="json") for item in self.blobs]
        )
        if not hmac.compare_digest(self.blob_set_sha256, expected_set):
            raise ValueError("Evaluation Source blob set digest 不一致。")
        if self.total_bytes != sum(item.size_bytes for item in self.blobs):
            raise ValueError("Evaluation Source total bytes 不一致。")
        if datetime.fromisoformat(self.created_at).utcoffset() is None:
            raise ValueError("Evaluation Source created_at 必须包含 UTC offset。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"snapshot_id", "snapshot_sha256"})
        )
        if not hmac.compare_digest(self.snapshot_sha256, digest):
            raise ValueError("Evaluation Source Snapshot 摘要不一致。")
        if self.snapshot_id != f"evrevalsrc_{digest[:24]}":
            raise ValueError("Evaluation Source Snapshot identity 不一致。")
        return self


class EvolutionRevalidationEvaluationSourceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationSourceProvider(Protocol):
    async def materialize_current_source(
        self, *, workspace_root: str | Path, request_id: str
    ) -> tuple[HarnessEvolutionRevalidationSource, HarnessEvolutionRevalidationPlan]: ...


class EvolutionRevalidationEvaluationSourceStore:
    def __init__(self, db_path: str | Path, *, storage_dir: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._storage_dir = Path(storage_dir).expanduser().resolve()

    async def record(
        self, item: EvolutionRevalidationEvaluationSourceSnapshot
    ) -> EvolutionRevalidationEvaluationSourceSnapshot:
        artifact = EvolutionRevalidationEvaluationSourceSnapshot.model_validate_json(
            item.model_dump_json()
        )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            dependency = await (
                await db.execute(
                    "SELECT plan_sha256 FROM evolution_revalidation_evaluation_plans "
                    "WHERE plan_id = ?",
                    (artifact.plan_id,),
                )
            ).fetchone()
            if dependency is None or dependency["plan_sha256"] != artifact.plan_sha256:
                await db.rollback()
                raise EvolutionRevalidationEvaluationSourceError(
                    "revalidation_evaluation_source_plan_mismatch",
                    "持久化 Fresh Evaluation Plan 不存在或 digest 不一致。",
                )
            row = await (
                await db.execute(
                    "SELECT snapshot_json FROM evolution_revalidation_evaluation_sources "
                    "WHERE plan_id = ?",
                    (artifact.plan_id,),
                )
            ).fetchone()
            if row is not None:
                restored = EvolutionRevalidationEvaluationSourceSnapshot.model_validate_json(
                    row["snapshot_json"]
                )
                await db.rollback()
                if restored != artifact:
                    raise EvolutionRevalidationEvaluationSourceError(
                        "revalidation_evaluation_source_conflict",
                        "同一 Fresh Evaluation Plan 已绑定不同 Source Snapshot。",
                    )
                await self.verify_blobs(restored)
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_evaluation_sources "
                "(snapshot_id, snapshot_sha256, plan_id, snapshot_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    artifact.snapshot_id,
                    artifact.snapshot_sha256,
                    artifact.plan_id,
                    artifact.model_dump_json(),
                    artifact.created_at,
                ),
            )
            await db.commit()
        return artifact

    async def get(
        self, snapshot_id: str
    ) -> EvolutionRevalidationEvaluationSourceSnapshot | None:
        if re.fullmatch(r"evrevalsrc_[0-9a-f]{24}", str(snapshot_id)) is None:
            raise ValueError("Evaluation Source Snapshot ID 格式无效。")
        if not self._db_path.exists():
            return None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT snapshot_json FROM evolution_revalidation_evaluation_sources "
                    "WHERE snapshot_id = ?",
                    (snapshot_id,),
                )
            ).fetchone()
        return (
            None
            if row is None
            else EvolutionRevalidationEvaluationSourceSnapshot.model_validate_json(
                row["snapshot_json"]
            )
        )

    async def get_by_plan(
        self, plan_id: str
    ) -> EvolutionRevalidationEvaluationSourceSnapshot | None:
        if re.fullmatch(r"evrevalplan_[0-9a-f]{24}", str(plan_id)) is None:
            raise ValueError("Fresh Evaluation Plan ID 格式无效。")
        if not self._db_path.exists():
            return None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT snapshot_json FROM evolution_revalidation_evaluation_sources "
                    "WHERE plan_id = ?",
                    (plan_id,),
                )
            ).fetchone()
        return (
            None
            if row is None
            else EvolutionRevalidationEvaluationSourceSnapshot.model_validate_json(
                row["snapshot_json"]
            )
        )

    async def persist_blobs(
        self, overlays: tuple[HarnessSandboxSourceOverlay, ...]
    ) -> tuple[EvolutionRevalidationEvaluationSourceBlob, ...]:
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        blob_dir = self._storage_dir / "blobs"
        blob_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        normalized = tuple(sorted(overlays, key=lambda item: item.path))
        result = []
        for order, overlay in enumerate(normalized, 1):
            if len(overlay.content) > _MAX_BLOB_BYTES:
                raise EvolutionRevalidationEvaluationSourceError(
                    "revalidation_evaluation_source_blob_oversized",
                    f"Evaluation Source 文件超过 2 MiB：{overlay.path}",
                )
            digest = hashlib.sha256(overlay.content).hexdigest()
            if not hmac.compare_digest(digest, overlay.sha256):
                raise EvolutionRevalidationEvaluationSourceError(
                    "revalidation_evaluation_source_blob_digest_mismatch",
                    f"Evaluation Source 文件摘要不一致：{overlay.path}",
                )
            destination = blob_dir / digest
            if destination.exists() or destination.is_symlink():
                if _read_regular_file(destination) != overlay.content:
                    raise EvolutionRevalidationEvaluationSourceError(
                        "revalidation_evaluation_source_blob_conflict",
                        "Content-addressed Evaluation Source blob 已存在不同内容。",
                    )
            else:
                _atomic_write_read_only(destination, overlay.content)
            result.append(
                EvolutionRevalidationEvaluationSourceBlob(
                    order=order,
                    path=overlay.path,
                    sha256=digest,
                    size_bytes=len(overlay.content),
                    executable=overlay.executable,
                    storage_key=f"blobs/{digest}",
                )
            )
        return tuple(result)

    async def verify_blobs(
        self, snapshot: EvolutionRevalidationEvaluationSourceSnapshot
    ) -> None:
        for blob in snapshot.blobs:
            content = _read_regular_file(self._storage_dir / blob.storage_key)
            if len(content) != blob.size_bytes or not hmac.compare_digest(
                hashlib.sha256(content).hexdigest(), blob.sha256
            ):
                raise EvolutionRevalidationEvaluationSourceError(
                    "revalidation_evaluation_source_blob_corrupt",
                    f"Evaluation Source blob 损坏：{blob.path}",
                )

    async def load_overlays(
        self, snapshot_id: str
    ) -> tuple[HarnessSandboxSourceOverlay, ...]:
        snapshot = await self.get(snapshot_id)
        if snapshot is None:
            raise EvolutionRevalidationEvaluationSourceError(
                "revalidation_evaluation_source_not_found",
                "Evaluation Source Snapshot 不存在。",
            )
        await self.verify_blobs(snapshot)
        return tuple(
            HarnessSandboxSourceOverlay(
                path=item.path,
                content=_read_regular_file(self._storage_dir / item.storage_key),
                sha256=item.sha256,
                executable=item.executable,
            )
            for item in snapshot.blobs
        )


class EvolutionRevalidationEvaluationSourceService:
    def __init__(
        self,
        *,
        plan_service: EvolutionRevalidationEvaluationPlanService,
        outcome_store: EvolutionRevalidationOutcomeStore,
        source_provider: EvolutionRevalidationSourceProvider,
        store: EvolutionRevalidationEvaluationSourceStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._plan_service = plan_service
        self._outcome_store = outcome_store
        self._source_provider = source_provider
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    async def capture(
        self, *, workspace_root: str | Path, plan_id: str
    ) -> EvolutionRevalidationEvaluationSourceSnapshot:
        existing = await self._store.get_by_plan(plan_id)
        if existing is not None:
            await self._store.verify_blobs(existing)
            return existing
        plan_view = await self._plan_service.inspect(
            workspace_root=workspace_root,
            plan_id=plan_id,
        )
        if not plan_view.execution_eligible:
            raise EvolutionRevalidationEvaluationSourceError(
                "revalidation_evaluation_source_plan_stale",
                "Fresh Evaluation Plan 已 stale，不能捕获评测源码。",
            )
        plan = plan_view.plan
        outcome = await self._outcome_store.get(plan.outcome_id)
        if outcome is None or outcome.outcome_sha256 != plan.outcome_sha256:
            raise EvolutionRevalidationEvaluationSourceError(
                "revalidation_evaluation_source_outcome_mismatch",
                "Revalidation Outcome 不存在或 digest 不一致。",
            )
        source, harness_plan = await self._source_provider.materialize_current_source(
            workspace_root=workspace_root,
            request_id=outcome.request_id,
        )
        if not (
            source.revision == plan.target_head
            and source.revision_tree_sha256 == plan.target_tree_sha256
            and source.overlay_source_sha256 == plan.overlay_source_sha256
        ):
            raise EvolutionRevalidationEvaluationSourceError(
                "revalidation_evaluation_source_materialization_mismatch",
                "重新物化源码与 Fresh Evaluation Plan 不一致。",
            )
        blobs = await self._store.persist_blobs(source.overlays)
        if _overlay_set_sha256(blobs) != source.overlay_source_sha256:
            raise EvolutionRevalidationEvaluationSourceError(
                "revalidation_evaluation_source_manifest_mismatch",
                "持久化 blob manifest 与 revalidation overlay 不一致。",
            )
        now = self._clock()
        if now.utcoffset() is None:
            raise EvolutionRevalidationEvaluationSourceError(
                "revalidation_evaluation_source_clock_invalid",
                "Evaluation Source Snapshot 时钟必须包含 UTC offset。",
            )
        payload = {
            "schema_version": 1,
            "policy_version": EVOLUTION_REVALIDATION_EVALUATION_SOURCE_POLICY,
            "plan_id": plan.plan_id,
            "plan_sha256": plan.plan_sha256,
            "outcome_id": outcome.outcome_id,
            "outcome_sha256": outcome.outcome_sha256,
            "request_id": outcome.request_id,
            "validation_receipt_id": outcome.validation_receipt_id,
            "validation_receipt_sha256": outcome.validation_receipt_sha256,
            "target_head": source.revision,
            "target_tree_sha256": source.revision_tree_sha256,
            "overlay_source_sha256": source.overlay_source_sha256,
            "harness_profile_sha256": harness_plan.profile_sha256,
            "harness_plan_sha256": harness_plan.plan_sha256,
            "blobs": [item.model_dump(mode="json") for item in blobs],
            "blob_set_sha256": _sha256_payload(
                [item.model_dump(mode="json") for item in blobs]
            ),
            "total_bytes": sum(item.size_bytes for item in blobs),
            "content_addressed": True,
            "immutable_source_complete": True,
            "evaluation_execution_started": False,
            "promotion_authority": False,
            "created_at": now.isoformat(),
        }
        digest = _sha256_payload(payload)
        snapshot = EvolutionRevalidationEvaluationSourceSnapshot.model_validate(
            {
                **payload,
                "snapshot_id": f"evrevalsrc_{digest[:24]}",
                "snapshot_sha256": digest,
            }
        )
        return await self._store.record(snapshot)


def render_evolution_revalidation_evaluation_source(
    item: EvolutionRevalidationEvaluationSourceSnapshot,
) -> str:
    return "\n".join(
        [
            f"# Evolution Evaluation Source `{item.snapshot_id}`",
            "",
            f"- Fresh Evaluation Plan：`{item.plan_id}`",
            f"- Target：`{item.target_head}`",
            f"- Overlay source：`{item.overlay_source_sha256}`",
            f"- Immutable blobs：{len(item.blobs)} / {item.total_bytes} bytes",
            "- Content addressed / complete：`true` / `true`",
            "- Evaluation execution / Promotion authority：`false` / `false`",
        ]
    )


def _atomic_write_read_only(path: Path, content: bytes) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".blob-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o400)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _read_regular_file(path: Path) -> bytes:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise EvolutionRevalidationEvaluationSourceError(
            "revalidation_evaluation_source_blob_unreadable",
            "Evaluation Source blob 不存在、是 symlink 或无法读取。",
        ) from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise EvolutionRevalidationEvaluationSourceError(
            "revalidation_evaluation_source_blob_unreadable",
            "Evaluation Source blob 不存在、是 symlink 或无法读取。",
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise EvolutionRevalidationEvaluationSourceError(
            "revalidation_evaluation_source_blob_unreadable",
            "Evaluation Source blob 不存在、是 symlink 或无法读取。",
        ) from exc
    try:
        info = os.fstat(fd)
        try:
            after = os.lstat(path)
        except OSError as exc:
            raise EvolutionRevalidationEvaluationSourceError(
                "revalidation_evaluation_source_blob_replaced",
                "Evaluation Source blob 在读取期间被替换。",
            ) from exc
        if not (
            stat.S_ISREG(info.st_mode)
            and os.path.samestat(before, info)
            and os.path.samestat(after, info)
        ):
            raise EvolutionRevalidationEvaluationSourceError(
                "revalidation_evaluation_source_blob_replaced",
                "Evaluation Source blob 在读取期间被替换或链接。",
            )
        if info.st_size > _MAX_BLOB_BYTES:
            raise EvolutionRevalidationEvaluationSourceError(
                "revalidation_evaluation_source_blob_unsafe",
                "Evaluation Source blob 不是安全 regular file。",
            )
        chunks = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1_024))
            if not chunk:
                raise EvolutionRevalidationEvaluationSourceError(
                    "revalidation_evaluation_source_blob_truncated",
                    "Evaluation Source blob 读取时被截断。",
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _overlay_set_sha256(
    blobs: tuple[EvolutionRevalidationEvaluationSourceBlob, ...]
) -> str:
    return _sha256_payload(
        [
            {
                "path": item.path,
                "sha256": item.sha256,
                "executable": item.executable,
            }
            for item in blobs
        ]
    )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_evaluation_sources ("
        "snapshot_id TEXT PRIMARY KEY, snapshot_sha256 TEXT NOT NULL UNIQUE, "
        "plan_id TEXT NOT NULL UNIQUE, snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    await db.commit()


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_EVALUATION_SOURCE_POLICY",
    "EvolutionRevalidationEvaluationSourceBlob",
    "EvolutionRevalidationEvaluationSourceError",
    "EvolutionRevalidationEvaluationSourceService",
    "EvolutionRevalidationEvaluationSourceSnapshot",
    "EvolutionRevalidationEvaluationSourceStore",
    "EvolutionRevalidationSourceProvider",
    "render_evolution_revalidation_evaluation_source",
]
