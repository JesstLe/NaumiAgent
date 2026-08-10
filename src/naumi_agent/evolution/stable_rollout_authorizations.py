"""Short-lived single-use authorization for binary-only stable rollout finalization."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, Self, runtime_checkable

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.revalidation_rollout_stage_entries import (
    EvolutionRevalidationRolloutControlEvent,
    EvolutionRevalidationRolloutControlState,
    EvolutionRevalidationRolloutControlStore,
)
from naumi_agent.evolution.stable_population_completions import (
    EvolutionStablePopulationCompletionView,
)
from naumi_agent.evolution.stable_rollback_readiness import (
    EvolutionStableRollbackReadiness,
)

EVOLUTION_STABLE_ROLLOUT_AUTHORIZATION_POLICY = "evolution-stable-rollout-authorization-v1"
_AUTH_RE = re.compile(r"^evstablerolloutauth_[0-9a-f]{24}$")
_COMPLETION_RE = re.compile(r"^evstablepopcomplete_[0-9a-f]{24}$")
_INTENT_RE = re.compile(r"^evrestableintent_[0-9a-f]{24}$")
_CONSUMER_RE = re.compile(r"^[a-z][a-z0-9_.:-]{2,127}$")
_B64_32_RE = r"^[A-Za-z0-9+/]{43}=$"
_MAX_ARTIFACT_BYTES = 256 * 1024


@runtime_checkable
class EvolutionStablePopulationCompletionInspectionPort(Protocol):
    async def inspect(
        self,
        *,
        receipt_id: str,
    ) -> EvolutionStablePopulationCompletionView: ...


@runtime_checkable
class EvolutionStableRollbackReadinessInspectionPort(Protocol):
    async def inspect(
        self,
        *,
        completion_receipt_id: str,
        intent_id: str,
    ) -> EvolutionStableRollbackReadiness: ...


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionStableRolloutAuthorization(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-stable-rollout-authorization-v1"] = (
        EVOLUTION_STABLE_ROLLOUT_AUTHORIZATION_POLICY
    )
    authorization_id: str = Field(pattern=r"^evstablerolloutauth_[0-9a-f]{24}$")
    authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_root: str = Field(min_length=1, max_length=4096)
    completion_receipt_id: str = Field(pattern=r"^evstablepopcomplete_[0-9a-f]{24}$")
    completion_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completion_source_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    population_snapshot_id: str = Field(pattern=r"^relpopsnapshot_[0-9a-f]{24}$")
    population_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_member_id: str = Field(pattern=r"^relpopmember_[0-9a-f]{24}$")
    stable_intent_id: str = Field(pattern=r"^evrestableintent_[0-9a-f]{24}$")
    readiness_id: str = Field(pattern=r"^evstablerollbackready_[0-9a-f]{24}$")
    readiness_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_active_pointer_id: str = Field(pattern=r"^relactive_[0-9a-f]{24}$")
    expected_active_pointer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_active_pointer_generation: int = Field(ge=2, le=1_000_000_000)
    rollback_slot_id: str = Field(pattern=r"^relslot_[0-9a-f]{24}$")
    rollback_slot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollback_boot_receipt_id: str = Field(pattern=r"^relboot_[0-9a-f]{24}$")
    rollback_boot_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    control_sequence: int = Field(ge=0, le=1_000_000)
    control_event_id: str = Field(default="", pattern=r"^(?:|evrerolloutctrl_[0-9a-f]{24})$")
    control_event_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    control_state: Literal["active"] = "active"
    attempt: int = Field(ge=1, le=10_000)
    previous_authorization_id: str = Field(
        default="", pattern=r"^(?:|evstablerolloutauth_[0-9a-f]{24})$"
    )
    previous_authorization_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    operation_scope: Literal["binary_only"] = "binary_only"
    allowed_operations: tuple[Literal["finalize_stable_population_member"], ...] = (
        "finalize_stable_population_member",
    )
    config_data_mutation_allowed: Literal[False] = False
    start_nonce_base64: str = Field(pattern=_B64_32_RE)
    start_nonce_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validity_seconds: int = Field(ge=60, le=900)
    issued_at: str = Field(min_length=1, max_length=100)
    expires_at: str = Field(min_length=1, max_length=100)
    stable_rollout_authority: Literal[True] = True
    deployment_authority: Literal[False] = False
    rollback_authority: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Stable Rollout Authorization workspace 必须 canonical。")
        if (self.control_sequence == 0) is bool(self.control_event_id):
            raise ValueError("Stable Rollout Authorization control projection 不一致。")
        if bool(self.control_event_id) is not bool(self.control_event_sha256):
            raise ValueError("Stable Rollout Authorization control digest 不一致。")
        if (self.attempt == 1) is bool(self.previous_authorization_id):
            raise ValueError("Stable Rollout Authorization chain 不一致。")
        if bool(self.previous_authorization_id) is not bool(self.previous_authorization_sha256):
            raise ValueError("Stable Rollout Authorization previous digest 不一致。")
        nonce = base64.b64decode(self.start_nonce_base64, validate=True)
        if len(nonce) != 32 or hashlib.sha256(nonce).hexdigest() != self.start_nonce_sha256:
            raise ValueError("Stable Rollout Authorization nonce identity 不一致。")
        issued = _aware(self.issued_at)
        if _aware(self.expires_at) != issued + timedelta(seconds=self.validity_seconds):
            raise ValueError("Stable Rollout Authorization expiry 不一致。")
        source = _source_identity(self)
        if self.source_set_sha256 != _digest(source):
            raise ValueError("Stable Rollout Authorization source-set 不一致。")
        core = self.model_dump(mode="json", exclude={"authorization_id", "authorization_sha256"})
        digest = _digest(core)
        if self.authorization_sha256 != digest or self.authorization_id != (
            f"evstablerolloutauth_{digest[:24]}"
        ):
            raise ValueError("Stable Rollout Authorization identity 不一致。")
        return self


class EvolutionStableRolloutConsumptionReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    receipt_id: str = Field(pattern=r"^evstablerolloutconsume_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_id: str = Field(pattern=r"^evstablerolloutauth_[0-9a-f]{24}$")
    authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    consumer_id: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{2,127}$")
    start_nonce_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    consumed_at: str = Field(min_length=1, max_length=100)
    single_use_consumed: Literal[True] = True

    @model_validator(mode="after")
    def _exact(self) -> Self:
        _aware(self.consumed_at)
        core = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = _digest(core)
        if self.receipt_sha256 != digest or self.receipt_id != (
            f"evstablerolloutconsume_{digest[:24]}"
        ):
            raise ValueError("Stable Rollout Consumption identity 不一致。")
        return self


class EvolutionStableRolloutAuthorizationView(_StrictModel):
    authorization: EvolutionStableRolloutAuthorization
    source_current: bool
    completion_current: bool
    readiness_current: bool
    control_current: bool
    expired: bool
    consumed: bool
    invalidation_reasons: tuple[str, ...] = Field(max_length=16)
    stable_rollout_authority: bool
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _projection(self) -> Self:
        expected = bool(
            self.source_current
            and self.completion_current
            and self.readiness_current
            and self.control_current
            and not self.expired
            and not self.consumed
        )
        if self.stable_rollout_authority is not expected:
            raise ValueError("Stable Rollout Authorization View 投影不一致。")
        return self


class EvolutionStableRolloutAuthorizationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionStableRolloutAuthorizationStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    async def get(self, authorization_id: str):
        item_id = _authorization_id(authorization_id)
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT authorization_json FROM evolution_stable_rollout_authorizations "
                    "WHERE authorization_id = ?",
                    (item_id,),
                )
            ).fetchone()
        return None if row is None else _restore_authorization(row["authorization_json"])

    async def consumption(self, authorization_id: str):
        item_id = _authorization_id(authorization_id)
        if not self.db_path.is_file():
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_stable_rollout_consumptions "
                    "WHERE authorization_id = ?",
                    (item_id,),
                )
            ).fetchone()
        return None if row is None else _restore_consumption(row["receipt_json"])

    async def issue(
        self,
        *,
        completion_receipt_id: str,
        stable_intent_id: str,
        source_set_sha256: str,
        now: datetime,
        build: Callable[
            [EvolutionStableRolloutAuthorization | None], EvolutionStableRolloutAuthorization
        ],
    ) -> EvolutionStableRolloutAuthorization:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT authorization_json FROM evolution_stable_rollout_authorizations "
                    "WHERE completion_receipt_id = ? AND stable_intent_id = ? "
                    "ORDER BY attempt DESC LIMIT 1",
                    (completion_receipt_id, stable_intent_id),
                )
            ).fetchone()
            previous = None if row is None else _restore_authorization(row["authorization_json"])
            if previous is not None and previous.source_set_sha256 == source_set_sha256:
                used = await (
                    await db.execute(
                        "SELECT 1 FROM evolution_stable_rollout_consumptions "
                        "WHERE authorization_id = ?",
                        (previous.authorization_id,),
                    )
                ).fetchone()
                if used is None and _aware(previous.expires_at) > now:
                    await db.rollback()
                    return previous
            item = build(previous)
            await self._require_sources(db, item)
            encoded = item.model_dump_json()
            if len(encoded.encode()) > _MAX_ARTIFACT_BYTES:
                raise EvolutionStableRolloutAuthorizationError(
                    "stable_rollout_authorization_oversized",
                    "Stable Rollout Authorization 超过 256 KiB。",
                )
            await db.execute(
                "INSERT INTO evolution_stable_rollout_authorizations "
                "(authorization_id, authorization_sha256, completion_receipt_id, "
                "stable_intent_id, attempt, authorization_json, issued_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.authorization_id,
                    item.authorization_sha256,
                    item.completion_receipt_id,
                    item.stable_intent_id,
                    item.attempt,
                    encoded,
                    item.issued_at,
                    item.expires_at,
                ),
            )
            await db.commit()
        return item

    async def _require_sources(self, db, item) -> None:
        completion = await (
            await db.execute(
                "SELECT receipt_json FROM evolution_stable_population_completions "
                "WHERE receipt_id = ?",
                (item.completion_receipt_id,),
            )
        ).fetchone()
        if completion is None:
            raise EvolutionStableRolloutAuthorizationError(
                "stable_rollout_completion_missing", "Population Completion durable source 缺失。"
            )
        restored = json.loads(completion["receipt_json"])
        if restored.get("receipt_sha256") != item.completion_receipt_sha256:
            raise EvolutionStableRolloutAuthorizationError(
                "stable_rollout_completion_changed", "Population Completion durable source 已变化。"
            )
        control = await (
            await db.execute(
                "SELECT event_json FROM evolution_revalidation_rollout_control_events "
                "WHERE workspace_root = ? ORDER BY sequence DESC LIMIT 1",
                (item.workspace_root,),
            )
        ).fetchone()
        projection = (
            (0, "", "")
            if control is None
            else _control_identity(
                EvolutionRevalidationRolloutControlEvent.model_validate_json(control["event_json"])
            )
        )
        if projection != (item.control_sequence, item.control_event_id, item.control_event_sha256):
            raise EvolutionStableRolloutAuthorizationError(
                "stable_rollout_control_changed", "Rollout kill switch generation 已变化。"
            )

    async def consume(
        self, *, authorization_id: str, nonce_base64: str, consumer_id: str, consumed_at: datetime
    ) -> EvolutionStableRolloutConsumptionReceipt:
        item_id = _authorization_id(authorization_id)
        consumer = _consumer_id(consumer_id)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await _ensure_schema(db)
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT authorization_json FROM evolution_stable_rollout_authorizations "
                    "WHERE authorization_id = ?",
                    (item_id,),
                )
            ).fetchone()
            if row is None:
                raise EvolutionStableRolloutAuthorizationError(
                    "stable_rollout_authorization_missing", "Stable Rollout Authorization 不存在。"
                )
            item = _restore_authorization(row["authorization_json"])
            existing = await (
                await db.execute(
                    "SELECT receipt_json FROM evolution_stable_rollout_consumptions "
                    "WHERE authorization_id = ?",
                    (item_id,),
                )
            ).fetchone()
            if existing is not None:
                receipt = _restore_consumption(existing["receipt_json"])
                if receipt.consumer_id == consumer and hmac.compare_digest(
                    item.start_nonce_base64, str(nonce_base64)
                ):
                    await db.rollback()
                    return receipt
                raise EvolutionStableRolloutAuthorizationError(
                    "stable_rollout_authorization_consumed",
                    "Stable Rollout Authorization 已被消费。",
                )
            if consumed_at >= _aware(item.expires_at):
                raise EvolutionStableRolloutAuthorizationError(
                    "stable_rollout_authorization_expired", "Stable Rollout Authorization 已过期。"
                )
            if not hmac.compare_digest(item.start_nonce_base64, str(nonce_base64)):
                raise EvolutionStableRolloutAuthorizationError(
                    "stable_rollout_nonce_mismatch", "Stable Rollout Authorization nonce 不匹配。"
                )
            receipt = _build_consumption(item, consumer, consumed_at)
            await db.execute(
                "INSERT INTO evolution_stable_rollout_consumptions "
                "(receipt_id, authorization_id, receipt_json, consumed_at) VALUES (?, ?, ?, ?)",
                (receipt.receipt_id, item_id, receipt.model_dump_json(), receipt.consumed_at),
            )
            await db.commit()
        return receipt


class EvolutionStableRolloutAuthorizationService:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        completion_inspector: EvolutionStablePopulationCompletionInspectionPort,
        readiness_inspector: EvolutionStableRollbackReadinessInspectionPort,
        control_store: EvolutionRevalidationRolloutControlStore,
        store: EvolutionStableRolloutAuthorizationStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
        self.completion_inspector = completion_inspector
        self.readiness_inspector = readiness_inspector
        self.control_store = control_store
        self.store = store
        self.clock = clock
        self.random_bytes = random_bytes
        self._lock = asyncio.Lock()

    async def issue(
        self, *, completion_receipt_id: str, intent_id: str, validity_seconds: int = 300
    ) -> EvolutionStableRolloutAuthorizationView:
        completion_id = _completion_id(completion_receipt_id)
        stable_intent_id = _intent_id(intent_id)
        if not 60 <= int(validity_seconds) <= 900:
            raise ValueError("validity_seconds 必须在 60 到 900 之间。")
        async with self._lock:
            completion_view, readiness, control = await asyncio.gather(
                self.completion_inspector.inspect(receipt_id=completion_id),
                self.readiness_inspector.inspect(
                    completion_receipt_id=completion_id, intent_id=stable_intent_id
                ),
                self.control_store.latest(self.workspace_root),
            )
            completion = completion_view.receipt
            if not completion_view.stable_population_completion_authority:
                raise EvolutionStableRolloutAuthorizationError(
                    "stable_rollout_completion_not_current",
                    "Population Completion 当前无 authority。",
                )
            if not _readiness_matches(completion, readiness, stable_intent_id):
                raise EvolutionStableRolloutAuthorizationError(
                    "stable_rollout_readiness_mismatch", "Rollback Readiness 与 Completion 不一致。"
                )
            if (
                control is not None
                and control.state is not EvolutionRevalidationRolloutControlState.ACTIVE
            ):
                raise EvolutionStableRolloutAuthorizationError(
                    "stable_rollout_control_paused", "Rollout kill switch 当前已暂停。"
                )
            control_identity = _control_identity(control)
            source_sha = _digest(_source_values(completion, readiness, control_identity))
            now = _aware(self.clock())

            def build(previous):
                return _build_authorization(
                    workspace_root=self.workspace_root,
                    completion=completion,
                    readiness=readiness,
                    control_identity=control_identity,
                    previous=previous,
                    validity_seconds=int(validity_seconds),
                    issued_at=now,
                    nonce=self.random_bytes(32),
                )

            item = await self.store.issue(
                completion_receipt_id=completion_id,
                stable_intent_id=stable_intent_id,
                source_set_sha256=source_sha,
                now=now,
                build=build,
            )
        return await self.inspect(authorization_id=item.authorization_id)

    async def inspect(self, *, authorization_id: str) -> EvolutionStableRolloutAuthorizationView:
        item = await self.store.get(authorization_id)
        if item is None:
            raise EvolutionStableRolloutAuthorizationError(
                "stable_rollout_authorization_missing", "Stable Rollout Authorization 不存在。"
            )
        reasons: list[str] = []
        source_current = True
        try:
            restored = await self.store.get(item.authorization_id)
            source_current = restored == item
        except (OSError, RuntimeError, TypeError, ValueError):
            source_current = False
        if not source_current:
            reasons.append("authorization_source_changed")
        try:
            completion_view, readiness, control, consumption = await asyncio.gather(
                self.completion_inspector.inspect(receipt_id=item.completion_receipt_id),
                self.readiness_inspector.inspect(
                    completion_receipt_id=item.completion_receipt_id,
                    intent_id=item.stable_intent_id,
                ),
                self.control_store.latest(self.workspace_root),
                self.store.consumption(item.authorization_id),
            )
            completion_current = bool(
                completion_view.stable_population_completion_authority
                and completion_view.receipt.receipt_sha256 == item.completion_receipt_sha256
            )
            readiness_current = bool(
                readiness.readiness_id == item.readiness_id
                and readiness.readiness_sha256 == item.readiness_sha256
                and _readiness_matches(completion_view.receipt, readiness, item.stable_intent_id)
            )
            control_current = _control_identity(control) == (
                item.control_sequence,
                item.control_event_id,
                item.control_event_sha256,
            ) and (
                control is None or control.state is EvolutionRevalidationRolloutControlState.ACTIVE
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            completion_current = readiness_current = control_current = False
            consumption = None
        if not completion_current:
            reasons.append("completion_changed")
        if not readiness_current:
            reasons.append("readiness_changed")
        if not control_current:
            reasons.append("rollout_control_changed")
        expired = _aware(self.clock()) >= _aware(item.expires_at)
        consumed = consumption is not None
        if expired:
            reasons.append("authorization_expired")
        if consumed:
            reasons.append("authorization_consumed")
        return EvolutionStableRolloutAuthorizationView(
            authorization=item,
            source_current=source_current,
            completion_current=completion_current,
            readiness_current=readiness_current,
            control_current=control_current,
            expired=expired,
            consumed=consumed,
            invalidation_reasons=tuple(sorted(set(reasons))),
            stable_rollout_authority=bool(
                source_current
                and completion_current
                and readiness_current
                and control_current
                and not expired
                and not consumed
            ),
        )

    async def consume(self, *, authorization_id: str, nonce_base64: str, consumer_id: str):
        view = await self.inspect(authorization_id=authorization_id)
        if not view.stable_rollout_authority and not view.consumed:
            raise EvolutionStableRolloutAuthorizationError(
                "stable_rollout_authorization_not_current",
                "Stable Rollout Authorization 当前不可消费。",
            )
        return await self.store.consume(
            authorization_id=authorization_id,
            nonce_base64=nonce_base64,
            consumer_id=consumer_id,
            consumed_at=_aware(self.clock()),
        )


def render_stable_rollout_authorization(view: EvolutionStableRolloutAuthorizationView) -> str:
    item = view.authorization
    status = "可执行" if view.stable_rollout_authority else "已撤权"
    return "\n".join(
        (
            "## Stable Rollout Authorization",
            "",
            f"- 状态：**{status}**",
            f"- Authorization：`{item.authorization_id}`",
            f"- Member：`{item.installation_member_id}`",
            f"- Intent：`{item.stable_intent_id}`",
            f"- Scope：`{item.operation_scope}`",
            f"- Kill switch generation：{item.control_sequence}",
            f"- Expires：`{item.expires_at}`",
            f"- Consumed：`{str(view.consumed).lower()}`",
            f"- Stable rollout authority：`{str(view.stable_rollout_authority).lower()}`",
            "- Config/Data mutation authority：`false`",
            "- Promotion authority：`false`",
        )
    )


def _build_authorization(
    *,
    workspace_root,
    completion,
    readiness,
    control_identity,
    previous,
    validity_seconds,
    issued_at,
    nonce,
):
    if len(nonce) != 32:
        raise ValueError("Stable Rollout Authorization nonce 必须是 32 bytes。")
    sequence, control_id, control_sha = control_identity
    source = _source_values(completion, readiness, control_identity)
    core = {
        "schema_version": 1,
        "policy_version": EVOLUTION_STABLE_ROLLOUT_AUTHORIZATION_POLICY,
        "source_set_sha256": _digest(source),
        "workspace_root": str(workspace_root),
        "completion_receipt_id": completion.receipt_id,
        "completion_receipt_sha256": completion.receipt_sha256,
        "completion_source_set_sha256": completion.source_set_sha256,
        "population_snapshot_id": completion.population_snapshot_id,
        "population_snapshot_sha256": completion.population_snapshot_sha256,
        "installation_member_id": readiness.installation_member_id,
        "stable_intent_id": readiness.stable_intent_id,
        "readiness_id": readiness.readiness_id,
        "readiness_sha256": readiness.readiness_sha256,
        "expected_active_pointer_id": readiness.expected_active_pointer_id,
        "expected_active_pointer_sha256": readiness.expected_active_pointer_sha256,
        "expected_active_pointer_generation": readiness.expected_active_pointer_generation,
        "rollback_slot_id": readiness.rollback_slot_id,
        "rollback_slot_sha256": readiness.rollback_slot_sha256,
        "rollback_boot_receipt_id": readiness.rollback_boot_receipt_id,
        "rollback_boot_receipt_sha256": readiness.rollback_boot_receipt_sha256,
        "control_sequence": sequence,
        "control_event_id": control_id,
        "control_event_sha256": control_sha,
        "control_state": "active",
        "attempt": 1 if previous is None else previous.attempt + 1,
        "previous_authorization_id": "" if previous is None else previous.authorization_id,
        "previous_authorization_sha256": "" if previous is None else previous.authorization_sha256,
        "operation_scope": "binary_only",
        "allowed_operations": ["finalize_stable_population_member"],
        "config_data_mutation_allowed": False,
        "start_nonce_base64": base64.b64encode(nonce).decode("ascii"),
        "start_nonce_sha256": hashlib.sha256(nonce).hexdigest(),
        "validity_seconds": validity_seconds,
        "issued_at": issued_at.isoformat(),
        "expires_at": (issued_at + timedelta(seconds=validity_seconds)).isoformat(),
        "stable_rollout_authority": True,
        "deployment_authority": False,
        "rollback_authority": False,
        "promotion_authority": False,
    }
    digest = _digest(core)
    return EvolutionStableRolloutAuthorization.model_validate(
        {
            **core,
            "authorization_id": f"evstablerolloutauth_{digest[:24]}",
            "authorization_sha256": digest,
        }
    )


def _build_consumption(item, consumer, consumed_at):
    core = {
        "schema_version": 1,
        "authorization_id": item.authorization_id,
        "authorization_sha256": item.authorization_sha256,
        "consumer_id": consumer,
        "start_nonce_sha256": item.start_nonce_sha256,
        "consumed_at": consumed_at.isoformat(),
        "single_use_consumed": True,
    }
    digest = _digest(core)
    return EvolutionStableRolloutConsumptionReceipt.model_validate(
        {
            **core,
            "receipt_id": f"evstablerolloutconsume_{digest[:24]}",
            "receipt_sha256": digest,
        }
    )


def _source_values(completion, readiness, control_identity):
    return {
        "completion_receipt_id": completion.receipt_id,
        "completion_receipt_sha256": completion.receipt_sha256,
        "completion_source_set_sha256": completion.source_set_sha256,
        "stable_intent_id": readiness.stable_intent_id,
        "readiness_id": readiness.readiness_id,
        "readiness_sha256": readiness.readiness_sha256,
        "control_sequence": control_identity[0],
        "control_event_id": control_identity[1],
        "control_event_sha256": control_identity[2],
    }


def _source_identity(item):
    return {
        "completion_receipt_id": item.completion_receipt_id,
        "completion_receipt_sha256": item.completion_receipt_sha256,
        "completion_source_set_sha256": item.completion_source_set_sha256,
        "stable_intent_id": item.stable_intent_id,
        "readiness_id": item.readiness_id,
        "readiness_sha256": item.readiness_sha256,
        "control_sequence": item.control_sequence,
        "control_event_id": item.control_event_id,
        "control_event_sha256": item.control_event_sha256,
    }


def _readiness_matches(completion, readiness, intent_id):
    try:
        index = completion.intent_ids.index(intent_id)
    except ValueError:
        return False
    return bool(
        readiness.binary_rollback_readiness_authority
        and not readiness.config_data_rollback_readiness_authority
        and readiness.completion_receipt_id == completion.receipt_id
        and readiness.completion_receipt_sha256 == completion.receipt_sha256
        and readiness.stable_intent_id == intent_id
        and readiness.installation_member_id == completion.installation_member_ids[index]
    )


def _control_identity(control):
    if control is None:
        return 0, "", ""
    return control.sequence, control.event_id, control.event_sha256


async def _ensure_schema(db):
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_rollout_authorizations ("
        "authorization_id TEXT PRIMARY KEY, authorization_sha256 TEXT NOT NULL, "
        "completion_receipt_id TEXT NOT NULL, stable_intent_id TEXT NOT NULL, "
        "attempt INTEGER NOT NULL, authorization_json TEXT NOT NULL, issued_at TEXT NOT NULL, "
        "expires_at TEXT NOT NULL, UNIQUE(completion_receipt_id, stable_intent_id, attempt))"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS evolution_stable_rollout_consumptions ("
        "receipt_id TEXT PRIMARY KEY, authorization_id TEXT NOT NULL UNIQUE, "
        "receipt_json TEXT NOT NULL, consumed_at TEXT NOT NULL)"
    )


def _restore_authorization(value):
    return EvolutionStableRolloutAuthorization.model_validate_json(value)


def _restore_consumption(value):
    return EvolutionStableRolloutConsumptionReceipt.model_validate_json(value)


def _authorization_id(value):
    normalized = str(value).strip()
    if not _AUTH_RE.fullmatch(normalized):
        raise ValueError("authorization_id 格式无效。")
    return normalized


def _completion_id(value):
    normalized = str(value).strip()
    if not _COMPLETION_RE.fullmatch(normalized):
        raise ValueError("completion_receipt_id 格式无效。")
    return normalized


def _intent_id(value):
    normalized = str(value).strip()
    if not _INTENT_RE.fullmatch(normalized):
        raise ValueError("intent_id 格式无效。")
    return normalized


def _consumer_id(value):
    normalized = str(value).strip()
    if not _CONSUMER_RE.fullmatch(normalized):
        raise ValueError("consumer_id 格式无效。")
    return normalized


def _aware(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp 必须包含 offset。")
    return parsed.astimezone(UTC)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


__all__ = [
    "EVOLUTION_STABLE_ROLLOUT_AUTHORIZATION_POLICY",
    "EvolutionStableRolloutAuthorization",
    "EvolutionStableRolloutAuthorizationError",
    "EvolutionStableRolloutAuthorizationService",
    "EvolutionStableRolloutAuthorizationStore",
    "EvolutionStableRolloutAuthorizationView",
    "EvolutionStableRolloutConsumptionReceipt",
    "render_stable_rollout_authorization",
]
