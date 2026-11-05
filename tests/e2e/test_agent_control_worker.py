"""Production worker control acceptance with real local HTTP, files, and UI reducers."""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from naumi_agent.agents.base import AgentConfig, AgentResult
from naumi_agent.config.settings import AppConfig, MemoryConfig, ModelConfig, SafetyConfig
from naumi_agent.daemons.agent_jobs import AgentJobState, AgentJobStore
from naumi_agent.orchestrator.subagent_manager import SubTask
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.tools.base import InterruptBehavior, Tool, ToolMetadata
from naumi_agent.tui.agent_control import format_agent_control_markdown
from naumi_agent.ui.bridge import JsonlEngineBridge

pytestmark = pytest.mark.usefixtures("runtime_payload_key")


class _ControlLoopbackServer:
    """Serve deterministic model protocol responses, never an external provider."""

    def __init__(self, paths: dict[str, Path]) -> None:
        self.requests: list[dict[str, Any]] = []
        requests = self.requests
        request_lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(length))
                with request_lock:
                    requests.append(body)
                    request_number = len(requests)
                messages = body["messages"]
                combined = "\n".join(str(item.get("content", "")) for item in messages)
                role = "target" if "CONTROL-TARGET" in combined else "sibling"
                tool_results = [item for item in messages if item.get("role") == "tool"]
                if tool_results:
                    message: dict[str, Any] = {
                        "role": "assistant",
                        "content": f"文件读取完成：{tool_results[-1]['content']}",
                    }
                    finish_reason = "stop"
                else:
                    message = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": f"call-control-{role}",
                            "type": "function",
                            "function": {
                                "name": "control_file_read",
                                "arguments": json.dumps({"path": str(paths[role])}),
                            },
                        }],
                    }
                    finish_reason = "tool_calls"
                response = {
                    "id": f"chatcmpl-control-{request_number}",
                    "object": "chat.completion",
                    "created": 1,
                    "model": body["model"],
                    "choices": [{
                        "index": 0,
                        "message": message,
                        "finish_reason": finish_reason,
                    }],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
                }
                encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    return

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self) -> _ControlLoopbackServer:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        assert not self.thread.is_alive()


class _ControlledFileRead(Tool):
    """Read real exact files before holding independent, cancellable tool operations."""

    def __init__(self, paths: dict[str, Path]) -> None:
        self.paths = paths
        self.started = {role: asyncio.Event() for role in paths}
        self.release = {role: asyncio.Event() for role in paths}
        self.cancelled = {role: asyncio.Event() for role in paths}
        self.reads: dict[str, list[str]] = {role: [] for role in paths}
        self.completed: list[str] = []

    @property
    def name(self) -> str:
        return "control_file_read"

    @property
    def description(self) -> str:
        return "读取本次控制中心验收的指定文件"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            requires_confirmation=False,
            path_argument_names=("path",),
            command_argument_names=(),
            interrupt_behavior=InterruptBehavior.CANCEL,
            user_facing_name="读取验收文件",
        )

    async def execute(self, *, path: str) -> str:
        exact_path = Path(path).resolve(strict=True)
        role = next(role for role, expected in self.paths.items() if exact_path == expected)
        content = exact_path.read_text(encoding="utf-8")
        self.reads[role].append(content)
        self.started[role].set()
        try:
            await self.release[role].wait()
        except asyncio.CancelledError:
            self.cancelled[role].set()
            raise
        self.completed.append(role)
        return content


def _records(writer: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in writer.getvalue().splitlines() if line]


