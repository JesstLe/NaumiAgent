"""Shared durable interaction authority adapter for UI hosts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from naumi_agent.harness.interaction import (
    HarnessInteractionRecord,
    InteractionSubjectKind,
    new_interaction_record,
)
from naumi_agent.harness.store import HarnessStoreConflictError
from naumi_agent.user_interaction import (
    UserInteractionRequest,
    normalize_interaction_response,
)


class InteractionAuthorityStore(Protocol):
    async def get_interaction(
        self,
        **kwargs: Any,
    ) -> HarnessInteractionRecord | None: ...

    async def create_interaction(self, **kwargs: Any) -> HarnessInteractionRecord: ...

    async def answer_interaction(self, **kwargs: Any) -> HarnessInteractionRecord: ...

    async def expire_interaction(self, **kwargs: Any) -> HarnessInteractionRecord: ...

    async def takeover_interaction(self, **kwargs: Any) -> HarnessInteractionRecord: ...

    async def list_pending_interactions(
        self,
        **kwargs: Any,
    ) -> tuple[HarnessInteractionRecord, ...]: ...


@dataclass(frozen=True, slots=True)
class InteractionRecoveryBatch:
    claimed: tuple[HarnessInteractionRecord, ...]
    expired_ids: tuple[str, ...]
    retry_after_seconds: float | None


class DurableInteractionAuthorityClient:
    """Apply one authority protocol identically for Bridge and TUI hosts."""

    def __init__(
        self,
        *,
        store: InteractionAuthorityStore,
        workspace_root: str | Path,
        owner_id: str,
        owner_lease_seconds: int = 30,
    ) -> None:
        if not 3 <= owner_lease_seconds <= 86_400:
            raise ValueError("interaction owner lease 必须在 3..86400 秒之间。")
        self.store = store
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.owner_id = owner_id
        self.owner_lease_seconds = owner_lease_seconds
        self.owner_renew_interval_seconds = max(1.0, owner_lease_seconds / 3)

    async def create(
        self,
        *,
        request: UserInteractionRequest,
        interaction_id: str,
        subject_kind: str,
        subject_id: str,
        session_id: str,
        agent_name: str,
        now: str | None = None,
    ) -> HarnessInteractionRecord:
        timestamp = now or datetime.now(UTC).isoformat()
        record = new_interaction_record(
            request=request,
            subject_kind=cast(InteractionSubjectKind, subject_kind),
            subject_id=subject_id,
            session_id=session_id,
            agent_name=agent_name,
            owner_id=self.owner_id,
            created_at=timestamp,
            owner_lease_seconds=self.owner_lease_seconds,
            timeout_seconds=request.timeout_seconds,
            interaction_id=interaction_id,
        )
        return await self.store.create_interaction(
            workspace_root=self.workspace_root,
            record=record,
        )

    async def answer(
        self,
        *,
        record: HarnessInteractionRecord,
        response: dict[str, Any],
        answered_by: str = "user",
        now: str | None = None,
    ) -> tuple[HarnessInteractionRecord, dict[str, str]]:
        normalized = normalize_interaction_response(record.request(), response)
        if record.state == "pending":
            record = await self.store.answer_interaction(
                workspace_root=self.workspace_root,
                interaction_id=record.interaction_id,
                expected_sequence=record.sequence,
                owner_id=record.owner_id,
                owner_epoch=record.owner_epoch,
                response=normalized,
                answered_by=answered_by,
                now=now or datetime.now(UTC).isoformat(),
            )
        if record.state != "answered":
            raise ValueError("interaction 已不是可回答状态。")
        return record, {
            "kind": record.answer_kind,
            "value": record.answer_value,
            "label": record.answer_label,
            "custom_text": record.custom_text,
        }

    async def expire(
        self,
        *,
        record: HarnessInteractionRecord,
        now: str | None = None,
    ) -> HarnessInteractionRecord:
        return await self.store.expire_interaction(
            workspace_root=self.workspace_root,
            interaction_id=record.interaction_id,
            expected_sequence=record.sequence,
            now=now or datetime.now(UTC).isoformat(),
        )

    async def renew(
        self,
        *,
        record: HarnessInteractionRecord,
        now: str | None = None,
    ) -> HarnessInteractionRecord:
        if record.state != "pending":
            raise ValueError("只有 pending interaction 可以续租。")
        if record.owner_id != self.owner_id:
            raise ValueError("不能续租其他 interaction owner。")
        return await self.store.takeover_interaction(
            workspace_root=self.workspace_root,
            interaction_id=record.interaction_id,
            expected_sequence=record.sequence,
            owner_id=self.owner_id,
            now=now or datetime.now(UTC).isoformat(),
            owner_lease_seconds=self.owner_lease_seconds,
        )

    async def recover_pending(
        self,
        *,
        now: str | None = None,
        limit: int = 50,
    ) -> InteractionRecoveryBatch:
        timestamp = datetime.fromisoformat(now) if now else datetime.now(UTC)
        if timestamp.utcoffset() is None:
            raise ValueError("interaction recovery now 必须包含时区。")
        records = await self.store.list_pending_interactions(
            workspace_root=self.workspace_root,
            limit=limit,
        )
        claimed: list[HarnessInteractionRecord] = []
        expired_ids: list[str] = []
        retry_after: float | None = None
        for record in records:
            try:
                if (
                    record.expires_at
                    and datetime.fromisoformat(record.expires_at) <= timestamp
                ):
                    await self.expire(record=record, now=timestamp.isoformat())
                    expired_ids.append(record.interaction_id)
                    continue
                owner_expiry = datetime.fromisoformat(
                    record.owner_lease_expires_at
                )
                if record.owner_id != self.owner_id and owner_expiry > timestamp:
                    remaining = (owner_expiry - timestamp).total_seconds()
                    retry_after = min(retry_after or remaining, remaining)
                    continue
                if owner_expiry <= timestamp:
                    record = await self.store.takeover_interaction(
                        workspace_root=self.workspace_root,
                        interaction_id=record.interaction_id,
                        expected_sequence=record.sequence,
                        owner_id=self.owner_id,
                        now=timestamp.isoformat(),
                        owner_lease_seconds=self.owner_lease_seconds,
                    )
                claimed.append(record)
            except (HarnessStoreConflictError, ValueError):
                retry_after = min(retry_after or 0.5, 0.5)
        return InteractionRecoveryBatch(
            claimed=tuple(claimed),
            expired_ids=tuple(expired_ids),
            retry_after_seconds=retry_after,
        )

    @staticmethod
    def remaining_timeout_seconds(
        record: HarnessInteractionRecord,
        *,
        now: datetime | None = None,
    ) -> float | None:
        if not record.expires_at:
            return None
        current = now or datetime.now(UTC)
        return max(
            0.0,
            (datetime.fromisoformat(record.expires_at) - current).total_seconds(),
        )


__all__ = [
    "DurableInteractionAuthorityClient",
    "InteractionAuthorityStore",
    "InteractionRecoveryBatch",
]
