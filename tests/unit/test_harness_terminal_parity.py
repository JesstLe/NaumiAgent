"""HAR-07.6 shared Harness receipt/detail parity tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from textual.widgets import Input

from naumi_agent.config.settings import AppConfig
from naumi_agent.orchestrator.engine import AgentResult, AgentUsage
from naumi_agent.runtime.composition import create_agent_engine
from naumi_agent.tui import app as tui_app
from naumi_agent.tui.app import NaumiApp
from naumi_agent.tui.completion_receipt import format_completion_receipt_text
from naumi_agent.ui.harness_detail import (
    render_harness_detail_markdown,
    render_harness_evidence_markdown,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GOLDEN = json.loads(
    (
        PROJECT_ROOT
        / "tests"
        / "fixtures"
        / "har07"
        / "terminal-parity-golden.json"
    ).read_text(encoding="utf-8")
)


def test_tui_receipt_and_detail_cover_shared_harness_golden() -> None:
    receipt = format_completion_receipt_text(
        GOLDEN["completion_receipt"],
        GOLDEN["harness_receipt"],
    ).plain
    detail = render_harness_detail_markdown(
        GOLDEN["explain"],
        GOLDEN["replay"],
    )
    evidence = render_harness_evidence_markdown(GOLDEN["explain"])

    for fragment in GOLDEN["expected_receipt_fragments"]:
        assert fragment in receipt
    for fragment in GOLDEN["expected_detail_fragments"]:
        assert fragment in detail
    for fragment in GOLDEN["expected_evidence_focus_fragments"]:
        assert fragment in evidence
    assert "private" not in f"{receipt}\n{detail}\n{evidence}"

    customized = format_completion_receipt_text(
        GOLDEN["completion_receipt"],
        GOLDEN["harness_receipt"],
        detail_shortcut="Ctrl+U",
    ).plain
    assert "Ctrl+U 查看详情" in customized
    assert "Ctrl+O 查看详情" not in customized


@pytest.mark.asyncio
async def test_live_tui_merges_harness_receipt_into_single_completion_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig(
        workspace_root=str(tmp_path),
        memory={
            "session_db_path": str(tmp_path / "sessions.db"),
            "vector_db_path": str(tmp_path / "chroma"),
            "long_term_enabled": False,
        },
    )
    engine = create_agent_engine(config)
    captured: list[tuple[str, str]] = []
    real_formatter = format_completion_receipt_text

    def capture_formatter(value, harness_receipt=None, **kwargs):
        rendered = real_formatter(value, harness_receipt, **kwargs)
        captured.append((value.run_id, rendered.plain))
        return rendered

    async def run_streaming(_task: str, sink) -> AgentResult:
        await sink._callback(  # noqa: SLF001
            "harness_completion_receipt",
            GOLDEN["harness_receipt"],
        )
        await sink._callback(  # noqa: SLF001
            "completion_receipt",
            GOLDEN["completion_receipt"],
        )
        return AgentResult(
            status="completed",
            response="完成",
            usage=AgentUsage(turns=1),
        )

    monkeypatch.setattr(tui_app, "format_completion_receipt_text", capture_formatter)
    monkeypatch.setattr(engine, "run_streaming", run_streaming)
    routed: list[str] = []
    monkeypatch.setattr(
        NaumiApp,
        "_run_cli_slash_command",
        lambda _self, text: routed.append(text),
    )
    app = NaumiApp(engine)

    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one("#msg-input", Input)
        composer.value = "验证 Harness 回执合并"
        composer.focus()
        await pilot.press("enter")
        for _ in range(60):
            if captured and not app._agent_busy:  # noqa: SLF001
                break
            await pilot.pause(0.05)
        else:
            pytest.fail("TUI 未在时限内渲染合并完成回执")
        app.action_open_latest_harness_detail()
        await pilot.pause()

    assert len(captured) == 1
    assert captured[0][0] == "run-parity-1"
    assert "Harness 未验证" in captured[0][1]
    assert "Ctrl+O 查看详情" in captured[0][1]
    assert "/harness detail run-parity-1" in captured[0][1]
    assert routed == ["/harness detail run-parity-1"]
    assert app._pending_harness_receipts == {}  # noqa: SLF001
    assert app._latest_harness_detail_run_id == "run-parity-1"  # noqa: SLF001


def test_tui_harness_receipt_rejects_untyped_collection_shapes() -> None:
    rendered = format_completion_receipt_text(
        GOLDEN["completion_receipt"],
        {
            "status": "completed_unverified",
            "checks": "private-check",
            "criteria": {"private-criterion": "private"},
            "warnings": "private-warning",
        },
    ).plain

    assert "检查 0/0" in rendered
    assert "准则 0/0" in rendered
    assert "private" not in rendered


@pytest.mark.asyncio
async def test_tui_direct_detail_without_paired_receipt_stays_local(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        workspace_root=str(tmp_path),
        memory={
            "session_db_path": str(tmp_path / "sessions.db"),
            "vector_db_path": str(tmp_path / "chroma"),
            "long_term_enabled": False,
        },
    )
    app = NaumiApp(create_agent_engine(config))

    async with app.run_test(size=(100, 30)) as pilot:
        app.action_open_latest_harness_detail()
        await pilot.pause()
        assert app.query_one(tui_app.StatusBar).status_text == (
            "当前没有可查看的 Harness 完成回执。"
        )