def _render_with_node(
    records: list[dict[str, Any]], session_id: str, selected_id: str,
) -> dict[str, Any]:
    script = r"""
import fs from 'node:fs';
import { stripAnsi } from './frontend/terminal-ui/src/ansi.js';
import { normalizeServerRecord } from './frontend/terminal-ui/src/protocol.js';
import { createInitialState, reduceServerEvent } from './frontend/terminal-ui/src/state.js';
import { renderScreen } from './frontend/terminal-ui/src/render.js';
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const state = createInitialState();
state.currentSessionId = input.session_id;
state.route = { name: 'agents', originAnchor: null };
state.agents.open = true;
state.agents.selectedTab = 'executions';
for (const raw of input.records) {
  reduceServerEvent(state, normalizeServerRecord(raw));
}
state.agents.detailId = input.selected_id;
process.stdout.write(JSON.stringify({
  revision: state.agents.revision,
  executions: state.agents.snapshot?.executions ?? [],
  screen: renderScreen(state, 140, 40).map(stripAnsi).join('\n'),
}));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parents[2],
        env=os.environ.copy(),
        input=json.dumps({
            "records": records, "session_id": session_id, "selected_id": selected_id,
        }),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.asyncio
async def test_independent_workers_bridge_stop_preserves_sibling_and_durable_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = {role: (tmp_path / f"{role}.txt").resolve() for role in ("target", "sibling")}
    contents = {"target": "目标文件原文-731", "sibling": "兄弟文件原文-946"}
    for role, path in paths.items():
        path.write_text(contents[role], encoding="utf-8")

    with _ControlLoopbackServer(paths) as loopback:
        engine = create_agent_engine(AppConfig(
            workspace_root=str(tmp_path),
            models=ModelConfig(
                default_model="openai/gpt-4o-mini",
                fast_model="openai/gpt-4o-mini",
                reasoning_model="openai/gpt-4o-mini",
                api_base=loopback.base_url,
                api_key="control-loopback-only-key",
                temperature=0,
                max_tokens=64,
            ),
            memory=MemoryConfig(
                session_db_path=str(tmp_path / ".naumi" / "sessions.db"),
                vector_db_path=str(tmp_path / ".naumi" / "vectors"),
                long_term_enabled=False,
            ),
            safety=SafetyConfig(
                permission_mode="moderate",
                allowed_dirs=[str(tmp_path)],
                max_parallel_agents=2,
                max_queued_agents=2,
            ),
        ))
        tool = _ControlledFileRead(paths)
        engine.tool_registry.register_unique(tool)
        delegates: dict[str, asyncio.Task[AgentResult]] = {}
        embedded_calls: list[str] = []

        async def forbidden_embedded_execute(**_kwargs: Any) -> AgentResult:
            embedded_calls.append("unexpected embedded execution")
            raise AssertionError("Production worker routing entered the embedded backend")

        try:
            assert engine.agent_worker_process_factory.model_execution_enabled is True
            session = await engine.get_or_create_session(title="独立 Worker 控制验收")
            writer = io.StringIO()
            bridge = JsonlEngineBridge(engine, config_path="config.yaml")
            bridge.bind_writer(writer)
            for role in paths:
                name = f"control-{role}"
                engine.subagent_manager.spawn(AgentConfig(
                    name=name,
                    description="独立 Worker 文件验收",
                    capabilities=[],
                    tools=[tool.name],
                    system_prompt="读取指定文件并返回原文。",
                    model_tier="capable",
                    permission_level="moderate",
                    max_turns=3,
                    timeout_seconds=30,
                ))
                agent = engine.subagent_manager.get_agent(name)
                assert agent is not None
                monkeypatch.setattr(agent, "execute", forbidden_embedded_execute)
                delegates[role] = asyncio.create_task(engine.subagent_manager.delegate(
                    SubTask(f"control-{role}-task", f"CONTROL-{role.upper()}", name),
                    event_callback=bridge.handle_engine_event,
                ))

            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in tool.started.values())), timeout=30,
            )
            assert tool.reads == {role: [contents[role]] for role in paths}
            assert embedded_calls == []
            await bridge.handle_client_record({
                "id": "worker-open",
                "type": "agents/request",
                "payload": {"open": True, "known_revision": 0, "session_id": session.id},
            })
            opened = [item for item in _records(writer) if item["type"] == "agents/snapshot"][-1]
            active = {item["task_id"]: item for item in opened["payload"]["executions"]}
            assert set(active) == {f"control-{role}-task" for role in paths}
            for item in active.values():
                assert item["status"] == "running"
                assert item["phase"] == "running_tool"
                assert item["worker_backend"] == "independent"
                assert item["worker_job_state"] == "running"
                assert item["worker_claim_epoch"] == 1
                assert item["worker_tool_scope"] == [tool.name]
                assert item["current_tool"] == tool.name
                assert item["worker_request_sha256"]

            before = _render_with_node([opened], session.id, "control-target-task")
            assert "control-target-task" in before["screen"]
            assert "control-sibling-task" in before["screen"]
            assert tool.name in before["screen"]
            before_textual = format_agent_control_markdown(
                await engine.agent_control.snapshot(), "executions", "control-target-task",
            )
            assert "执行后端：独立 Agent Worker" in before_textual
            assert tool.name in before_textual

            await bridge.handle_client_record({
                "id": "worker-stop",
                "type": "agents/stop",
                "payload": {
                    "session_id": session.id,
                    "task_id": "control-target-task",
                    "reason": "仅停止目标文件读取。",
                },
            })
            target_result = await asyncio.wait_for(delegates["target"], timeout=30)
            assert target_result.status == "cancelled"
            assert tool.cancelled["target"].is_set()
            assert not delegates["sibling"].done()
            assert not tool.cancelled["sibling"].is_set()
            assert tool.completed == []
            stopped = {item.task_id: item for item in engine.subagent_manager.list_executions()}
            target_record = stopped["control-target-task"]
            assert target_record.worker_job_state == AgentJobState.RUNNING.value
            assert target_record.worker_job_failure_code == (
                "agent_worker_interrupted_recovery_required"
            )
            assert target_record.worker_result_sha256 == ""
            assert stopped["control-sibling-task"].status == "running"

            # A second stop is an observation, not a replay or sibling cancellation.
            await bridge.handle_client_record({
                "id": "worker-stop-again",
                "type": "agents/stop",
                "payload": {"session_id": session.id, "task_id": "control-target-task"},
            })
            actions = [item for item in _records(writer) if item["type"] == "agents/action"]
            assert actions[-2]["payload"]["accepted"] is True
            assert actions[-2]["payload"]["code"] == "accepted"
            assert actions[-1]["payload"]["accepted"] is False
            assert actions[-1]["payload"]["code"] == "already_finished"

            tool.release["sibling"].set()
            sibling_result = await asyncio.wait_for(delegates["sibling"], timeout=30)
            expected_response = f"文件读取完成：{contents['sibling']}"
            assert sibling_result.status == "completed"
            assert sibling_result.response == expected_response
            assert sibling_result.total_tokens == 18
            assert sibling_result.turns == 2
            assert tool.completed == ["sibling"]
            assert embedded_calls == []

            await bridge.handle_client_record({
                "id": "worker-final-read",
                "type": "agents/request",
                "payload": {"open": True, "known_revision": 0, "session_id": session.id},
            })
            records = [item for item in _records(writer) if item["type"].startswith("agents/")]
            rendered = _render_with_node(records, session.id, "control-target-task")
            final = {item["task_id"]: item for item in rendered["executions"]}
            assert final["control-target-task"]["status"] == "cancelled"
            assert final["control-target-task"]["worker_job_state"] == "running"
            assert final["control-target-task"]["worker_job_failure_code"] == (
                "agent_worker_interrupted_recovery_required"
            )
            assert final["control-sibling-task"]["status"] == "completed"
            assert final["control-sibling-task"]["worker_job_state"] == "completed"
            assert final["control-sibling-task"]["worker_job_failure_code"] == ""
            assert "cancelled" in rendered["screen"]
            assert "持久任务" in rendered["screen"]
            snapshot = await engine.agent_control.snapshot()
            target_textual = format_agent_control_markdown(
                snapshot, "executions", "control-target-task",
            )
            assert "状态 `running`" in target_textual
            assert "agent_worker_interrupted_recovery_required" in target_textual
            assert "不可停止" in target_textual
            sibling_textual = format_agent_control_markdown(
                snapshot, "executions", "control-sibling-task",
            )
            assert "状态 `completed`" in sibling_textual
            assert "Token：18" in sibling_textual

            # Reopen the original authority; authenticate the exact original result.
            reopened = AgentJobStore(engine._resources.agent_job_store.db_path)
            target_job = await reopened.get(final["control-target-task"]["worker_job_id"])
            sibling_job = await reopened.get(final["control-sibling-task"]["worker_job_id"])
            assert target_job is not None and target_job.state is AgentJobState.RUNNING
            assert sibling_job is not None and sibling_job.state is AgentJobState.COMPLETED
            terminal = await reopened.recover_terminal_payload(
                sibling_job.job_id,
                expected_result_sha256=final["control-sibling-task"]["worker_result_sha256"],
            )
            assert terminal.response == expected_response
            assert terminal.error == ""
            assert len(loopback.requests) == 3
            assert sum(
                any(message.get("role") == "tool" for message in request["messages"])
                for request in loopback.requests
            ) == 1
        finally:
            for event in tool.release.values():
                event.set()
            for task in delegates.values():
                if not task.done():
                    task.cancel()
            try:
                if delegates:
                    await asyncio.wait_for(
                        asyncio.gather(*delegates.values(), return_exceptions=True), timeout=30,
                    )
            finally:
                await engine.shutdown()

    assert all(path.read_text(encoding="utf-8") == contents[role] for role, path in paths.items())
