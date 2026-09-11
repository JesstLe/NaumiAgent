"""Bounded mark-and-sweep retention for immutable Web2 output assets."""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from naumi_agent.tools.base import Tool, ToolMetadata
from naumi_agent.tools.output_publish import (
    ASSET_NAME,
    ASSET_STORAGE_LOCK,
    asset_root,
)

DEFAULT_MINIMUM_AGE_SECONDS = 3600
DEFAULT_DELETE_LIMIT = 100
MAX_DELETE_LIMIT = 500
MAX_ASSET_SCAN = 50_000
MAX_DIRECTORY_ENTRIES = 100_000
MAX_REFERENCE_ROWS = 50_000
MAXIMUM_AGE_SECONDS = 604800
_REFERENCE = re.compile(
    r"/api/v1/output-assets/"
    r"([a-f0-9]{64}\.(?:svg|png|jpg|webp|gif|pdf|csv|json|txt|md))"
)


@dataclass(frozen=True, slots=True)
class OutputAssetCandidate:
    name: str
    size_bytes: int
    modified_at: float
    modified_at_ns: int


@dataclass(frozen=True, slots=True)
class OutputAssetRetentionPreview:
    total_assets: int
    total_bytes: int
    referenced_assets: int
    referenced_bytes: int
    orphan_candidates: tuple[OutputAssetCandidate, ...]
    deferred_recent: int
    deferred_recent_bytes: int
    unmanaged_entries: int
    unsafe_entries: int
    reference_rows_scanned: int
    scan_complete: bool

    @property
    def orphan_bytes(self) -> int:
        return sum(item.size_bytes for item in self.orphan_candidates)


@dataclass(frozen=True, slots=True)
class OutputAssetRetentionResult:
    preview: OutputAssetRetentionPreview
    deleted_names: tuple[str, ...]
    deleted_bytes: int
    skipped_changed: int
    errors: tuple[str, ...]


def _extract_references(values: tuple[Any, ...]) -> set[str]:
    references: set[str] = set()
    for value in values:
        if value is None:
            continue
        references.update(_REFERENCE.findall(str(value)))
    return references


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _scan_query(
    db: sqlite3.Connection,
    query: str,
    *,
    remaining_rows: int,
) -> tuple[set[str], int, bool]:
    if remaining_rows <= 0:
        return set(), 0, False
    try:
        rows = db.execute(query, (remaining_rows + 1,)).fetchall()
    except sqlite3.DatabaseError:
        return set(), 0, False
    complete = len(rows) <= remaining_rows
    selected = rows[:remaining_rows]
    references: set[str] = set()
    for row in selected:
        references.update(_extract_references(tuple(row)))
    return references, len(selected), complete


def scan_persisted_output_references(
    session_db_path: Path,
    chat_run_db_path: Path,
    *,
    row_limit: int = MAX_REFERENCE_ROWS,
) -> tuple[set[str], int, bool]:
    """Collect asset names from durable session and run text without decoding JSON."""
    references: set[str] = set()
    scanned = 0
    complete = True
    databases = (
        (
            session_db_path,
            (
                (
                    "sessions",
                    "SELECT messages, summary FROM sessions "
                    "WHERE instr(messages, '/api/v1/output-assets/') > 0 "
                    "OR instr(summary, '/api/v1/output-assets/') > 0 LIMIT ?",
                ),
            ),
        ),
        (
            chat_run_db_path,
            (
                (
                    "chat_runs",
                    "SELECT receipt_json FROM chat_runs "
                    "WHERE instr(receipt_json, '/api/v1/output-assets/') > 0 LIMIT ?",
                ),
                (
                    "chat_run_steps",
                    "SELECT summary, detail, metadata_json FROM chat_run_steps "
                    "WHERE instr(summary, '/api/v1/output-assets/') > 0 "
                    "OR instr(detail, '/api/v1/output-assets/') > 0 "
                    "OR instr(metadata_json, '/api/v1/output-assets/') > 0 LIMIT ?",
                ),
                (
                    "chat_run_artifacts",
                    "SELECT title, summary_json, metadata_json FROM chat_run_artifacts "
                    "WHERE instr(title, '/api/v1/output-assets/') > 0 "
                    "OR instr(summary_json, '/api/v1/output-assets/') > 0 "
                    "OR instr(metadata_json, '/api/v1/output-assets/') > 0 LIMIT ?",
                ),
                (
                    "chat_sources",
                    "SELECT title, path, metadata_json FROM chat_sources "
                    "WHERE instr(title, '/api/v1/output-assets/') > 0 "
                    "OR instr(path, '/api/v1/output-assets/') > 0 "
                    "OR instr(metadata_json, '/api/v1/output-assets/') > 0 LIMIT ?",
                ),
            ),
        ),
    )
    for path, queries in databases:
        if not path.is_file():
            continue
        try:
            with sqlite3.connect(str(path), timeout=5) as db:
                for table, query in queries:
                    if not _table_exists(db, table):
                        continue
                    found, count, query_complete = _scan_query(
                        db,
                        query,
                        remaining_rows=row_limit - scanned,
                    )
                    references.update(found)
                    scanned += count
                    complete = complete and query_complete
                    if not query_complete or scanned >= row_limit:
                        complete = False
                        return references, scanned, complete
        except sqlite3.DatabaseError:
            complete = False
    return references, scanned, complete


