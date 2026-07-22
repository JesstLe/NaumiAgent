"""Tamper-evident, workspace-bound inputs for Evolution mechanical decisions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.evolution.candidate import EvolutionCandidateDraft
from naumi_agent.evolution.experiments import (
    EvolutionExperimentContractAuthority,
    EvolutionExperimentContractStore,
    EvolutionExperimentContractStoreError,
    default_experiment_budget,
)
from naumi_agent.evolution.final_evaluation_receipts import (
    EvolutionFinalEvaluationReceipt,
    EvolutionFinalEvaluationReceiptError,
    EvolutionFinalEvaluationReceiptStore,
)
from naumi_agent.evolution.mutation_receipts import (
    EvolutionMutationReceipt,
    EvolutionMutationReceiptError,
    EvolutionMutationReceiptStore,
)
from naumi_agent.evolution.store import (
    EvolutionCandidateStore,
    EvolutionStoredCandidate,
    EvolutionStoreError,
)

DECISION_INPUT_POLICY = "evolution-decision-input-v1"
DECISION_CANDIDATE_AUTHORITY_POLICY = "evolution-decision-candidate-authority-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_ARTIFACT_BYTES = 8 * 1_024 * 1_024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionDecisionCandidateAuthority(_StrictModel):
    """Stable envelope around the Candidate Store materialization."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-decision-candidate-authority-v1"] = (
        DECISION_CANDIDATE_AUTHORITY_POLICY
    )
    authority_id: str = Field(pattern=r"^evdca_[0-9a-f]{24}$")
    authority_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_sha256: str = Field(pattern=_SHA256_RE)
    risk_level: Literal["low", "medium", "high", "critical"]
    created_at: str = Field(min_length=1, max_length=100)
    updated_at: str = Field(min_length=1, max_length=100)
    draft: EvolutionCandidateDraft

    @field_validator("workspace_root")
    @classmethod
    def _canonical_workspace(cls, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_absolute() or any(char in value for char in ("\x00", "\r", "\n")):
            raise ValueError("Decision Candidate workspace 必须是安全绝对路径。")
        return str(path.resolve())

    @field_validator("created_at", "updated_at")
    @classmethod
    def _timezone_aware_timestamp(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Decision Candidate timestamp 必须是 ISO-8601。") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Decision Candidate timestamp 必须包含时区。")
        return value

    @model_validator(mode="after")
    def _authority_is_exact(self) -> Self:
        draft_digest = _sha256_payload(self.draft.model_dump(mode="json"))
        if not (
            self.candidate_id == self.draft.candidate_id
            and self.candidate_sha256 == draft_digest
            and self.risk_level == self.draft.risk.level
        ):
            raise ValueError("Decision Candidate Authority 投影不一致。")
        if datetime.fromisoformat(self.created_at.replace("Z", "+00:00")) > (
            datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
        ):
            raise ValueError("Decision Candidate Authority 时间窗口无效。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"authority_id", "authority_sha256"})
        )
        if not hmac.compare_digest(self.authority_sha256, digest):
            raise ValueError("Decision Candidate Authority 摘要不一致。")
        if self.authority_id != f"evdca_{digest[:24]}":
            raise ValueError("Decision Candidate Authority identity 不一致。")
        return self


class EvolutionDecisionConstraints(_StrictModel):
    """User-approved Experiment constraints projected for a mechanical gate."""

    contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    contract_manifest_sha256: str = Field(pattern=_SHA256_RE)
    impact_scope: str = Field(min_length=1, max_length=1_024)
    allowed_files: tuple[str, ...] = Field(min_length=1, max_length=16)
    max_changed_files: int = Field(ge=1, le=16)
    max_changed_lines: int = Field(ge=1, le=2_000)
    max_tool_calls: int = Field(ge=1, le=200)
    max_duration_seconds: int = Field(ge=60, le=3_600)
    max_attempts: int = Field(ge=1, le=3)
    allowed_tools: tuple[str, ...] = Field(min_length=1, max_length=16)
    required_metrics: tuple[str, ...] = Field(min_length=1, max_length=8)
    network_access: Literal[False] = False
    dependency_installation: Literal[False] = False


class EvolutionDecisionInput(_StrictModel):
    """Complete facts for EVO-04.2; this model is never a decision itself."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-decision-input-v1"] = DECISION_INPUT_POLICY
    decision_input_id: str = Field(pattern=r"^evdin_[0-9a-f]{24}$")
    decision_input_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    candidate_sha256: str = Field(pattern=_SHA256_RE)
    risk_level: Literal["low", "medium", "high", "critical"]
    mutation_receipt_id: str = Field(pattern=r"^evmr_[0-9a-f]{24}$")
    mutation_receipt_sha256: str = Field(pattern=_SHA256_RE)
    experiment_contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    experiment_contract_sha256: str = Field(pattern=_SHA256_RE)
    final_evaluation_receipt_id: str = Field(pattern=r"^evfinal_[0-9a-f]{24}$")
    final_evaluation_receipt_sha256: str = Field(pattern=_SHA256_RE)
    candidate: EvolutionDecisionCandidateAuthority
    mutation: EvolutionMutationReceipt
    experiment: EvolutionExperimentContractAuthority
    final_evaluation: EvolutionFinalEvaluationReceipt
    constraints: EvolutionDecisionConstraints
    decision_input_complete: Literal[True] = True
    mechanical_gate_ready: Literal[True] = True
    mechanical_gate_decided: Literal[False] = False
    candidate_acceptance_decided: Literal[False] = False
    promotion_ready: Literal[False] = False
    created_at: str = Field(min_length=1, max_length=100)

    @field_validator("workspace_root")
    @classmethod
    def _canonical_workspace(cls, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_absolute() or any(char in value for char in ("\x00", "\r", "\n")):
            raise ValueError("Decision Input workspace 必须是安全绝对路径。")
        return str(path.resolve())

    @model_validator(mode="after")
    def _input_chain_is_exact(self) -> Self:
        candidate = self.candidate
        mutation = self.mutation
        experiment = self.experiment
        final = self.final_evaluation
        contract = experiment.contract
        request = final.aggregation_contract.batch_request
        file_paths = tuple(item.path for item in mutation.files)
        metric_names = tuple(item.metric_name for item in contract.allowed_checks)
        constraints = _constraints_from_experiment(experiment)
        if not (
            self.workspace_root
            == candidate.workspace_root
            == experiment.workspace_root
            == final.workspace_root
            and self.candidate_id
            == candidate.candidate_id
            == mutation.candidate_id
            == experiment.candidate_id
            == final.candidate_id
            and self.candidate_revision
            == candidate.candidate_revision
            == mutation.candidate_revision
            == experiment.candidate_revision
            == final.candidate_revision
            and self.candidate_sha256
            == candidate.candidate_sha256
            == mutation.candidate_sha256
            == contract.source.candidate_sha256
            and self.risk_level == candidate.risk_level
            and self.mutation_receipt_id
            == mutation.mutation_receipt_id
            == request.mutation_receipt_id
            and self.mutation_receipt_sha256
            == mutation.receipt_sha256
            == request.mutation_receipt_sha256
            and self.experiment_contract_id
            == mutation.contract_id
            == experiment.contract_id
            == request.contract_id
            and self.experiment_contract_sha256
            == mutation.contract_manifest_sha256
            == experiment.contract_manifest_sha256
            == request.contract_manifest_sha256
            and self.final_evaluation_receipt_id == final.receipt_id
            and self.final_evaluation_receipt_sha256 == final.receipt_sha256
        ):
            raise ValueError("Decision Input authority 引用链不一致。")
        if not (
            candidate.draft.finding_code == mutation.finding_code
            and candidate.draft.scope == mutation.scope == contract.scope.impact_scope
            and file_paths == contract.scope.allowed_files
            and mutation.files_sha256 == request.candidate_files_sha256
            and mutation.required_metrics == metric_names
            and mutation.attempt <= mutation.max_attempts <= contract.budget.max_attempts
            and len(file_paths) <= contract.budget.max_changed_files
            and mutation.total_added_lines + mutation.total_deleted_lines
            <= contract.budget.max_changed_lines
            and self.constraints == constraints
            and self.created_at == final.created_at
        ):
            raise ValueError("Decision Input scope、budget 或 constraints 不一致。")
        risk_cap = default_experiment_budget(
            self.risk_level,
            file_count=len(contract.scope.allowed_files),
        )
        for field in (
            "max_changed_files",
            "max_changed_lines",
            "max_tool_calls",
            "max_duration_seconds",
            "max_attempts",
        ):
            if getattr(contract.budget, field) > getattr(risk_cap, field):
                raise ValueError("Decision Input Experiment budget 超过 Candidate risk 上限。")
        digest = _sha256_payload(
            self.model_dump(
                mode="json",
                exclude={"decision_input_id", "decision_input_sha256"},
            )
        )
        if not hmac.compare_digest(self.decision_input_sha256, digest):
            raise ValueError("Decision Input 摘要不一致。")
        if self.decision_input_id != f"evdin_{digest[:24]}":
            raise ValueError("Decision Input identity 不一致。")
        return self


class EvolutionDecisionInputError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionDecisionInputBuilder:
    def build(
        self,
        *,
        stored_candidate: EvolutionStoredCandidate,
        mutation: EvolutionMutationReceipt,
        experiment: EvolutionExperimentContractAuthority,
        final_evaluation: EvolutionFinalEvaluationReceipt,
    ) -> EvolutionDecisionInput:
        try:
            candidate = _candidate_authority(stored_candidate)
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionDecisionInputError(
                "decision_input_candidate_invalid",
                "Candidate authority 无效或已被篡改。",
            ) from exc
        payload = {
            "schema_version": 1,
            "policy_version": DECISION_INPUT_POLICY,
            "workspace_root": candidate.workspace_root,
            "candidate_id": candidate.candidate_id,
            "candidate_revision": candidate.candidate_revision,
            "candidate_sha256": candidate.candidate_sha256,
            "risk_level": candidate.risk_level,
            "mutation_receipt_id": mutation.mutation_receipt_id,
            "mutation_receipt_sha256": mutation.receipt_sha256,
            "experiment_contract_id": experiment.contract_id,
            "experiment_contract_sha256": experiment.contract_manifest_sha256,
            "final_evaluation_receipt_id": final_evaluation.receipt_id,
            "final_evaluation_receipt_sha256": final_evaluation.receipt_sha256,
            "candidate": candidate.model_dump(mode="json"),
            "mutation": mutation.model_dump(mode="json"),
            "experiment": experiment.model_dump(mode="json"),
            "final_evaluation": final_evaluation.model_dump(mode="json"),
            "constraints": _constraints_from_experiment(experiment).model_dump(
                mode="json"
            ),
            "decision_input_complete": True,
            "mechanical_gate_ready": True,
            "mechanical_gate_decided": False,
            "candidate_acceptance_decided": False,
            "promotion_ready": False,
            "created_at": final_evaluation.created_at,
        }
        digest = _sha256_payload(payload)
        try:
            return EvolutionDecisionInput.model_validate({
                **payload,
                "decision_input_id": f"evdin_{digest[:24]}",
                "decision_input_sha256": digest,
            })
        except (TypeError, ValueError) as exc:
            raise EvolutionDecisionInputError(
                "decision_input_authority_mismatch",
                "Decision Input authority、scope、budget 或完整性不一致。",
            ) from exc


class EvolutionDecisionInputStore:
    """Immutable store keyed by one Final Evaluation Receipt."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def record(self, artifact: EvolutionDecisionInput) -> EvolutionDecisionInput:
        try:
            item = EvolutionDecisionInput.model_validate(artifact.model_dump(mode="json"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvolutionDecisionInputError(
                "decision_input_invalid", "Decision Input 无效或已被篡改。"
            ) from exc
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
            raise EvolutionDecisionInputError(
                "decision_input_oversized", "Decision Input 超过 8 MiB 上限。"
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                row = await (
                    await db.execute(
                        "SELECT * FROM evolution_decision_inputs "
                        "WHERE final_evaluation_receipt_id = ?",
                        (item.final_evaluation_receipt_id,),
                    )
                ).fetchone()
                if row is not None:
                    restored = _from_row(row)
                    if restored != item:
                        await db.rollback()
                        raise EvolutionDecisionInputError(
                            "decision_input_conflict",
                            "同一 Final Evaluation Receipt 不可覆盖为不同 Decision Input。",
                        )
                    await db.rollback()
                    return restored
                await db.execute(
                    "INSERT INTO evolution_decision_inputs "
                    "(decision_input_id, decision_input_sha256, workspace_root, "
                    "final_evaluation_receipt_id, artifact_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        item.decision_input_id,
                        item.decision_input_sha256,
                        item.workspace_root,
                        item.final_evaluation_receipt_id,
                        encoded,
                        item.created_at,
                    ),
                )
                await db.commit()
        except EvolutionDecisionInputError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionDecisionInputError(
                "decision_input_store_error", "Decision Input 无法持久化。"
            ) from exc
        restored = await self.get(item.decision_input_id)
        assert restored is not None
        return restored

    async def get(self, decision_input_id: str) -> EvolutionDecisionInput | None:
        if not isinstance(decision_input_id, str) or re.fullmatch(
            r"evdin_[0-9a-f]{24}", decision_input_id
        ) is None:
            raise ValueError("decision_input_id 格式无效。")
        return await self._read("decision_input_id", decision_input_id)

    async def get_by_final_receipt(
        self, final_evaluation_receipt_id: str
    ) -> EvolutionDecisionInput | None:
        if not isinstance(final_evaluation_receipt_id, str) or re.fullmatch(
            r"evfinal_[0-9a-f]{24}", final_evaluation_receipt_id
        ) is None:
            raise ValueError("final_evaluation_receipt_id 格式无效。")
        return await self._read(
            "final_evaluation_receipt_id", final_evaluation_receipt_id
        )

    async def _read(self, column: str, value: str) -> EvolutionDecisionInput | None:
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                row = await (
                    await db.execute(
                        f"SELECT * FROM evolution_decision_inputs WHERE {column} = ?",
                        (value,),
                    )
                ).fetchone()
                return _from_row(row) if row is not None else None
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionDecisionInputError(
                "decision_input_store_corrupt", "Decision Input 损坏或无法读取。"
            ) from exc


class EvolutionDecisionInputExecutor:
    def __init__(
        self,
        *,
        candidate_store: EvolutionCandidateStore,
        mutation_store: EvolutionMutationReceiptStore,
        experiment_store: EvolutionExperimentContractStore,
        final_store: EvolutionFinalEvaluationReceiptStore,
        decision_store: EvolutionDecisionInputStore,
        builder: EvolutionDecisionInputBuilder | None = None,
    ) -> None:
        if not isinstance(candidate_store, EvolutionCandidateStore):
            raise TypeError("Decision Input executor 需要 Candidate Store。")
        if not isinstance(mutation_store, EvolutionMutationReceiptStore):
            raise TypeError("Decision Input executor 需要 Mutation Receipt Store。")
        if not isinstance(experiment_store, EvolutionExperimentContractStore):
            raise TypeError("Decision Input executor 需要 Experiment Contract Store。")
        if not isinstance(final_store, EvolutionFinalEvaluationReceiptStore):
            raise TypeError("Decision Input executor 需要 Final Evaluation Receipt Store。")
        if not isinstance(decision_store, EvolutionDecisionInputStore):
            raise TypeError("Decision Input executor 需要 Decision Input Store。")
        if builder is not None and not isinstance(builder, EvolutionDecisionInputBuilder):
            raise TypeError("Decision Input executor 需要 Decision Input Builder。")
        self._candidate_store = candidate_store
        self._mutation_store = mutation_store
        self._experiment_store = experiment_store
        self._final_store = final_store
        self._decision_store = decision_store
        self._builder = builder or EvolutionDecisionInputBuilder()

    async def execute(
        self,
        *,
        workspace_root: str | Path,
        final_evaluation_receipt_id: str,
    ) -> EvolutionDecisionInput:
        try:
            workspace = Path(workspace_root).expanduser().resolve(strict=True)
        except OSError as exc:
            raise EvolutionDecisionInputError(
                "decision_input_workspace_missing", "Decision Input 工作区不存在。"
            ) from exc
        if not workspace.is_dir():
            raise EvolutionDecisionInputError(
                "decision_input_workspace_missing", "Decision Input 工作区不存在。"
            )
        try:
            final = await self._final_store.get_by_receipt_id(
                final_evaluation_receipt_id.strip()
            )
        except (EvolutionFinalEvaluationReceiptError, TypeError, ValueError) as exc:
            raise EvolutionDecisionInputError(
                "decision_input_final_read_failed", "无法读取 Final Evaluation Receipt。"
            ) from exc
        if final is None:
            raise EvolutionDecisionInputError(
                "decision_input_final_missing", "Final Evaluation Receipt 不存在。"
            )
        if final.workspace_root != str(workspace):
            raise EvolutionDecisionInputError(
                "decision_input_workspace_mismatch",
                "Final Evaluation Receipt 不属于当前工作区。",
            )
        request = final.aggregation_contract.batch_request
        try:
            candidate, mutation, experiment = await asyncio.gather(
                self._candidate_store.get_candidate(workspace, final.candidate_id),
                asyncio.to_thread(
                    self._mutation_store.get, request.mutation_receipt_id
                ),
                self._experiment_store.get(workspace, request.contract_id),
            )
        except (
            EvolutionStoreError,
            EvolutionMutationReceiptError,
            EvolutionExperimentContractStoreError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise EvolutionDecisionInputError(
                "decision_input_authority_read_failed",
                "无法读取 Candidate、Mutation 或 Experiment authority。",
            ) from exc
        missing = tuple(
            name
            for name, item in (
                ("Candidate", candidate),
                ("Mutation Receipt", mutation),
                ("Experiment Contract", experiment),
            )
            if item is None
        )
        if missing:
            raise EvolutionDecisionInputError(
                "decision_input_authority_missing",
                f"Decision Input 缺少 durable authority：{', '.join(missing)}。",
            )
        assert candidate is not None and mutation is not None and experiment is not None
        artifact = self._builder.build(
            stored_candidate=candidate,
            mutation=mutation,
            experiment=experiment,
            final_evaluation=final,
        )
        return await self._decision_store.record(artifact)


def render_decision_input(artifact: EvolutionDecisionInput) -> str:
    item = EvolutionDecisionInput.model_validate(artifact.model_dump(mode="json"))
    changed_lines = item.mutation.total_added_lines + item.mutation.total_deleted_lines
    return "\n".join((
        f"# Evolution Decision Input `{item.decision_input_id}`",
        "",
        "**决策输入已完整冻结；尚未执行 mechanical gate，也没有接受或发布候选。**",
        "",
        f"- Candidate：`{item.candidate_id}` · revision {item.candidate_revision}",
        f"- 风险：`{item.risk_level}`",
        f"- Mutation Receipt：`{item.mutation_receipt_id}`",
        f"- Final Evaluation：`{item.final_evaluation_receipt_id}`",
        f"- Scope：`{item.constraints.impact_scope}`",
        f"- 文件：{len(item.constraints.allowed_files)} / {item.constraints.max_changed_files}",
        f"- 变更行：{changed_lines} / {item.constraints.max_changed_lines}",
        f"- 指标：{', '.join(f'`{value}`' for value in item.constraints.required_metrics)}",
        "- 网络/依赖安装：禁用 / 禁用",
        f"- Decision Input SHA-256：`{item.decision_input_sha256}`",
        "",
        "下一步：EVO-04.2 mechanical gate 只能读取本 authority，并机械判定 veto/pass。",
    ))


def _candidate_authority(stored: EvolutionStoredCandidate) -> EvolutionDecisionCandidateAuthority:
    payload = {
        "schema_version": 1,
        "policy_version": DECISION_CANDIDATE_AUTHORITY_POLICY,
        "workspace_root": stored.workspace_root,
        "candidate_id": stored.draft.candidate_id,
        "candidate_revision": stored.revision,
        "candidate_sha256": stored.draft_sha256,
        "risk_level": stored.draft.risk.level,
        "created_at": stored.created_at,
        "updated_at": stored.updated_at,
        "draft": stored.draft.model_dump(mode="json"),
    }
    digest = _sha256_payload(payload)
    return EvolutionDecisionCandidateAuthority.model_validate({
        **payload,
        "authority_id": f"evdca_{digest[:24]}",
        "authority_sha256": digest,
    })


def _constraints_from_experiment(
    authority: EvolutionExperimentContractAuthority,
) -> EvolutionDecisionConstraints:
    contract = authority.contract
    return EvolutionDecisionConstraints(
        contract_id=contract.contract_id,
        contract_manifest_sha256=contract.manifest_sha256,
        impact_scope=contract.scope.impact_scope,
        allowed_files=contract.scope.allowed_files,
        max_changed_files=contract.budget.max_changed_files,
        max_changed_lines=contract.budget.max_changed_lines,
        max_tool_calls=contract.budget.max_tool_calls,
        max_duration_seconds=contract.budget.max_duration_seconds,
        max_attempts=contract.budget.max_attempts,
        allowed_tools=contract.allowed_tools,
        required_metrics=tuple(item.metric_name for item in contract.allowed_checks),
        network_access=contract.network_access,
        dependency_installation=contract.dependency_installation,
    )


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA busy_timeout = 10000")
    await db.execute(
        """CREATE TABLE IF NOT EXISTS evolution_decision_inputs (
               decision_input_id TEXT PRIMARY KEY,
               decision_input_sha256 TEXT NOT NULL,
               workspace_root TEXT NOT NULL,
               final_evaluation_receipt_id TEXT NOT NULL UNIQUE,
               artifact_json TEXT NOT NULL,
               created_at TEXT NOT NULL
           )"""
    )


def _from_row(row: aiosqlite.Row) -> EvolutionDecisionInput:
    encoded = row["artifact_json"]
    if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise ValueError("Decision Input 持久化内容无效。")
    item = EvolutionDecisionInput.model_validate_json(encoded)
    if not (
        row["decision_input_id"] == item.decision_input_id
        and row["decision_input_sha256"] == item.decision_input_sha256
        and row["workspace_root"] == item.workspace_root
        and row["final_evaluation_receipt_id"]
        == item.final_evaluation_receipt_id
        and row["created_at"] == item.created_at
    ):
        raise ValueError("Decision Input 索引与内容不一致。")
    return item


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "EvolutionDecisionInput",
    "EvolutionDecisionInputBuilder",
    "EvolutionDecisionInputError",
    "EvolutionDecisionInputExecutor",
    "EvolutionDecisionInputStore",
    "render_decision_input",
]
