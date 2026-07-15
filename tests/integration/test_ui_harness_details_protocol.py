from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from naumi_agent.harness.completion import HarnessCompletionReceipt
from naumi_agent.harness.models import HarnessCompletionContract, HarnessTaskKind
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.ui.bridge import JsonlEngineBridge
from naumi_agent.ui.protocol import ClientEventType


class _BridgeEngine:
    def __init__(self, service: HarnessService) -> None:
        self.harness_service = service

    def set_permission_confirmer(self, _confirmer: object) -> None:
        return None

    def set_user_interaction_handler(self, _handler: object) -> None:
        return None


async def _persist_completed_run(
    store: HarnessStore,
    *,
    workspace: Path,
    run_id: str,
) -> None:
    contract = HarnessCompletionContract(
        run_id=run_id,
        session_id=f"session:{run_id}",
        task_kind=HarnessTaskKind.CHANGE,
        objective="验证 Harness 类型化详情协议",
    )
    await store.start_run(
        workspace_root=workspace,
        contract=contract,
        tree_fingerprint_before="a" * 64,
        started_at="2026-07-15T10:00:00+00:00",
    )
    await store.finish_run(
        run_id=run_id,
        receipt=HarnessCompletionReceipt(
            run_id=run_id,
            status="completed_verified",
            task_kind=HarnessTaskKind.CHANGE,
            changed_files=("source.py",),
            checks=(),
            criteria=(),
            warnings=(),
            tree_fingerprint="b" * 64,
        ),
        completed_at="2026-07-15T10:01:00+00:00",
    )


def _records(writer: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in writer.getvalue().splitlines() if line]


def _normalize_with_real_node(
    repo_root: Path,
    records: list[dict[str, object]],
) -> list[dict[str, object]]:
    source = r"""
import { normalizeServerRecord } from "./frontend/terminal-ui/src/protocol.js";
let input = "";
for await (const chunk of process.stdin) input += chunk;
const records = input.trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
const normalized = records.map((record) => normalizeServerRecord(record));
process.stdout.write(JSON.stringify(normalized));
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
async def test_real_store_bridge_and_node_recover_harness_details(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other-workspace"
    workspace.mkdir()
    other_workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    (workspace / "source.py").write_text("VALUE = 1\n", encoding="utf-8")

    db_path = tmp_path / "state" / "harness.db"
    writer_store = HarnessStore(db_path)
    await _persist_completed_run(
        writer_store,
        workspace=workspace,
        run_id="detail-real-run",
    )
    await _persist_completed_run(
        writer_store,
        workspace=other_workspace,
        run_id="other-workspace-run",
    )

    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "state" / "trust.db"),
        store=HarnessStore(db_path),
    )

    async def forbidden_check(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("类型化详情查询不得执行 Harness 检查")

    service._check_runner.run = forbidden_check  # type: ignore[method-assign]
    writer = io.StringIO()
    bridge = JsonlEngineBridge(_BridgeEngine(service), config_path="config.yaml")  # type: ignore[arg-type]
    bridge.bind_writer(writer)

    await bridge.handle_client_record(
        {
            "id": "real-explain",
            "type": ClientEventType.HARNESS_EXPLAIN_REQUEST,
            "payload": {"run_id": "detail-real-run", "known_revision": 0},
        }
    )
    await bridge.handle_client_record(
        {
            "id": "real-replay",
            "type": ClientEventType.HARNESS_REPLAY_REQUEST,
            "payload": {"run_id": "detail-real-run", "known_revision": 0},
        }
    )
    await bridge.handle_client_record(
        {
            "id": "cross-workspace",
            "type": ClientEventType.HARNESS_EXPLAIN_REQUEST,
            "payload": {"run_id": "other-workspace-run", "known_revision": 0},
        }
    )

    emitted = [
        record
        for record in _records(writer)
        if record["type"] in {"harness/explain", "harness/replay"}
    ]
    assert [record["request_id"] for record in emitted] == [
        "real-explain",
        "real-replay",
        "cross-workspace",
    ]
    assert emitted[0]["payload"]["explanation"]["verified"] is True  # type: ignore[index]
    assert emitted[1]["payload"]["result"]["status"] == "reproduced"  # type: ignore[index]
    assert emitted[2]["payload"]["lookup_status"] == "not_found"  # type: ignore[index]

    normalized = _normalize_with_real_node(repo_root, emitted)
    assert normalized[0]["payload"]["run_id"] == "detail-real-run"
    assert normalized[0]["payload"]["revision"] == 1
    assert normalized[0]["payload"]["explanation"]["verified"] is True
    assert normalized[1]["payload"]["result"]["status"] == "reproduced"
    assert normalized[2]["payload"]["lookup_status"] == "not_found"
