from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from naumi_agent.inspector import RuntimeInspectorSnapshot
from naumi_agent.orchestrator.engine import AgentResult, AgentRuntimeMode, AgentUsage
from naumi_agent.safety.permissions import PermissionMode
from naumi_agent.tasks.models import Task, TaskStatus
from naumi_agent.ui.bridge import JsonlEngineBridge, serve_stdio


class FakeRouter:
    def resolve_model(self, tier: str) -> str:
        return f"python-fixture-{tier}"


class FakeTaskStore:
    async def list_tasks(self) -> list[Task]:
        return [
            Task(
                id="1",
                session_id="session-python",
                subject="写入 Python bridge 页面",
                description="",
                status=TaskStatus.IN_PROGRESS,
                active_form="正在写入 Python bridge 页面",
                owner="main",
                updated_at="2026-06-02T12:00:00",
            )
        ]


class FakeRuntimeInspector:
    async def snapshot(self) -> RuntimeInspectorSnapshot:
        return RuntimeInspectorSnapshot.from_dict(
            {
                "schema_version": 1,
                "session_id": "session-python",
                "revision": 1,
                "generated_at": "2026-07-13T00:00:00+00:00",
                "active_run_id": "",
                "plan": {
                    "state": "ready",
                    "items": [
                        {
                            "id": "1",
                            "subject": "同步 Python Bridge Inspector",
                            "status": "in_progress",
                            "active_form": "正在同步 Python Bridge Inspector",
                            "owner": "main",
                            "blocked_by": [],
                        }
                    ],
                    "next_actions": [],
                    "warnings": [],
                },
                "tools": {
                    "state": "empty",
                    "items": [],
                    "approvals": [],
                    "warnings": [],
                },
                "context": {
                    "state": "ready",
                    "workspace_root": str(Path.cwd()),
                    "branch": "fixture",
                    "commit": "",
                    "git_available": False,
                    "git_dirty": False,
                    "model": "python-fixture-capable",
                    "runtime_mode": "default",
                    "permission_mode": "moderate",
                    "context_used": 21,
                    "context_window": 256000,
                    "context_percentage": 0.01,
                    "budget_used_usd": 0.02,
                    "budget_max_usd": 5.0,
                    "budget_percentage": 0.4,
                    "input_tokens": 21,
                    "output_tokens": 8,
                    "turns": 1,
                    "warnings": [],
                },
                "changes": {
                    "state": "empty",
                    "items": [],
                    "git_state": {},
                    "warnings": [],
                },
                "tests": {
                    "state": "empty",
                    "validations": [],
                    "unverified": [],
                    "next_actions": [],
                    "warnings": [],
                },
            }
        )


