"""Bounded public session-list projection for terminal frontends."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _text(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _timestamp(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return _text(value, 64)


@dataclass(frozen=True, slots=True)
class SessionListItem:
    session_id: str
    title: str
    model: str
    updated_at: str
    message_count: int
    user_message_count: int
    git_branch: str
    is_current: bool
    resumable: bool

    def to_protocol_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "model": self.model,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
            "user_message_count": self.user_message_count,
            "git_branch": self.git_branch,
            "is_current": self.is_current,
            "resumable": self.resumable,
        }


@dataclass(frozen=True, slots=True)
class SessionListSnapshot:
    generated_at: str
    page: int
    page_size: int
    total: int
    query: str
    items: tuple[SessionListItem, ...]
    warnings: tuple[str, ...] = ()

    def to_protocol_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "generated_at": self.generated_at,
            "scope": "workspace",
            "page": self.page,
            "page_size": self.page_size,
            "total": self.total,
            "query": self.query,
            "items": [item.to_protocol_dict() for item in self.items],
            "warnings": list(self.warnings),
        }


async def build_session_list_snapshot(
    engine: Any,
    *,
    page: int,
    page_size: int,
    query: str,
) -> SessionListSnapshot:
    """List only sessions owned by the launch workspace and project public fields."""
    workspace = str(Path(engine.workspace_root).expanduser().resolve())
    sessions, total = await engine.list_sessions(
        page=page,
        page_size=page_size,
        query=query,
        workspace_root=workspace,
    )
    current_id = str(
        getattr(getattr(engine, "_session", None), "id", "") or ""
    ).strip()
    items: list[SessionListItem] = []
    dropped = 0
    for session in sessions[:page_size]:
        session_id = str(getattr(session, "id", "") or "").strip()
        if not _SESSION_ID.fullmatch(session_id):
            dropped += 1
            continue
        messages = getattr(session, "messages", None)
        public_messages = messages if isinstance(messages, list) else []
        user_count = sum(
            1
            for message in public_messages
            if isinstance(message, dict) and message.get("role") == "user"
        )
        items.append(
            SessionListItem(
                session_id=session_id,
                title=_text(getattr(session, "title", ""), 300) or "未命名会话",
                model=_text(getattr(session, "model", ""), 200),
                updated_at=_timestamp(getattr(session, "updated_at", "")),
                message_count=len(public_messages),
                user_message_count=user_count,
                git_branch=_text(getattr(session, "git_branch", ""), 200),
                is_current=session_id == current_id,
                resumable=user_count > 0,
            )
        )
    warnings = (f"已忽略 {dropped} 个标识格式异常的会话。",) if dropped else ()
    return SessionListSnapshot(
        generated_at=datetime.now(UTC).isoformat(),
        page=page,
        page_size=page_size,
        total=max(0, int(total)),
        query=query,
        items=tuple(items),
        warnings=warnings,
    )


__all__ = ["SessionListItem", "SessionListSnapshot", "build_session_list_snapshot"]
