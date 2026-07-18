"""Immutable mutation artifacts built from committed isolated patch writes."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.experiment_leases import (
    ExperimentLeaseState,
    ExperimentWorktreeLease,
)
from naumi_agent.evolution.experiment_snapshots import EvolutionExperimentSourceSnapshot
from naumi_agent.evolution.experiments import EvolutionExperimentContract
from naumi_agent.evolution.mutation_generation import EvolutionMutationGenerationTrace
from naumi_agent.evolution.mutation_plans import EvolutionMutationPlan
from naumi_agent.evolution.patch_journals import (
    EvolutionPatchJournal,
    EvolutionPatchJournalStore,
    PatchJournalState,
)
from naumi_agent.evolution.patch_set_writers import (
    EvolutionPatchSetWriteReceipt,
)
from naumi_agent.evolution.patch_sets import (
    EvolutionPatchSetStore,
    EvolutionPatchSetTransaction,
    PatchSetState,
)
from naumi_agent.evolution.patch_writers import EvolutionPatchWriteReceipt
from naumi_agent.evolution.postflight_guards import (
    EvolutionPostflightGuard,
    EvolutionPostflightGuardError,
    EvolutionPostflightGuardReceipt,
    PostflightDiffFact,
)
from naumi_agent.evolution.static_guards import (
    EvolutionStaticGuardPolicy,
    EvolutionStaticGuardReceipt,
)

MUTATION_RECEIPT_POLICY = "evolution-mutation-receipt-v2"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_RECEIPT_BYTES = 256 * 1024
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class MutationReceiptFile(_StrictModel):
    path: str = Field(min_length=1, max_length=1_024)
    operation: Literal["modify", "create"]
    before_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    after_sha256: str = Field(pattern=_SHA256_RE)
    unified_diff_sha256: str = Field(pattern=_SHA256_RE)
    added_lines: int = Field(ge=0, le=4_194_304)
    deleted_lines: int = Field(ge=0, le=4_194_304)
    api_change: Literal["not_applicable", "unchanged", "additive"]
    fact_sha256: str = Field(pattern=_SHA256_RE)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = Path(normalized)
        if (
            not normalized
            or path.is_absolute()
            or ".." in path.parts
            or any(char in normalized for char in ("\x00", "\r", "\n"))
        ):
            raise ValueError("Mutation Receipt path 必须是安全相对路径。")
        return normalized

    @model_validator(mode="after")
    def _fact_is_tamper_evident(self) -> Self:
        if self.operation == "modify" and self.before_sha256 is None:
            raise ValueError("modify Mutation Receipt fact 缺少 before digest。")
        if self.operation == "create" and self.before_sha256 is not None:
            raise ValueError("create Mutation Receipt fact 不得包含 before digest。")
        expected = _sha256_payload(
            self.model_dump(mode="json", exclude={"fact_sha256"})
        )
        if not hmac.compare_digest(self.fact_sha256, expected):
            raise ValueError("Mutation Receipt file fact 摘要不一致。")
        return self


class MutationToolEvidence(_StrictModel):
    order: int = Field(ge=1, le=4)
    phase: Literal[
        "mutation_generation",
        "static_guard",
        "patch_write",
        "postflight_guard",
    ]
    tool_name: Literal[
        "evolution_mutation_generation",
        "evolution_static_guard",
        "evolution_patch_writer",
        "evolution_patch_set_writer",
        "evolution_postflight_guard",
    ]
    artifact_id: str = Field(min_length=1, max_length=64)
    artifact_sha256: str = Field(pattern=_SHA256_RE)
    evidence_uri: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _binding_is_valid(self) -> Self:
        if self.phase == "patch_write":
            writer_prefix = {
                "evolution_patch_writer": "evw_",
                "evolution_patch_set_writer": "evsw_",
            }.get(self.tool_name)
            binding_valid = writer_prefix is not None and self.artifact_id.startswith(
                writer_prefix
            )
        else:
            expected_tool, expected_prefix = {
                "mutation_generation": (
                    "evolution_mutation_generation",
                    "evmgt_",
                ),
                "static_guard": ("evolution_static_guard", "evg_"),
                "postflight_guard": ("evolution_postflight_guard", "evpg_"),
            }[self.phase]
            binding_valid = self.tool_name == expected_tool and self.artifact_id.startswith(
                expected_prefix
            )
        expected_uri = f"artifact://evolution/{self.phase}/{self.artifact_id}"
        if not binding_valid or self.evidence_uri != expected_uri:
            raise ValueError("Mutation Receipt tool evidence 绑定无效。")
        return self


class EvolutionMutationReceipt(_StrictModel):
    schema_version: Literal[1, 2] = 2
    policy_version: Literal[
        "evolution-mutation-receipt-v1",
        "evolution-mutation-receipt-v2",
    ] = (
        MUTATION_RECEIPT_POLICY
    )
    mutation_receipt_id: str = Field(pattern=r"^evmr_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    created_at: str = Field(min_length=1, max_length=100)
    contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    contract_manifest_sha256: str = Field(pattern=_SHA256_RE)
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    source_snapshot_id: str = Field(pattern=r"^evs_[0-9a-f]{24}$")
    source_snapshot_sha256: str = Field(pattern=_SHA256_RE)
    mutation_plan_id: str = Field(pattern=r"^evpplan_[0-9a-f]{24}$")
    mutation_plan_sha256: str = Field(pattern=_SHA256_RE)
    mutation_generation_trace_id: str | None = Field(
        default=None,
        pattern=r"^evmgt_[0-9a-f]{24}$",
    )
    mutation_generation_trace_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_RE,
    )
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_sha256: str = Field(pattern=_SHA256_RE)
    finding_code: str = Field(min_length=1, max_length=128)
    scope: str = Field(min_length=1, max_length=1_024)
    rationale: str = Field(min_length=1, max_length=4_000)
    rationale_sha256: str = Field(pattern=_SHA256_RE)
    writer_kind: Literal["single_file", "multi_file"]
    transaction_id: str = Field(pattern=r"^(?:evj|evset)_[0-9a-f]{24}$")
    write_receipt_id: str = Field(pattern=r"^(?:evw|evsw)_[0-9a-f]{24}$")
    write_receipt_sha256: str = Field(pattern=_SHA256_RE)
    static_guard_id: str = Field(pattern=r"^evg_[0-9a-f]{24}$")
    static_guard_sha256: str = Field(pattern=_SHA256_RE)
    postflight_guard_id: str = Field(pattern=r"^evpg_[0-9a-f]{24}$")
    postflight_guard_sha256: str = Field(pattern=_SHA256_RE)
    attempt: int = Field(ge=1, le=3)
    max_attempts: int = Field(ge=1, le=3)
    files: tuple[MutationReceiptFile, ...] = Field(min_length=1, max_length=16)
    files_sha256: str = Field(pattern=_SHA256_RE)
    total_added_lines: int = Field(ge=0, le=67_108_864)
    total_deleted_lines: int = Field(ge=0, le=67_108_864)
    required_metrics: tuple[str, ...] = Field(min_length=1, max_length=8)
    tool_evidence: tuple[MutationToolEvidence, ...] = Field(min_length=3, max_length=4)
    mutation_completed: Literal[True] = True
    validation_status: Literal["pending"] = "pending"
    validation_ready: Literal[True] = True
    promotion_ready: Literal[False] = False
    execution_ready: Literal[False] = False

    @field_validator("finding_code")
    @classmethod
    def _safe_finding_code(cls, value: str) -> str:
        normalized = value.strip()
        if not _SAFE_CODE_RE.fullmatch(normalized):
            raise ValueError("Mutation Receipt finding_code 格式无效。")
        return normalized

    @field_validator("scope", "rationale")
    @classmethod
    def _safe_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(char in normalized for char in ("\x00", "\r")):
            raise ValueError("Mutation Receipt 文本字段格式无效。")
        return normalized

    @field_validator("required_metrics")
    @classmethod
    def _safe_metrics(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _SAFE_CODE_RE.fullmatch(value) for value in values):
            raise ValueError("Mutation Receipt required metric 格式无效。")
        return values

    @model_validator(mode="after")
    def _receipt_is_bound_and_tamper_evident(self) -> Self:
        _parse_time(self.created_at)
        generation_fields = (
            self.mutation_generation_trace_id,
            self.mutation_generation_trace_sha256,
        )
        if self.schema_version == 2 and (
            self.policy_version != MUTATION_RECEIPT_POLICY
            or any(item is None for item in generation_fields)
        ):
            raise ValueError("Mutation Receipt v2 缺少 Generation Trace。")
        if self.schema_version == 1 and (
            self.policy_version != "evolution-mutation-receipt-v1"
            or any(item is not None for item in generation_fields)
        ):
            raise ValueError("Mutation Receipt v1 字段不一致。")
        if self.mutation_generation_trace_id is not None and (
            self.mutation_generation_trace_sha256 is None
            or self.mutation_generation_trace_id
            != f"evmgt_{self.mutation_generation_trace_sha256[:24]}"
        ):
            raise ValueError("Mutation Receipt Generation Trace identity 不一致。")
        if self.attempt > self.max_attempts:
            raise ValueError("Mutation Receipt attempt 超过计划上限。")
        if self.contract_id != f"evx_{self.contract_manifest_sha256[:24]}":
            raise ValueError("Mutation Receipt Contract identity 不一致。")
        expected_lease = hashlib.sha256(
            f"{self.contract_id}:{self.contract_manifest_sha256}".encode()
        ).hexdigest()
        if self.lease_id != f"evl_{expected_lease[:24]}":
            raise ValueError("Mutation Receipt Lease identity 不一致。")
        if self.source_snapshot_id != f"evs_{self.source_snapshot_sha256[:24]}":
            raise ValueError("Mutation Receipt Snapshot identity 不一致。")
        if self.mutation_plan_id != f"evpplan_{self.mutation_plan_sha256[:24]}":
            raise ValueError("Mutation Receipt Plan identity 不一致。")
        if self.static_guard_id != f"evg_{self.static_guard_sha256[:24]}":
            raise ValueError("Mutation Receipt Static Guard identity 不一致。")
        if self.postflight_guard_id != f"evpg_{self.postflight_guard_sha256[:24]}":
            raise ValueError("Mutation Receipt Postflight identity 不一致。")
        expected_writer = (
            ("evj_", "evw_")
            if self.writer_kind == "single_file"
            else ("evset_", "evsw_")
        )
        if not self.transaction_id.startswith(expected_writer[0]):
            raise ValueError("Mutation Receipt transaction kind 不一致。")
        if not self.write_receipt_id.startswith(expected_writer[1]):
            raise ValueError("Mutation Receipt writer kind 不一致。")
        if self.write_receipt_id != (
            f"{expected_writer[1]}{self.write_receipt_sha256[:24]}"
        ):
            raise ValueError("Mutation Receipt Write Receipt identity 不一致。")
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("Mutation Receipt files 必须排序且不得重复。")
        expected_files = _sha256_payload(
            [item.model_dump(mode="json") for item in self.files]
        )
        if not hmac.compare_digest(self.files_sha256, expected_files):
            raise ValueError("Mutation Receipt files 摘要不一致。")
        if self.total_added_lines != sum(item.added_lines for item in self.files):
            raise ValueError("Mutation Receipt added lines 不一致。")
        if self.total_deleted_lines != sum(item.deleted_lines for item in self.files):
            raise ValueError("Mutation Receipt deleted lines 不一致。")
        expected_rationale = hashlib.sha256(self.rationale.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(self.rationale_sha256, expected_rationale):
            raise ValueError("Mutation Receipt rationale 摘要不一致。")
        if len(self.required_metrics) != len(set(self.required_metrics)):
            raise ValueError("Mutation Receipt required metrics 不得重复。")
        expected_writer_tool = (
            "evolution_patch_writer"
            if self.writer_kind == "single_file"
            else "evolution_patch_set_writer"
        )
        governance_evidence = (
            (
                2 if self.schema_version == 2 else 1,
                "static_guard",
                "evolution_static_guard",
                self.static_guard_id,
                self.static_guard_sha256,
            ),
            (
                3 if self.schema_version == 2 else 2,
                "patch_write",
                expected_writer_tool,
                self.write_receipt_id,
                self.write_receipt_sha256,
            ),
            (
                4 if self.schema_version == 2 else 3,
                "postflight_guard",
                "evolution_postflight_guard",
                self.postflight_guard_id,
                self.postflight_guard_sha256,
            ),
        )
        expected_evidence = governance_evidence
        if self.schema_version == 2:
            expected_evidence = (
                (
                    1,
                    "mutation_generation",
                    "evolution_mutation_generation",
                    self.mutation_generation_trace_id,
                    self.mutation_generation_trace_sha256,
                ),
                *governance_evidence,
            )
        observed_evidence = tuple(
            (
                item.order,
                item.phase,
                item.tool_name,
                item.artifact_id,
                item.artifact_sha256,
            )
            for item in self.tool_evidence
        )
        if observed_evidence != expected_evidence:
            raise ValueError("Mutation Receipt tool evidence 顺序或引用不一致。")
        excluded = {"mutation_receipt_id", "receipt_sha256"}
        if self.schema_version == 1:
            excluded.update({
                "mutation_generation_trace_id",
                "mutation_generation_trace_sha256",
            })
        expected = _sha256_payload(self.model_dump(mode="json", exclude=excluded))
        if not hmac.compare_digest(self.receipt_sha256, expected):
            raise ValueError("Mutation Receipt 摘要不一致。")
        if self.mutation_receipt_id != f"evmr_{expected[:24]}":
            raise ValueError("Mutation Receipt identity 不一致。")
        return self


class EvolutionMutationReceiptError(RuntimeError):
    """Typed failure that never embeds source or diff content."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionMutationReceiptConflictError(EvolutionMutationReceiptError):
    """A lease or plan already owns a different immutable receipt."""


