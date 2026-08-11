"""Durable Population authority over exact remote member finalization receipts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlEvent,
    EvolutionRevalidationRolloutControlState,
)
from naumi_agent.evolution.stable_remote_finalization_authorizations import (
    EvolutionStableRemoteFinalizationAuthorizationEnvelope,
    EvolutionStableRemoteFinalizationConsumptionReceipt,
)
from naumi_agent.evolution.stable_remote_finalizations import (
    EvolutionStableRemoteFinalizationAggregationMaterial,
    EvolutionStableRemoteFinalizationExecutionPackage,
    EvolutionStableRemoteFinalizationReceipt,
    EvolutionStableRemoteFinalizationService,
    EvolutionStableRemoteFinalizationStore,
)
from naumi_agent.release.population_registry import (
    ReleaseManagedInstallationCredential,
    ReleasePopulationRegistryError,
    ReleasePopulationSnapshot,
    ReleasePopulationSnapshotStore,
)

EVOLUTION_STABLE_REMOTE_POPULATION_FINALIZATION_POLICY = (
    "evolution-stable-remote-population-finalization-v1"
)
_RECEIPT_RE = re.compile(r"^evstableremotepopfinal_[0-9a-f]{24}$")
_SNAPSHOT_RE = re.compile(r"^relpopsnapshot_[0-9a-f]{24}$")
_MAX_MEMBERS = 10_000
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_INSPECTION_CONCURRENCY = 32


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionStableRemotePopulationFinalizationMember(_StrictModel):
    schema_version: Literal[1] = 1
    member_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    installation_credential_id: str = Field(pattern=r"^relpopcred_[0-9a-f]{24}$")
    installation_credential_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_public_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    member_receipt_id: str = Field(pattern=r"^evstableremotefinalreceipt_[0-9a-f]{24}$")
    member_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_id: str = Field(pattern=r"^evstableremotefinalauth_[0-9a-f]{24}$")
    authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_attempt: int = Field(ge=1, le=10_000)
    grant_id: str = Field(pattern=r"^evstableremotefinalgrant_[0-9a-f]{24}$")
    grant_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_id: str = Field(pattern=r"^evstableremotefinalresult_[0-9a-f]{24}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_finalization_id: str = Field(pattern=r"^relstablefinal_[0-9a-f]{24}$")
    release_finalization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    expected_pointer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_pointer_generation: int = Field(ge=2, le=1_000_000_000)
    completed_at: str = Field(min_length=1, max_length=100)
    recorded_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if _aware(self.completed_at) > _aware(self.recorded_at):
            raise ValueError("Population member finalization 时间顺序无效。")
        digest = _digest(self.model_dump(mode="json", exclude={"member_source_sha256"}))
        if self.member_source_sha256 != digest:
            raise ValueError("Population member finalization source identity 不一致。")
        return self


class EvolutionStableRemotePopulationFinalizationReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-stable-remote-population-finalization-v1"] = (
        EVOLUTION_STABLE_REMOTE_POPULATION_FINALIZATION_POLICY
    )
    receipt_id: str = Field(pattern=r"^evstableremotepopfinal_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    population_snapshot_id: str = Field(pattern=r"^relpopsnapshot_[0-9a-f]{24}$")
    population_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    population_sequence: int = Field(ge=1, le=1_000_000)
    population_denominator: int = Field(ge=1, le=_MAX_MEMBERS)
    population_completion_receipt_id: str = Field(pattern=r"^evstablepopcomplete_[0-9a-f]{24}$")
    population_completion_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_version: str = Field(min_length=1, max_length=128)
    control_sequence: int = Field(ge=0, le=1_000_000)
    control_event_id: str = Field(default="", pattern=r"^(?:|evrerolloutctrl_[0-9a-f]{24})$")
    control_event_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    members: tuple[EvolutionStableRemotePopulationFinalizationMember, ...] = Field(
        min_length=1,
        max_length=_MAX_MEMBERS,
    )
    finalized_at: str = Field(min_length=1, max_length=100)
    remote_member_receipts_complete: Literal[True] = True
    binary_only: Literal[True] = True
    stable_population_finalization_fact: Literal[True] = True
    population_snapshot_prevalidated: Literal[True] = True
    config_data_finalization_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        member_ids = tuple(item.installation_member_id for item in self.members)
        receipt_ids = tuple(item.member_receipt_id for item in self.members)
        expected_source = _source_set(
            snapshot_id=self.population_snapshot_id,
            snapshot_sha256=self.population_snapshot_sha256,
            members=self.members,
        )
        if not (
            self.population_denominator == len(self.members)
            and member_ids == tuple(sorted(member_ids))
            and len(member_ids) == len(set(member_ids))
            and len(receipt_ids) == len(set(receipt_ids))
            and (self.control_sequence == 0) is (not self.control_event_id)
            and bool(self.control_event_id) is bool(self.control_event_sha256)
            and _aware(self.finalized_at) == max(_aware(item.recorded_at) for item in self.members)
            and self.source_set_sha256 == expected_source
        ):
            raise ValueError("Population finalization Receipt member/source 投影无效。")
        digest = _digest(self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"}))
        if self.receipt_sha256 != digest or self.receipt_id != (
            f"evstableremotepopfinal_{digest[:24]}"
        ):
            raise ValueError("Population finalization Receipt identity 不一致。")
        return self


class EvolutionStableRemotePopulationFinalizationView(_StrictModel):
    receipt: EvolutionStableRemotePopulationFinalizationReceipt
    durable_receipt_valid: bool
    latest_for_snapshot: bool
    population_snapshot_current: bool
    receipt_set_current: bool
    member_authority_current: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=32)
    stable_population_finalization_fact: bool
    stable_population_finalization_authority: bool
    config_data_finalization_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        authority = bool(
            self.durable_receipt_valid
            and self.latest_for_snapshot
            and self.population_snapshot_current
            and self.receipt_set_current
            and self.member_authority_current
        )
        if not (
            self.stable_population_finalization_fact is self.durable_receipt_valid
            and self.stable_population_finalization_authority is authority
            and self.invalidation_reasons == tuple(sorted(set(self.invalidation_reasons)))
        ):
            raise ValueError("Population finalization View authority 投影不一致。")
        return self


class EvolutionStableRemotePopulationFinalizationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStableRemotePopulationFinalizationStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get(
        self,
        receipt_id: str,
    ) -> EvolutionStableRemotePopulationFinalizationReceipt | None:
        item_id = _receipt_id(receipt_id)
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM "
                    "evolution_stable_remote_population_finalizations "
                    "WHERE receipt_id = ?",
                    (item_id,),
                )
            ).fetchone()
        return None if row is None else _restore_receipt(row["receipt_json"])

    async def latest(
        self,
        population_snapshot_id: str,
    ) -> EvolutionStableRemotePopulationFinalizationReceipt | None:
        snapshot_id = _snapshot_id(population_snapshot_id)
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM "
                    "evolution_stable_remote_population_finalizations "
                    "WHERE population_snapshot_id = ? ORDER BY rowid DESC LIMIT 1",
                    (snapshot_id,),
                )
            ).fetchone()
        return None if row is None else _restore_receipt(row["receipt_json"])

    async def record(
        self,
        receipt: EvolutionStableRemotePopulationFinalizationReceipt,
        *,
        workspace_root: Path,
    ) -> EvolutionStableRemotePopulationFinalizationReceipt:
        item = EvolutionStableRemotePopulationFinalizationReceipt.model_validate_json(
            receipt.model_dump_json()
        )
        root = Path(workspace_root).expanduser().resolve(strict=True)
        if item.workspace_root_sha256 != _workspace_sha(root):
            raise EvolutionStableRemotePopulationFinalizationError(
                "stable_remote_population_workspace_mismatch",
                "Population finalization workspace identity 不一致。",
            )
        encoded = item.model_dump_json()
        _bounded(encoded)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            await _require_exact_remote_sources(db, item, workspace_root=root)
            existing = await (
                await db.execute(
                    "SELECT receipt_json FROM "
                    "evolution_stable_remote_population_finalizations "
                    "WHERE source_set_sha256 = ?",
                    (item.source_set_sha256,),
                )
            ).fetchone()
            if existing is not None:
                restored = _restore_receipt(existing["receipt_json"])
                await db.rollback()
                if restored != item:
                    raise EvolutionStableRemotePopulationFinalizationError(
                        "stable_remote_population_source_conflict",
                        "同一 Population finalization source-set 已绑定不同内容。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_stable_remote_population_finalizations "
                "(receipt_id, receipt_sha256, source_set_sha256, "
                "population_snapshot_id, receipt_json, finalized_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    item.receipt_id,
                    item.receipt_sha256,
                    item.source_set_sha256,
                    item.population_snapshot_id,
                    encoded,
                    item.finalized_at,
                ),
            )
            await db.commit()
        return item


class EvolutionStableRemotePopulationFinalizationService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        population_store: ReleasePopulationSnapshotStore,
        member_service: EvolutionStableRemoteFinalizationService,
        store: EvolutionStableRemotePopulationFinalizationStore,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve(strict=True)
        if not (
            isinstance(population_store, ReleasePopulationSnapshotStore)
            and isinstance(member_service, EvolutionStableRemoteFinalizationService)
            and isinstance(store, EvolutionStableRemotePopulationFinalizationStore)
            and member_service.workspace_root == root
            and member_service.store.db_path == store.db_path
        ):
            raise ValueError("Population finalization Service authority composition 不一致。")
        self.workspace_root = root
        self.population_store = population_store
        self.member_service = member_service
        self.member_store: EvolutionStableRemoteFinalizationStore = member_service.store
        self.store = store
        self._locks: dict[str, asyncio.Lock] = {}

    async def complete(
        self,
        *,
        snapshot_id: str | None = None,
    ) -> EvolutionStableRemotePopulationFinalizationView:
        snapshot = await self._current_snapshot(snapshot_id)
        item_id = snapshot.snapshot_id
        lock = self._locks.setdefault(item_id, asyncio.Lock())
        async with lock:
            receipts = await self._exact_member_receipts(snapshot)
            materials = await self._inspect_materials(snapshot, receipts)
            if not all(item.aggregation_authority for item in materials):
                raise EvolutionStableRemotePopulationFinalizationError(
                    "stable_remote_population_member_not_current",
                    "至少一个 member Receipt 未通过当前签名、控制面或凭据重验。",
                )
            receipt = _build_receipt(
                workspace_root=self.workspace_root,
                snapshot=snapshot,
                materials=materials,
            )
            stored = await self.store.record(
                receipt,
                workspace_root=self.workspace_root,
            )
        return await self.inspect(receipt_id=stored.receipt_id)

    async def inspect(
        self,
        *,
        receipt_id: str,
    ) -> EvolutionStableRemotePopulationFinalizationView:
        receipt = await self.store.get(receipt_id)
        if receipt is None:
            raise EvolutionStableRemotePopulationFinalizationError(
                "stable_remote_population_receipt_missing",
                "Population finalization Receipt 不存在。",
            )
        reasons: list[str] = []
        durable = latest = snapshot_current = receipt_set_current = False
        member_authority = False
        try:
            durable = await self.store.get(receipt.receipt_id) == receipt
        except (OSError, RuntimeError, TypeError, ValueError):
            durable = False
        if not durable:
            reasons.append("population_finalization_receipt_changed")
        try:
            latest = await self.store.latest(receipt.population_snapshot_id) == receipt
        except (OSError, RuntimeError, TypeError, ValueError):
            latest = False
        if not latest:
            reasons.append("newer_population_finalization_exists")
        snapshot: ReleasePopulationSnapshot | None = None
        try:
            view = await self.population_store.inspect(snapshot_id=receipt.population_snapshot_id)
            snapshot = view.snapshot
            snapshot_current = bool(
                view.population_snapshot_authority
                and snapshot.snapshot_sha256 == receipt.population_snapshot_sha256
                and snapshot.payload.sequence == receipt.population_sequence
                and snapshot.payload.population_denominator == receipt.population_denominator
            )
        except (ReleasePopulationRegistryError, OSError, TypeError, ValueError):
            snapshot_current = False
        if not snapshot_current:
            reasons.append("population_snapshot_not_current")
        if snapshot is not None:
            try:
                member_receipts = await self._exact_member_receipts(snapshot)
                current_sources = tuple(_member_source(item) for item in member_receipts)
                receipt_set_current = current_sources == receipt.members
                if receipt_set_current:
                    materials = await self._inspect_materials(
                        snapshot,
                        member_receipts,
                    )
                    member_authority = all(item.aggregation_authority for item in materials)
            except (OSError, RuntimeError, TypeError, ValueError):
                receipt_set_current = False
                member_authority = False
        if not receipt_set_current:
            reasons.append("member_receipt_set_changed")
        if not member_authority:
            reasons.append("member_finalization_authority_changed")
        authority = bool(
            durable and latest and snapshot_current and receipt_set_current and member_authority
        )
        return EvolutionStableRemotePopulationFinalizationView(
            receipt=receipt,
            durable_receipt_valid=durable,
            latest_for_snapshot=latest,
            population_snapshot_current=snapshot_current,
            receipt_set_current=receipt_set_current,
            member_authority_current=member_authority,
            invalidation_reasons=tuple(sorted(set(reasons))),
            stable_population_finalization_fact=durable,
            stable_population_finalization_authority=authority,
        )

    async def _current_snapshot(
        self,
        snapshot_id: str | None,
    ) -> ReleasePopulationSnapshot:
        if snapshot_id is None:
            snapshot = await self.population_store.latest("stable")
            if snapshot is None:
                raise EvolutionStableRemotePopulationFinalizationError(
                    "stable_remote_population_snapshot_missing",
                    "Stable Population Snapshot 不存在。",
                )
        else:
            snapshot = await self.population_store.get(_snapshot_id(snapshot_id))
            if snapshot is None:
                raise EvolutionStableRemotePopulationFinalizationError(
                    "stable_remote_population_snapshot_missing",
                    "指定的 Population Snapshot 不存在。",
                )
        view = await self.population_store.inspect(snapshot_id=snapshot.snapshot_id)
        if not view.population_snapshot_authority:
            raise EvolutionStableRemotePopulationFinalizationError(
                "stable_remote_population_snapshot_not_current",
                "Population Snapshot 当前不具备聚合权限。",
            )
        return view.snapshot

    async def _exact_member_receipts(
        self,
        snapshot: ReleasePopulationSnapshot,
    ) -> tuple[EvolutionStableRemoteFinalizationReceipt, ...]:
        receipts = await self.member_store.list_receipts_for_population_snapshot(
            snapshot.snapshot_id
        )
        by_member: dict[str, EvolutionStableRemoteFinalizationReceipt] = {}
        for receipt in receipts:
            authorization = receipt.execution_package.authorization.authorization
            member_id = authorization.installation_member_id
            if member_id in by_member:
                raise EvolutionStableRemotePopulationFinalizationError(
                    "stable_remote_population_member_receipt_conflict",
                    "同一 Population member 存在多个 Finalization Receipt。",
                )
            by_member[member_id] = receipt
        credentials = {item.payload.member_id: item for item in snapshot.payload.credentials}
        if set(by_member) != set(credentials):
            raise EvolutionStableRemotePopulationFinalizationError(
                "stable_remote_population_member_receipt_missing",
                "Population member Finalization Receipt 集合不完整。",
            )
        ordered: list[EvolutionStableRemoteFinalizationReceipt] = []
        for member_id in sorted(credentials):
            receipt = by_member[member_id]
            authorization = receipt.execution_package.authorization.authorization
            credential = credentials[member_id]
            if not (
                authorization.population_snapshot_sha256 == snapshot.snapshot_sha256
                and authorization.installation_credential_id == credential.credential_id
                and authorization.installation_credential_sha256 == credential.credential_sha256
                and authorization.installation_public_key_sha256
                == credential.payload.installation_public_key_sha256
            ):
                raise EvolutionStableRemotePopulationFinalizationError(
                    "stable_remote_population_member_identity_mismatch",
                    "Member Receipt 与 current Population Credential 不一致。",
                )
            ordered.append(receipt)
        return tuple(ordered)

    async def _inspect_materials(
        self,
        snapshot: ReleasePopulationSnapshot,
        receipts: tuple[EvolutionStableRemoteFinalizationReceipt, ...],
    ) -> tuple[EvolutionStableRemoteFinalizationAggregationMaterial, ...]:
        credentials: dict[str, ReleaseManagedInstallationCredential] = {
            item.payload.member_id: item for item in snapshot.payload.credentials
        }
        semaphore = asyncio.Semaphore(_INSPECTION_CONCURRENCY)

        async def inspect(
            receipt: EvolutionStableRemoteFinalizationReceipt,
        ) -> EvolutionStableRemoteFinalizationAggregationMaterial:
            member_id = receipt.execution_package.authorization.authorization.installation_member_id
            async with semaphore:
                return await self.member_service.inspect_aggregation_material(
                    receipt_id=receipt.receipt_id,
                    credential=credentials[member_id],
                )

        return tuple(await asyncio.gather(*(inspect(item) for item in receipts)))


def _build_receipt(
    *,
    workspace_root: Path,
    snapshot: ReleasePopulationSnapshot,
    materials: tuple[EvolutionStableRemoteFinalizationAggregationMaterial, ...],
) -> EvolutionStableRemotePopulationFinalizationReceipt:
    members = tuple(
        sorted(
            (_member_source(item.receipt) for item in materials),
            key=lambda item: item.installation_member_id,
        )
    )
    authorizations = tuple(
        item.receipt.execution_package.authorization.authorization for item in materials
    )
    completion_ids = {
        (item.completion_receipt_id, item.completion_receipt_sha256) for item in authorizations
    }
    versions = {item.candidate_version for item in authorizations}
    controls = {
        (item.control_sequence, item.control_event_id, item.control_event_sha256)
        for item in authorizations
    }
    if not (len(completion_ids) == len(versions) == len(controls) == 1):
        raise EvolutionStableRemotePopulationFinalizationError(
            "stable_remote_population_authority_conflict",
            "Member Receipt 未绑定同一 Population Completion、版本或控制代。",
        )
    completion_id, completion_sha = next(iter(completion_ids))
    control_sequence, control_event_id, control_event_sha = next(iter(controls))
    source_set = _source_set(
        snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.snapshot_sha256,
        members=members,
    )
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_REMOTE_POPULATION_FINALIZATION_POLICY,
        "source_set_sha256": source_set,
        "workspace_root_sha256": _workspace_sha(workspace_root),
        "population_snapshot_id": snapshot.snapshot_id,
        "population_snapshot_sha256": snapshot.snapshot_sha256,
        "population_sequence": snapshot.payload.sequence,
        "population_denominator": snapshot.payload.population_denominator,
        "population_completion_receipt_id": completion_id,
        "population_completion_receipt_sha256": completion_sha,
        "candidate_version": next(iter(versions)),
        "control_sequence": control_sequence,
        "control_event_id": control_event_id,
        "control_event_sha256": control_event_sha,
        "members": tuple(item.model_dump(mode="json") for item in members),
        "finalized_at": max(_aware(item.recorded_at) for item in members).isoformat(),
        "remote_member_receipts_complete": True,
        "binary_only": True,
        "stable_population_finalization_fact": True,
        "population_snapshot_prevalidated": True,
        "config_data_finalization_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    return EvolutionStableRemotePopulationFinalizationReceipt.model_validate(
        {
            **core,
            "receipt_id": f"evstableremotepopfinal_{digest[:24]}",
            "receipt_sha256": digest,
        }
    )


def _member_source(
    receipt: EvolutionStableRemoteFinalizationReceipt,
) -> EvolutionStableRemotePopulationFinalizationMember:
    package = receipt.execution_package
    authorization = package.authorization.authorization
    result = receipt.submission.result
    finalization = result.release_finalization
    core = {
        "schema_version": 1,
        "installation_member_id": authorization.installation_member_id,
        "installation_credential_id": authorization.installation_credential_id,
        "installation_credential_sha256": (authorization.installation_credential_sha256),
        "installation_public_key_sha256": (authorization.installation_public_key_sha256),
        "member_receipt_id": receipt.receipt_id,
        "member_receipt_sha256": receipt.receipt_sha256,
        "authorization_id": authorization.authorization_id,
        "authorization_sha256": authorization.authorization_sha256,
        "authorization_attempt": authorization.attempt,
        "grant_id": package.grant.grant_id,
        "grant_sha256": package.grant.grant_sha256,
        "result_id": result.result_id,
        "result_sha256": result.result_sha256,
        "release_finalization_id": finalization.finalization_id,
        "release_finalization_sha256": finalization.finalization_sha256,
        "expected_pointer_id": package.grant.expected_pointer_id,
        "expected_pointer_sha256": package.grant.expected_pointer_sha256,
        "expected_pointer_generation": package.grant.expected_pointer_generation,
        "completed_at": result.completed_at,
        "recorded_at": receipt.recorded_at,
    }
    return EvolutionStableRemotePopulationFinalizationMember.model_validate(
        {
            **core,
            "member_source_sha256": _digest(core),
        }
    )


async def _require_exact_remote_sources(
    db: aiosqlite.Connection,
    receipt: EvolutionStableRemotePopulationFinalizationReceipt,
    *,
    workspace_root: Path,
) -> None:
    remote_receipts = await _read_remote_receipts(
        db,
        receipt.population_snapshot_id,
    )
    try:
        actual_members = tuple(
            sorted(
                (_member_source(item) for item in remote_receipts),
                key=lambda item: item.installation_member_id,
            )
        )
    except ValueError as exc:
        raise EvolutionStableRemotePopulationFinalizationError(
            "stable_remote_population_source_invalid",
            "Remote member Receipt source 无法验证。",
        ) from exc
    member_ids = tuple(item.installation_member_id for item in actual_members)
    if len(member_ids) != len(set(member_ids)):
        raise EvolutionStableRemotePopulationFinalizationError(
            "stable_remote_population_member_receipt_conflict",
            "Writer fence 检测到同一 member 的多个 Receipt。",
        )
    if actual_members != receipt.members:
        raise EvolutionStableRemotePopulationFinalizationError(
            "stable_remote_population_receipt_set_changed",
            "Member Receipt 集合在 Population writer 前已变化。",
        )
    authorizations = tuple(
        item.execution_package.authorization.authorization for item in remote_receipts
    )
    source_metadata = {
        (
            item.workspace_root_sha256,
            item.population_snapshot_id,
            item.population_snapshot_sha256,
            item.completion_receipt_id,
            item.completion_receipt_sha256,
            item.candidate_version,
            item.control_sequence,
            item.control_event_id,
            item.control_event_sha256,
        )
        for item in authorizations
    }
    expected_metadata = (
        receipt.workspace_root_sha256,
        receipt.population_snapshot_id,
        receipt.population_snapshot_sha256,
        receipt.population_completion_receipt_id,
        receipt.population_completion_receipt_sha256,
        receipt.candidate_version,
        receipt.control_sequence,
        receipt.control_event_id,
        receipt.control_event_sha256,
    )
    if source_metadata != {expected_metadata}:
        raise EvolutionStableRemotePopulationFinalizationError(
            "stable_remote_population_metadata_mismatch",
            "Population Receipt metadata 与 exact member Authorization 不一致。",
        )
    for remote_receipt in remote_receipts:
        package = remote_receipt.execution_package
        authorization = package.authorization
        consumption = package.consumption
        package_row = await (
            await db.execute(
                "SELECT package_json FROM evolution_stable_remote_finalization_grants "
                "WHERE grant_id = ?",
                (package.grant.grant_id,),
            )
        ).fetchone()
        authorization_row = await (
            await db.execute(
                "SELECT envelope_json FROM "
                "evolution_stable_remote_finalization_authorizations "
                "WHERE authorization_id = ?",
                (authorization.authorization.authorization_id,),
            )
        ).fetchone()
        consumption_row = await (
            await db.execute(
                "SELECT receipt_json FROM "
                "evolution_stable_remote_finalization_consumptions "
                "WHERE authorization_id = ?",
                (authorization.authorization.authorization_id,),
            )
        ).fetchone()
        try:
            durable_package = (
                package_row is not None
                and EvolutionStableRemoteFinalizationExecutionPackage.model_validate_json(
                    package_row["package_json"]
                )
                == package
            )
            durable_authorization = (
                authorization_row is not None
                and EvolutionStableRemoteFinalizationAuthorizationEnvelope.model_validate_json(
                    authorization_row["envelope_json"]
                )
                == authorization
            )
            durable_consumption = (
                consumption_row is not None
                and EvolutionStableRemoteFinalizationConsumptionReceipt.model_validate_json(
                    consumption_row["receipt_json"]
                )
                == consumption
            )
        except ValueError as exc:
            raise EvolutionStableRemotePopulationFinalizationError(
                "stable_remote_population_durable_source_corrupt",
                "Remote finalization durable source 已损坏。",
            ) from exc
        if not (durable_package and durable_authorization and durable_consumption):
            raise EvolutionStableRemotePopulationFinalizationError(
                "stable_remote_population_durable_source_changed",
                "Remote finalization durable Grant/Authorization/Consumption 已变化。",
            )
    row = await (
        await db.execute(
            "SELECT event_json FROM evolution_revalidation_rollout_control_events "
            "WHERE workspace_root = ? ORDER BY sequence DESC LIMIT 1",
            (str(workspace_root),),
        )
    ).fetchone()
    try:
        control = (
            None
            if row is None
            else EvolutionRevalidationRolloutControlEvent.model_validate_json(row["event_json"])
        )
    except (TypeError, ValueError) as exc:
        raise EvolutionStableRemotePopulationFinalizationError(
            "stable_remote_population_control_corrupt",
            "Rollout control durable source 已损坏。",
        ) from exc
    actual_control = (
        (0, "", "")
        if control is None
        else (control.sequence, control.event_id, control.event_sha256)
    )
    if actual_control != (
        receipt.control_sequence,
        receipt.control_event_id,
        receipt.control_event_sha256,
    ) or (
        control is not None and control.state is not EvolutionRevalidationRolloutControlState.ACTIVE
    ):
        raise EvolutionStableRemotePopulationFinalizationError(
            "stable_remote_population_control_changed",
            "Rollout control generation 在 Population writer 前已变化。",
        )


async def _read_remote_receipts(
    db: aiosqlite.Connection,
    population_snapshot_id: str,
) -> tuple[EvolutionStableRemoteFinalizationReceipt, ...]:
    try:
        rows = await (
            await db.execute(
                "SELECT receipt_json FROM "
                "evolution_stable_remote_finalization_receipts "
                "WHERE json_valid(receipt_json) = 1 AND "
                "json_extract(receipt_json, "
                "'$.execution_package.authorization.authorization."
                "population_snapshot_id') = ? ORDER BY receipt_id LIMIT ?",
                (_snapshot_id(population_snapshot_id), _MAX_MEMBERS + 1),
            )
        ).fetchall()
    except aiosqlite.Error as exc:
        raise EvolutionStableRemotePopulationFinalizationError(
            "stable_remote_population_member_receipts_unavailable",
            "Remote member Receipt source 不可用。",
        ) from exc
    if len(rows) > _MAX_MEMBERS:
        raise EvolutionStableRemotePopulationFinalizationError(
            "stable_remote_population_member_limit_exceeded",
            "Remote member Receipt 数量超过 Population 上限。",
        )
    try:
        return tuple(
            EvolutionStableRemoteFinalizationReceipt.model_validate_json(row["receipt_json"])
            for row in rows
        )
    except ValueError as exc:
        raise EvolutionStableRemotePopulationFinalizationError(
            "stable_remote_population_member_receipt_corrupt",
            "Remote member Receipt 已损坏。",
        ) from exc


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS "
        "evolution_stable_remote_population_finalizations ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "source_set_sha256 TEXT NOT NULL UNIQUE, "
        "population_snapshot_id TEXT NOT NULL, receipt_json TEXT NOT NULL, "
        "finalized_at TEXT NOT NULL)"
    )


def _source_set(
    *,
    snapshot_id: str,
    snapshot_sha256: str,
    members: tuple[EvolutionStableRemotePopulationFinalizationMember, ...],
) -> str:
    return _digest(
        {
            "population_snapshot_id": snapshot_id,
            "population_snapshot_sha256": snapshot_sha256,
            "member_source_sha256": tuple(item.member_source_sha256 for item in members),
        }
    )


def _restore_receipt(value: str) -> EvolutionStableRemotePopulationFinalizationReceipt:
    try:
        return EvolutionStableRemotePopulationFinalizationReceipt.model_validate_json(value)
    except (TypeError, ValueError) as exc:
        raise EvolutionStableRemotePopulationFinalizationError(
            "stable_remote_population_receipt_corrupt",
            "Population finalization durable Receipt 已损坏。",
        ) from exc


def _bounded(value: str) -> None:
    if not 1 <= len(value.encode()) <= _MAX_ARTIFACT_BYTES:
        raise EvolutionStableRemotePopulationFinalizationError(
            "stable_remote_population_receipt_oversized",
            "Population finalization Receipt 超过 16 MiB。",
        )


def _receipt_id(value: str) -> str:
    item = str(value or "").strip()
    if _RECEIPT_RE.fullmatch(item) is None:
        raise ValueError("Population finalization receipt_id 格式无效。")
    return item


def _snapshot_id(value: str) -> str:
    item = str(value or "").strip()
    if _SNAPSHOT_RE.fullmatch(item) is None:
        raise ValueError("Population finalization snapshot_id 格式无效。")
    return item


def _workspace_sha(path: Path) -> str:
    return hashlib.sha256(str(path).encode()).hexdigest()


def _aware(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ValueError("Population finalization timestamp 必须包含时区。")
    return parsed.astimezone(UTC)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def render_stable_remote_population_finalization(
    view: EvolutionStableRemotePopulationFinalizationView,
) -> str:
    receipt = view.receipt
    status = "已完成" if view.stable_population_finalization_authority else "已撤权"
    lines = [
        "## Remote Population Finalization",
        "",
        f"- 状态：**{status}**",
        f"- Receipt：`{receipt.receipt_id}`",
        f"- Population：`{receipt.population_snapshot_id}`",
        f"- Candidate：`{receipt.candidate_version}`",
        f"- Members：`{len(receipt.members)}/{receipt.population_denominator}`",
        f"- Source set：`{receipt.source_set_sha256}`",
        f"- Historical fact：`{str(view.stable_population_finalization_fact).lower()}`",
        f"- Current authority：`{str(view.stable_population_finalization_authority).lower()}`",
        "- Config/data finalization authority：`false`",
        "- Promotion authority：`false`",
    ]
    if view.invalidation_reasons:
        lines.append("- 撤权原因：" + "、".join(view.invalidation_reasons))
    return "\n".join(lines)


__all__ = [
    "EVOLUTION_STABLE_REMOTE_POPULATION_FINALIZATION_POLICY",
    "EvolutionStableRemotePopulationFinalizationError",
    "EvolutionStableRemotePopulationFinalizationMember",
    "EvolutionStableRemotePopulationFinalizationReceipt",
    "EvolutionStableRemotePopulationFinalizationService",
    "EvolutionStableRemotePopulationFinalizationStore",
    "EvolutionStableRemotePopulationFinalizationView",
    "render_stable_remote_population_finalization",
]
