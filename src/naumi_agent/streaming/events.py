"""事件类型与数据模型."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class EventType(StrEnum):
    # LLM 响应
    TOKEN_DELTA = "token_delta"
    THINKING_DELTA = "thinking_delta"
    THINKING_START = "thinking_start"
    THINKING_END = "thinking_end"

    # 工具调用
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_END = "tool_call_end"
    TOOL_CALL_ERROR = "tool_call_error"

    # 规划
    PLAN_CREATED = "plan_created"
    PLAN_STEP_START = "plan_step_start"
    PLAN_STEP_UPDATE = "plan_step_update"
    PLAN_STEP_END = "plan_step_end"

    # 记忆
    MEMORY_STORED = "memory_stored"
    MEMORY_RECALLED = "memory_recalled"
    CONTEXT_COMPACTED = "context_compacted"

    # 生命周期
    AGENT_START = "agent_start"
    AGENT_END = "agent_end"
    AGENT_ERROR = "agent_error"
    TURN_START = "turn_start"
    TURN_END = "turn_end"

    # 资源
    BUDGET_UPDATE = "budget_update"
    TOKEN_COUNT = "token_count"

    # Workbench / Mac app
    WORKBENCH_EVENT = "workbench_event"
    WORKBENCH_SNAPSHOT = "workbench_snapshot"


@dataclass(frozen=True)
class StreamEvent:
    """统一事件模型."""

    type: EventType
    data: dict
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    session_id: str = ""
    turn: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type.value,
            "data": self.data,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "turn": self.turn,
        }

    def to_sse(self) -> str:
        return f"data: {json.dumps(self.to_dict(), ensure_ascii=False)}\n\n"

    def to_ws(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)
