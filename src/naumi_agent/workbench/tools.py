"""LLM tools for safe workbench interaction."""

from __future__ import annotations

import json
from typing import Any

from naumi_agent.tools.base import Tool
from naumi_agent.workbench.service import WorkbenchService


def create_workbench_tools(service: WorkbenchService) -> list[Tool]:
    return [
        WorkbenchSnapshotTool(service),
        WorkbenchProposeIssueTool(service),
    ]


class WorkbenchSnapshotTool(Tool):
    def __init__(self, service: WorkbenchService) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return "workbench_snapshot"

    @property
    def description(self) -> str:
        return "读取当前 Mac 工作台快照，包括 mission、issue、任务、失败卡片和审计事件。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"session_id": {"type": "string", "description": "会话 ID"}},
            "required": ["session_id"],
        }

    async def execute(self, *, session_id: str, **kwargs: Any) -> str:  # type: ignore[override]
        snapshot = await self._service.dashboard_snapshot(session_id)
        return json.dumps(snapshot, ensure_ascii=False, indent=2)


class WorkbenchProposeIssueTool(Tool):
    def __init__(self, service: WorkbenchService) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return "workbench_propose_issue"

    @property
    def description(self) -> str:
        return "创建 proposal 级别的问题建议，不直接修改代码或认领任务。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "mission_id": {"type": "string"},
                "title": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["session_id", "mission_id", "title", "reason"],
        }

    async def execute(  # type: ignore[override]
        self,
        *,
        session_id: str,
        mission_id: str,
        title: str,
        reason: str,
        **kwargs: Any,
    ) -> str:
        await self._service._workbench_store.append_event(
            session_id=session_id,
            type="proposal.issue_suggested",
            actor="Agent",
            subject_id=mission_id,
            payload={"title": title, "reason": reason},
        )
        return f"已记录建议 issue：{title}"