class OutputAssetRetention:
    """Inspect and delete only old, unreferenced, managed output assets."""

    def __init__(
        self,
        *,
        directory: Path,
        session_db_path: Path,
        chat_run_db_path: Path,
    ) -> None:
        self.directory = directory.resolve()
        self.session_db_path = session_db_path.resolve()
        self.chat_run_db_path = chat_run_db_path.resolve()

    def preview(
        self,
        *,
        minimum_age_seconds: int = DEFAULT_MINIMUM_AGE_SECONDS,
        now: float | None = None,
    ) -> OutputAssetRetentionPreview:
        if (
            isinstance(minimum_age_seconds, bool)
            or not isinstance(minimum_age_seconds, int)
            or not 60 <= minimum_age_seconds <= MAXIMUM_AGE_SECONDS
        ):
            raise ValueError("资源最小保留时间必须在 60 到 604800 秒之间")
        timestamp = time.time() if now is None else now
        with ASSET_STORAGE_LOCK:
            return self._preview_locked(
                minimum_age_seconds=minimum_age_seconds,
                now=timestamp,
            )

    def _preview_locked(
        self,
        *,
        minimum_age_seconds: int,
        now: float,
    ) -> OutputAssetRetentionPreview:
        references, reference_rows, references_complete = (
            scan_persisted_output_references(
                self.session_db_path,
                self.chat_run_db_path,
            )
        )
        if not self.directory.exists():
            return OutputAssetRetentionPreview(
                total_assets=0,
                total_bytes=0,
                referenced_assets=0,
                referenced_bytes=0,
                orphan_candidates=(),
                deferred_recent=0,
                deferred_recent_bytes=0,
                unmanaged_entries=0,
                unsafe_entries=0,
                reference_rows_scanned=reference_rows,
                scan_complete=references_complete,
            )

        total_assets = total_bytes = referenced_assets = referenced_bytes = 0
        deferred_recent = deferred_recent_bytes = 0
        unmanaged_entries = unsafe_entries = 0
        candidates: list[OutputAssetCandidate] = []
        assets_scanned = 0
        assets_complete = True
        try:
            with os.scandir(self.directory) as entries:
                for entry_index, entry in enumerate(entries):
                    if entry_index >= MAX_DIRECTORY_ENTRIES:
                        assets_complete = False
                        break
                    if not ASSET_NAME.fullmatch(entry.name):
                        if not entry.name.startswith((".publish-", ".asset-store-lock")):
                            unmanaged_entries += 1
                        continue
                    if assets_scanned >= MAX_ASSET_SCAN:
                        assets_complete = False
                        break
                    assets_scanned += 1
                    try:
                        unsafe = entry.is_symlink() or not entry.is_file(
                            follow_symlinks=False
                        )
                    except OSError:
                        unsafe = True
                    if unsafe:
                        unsafe_entries += 1
                        continue
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        unsafe_entries += 1
                        continue
                    total_assets += 1
                    total_bytes += stat.st_size
                    if entry.name in references:
                        referenced_assets += 1
                        referenced_bytes += stat.st_size
                    elif now - stat.st_mtime < minimum_age_seconds:
                        deferred_recent += 1
                        deferred_recent_bytes += stat.st_size
                    else:
                        candidates.append(
                            OutputAssetCandidate(
                                entry.name,
                                stat.st_size,
                                stat.st_mtime,
                                stat.st_mtime_ns,
                            )
                        )
        except OSError as exc:
            raise RuntimeError("输出资源目录无法读取，请检查目录权限") from exc
        candidates.sort(key=lambda item: (item.modified_at, item.name))
        return OutputAssetRetentionPreview(
            total_assets=total_assets,
            total_bytes=total_bytes,
            referenced_assets=referenced_assets,
            referenced_bytes=referenced_bytes,
            orphan_candidates=tuple(candidates),
            deferred_recent=deferred_recent,
            deferred_recent_bytes=deferred_recent_bytes,
            unmanaged_entries=unmanaged_entries,
            unsafe_entries=unsafe_entries,
            reference_rows_scanned=reference_rows,
            scan_complete=references_complete and assets_complete,
        )

    def run(
        self,
        *,
        minimum_age_seconds: int = DEFAULT_MINIMUM_AGE_SECONDS,
        delete_limit: int = DEFAULT_DELETE_LIMIT,
        now: float | None = None,
    ) -> OutputAssetRetentionResult:
        if (
            isinstance(delete_limit, bool)
            or not isinstance(delete_limit, int)
            or not 1 <= delete_limit <= MAX_DELETE_LIMIT
        ):
            raise ValueError(f"单次清理数量必须在 1 到 {MAX_DELETE_LIMIT} 之间")
        timestamp = time.time() if now is None else now
        with ASSET_STORAGE_LOCK:
            preview = self._preview_locked(
                minimum_age_seconds=minimum_age_seconds,
                now=timestamp,
            )
            if not preview.scan_complete:
                return OutputAssetRetentionResult(
                    preview=preview,
                    deleted_names=(),
                    deleted_bytes=0,
                    skipped_changed=0,
                    errors=("引用或资源扫描达到上限，为避免误删，本轮未执行清理。",),
                )
            deleted: list[str] = []
            deleted_bytes = skipped_changed = 0
            errors: list[str] = []
            for candidate in preview.orphan_candidates[:delete_limit]:
                path = self.directory / candidate.name
                try:
                    if path.is_symlink() or not path.is_file():
                        skipped_changed += 1
                        continue
                    stat = path.stat()
                    if (
                        stat.st_size != candidate.size_bytes
                        or stat.st_mtime_ns != candidate.modified_at_ns
                        or timestamp - stat.st_mtime < minimum_age_seconds
                    ):
                        skipped_changed += 1
                        continue
                    path.unlink()
                except OSError as exc:
                    errors.append(f"{candidate.name}: {exc.strerror or type(exc).__name__}")
                    continue
                deleted.append(candidate.name)
                deleted_bytes += candidate.size_bytes
            return OutputAssetRetentionResult(
                preview=preview,
                deleted_names=tuple(deleted),
                deleted_bytes=deleted_bytes,
                skipped_changed=skipped_changed,
                errors=tuple(errors),
            )


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / 1024 / 1024:.1f} MB"


