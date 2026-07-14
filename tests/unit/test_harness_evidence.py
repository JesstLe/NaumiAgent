from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from naumi_agent.config.settings import AppConfig, MemoryConfig
from naumi_agent.harness.evidence import EvidenceCollector
from naumi_agent.harness.models import HarnessCompletionContract, HarnessTaskKind
from naumi_agent.harness.service import HarnessService
from naumi_agent.harness.store import HarnessStore
from naumi_agent.harness.trust import HarnessTrustStore
from naumi_agent.orchestrator.engine import AgentEngine
from naumi_agent.tools.base import Tool, ToolMetadata

_PROFILE_DIGEST = "a" * 64
_TREE = "b" * 64
_NOW = "2026-07-15T10:00:00+08:00"


async def _store_with_run(tmp_path: Path, *, run_id: str = "evidence-run") -> HarnessStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = HarnessStore(tmp_path / "harness.db")
    await store.start_run(
        workspace_root=workspace,
        contract=HarnessCompletionContract(
            run_id=run_id,
            session_id="evidence-session",
            profile_digest=_PROFILE_DIGEST,
            task_kind=HarnessTaskKind.ANALYSIS,
            objective="收集工具执行证据",
        ),
        tree_fingerprint_before=_TREE,
        started_at=_NOW,
    )
    return store


@pytest.mark.asyncio
async def test_collector_persists_digest_only_tool_evidence(tmp_path: Path) -> None:
    store = await _store_with_run(tmp_path)
    collector = EvidenceCollector(store=store)
    secret = "brave-private-api-key-value"
    raw_result = "provider output with sk-123456789012345678901234567890"

    assert await collector.observe(
        run_id="evidence-run",
        event="tool_start",
        data={
            "call_id": "call-1",
            "name": "web_search",
            "args": json.dumps(
                {"query": "private search terms", "api_key": secret}
            ),
            "read_only": True,
            "destructive": False,
        },
    ) is None
    evidence = await collector.observe(
        run_id="evidence-run",
        event="tool_end",
        data={
            "call_id": "call-1",
            "name": "web_search",
            "status": "success",
            "duration_ms": 37,
            "content": raw_result,
        },
    )

    restored = await HarnessStore(store.db_path).get_run("evidence-run")
    assert evidence is not None
    assert evidence.kind == "tool_execution"
    assert evidence.summary == "工具 web_search 执行成功（37ms）"
    assert restored is not None
    assert len(restored.evidence) == 1
    stored = restored.evidence[0]
    assert stored.id == evidence.id
    assert stored.uri.startswith("chat-run://evidence-run/tool/tool-")
    assert stored.summary["tool_name"] == "web_search"
    assert stored.summary["status"] == "success"
    assert stored.summary["duration_ms"] == 37
    assert stored.summary["read_only"] is True
    assert stored.summary["destructive"] is False
    assert stored.summary["start_missing"] is False
    assert len(stored.summary["arguments_sha256"]) == 64
    assert len(stored.summary["result_sha256"]) == 64
    assert stored.summary["result_size_bytes"] == len(raw_result.encode("utf-8"))
    raw_database = store.db_path.read_bytes()
    assert secret.encode() not in raw_database
    assert b"private search terms" not in raw_database
    assert raw_result.encode() not in raw_database


@pytest.mark.asyncio
async def test_duplicate_tool_end_is_idempotent(tmp_path: Path) -> None:
    store = await _store_with_run(tmp_path)
    collector = EvidenceCollector(store=store)
    await collector.observe(
        run_id="evidence-run",
        event="tool_start",
        data={"call_id": "same-call", "name": "read", "args": "{}"},
    )
    end = {
        "call_id": "same-call",
        "name": "read",
        "status": "success",
        "duration_ms": 1,
        "content": "ok",
    }

    first = await collector.observe(run_id="evidence-run", event="tool_end", data=end)
    second = await collector.observe(run_id="evidence-run", event="tool_end", data=end)

    restored = await store.get_run("evidence-run")
    assert first == second
    assert restored is not None
    assert len(restored.evidence) == 1


