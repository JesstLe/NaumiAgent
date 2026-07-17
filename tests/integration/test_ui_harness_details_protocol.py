from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.config.settings import MemoryConfig
from naumi_agent.harness.completion import HarnessCompletionReceipt
from naumi_agent.harness.models import HarnessCompletionContract, HarnessTaskKind
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.memory.session import Session, SessionStore
from naumi_agent.runs.models import CompletionReceipt
from naumi_agent.runs.store import ChatRunStore
from naumi_agent.ui.bridge import JsonlEngineBridge
from naumi_agent.ui.protocol import ClientEventType


class _BridgeEngine:
    def __init__(
        self,
        service: HarnessService,
        *,
        workspace_root: Path | None = None,
        session_store: SessionStore | None = None,
        chat_run_store: ChatRunStore | None = None,
    ) -> None:
        self.harness_service = service
        self.usage = SimpleNamespace(
            total_input_tokens=0,
            total_output_tokens=0,
            turns=0,
        )
        self.router = object()
        self.runtime_mode = "default"
        self.permission_mode = "moderate"
        self.workspace_root = workspace_root or Path.cwd()
        self.session_store = session_store
        self.chat_run_store = chat_run_store
        self._session = None

    def set_permission_confirmer(self, _confirmer: object) -> None:
        return None

    def set_user_interaction_handler(self, _handler: object) -> None:
        return None

    async def load_session(self, session_id: str) -> bool:
        if self.session_store is None:
            return False
        self._session = await self.session_store.load(session_id)
        return self._session is not None


