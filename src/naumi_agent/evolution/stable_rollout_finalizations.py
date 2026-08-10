"""Crash-recoverable member finalization for an authorized stable rollout."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from weakref import WeakValueDictionary

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlEvent,
    EvolutionRevalidationRolloutControlState,
)
from naumi_agent.evolution.stable_rollout_authorizations import (
    EvolutionStableRolloutAuthorization,
    EvolutionStableRolloutAuthorizationService,
    EvolutionStableRolloutAuthorizationView,
    EvolutionStableRolloutConsumptionReceipt,
)
from naumi_agent.release.slots import (
    ReleaseSlotError,
    ReleaseSlotStore,
    ReleaseStableMemberFinalization,
    ReleaseStableMemberFinalizationAuthority,
)

EVOLUTION_STABLE_ROLLOUT_FINALIZATION_POLICY = "evolution-stable-rollout-member-finalization-v1"
_MAX_ARTIFACT_BYTES = 512 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class EvolutionStableRolloutFinalizationReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-stable-rollout-member-finalization-v1"] = (
        EVOLUTION_STABLE_ROLLOUT_FINALIZATION_POLICY
    )
    receipt_id: str = Field(pattern=r"^evstablerolloutfinal_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_root: str = Field(min_length=1, max_length=4096)
    authorization: EvolutionStableRolloutAuthorization
    consumption: EvolutionStableRolloutConsumptionReceipt
    release_finalization: ReleaseStableMemberFinalization
    authorization_consumed: Literal[True] = True
    expected_pointer_cas_satisfied: Literal[True] = True
    binary_stable_member_finalized: Literal[True] = True
    config_data_mutation_executed: Literal[False] = False
    deployment_executed: Literal[False] = False
    rollback_executed: Literal[False] = False
    promotion_executed: Literal[False] = False
    completed_at: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exact(self) -> Self:
        item = self.authorization
        finalization = self.release_finalization
        authority = finalization.authority
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Stable Rollout Finalization workspace 必须 canonical。")
        if not (
            self.consumption.authorization_id == item.authorization_id
            and self.consumption.authorization_sha256 == item.authorization_sha256
            and self.consumption.start_nonce_sha256 == item.start_nonce_sha256
            and authority.authority_id == item.authorization_id
            and authority.authority_sha256 == item.authorization_sha256
            and authority.completion_receipt_id == item.completion_receipt_id
            and authority.completion_receipt_sha256 == item.completion_receipt_sha256
            and authority.installation_member_id == item.installation_member_id
            and authority.expected_pointer_id == item.expected_active_pointer_id
            and authority.expected_pointer_sha256 == item.expected_active_pointer_sha256
            and authority.expected_pointer_generation == item.expected_active_pointer_generation
        ):
            raise ValueError("Stable Rollout Finalization authority projection 不一致。")
        consumed_at = _aware(self.consumption.consumed_at)
        finalized_at = _aware(finalization.finalized_at)
        completed_at = _aware(self.completed_at)
        if not (
            consumed_at <= finalized_at == completed_at and finalized_at < _aware(item.expires_at)
        ):
            raise ValueError("Stable Rollout Finalization 时间边界不一致。")
        core = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _digest(core)
        if self.receipt_sha256 != digest or self.receipt_id != (
            f"evstablerolloutfinal_{digest[:24]}"
        ):
            raise ValueError("Stable Rollout Finalization identity 不一致。")
        return self


class EvolutionStableRolloutFinalizationView(_StrictModel):
    receipt: EvolutionStableRolloutFinalizationReceipt
    authorization_source_valid: bool
    consumption_source_valid: bool
    release_finalization_valid: bool
    control_history_valid: bool
    authorization_sources_current: bool
    active_pointer_current: bool
    stable_member_completion_fact: bool
    active_stable_member_authority: bool
    stable_population_rollout_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        fact = bool(
            self.authorization_source_valid
            and self.consumption_source_valid
            and self.release_finalization_valid
            and self.control_history_valid
        )
        current = bool(fact and self.authorization_sources_current and self.active_pointer_current)
        if not (
            self.stable_member_completion_fact is fact
            and self.active_stable_member_authority is current
        ):
            raise ValueError("Stable Rollout Finalization View 投影不一致。")
        return self


class EvolutionStableRolloutFinalizationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStableRolloutFinalizationStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get_by_authorization(
        self, authorization_id: str
    ) -> EvolutionStableRolloutFinalizationReceipt | None:
        if re.fullmatch(r"evstablerolloutauth_[0-9a-f]{24}", authorization_id) is None:
            raise ValueError("authorization_id 格式无效。")
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_stable_rollout_finalizations "
                    "WHERE authorization_id = ?",
                    (authorization_id,),
                )
            ).fetchone()
        return None if row is None else _restore_receipt(row["receipt_json"])

    async def record(
        self, receipt: EvolutionStableRolloutFinalizationReceipt
    ) -> EvolutionStableRolloutFinalizationReceipt:
        item = EvolutionStableRolloutFinalizationReceipt.model_validate_json(
            receipt.model_dump_json()
        )
        encoded = item.model_dump_json()
        if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
            raise EvolutionStableRolloutFinalizationError(
                "stable_rollout_finalization_oversized",
                "Stable Rollout Finalization Receipt 超过 512 KiB。",
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            authorization_row = await (
                await db.execute(
                    "SELECT authorization_json FROM evolution_stable_rollout_authorizations "
                    "WHERE authorization_id = ?",
                    (item.authorization.authorization_id,),
                )
            ).fetchone()
            consumption_row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_stable_rollout_consumptions "
                    "WHERE authorization_id = ?",
                    (item.authorization.authorization_id,),
                )
            ).fetchone()
            if not (
                authorization_row is not None
                and consumption_row is not None
                and EvolutionStableRolloutAuthorization.model_validate_json(
                    authorization_row["authorization_json"]
                )
                == item.authorization
                and EvolutionStableRolloutConsumptionReceipt.model_validate_json(
                    consumption_row["receipt_json"]
                )
                == item.consumption
            ):
                raise EvolutionStableRolloutFinalizationError(
                    "stable_rollout_finalization_source_changed",
                    "Stable Rollout Authorization 或 Consumption durable source 已变化。",
                )
            await _require_control_active_at_finalization(db, item)
            existing = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_stable_rollout_finalizations "
                    "WHERE authorization_id = ?",
                    (item.authorization.authorization_id,),
                )
            ).fetchone()
            if existing is not None:
                restored = _restore_receipt(existing["receipt_json"])
                await db.rollback()
                if restored != item:
                    raise EvolutionStableRolloutFinalizationError(
                        "stable_rollout_finalization_conflict",
                        "同一 Authorization 已绑定不同 Finalization Receipt。",
                    )
                return restored
            await db.execute(
                "INSERT INTO evolution_stable_rollout_finalizations "
                "(receipt_id, receipt_sha256, authorization_id, completion_receipt_id, "
                "installation_member_id, receipt_json, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item.receipt_id,
                    item.receipt_sha256,
                    item.authorization.authorization_id,
                    item.authorization.completion_receipt_id,
                    item.authorization.installation_member_id,
                    encoded,
                    item.completed_at,
                ),
            )
            await db.commit()
        return item

    async def control_history_valid(
        self, receipt: EvolutionStableRolloutFinalizationReceipt
    ) -> bool:
        if not self.db_path.is_file():
            return False
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await _require_control_active_at_finalization(db, receipt)
            return True
        except (
            EvolutionStableRolloutFinalizationError,
            aiosqlite.Error,
            OSError,
            TypeError,
            ValueError,
        ):
            return False


class EvolutionStableRolloutFinalizationService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        authorization_service: EvolutionStableRolloutAuthorizationService,
        release_slot_store: ReleaseSlotStore,
        store: EvolutionStableRolloutFinalizationStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.authorization_service = authorization_service
        self.release_slot_store = release_slot_store
        self.store = store
        self.clock = clock
        if authorization_service.workspace_root != self.workspace_root:
            raise ValueError("Stable Rollout Finalization workspace identity 不一致。")
        if authorization_service.store.db_path != store.db_path:
            raise ValueError("Stable Rollout Finalization 必须共用同一证据数据库。")
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def execute(self, *, authorization_id: str) -> EvolutionStableRolloutFinalizationView:
        if re.fullmatch(r"evstablerolloutauth_[0-9a-f]{24}", authorization_id) is None:
            raise ValueError("authorization_id 格式无效。")
        lock = self._locks.setdefault(authorization_id, asyncio.Lock())
        async with lock:
            existing = await self.store.get_by_authorization(authorization_id)
            if existing is not None:
                return await self.inspect(authorization_id=authorization_id)
            item = await self.authorization_service.store.get(authorization_id)
            if item is None:
                raise EvolutionStableRolloutFinalizationError(
                    "stable_rollout_finalization_authorization_missing",
                    "Stable Rollout Authorization 不存在。",
                )
            consumer = _consumer_id(item.installation_member_id)
            release_item = await asyncio.to_thread(
                self.release_slot_store.get_stable_member_finalization,
                item.authorization_id,
            )
            consumption = await self.authorization_service.store.consumption(item.authorization_id)
            if release_item is None:
                view = await self.authorization_service.inspect(
                    authorization_id=item.authorization_id
                )
                if consumption is None:
                    if not view.stable_rollout_authority:
                        raise EvolutionStableRolloutFinalizationError(
                            "stable_rollout_finalization_authorization_not_current",
                            "Stable Rollout Authorization 当前不可执行。",
                        )
                    consumption = await self.authorization_service.consume(
                        authorization_id=item.authorization_id,
                        nonce_base64=item.start_nonce_base64,
                        consumer_id=consumer,
                    )
                elif consumption.consumer_id != consumer:
                    raise EvolutionStableRolloutFinalizationError(
                        "stable_rollout_finalization_consumer_conflict",
                        "Stable Rollout Authorization 已被其他 consumer 消费。",
                    )
                consumed_view = await self.authorization_service.inspect(
                    authorization_id=item.authorization_id
                )
                if not (
                    consumed_view.consumed
                    and not consumed_view.expired
                    and _sources_current(consumed_view)
                ):
                    raise EvolutionStableRolloutFinalizationError(
                        "stable_rollout_finalization_sources_changed",
                        "Authorization source 在 finalization 前已变化。",
                    )
                authority = _release_authority(item)
                try:
                    release_item = await asyncio.to_thread(
                        self.release_slot_store.finalize_stable_member,
                        authority,
                        finalized_at=_aware(self.clock()).isoformat(),
                    )
                except ReleaseSlotError as exc:
                    raise EvolutionStableRolloutFinalizationError(
                        "stable_rollout_finalization_release_failed",
                        "Stable member release finalization 失败。",
                    ) from exc
            else:
                if consumption is None or consumption.consumer_id != consumer:
                    raise EvolutionStableRolloutFinalizationError(
                        "stable_rollout_finalization_consumption_missing",
                        "Release finalization 缺少 exact Authorization Consumption。",
                    )
            receipt = _build_receipt(
                workspace_root=self.workspace_root,
                authorization=item,
                consumption=consumption,
                release_finalization=release_item,
            )
            await self.store.record(receipt)
        return await self.inspect(authorization_id=authorization_id)

    async def inspect(self, *, authorization_id: str) -> EvolutionStableRolloutFinalizationView:
        receipt = await self.store.get_by_authorization(authorization_id)
        if receipt is None:
            raise EvolutionStableRolloutFinalizationError(
                "stable_rollout_finalization_missing",
                "尚未形成 Stable Rollout Finalization Receipt。",
            )
        authorization_valid = consumption_valid = release_valid = control_valid = False
        sources_current = active_current = False
        try:
            (
                authorization,
                consumption,
                release_item,
                active,
                view,
                control_valid,
            ) = await asyncio.gather(
                self.authorization_service.store.get(authorization_id),
                self.authorization_service.store.consumption(authorization_id),
                asyncio.to_thread(
                    self.release_slot_store.get_stable_member_finalization,
                    authorization_id,
                ),
                asyncio.to_thread(self.release_slot_store.active),
                self.authorization_service.inspect(authorization_id=authorization_id),
                self.store.control_history_valid(receipt),
            )
            authorization_valid = authorization == receipt.authorization
            consumption_valid = consumption == receipt.consumption
            release_valid = release_item == receipt.release_finalization
            sources_current = _sources_current(view)
            active_current = active == receipt.release_finalization.active_pointer
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        fact = authorization_valid and consumption_valid and release_valid and control_valid
        return EvolutionStableRolloutFinalizationView(
            receipt=receipt,
            authorization_source_valid=authorization_valid,
            consumption_source_valid=consumption_valid,
            release_finalization_valid=release_valid,
            control_history_valid=control_valid,
            authorization_sources_current=sources_current,
            active_pointer_current=active_current,
            stable_member_completion_fact=fact,
            active_stable_member_authority=bool(fact and sources_current and active_current),
        )


def render_stable_rollout_finalization(
    view: EvolutionStableRolloutFinalizationView,
) -> str:
    item = view.receipt
    status = "当前有效" if view.active_stable_member_authority else "历史完成"
    return "\n".join(
        (
            "## Stable Rollout Member Finalization",
            "",
            f"- 状态：**{status}**",
            f"- Completion：`{item.receipt_id}`",
            f"- Authorization：`{item.authorization.authorization_id}`",
            f"- Member：`{item.authorization.installation_member_id}`",
            f"- Release finalization：`{item.release_finalization.finalization_id}`",
            "- Expected pointer CAS：`true`",
            f"- Completion fact：`{str(view.stable_member_completion_fact).lower()}`",
            f"- Active member authority：`{str(view.active_stable_member_authority).lower()}`",
            "- Stable population rollout authority：`false`",
            "- Config/Data mutation executed：`false`",
            "- Promotion authority：`false`",
        )
    )


def _build_receipt(*, workspace_root, authorization, consumption, release_finalization):
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_ROLLOUT_FINALIZATION_POLICY,
        "workspace_root": str(workspace_root),
        "authorization": authorization.model_dump(mode="json"),
        "consumption": consumption.model_dump(mode="json"),
        "release_finalization": release_finalization.model_dump(mode="json"),
        "authorization_consumed": True,
        "expected_pointer_cas_satisfied": True,
        "binary_stable_member_finalized": True,
        "config_data_mutation_executed": False,
        "deployment_executed": False,
        "rollback_executed": False,
        "promotion_executed": False,
        "completed_at": release_finalization.finalized_at,
    }
    digest = _digest(core)
    return EvolutionStableRolloutFinalizationReceipt.model_validate(
        {
            **core,
            "receipt_id": f"evstablerolloutfinal_{digest[:24]}",
            "receipt_sha256": digest,
        }
    )


def _release_authority(
    item: EvolutionStableRolloutAuthorization,
) -> ReleaseStableMemberFinalizationAuthority:
    return ReleaseStableMemberFinalizationAuthority(
        authority_id=item.authorization_id,
        authority_sha256=item.authorization_sha256,
        completion_receipt_id=item.completion_receipt_id,
        completion_receipt_sha256=item.completion_receipt_sha256,
        installation_member_id=item.installation_member_id,
        expected_pointer_id=item.expected_active_pointer_id,
        expected_pointer_sha256=item.expected_active_pointer_sha256,
        expected_pointer_generation=item.expected_active_pointer_generation,
    )


def _consumer_id(member_id: str) -> str:
    return f"stable.finalizer:{member_id}"


def _sources_current(view: EvolutionStableRolloutAuthorizationView) -> bool:
    return bool(
        view.source_current
        and view.completion_current
        and view.readiness_current
        and view.control_current
    )


async def _require_control_active_at_finalization(db, item) -> None:
    authorization = item.authorization
    finalized_at = item.release_finalization.finalized_at
    rows = await (
        await db.execute(
            "SELECT event_json FROM evolution_revalidation_rollout_control_events "
            "WHERE workspace_root = ? AND changed_at <= ? ORDER BY sequence",
            (authorization.workspace_root, finalized_at),
        )
    ).fetchall()
    events = tuple(
        EvolutionRevalidationRolloutControlEvent.model_validate_json(row["event_json"])
        for row in rows
    )
    if authorization.control_sequence == 0:
        valid = not events
    else:
        latest = None if not events else events[-1]
        valid = bool(
            latest is not None
            and latest.sequence == authorization.control_sequence
            and latest.event_id == authorization.control_event_id
            and latest.event_sha256 == authorization.control_event_sha256
            and latest.state is EvolutionRevalidationRolloutControlState.ACTIVE
        )
    if not valid:
        raise EvolutionStableRolloutFinalizationError(
            "stable_rollout_finalization_control_changed",
            "Rollout kill switch 在 finalization 前已变化。",
        )


def _restore_receipt(encoded: str) -> EvolutionStableRolloutFinalizationReceipt:
    try:
        return EvolutionStableRolloutFinalizationReceipt.model_validate_json(encoded)
    except (TypeError, ValueError) as exc:
        raise EvolutionStableRolloutFinalizationError(
            "stable_rollout_finalization_corrupt",
            "Stable Rollout Finalization durable artifact 已损坏。",
        ) from exc


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_rollout_finalizations ("
        "receipt_id TEXT PRIMARY KEY, receipt_sha256 TEXT NOT NULL UNIQUE, "
        "authorization_id TEXT NOT NULL UNIQUE, completion_receipt_id TEXT NOT NULL, "
        "installation_member_id TEXT NOT NULL, receipt_json TEXT NOT NULL, "
        "completed_at TEXT NOT NULL)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_evolution_stable_rollout_finalization_member "
        "ON evolution_stable_rollout_finalizations("
        "completion_receipt_id, installation_member_id, completed_at)"
    )


def _aware(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间戳必须包含时区。")
    return parsed.astimezone(UTC)


def _digest(payload) -> str:
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
    "EVOLUTION_STABLE_ROLLOUT_FINALIZATION_POLICY",
    "EvolutionStableRolloutFinalizationError",
    "EvolutionStableRolloutFinalizationReceipt",
    "EvolutionStableRolloutFinalizationService",
    "EvolutionStableRolloutFinalizationStore",
    "EvolutionStableRolloutFinalizationView",
    "render_stable_rollout_finalization",
]
