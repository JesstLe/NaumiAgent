"""Validate the offline lesson using real files and the production task store."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/interview_lab.py"
spec = importlib.util.spec_from_file_location("interview_lab", SCRIPT)
assert spec is not None and spec.loader is not None
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


@pytest.mark.parametrize("case", lab.CASES)
async def test_lesson_uses_persisted_tasks_and_independent_artifact_checks(tmp_path, case):
    result = await lab.run_case(case, tmp_path)
    assert result["lesson_assertions_passed"] is True
    assert result["business_goal_met"] is (case == "complete")
    run_dir = Path(result["run_directory"])
    assert (run_dir / "tasks.db").is_file()
    assert json.loads((run_dir / "receipt.json").read_text()) == result
    if case == "pending":
        assert result["first_reconciliation"] == "none"
        assert result["checks"]["artifact_matches_expected"] is True
    if case in {"missing-artifact", "tampered-artifact"}:
        assert result["checks"]["all_tasks_completed"] is True
        assert result["checks"]["artifact_matches_expected"] is False
    if case == "stale":
        assert result["first_reconciliation"] == "retry"
        assert result["second_reconciliation"] == "blocked"


async def test_concurrent_lessons_use_distinct_databases_and_preserve_existing_files(tmp_path):
    existing = tmp_path / "tasks.db"
    existing.write_text("用户原有内容", encoding="utf-8")
    results = await asyncio.gather(*(lab.run_case("complete", tmp_path) for _ in range(3)))
    assert len({result["run_directory"] for result in results}) == 3
    assert all(result["business_goal_met"] for result in results)
    assert existing.read_text(encoding="utf-8") == "用户原有内容"


async def test_unknown_case_fails_before_creating_artifacts(tmp_path):
    with pytest.raises(ValueError, match="未知"):
        await lab.run_case("../../escape", tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("raw", [b"", b"[]", b"null", b"{}", b"[1]", b"\xff", b"x" * 65537])
def test_bad_fixture_is_rejected(raw):
    with pytest.raises(ValueError):
        lab.parse_updates(raw)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("id", "../file"),
        ("date", "2026-02-30"),
        ("date", "20260911"),
        ("status", "unknown"),
        ("summary", ""),
        ("summary", "a\nb"),
        ("summary", "a" * 501),
        ("summary", 42),
    ],
)
def test_fixture_field_boundaries(key, value):
    row = {"id": "UPD-001", "date": "2026-09-11", "status": "completed", "summary": "教学"}
    row[key] = value
    with pytest.raises(ValueError):
        lab.parse_updates(json.dumps([row]).encode())


def test_duplicate_ids_and_unknown_fields_are_rejected():
    row = {"id": "UPD-001", "date": "2026-09-11", "status": "completed", "summary": "教学"}
    with pytest.raises(ValueError):
        lab.parse_updates(json.dumps([row, row]).encode())
    row["secret"] = "not-a-real-secret"
    with pytest.raises(ValueError):
        lab.parse_updates(json.dumps([row]).encode())


def test_renderer_does_not_emit_raw_html_or_markdown_links():
    row = {
        "id": "UPD-001",
        "date": "2026-09-11",
        "status": "blocked",
        "summary": "<script> [点击](https://example.invalid)",
    }
    rendered = lab.render_report([row])
    assert "<script>" not in rendered
    assert "[点击](" not in rendered
    assert "UPD-001" in rendered


def test_cli_rejects_unknown_case_without_traceback():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--case", "unknown"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