@pytest.mark.asyncio
async def test_late_start_after_completed_call_is_ignored(tmp_path: Path) -> None:
    store = await _store_with_run(tmp_path)
    collector = EvidenceCollector(store=store)
    end = {
        "call_id": "completed-call",
        "name": "read",
        "status": "success",
        "content": "ok",
    }

    completed = await collector.observe(
        run_id="evidence-run",
        event="tool_end",
        data=end,
    )
    await collector.observe(
        run_id="evidence-run",
        event="tool_start",
        data={
            "call_id": "completed-call",
            "name": "read",
            "args": json.dumps({"path": "late.txt"}),
        },
    )

    assert await collector.observe(
        run_id="evidence-run",
        event="tool_end",
        data=end,
    ) == completed
    assert not collector._pending.get("evidence-run")  # noqa: SLF001


@pytest.mark.asyncio
async def test_result_size_is_measured_in_utf8_bytes(tmp_path: Path) -> None:
    store = await _store_with_run(tmp_path)
    collector = EvidenceCollector(store=store)
    content = "工具完成"

    await collector.observe(
        run_id="evidence-run",
        event="tool_end",
        data={
            "call_id": "unicode-result",
            "name": "read",
            "status": "success",
            "content": content,
            "content_length": len(content.encode("utf-8")),
        },
    )

    restored = await store.get_run("evidence-run")
    assert restored is not None
    assert restored.evidence[0].summary["result_size_bytes"] == len(
        content.encode("utf-8")
    )


@pytest.mark.asyncio
async def test_concurrent_calls_keep_their_own_start_digest(tmp_path: Path) -> None:
    store = await _store_with_run(tmp_path)
    collector = EvidenceCollector(store=store, max_runs=4, max_calls_per_run=32)

    await asyncio.gather(
        *(
            collector.observe(
                run_id="evidence-run",
                event="tool_start",
                data={
                    "call_id": f"call-{index}",
                    "name": "read",
                    "args": json.dumps({"path": f"file-{index}.txt"}),
                },
            )
            for index in range(20)
        )
    )
    await asyncio.gather(
        *(
            collector.observe(
                run_id="evidence-run",
                event="tool_end",
                data={
                    "call_id": f"call-{index}",
                    "name": "read",
                    "status": "success",
                    "duration_ms": index,
                    "content": f"result-{index}",
                },
            )
            for index in reversed(range(20))
        )
    )

    restored = await store.get_run("evidence-run")
    assert restored is not None
    assert len(restored.evidence) == 20
    assert len(
        {item.summary["arguments_sha256"] for item in restored.evidence}
    ) == 20
    expected_call_hashes = {
        hashlib.sha256(f"call-{index}".encode()).hexdigest()
        for index in range(20)
    }
    assert {
        item.summary["call_id_sha256"] for item in restored.evidence
    } == expected_call_hashes


@pytest.mark.asyncio
async def test_missing_start_is_explicit_and_unknown_event_is_ignored(
    tmp_path: Path,
) -> None:
    store = await _store_with_run(tmp_path)
    collector = EvidenceCollector(store=store)

    assert await collector.observe(
        run_id="evidence-run",
        event="thinking_delta",
        data={"call_id": "ignored", "content": "reasoning must not persist"},
    ) is None
    evidence = await collector.observe(
        run_id="evidence-run",
        event="tool_end",
        data={
            "call_id": "end-only",
            "name": "read",
            "status": "error",
            "duration_ms": -5,
            "content": "failed",
        },
    )

    restored = await store.get_run("evidence-run")
    assert evidence is not None
    assert restored is not None
    assert len(restored.evidence) == 1
    assert restored.evidence[0].summary["start_missing"] is True
    assert restored.evidence[0].summary["duration_ms"] == 0
    assert b"reasoning must not persist" not in store.db_path.read_bytes()


