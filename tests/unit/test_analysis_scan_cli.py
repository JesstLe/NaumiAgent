"""Tests for the deterministic analysis scan CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

FIXTURE_SOURCE = '''\
import requests

DB_HOST = "10.0.0.5"

def get_user(uid):
    try:
        return requests.get(f"http://users.internal/{uid}").json()
    except:
        return None
'''


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "naumi_agent.analysis_scan", *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _make_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "service"
    target.mkdir()
    (target / "app.py").write_text(FIXTURE_SOURCE, encoding="utf-8")
    return target


def test_chaos_scan_returns_json_evidence(tmp_path: Path) -> None:
    target = _make_fixture(tmp_path)
    result = _run("--mode", "chaos", "--target", str(target))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["mode"] == "chaos"
    assert payload["files_scanned"] == 1
    assert "裸 except" in payload["report"]
    assert "timeout" in payload["report"]


def test_scale_scan_honors_qps(tmp_path: Path) -> None:
    target = _make_fixture(tmp_path)
    result = _run("--mode", "scale", "--target", str(target), "--qps", "5000")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["qps"] == 5000
    assert payload["report"]


def test_state_scan_runs(tmp_path: Path) -> None:
    target = _make_fixture(tmp_path)
    result = _run("--mode", "state", "--target", str(target))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["mode"] == "state"
    assert "DB_HOST" in payload["report"] or payload["report"]


def test_missing_target_fails_closed(tmp_path: Path) -> None:
    result = _run("--mode", "chaos", "--target", str(tmp_path / "nope"))
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "不存在" in payload["error"]


def test_directory_without_sources_fails_closed(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run("--mode", "chaos", "--target", str(empty))
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "源码文件" in payload["error"]
