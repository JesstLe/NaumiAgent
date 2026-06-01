"""Structured debug trace tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from naumi_agent.cli.layout import CLIApp
from naumi_agent.debug_trace import DebugTrace


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


def test_debug_trace_can_be_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAUMI_DEBUG_TRACE", "0")

    trace = DebugTrace.create(interface="tui", base_dir=tmp_path)
    trace.output("tui.response", "hidden")
    trace.close()

    assert not trace.run_dir.exists()
    assert "禁用" in trace.describe()


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

