"""Real cross-frontend acceptance for the authoritative Agent Control Center."""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from naumi_agent.agents.base import AgentResult
from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.orchestrator.subagent_manager import SubTask
from naumi_agent.tui.agent_control import format_agent_control_markdown
from naumi_agent.ui.bridge import JsonlEngineBridge


def _records(writer: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in writer.getvalue().splitlines() if line]


def _render_records_with_node(
    project_root: Path,
    records: list[dict[str, Any]],
    session_id: str,
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
  const record = normalizeServerRecord(raw);
  reduceServerEvent(state, record);
}
state.agents.detailId = 'target-task';
const screen = renderScreen(state, 120, 30).map(stripAnsi).join('\n');
process.stdout.write(JSON.stringify({
  revision: state.agents.revision,
  executions: state.agents.snapshot?.executions ?? [],
  screen,
}));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=project_root,
        env=os.environ.copy(),
        input=json.dumps({"records": records, "session_id": session_id}),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.asyncio
async def test_real_manager_bridge_node_stop_and_textual_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    engine = AgentEngine(AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / "sessions.db"),
            vector_db_path=str(tmp_path / "vectors"),
            long_term_enabled=False,
        ),
    ))
    target_delegate: asyncio.Task[AgentResult] | None = None
    sibling_delegate: asyncio.Task[AgentResult] | None = None
    try:
        session = await engine.get_or_create_session(title="Agent Control 真实验收")
        target_agent = engine.subagent_manager.get_agent("coder")
        sibling_agent = engine.subagent_manager.get_agent("researcher")
        assert target_agent is not None
        assert sibling_agent is not None
        target_started = asyncio.Event()
        sibling_started = asyncio.Event()
        sibling_release = asyncio.Event()

        async def target_execute(
            *,
            event_callback: Any,
            **kwargs: Any,
        ) -> AgentResult:
            await event_callback("tool_start", {"tool_name": "file_read"})
            target_started.set()
            await asyncio.Event().wait()
            return AgentResult(status="completed", turns=1)

        async def sibling_execute(
            *,
            event_callback: Any,
            **kwargs: Any,
        ) -> AgentResult:
            await event_callback("tool_start", {"tool_name": "grep"})
            sibling_started.set()
            await sibling_release.wait()
            await event_callback("tool_result", {"tool_name": "grep"})
            return AgentResult(
                status="completed",
                response="兄弟任务正常完成。",
                total_tokens=17,
                total_cost_usd=0.002,
                turns=1,
            )

        monkeypatch.setattr(target_agent, "execute", target_execute)
        monkeypatch.setattr(sibling_agent, "execute", sibling_execute)
        target_delegate = asyncio.create_task(engine.subagent_manager.delegate(
            SubTask("target-task", "等待用户停止", "coder")
        ))
        sibling_delegate = asyncio.create_task(engine.subagent_manager.delegate(
            SubTask("sibling-task", "独立完成", "researcher")
        ))
        await asyncio.wait_for(target_started.wait(), timeout=1)
        await asyncio.wait_for(sibling_started.wait(), timeout=1)

        writer = io.StringIO()
        bridge = JsonlEngineBridge(engine, config_path="config.yaml")
        bridge.bind_writer(writer)
        await bridge.handle_client_record({
            "id": "agents-real-open",
            "type": "agents/request",
            "payload": {
                "open": True,
                "known_revision": 0,
                "session_id": session.id,
            },
        })
        opened = next(
            record for record in _records(writer)
            if record["type"] == "agents/snapshot"
        )
        assert {item["task_id"] for item in opened["payload"]["executions"]} == {
            "target-task",
            "sibling-task",
        }
        assert all(
            item["status"] == "running"
            for item in opened["payload"]["executions"]
        )

        before = _render_records_with_node(
            project_root,
            [opened],
            session.id,
        )
        assert "target-task" in before["screen"]
        assert "sibling-task" in before["screen"]
        assert "file_read" in before["screen"]

        await bridge.handle_client_record({
            "id": "agents-real-stop",
            "type": "agents/stop",
            "payload": {
                "session_id": session.id,
                "task_id": "target-task",
                "reason": "真实 E2E 确认停止。",
            },
        })
        target_result = await asyncio.wait_for(target_delegate, timeout=1)
        assert target_result.status == "cancelled"
        assert not sibling_delegate.done()
        await bridge.handle_engine_event("subagent_event", {
            "task_id": "target-task",
            "agent_name": "coder",
            "status": "cancelled",
        })

        sibling_release.set()
        sibling_result = await asyncio.wait_for(sibling_delegate, timeout=1)
        assert sibling_result.status == "completed"
        assert sibling_result.response == "兄弟任务正常完成。"
        await bridge.handle_engine_event("subagent_event", {
            "task_id": "sibling-task",
            "agent_name": "researcher",
            "status": "completed",
        })

        bridge_records = [
            record for record in _records(writer)
            if record["type"].startswith("agents/")
        ]
        action = next(
            record for record in bridge_records
            if record["type"] == "agents/action"
        )
        assert action["payload"]["accepted"] is True
        assert action["payload"]["code"] == "accepted"

        rendered = _render_records_with_node(
            project_root,
            bridge_records,
            session.id,
        )
        by_id = {item["task_id"]: item for item in rendered["executions"]}
        assert by_id["target-task"]["status"] == "cancelled"
        assert by_id["sibling-task"]["status"] == "completed"
        assert "cancelled" in rendered["screen"]
        assert "当前不可停止" in rendered["screen"]

        final_snapshot = await engine.agent_control.snapshot()
        textual = format_agent_control_markdown(
            final_snapshot,
            "executions",
            "target-task",
        )
        assert "target-task" in textual
        assert "cancelled" in textual
        assert "不可停止" in textual
        sibling_textual = format_agent_control_markdown(
            final_snapshot,
            "executions",
            "sibling-task",
        )
        assert "sibling-task" in sibling_textual
        assert "completed" in sibling_textual
        assert "Token：17" in sibling_textual
    finally:
        for delegated in (target_delegate, sibling_delegate):
            if delegated is not None and not delegated.done():
                delegated.cancel()
        await asyncio.gather(
            *(
                delegated
                for delegated in (target_delegate, sibling_delegate)
                if delegated is not None
            ),
            return_exceptions=True,
        )
        await engine.shutdown()
