"""Immutable public records for the personal agent catalog."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


class PersonalAgentError(ValueError):
    """A stable error code paired with a user-facing explanation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class PersonalAgent:
    id: str
    name: str
    description: str
    instructions: str
    model_tier: str
    tool_names: tuple[str, ...]
    kind: str
    member_ids: tuple[str, ...]
    status: str
    version: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["tool_names"] = list(self.tool_names)
        result["member_ids"] = list(self.member_ids)
        return result


@dataclass(frozen=True, slots=True)
class PersonalAgentBinding:
    agent_id: str
    workspace_key: str
    session_id: str
    session_db_key: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)