def render_output_asset_preview(preview: OutputAssetRetentionPreview) -> str:
    status = "完整" if preview.scan_complete else "达到扫描上限"
    lines = [
        "## 输出资源清理预览",
        f"- 扫描状态：{status}",
        f"- 已管理资源：{preview.total_assets} 个（{_format_bytes(preview.total_bytes)}）",
        (
            f"- 持久化引用保护：{preview.referenced_assets} 个"
            f"（{_format_bytes(preview.referenced_bytes)}）"
        ),
        (
            f"- 可清理孤儿资源：{len(preview.orphan_candidates)} 个"
            f"（{_format_bytes(preview.orphan_bytes)}）"
        ),
        (
            f"- 新近资源延迟清理：{preview.deferred_recent} 个"
            f"（{_format_bytes(preview.deferred_recent_bytes)}）"
        ),
        f"- 已扫描引用记录：{preview.reference_rows_scanned} 条",
    ]
    if preview.unmanaged_entries:
        lines.append(f"- 非托管条目：{preview.unmanaged_entries} 个（未触碰）")
    if preview.unsafe_entries:
        lines.append(f"- 不安全条目：{preview.unsafe_entries} 个（未触碰）")
    if preview.orphan_candidates:
        lines.append("\n候选资源（最多显示 20 个）：")
        lines.extend(
            f"- `{item.name}` · {_format_bytes(item.size_bytes)}"
            for item in preview.orphan_candidates[:20]
        )
    if not preview.scan_complete:
        lines.append("\n扫描不完整时清理会拒绝执行，避免遗漏引用后误删资源。")
    return "\n".join(lines)


