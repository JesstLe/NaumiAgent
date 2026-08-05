from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from naumi_agent.agents.base import AgentConfig
from naumi_agent.config.settings import AppConfig, ModelConfig, SafetyConfig
from naumi_agent.daemons.agent_jobs import AgentJobState
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.orchestrator.subagent_manager import SubTask
from naumi_agent.runtime.ports.events import RuntimeEventType
from naumi_agent.tools.base import Tool, ToolMetadata

pytestmark = pytest.mark.usefixtures("runtime_payload_key")


class _ToolLoopbackServer:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(length))
                requests.append(body)
                tool_result = next(
                    (
                        str(item.get("content", ""))
                        for item in body.get("messages", [])
                        if item.get("role") == "tool"
                    ),
                    "",
                )
                if tool_result:
                    message = {
                        "role": "assistant",
                        "content": f"独立生产路由完成：{tool_result}",
                    }
                else:
                    message = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-production-authority",
                                "type": "function",
                                "function": {
                                    "name": "bash_run",
                                    "arguments": (
                                        '{"command":"authority-secret-command"}'
                                    ),
                                },
                            }
                        ],
                    }
                response = {
                    "id": f"chatcmpl-production-{len(requests)}",
                    "object": "chat.completion",
                    "created": 1,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 4,
                        "total_tokens": 9,
                    },
                }
                encoded = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except BrokenPipeError:
                    return

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self) -> _ToolLoopbackServer:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class _AuthorityTool(Tool):
    def __init__(self, *, block: asyncio.Event | None = None) -> None:
        self.calls: list[str] = []
        self.started = asyncio.Event()
        self._block = block

    @property
    def name(self) -> str:
        return "bash_run"

    @property
    def description(self) -> str:
        return "生产路由权限验证工具"

    @property
    def parameters_schema(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            requires_confirmation=True,
            command_argument_names=("command",),
            user_facing_name="权限验证工具",
        )

    async def execute(self, *, command: str) -> str:
        self.calls.append(command)
        self.started.set()
        if self._block is not None:
            await self._block.wait()
        return "authority-secret-result"


def _engine(tmp_path: Path, base_url: str) -> AgentEngine:
    return AgentEngine(
        AppConfig(
            workspace_root=str(tmp_path),
            models=ModelConfig(
                default_model="openai/gpt-4o-mini",
                fast_model="openai/gpt-4o-mini",
                reasoning_model="openai/gpt-4o-mini",
                api_base=base_url,
                api_key="production-loopback-key",
                max_tokens=64,
                temperature=0,
            ),
            memory={
                "session_db_path": str(tmp_path / ".naumi" / "sessions.db"),
                "vector_db_path": str(tmp_path / ".naumi" / "vectors"),
                "long_term_enabled": False,
            },
            safety=SafetyConfig(
                permission_mode="moderate",
                allowed_dirs=[str(tmp_path)],
                max_parallel_agents=2,
                max_queued_agents=2,
            ),
        )
    )


def _spawn_test_agent(engine: AgentEngine) -> None:
    engine.subagent_manager.spawn(
        AgentConfig(
            name="worker-test",
            description="验证独立 Worker 生产路由",
            capabilities=[],
            tools=["bash_run"],
            system_prompt="SYSTEM-WORKER-ROUTING-SENTINEL",
            model_tier="capable",
            max_turns=5,
            timeout_seconds=30,
            permission_level="moderate",
        )
    )


