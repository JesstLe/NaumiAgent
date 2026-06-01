"""Structured debug trace tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from naumi_agent.cli import layout as cli_layout
from naumi_agent.cli.layout import CLIApp
from naumi_agent.clipboard import CopyResult
from naumi_agent.debug_trace import (
    DebugTrace,
    find_latest_run,
    list_debug_runs,
    render_debug_replay,
    render_debug_runs_index,
)


def _read_events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_debug_trace_writes_manifest_events_and_transcript(tmp_path: Path) -> None:
    trace = DebugTrace.create(
        interface="cli",
        base_dir=tmp_path,
        metadata={"workspace_root": "/tmp/workspace"},
    )

    trace.input("cli.input", "hello")
    trace.output("cli.output", "world\n")
    trace.close()

    assert trace.manifest_path.exists()
    assert trace.events_path.exists()
    assert trace.transcript_path.read_text(encoding="utf-8") == "world\n"

    manifest = json.loads(trace.manifest_path.read_text(encoding="utf-8"))
    assert manifest["interface"] == "cli"
    assert manifest["metadata"]["workspace_root"] == "/tmp/workspace"

    events = _read_events(trace.events_path)
    assert [event["event"] for event in events] == [
        "trace_started",
        "input",
        "output",
        "trace_closed",
    ]
    assert events[1]["data"]["text"] == "hello"


def test_debug_trace_describes_runtime_paths(tmp_path: Path) -> None:
    trace = DebugTrace.create(
        interface="cli",
        base_dir=tmp_path,
        metadata={
            "config_path": "/tmp/naumi/config.yaml",
            "workspace_root": "/tmp/workspace",
            "session_db_path": "/tmp/naumi/data/sessions.db",
            "vector_db_path": "/tmp/naumi/data/chroma",
            "debug_runs_dir": str(tmp_path),
        },
    )

    text = trace.describe()

    assert "运行路径" in text
    assert "- 配置文件: /tmp/naumi/config.yaml" in text
    assert "- 工作区: /tmp/workspace" in text
    assert "- 会话库: /tmp/naumi/data/sessions.db" in text
    assert "- 向量库: /tmp/naumi/data/chroma" in text
    assert f"- debug-runs: {tmp_path}" in text
    assert "最近 debug-runs" in text


def test_debug_trace_can_be_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAUMI_DEBUG_TRACE", "0")

    trace = DebugTrace.create(interface="tui", base_dir=tmp_path)
    trace.output("tui.response", "hidden")
    trace.close()

    assert not trace.run_dir.exists()
    assert "禁用" in trace.describe()


def test_debug_trace_builds_last_turn_diagnostic(tmp_path: Path) -> None:
    trace = DebugTrace.create(interface="cli", base_dir=tmp_path)
    trace.input("cli.input", "第一轮")
    trace.output("cli.output", "旧输出\n")
    trace.input("cli.input", "第二轮")
    trace.event("engine.stream_event", {"event": "tool_start", "data": {"name": "file_read"}})
    trace.output("cli.live", "新输出\n")

    text = trace.build_diagnostic_text("last")

    assert "最近一轮诊断记录" in text
    assert "第二轮" in text
    assert "新输出" in text
    assert "旧输出" not in text


def test_debug_trace_builds_error_diagnostic_from_stream_error(tmp_path: Path) -> None:
    trace = DebugTrace.create(interface="tui", base_dir=tmp_path)
    trace.input("tui.input", "触发错误")
    trace.event("engine.stream_event", {"event": "tool_start", "data": {"name": "bash_run"}})
    trace.event("engine.stream_event", {"event": "error", "data": {"message": "boom"}})
    trace.output("tui.response", "错误: boom\n")

    text = trace.build_diagnostic_text("error")

    assert "最近错误诊断记录" in text
    assert "触发错误" in text
    assert "boom" in text
    assert "bash_run" in text


def test_cli_app_records_outputs_and_submit_failure(tmp_path: Path) -> None:
    trace = DebugTrace.create(interface="cli", base_dir=tmp_path)
    cli = CLIApp(debug_trace=trace)

    cli.append_output("hello\n")
    cli.append_live("token")
    cli.record_debug_event("custom", {"ok": True})
    trace.close()

    transcript = trace.transcript_path.read_text(encoding="utf-8")
    assert "hello" in transcript
    assert "token" in transcript

    events = _read_events(trace.events_path)
    names = [event["event"] for event in events]
    assert "output" in names
    assert "custom" in names


def test_cli_copy_last_uses_debug_trace_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = DebugTrace.create(interface="cli", base_dir=tmp_path)
    trace.input("cli.input", "最近任务")
    trace.output("cli.live", "最近输出\n")
    cli = CLIApp(debug_trace=trace)
    copied: list[tuple[str, str]] = []

    def fake_copy(text: str, *, base_dir: Path, prefix: str) -> CopyResult:
        copied.append((text, prefix))
        return CopyResult(copied=True, path=base_dir / "exports" / "x.txt", message="ok")

    monkeypatch.setattr(cli_layout, "copy_or_save_transcript", fake_copy)

    message = cli.copy_transcript("last")

    assert message == "ok"
    assert copied
    assert copied[0][1] == "cli-last-diagnostic"
    assert "最近任务" in copied[0][0]


def test_render_debug_replay_accepts_run_directory(tmp_path: Path) -> None:
    trace = DebugTrace.create(interface="cli", base_dir=tmp_path)
    trace.input("cli.input", "回放输入")
    trace.event("perf_phase", {"phase": "planning", "label": "规划", "duration_ms": 12})
    trace.output("cli.output", "回放输出\n")

    text = render_debug_replay(trace.run_dir)

    assert "NaumiAgent Debug Replay" in text
    assert "回放输入" in text
    assert "回放输出" in text
    assert "planning" in text


def test_find_latest_run_returns_newest_run(tmp_path: Path) -> None:
    older = DebugTrace.create(interface="cli", base_dir=tmp_path)
    newer = DebugTrace.create(interface="tui", base_dir=tmp_path)
    newer_time = older.events_path.stat().st_mtime + 10
    os.utime(newer.events_path, (newer_time, newer_time))

    latest = find_latest_run(tmp_path)

    assert latest == newer.run_dir
    assert latest != older.run_dir


def test_debug_runs_index_lists_recent_runs_with_counts(tmp_path: Path) -> None:
    trace = DebugTrace.create(
        interface="cli",
        base_dir=tmp_path,
        metadata={"workspace_root": "/tmp/workspace"},
    )
    trace.input("cli.input", "查看日志")
    trace.event("engine.stream_event", {"event": "tool_start", "data": {"name": "bash_run"}})
    trace.event("exception", {"where": "cli.submit", "message": "boom"})
    trace.close()

    summaries = list_debug_runs(tmp_path)
    rendered = render_debug_runs_index(tmp_path)

    assert len(summaries) == 1
    assert summaries[0].interface == "cli"
    assert summaries[0].stream_event_count == 1
    assert summaries[0].exception_count == 1
    assert summaries[0].workspace == "/tmp/workspace"
    assert "最近 debug-runs" in rendered
    assert "cli" in rendered
    assert "工作区: /tmp/workspace" in rendered
    assert "/debug-replay" in rendered