async def _persist_completed_run(
    store: HarnessStore,
    *,
    workspace: Path,
    run_id: str,
    session_id: str | None = None,
) -> None:
    contract = HarnessCompletionContract(
        run_id=run_id,
        session_id=session_id or f"session:{run_id}",
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


def _render_compact_card_with_real_node(
    repo_root: Path,
    records: list[dict[str, object]],
) -> dict[str, object]:
    source = r"""
import { stripAnsi, visibleWidth } from "./frontend/terminal-ui/src/ansi.js";
import { renderMessage } from "./frontend/terminal-ui/src/components/message.js";
import { normalizeServerRecord } from "./frontend/terminal-ui/src/protocol.js";
import { createInitialState, reduceServerEvent } from "./frontend/terminal-ui/src/state.js";
let input = "";
for await (const chunk of process.stdin) input += chunk;
const records = input.trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
const state = createInitialState();
for (const record of records) reduceServerEvent(state, normalizeServerRecord(record));
const receipts = state.messages.filter((message) => message.kind === "completion_receipt");
const widths = {};
for (const width of [80, 120, 200]) {
  const lines = renderMessage(receipts[0], width, { width, state });
  widths[width] = {
    bounded: lines.every((line) => visibleWidth(line) <= width),
    text: lines.map(stripAnsi).join("\n"),
  };
}
process.stdout.write(JSON.stringify({
  receiptCount: receipts.length,
  harnessStatus: receipts[0]?.harnessReceipt?.status ?? "",
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


@pytest.mark.asyncio
async def test_real_bridge_node_reducer_renders_one_combined_completion_card() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    writer = io.StringIO()
    bridge = JsonlEngineBridge(_BridgeEngine(object()), config_path="config.yaml")  # type: ignore[arg-type]
    bridge.bind_writer(writer)
    run_id = "compact-real-run"
    harness_payload = {
        "run_id": run_id,
        "status": "completed_unverified",
        "task_kind": "change",
        "changed_files": ["src/app.py"],
        "checks": [
            {"id": "unit", "status": "failed"},
            {"id": "integration", "status": "infrastructure_error"},
        ],
        "criteria": [
            {
                "id": "tests",
                "status": "unsatisfied",
                "evidence_ids": ["check:unit"],
            }
        ],
        "warnings": ["集成环境不可用"],
        "tree_fingerprint": "c" * 64,
    }
    generic_payload = CompletionReceipt.from_dict(
        {
            "schema_version": 1,
            "receipt_id": "compact-real-receipt",
            "run_id": run_id,
            "outcome": "partial",
            "summary": "实现完成，定向验证存在已披露问题。",
            "duration_ms": 1250,
            "risks": [
                {
                    "code": "validation_failed",
                    "level": "high",
                    "message": "定向验证未全部通过。",
                }
            ],
            "git_state": {"available": True, "branch": "main", "dirty": True},
        }
    ).to_dict()

    await bridge.handle_engine_event("harness_completion_receipt", harness_payload)
    await bridge.handle_engine_event("completion_receipt", generic_payload)

    records = _records(writer)
    assert any(record["type"] == "harness/receipt" for record in records)
    assert any(record["type"] == "completion/receipt" for record in records)
    assert not any(
        record["type"] == "ui/message"
        and record["payload"].get("title") == "Harness 完成回执"  # type: ignore[union-attr]
        for record in records
    )

    rendered = _render_compact_card_with_real_node(repo_root, records)
    assert rendered["receiptCount"] == 1
    assert rendered["harnessStatus"] == "completed_unverified"
    for width in ("80", "120", "200"):
        snapshot = rendered["widths"][width]  # type: ignore[index]
        assert snapshot["bounded"] is True
        assert "Harness 未验证" in snapshot["text"]
        assert "检查失败 · unit" in snapshot["text"]
        assert "基础设施异常 · integration" in snapshot["text"]


@pytest.mark.asyncio
async def test_new_bridge_recovers_one_combined_receipt_from_real_stores(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other-workspace"
    workspace.mkdir()
    other_workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)

    state_dir = tmp_path / "state"
    session_db = state_dir / "sessions.db"
    chat_db = state_dir / "chat-runs.db"
    harness_db = state_dir / "harness.db"
    session_id = "session-real-resume"
    run_id = "run-real-resume"

    session_writer = SessionStore(MemoryConfig(session_db_path=str(session_db)))
    session = Session(
        id=session_id,
        title="真实恢复会话",
        workspace_root=str(workspace),
    )
    session.add_message("user", "恢复上一轮结果")
    session.add_message("assistant", "上一轮已完成并验证。")
    await session_writer.save(session)
    await session_writer.close()

    chat_writer = ChatRunStore(chat_db)
    await chat_writer.start_run(
        session_id=session_id,
        user_message_id="message-real-resume",
        run_id=run_id,
    )
    generic_receipt = CompletionReceipt.from_dict(
        {
            "schema_version": 1,
            "receipt_id": "receipt-real-resume",
            "run_id": run_id,
            "outcome": "completed",
            "summary": "历史运行已通过 Harness 验证。",
            "git_state": {"available": True, "branch": "main", "dirty": False},
        }
    )
    await chat_writer.finish_run(
        run_id,
        status="completed",
        receipt=generic_receipt,
    )

    harness_writer = HarnessStore(harness_db)
    await _persist_completed_run(
        harness_writer,
        workspace=workspace,
        run_id=run_id,
        session_id=session_id,
    )
    await _persist_completed_run(
        harness_writer,
        workspace=other_workspace,
        run_id="cross-workspace-resume",
        session_id=session_id,
    )

    service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(state_dir / "trust-reader.db"),
        store=HarnessStore(harness_db),
    )
    engine = _BridgeEngine(
        service,
        workspace_root=workspace,
        session_store=SessionStore(MemoryConfig(session_db_path=str(session_db))),
        chat_run_store=ChatRunStore(chat_db),
    )
    writer = io.StringIO()
    bridge = JsonlEngineBridge(engine, config_path="config.yaml")  # type: ignore[arg-type]
    bridge.bind_writer(writer)

    await bridge.resume_session(
        {"session_id": session_id, "clear": True},
        request_id="resume-real-stores",
    )

    records = _records(writer)
    receipts = [
        record
        for record in records
        if record["type"] in {"harness/receipt", "completion/receipt"}
    ]
    assert [record["type"] for record in receipts] == [
        "harness/receipt",
        "completion/receipt",
    ]
    assert receipts[0]["payload"]["run_id"] == run_id  # type: ignore[index]
    assert all(record["request_id"] == "resume-real-stores" for record in receipts)

    rendered = _render_compact_card_with_real_node(repo_root, records)
    assert rendered["receiptCount"] == 1
    assert rendered["harnessStatus"] == "completed_verified"
    for width in ("80", "120", "200"):
        snapshot = rendered["widths"][width]  # type: ignore[index]
        assert snapshot["bounded"] is True
        assert "Harness 已验证" in snapshot["text"]

    await engine.session_store.close()  # type: ignore[union-attr]
