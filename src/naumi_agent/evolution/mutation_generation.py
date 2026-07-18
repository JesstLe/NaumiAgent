"""In-memory mutation tool execution with immutable generation traces."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.experiment_leases import (
    ExperimentLeaseState,
    ExperimentWorktreeLease,
)
from naumi_agent.evolution.experiment_snapshots import EvolutionExperimentSourceSnapshot
from naumi_agent.evolution.experiments import EvolutionExperimentContract
from naumi_agent.evolution.mutation_plans import EvolutionMutationPlan, MutationFileFact
from naumi_agent.tools.base import ToolCall, ToolResult

MUTATION_GENERATION_POLICY = "evolution-mutation-generation-trace-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_SAFE_ID_RE = re.compile(r"^[^\x00\r\n]{1,128}$")
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_TRACE_BYTES = 256 * 1024
_SUCCESS = "success"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class MutationGenerationCallFact(_StrictModel):
    order: int = Field(ge=1, le=200)
    call_id_sha256: str = Field(pattern=_SHA256_RE)
    tool_name: Literal["file_edit", "file_write"]
    path: str = Field(min_length=1, max_length=1_024)
    status: Literal["success", "error"]
    arguments_sha256: str = Field(pattern=_SHA256_RE)
    arguments_size_bytes: int = Field(ge=2, le=_MAX_SOURCE_BYTES * 3 + 8_192)
    result_sha256: str = Field(pattern=_SHA256_RE)
    result_size_bytes: int = Field(ge=0, le=4_096)
    before_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    after_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    error_code: str = Field(default="", max_length=128)
    fact_sha256: str = Field(pattern=_SHA256_RE)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        return _safe_relative_path(value)

    @field_validator("error_code")
    @classmethod
    def _safe_error(cls, value: str) -> str:
        if value and not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", value):
            raise ValueError("Mutation Generation error code 格式无效。")
        return value

    @model_validator(mode="after")
    def _fact_is_consistent(self) -> Self:
        if self.status == "success":
            if self.after_sha256 is None or self.error_code:
                raise ValueError("成功 Mutation Generation call 字段不一致。")
            if self.tool_name == "file_edit" and self.before_sha256 is None:
                raise ValueError("file_edit call 缺少 before digest。")
        elif not self.error_code:
            raise ValueError("失败 Mutation Generation call 缺少 error code。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"fact_sha256"})
        )
        if not hmac.compare_digest(self.fact_sha256, expected):
            raise ValueError("Mutation Generation call fact 摘要不一致。")
        return self


class MutationGenerationFileFact(_StrictModel):
    path: str = Field(min_length=1, max_length=1_024)
    operation: Literal["modify", "create"]
    before_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    after_sha256: str = Field(pattern=_SHA256_RE)
    size_bytes: int = Field(ge=0, le=_MAX_SOURCE_BYTES)
    last_successful_call_order: int = Field(ge=1, le=200)
    fact_sha256: str = Field(pattern=_SHA256_RE)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        return _safe_relative_path(value)

    @model_validator(mode="after")
    def _fact_is_consistent(self) -> Self:
        if self.operation == "modify" and self.before_sha256 is None:
            raise ValueError("modify generation file 缺少 before digest。")
        if self.operation == "create" and self.before_sha256 is not None:
            raise ValueError("create generation file 不得包含 before digest。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"fact_sha256"})
        )
        if not hmac.compare_digest(self.fact_sha256, expected):
            raise ValueError("Mutation Generation file fact 摘要不一致。")
        return self


class EvolutionMutationGenerationTrace(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-mutation-generation-trace-v1"] = (
        MUTATION_GENERATION_POLICY
    )
    trace_id: str = Field(pattern=r"^evmgt_[0-9a-f]{24}$")
    trace_sha256: str = Field(pattern=_SHA256_RE)
    run_id: str = Field(min_length=1, max_length=128)
    created_at: str = Field(min_length=1, max_length=100)
    completed_at: str = Field(min_length=1, max_length=100)
    contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    contract_manifest_sha256: str = Field(pattern=_SHA256_RE)
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    source_snapshot_id: str = Field(pattern=r"^evs_[0-9a-f]{24}$")
    source_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    mutation_plan_id: str = Field(pattern=r"^evpplan_[0-9a-f]{24}$")
    mutation_plan_sha256: str = Field(pattern=_SHA256_RE)
    attempt: int = Field(ge=1, le=3)
    max_attempts: int = Field(ge=1, le=3)
    max_tool_calls: int = Field(ge=1, le=200)
    calls: tuple[MutationGenerationCallFact, ...] = Field(min_length=1, max_length=200)
    calls_sha256: str = Field(pattern=_SHA256_RE)
    final_files: tuple[MutationGenerationFileFact, ...] = Field(
        min_length=1,
        max_length=16,
    )
    final_files_sha256: str = Field(pattern=_SHA256_RE)
    total_tool_calls: int = Field(ge=1, le=200)
    successful_tool_calls: int = Field(ge=1, le=200)
    failed_tool_calls: int = Field(ge=0, le=199)
    trace_ready: Literal[True] = True
    write_authorized: Literal[False] = False
    execution_ready: Literal[False] = False

    @field_validator("run_id")
    @classmethod
    def _safe_run_id(cls, value: str) -> str:
        normalized = value.strip()
        if not _SAFE_ID_RE.fullmatch(normalized):
            raise ValueError("Mutation Generation run_id 格式无效。")
        return normalized

    @model_validator(mode="after")
    def _trace_is_bound_and_tamper_evident(self) -> Self:
        created = _parse_time(self.created_at)
        completed = _parse_time(self.completed_at)
        if completed < created:
            raise ValueError("Mutation Generation trace 时间顺序无效。")
        if self.attempt > self.max_attempts:
            raise ValueError("Mutation Generation attempt 超过上限。")
        if self.contract_id != f"evx_{self.contract_manifest_sha256[:24]}":
            raise ValueError("Mutation Generation Contract identity 不一致。")
        expected_lease = hashlib.sha256(
            f"{self.contract_id}:{self.contract_manifest_sha256}".encode()
        ).hexdigest()
        if self.lease_id != f"evl_{expected_lease[:24]}":
            raise ValueError("Mutation Generation Lease identity 不一致。")
        if self.source_snapshot_id != f"evs_{self.source_snapshot_sha256[:24]}":
            raise ValueError("Mutation Generation Snapshot identity 不一致。")
        if self.mutation_plan_id != f"evpplan_{self.mutation_plan_sha256[:24]}":
            raise ValueError("Mutation Generation Plan identity 不一致。")
        if self.total_tool_calls != len(self.calls):
            raise ValueError("Mutation Generation tool call 总数不一致。")
        if self.total_tool_calls > self.max_tool_calls:
            raise ValueError("Mutation Generation tool call 超过计划预算。")
        successful = sum(item.status == "success" for item in self.calls)
        failed = len(self.calls) - successful
        if (
            self.successful_tool_calls != successful
            or self.failed_tool_calls != failed
        ):
            raise ValueError("Mutation Generation call 结果计数不一致。")
        if tuple(item.order for item in self.calls) != tuple(
            range(1, len(self.calls) + 1)
        ):
            raise ValueError("Mutation Generation call 顺序必须连续。")
        if len({item.call_id_sha256 for item in self.calls}) != len(self.calls):
            raise ValueError("Mutation Generation call identity 不得重复。")
        paths = tuple(item.path for item in self.final_files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("Mutation Generation final files 必须排序且不得重复。")
        allowed = set(paths)
        if any(item.path not in allowed for item in self.calls):
            raise ValueError("Mutation Generation call 路径越过最终 scope。")
        for final in self.final_files:
            path_calls = tuple(item for item in self.calls if item.path == final.path)
            successful_calls = tuple(
                item for item in path_calls if item.status == "success"
            )
            if not successful_calls:
                raise ValueError("Mutation Generation final file 缺少成功 tool call。")
            current = final.before_sha256
            for call in path_calls:
                if call.before_sha256 != current:
                    raise ValueError("Mutation Generation call digest 链不连续。")
                if call.status == "success":
                    current = call.after_sha256
                elif call.after_sha256 != current:
                    raise ValueError("失败 Mutation Generation call 改变了草稿状态。")
            last = successful_calls[-1]
            if (
                last.order != final.last_successful_call_order
                or last.after_sha256 != final.after_sha256
                or current != final.after_sha256
            ):
                raise ValueError("Mutation Generation final file 与 call trace 不一致。")
        expected_calls = _sha256_payload(
            [item.model_dump(mode="json") for item in self.calls]
        )
        expected_files = _sha256_payload(
            [item.model_dump(mode="json") for item in self.final_files]
        )
        if not hmac.compare_digest(self.calls_sha256, expected_calls):
            raise ValueError("Mutation Generation calls 摘要不一致。")
        if not hmac.compare_digest(self.final_files_sha256, expected_files):
            raise ValueError("Mutation Generation final files 摘要不一致。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"trace_id", "trace_sha256"})
        )
        if not hmac.compare_digest(self.trace_sha256, expected):
            raise ValueError("Mutation Generation trace 摘要不一致。")
        if self.trace_id != f"evmgt_{expected[:24]}":
            raise ValueError("Mutation Generation trace identity 不一致。")
        return self


@dataclass(frozen=True, slots=True)
class EvolutionMutationGenerationResult:
    trace: EvolutionMutationGenerationTrace
    proposed_contents: Mapping[str, str]


class EvolutionMutationGenerationError(RuntimeError):
    """Typed failure without raw mutation content."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionMutationGenerationTraceStore:
    """Immutable SQLite storage keyed by Plan attempt and trace identity."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = str(database_path)

    def put(
        self,
        trace: EvolutionMutationGenerationTrace,
    ) -> EvolutionMutationGenerationTrace:
        try:
            trace = EvolutionMutationGenerationTrace.model_validate(
                trace.model_dump(mode="json")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionMutationGenerationError(
                "mutation_trace_invalid", "Mutation Generation trace 输入不可验证。"
            ) from exc
        serialized = trace.model_dump_json()
        if len(serialized.encode("utf-8")) > _MAX_TRACE_BYTES:
            raise EvolutionMutationGenerationError(
                "mutation_trace_oversized", "Mutation Generation trace 超过 256 KiB。"
            )
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT * FROM evolution_mutation_generation_traces
                   WHERE mutation_plan_id = ? AND attempt = ?""",
                (trace.mutation_plan_id, trace.attempt),
            ).fetchone()
            if row is not None:
                current = self._from_row(row)
                if current != trace:
                    raise EvolutionMutationGenerationError(
                        "mutation_trace_conflict",
                        "同一 Mutation Plan attempt 已存在不同 generation trace。",
                    )
                db.commit()
                return current
            db.execute(
                """INSERT INTO evolution_mutation_generation_traces
                   (trace_id, mutation_plan_id, attempt, trace_sha256, trace_json,
                    completed_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    trace.trace_id,
                    trace.mutation_plan_id,
                    trace.attempt,
                    trace.trace_sha256,
                    serialized,
                    trace.completed_at,
                ),
            )
            db.commit()
        return trace

    def get(self, trace_id: str) -> EvolutionMutationGenerationTrace | None:
        with closing(self._connect()) as db:
            row = db.execute(
                """SELECT * FROM evolution_mutation_generation_traces
                   WHERE trace_id = ?""",
                (trace_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def get_for_attempt(
        self,
        mutation_plan_id: str,
        attempt: int,
    ) -> EvolutionMutationGenerationTrace | None:
        with closing(self._connect()) as db:
            row = db.execute(
                """SELECT * FROM evolution_mutation_generation_traces
                   WHERE mutation_plan_id = ? AND attempt = ?""",
                (mutation_plan_id, attempt),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._database_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 10000")
        db.execute("PRAGMA journal_mode = WAL")
        db.execute(
            """CREATE TABLE IF NOT EXISTS evolution_mutation_generation_traces (
                   trace_id TEXT PRIMARY KEY,
                   mutation_plan_id TEXT NOT NULL,
                   attempt INTEGER NOT NULL,
                   trace_sha256 TEXT NOT NULL,
                   trace_json TEXT NOT NULL,
                   completed_at TEXT NOT NULL,
                   UNIQUE(mutation_plan_id, attempt)
               )"""
        )
        return db

    @staticmethod
    def _from_row(row: sqlite3.Row) -> EvolutionMutationGenerationTrace:
        serialized = row["trace_json"]
        if (
            not isinstance(serialized, str)
            or len(serialized.encode("utf-8")) > _MAX_TRACE_BYTES
        ):
            raise EvolutionMutationGenerationError(
                "mutation_trace_corrupt", "Mutation Generation trace 持久化内容损坏。"
            )
        try:
            trace = EvolutionMutationGenerationTrace.model_validate_json(serialized)
        except (TypeError, ValueError) as exc:
            raise EvolutionMutationGenerationError(
                "mutation_trace_corrupt", "Mutation Generation trace 持久化内容损坏。"
            ) from exc
        if (
            row["trace_id"] != trace.trace_id
            or row["mutation_plan_id"] != trace.mutation_plan_id
            or row["attempt"] != trace.attempt
            or row["trace_sha256"] != trace.trace_sha256
            or row["completed_at"] != trace.completed_at
        ):
            raise EvolutionMutationGenerationError(
                "mutation_trace_corrupt", "Mutation Generation trace 索引与内容不一致。"
            )
        return trace


@dataclass(frozen=True, slots=True)
class _PendingCall:
    call_id: str
    call_id_sha256: str
    tool_name: Literal["file_edit", "file_write"]
    path: str
    arguments: dict[str, str]
    arguments_sha256: str
    arguments_size_bytes: int


class EvolutionMutationGenerationSession:
    """Serialize virtual file tools and retain only bounded trace facts."""

    def __init__(
        self,
        *,
        contract: EvolutionExperimentContract,
        lease: ExperimentWorktreeLease,
        source_snapshot: EvolutionExperimentSourceSnapshot,
        mutation_plan: EvolutionMutationPlan,
        run_id: str,
        attempt: int,
        baseline_contents: Mapping[str, bytes | None],
        trace_store: EvolutionMutationGenerationTraceStore,
        clock: Callable[[], datetime],
    ) -> None:
        self._contract = contract
        self._lease = lease
        self._snapshot = source_snapshot
        self._plan = mutation_plan
        self._run_id = _safe_run_id(run_id)
        self._attempt = attempt
        self._baseline = dict(baseline_contents)
        self._current = dict(baseline_contents)
        self._trace_store = trace_store
        self._clock = clock
        self._created_at = _iso(clock())
        self._calls: list[MutationGenerationCallFact] = []
        self._results: dict[str, tuple[str, ToolResult]] = {}
        self._last_success: dict[str, int] = {}
        self._fatal_code = ""
        self._finalized: EvolutionMutationGenerationResult | None = None
        self._lock = asyncio.Lock()

    async def execute(self, call: ToolCall) -> ToolResult:
        async with self._lock:
            if not (
                isinstance(call, ToolCall)
                and isinstance(call.id, str)
                and isinstance(call.name, str)
                and isinstance(call.arguments, str)
            ):
                self._fatal_code = "mutation_call_invalid"
                return _tool_error(str(getattr(call, "id", "")), self._fatal_code)
            if self._finalized is not None:
                return _tool_error(call.id, "mutation_trace_finalized")
            if self._fatal_code:
                return _tool_error(call.id, self._fatal_code)
            raw_call_id = call.id.strip()
            if (
                not raw_call_id
                or raw_call_id != call.id
                or len(raw_call_id) > 500
                or any(char in raw_call_id for char in ("\x00", "\r", "\n"))
            ):
                self._fatal_code = "mutation_call_id_invalid"
                return _tool_error(call.id, self._fatal_code)
            arguments_digest = hashlib.sha256(call.arguments.encode("utf-8")).hexdigest()
            call_signature = hashlib.sha256(
                call.name.encode("utf-8") + b"\x00" + call.arguments.encode("utf-8")
            ).hexdigest()
            cached = self._results.get(raw_call_id)
            if cached is not None:
                if cached[0] != call_signature:
                    self._fatal_code = "mutation_call_id_collision"
                    return _tool_error(call.id, self._fatal_code)
                return cached[1]
            if len(self._calls) >= self._plan.max_tool_calls:
                self._fatal_code = "mutation_tool_budget_exceeded"
                return _tool_error(call.id, self._fatal_code)
            if call.name not in {"file_edit", "file_write"}:
                self._fatal_code = "mutation_tool_not_allowed"
                return _tool_error(call.id, self._fatal_code)
            try:
                pending = _parse_call(call)
                if pending.path not in self._plan.authorized_files:
                    raise EvolutionMutationGenerationError(
                        "mutation_scope_expansion",
                        "Mutation tool path 越过 approved scope。",
                    )
                result, before, after = self._apply(pending)
                error_code = ""
                status: Literal["success", "error"] = "success"
            except EvolutionMutationGenerationError as exc:
                pending = _safe_failed_call(call, arguments_digest)
                result = _tool_error(call.id, exc.code)
                before = self._current.get(pending.path)
                after = before
                error_code = exc.code
                status = "error"
                if exc.code in {
                    "mutation_arguments_invalid",
                    "mutation_path_invalid",
                    "mutation_scope_expansion",
                }:
                    self._fatal_code = exc.code
            fact = _call_fact(
                order=len(self._calls) + 1,
                pending=pending,
                status=status,
                result=result,
                before=before,
                after=after,
                error_code=error_code,
            )
            self._calls.append(fact)
            if status == "success":
                self._last_success[pending.path] = fact.order
            self._results[raw_call_id] = (call_signature, result)
            return result

    async def finalize(self) -> EvolutionMutationGenerationResult:
        async with self._lock:
            if self._finalized is not None:
                return self._finalized
            if self._fatal_code:
                raise EvolutionMutationGenerationError(
                    self._fatal_code,
                    "Mutation Generation session 存在不可恢复的协议违规。",
                )
            successful_paths = tuple(sorted(self._last_success))
            if successful_paths != tuple(sorted(self._plan.authorized_files)):
                raise EvolutionMutationGenerationError(
                    "mutation_scope_incomplete",
                    "Mutation Generation 未覆盖完整 approved scope。",
                )
            final_files = tuple(
                _final_file(
                    planned=planned,
                    before=self._baseline[planned.path],
                    after=self._current[planned.path],
                    last_order=self._last_success[planned.path],
                )
                for planned in sorted(self._plan.planned_files, key=lambda item: item.path)
            )
            completed_at = _iso(self._clock())
            trace = _build_trace(
                contract=self._contract,
                lease=self._lease,
                snapshot=self._snapshot,
                plan=self._plan,
                run_id=self._run_id,
                attempt=self._attempt,
                created_at=self._created_at,
                completed_at=completed_at,
                calls=tuple(self._calls),
                final_files=final_files,
            )
            stored = self._trace_store.put(trace)
            proposed = MappingProxyType({
                path: _decode_source(self._current[path], path)
                for path in self._plan.authorized_files
            })
            self._finalized = EvolutionMutationGenerationResult(
                trace=stored,
                proposed_contents=proposed,
            )
            return self._finalized

    def _apply(
        self,
        pending: _PendingCall,
    ) -> tuple[ToolResult, bytes | None, bytes]:
        before = self._current[pending.path]
        if pending.tool_name == "file_write":
            after = _encode_source(pending.arguments["content"], pending.path)
        else:
            if before is None:
                raise EvolutionMutationGenerationError(
                    "mutation_edit_missing_file", "file_edit 目标不存在。"
                )
            current = _decode_source(before, pending.path)
            old_text = pending.arguments["old_text"]
            occurrences = current.count(old_text)
            if occurrences == 0:
                raise EvolutionMutationGenerationError(
                    "mutation_edit_target_missing", "file_edit old_text 未找到。"
                )
            if occurrences > 1:
                raise EvolutionMutationGenerationError(
                    "mutation_edit_target_ambiguous", "file_edit old_text 不唯一。"
                )
            after = _encode_source(
                current.replace(old_text, pending.arguments["new_text"], 1),
                pending.path,
            )
        self._current[pending.path] = after
        return ToolResult(
            call_id=pending.call_id,
            status=_SUCCESS,
            content="Mutation proposal 已在隔离内存草稿中更新。",
        ), before, after


class EvolutionMutationGenerationService:
    """Create authority-bound in-memory generation sessions from real baselines."""

    def __init__(
        self,
        *,
        trace_store: EvolutionMutationGenerationTraceStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._trace_store = trace_store
        self._clock = clock or (lambda: datetime.now(UTC))

    def begin(
        self,
        *,
        contract: EvolutionExperimentContract,
        lease: ExperimentWorktreeLease,
        source_snapshot: EvolutionExperimentSourceSnapshot,
        mutation_plan: EvolutionMutationPlan,
        run_id: str,
        attempt: int,
    ) -> EvolutionMutationGenerationSession:
        contract, lease, source_snapshot, mutation_plan = _revalidate_inputs(
            contract, lease, source_snapshot, mutation_plan,
        )
        _require_authority(contract, lease, source_snapshot, mutation_plan)
        if not 1 <= attempt <= mutation_plan.max_attempts:
            raise EvolutionMutationGenerationError(
                "mutation_attempt_invalid", "Mutation Generation attempt 超出计划。"
            )
        if self._trace_store.get_for_attempt(mutation_plan.plan_id, attempt) is not None:
            raise EvolutionMutationGenerationError(
                "mutation_trace_attempt_exists",
                "该 Mutation Plan attempt 已有不可变 generation trace。",
            )
        baseline = _load_baseline(lease, mutation_plan)
        return EvolutionMutationGenerationSession(
            contract=contract,
            lease=lease,
            source_snapshot=source_snapshot,
            mutation_plan=mutation_plan,
            run_id=run_id,
            attempt=attempt,
            baseline_contents=baseline,
            trace_store=self._trace_store,
            clock=self._clock,
        )


def _parse_call(call: ToolCall) -> _PendingCall:
    try:
        parsed = json.loads(call.arguments)
    except (json.JSONDecodeError, TypeError) as exc:
        raise EvolutionMutationGenerationError(
            "mutation_arguments_invalid", "Mutation tool arguments 必须是 JSON object。"
        ) from exc
    if not isinstance(parsed, dict):
        raise EvolutionMutationGenerationError(
            "mutation_arguments_invalid", "Mutation tool arguments 必须是 JSON object。"
        )
    expected = (
        {"path", "content"}
        if call.name == "file_write"
        else {"path", "old_text", "new_text"}
    )
    if set(parsed) != expected or any(not isinstance(parsed[key], str) for key in expected):
        raise EvolutionMutationGenerationError(
            "mutation_arguments_invalid", "Mutation tool arguments 字段不完整或包含额外字段。"
        )
    path = _safe_relative_path(parsed["path"])
    canonical = _canonical_json(parsed)
    if len(canonical.encode("utf-8")) > _MAX_SOURCE_BYTES * 3 + 8_192:
        raise EvolutionMutationGenerationError(
            "mutation_arguments_oversized", "Mutation tool arguments 超过安全上限。"
        )
    return _PendingCall(
        call_id=str(call.id),
        call_id_sha256=hashlib.sha256(str(call.id).encode()).hexdigest(),
        tool_name=call.name,
        path=path,
        arguments={key: parsed[key] for key in expected},
        arguments_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
        arguments_size_bytes=len(canonical.encode("utf-8")),
    )


def _safe_failed_call(call: ToolCall, fallback_digest: str) -> _PendingCall:
    try:
        parsed = json.loads(call.arguments)
        raw_path = parsed.get("path", "<invalid>") if isinstance(parsed, dict) else "<invalid>"
        path = _safe_relative_path(str(raw_path))
    except (json.JSONDecodeError, EvolutionMutationGenerationError, TypeError, ValueError):
        path = "<invalid>"
    tool_name: Literal["file_edit", "file_write"] = (
        "file_edit" if call.name == "file_edit" else "file_write"
    )
    encoded = call.arguments.encode("utf-8", errors="replace")
    return _PendingCall(
        call_id=str(call.id),
        call_id_sha256=hashlib.sha256(str(call.id).encode()).hexdigest(),
        tool_name=tool_name,
        path=path,
        arguments={},
        arguments_sha256=fallback_digest,
        arguments_size_bytes=max(2, min(len(encoded), _MAX_SOURCE_BYTES * 3 + 8_192)),
    )


def _call_fact(
    *,
    order: int,
    pending: _PendingCall,
    status: Literal["success", "error"],
    result: ToolResult,
    before: bytes | None,
    after: bytes | None,
    error_code: str,
) -> MutationGenerationCallFact:
    result_bytes = result.content.encode("utf-8", errors="replace")[:4_096]
    payload = {
        "order": order,
        "call_id_sha256": pending.call_id_sha256,
        "tool_name": pending.tool_name,
        "path": pending.path,
        "status": status,
        "arguments_sha256": pending.arguments_sha256,
        "arguments_size_bytes": pending.arguments_size_bytes,
        "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "result_size_bytes": len(result_bytes),
        "before_sha256": _digest_bytes(before),
        "after_sha256": _digest_bytes(after),
        "error_code": error_code,
    }
    return MutationGenerationCallFact.model_validate({
        **payload,
        "fact_sha256": _sha256_payload(payload),
    })


def _final_file(
    *,
    planned: MutationFileFact,
    before: bytes | None,
    after: bytes | None,
    last_order: int,
) -> MutationGenerationFileFact:
    if after is None:
        raise EvolutionMutationGenerationError(
            "mutation_scope_incomplete", "Mutation Generation final file 缺少内容。"
        )
    payload = {
        "path": planned.path,
        "operation": planned.change_mode,
        "before_sha256": _digest_bytes(before),
        "after_sha256": _digest_bytes(after),
        "size_bytes": len(after),
        "last_successful_call_order": last_order,
    }
    return MutationGenerationFileFact.model_validate({
        **payload,
        "fact_sha256": _sha256_payload(payload),
    })


def _build_trace(
    *,
    contract: EvolutionExperimentContract,
    lease: ExperimentWorktreeLease,
    snapshot: EvolutionExperimentSourceSnapshot,
    plan: EvolutionMutationPlan,
    run_id: str,
    attempt: int,
    created_at: str,
    completed_at: str,
    calls: tuple[MutationGenerationCallFact, ...],
    final_files: tuple[MutationGenerationFileFact, ...],
) -> EvolutionMutationGenerationTrace:
    payload = {
        "schema_version": 1,
        "policy_version": MUTATION_GENERATION_POLICY,
        "run_id": run_id,
        "created_at": created_at,
        "completed_at": completed_at,
        "contract_id": contract.contract_id,
        "contract_manifest_sha256": contract.manifest_sha256,
        "lease_id": lease.lease_id,
        "source_snapshot_id": snapshot.snapshot_id,
        "source_snapshot_sha256": snapshot.snapshot_sha256,
        "mutation_plan_id": plan.plan_id,
        "mutation_plan_sha256": plan.plan_sha256,
        "attempt": attempt,
        "max_attempts": plan.max_attempts,
        "max_tool_calls": plan.max_tool_calls,
        "calls": [item.model_dump(mode="json") for item in calls],
        "calls_sha256": _sha256_payload(
            [item.model_dump(mode="json") for item in calls]
        ),
        "final_files": [item.model_dump(mode="json") for item in final_files],
        "final_files_sha256": _sha256_payload(
            [item.model_dump(mode="json") for item in final_files]
        ),
        "total_tool_calls": len(calls),
        "successful_tool_calls": sum(item.status == "success" for item in calls),
        "failed_tool_calls": sum(item.status == "error" for item in calls),
        "trace_ready": True,
        "write_authorized": False,
        "execution_ready": False,
    }
    digest = _sha256_payload(payload)
    return EvolutionMutationGenerationTrace.model_validate({
        **payload,
        "trace_id": f"evmgt_{digest[:24]}",
        "trace_sha256": digest,
    })


def _revalidate_inputs(
    contract: EvolutionExperimentContract,
    lease: ExperimentWorktreeLease,
    snapshot: EvolutionExperimentSourceSnapshot,
    plan: EvolutionMutationPlan,
) -> tuple[
    EvolutionExperimentContract,
    ExperimentWorktreeLease,
    EvolutionExperimentSourceSnapshot,
    EvolutionMutationPlan,
]:
    try:
        return (
            EvolutionExperimentContract.model_validate(contract.model_dump(mode="json")),
            ExperimentWorktreeLease.model_validate(lease.model_dump(mode="json")),
            EvolutionExperimentSourceSnapshot.model_validate(
                snapshot.model_dump(mode="json")
            ),
            EvolutionMutationPlan.model_validate(plan.model_dump(mode="json")),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionMutationGenerationError(
            "mutation_generation_authority_invalid",
            "Mutation Generation authority 输入不可验证。",
        ) from exc


def _require_authority(
    contract: EvolutionExperimentContract,
    lease: ExperimentWorktreeLease,
    snapshot: EvolutionExperimentSourceSnapshot,
    plan: EvolutionMutationPlan,
) -> None:
    if (
        lease.contract_id != contract.contract_id
        or lease.manifest_sha256 != contract.manifest_sha256
        or lease.baseline_commit != contract.baseline.commit
        or lease.state is not ExperimentLeaseState.ACTIVE
        or not lease.worktree_ready
        or snapshot.contract_id != contract.contract_id
        or snapshot.contract_manifest_sha256 != contract.manifest_sha256
        or snapshot.lease_id != lease.lease_id
        or snapshot.baseline_commit != contract.baseline.commit
        or plan.contract_id != contract.contract_id
        or plan.contract_manifest_sha256 != contract.manifest_sha256
        or plan.lease_id != lease.lease_id
        or plan.source_snapshot_id != snapshot.snapshot_id
        or plan.source_snapshot_sha256 != snapshot.snapshot_sha256
        or plan.candidate_id != contract.source.candidate_id
        or plan.candidate_revision != contract.source.candidate_revision
        or plan.candidate_sha256 != contract.source.candidate_sha256
        or plan.authorized_files != contract.scope.allowed_files
        or plan.stages[2].allowed_tools != ("file_edit", "file_write")
    ):
        raise EvolutionMutationGenerationError(
            "mutation_generation_authority_mismatch",
            "Mutation Generation authority 绑定不一致。",
        )


def _load_baseline(
    lease: ExperimentWorktreeLease,
    plan: EvolutionMutationPlan,
) -> dict[str, bytes | None]:
    root = Path(lease.worktree_path).resolve(strict=True)
    contents: dict[str, bytes | None] = {}
    for planned in plan.planned_files:
        target = root / planned.path
        if planned.change_mode == "create":
            if target.exists() or target.is_symlink():
                raise EvolutionMutationGenerationError(
                    "mutation_baseline_mismatch", "计划创建的目标当前已经存在。"
                )
            content = None
        else:
            content = _read_regular_file(target)
            if not hmac.compare_digest(
                hashlib.sha256(content).hexdigest(),
                planned.content_sha256,
            ):
                raise EvolutionMutationGenerationError(
                    "mutation_baseline_mismatch", "Mutation target 已偏离计划 baseline。"
                )
        contents[planned.path] = content
    return contents


def _read_regular_file(target: Path) -> bytes:
    try:
        before = target.lstat()
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise EvolutionMutationGenerationError(
                "mutation_baseline_file_type", "Mutation baseline 不是普通文件。"
            )
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise EvolutionMutationGenerationError(
                    "mutation_baseline_changed", "Mutation baseline 在读取期间变化。"
                )
            chunks: list[bytes] = []
            remaining = _MAX_SOURCE_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
        finally:
            os.close(descriptor)
    except EvolutionMutationGenerationError:
        raise
    except OSError as exc:
        raise EvolutionMutationGenerationError(
            "mutation_baseline_read_failed", "Mutation baseline 无法读取。"
        ) from exc
    if len(content) > _MAX_SOURCE_BYTES:
        raise EvolutionMutationGenerationError(
            "mutation_baseline_oversized", "Mutation baseline 超过 2 MiB。"
        )
    _decode_source(content, str(target.name))
    return content


def _safe_relative_path(value: str) -> str:
    normalized = str(value).strip().replace("\\", "/")
    path = Path(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or any(char in normalized for char in ("\x00", "\r", "\n"))
    ):
        raise EvolutionMutationGenerationError(
            "mutation_path_invalid", "Mutation Generation path 必须是安全相对路径。"
        )
    return normalized


def _safe_run_id(value: str) -> str:
    normalized = str(value).strip()
    if not _SAFE_ID_RE.fullmatch(normalized):
        raise EvolutionMutationGenerationError(
            "mutation_run_id_invalid", "Mutation Generation run_id 格式无效。"
        )
    return normalized


def _encode_source(content: str, path: str) -> bytes:
    encoded = content.encode("utf-8")
    if len(encoded) > _MAX_SOURCE_BYTES:
        raise EvolutionMutationGenerationError(
            "mutation_content_oversized", f"Mutation proposal 超过 2 MiB：{path}"
        )
    if "\x00" in content:
        raise EvolutionMutationGenerationError(
            "mutation_content_binary", f"Mutation proposal 含 NUL：{path}"
        )
    return encoded


def _decode_source(content: bytes | None, path: str) -> str:
    if content is None:
        raise EvolutionMutationGenerationError(
            "mutation_content_missing", f"Mutation proposal 缺少内容：{path}"
        )
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvolutionMutationGenerationError(
            "mutation_content_encoding", f"Mutation proposal 不是 UTF-8：{path}"
        ) from exc


def _tool_error(call_id: str, code: str) -> ToolResult:
    return ToolResult(
        call_id=str(call_id),
        status="error",
        content=f"Mutation proposal 未更新：{code}",
    )


def _digest_bytes(value: bytes | None) -> str | None:
    return hashlib.sha256(value).hexdigest() if value is not None else None


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_payload(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Mutation Generation 时间必须是 ISO-8601。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Mutation Generation 时间必须包含时区。")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


__all__ = [
    "EvolutionMutationGenerationError",
    "EvolutionMutationGenerationResult",
    "EvolutionMutationGenerationService",
    "EvolutionMutationGenerationSession",
    "EvolutionMutationGenerationTrace",
    "EvolutionMutationGenerationTraceStore",
    "MutationGenerationCallFact",
    "MutationGenerationFileFact",
]