@pytest.mark.asyncio
async def test_production_subagent_routes_through_worker_and_engine_authority(
    tmp_path: Path,
) -> None:
    with _ToolLoopbackServer() as loopback:
        engine = _engine(tmp_path, loopback.base_url)
        tool = _AuthorityTool()
        engine.tool_registry.register(tool)
        await engine.get_or_create_session()
        _spawn_test_agent(engine)
        confirmations: list[dict[str, object]] = []
        events: list[tuple[str, dict[str, Any]]] = []

        async def confirm(payload: dict[str, object]) -> str:
            confirmations.append(payload)
            return "allow_once"

        async def on_event(event: str, data: dict[str, Any]) -> None:
            events.append((event, data))

        engine.set_permission_confirmer(confirm)
        try:
            result = await engine.subagent_manager.delegate(
                SubTask(
                    id="production-worker-route",
                    description="执行一次权限验证工具",
                    agent_name="worker-test",
                    context="CONTEXT-WORKER-ROUTING-SENTINEL",
                ),
                event_callback=on_event,
            )
        finally:
            await engine.shutdown()

        assert result.status == "completed"
        assert result.response == "独立生产路由完成：authority-secret-result"
        assert result.total_tokens == 18
        assert result.turns == 2
        assert tool.calls == ["authority-secret-command"]
        assert len(confirmations) == 1
        assert confirmations[0]["agent_name"] == "worker-test"
        assert confirmations[0]["tool_name"] == "bash_run"
        bubbles = [
            data
            for event, data in events
            if event == RuntimeEventType.PERMISSION_BUBBLE.value
        ]
        assert [item["status"] for item in bubbles] == [
            "needs_confirmation",
            "confirmed",
        ]
        assert len(loopback.requests) == 2
        assert loopback.requests[0]["messages"][0] == {
            "role": "system",
            "content": (
                "## 前置上下文\nCONTEXT-WORKER-ROUTING-SENTINEL\n\n"
                "SYSTEM-WORKER-ROUTING-SENTINEL"
            ),
        }
        record = engine.subagent_manager.list_executions(limit=1)[0]
        assert record.worker_backend == "independent"
        assert record.worker_job_state == AgentJobState.COMPLETED.value
        assert record.worker_result_sha256
        assert record.worker_job_failure_code == ""
        assert record.worker_tool_scope == ("bash_run",)
        raw_databases = tuple(tmp_path.rglob("*.db"))
        assert raw_databases
        for path in raw_databases:
            raw = path.read_bytes()
            assert b"authority-secret-command" not in raw
            assert b"authority-secret-result" not in raw
            assert b"production-loopback-key" not in raw


@pytest.mark.asyncio
async def test_stopping_independent_tool_execution_preserves_running_recovery(
    tmp_path: Path,
) -> None:
    with _ToolLoopbackServer() as loopback:
        engine = _engine(tmp_path, loopback.base_url)
        release = asyncio.Event()
        tool = _AuthorityTool(block=release)
        engine.tool_registry.register(tool)
        await engine.get_or_create_session()
        _spawn_test_agent(engine)

        async def confirm(_payload: dict[str, object]) -> str:
            return "allow_once"

        engine.set_permission_confirmer(confirm)
        task = asyncio.create_task(
            engine.subagent_manager.delegate(
                SubTask(
                    id="production-worker-stop",
                    description="执行并停止权限验证工具",
                    agent_name="worker-test",
                )
            )
        )
        try:
            await asyncio.wait_for(tool.started.wait(), timeout=30)
            active_snapshot = await engine.agent_control.snapshot()
            active_execution = next(
                item
                for item in active_snapshot.executions
                if item.task_id == "production-worker-stop"
            )
            assert active_execution.worker_backend == "independent"
            assert active_execution.phase == "running_tool"
            assert active_execution.current_tool == "bash_run"
            assert active_execution.worker_job_state == AgentJobState.RUNNING.value
            stopped = await engine.subagent_manager.stop_execution(
                "production-worker-stop"
            )
            assert stopped.accepted is True
            result = await asyncio.wait_for(task, timeout=30)
        finally:
            release.set()
            await engine.shutdown()

        assert result.status == "cancelled"
        record = engine.subagent_manager.list_executions(limit=1)[0]
        assert record.worker_backend == "independent"
        assert record.worker_job_state == AgentJobState.RUNNING.value
        assert record.worker_result_sha256 == ""
        assert record.worker_job_failure_code == (
            "agent_worker_interrupted_recovery_required"
        )
