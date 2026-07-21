from __future__ import annotations

import asyncio
import io
import json
import subprocess
from pathlib import Path

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.daemons.permission_decisions import (
    PermissionDecisionActor,
    PermissionDecisionOutcome,
    PermissionDecisionSource,
)
from naumi_agent.memory.session import Session
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.safety.permissions import PermissionMode
from naumi_agent.ui.bridge import JsonlEngineBridge
from naumi_agent.ui.protocol import ClientEventType


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        workspace_root=str(tmp_path),
        memory=MemoryConfig(
            session_db_path=str(tmp_path / ".naumi" / "sessions.db"),
            vector_db_path=str(tmp_path / ".naumi" / "chroma"),
            long_term_enabled=False,
        ),
    )


def _records(writer: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in writer.getvalue().splitlines() if line]


def _render_with_node(repo_root: Path, record: dict[str, object]) -> dict[str, object]:
    source = r"""
import { stripAnsi, visibleWidth } from "./frontend/terminal-ui/src/ansi.js";
import {
  renderPermissionCenterPage,
} from "./frontend/terminal-ui/src/components/permission-center-page.js";
import { normalizeServerRecord } from "./frontend/terminal-ui/src/protocol.js";
import { createInitialState, reduceServerEvent } from "./frontend/terminal-ui/src/state.js";
let input = "";
for await (const chunk of process.stdin) input += chunk;
const state = createInitialState();
reduceServerEvent(state, normalizeServerRecord(JSON.parse(input)));
const widths = {};
for (const width of [80, 120, 200]) {
  const lines = renderPermissionCenterPage(state.permissionCenter, width, 24);
  widths[width] = {
    bounded: lines.every((line) => visibleWidth(line) <= width),
    text: lines.map(stripAnsi).join("\n"),
  };
}
process.stdout.write(JSON.stringify({ snapshot: state.permissionCenter.snapshot, widths }));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=repo_root,
        input=json.dumps(record, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    return json.loads(completed.stdout)


@pytest.mark.asyncio
async def test_real_engine_bridge_node_permission_snapshot(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    engine = create_agent_engine(_config(tmp_path))
    session = Session(id="permission-real-session", workspace_root=str(tmp_path))
    engine._session = session
    engine._permission_grant_store.create(session.id, "shell", "perm-source")
    await engine._permission_decision_store.issue(
        request_id="history-real",
        session_id=session.id,
        run_id="run-history-real",
        call_id="history-real",
        agent_name="main",
        tool_name="file_write",
        tool_family="filesystem",
        arguments={"path": "target.txt"},
        outcome=PermissionDecisionOutcome.ALLOW_ONCE,
        actor=PermissionDecisionActor.USER,
        source=PermissionDecisionSource.USER_CONFIRMATION,
        permission_mode=PermissionMode.MODERATE,
        risk_level="medium",
        decided_at="2026-07-21T12:00:00+00:00",
    )
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")
    bridge.bind_writer(writer)
    pending_task = asyncio.create_task(
        bridge.confirm_permission(
            {
                "call_id": "pending-real",
                "session_id": session.id,
                "run_id": "run-real",
                "tool_name": "bash_run",
                "tool_family": "shell",
                "arguments": {"command": "printf permission-snapshot"},
                "reason": "运行只读集成检查。",
                "risk_level": "medium",
                "choices": ["allow_once", "deny", "grant_session"],
            }
        )
    )
    await asyncio.sleep(0)

    try:
        await bridge.handle_client_record(
            {
                "id": "permission-panel-real",
                "type": ClientEventType.PERMISSIONS_PANEL,
                "payload": {"limit": 12},
            }
        )
        snapshot_record = next(
            record for record in _records(writer) if record["type"] == "permissions/snapshot"
        )
        await bridge.resolve_permission(
            {"request_id": "pending-real", "choice": "deny"},
            request_id="permission-deny-real",
        )
        assert await pending_task == "deny"

        rendered = _render_with_node(repo_root, snapshot_record)
        assert rendered["snapshot"]["pending"][0]["request_id"] == "pending-real"
        assert rendered["snapshot"]["grants"][0]["tool_family"] == "shell"
        for width in ("80", "120", "200"):
            page = rendered["widths"][width]
            assert page["bounded"] is True
            assert "权限策略中心" in page["text"]
            assert "pending-real" in page["text"]
            assert "TOOL_PERMISSIONS:bash_run" in page["text"]
            assert "范围 · 当前会话" in page["text"]
            assert "history-real" in page["text"]
    finally:
        if not pending_task.done():
            pending_task.cancel()
        await engine.shutdown()
