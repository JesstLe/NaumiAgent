"""Focused tests for cross-surface completion receipt export."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from naumi_agent import clipboard
from naumi_agent.cli.slash_router import execute_slash_command
from naumi_agent.config.settings import AppConfig
from naumi_agent.runs.models import CompletionReceipt
from naumi_agent.runs.store import ChatRunStore
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.ui.completion_receipt import format_completion_receipt_text
from naumi_agent.ui.completion_receipt_export import (
    CompletionReceiptExportError,
    copy_completion_receipt,
)


def _receipt(
    run_id: str,
    receipt_id: str,
    *,
    summary: str = "定向实现与验证均已完成。",
) -> CompletionReceipt:
    return CompletionReceipt.from_dict(
        {
            "schema_version": 1,
            "receipt_id": receipt_id,
            "run_id": run_id,
            "outcome": "completed",
            "summary": summary,
            "changes": [
                {
                    "path": "/private/workspace/secret-name.py",
                    "status": "modified",
                    "scope": "task",
                }
            ],
            "validations": [
                {
                    "command": "pytest tests/unit/test_target.py -q",
                    "scope": "tests/unit/test_target.py",
                    "status": "passed",
                    "passed": 3,
                }
            ],
            "git_state": {
                "available": True,
                "branch": "main",
                "dirty": True,
            },
            "evidence_refs": ["private:evidence:raw-secret"],
            "duration_ms": 1250,
        }
    )


def _engine(
    tmp_path: Path,
    store: ChatRunStore,
    *,
    session_id: str = "session-current",
    harness_store: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        _session=SimpleNamespace(id=session_id),
        chat_run_store=store,
        _harness_store=harness_store,
        _runtime_data_dir=tmp_path / ".naumi",
        workspace_root=tmp_path,
    )


async def _persist(
    store: ChatRunStore,
    *,
    session_id: str,
    run_id: str,
    receipt_id: str,
) -> CompletionReceipt:
    await store.start_run(
        session_id=session_id,
        user_message_id=f"message-{run_id}",
        run_id=run_id,
    )
    receipt = _receipt(run_id, receipt_id)
    await store.finish_run(run_id, status="completed", receipt=receipt)
    return receipt


@pytest.mark.asyncio
async def test_latest_receipt_is_copied_from_current_session_and_saved_under_runtime_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ChatRunStore(tmp_path / "chat-runs.db")
    await _persist(
        store,
        session_id="session-current",
        run_id="run-1",
        receipt_id="receipt-1",
    )
    await _persist(
        store,
        session_id="session-current",
        run_id="run-2",
        receipt_id="receipt-2",
    )
    copied: list[str] = []
    monkeypatch.setattr(
        clipboard,
        "copy_text",
        lambda text: copied.append(text) is None,
    )

    result = await copy_completion_receipt(_engine(tmp_path, store), "latest")

    assert result.receipt_id == "receipt-2"
    assert result.run_id == "run-2"
    assert result.copy_result.copied is True
    assert result.copy_result.path.parent == tmp_path / ".naumi" / "exports"
    assert copied == [result.text]
    assert "pytest tests/unit/test_target.py -q" in result.text
    assert "/private/workspace/secret-name.py" not in result.text
    assert "private:evidence:raw-secret" not in result.text


@pytest.mark.asyncio
async def test_exact_receipt_lookup_cannot_cross_session_boundary(
    tmp_path: Path,
) -> None:
    store = ChatRunStore(tmp_path / "chat-runs.db")
    await _persist(
        store,
        session_id="session-other",
        run_id="run-other",
        receipt_id="receipt-other",
    )

    with pytest.raises(
        CompletionReceiptExportError,
        match="不存在或不属于当前会话",
    ):
        await copy_completion_receipt(
            _engine(tmp_path, store),
            "receipt-other",
        )

    assert not (tmp_path / ".naumi" / "exports").exists()


@pytest.mark.asyncio
async def test_matching_harness_receipt_is_included_but_wrong_owner_is_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ChatRunStore(tmp_path / "chat-runs.db")
    receipt = await _persist(
        store,
        session_id="session-current",
        run_id="run-harness",
        receipt_id="receipt-harness",
    )

    class _HarnessReceipt:
        def model_dump(self, *, mode: str) -> dict[str, Any]:
            assert mode == "json"
            return {
                "run_id": receipt.run_id,
                "status": "completed_verified",
                "checks": [{"id": "unit", "status": "passed"}],
                "criteria": [
                    {
                        "id": "tests",
                        "status": "satisfied",
                        "evidence_ids": ["check:unit"],
                    }
                ],
                "warnings": [],
            }

    class _HarnessStore:
        def __init__(self, session_id: str) -> None:
            self.session_id = session_id

        async def get_run(self, run_id: str) -> Any:
            assert run_id == receipt.run_id
            return SimpleNamespace(
                session_id=self.session_id,
                workspace_root=str(tmp_path),
                receipt=_HarnessReceipt(),
            )

    monkeypatch.setattr(clipboard, "copy_text", lambda _text: True)
    included = await copy_completion_receipt(
        _engine(tmp_path, store, harness_store=_HarnessStore("session-current")),
        receipt.receipt_id,
    )
    excluded = await copy_completion_receipt(
        _engine(tmp_path, store, harness_store=_HarnessStore("session-other")),
        receipt.receipt_id,
    )

    assert included.harness_included is True
    assert "Harness 已验证 · 检查 1/1 · 准则 1/1 · 证据 1" in included.text
    assert excluded.harness_included is False
    assert "Harness" not in excluded.text


@pytest.mark.asyncio
async def test_harness_store_failure_degrades_explicitly_without_losing_generic_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ChatRunStore(tmp_path / "chat-runs.db")
    receipt = await _persist(
        store,
        session_id="session-current",
        run_id="run-generic",
        receipt_id="receipt-generic",
    )

    class _BrokenHarnessStore:
        async def get_run(self, _run_id: str) -> Any:
            raise OSError("private storage detail")

    monkeypatch.setattr(clipboard, "copy_text", lambda _text: False)
    result = await copy_completion_receipt(
        _engine(tmp_path, store, harness_store=_BrokenHarnessStore()),
        receipt.receipt_id,
    )

    assert result.copy_result.copied is False
    assert "已保存完成回执" in result.copy_result.message
    assert result.harness_warning
    assert "private storage detail" not in result.text
    assert "定向实现与验证均已完成" in result.text


@pytest.mark.asyncio
async def test_malformed_harness_projection_degrades_without_aborting_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ChatRunStore(tmp_path / "chat-runs.db")
    receipt = await _persist(
        store,
        session_id="session-current",
        run_id="run-malformed-harness",
        receipt_id="receipt-malformed-harness",
    )

    class _MalformedReceipt:
        def model_dump(self, *, mode: str) -> dict[str, Any]:
            raise ValueError(f"private malformed {mode}")

    class _MalformedHarnessStore:
        async def get_run(self, _run_id: str) -> Any:
            return SimpleNamespace(
                session_id="session-current",
                workspace_root=str(tmp_path),
                receipt=_MalformedReceipt(),
            )

    monkeypatch.setattr(clipboard, "copy_text", lambda _text: True)
    result = await copy_completion_receipt(
        _engine(tmp_path, store, harness_store=_MalformedHarnessStore()),
        receipt.receipt_id,
    )

    assert result.harness_included is False
    assert result.harness_warning
    assert "private malformed" not in result.text
    assert result.copy_result.copied is True


@pytest.mark.asyncio
async def test_export_requires_active_session_and_runtime_directory(
    tmp_path: Path,
) -> None:
    store = ChatRunStore(tmp_path / "chat-runs.db")
    engine = _engine(tmp_path, store)
    engine._session = None
    with pytest.raises(CompletionReceiptExportError, match="没有活动会话"):
        await copy_completion_receipt(engine)

    receipt = await _persist(
        store,
        session_id="session-current",
        run_id="run-no-dir",
        receipt_id="receipt-no-dir",
    )
    engine._session = SimpleNamespace(id="session-current")
    engine._runtime_data_dir = None
    with pytest.raises(CompletionReceiptExportError, match="安全的回执导出目录"):
        await copy_completion_receipt(engine, receipt.receipt_id)


@pytest.mark.asyncio
async def test_shared_slash_route_copies_receipt_without_frontend_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ChatRunStore(tmp_path / "chat-runs.db")
    await _persist(
        store,
        session_id="session-current",
        run_id="run-slash",
        receipt_id="receipt-slash",
    )
    monkeypatch.setattr(clipboard, "copy_text", lambda _text: True)

    output = await execute_slash_command(
        _engine(tmp_path, store),
        "/copy receipt latest",
        frontend=None,
    )

    assert "已复制完成回执" in output
    assert "当前界面不支持" not in output


@pytest.mark.asyncio
async def test_shared_slash_route_rejects_extra_receipt_arguments_without_export(
    tmp_path: Path,
) -> None:
    store = ChatRunStore(tmp_path / "chat-runs.db")
    await _persist(
        store,
        session_id="session-current",
        run_id="run-invalid",
        receipt_id="receipt-invalid",
    )

    output = await execute_slash_command(
        _engine(tmp_path, store),
        "/copy receipt receipt-invalid extra",
        frontend=None,
    )

    assert "用法：/copy receipt" in clipboard.strip_ansi(output)
    assert not (tmp_path / ".naumi" / "exports").exists()


@pytest.mark.asyncio
async def test_real_engine_composition_exports_persisted_receipt_after_session_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = create_agent_engine(
        AppConfig(
            workspace_root=str(workspace),
            memory={
                "session_db_path": str(workspace / ".naumi" / "sessions.db"),
                "vector_db_path": str(workspace / ".naumi" / "vectors"),
                "long_term_enabled": False,
            },
        )
    )
    monkeypatch.setattr(clipboard, "copy_text", lambda _text: False)
    try:
        session = await engine.get_or_create_session(title="真实回执复制场景")
        await engine.chat_run_store.start_run(
            session_id=session.id,
            user_message_id="message-real",
            run_id="run-real",
        )
        receipt = _receipt(
            "run-real",
            "receipt-real",
            summary="真实 Composition Root 已完成回执导出。",
        )
        await engine.chat_run_store.finish_run(
            "run-real",
            status="completed",
            receipt=receipt,
        )

        output = await execute_slash_command(
            engine,
            "/copy receipt receipt-real",
            frontend=None,
        )

        exports = list(
            (workspace / ".naumi" / "exports").glob(
                "completion-receipt-*.txt"
            )
        )
        assert "已保存完成回执" in output
        assert len(exports) == 1
        assert "真实 Composition Root 已完成回执导出。" in exports[0].read_text(
            encoding="utf-8"
        )
    finally:
        await engine.shutdown()


def test_receipt_action_strips_terminal_control_sequences_from_identifier() -> None:
    receipt = _receipt(
        "run-safe",
        "receipt-\x1b]8;;https://evil.invalid\x07link\x1b]8;;\x07-\x1b[31mred",
    )

    rendered = format_completion_receipt_text(receipt).plain

    assert "\x1b" not in rendered
    assert "evil.invalid" not in rendered
    assert "/copy receipt receipt-link-red" in rendered


def test_receipt_action_quotes_identifier_with_spaces() -> None:
    rendered = format_completion_receipt_text(
        _receipt("run-quoted", "receipt with spaces")
    ).plain

    assert "/copy receipt 'receipt with spaces'" in rendered


def test_receipt_projection_redacts_secret_like_public_text() -> None:
    receipt = _receipt(
        "run-redacted",
        "receipt-redacted",
        summary="token=abcdefghijklmnopqrstuvwxyz123456",
    )

    rendered = format_completion_receipt_text(receipt).plain

    assert "abcdefghijklmnopqrstuvwxyz123456" not in rendered
    assert "token=[REDACTED]" in rendered
