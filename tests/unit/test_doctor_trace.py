"""Focused privacy, bounds, and filtering tests for UI-13.4a."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from naumi_agent.tools.doctor import DoctorTraceIndexTool
from naumi_agent.ui.doctor_trace import (
    DOCTOR_TRACE_MAX_QUERY_CHARS,
    DoctorTraceIndexError,
    build_doctor_trace_index,
    render_doctor_trace_index,
)


def _write_run(base: Path, run_id: str, events: list[dict]) -> Path:
    run = base / run_id
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "interface": "terminal-ui-bridge",
                "started_at": "2026-08-05T01:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (run / "events.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
    return run


def _event(name: str, data: dict, *, ts: str = "2026-08-05T01:00:00+00:00") -> dict:
    return {"ts": ts, "run_id": "run-a", "event": name, "data": data}


def test_index_folds_bodies_and_preserves_safe_identifiers(tmp_path: Path) -> None:
    secret = "sk-live-secret-user-body"
    _write_run(
        tmp_path,
        "run-a",
        [
            _event("input", {"text": secret, "request_id": "req-1"}),
            _event(
                "engine.stream_event",
                {
                    "event": "tool_start",
                    "data": {
                        "name": "bash_run",
                        "call_id": "call-7",
                        "arguments": secret,
                    },
                },
            ),
            _event(
                "exception",
                {
                    "type": "RuntimeError",
                    "where": "bridge.submit",
                    "message": secret,
                    "trace": f"Traceback {secret}",
                },
            ),
        ],
    )

    index = build_doctor_trace_index(tmp_path)
    payload = json.dumps(index.to_dict(), ensure_ascii=False)
    rendered = render_doctor_trace_index(index)

    assert index.status == "ready"
    assert len(index.entries) == 3
    assert index.entries[0].event_type == "exception"
    assert index.entries[0].severity == "error"
    assert dict(index.entries[1].identifiers)["call_id"] == "call-7"
    assert secret not in payload
    assert secret not in rendered
    assert "正文已折叠" in rendered


def test_index_folds_sensitive_values_even_when_they_match_metadata_syntax(
    tmp_path: Path,
) -> None:
    _write_run(
        tmp_path,
        "run-a",
        [
            _event(
                "custom.event",
                {
                    "source": "api-key-secret-token",
                    "where": "authorization/bearer-token",
                    "status": "ok",
                    "request_id": "secret-token",
                },
                ts="password-secret",
            )
        ],
    )

    index = build_doctor_trace_index(tmp_path)

    rendered = json.dumps(index.to_dict(), ensure_ascii=False)
    assert "api-key-secret-token" not in rendered
    assert "authorization/bearer-token" not in rendered
    assert "secret-token" not in rendered
    assert "password-secret" not in rendered
    assert "status=ok" in rendered


def test_index_filters_type_severity_and_identifier(tmp_path: Path) -> None:
    _write_run(
        tmp_path,
        "run-a",
        [
            _event("input", {"text": "hidden", "request_id": "req-one"}),
            _event("exception", {"where": "ui", "type": "ValueError"}),
        ],
    )

    by_error = build_doctor_trace_index(tmp_path, query="error")
    by_id = build_doctor_trace_index(tmp_path, query="req-one")
    by_missing = build_doctor_trace_index(tmp_path, query="not-found")

    assert [entry.event_type for entry in by_error.entries] == ["exception"]
    assert [entry.event_type for entry in by_id.entries] == ["input"]
    assert by_missing.entries == ()


def test_index_is_bounded_newest_first_and_reports_malformed_lines(tmp_path: Path) -> None:
    run = _write_run(
        tmp_path,
        "run-a",
        [_event("phase", {"phase": f"step-{index}"}) for index in range(5)],
    )
    with (run / "events.jsonl").open("ab") as handle:
        handle.write(b"not-json\n")

    index = build_doctor_trace_index(tmp_path, limit=2)

    assert index.status == "degraded"
    assert index.diagnostic_code == "trace_malformed_lines"
    assert index.malformed_line_count == 1
    assert index.truncated is True
    assert [entry.summary for entry in index.entries] == [
        "phase=step-4",
        "phase=step-3",
    ]


def test_index_selects_latest_or_exact_safe_run(tmp_path: Path) -> None:
    older = _write_run(tmp_path, "run-old", [_event("old", {})])
    newer = _write_run(tmp_path, "run-new", [_event("new", {})])
    old_time = older.stat().st_mtime_ns
    (newer / "events.jsonl").touch()
    assert (newer / "events.jsonl").stat().st_mtime_ns >= old_time

    latest = build_doctor_trace_index(tmp_path)
    exact = build_doctor_trace_index(tmp_path, preferred_run_id="run-old")

    assert latest.run_id == "run-new"
    assert exact.run_id == "run-old"


def test_index_rejects_missing_root_unsafe_run_and_oversized_query(tmp_path: Path) -> None:
    with pytest.raises(DoctorTraceIndexError) as missing:
        build_doctor_trace_index(tmp_path / "missing")
    assert missing.value.code == "trace_root_missing"

    _write_run(tmp_path, "run-a", [_event("event", {})])
    with pytest.raises(DoctorTraceIndexError) as unsafe:
        build_doctor_trace_index(tmp_path, preferred_run_id="../escape")
    assert unsafe.value.code == "trace_run_id_invalid"

    with pytest.raises(DoctorTraceIndexError) as oversized:
        build_doctor_trace_index(tmp_path, query="x" * (DOCTOR_TRACE_MAX_QUERY_CHARS + 1))
    assert oversized.value.code == "trace_query_too_long"


def test_index_does_not_follow_manifest_symlink_outside_run(tmp_path: Path) -> None:
    run = _write_run(tmp_path, "run-a", [_event("event", {})])
    external = tmp_path / "external-manifest.json"
    external.write_text(
        json.dumps({"run_id": "secret-token", "interface": "password-secret"}),
        encoding="utf-8",
    )
    (run / "manifest.json").unlink()
    (run / "manifest.json").symlink_to(external)

    index = build_doctor_trace_index(tmp_path)

    assert index.run_id == "run-a"
    assert index.interface == "unknown"
    assert "secret-token" not in json.dumps(index.to_dict())


def test_index_snapshot_is_deterministic_for_unchanged_file(tmp_path: Path) -> None:
    _write_run(tmp_path, "run-a", [_event("phase", {"phase": "planning"})])

    first = build_doctor_trace_index(tmp_path, query="planning")
    second = build_doctor_trace_index(tmp_path, query="planning")

    assert first == second
    assert len(first.snapshot_sha256) == 64


@pytest.mark.asyncio
async def test_agent_tool_uses_latest_configured_run_without_arbitrary_path(
    tmp_path: Path,
) -> None:
    debug_runs = tmp_path / "debug-runs"
    _write_run(debug_runs, "run-a", [_event("exception", {"message": "private"})])
    engine = SimpleNamespace(
        _config=SimpleNamespace(
            memory=SimpleNamespace(session_db_path=str(tmp_path / "sessions.db")),
        ),
    )
    tool = DoctorTraceIndexTool(engine)

    rendered = await tool.execute(query="error", limit=10)

    assert "Doctor Trace 索引" in rendered
    assert "exception" in rendered
    assert "private" not in rendered
    assert tool.metadata.read_only is True
    assert tool.metadata.concurrency_safe is True