def render_output_asset_result(result: OutputAssetRetentionResult) -> str:
    lines = [
        "## 输出资源清理结果",
        f"- 已删除：{len(result.deleted_names)} 个（{_format_bytes(result.deleted_bytes)}）",
        f"- 状态变化跳过：{result.skipped_changed} 个",
        f"- 删除失败：{len(result.errors)} 个",
        (
            f"- 清理前孤儿资源：{len(result.preview.orphan_candidates)} 个"
            f"（{_format_bytes(result.preview.orphan_bytes)}）"
        ),
    ]
    if result.errors:
        lines.append("\n未完成项目：")
        lines.extend(f"- {error}" for error in result.errors[:20])
    return "\n".join(lines)


def _retention_for_engine(engine: Any) -> OutputAssetRetention:
    config = engine.config
    session_db = Path(config.memory.session_db_path)
    chat_store = getattr(engine, "chat_run_store", None)
    chat_db = Path(getattr(chat_store, "db_path", session_db.parent / "chat-runs.db"))
    return OutputAssetRetention(
        directory=asset_root(config),
        session_db_path=session_db,
        chat_run_db_path=chat_db,
    )


class OutputAssetRetentionPreviewTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "output_asset_retention_preview"

    @property
    def description(self) -> str:
        return "只读扫描输出资源引用，预览可安全清理的孤儿图片和文件。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "minimum_age_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 604800,
                    "default": DEFAULT_MINIMUM_AGE_SECONDS,
                    "description": "未引用资源至少保留多久，默认一小时。",
                }
            },
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="预览输出资源清理",
            search_hint="output assets retention preview orphan 图片 文件 清理预览",
        )

    async def execute(
        self,
        minimum_age_seconds: int = DEFAULT_MINIMUM_AGE_SECONDS,
    ) -> str:
        preview = await asyncio.to_thread(
            _retention_for_engine(self._engine).preview,
            minimum_age_seconds=minimum_age_seconds,
        )
        return render_output_asset_preview(preview)


class OutputAssetRetentionRunTool(Tool):
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "output_asset_retention_run"

    @property
    def description(self) -> str:
        return "执行一轮有界输出资源清理，只删除旧的、无持久化引用的托管文件。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "minimum_age_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 604800,
                    "default": DEFAULT_MINIMUM_AGE_SECONDS,
                },
                "delete_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_DELETE_LIMIT,
                    "default": DEFAULT_DELETE_LIMIT,
                },
            },
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            requires_confirmation=True,
            concurrency_safe=False,
            path_argument_names=(),
            command_argument_names=(),
            user_facing_name="清理输出资源",
            search_hint="output assets retention run orphan 图片 文件 清理",
        )

    async def execute(
        self,
        minimum_age_seconds: int = DEFAULT_MINIMUM_AGE_SECONDS,
        delete_limit: int = DEFAULT_DELETE_LIMIT,
    ) -> str:
        result = await asyncio.to_thread(
            _retention_for_engine(self._engine).run,
            minimum_age_seconds=minimum_age_seconds,
            delete_limit=delete_limit,
        )
        return render_output_asset_result(result)