@pytest.mark.asyncio
async def test_permission_decisions_are_joined_without_reason_text(tmp_path: Path) -> None:
    store = await _store_with_run(tmp_path)
    collector = EvidenceCollector(store=store)
    await collector.observe(
        run_id="evidence-run",
        event="tool_start",
        data={"call_id": "permission-call", "name": "write", "args": "{}"},
    )
    await collector.observe(
        run_id="evidence-run",
        event="permission_bubble",
        data={
            "call_id": "permission-call",
            "tool_name": "write",
            "status": "needs_confirmation",
            "reason": "contains private approval explanation",
            "risk_level": "medium",
            "requires_confirmation": True,
        },
    )
    await collector.observe(
        run_id="evidence-run",
        event="permission_bubble",
        data={
            "call_id": "permission-call",
            "tool_name": "write",
            "status": "confirmed",
            "reason": "contains another private explanation",
            "risk_level": "medium",
            "requires_confirmation": False,
        },
    )
    await collector.observe(
        run_id="evidence-run",
        event="tool_end",
        data={
            "call_id": "permission-call",
            "name": "write",
            "status": "success",
            "content": "ok",
        },
    )

    restored = await store.get_run("evidence-run")
    assert restored is not None
    summary = restored.evidence[0].summary
    assert summary["permission_status"] == "confirmed"
    assert summary["permission_risk_level"] == "medium"
    assert summary["permission_required_confirmation"] is True
    raw_database = store.db_path.read_bytes()
    assert b"private approval explanation" not in raw_database
    assert b"another private explanation" not in raw_database


@pytest.mark.asyncio
async def test_list_refs_and_forget_run_are_bounded(tmp_path: Path) -> None:
    store = await _store_with_run(tmp_path)
    collector = EvidenceCollector(store=store, max_runs=1, max_calls_per_run=2)
    for index in range(3):
        await collector.observe(
            run_id="evidence-run",
            event="tool_end",
            data={
                "call_id": f"call-{index}",
                "name": "read",
                "status": "success",
                "content": "ok",
            },
        )

    refs = await collector.list_refs("evidence-run")

    assert len(refs) == 2
    await collector.forget_run("evidence-run")
    assert await collector.list_refs("evidence-run") == ()


class _EvidenceProbeTool(Tool):
    @property
    def name(self) -> str:
        return "evidence_probe"

    @property
    def description(self) -> str:
        return "Return one deterministic probe result."

    @property
    def parameters_schema(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(read_only=True, concurrency_safe=True)

    async def execute(self, **kwargs: object) -> str:
        return f"探针:{kwargs['value']}"


class _ConfirmedEvidenceProbeTool(_EvidenceProbeTool):
    @property
    def name(self) -> str:
        return "confirmed_evidence_probe"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            requires_confirmation=True,
        )


