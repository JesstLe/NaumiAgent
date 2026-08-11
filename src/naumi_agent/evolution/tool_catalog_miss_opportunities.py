"""Durable exact Tool Catalog misses and revocable Evolution opportunities."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field

from naumi_agent.evolution.candidate import EvolutionCandidateDraft, build_candidate_draft
from naumi_agent.evolution.evidence import EvolutionEvidence, EvolutionEvidenceRef
from naumi_agent.evolution.store import (
    EvolutionCandidateStore,
    EvolutionStoreCorruptionError,
    EvolutionStoreError,
)

EVOLUTION_TOOL_CATALOG_MISS_POLICY = "evolution-tool-catalog-miss-v1"
_MISS_ID_RE = re.compile(r"^tsm_[0-9a-f]{24}$")
_MISS_URI_RE = re.compile(r"^tool-search://misses/(tsm_[0-9a-f]{24})$")
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IGNORED_CATALOG_TOOLS = frozenset({"tool_search"})


class _ToolCatalog(Protocol):
    @property
    def names(self) -> list[str]: ...


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class ToolCatalogMissRecord(_StrictModel):
    schema_version: Literal[1] = 1
    miss_id: str = Field(pattern=r"^tsm_[0-9a-f]{24}$")
    workspace_root: str = Field(min_length=1, max_length=4096)
    requested_name: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: str = Field(min_length=20, max_length=64)


class StoredToolCatalogMiss(_StrictModel):
    record: ToolCatalogMissRecord
    record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvolutionToolCatalogMissOpportunityResult(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-tool-catalog-miss-v1"] = (
        EVOLUTION_TOOL_CATALOG_MISS_POLICY
    )
    status: Literal["recorded"] = "recorded"
    miss_id: str = Field(pattern=r"^tsm_[0-9a-f]{24}$")
    requested_name: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    occurrence_count: int = Field(ge=1, le=10_000)
    evidence_id: str = Field(pattern=r"^eve_[0-9a-f]{24}$")
    source_authority_valid: Literal[True] = True
    experiment_eligible: Literal[False] = False
    promotion_authority: Literal[False] = False


class EvolutionToolCatalogMissOpportunityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ToolCatalogMissStore:
    """Persist immutable exact-name misses in the user-owned Evolution DB."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def record(
        self,
        workspace_root: str | Path,
        *,
        requested_name: str,
        catalog_sha256: str,
    ) -> StoredToolCatalogMiss:
        workspace = _canonical_workspace(workspace_root)
        normalized_name = normalize_missing_tool_name(requested_name)
        normalized_catalog = _catalog_sha256(catalog_sha256)
        miss_id = _miss_id(workspace, normalized_name, normalized_catalog)
        await self._ensure_schema()
        try:
            async with self._write_lock, self._connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                existing = await self._read_row(db, workspace, miss_id)
                if existing is None:
                    record = ToolCatalogMissRecord(
                        miss_id=miss_id,
                        workspace_root=workspace,
                        requested_name=normalized_name,
                        catalog_sha256=normalized_catalog,
                        observed_at=_timestamp(self._clock()),
                    )
                    payload, digest = _model_payload(record)
                    await db.execute(
                        """
                        INSERT INTO evolution_tool_catalog_misses (
                            workspace_root, miss_id, requested_name, catalog_sha256,
                            observed_at, record_json, record_sha256
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            workspace,
                            miss_id,
                            normalized_name,
                            normalized_catalog,
                            record.observed_at,
                            payload,
                            digest,
                        ),
                    )
                    stored = StoredToolCatalogMiss(
                        record=record,
                        record_sha256=digest,
                    )
                else:
                    stored = _stored_from_row(existing)
                await db.commit()
                return stored
        except EvolutionStoreError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise EvolutionStoreError("无法保存 Tool Catalog 缺失事实。") from exc

    async def get(
        self,
        workspace_root: str | Path,
        miss_id: str,
    ) -> StoredToolCatalogMiss | None:
        workspace = _canonical_workspace(workspace_root)
        normalized_id = _miss_id_value(miss_id)
        if not self._db_path.is_file():
            return None
        try:
            async with self._connection() as db:
                row = await self._read_row(db, workspace, normalized_id)
                return None if row is None else _stored_from_row(row)
        except aiosqlite.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise EvolutionStoreError("无法读取 Tool Catalog 缺失事实。") from exc
        except EvolutionStoreError:
            raise
        except (aiosqlite.Error, OSError, ValueError) as exc:
            raise EvolutionStoreError("无法读取 Tool Catalog 缺失事实。") from exc

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            try:
                self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _restrict_permissions(self._db_path.parent, 0o700)
                async with self._connection() as db:
                    await db.executescript(_MISS_SCHEMA)
                    await db.commit()
                _restrict_permissions(self._db_path, 0o600)
            except (aiosqlite.Error, OSError) as exc:
                raise EvolutionStoreError(
                    "无法初始化 Tool Catalog Miss Store。"
                ) from exc
            self._schema_ready = True

    async def _read_row(
        self,
        db: aiosqlite.Connection,
        workspace: str,
        miss_id: str,
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            """
            SELECT * FROM evolution_tool_catalog_misses
            WHERE workspace_root = ? AND miss_id = ?
            """,
            (workspace, miss_id),
        )
        return await cursor.fetchone()

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(self._db_path, timeout=5.0)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA busy_timeout = 5000")
            yield db
        finally:
            await db.close()


class EvolutionToolCatalogMissOpportunityService:
    """Project current exact-name catalog misses into revocable Candidates."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        miss_store: ToolCatalogMissStore,
        tool_catalog: _ToolCatalog,
        candidate_store: EvolutionCandidateStore,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        if not isinstance(miss_store, ToolCatalogMissStore):
            raise TypeError("miss_store 必须是 ToolCatalogMissStore 实例。")
        if not isinstance(candidate_store, EvolutionCandidateStore):
            raise TypeError("candidate_store 必须是 EvolutionCandidateStore 实例。")
        if not isinstance(getattr(tool_catalog, "names", None), list):
            raise TypeError("tool_catalog 必须提供工具名目录。")
        self.miss_store = miss_store
        self.tool_catalog = tool_catalog
        self.candidate_store = candidate_store

    async def discover(
        self,
        *,
        miss_id: str,
    ) -> EvolutionToolCatalogMissOpportunityResult:
        stored = await self._load_current_miss(miss_id)
        evidence = adapt_tool_catalog_miss_evidence(stored)
        candidate = await self.candidate_store.upsert_candidate(
            self.workspace_root,
            build_candidate_draft((evidence,)),
        )
        return EvolutionToolCatalogMissOpportunityResult(
            miss_id=stored.record.miss_id,
            requested_name=stored.record.requested_name,
            catalog_sha256=stored.record.catalog_sha256,
            candidate_id=candidate.draft.candidate_id,
            candidate_revision=candidate.revision,
            occurrence_count=candidate.draft.occurrence_count,
            evidence_id=evidence.evidence_id,
        )

    async def validate_candidate_sources(
        self,
        candidate: EvolutionCandidateDraft,
    ) -> bool:
        selected = tuple(
            item
            for item in candidate.evidence
            if item.source_kind == "tool_catalog_miss"
        )
        if not selected:
            return True
        for expected in selected:
            match = _MISS_URI_RE.fullmatch(expected.source_uri)
            if match is None:
                return False
            try:
                stored = await self._load_current_miss(match.group(1))
                actual = adapt_tool_catalog_miss_evidence(stored)
            except (EvolutionStoreError, EvolutionToolCatalogMissOpportunityError):
                return False
            if actual != expected:
                return False
        return True

    async def _load_current_miss(self, miss_id: str) -> StoredToolCatalogMiss:
        try:
            stored = await self.miss_store.get(self.workspace_root, miss_id)
        except (EvolutionStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionToolCatalogMissOpportunityError(
                "tool_catalog_miss_source_unavailable",
                "Tool Catalog 缺失事实损坏或无法读取。",
            ) from exc
        if stored is None:
            raise EvolutionToolCatalogMissOpportunityError(
                "tool_catalog_miss_source_unavailable",
                "当前工作区不存在该 Tool Catalog 缺失事实。",
            )
        if tool_name_is_available(
            self.tool_catalog.names,
            stored.record.requested_name,
        ):
            raise EvolutionToolCatalogMissOpportunityError(
                "tool_catalog_miss_satisfied",
                "目标工具已经存在，缺失能力 authority 已撤销。",
            )
        current_catalog = tool_catalog_sha256(self.tool_catalog.names)
        if current_catalog != stored.record.catalog_sha256:
            raise EvolutionToolCatalogMissOpportunityError(
                "tool_catalog_miss_catalog_changed",
                "工具目录已经变化；请重新执行精确 Tool Search 生成当前缺失事实。",
            )
        return stored


def adapt_tool_catalog_miss_evidence(
    stored: StoredToolCatalogMiss,
) -> EvolutionEvidence:
    if not isinstance(stored, StoredToolCatalogMiss):
        raise TypeError("Tool Catalog miss adapter 只接受已验证的 durable record。")
    record = stored.record
    finding_code = "missing_tool_capability"
    scope = f"capability:tool:{record.requested_name}"
    root_fingerprint = _digest({
        "finding_code": finding_code,
        "requested_name": record.requested_name,
        "scope": scope,
    })
    evidence_sha256 = _digest({
        "miss_id": record.miss_id,
        "record_sha256": stored.record_sha256,
        "root_fingerprint": root_fingerprint,
    })
    uri = f"tool-search://misses/{record.miss_id}"
    return EvolutionEvidence(
        evidence_id=f"eve_{evidence_sha256[:24]}",
        source_kind="tool_catalog_miss",
        source_uri=uri,
        observed_at=record.observed_at,
        finding_code=finding_code,
        scope=scope,
        root_fingerprint=root_fingerprint,
        refs=(EvolutionEvidenceRef(uri=uri, sha256=stored.record_sha256),),
    )


def render_tool_catalog_miss_opportunity(
    result: EvolutionToolCatalogMissOpportunityResult,
) -> str:
    return "\n".join([
        "# Tool Catalog 缺失能力已进入机会发现",
        "",
        f"- 缺失工具：`{result.requested_name}`",
        f"- Miss：`{result.miss_id}`",
        f"- Candidate：`{result.candidate_id}` · revision {result.candidate_revision}",
        f"- Evidence：`{result.evidence_id}`",
        "- 来源 authority：当前完整 Tool Catalog 与 miss 记录一致，目标工具仍不存在",
        "- 隐私：只保存安全工具标识符，不保存自然语言查询或会话正文",
        "- 状态：只允许人工 Review；未授予实验或推广权限",
        "",
        f"下一步：`/evolution detail {result.candidate_id}`",
    ])


def normalize_missing_tool_name(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("缺失工具名必须是字符串。")
    normalized = value.strip().lower()
    if _TOOL_NAME_RE.fullmatch(normalized) is None:
        raise ValueError(
            "只有安全的精确工具标识符才能形成 durable miss；"
            "格式为字母开头的 1..128 位字母、数字、_、.、:、-。"
        )
    return normalized


def tool_catalog_sha256(names: Iterable[str]) -> str:
    normalized = sorted({
        str(name).strip().lower()
        for name in names
        if str(name).strip().lower() not in _IGNORED_CATALOG_TOOLS
    })
    if any(_TOOL_NAME_RE.fullmatch(name) is None for name in normalized):
        raise ValueError("Tool Catalog 包含无效工具标识符。")
    return _digest({"tool_names": normalized})


def tool_name_is_available(names: Iterable[str], requested_name: str) -> bool:
    requested = normalize_missing_tool_name(requested_name)
    requested_normalized = _normalized_tool_name(requested)
    return any(
        str(name).strip().lower() == requested
        or _normalized_tool_name(str(name)) == requested_normalized
        for name in names
    )


def _stored_from_row(row: aiosqlite.Row) -> StoredToolCatalogMiss:
    payload = str(row["record_json"])
    digest = str(row["record_sha256"])
    try:
        record = ToolCatalogMissRecord.model_validate_json(payload)
    except ValueError as exc:
        raise EvolutionStoreCorruptionError(
            "Tool Catalog miss JSON 无效。"
        ) from exc
    if (
        _sha256(payload) != digest
        or str(row["workspace_root"]) != record.workspace_root
        or str(row["miss_id"]) != record.miss_id
        or str(row["requested_name"]) != record.requested_name
        or str(row["catalog_sha256"]) != record.catalog_sha256
        or str(row["observed_at"]) != record.observed_at
        or _miss_id(
            record.workspace_root,
            record.requested_name,
            record.catalog_sha256,
        )
        != record.miss_id
    ):
        raise EvolutionStoreCorruptionError(
            "Tool Catalog miss 摘要或投影列不一致。"
        )
    return StoredToolCatalogMiss(record=record, record_sha256=digest)


def _canonical_workspace(value: str | Path) -> str:
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError("workspace_root 必须是存在的目录。")
    return str(path)


def _catalog_sha256(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise ValueError("catalog_sha256 必须是 64 位十六进制摘要。")
    return normalized


def _miss_id_value(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if _MISS_ID_RE.fullmatch(normalized) is None:
        raise ValueError("Miss ID 格式无效；应为 tsm_<24位十六进制>。")
    return normalized


def _miss_id(workspace: str, requested_name: str, catalog_sha256: str) -> str:
    digest = _digest({
        "catalog_sha256": catalog_sha256,
        "requested_name": requested_name,
        "workspace_root": workspace,
    })
    return f"tsm_{digest[:24]}"


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Tool Catalog miss clock 必须返回带时区时间。")
    return value.astimezone(UTC).isoformat()


def _model_payload(model: BaseModel) -> tuple[str, str]:
    payload = model.model_dump_json(exclude_none=False)
    return payload, _sha256(payload)


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256(payload)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized_tool_name(name: str) -> str:
    with_spaces = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return " ".join(
        with_spaces.replace("__", " ").replace("_", " ").replace(".", " ").lower().split()
    )


def _restrict_permissions(path: Path, mode: int) -> None:
    if os.name == "posix" and path.exists():
        os.chmod(path, mode)


_MISS_SCHEMA = """
CREATE TABLE IF NOT EXISTS evolution_tool_catalog_misses (
    workspace_root TEXT NOT NULL,
    miss_id TEXT NOT NULL,
    requested_name TEXT NOT NULL,
    catalog_sha256 TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    record_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    PRIMARY KEY (workspace_root, miss_id),
    UNIQUE (workspace_root, requested_name, catalog_sha256)
);
CREATE INDEX IF NOT EXISTS idx_evolution_tool_catalog_misses_name
ON evolution_tool_catalog_misses (workspace_root, requested_name, observed_at DESC);
"""


__all__ = [
    "EVOLUTION_TOOL_CATALOG_MISS_POLICY",
    "EvolutionToolCatalogMissOpportunityError",
    "EvolutionToolCatalogMissOpportunityResult",
    "EvolutionToolCatalogMissOpportunityService",
    "StoredToolCatalogMiss",
    "ToolCatalogMissRecord",
    "ToolCatalogMissStore",
    "adapt_tool_catalog_miss_evidence",
    "normalize_missing_tool_name",
    "render_tool_catalog_miss_opportunity",
    "tool_catalog_sha256",
    "tool_name_is_available",
]
