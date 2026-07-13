from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from naumi_agent.agent_control import AgentControlSnapshot
from naumi_agent.inspector import RuntimeInspectorSnapshot
from naumi_agent.orchestrator.engine import AgentResult, AgentRuntimeMode, AgentUsage
from naumi_agent.orchestrator.subagent_manager import StopExecutionResult
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
                    "budget_enabled": False,
                    "budget_used_usd": 0.02,
                    "budget_max_usd": None,
                    "budget_percentage": None,
                    "budget_max_input_tokens": None,
                    "budget_max_output_tokens": None,
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


class FakeAgentControl:
    def __init__(self, engine: FakeEngine) -> None:
        self._engine = engine

    async def snapshot(self) -> AgentControlSnapshot:
        stopped = self._engine.agent_stopped
        return AgentControlSnapshot.from_dict(
            {
                "schema_version": 1,
                "session_id": "session-python",
                "revision": 2 if stopped else 1,
                "generated_at": "2026-07-13T00:00:01+00:00",
                "summary": {
                    "total_agents": 1,
                    "active_agents": 0 if stopped else 1,
                    "attention_agents": 0,
                    "stoppable_executions": 0 if stopped else 1,
                    "pending_messages": 0,
                },
                "agents": [
                    {
                        "name": "coder",
                        "description": "Python Bridge 编程 Agent",
                        "kind": "preset",
                        "state": "ready" if stopped else "running",
                        "task_count": 1,
                        "model_tier": "capable",
                        "capabilities": ["coding"],
                        "tools": ["file_write"],
                        "permission_level": "standard",
                        "age_ms": 1200,
                        "heartbeat_age_ms": 30,
                    }
                ],
                "executions": [
                    {
                        "task_id": "python-agent-task",
                        "session_id": "session-python",
                        "agent_name": "coder",
                        "description": "验证 Python Bridge Agent 控制",
                        "status": "cancelled" if stopped else "running",
                        "phase": "finished" if stopped else "running_tool",
                        "started_at": 1.0,
                        "finished_at": 2.0 if stopped else None,
                        "elapsed_ms": 1000,
                        "heartbeat_age_ms": 30,
                        "current_tool": "" if stopped else "file_write",
                        "recent_tools": ["file_read", "file_write"],
                        "total_tokens": 64,
                        "total_cost_usd": 0.01,
                        "turns": 1,
                        "error": "",
                        "stop_supported": not stopped,
                        "stop_requested": stopped,
                    }
                ],
                "team_messages": [],
                "blackboard": [],
                "warnings": [],
            }
        )

    @staticmethod
    def changed_sections(
        previous: AgentControlSnapshot,
        current: AgentControlSnapshot,
    ) -> tuple[str, ...]:
        return tuple(
            name
            for name in (
                "summary",
                "agents",
                "executions",
                "team_messages",
                "blackboard",
                "warnings",
            )
            if getattr(previous, name) != getattr(current, name)
        )


class FakeSubAgentManager:
    def __init__(self, engine: FakeEngine) -> None:
        self._engine = engine

    def list_executions(self, limit: int = 100) -> list[Any]:
        if limit < 1:
            return []
        return [
            SimpleNamespace(
                task_id="python-agent-task",
                session_id="session-python",
            )
        ]

    async def stop_execution(
        self,
        task_id: str,
        reason: str = "用户请求停止子 Agent。",
    ) -> StopExecutionResult:
        del reason
        if task_id != "python-agent-task":
            return StopExecutionResult(task_id, False, "not_found", "未找到 Agent 执行。")
        if self._engine.agent_stopped:
            return StopExecutionResult(task_id, False, "already_finished", "Agent 执行已结束。")
        self._engine.agent_stopped = True
        return StopExecutionResult(task_id, True, "accepted", "已请求停止 Agent 执行。")


class FakeEngine:
    def __init__(self) -> None:
        self.runtime_mode = AgentRuntimeMode.DEFAULT
        self.permission_mode = PermissionMode.MODERATE
        self.workspace_root = Path.cwd()
        self.usage = AgentUsage(total_input_tokens=21, total_output_tokens=8, turns=1)
        self.router = FakeRouter()
        self.task_store = FakeTaskStore()
        self.runtime_inspector = FakeRuntimeInspector()
        self.agent_stopped = False
        self.agent_control = FakeAgentControl(self)
        self.subagent_manager = FakeSubAgentManager(self)
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
        return {
            "enabled": False,
            "used_usd": 0.02,
            "max_usd": None,
            "remaining_usd": None,
            "cost_percentage": None,
            "input_tokens": 21,
            "max_input_tokens": None,
            "input_percentage": None,
            "output_tokens": 8,
            "max_output_tokens": None,
            "output_percentage": None,
            "percentage": None,
        }

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
