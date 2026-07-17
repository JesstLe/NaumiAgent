from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from naumi_agent.tasks.models import TaskStatus
from naumi_agent.tasks.store import TaskStore
from naumi_agent.ui.bridge import JsonlEngineBridge
from naumi_agent.ui.protocol import ClientEventType
from naumi_agent.workbench.models import RiskLevel
from naumi_agent.workbench.service import WorkbenchService
from naumi_agent.workbench.store import WorkbenchStore


class _WorkbenchBridgeEngine:
    def __init__(self, service: WorkbenchService, session_id: str) -> None:
        self.workbench_service = service
        self._session = SimpleNamespace(id=session_id)
        self.workspace_root = Path.cwd()

    def set_permission_confirmer(self, _confirmer: Any) -> None:
        return None

    def set_user_interaction_handler(self, _handler: Any) -> None:
        return None

    async def get_or_create_session(self) -> Any:
        return self._session


def _records(writer: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in writer.getvalue().splitlines() if line]


def _reduce_with_real_node(repo_root: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    source = r"""
import { normalizeServerRecord } from "./frontend/terminal-ui/src/protocol.js";
import { createInitialState, reduceServerEvent } from "./frontend/terminal-ui/src/state.js";
import { stripAnsi, visibleWidth } from "./frontend/terminal-ui/src/ansi.js";
import {
  renderWorkbenchOverview,
} from "./frontend/terminal-ui/src/components/workbench-overview.js";
let input = "";
for await (const chunk of process.stdin) input += chunk;
const state = createInitialState();
state.currentSessionId = "session-workbench-real";
for (const line of input.trim().split("\n").filter(Boolean)) {
  reduceServerEvent(state, normalizeServerRecord(JSON.parse(line)));
}
const widths = {};
for (const width of [80, 120, 200]) {
  const lines = renderWorkbenchOverview(state.workbench, width, 24);
  widths[width] = {
    bounded: lines.every((line) => visibleWidth(line) <= width),
    text: lines.map(stripAnsi).join("\n"),
  };
}
process.stdout.write(JSON.stringify({
  streamId: state.workbench.stream_id,
  revision: state.workbench.revision,
  counts: state.workbench.counts,
  activeSelection: state.workbench.active_selection,
  taskStatus: state.workbench.tasks[0]?.status ?? "",
  loading: state.workbench.loading,
  widths,
}));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=repo_root,
        input="\n".join(json.dumps(record, ensure_ascii=False) for record in records),
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    return json.loads(completed.stdout)


@pytest.mark.asyncio
async def test_real_workbench_store_bridge_and_node_keep_revisioned_snapshot(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    database = tmp_path / "workbench.db"
    session_id = "session-workbench-real"
    task_store = TaskStore(database)
    task_store.set_session(session_id)
    workbench_store = WorkbenchStore(database)
    writer_service = WorkbenchService(
        task_store=task_store,
        workbench_store=workbench_store,
    )
    mission = await writer_service.create_mission(
        session_id=session_id,
        title="终端 Workbench",
        goal="验证真实快照链路",
    )
    task = await task_store.create_task("实现 UI-10.2 Overview")
    await task_store.update_task(task.id, status=TaskStatus.IN_PROGRESS)
    await writer_service.attach_issue(
        session_id=session_id,
        mission_id=mission.id,
        task_id=task.id,
        acceptance_criteria=["快照 revision 连续"],
    )
    await workbench_store.set_issue_worktree(
        session_id=session_id,
        task_id=task.id,
        worktree_name="ui-10-real",
    )
    await workbench_store.upsert_issue(
        session_id=session_id,
        task_id=task.id,
        mission_id=mission.id,
        risk_level=RiskLevel.HIGH,
        acceptance_criteria=["80/120/200 列无溢出"],
        related_branch="codex/ui-10-overview",
        related_worktree="ui-10-real",
        related_pr="#128",
    )
    await workbench_store.record_validation_run(
        session_id=session_id,
        task_id=task.id,
        actor="Validation-Agent",
        command=["node", "--test", "render.test.js"],
        cwd=str(tmp_path),
        status="passed",
        exit_code=0,
        output="1 passed",
        started_at="2026-07-17T14:00:00+08:00",
        completed_at="2026-07-17T14:00:01+08:00",
    )
    approval = await workbench_store.add_approval(
        session_id=session_id,
        mission_id=mission.id,
        task_id=task.id,
        title="审查真实链路",
        detail="只读快照",
        requester="Backend-Agent",
    )

    reader_service = WorkbenchService(
        task_store=TaskStore(database),
        workbench_store=WorkbenchStore(database),
    )
    engine = _WorkbenchBridgeEngine(reader_service, session_id)
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")  # type: ignore[arg-type]
    bridge.bind_writer(writer)

    events_before_read = await workbench_store.list_events(session_id, limit=50)
    for request_id in ("workbench-first", "workbench-duplicate"):
        await bridge.handle_client_record(
            {
                "id": request_id,
                "type": ClientEventType.WORKBENCH_REQUEST,
                "payload": {"session_id": session_id},
            }
        )
    events_after_read = await workbench_store.list_events(session_id, limit=50)
    assert [event.id for event in events_after_read] == [
        event.id for event in events_before_read
    ]
    await task_store.update_task(task.id, status=TaskStatus.COMPLETED)
    await bridge.handle_client_record(
        {
            "id": "workbench-changed",
            "type": ClientEventType.WORKBENCH_REQUEST,
            "payload": {"session_id": session_id},
        }
    )

    snapshots = [
        record for record in _records(writer) if record["type"] == "workbench/snapshot"
    ]
    assert [record["payload"]["revision"] for record in snapshots] == [1, 1, 2]
    assert len({record["payload"]["stream_id"] for record in snapshots}) == 1

    reduced = _reduce_with_real_node(repo_root, snapshots)
    assert reduced["revision"] == 2
    assert reduced["counts"] == {
        "missions": 1,
        "tasks": 1,
        "worktrees": 1,
        "reviews": 1,
        "failures": 0,
    }
    assert reduced["activeSelection"]["mission_id"] == mission.id
    assert reduced["activeSelection"]["review_id"] == approval.id
    assert reduced["taskStatus"] == "completed"
    assert reduced["loading"] is False
    for width in ("80", "120", "200"):
        rendered = reduced["widths"][width]
        assert rendered["bounded"] is True
        assert "终端 Workbench" in rendered["text"]
        assert "实现 UI-10.2 Overview" in rendered["text"]
        assert "codex/ui-10-overview" in rendered["text"]
        assert "验证通过" in rendered["text"]
        assert "高风险" in rendered["text"]