class EvolutionMutationReceiptStore:
    """SQLite store for one immutable receipt per lease and Mutation Plan."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = str(database_path)

    def put(self, receipt: EvolutionMutationReceipt) -> EvolutionMutationReceipt:
        try:
            receipt = EvolutionMutationReceipt.model_validate(
                receipt.model_dump(mode="json")
            )
        except (TypeError, ValueError) as exc:
            raise EvolutionMutationReceiptError(
                "mutation_receipt_invalid", "Mutation Receipt 输入不可验证。"
            ) from exc
        encoded = receipt.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_RECEIPT_BYTES:
            raise EvolutionMutationReceiptError(
                "mutation_receipt_oversized", "Mutation Receipt 超过 256 KiB 上限。"
            )
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT * FROM evolution_mutation_receipts
                   WHERE lease_id = ? OR mutation_plan_id = ?""",
                (receipt.lease_id, receipt.mutation_plan_id),
            ).fetchall()
            if rows:
                if len(rows) != 1:
                    raise EvolutionMutationReceiptConflictError(
                        "mutation_receipt_conflict",
                        "Lease 与 Mutation Plan 已绑定冲突的 Mutation Receipt。",
                    )
                current = self._from_row(rows[0])
                if current != receipt:
                    raise EvolutionMutationReceiptConflictError(
                        "mutation_receipt_conflict",
                        "该 Lease 或 Mutation Plan 已存在不同的 Mutation Receipt。",
                    )
                db.commit()
                return current
            db.execute(
                """INSERT INTO evolution_mutation_receipts
                   (mutation_receipt_id, lease_id, mutation_plan_id, receipt_sha256,
                    receipt_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    receipt.mutation_receipt_id,
                    receipt.lease_id,
                    receipt.mutation_plan_id,
                    receipt.receipt_sha256,
                    encoded,
                    receipt.created_at,
                ),
            )
            db.commit()
        return receipt

    def get(self, mutation_receipt_id: str) -> EvolutionMutationReceipt | None:
        with closing(self._connect()) as db:
            row = db.execute(
                """SELECT * FROM evolution_mutation_receipts
                   WHERE mutation_receipt_id = ?""",
                (mutation_receipt_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def get_by_lease(self, lease_id: str) -> EvolutionMutationReceipt | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM evolution_mutation_receipts WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list_recent(self, *, limit: int = 100) -> tuple[EvolutionMutationReceipt, ...]:
        if not 1 <= limit <= 1_000:
            raise ValueError("Mutation Receipt limit 必须在 1..1000。")
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT * FROM evolution_mutation_receipts
                   ORDER BY created_at DESC, mutation_receipt_id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._database_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 10000")
        db.execute("PRAGMA journal_mode = WAL")
        db.execute(
            """CREATE TABLE IF NOT EXISTS evolution_mutation_receipts (
                   mutation_receipt_id TEXT PRIMARY KEY,
                   lease_id TEXT NOT NULL UNIQUE,
                   mutation_plan_id TEXT NOT NULL UNIQUE,
                   receipt_sha256 TEXT NOT NULL,
                   receipt_json TEXT NOT NULL,
                   created_at TEXT NOT NULL
               )"""
        )
        return db

    @staticmethod
    def _from_row(row: sqlite3.Row) -> EvolutionMutationReceipt:
        serialized = row["receipt_json"]
        if (
            not isinstance(serialized, str)
            or len(serialized.encode("utf-8")) > _MAX_RECEIPT_BYTES
        ):
            raise EvolutionMutationReceiptError(
                "mutation_receipt_corrupt", "Mutation Receipt 持久化内容损坏。"
            )
        try:
            receipt = EvolutionMutationReceipt.model_validate_json(serialized)
        except (TypeError, ValueError) as exc:
            raise EvolutionMutationReceiptError(
                "mutation_receipt_corrupt", "Mutation Receipt 持久化内容损坏。"
            ) from exc
        if (
            row["mutation_receipt_id"] != receipt.mutation_receipt_id
            or row["lease_id"] != receipt.lease_id
            or row["mutation_plan_id"] != receipt.mutation_plan_id
            or row["receipt_sha256"] != receipt.receipt_sha256
            or row["created_at"] != receipt.created_at
        ):
            raise EvolutionMutationReceiptError(
                "mutation_receipt_corrupt", "Mutation Receipt 索引与内容不一致。"
            )
        return receipt


class EvolutionMutationReceiptService:
    """Finalize one committed write into a validation-ready mutation artifact."""

    def __init__(
        self,
        *,
        journal_store: EvolutionPatchJournalStore,
        patch_set_store: EvolutionPatchSetStore,
        receipt_store: EvolutionMutationReceiptStore,
        postflight_guard: EvolutionPostflightGuard | None = None,
    ) -> None:
        self._journal_store = journal_store
        self._patch_set_store = patch_set_store
        self._receipt_store = receipt_store
        self._postflight_guard = postflight_guard or EvolutionPostflightGuard()

    def finalize(
        self,
        *,
        contract: EvolutionExperimentContract,
        lease: ExperimentWorktreeLease,
        source_snapshot: EvolutionExperimentSourceSnapshot,
        mutation_plan: EvolutionMutationPlan,
        static_guard: EvolutionStaticGuardReceipt,
        generation_trace: EvolutionMutationGenerationTrace,
    ) -> EvolutionMutationReceipt:
        (
            contract,
            lease,
            source_snapshot,
            mutation_plan,
            static_guard,
            generation_trace,
        ) = _revalidate_authority_inputs(
            contract,
            lease,
            source_snapshot,
            mutation_plan,
            static_guard,
            generation_trace,
        )
        _require_authority(
            contract,
            lease,
            source_snapshot,
            mutation_plan,
            static_guard,
            generation_trace,
        )
        _require_safe_rationale(mutation_plan.objective.hypothesis)
        journal = self._journal_store.get_by_lease(lease.lease_id)
        patch_set = self._patch_set_store.get_by_lease(lease.lease_id)
        source = _select_committed_source(journal, patch_set)
        write_receipt, transaction_id, attempt, max_attempts, writer_kind = (
            self._load_write_receipt(source)
        )
        _require_write_binding(
            write_receipt,
            contract=contract,
            lease=lease,
            source_snapshot=source_snapshot,
            mutation_plan=mutation_plan,
            static_guard=static_guard,
            generation_trace=generation_trace,
        )
        if attempt != generation_trace.attempt:
            raise EvolutionMutationReceiptError(
                "mutation_generation_attempt_mismatch",
                "Committed Patch attempt 与 Mutation Generation Trace 不一致。",
            )
        try:
            postflight = self._postflight_guard.inspect(
                contract=contract,
                lease=lease,
                source_snapshot=source_snapshot,
                mutation_plan=mutation_plan,
                static_guard=static_guard,
            )
        except EvolutionPostflightGuardError as exc:
            raise EvolutionMutationReceiptError(exc.code, str(exc)) from exc
        embedded = write_receipt.postflight_guard
        if embedded is None or embedded != postflight:
            raise EvolutionMutationReceiptError(
                "mutation_postflight_mismatch",
                "Write Receipt 与当前 Postflight Guard 不一致。",
            )
        receipt = _build_mutation_receipt(
            contract=contract,
            source_snapshot=source_snapshot,
            mutation_plan=mutation_plan,
            static_guard=static_guard,
            postflight=postflight,
            write_receipt=write_receipt,
            transaction_id=transaction_id,
            attempt=attempt,
            max_attempts=max_attempts,
            writer_kind=writer_kind,
            created_at=source.updated_at,
            generation_trace=generation_trace,
        )
        return self._receipt_store.put(receipt)

    def _load_write_receipt(
        self,
        source: EvolutionPatchJournal | EvolutionPatchSetTransaction,
    ) -> tuple[
        EvolutionPatchWriteReceipt | EvolutionPatchSetWriteReceipt,
        str,
        int,
        int,
        Literal["single_file", "multi_file"],
    ]:
        try:
            if isinstance(source, EvolutionPatchJournal):
                serialized = self._journal_store.load_receipt_json(source.journal_id)
                if serialized is None:
                    raise ValueError("committed journal 缺少 Write Receipt。")
                receipt = EvolutionPatchWriteReceipt.model_validate_json(serialized)
                writer_kind: Literal["single_file", "multi_file"] = "single_file"
                transaction_id = source.journal_id
            else:
                serialized = self._patch_set_store.load_receipt_json(
                    source.transaction_id
                )
                if serialized is None:
                    raise ValueError("committed patch set 缺少 Write Receipt。")
                receipt = EvolutionPatchSetWriteReceipt.model_validate_json(serialized)
                writer_kind = "multi_file"
                transaction_id = source.transaction_id
        except (TypeError, ValueError) as exc:
            raise EvolutionMutationReceiptError(
                "mutation_write_receipt_corrupt", "已提交 Write Receipt 不可验证。"
            ) from exc
        if receipt.schema_version != 3 or receipt.postflight_guard is None:
            raise EvolutionMutationReceiptError(
                "mutation_write_receipt_legacy",
                "历史 Write Receipt 缺少 Generation Trace，不能进入自进化验证。",
            )
        return (
            receipt,
            transaction_id,
            source.attempt,
            source.max_attempts,
            writer_kind,
        )


def _select_committed_source(
    journal: EvolutionPatchJournal | None,
    patch_set: EvolutionPatchSetTransaction | None,
) -> EvolutionPatchJournal | EvolutionPatchSetTransaction:
    committed: list[EvolutionPatchJournal | EvolutionPatchSetTransaction] = []
    if journal is not None and journal.state is PatchJournalState.COMMITTED:
        committed.append(journal)
    if patch_set is not None and patch_set.state is PatchSetState.COMMITTED:
        committed.append(patch_set)
    if len(committed) > 1:
        raise EvolutionMutationReceiptError(
            "mutation_write_ambiguous", "同一 Lease 存在多个 committed Patch 事实。"
        )
    if not committed:
        raise EvolutionMutationReceiptError(
            "mutation_write_not_committed", "Mutation Receipt 需要 committed Patch。"
        )
    return committed[0]


def _revalidate_authority_inputs(
    contract: EvolutionExperimentContract,
    lease: ExperimentWorktreeLease,
    snapshot: EvolutionExperimentSourceSnapshot,
    plan: EvolutionMutationPlan,
    guard: EvolutionStaticGuardReceipt,
    generation_trace: EvolutionMutationGenerationTrace,
) -> tuple[
    EvolutionExperimentContract,
    ExperimentWorktreeLease,
    EvolutionExperimentSourceSnapshot,
    EvolutionMutationPlan,
    EvolutionStaticGuardReceipt,
    EvolutionMutationGenerationTrace,
]:
    try:
        return (
            EvolutionExperimentContract.model_validate(contract.model_dump(mode="json")),
            ExperimentWorktreeLease.model_validate(lease.model_dump(mode="json")),
            EvolutionExperimentSourceSnapshot.model_validate(
                snapshot.model_dump(mode="json")
            ),
            EvolutionMutationPlan.model_validate(plan.model_dump(mode="json")),
            EvolutionStaticGuardReceipt.model_validate(guard.model_dump(mode="json")),
            EvolutionMutationGenerationTrace.model_validate(
                generation_trace.model_dump(mode="json")
            ),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvolutionMutationReceiptError(
            "mutation_authority_invalid", "Mutation Receipt authority 输入不可验证。"
        ) from exc


def _require_safe_rationale(rationale: str) -> None:
    violations = EvolutionStaticGuardPolicy().inspect_content(
        "mutation-rationale.txt",
        rationale.encode("utf-8"),
    )
    if any(item.code == "hardcoded_secret" for item in violations):
        raise EvolutionMutationReceiptError(
            "mutation_rationale_secret",
            "Mutation rationale 疑似包含机密信息，拒绝持久化。",
        )


def _require_authority(
    contract: EvolutionExperimentContract,
    lease: ExperimentWorktreeLease,
    snapshot: EvolutionExperimentSourceSnapshot,
    plan: EvolutionMutationPlan,
    guard: EvolutionStaticGuardReceipt,
    generation_trace: EvolutionMutationGenerationTrace,
) -> None:
    trace_files = tuple(
        (item.path, item.operation, item.before_sha256, item.after_sha256)
        for item in generation_trace.final_files
    )
    guard_files = tuple(
        (item.path, item.operation, item.before_sha256, item.after_sha256)
        for item in guard.changes
    )
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
        or guard.contract_id != contract.contract_id
        or guard.lease_id != lease.lease_id
        or guard.source_snapshot_id != snapshot.snapshot_id
        or guard.mutation_plan_id != plan.plan_id
        or guard.mutation_plan_sha256 != plan.plan_sha256
        or not guard.preflight_passed
        or guard.schema_version != 2
        or guard.mutation_generation_trace_id != generation_trace.trace_id
        or guard.mutation_generation_trace_sha256 != generation_trace.trace_sha256
        or guard.mutation_generation_attempt != generation_trace.attempt
        or generation_trace.contract_id != contract.contract_id
        or generation_trace.contract_manifest_sha256 != contract.manifest_sha256
        or generation_trace.lease_id != lease.lease_id
        or generation_trace.source_snapshot_id != snapshot.snapshot_id
        or generation_trace.source_snapshot_sha256 != snapshot.snapshot_sha256
        or generation_trace.mutation_plan_id != plan.plan_id
        or generation_trace.mutation_plan_sha256 != plan.plan_sha256
        or generation_trace.max_attempts != plan.max_attempts
        or trace_files != guard_files
    ):
        raise EvolutionMutationReceiptError(
            "mutation_authority_mismatch", "Mutation Receipt 权威绑定不一致。"
        )


def _require_write_binding(
    receipt: EvolutionPatchWriteReceipt | EvolutionPatchSetWriteReceipt,
    *,
    contract: EvolutionExperimentContract,
    lease: ExperimentWorktreeLease,
    source_snapshot: EvolutionExperimentSourceSnapshot,
    mutation_plan: EvolutionMutationPlan,
    static_guard: EvolutionStaticGuardReceipt,
    generation_trace: EvolutionMutationGenerationTrace,
) -> None:
    receipt_paths = (
        (receipt.change.path,)
        if isinstance(receipt, EvolutionPatchWriteReceipt)
        else tuple(item.path for item in receipt.changes)
    )
    if (
        receipt.contract_id != contract.contract_id
        or receipt.lease_id != lease.lease_id
        or receipt.source_snapshot_id != source_snapshot.snapshot_id
        or receipt.mutation_plan_id != mutation_plan.plan_id
        or receipt.guard_id != static_guard.guard_id
        or receipt.guard_receipt_sha256 != static_guard.receipt_sha256
        or receipt.schema_version != 3
        or receipt.mutation_generation_trace_id != generation_trace.trace_id
        or receipt.mutation_generation_trace_sha256
        != generation_trace.trace_sha256
        or receipt.mutation_generation_attempt != generation_trace.attempt
        or receipt_paths != mutation_plan.authorized_files
        or not receipt.postflight_passed
        or not receipt.write_completed
    ):
        raise EvolutionMutationReceiptError(
            "mutation_write_binding_mismatch", "Write Receipt 与 Mutation authority 不一致。"
        )


def _build_mutation_receipt(
    *,
    contract: EvolutionExperimentContract,
    source_snapshot: EvolutionExperimentSourceSnapshot,
    mutation_plan: EvolutionMutationPlan,
    static_guard: EvolutionStaticGuardReceipt,
    postflight: EvolutionPostflightGuardReceipt,
    write_receipt: EvolutionPatchWriteReceipt | EvolutionPatchSetWriteReceipt,
    transaction_id: str,
    attempt: int,
    max_attempts: int,
    writer_kind: Literal["single_file", "multi_file"],
    created_at: str,
    generation_trace: EvolutionMutationGenerationTrace,
) -> EvolutionMutationReceipt:
    files = tuple(_mutation_file(item) for item in postflight.facts)
    write_id = write_receipt.write_id
    write_sha = write_receipt.write_sha256
    writer_tool = (
        "evolution_patch_writer"
        if writer_kind == "single_file"
        else "evolution_patch_set_writer"
    )
    rationale = mutation_plan.objective.hypothesis
    evidence = (
        _tool_evidence(
            order=1,
            phase="mutation_generation",
            tool_name="evolution_mutation_generation",
            artifact_id=generation_trace.trace_id,
            artifact_sha256=generation_trace.trace_sha256,
        ),
        _tool_evidence(
            order=2,
            phase="static_guard",
            tool_name="evolution_static_guard",
            artifact_id=static_guard.guard_id,
            artifact_sha256=static_guard.receipt_sha256,
        ),
        _tool_evidence(
            order=3,
            phase="patch_write",
            tool_name=writer_tool,
            artifact_id=write_id,
            artifact_sha256=write_sha,
        ),
        _tool_evidence(
            order=4,
            phase="postflight_guard",
            tool_name="evolution_postflight_guard",
            artifact_id=postflight.postflight_guard_id,
            artifact_sha256=postflight.receipt_sha256,
        ),
    )
    payload = {
        "schema_version": 2,
        "policy_version": MUTATION_RECEIPT_POLICY,
        "created_at": created_at,
        "contract_id": contract.contract_id,
        "contract_manifest_sha256": contract.manifest_sha256,
        "lease_id": source_snapshot.lease_id,
        "source_snapshot_id": source_snapshot.snapshot_id,
        "source_snapshot_sha256": source_snapshot.snapshot_sha256,
        "mutation_plan_id": mutation_plan.plan_id,
        "mutation_plan_sha256": mutation_plan.plan_sha256,
        "mutation_generation_trace_id": generation_trace.trace_id,
        "mutation_generation_trace_sha256": generation_trace.trace_sha256,
        "candidate_id": mutation_plan.candidate_id,
        "candidate_revision": mutation_plan.candidate_revision,
        "candidate_sha256": mutation_plan.candidate_sha256,
        "finding_code": mutation_plan.objective.finding_code,
        "scope": mutation_plan.objective.scope,
        "rationale": rationale,
        "rationale_sha256": hashlib.sha256(rationale.encode("utf-8")).hexdigest(),
        "writer_kind": writer_kind,
        "transaction_id": transaction_id,
        "write_receipt_id": write_id,
        "write_receipt_sha256": write_sha,
        "static_guard_id": static_guard.guard_id,
        "static_guard_sha256": static_guard.receipt_sha256,
        "postflight_guard_id": postflight.postflight_guard_id,
        "postflight_guard_sha256": postflight.receipt_sha256,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "files": [item.model_dump(mode="json") for item in files],
        "files_sha256": _sha256_payload(
            [item.model_dump(mode="json") for item in files]
        ),
        "total_added_lines": sum(item.added_lines for item in files),
        "total_deleted_lines": sum(item.deleted_lines for item in files),
        "required_metrics": mutation_plan.stages[1].metric_names,
        "tool_evidence": [item.model_dump(mode="json") for item in evidence],
        "mutation_completed": True,
        "validation_status": "pending",
        "validation_ready": True,
        "promotion_ready": False,
        "execution_ready": False,
    }
    digest = _sha256_payload(payload)
    return EvolutionMutationReceipt.model_validate({
        **payload,
        "mutation_receipt_id": f"evmr_{digest[:24]}",
        "receipt_sha256": digest,
    })


def _mutation_file(fact: PostflightDiffFact) -> MutationReceiptFile:
    payload = {
        "path": fact.path,
        "operation": fact.operation,
        "before_sha256": fact.before_sha256,
        "after_sha256": fact.after_sha256,
        "unified_diff_sha256": fact.unified_diff_sha256,
        "added_lines": fact.added_lines,
        "deleted_lines": fact.deleted_lines,
        "api_change": fact.api_change,
    }
    return MutationReceiptFile.model_validate({
        **payload,
        "fact_sha256": _sha256_payload(payload),
    })


def _tool_evidence(
    *,
    order: int,
    phase: Literal[
        "mutation_generation",
        "static_guard",
        "patch_write",
        "postflight_guard",
    ],
    tool_name: str,
    artifact_id: str,
    artifact_sha256: str,
) -> MutationToolEvidence:
    return MutationToolEvidence(
        order=order,
        phase=phase,
        tool_name=tool_name,
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
        evidence_uri=f"artifact://evolution/{phase}/{artifact_id}",
    )


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Mutation Receipt created_at 必须是 ISO-8601 时间。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Mutation Receipt created_at 必须包含时区。")
    return parsed.astimezone(UTC)


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "EvolutionMutationReceipt",
    "EvolutionMutationReceiptConflictError",
    "EvolutionMutationReceiptError",
    "EvolutionMutationReceiptService",
    "EvolutionMutationReceiptStore",
    "MutationReceiptFile",
    "MutationToolEvidence",
]
