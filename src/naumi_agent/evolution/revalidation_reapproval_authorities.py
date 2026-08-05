"""Fresh Final Evaluation gate for mandatory professional reapproval."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_final_evaluations import (
    EvolutionRevalidationFinalEvaluationStore,
)
from naumi_agent.evolution.revalidation_runtime_contracts import (
    EvolutionRevalidationRuntimeContractService,
)

EVOLUTION_REVALIDATION_REAPPROVAL_AUTHORITY_POLICY = (
    "evolution-revalidation-reapproval-authority-v1"
)
_SHA256_RE = r"^[0-9a-f]{64}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionRevalidationReapprovalAuthority(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-revalidation-reapproval-authority-v1"] = (
        EVOLUTION_REVALIDATION_REAPPROVAL_AUTHORITY_POLICY
    )
    authority_id: str = Field(pattern=r"^evreapproval_[0-9a-f]{24}$")
    authority_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    contract_id: str = Field(pattern=r"^evrevalruntime_[0-9a-f]{24}$")
    contract_sha256: str = Field(pattern=_SHA256_RE)
    validation_plan_id: str = Field(pattern=r"^evrevalvplan_[0-9a-f]{24}$")
    validation_plan_sha256: str = Field(pattern=_SHA256_RE)
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=1)
    final_evaluation_id: str = Field(pattern=r"^evrevalfinal_[0-9a-f]{24}$")
    final_evaluation_sha256: str = Field(pattern=_SHA256_RE)
    required_platforms: tuple[Literal["linux", "macos", "windows"], ...] = Field(
        min_length=1, max_length=3
    )
    required_roles: tuple[
        Literal[
            "user",
            "independent_reviewer",
            "security_reviewer",
            "data_owner",
            "release_manager",
        ], ...
    ] = Field(min_length=3, max_length=5)
    prior_approval_reusable: Literal[False] = False
    prior_signature_reusable: Literal[False] = False
    professional_role_resign_required: Literal[True] = True
    fresh_interaction_required: Literal[True] = True
    source_current_at_issue: Literal[True] = True
    reapproval_authorized: Literal[True] = True
    approval_decided: Literal[False] = False
    promotion_authority: Literal[False] = False
    issued_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Reapproval authority workspace 必须 canonical。")
        if self.required_roles != tuple(dict.fromkeys(self.required_roles)):
            raise ValueError("Reapproval required roles 不得重复。")
        if datetime.fromisoformat(self.issued_at).utcoffset() is None:
            raise ValueError("Reapproval authority issued_at 必须包含 offset。")
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"authority_id", "authority_sha256"})
        )
        if not hmac.compare_digest(self.authority_sha256, digest):
            raise ValueError("Reapproval authority digest 不一致。")
        if self.authority_id != f"evreapproval_{digest[:24]}":
            raise ValueError("Reapproval authority identity 不一致。")
        return self


class EvolutionRevalidationReapprovalAuthorityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionRevalidationReapprovalAuthorityStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()

    async def get(self, contract_id: str):
        if not self._db_path.exists():
            return None
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (await db.execute(
                "SELECT authority_json FROM evolution_revalidation_reapproval_authorities "
                "WHERE contract_id = ?", (contract_id,)
            )).fetchone()
        return None if row is None else (
            EvolutionRevalidationReapprovalAuthority.model_validate_json(
                row["authority_json"]
            )
        )

    async def record(self, authority):
        item = EvolutionRevalidationReapprovalAuthority.model_validate_json(
            authority.model_dump_json()
        )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            final = await (await db.execute(
                "SELECT receipt_sha256 FROM evolution_revalidation_final_evaluations "
                "WHERE contract_id = ? AND receipt_id = ?",
                (item.contract_id, item.final_evaluation_id),
            )).fetchone()
            if final is None or final["receipt_sha256"] != item.final_evaluation_sha256:
                await db.rollback()
                raise EvolutionRevalidationReapprovalAuthorityError(
                    "fresh_reapproval_final_dependency_mismatch",
                    "Fresh Final Evaluation 持久化依赖不一致。",
                )
            existing = await (await db.execute(
                "SELECT authority_json FROM evolution_revalidation_reapproval_authorities "
                "WHERE contract_id = ?", (item.contract_id,)
            )).fetchone()
            if existing is not None:
                restored = EvolutionRevalidationReapprovalAuthority.model_validate_json(
                    existing["authority_json"]
                )
                await db.rollback()
                if restored != item:
                    raise EvolutionRevalidationReapprovalAuthorityError(
                        "fresh_reapproval_authority_conflict",
                        "同一 Runtime Contract 已绑定不同 Reapproval authority。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_revalidation_reapproval_authorities "
                "(authority_id, authority_sha256, contract_id, authority_json, issued_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (item.authority_id, item.authority_sha256, item.contract_id,
                 item.model_dump_json(), item.issued_at),
            )
            await db.commit()
        return item


class EvolutionRevalidationReapprovalAuthorityService:
    def __init__(self, *, workspace_root, contract_service, final_store, store):
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.contract_service: EvolutionRevalidationRuntimeContractService = contract_service
        self.final_store: EvolutionRevalidationFinalEvaluationStore = final_store
        self.store: EvolutionRevalidationReapprovalAuthorityStore = store

    async def issue(self, *, contract_id: str):
        view = await self.contract_service.inspect(
            workspace_root=self.workspace_root, contract_id=contract_id
        )
        if not view.execution_eligible:
            raise EvolutionRevalidationReapprovalAuthorityError(
                "fresh_reapproval_contract_not_ready",
                f"Fresh Runtime Contract 当前为 {view.current_status}。",
            )
        final = await self.final_store.get(contract_id)
        if final is None:
            raise EvolutionRevalidationReapprovalAuthorityError(
                "fresh_reapproval_final_missing", "Fresh Final Evaluation 尚未签发。"
            )
        contract = view.contract
        if final.contract != contract or not final.source_current_at_issue:
            raise EvolutionRevalidationReapprovalAuthorityError(
                "fresh_reapproval_final_mismatch",
                "Fresh Final Evaluation 与当前 Runtime Contract 不一致。",
            )
        if not final.reapproval_eligible:
            raise EvolutionRevalidationReapprovalAuthorityError(
                "fresh_reapproval_evaluation_blocked",
                "Fresh Final Evaluation 未通过重新审批资格门禁。",
            )
        roles = (
            "user",
            "independent_reviewer",
            "security_reviewer",
            "release_manager",
        )
        payload = {
            "schema_version": 1,
            "policy_version": EVOLUTION_REVALIDATION_REAPPROVAL_AUTHORITY_POLICY,
            "workspace_root": contract.workspace_root,
            "contract_id": contract.contract_id,
            "contract_sha256": contract.contract_sha256,
            "validation_plan_id": contract.validation_plan_id,
            "validation_plan_sha256": contract.validation_plan_sha256,
            "candidate_id": contract.candidate_id,
            "candidate_revision": contract.candidate_revision,
            "final_evaluation_id": final.receipt_id,
            "final_evaluation_sha256": final.receipt_sha256,
            "required_platforms": list(contract.required_platforms),
            "required_roles": list(roles),
            "prior_approval_reusable": False,
            "prior_signature_reusable": False,
            "professional_role_resign_required": True,
            "fresh_interaction_required": True,
            "source_current_at_issue": True,
            "reapproval_authorized": True,
            "approval_decided": False,
            "promotion_authority": False,
            "issued_at": final.created_at,
        }
        digest = _sha256_payload(payload)
        return await self.store.record(
            EvolutionRevalidationReapprovalAuthority.model_validate({
                **payload,
                "authority_id": f"evreapproval_{digest[:24]}",
                "authority_sha256": digest,
            })
        )


async def _ensure_schema(db):
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_revalidation_reapproval_authorities ("
        "authority_id TEXT PRIMARY KEY, authority_sha256 TEXT NOT NULL UNIQUE, "
        "contract_id TEXT NOT NULL UNIQUE, authority_json TEXT NOT NULL, "
        "issued_at TEXT NOT NULL)"
    )
    await db.commit()


def _sha256_payload(payload):
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


__all__ = [
    "EVOLUTION_REVALIDATION_REAPPROVAL_AUTHORITY_POLICY",
    "EvolutionRevalidationReapprovalAuthority",
    "EvolutionRevalidationReapprovalAuthorityError",
    "EvolutionRevalidationReapprovalAuthorityService",
    "EvolutionRevalidationReapprovalAuthorityStore",
]
