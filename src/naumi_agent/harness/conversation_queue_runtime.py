"""Runtime authority for durable queued conversations and fenced dispatch."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from naumi_agent.harness.run_lease import HarnessRunLease, HarnessRunLeaseState
from naumi_agent.harness.store import (
    HarnessConversationQueueItem,
    HarnessConversationQueueResolution,
    HarnessStore,
    HarnessStoreConflictError,
)

_QUEUE_CLAIM_LEASE_SECONDS = 30


class ConversationQueueClaimError(RuntimeError):
    """Raised when a queue item cannot safely cross the dispatch boundary."""


@dataclass(frozen=True, slots=True)
class ConversationQueueClaim:
    item: HarnessConversationQueueItem
    lease: HarnessRunLease


@dataclass(frozen=True, slots=True)
class ConversationQueueRecovery:
    ready: tuple[HarnessConversationQueueItem, ...]
    blocked: tuple[HarnessConversationQueueItem, ...]
    blocker_code: str


@dataclass(frozen=True, slots=True)
class ConversationQueueReview:
    item: HarnessConversationQueueItem
    lease: HarnessRunLease
    resolvable: bool
    resolution_code: str


class DurableConversationQueueAuthority:
    """Bind one workspace/session queue to the shared Harness lease authority."""

    def __init__(
        self,
        *,
        store: HarnessStore,
        workspace_root: str | Path,
        session_id: str,
        owner_id: str,
        lease_seconds: int = _QUEUE_CLAIM_LEASE_SECONDS,
    ) -> None:
        self.store = store
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.session_id = _bounded_text(session_id, field="session_id", maximum=256)
        self.owner_id = _bounded_identity(owner_id, field="owner_id")
        if not 3 <= lease_seconds <= 86_400:
            raise ValueError("queue lease_seconds 必须在 3 到 86400 之间。")
        self.lease_seconds = lease_seconds

    async def enqueue(
        self,
        *,
        request_id: str,
        text: str,
        client_id: str,
        now: str | None = None,
    ) -> HarnessConversationQueueItem:
        return await self.store.enqueue_conversation(
            workspace_root=self.workspace_root,
            session_id=self.session_id,
            request_id=request_id,
            client_id=client_id,
            text=text,
            enqueued_at=now or _utc_now(),
        )

    async def promote(
        self,
        *,
        request_id: str,
        now: str | None = None,
    ) -> HarnessConversationQueueItem:
        recovery = await self.recover()
        if recovery.blocked:
            raise ConversationQueueClaimError(
                "队列存在待核对的历史 claim，完成恢复处理前不能重排。"
            )
        return await self.store.promote_queued_conversation(
            workspace_root=self.workspace_root,
            session_id=self.session_id,
            request_id=request_id,
            updated_at=now or _utc_now(),
        )

    async def recover(self, *, limit: int = 20) -> ConversationQueueRecovery:
        """Return safe unclaimed prefix and fail-closed ambiguous suffix."""
        items = await self.store.list_queued_conversations(
            workspace_root=self.workspace_root,
            session_id=self.session_id,
            limit=limit,
        )
        ready: list[HarnessConversationQueueItem] = []
        blocked: list[HarnessConversationQueueItem] = []
        blocker_code = ""
        for index, item in enumerate(items):
            lease = await self.store.get_run_lease(
                workspace_root=self.workspace_root,
                run_kind="runtime",
                run_id=self.claim_run_id(item.request_id),
            )
            if lease is None and not blocked:
                ready.append(item)
                continue
            blocked.extend(items[index:])
            blocker_code = (
                "queue_claim_released"
                if lease is not None and lease.state is HarnessRunLeaseState.RELEASED
                else "queue_claim_ambiguous"
            )
            break
        return ConversationQueueRecovery(
            ready=tuple(ready),
            blocked=tuple(blocked),
            blocker_code=blocker_code,
        )

    async def review(
        self,
        *,
        request_id: str,
        now: str | None = None,
    ) -> ConversationQueueReview:
        """Read the exact claim facts a user must inspect before resolution."""
        request = _bounded_identity(request_id, field="request_id")
        items = await self.store.list_queued_conversations(
            workspace_root=self.workspace_root,
            session_id=self.session_id,
            limit=100,
        )
        item = next((candidate for candidate in items if candidate.request_id == request), None)
        if item is None:
            raise ConversationQueueClaimError("未找到仍在等待处置的排队消息。")
        lease = await self.store.get_run_lease(
            workspace_root=self.workspace_root,
            run_kind="runtime",
            run_id=self.claim_run_id(request),
        )
        if lease is None:
            raise ConversationQueueClaimError("该消息从未派发，无需执行恢复处置。")
        timestamp = datetime.fromisoformat(now or _utc_now())
        if lease.state is HarnessRunLeaseState.RELEASED:
            return ConversationQueueReview(item, lease, True, "claim_released")
        if datetime.fromisoformat(lease.expires_at) <= timestamp:
            return ConversationQueueReview(item, lease, True, "claim_expired")
        return ConversationQueueReview(item, lease, False, "claim_live")

    async def resolve(
        self,
        *,
        request_id: str,
        action: str,
        reason: str,
        now: str | None = None,
    ) -> HarnessConversationQueueResolution:
        """Apply one explicit user decision to an ambiguous historical claim."""
        timestamp = now or _utc_now()
        review = await self.review(request_id=request_id, now=timestamp)
        if not review.resolvable:
            raise ConversationQueueClaimError(
                "该消息仍由活跃实例持有，不能重试或放弃。"
            )
        normalized_action = action.strip().lower() if isinstance(action, str) else ""
        if normalized_action not in {"retry", "cancel"}:
            raise ValueError("queue resolution action 必须是 retry 或 cancel。")
        retry_request_id = ""
        if normalized_action == "retry":
            payload = "\x00".join((
                self.session_id,
                review.item.request_id,
                review.lease.run_id,
                str(review.lease.epoch),
            ))
            retry_request_id = f"retry-{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"
        try:
            return await self.store.resolve_ambiguous_queued_conversation(
                workspace_root=self.workspace_root,
                session_id=self.session_id,
                request_id=review.item.request_id,
                action=normalized_action,
                retry_request_id=retry_request_id,
                reviewed_run_id=review.lease.run_id,
                reviewed_owner_id=review.lease.owner_id,
                reviewed_epoch=review.lease.epoch,
                actor_id=self.owner_id,
                reason=reason,
                resolved_at=timestamp,
            )
        except HarnessStoreConflictError as exc:
            raise ConversationQueueClaimError(str(exc)) from exc

    async def claim(
        self,
        item: HarnessConversationQueueItem,
        *,
        now: str | None = None,
    ) -> ConversationQueueClaim:
        self._validate_item_scope(item)
        timestamp = now or _utc_now()
        run_id = self.claim_run_id(item.request_id)
        existing = await self.store.get_run_lease(
            workspace_root=self.workspace_root,
            run_kind="runtime",
            run_id=run_id,
        )
        if existing is not None:
            raise ConversationQueueClaimError(
                "排队消息存在历史 claim，必须先核对上次派发结果。"
            )
        lease = await self.store.acquire_run_lease(
            workspace_root=self.workspace_root,
            run_kind="runtime",
            run_id=run_id,
            owner_id=self.owner_id,
            now=timestamp,
            lease_seconds=self.lease_seconds,
        )
        if lease is None:
            raise ConversationQueueClaimError("排队消息已被其他运行实例领取。")
        return ConversationQueueClaim(item=item, lease=lease)

    async def renew(
        self,
        claim: ConversationQueueClaim,
        *,
        now: str | None = None,
    ) -> ConversationQueueClaim:
        self._validate_claim(claim)
        lease = await self.store.renew_run_lease(
            workspace_root=self.workspace_root,
            run_kind="runtime",
            run_id=claim.lease.run_id,
            owner_id=self.owner_id,
            epoch=claim.lease.epoch,
            now=now or _utc_now(),
            lease_seconds=self.lease_seconds,
        )
        if lease is None:
            raise ConversationQueueClaimError("排队消息 claim 续租失败，停止提交运行结果。")
        return ConversationQueueClaim(item=claim.item, lease=lease)

    async def finish(
        self,
        claim: ConversationQueueClaim,
        *,
        state: str,
        terminal_reason: str,
        now: str | None = None,
    ) -> HarnessConversationQueueItem:
        self._validate_claim(claim)
        try:
            return await self.store.finish_queued_conversation(
                workspace_root=self.workspace_root,
                session_id=self.session_id,
                request_id=claim.item.request_id,
                state=state,
                terminal_reason=terminal_reason,
                updated_at=now or _utc_now(),
                claim_run_id=claim.lease.run_id,
                claim_owner_id=self.owner_id,
                claim_epoch=claim.lease.epoch,
            )
        except HarnessStoreConflictError as exc:
            raise ConversationQueueClaimError(str(exc)) from exc

    async def cancel_unclaimed(
        self,
        item: HarnessConversationQueueItem,
        *,
        reason: str,
        now: str | None = None,
    ) -> HarnessConversationQueueItem:
        self._validate_item_scope(item)
        lease = await self.store.get_run_lease(
            workspace_root=self.workspace_root,
            run_kind="runtime",
            run_id=self.claim_run_id(item.request_id),
        )
        if lease is not None:
            raise ConversationQueueClaimError(
                "排队消息已经跨过派发边界，不能按未执行消息取消。"
            )
        return await self.store.finish_queued_conversation(
            workspace_root=self.workspace_root,
            session_id=self.session_id,
            request_id=item.request_id,
            state="cancelled",
            terminal_reason=reason,
            updated_at=now or _utc_now(),
        )

    def claim_run_id(self, request_id: str) -> str:
        request = _bounded_identity(request_id, field="request_id")
        payload = "\x00".join((str(self.workspace_root), self.session_id, request))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"conversation-queue-{digest}"

    def _validate_item_scope(self, item: HarnessConversationQueueItem) -> None:
        if (
            item.workspace_root != str(self.workspace_root)
            or item.session_id != self.session_id
            or item.state != "queued"
        ):
            raise ConversationQueueClaimError("排队消息不属于当前 workspace/session。")

    def _validate_claim(self, claim: ConversationQueueClaim) -> None:
        self._validate_item_scope(claim.item)
        if (
            claim.lease.workspace_root != str(self.workspace_root)
            or claim.lease.run_id != self.claim_run_id(claim.item.request_id)
            or claim.lease.owner_id != self.owner_id
            or claim.lease.state is not HarnessRunLeaseState.ACTIVE
        ):
            raise ConversationQueueClaimError("排队消息 claim 与当前 authority 不匹配。")


def _bounded_text(value: str, *, field: str, maximum: int) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise ValueError(f"{field} 必须是 1..{maximum} 字符的有效文本。")
    return normalized


def _bounded_identity(value: str, *, field: str) -> str:
    normalized = _bounded_text(value, field=field, maximum=128)
    if not all(char.isalnum() or char in "._:-" for char in normalized):
        raise ValueError(f"{field} 包含不允许的字符。")
    return normalized


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "ConversationQueueClaim",
    "ConversationQueueClaimError",
    "ConversationQueueRecovery",
    "ConversationQueueReview",
    "DurableConversationQueueAuthority",
]