class FakeEngine:
    def __init__(self) -> None:
        self.runtime_mode = AgentRuntimeMode.DEFAULT
        self.permission_mode = PermissionMode.MODERATE
        self.workspace_root = Path.cwd()
        self.usage = AgentUsage(total_input_tokens=21, total_output_tokens=8, turns=1)
        self.router = FakeRouter()
        self.task_store = FakeTaskStore()
        self.runtime_inspector = FakeRuntimeInspector()
        self.permission_confirmer = None
        self._session = None

    async def get_or_create_session(self) -> Any:
        if self._session is None:
            self._session = SimpleNamespace(
                id="session-python",
                title="Python Bridge",
                messages=[],
            )
        return self._session

    def set_permission_confirmer(self, confirmer: Any) -> None:
        self.permission_confirmer = confirmer

    def set_runtime_mode(self, mode: str) -> AgentRuntimeMode:
        self.runtime_mode = AgentRuntimeMode(mode)
        if self.runtime_mode == AgentRuntimeMode.PLAN:
            self.permission_mode = PermissionMode.STRICT
        elif self.runtime_mode == AgentRuntimeMode.BYPASS:
            self.permission_mode = PermissionMode.BYPASS
        else:
            self.permission_mode = PermissionMode.MODERATE
        return self.runtime_mode

    def cycle_runtime_mode(self) -> AgentRuntimeMode:
        modes = [
            AgentRuntimeMode.DEFAULT,
            AgentRuntimeMode.PLAN,
            AgentRuntimeMode.BYPASS,
        ]
        index = modes.index(self.runtime_mode)
        return self.set_runtime_mode(modes[(index + 1) % len(modes)].value)

    def get_context_info(self) -> dict[str, Any]:
        return {"used": 21, "window": 256000, "percentage": 0.01}

    def get_budget_info(self) -> dict[str, Any]:
        return {"used_usd": 0.02, "max_usd": 5.0, "percentage": 0.4}

    def get_recent_permission_bubbles(self, limit: int = 8) -> list[dict[str, Any]]:
        return [
            {
                "request_id": "perm-python-1",
                "agent_name": "main",
                "tool_name": "file_write",
                "status": "confirmed",
                "reason": "Python bridge fixture 已确认。",
            }
        ][-limit:]

    async def run_streaming(self, task: str, on_event: Any) -> AgentResult:
        await on_event("turn_start", {"turn": 1, "model": "python-fixture-capable"})
        await on_event("response_start", {})
        await on_event("token", {"content": f"Python bridge 收到: {task}"})
        await on_event("response_end", {})
        if self.permission_confirmer is not None:
            await self.permission_confirmer(
                {
                    "tool_name": "file_write",
                    "call_id": "perm-python-1",
                    "reason": "需要写入 fixture 输出文件。",
                    "requires_confirmation": True,
                }
            )
        await on_event(
            "tool_prepare_start",
            {
                "name": "file_write",
                "tool_call_id": "call-python-1",
                "path": "python-fixture/index.html",
                "argument_chars": 128,
                "elapsed_ms": 1,
            },
        )
        await on_event(
            "tool_prepare_snapshot",
            {
                "name": "file_write",
                "tool_call_id": "call-python-1",
                "path": "python-fixture/index.html",
                "argument_chars": 2048,
                "content_chars": 480,
                "content_lines": 12,
                "elapsed_ms": 5,
            },
        )
        await on_event(
            "tool_prepare_end",
            {
                "name": "file_write",
                "tool_call_id": "call-python-1",
                "path": "python-fixture/index.html",
                "argument_chars": 2048,
                "content_chars": 480,
                "content_lines": 12,
                "elapsed_ms": 8,
            },
        )
        await on_event(
            "tool_start",
            {
                "name": "file_write",
                "call_id": "call-python-1",
                "args": {"file_path": "python-fixture/index.html"},
            },
        )
        await on_event(
            "tool_end",
            {
                "name": "file_write",
                "call_id": "call-python-1",
                "status": "success",
                "duration_ms": 9,
                "content": "--- a/python-fixture/index.html\n"
                "+++ b/python-fixture/index.html\n"
                "@@\n"
                "-old\n"
                "+new from python bridge\n",
            },
        )
        self.usage = AgentUsage(total_input_tokens=42, total_output_tokens=16, turns=2)
        return AgentResult(status="completed", response="完成", usage=self.usage)

    async def list_sessions(self, page: int = 1, page_size: int = 20) -> tuple[list[Any], int]:
        session = SimpleNamespace(
            id="session-python",
            title="Python Bridge 恢复",
            messages=[
                {"role": "user", "content": "恢复 Python bridge 会话"},
                {"role": "assistant", "content": "这是 Python bridge replay。"},
            ],
        )
        return [session], 1

    async def load_session(self, session_id: str) -> bool:
        if session_id != "session-python":
            return False
        self._session = SimpleNamespace(
            id="session-python",
            title="Python Bridge 恢复",
            messages=[
                {"role": "user", "content": "恢复 Python bridge 会话"},
                {"role": "assistant", "content": "这是 Python bridge replay。"},
            ],
        )
        return True

    async def shutdown(self) -> None:
        return None


async def main() -> None:
    bridge = JsonlEngineBridge(
        FakeEngine(),
        config_path="python-bridge-fixture.yaml",
        debug_trace=None,
    )
    await serve_stdio(bridge)


if __name__ == "__main__":
    asyncio.run(main())