@pytest.mark.asyncio
async def test_engine_collects_evidence_without_ui_callback(tmp_path: Path) -> None:
    workspace = tmp_path / "engine-workspace"
    workspace.mkdir()
    profile = workspace / ".naumi" / "harness.yaml"
    profile.parent.mkdir()
    profile.write_text("schema_version: 1\n", encoding="utf-8")
    store = HarnessStore(tmp_path / "engine-harness.db")
    engine = AgentEngine(
        AppConfig(
            workspace_root=str(workspace),
            memory=MemoryConfig(
                session_db_path=str(tmp_path / "sessions.db"),
                vector_db_path=str(tmp_path / "chroma"),
                long_term_enabled=False,
            ),
        )
    )
    engine.harness_service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "engine-trust.db"),
        store=store,
    )
    engine.tool_registry.register(_EvidenceProbeTool())
    await engine.harness_service.trust(source="test")
    session = await engine.get_or_create_session()
    await engine._begin_harness_completion_run(
        "执行探针",
        run_id="engine-evidence-run",
    )

    try:
        signatures = await engine._execute_tool_calls(
            [
                {
                    "id": "engine-call",
                    "function": {
                        "name": "evidence_probe",
                        "arguments": json.dumps({"value": "real-value"}),
                    },
                }
            ],
            tool_call_history=[],
            session_id=session.id,
            turn=1,
            on_event=None,
        )

        restored = await HarnessStore(store.db_path).get_run("engine-evidence-run")
        assert signatures == [
            f'evidence_probe:{json.dumps({"value": "real-value"})}'
        ]
        assert restored is not None
        assert len(restored.evidence) == 1
        assert restored.evidence[0].summary["tool_name"] == "evidence_probe"
        assert restored.evidence[0].summary["status"] == "success"
        assert restored.evidence[0].summary["result_size_bytes"] == len(
            "探针:real-value".encode()
        )
        assert "探针:real-value".encode() not in store.db_path.read_bytes()
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_collects_repetition_guard_skip_as_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "skipped-workspace"
    workspace.mkdir()
    profile = workspace / ".naumi" / "harness.yaml"
    profile.parent.mkdir()
    profile.write_text("schema_version: 1\n", encoding="utf-8")
    store = HarnessStore(tmp_path / "skipped-harness.db")
    engine = AgentEngine(
        AppConfig(
            workspace_root=str(workspace),
            memory=MemoryConfig(
                session_db_path=str(tmp_path / "skipped-sessions.db"),
                vector_db_path=str(tmp_path / "skipped-chroma"),
                long_term_enabled=False,
            ),
        )
    )
    engine.harness_service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "skipped-trust.db"),
        store=store,
    )
    engine.tool_registry.register(_EvidenceProbeTool())
    await engine.harness_service.trust(source="test")
    session = await engine.get_or_create_session()
    await engine._begin_harness_completion_run(
        "触发重复保护",
        run_id="skipped-evidence-run",
    )
    arguments = json.dumps({"value": "repeat"})
    signature = f"evidence_probe:{arguments}"

    try:
        await engine._execute_tool_calls(
            [
                {
                    "id": "skipped-call",
                    "function": {
                        "name": "evidence_probe",
                        "arguments": arguments,
                    },
                }
            ],
            tool_call_history=[signature, signature],
            session_id=session.id,
            turn=2,
            on_event=None,
        )

        restored = await HarnessStore(store.db_path).get_run("skipped-evidence-run")
        assert restored is not None
        assert len(restored.evidence) == 1
        assert restored.evidence[0].summary["status"] == "skipped"
        assert restored.evidence[0].summary["start_missing"] is False
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_engine_forwards_permission_decision_into_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "permission-workspace"
    workspace.mkdir()
    profile = workspace / ".naumi" / "harness.yaml"
    profile.parent.mkdir()
    profile.write_text("schema_version: 1\n", encoding="utf-8")
    store = HarnessStore(tmp_path / "permission-harness.db")
    engine = AgentEngine(
        AppConfig(
            workspace_root=str(workspace),
            memory=MemoryConfig(
                session_db_path=str(tmp_path / "permission-sessions.db"),
                vector_db_path=str(tmp_path / "permission-chroma"),
                long_term_enabled=False,
            ),
        )
    )
    engine.harness_service = HarnessService(
        workspace_root=workspace,
        trust_store=HarnessTrustStore(tmp_path / "permission-trust.db"),
        store=store,
    )
    engine.tool_registry.register(_ConfirmedEvidenceProbeTool())

    async def confirm(_: dict[str, object]) -> str:
        return "allow_once"

    engine.set_permission_confirmer(confirm)
    await engine.harness_service.trust(source="test")
    session = await engine.get_or_create_session()
    await engine._begin_harness_completion_run(
        "确认后执行探针",
        run_id="permission-evidence-run",
    )

    try:
        await engine._execute_tool_calls(
            [
                {
                    "id": "permission-engine-call",
                    "function": {
                        "name": "confirmed_evidence_probe",
                        "arguments": json.dumps({"value": "confirmed"}),
                    },
                }
            ],
            tool_call_history=[],
            session_id=session.id,
            turn=3,
            on_event=None,
        )

        restored = await HarnessStore(store.db_path).get_run(
            "permission-evidence-run"
        )
        assert restored is not None
        assert len(restored.evidence) == 1
        summary = restored.evidence[0].summary
        assert summary["permission_status"] == "confirmed"
        assert summary["permission_required_confirmation"] is True
        assert summary["permission_risk_level"] == "medium"
    finally:
        await engine.shutdown()
